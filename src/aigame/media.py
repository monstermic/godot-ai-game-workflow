from __future__ import annotations

import hashlib
import io
import json
import math
import random
import re
import subprocess
import time
import tomllib
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from . import __version__
from .automation import create_agent_approval, is_ai_staging
from .core import MissingCapability, WorkflowError, fingerprint


SCHEMA_VERSION = "1.0"
DERIVED_TIME = "2000-01-01T00:00:00Z"
MEDIA_STATES = (
    "unplanned",
    "inventoried",
    "style_sample",
    "style_approval",
    "ready",
    "generating",
    "validating",
    "integrated",
    "blocked",
    "rework",
)
MEDIA_TRANSITIONS = {
    "unplanned": {"inventoried", "blocked"},
    "inventoried": {"style_sample", "rework", "blocked"},
    "style_sample": {"style_approval", "rework", "blocked"},
    "style_approval": {"ready", "rework", "blocked"},
    "ready": {"generating", "rework", "blocked"},
    "generating": {"validating", "rework", "blocked"},
    "validating": {"generating", "integrated", "rework", "blocked"},
    "integrated": {"rework"},
    "blocked": {"rework"},
    "rework": {"inventoried", "blocked"},
}
LAYER_SLOTS = [
    "shadow",
    "rear_effect",
    "rear_weapon",
    "body",
    "legs",
    "torso",
    "clothing",
    "armor",
    "head",
    "face",
    "hair",
    "front_weapon",
    "offhand",
    "front_effect",
]
DIRECTIONS = ["down", "left", "right", "up"]
BASELINE_CLIPS = [
    {"name": "idle", "frames": 4, "fps": 6.0, "loop": True, "events": []},
    {"name": "move", "frames": 6, "fps": 12.0, "loop": True, "events": []},
    {
        "name": "primary_attack",
        "frames": 6,
        "fps": 12.0,
        "loop": False,
        "events": [{"frame": 3, "event": "primary_attack"}],
    },
    {"name": "hit", "frames": 3, "fps": 12.0, "loop": False, "events": []},
    {
        "name": "death",
        "frames": 6,
        "fps": 10.0,
        "loop": False,
        "events": [{"frame": 6, "event": "hold_final"}],
    },
    {
        "name": "spawn",
        "frames": 6,
        "fps": 10.0,
        "loop": False,
        "events": [{"frame": 5, "event": "spawn_active"}],
    },
]
SCHEMA_BY_PREFIX = {
    "APL": "asset-plan.schema.json",
    "ASP": "asset-spec.schema.json",
    "STY": "style-pack.schema.json",
    "PRT": "part-spec.schema.json",
    "ANI": "animation-set.schema.json",
    "TIL": "tile-set-spec.schema.json",
    "PFX": "particle-spec.schema.json",
    "SND": "sound-spec.schema.json",
    "RCP": "media-recipe.schema.json",
}


def _project(root: Path | str) -> Path:
    project = Path(root).resolve()
    if not (project / ".aigame" / "project.toml").is_file():
        raise WorkflowError(f"Not an initialized aigame project: {project}")
    return project


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Invalid JSON at {path}: {error}") from error
    if not isinstance(value, dict):
        raise WorkflowError(f"Expected a JSON object at {path}")
    return value


def _write_bytes_if_changed(path: Path, payload: bytes) -> bool:
    if path.is_file() and path.read_bytes() == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.aigame-tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return True


def _write_json(path: Path, value: Any) -> bool:
    return _write_bytes_if_changed(
        path,
        (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8"),
    )


def _record(
    value: dict[str, Any],
    *,
    record_id: str,
    status: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created_at = str((previous or {}).get("created_at") or DERIVED_TIME)
    revision = int((previous or {}).get("revision", 1))
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": record_id,
        "revision": revision,
        "status": status,
        "created_at": created_at,
        "updated_at": created_at,
        **{
            key: item
            for key, item in value.items()
            if key
            not in {
                "schema_version",
                "id",
                "revision",
                "status",
                "created_at",
                "updated_at",
                "input_fingerprint",
            }
        },
    }
    record["input_fingerprint"] = fingerprint(record)
    return record


def _refresh(record: dict[str, Any], **changes: Any) -> dict[str, Any]:
    value = {**record, **changes}
    value["revision"] = int(record.get("revision", 0)) + 1
    value["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    value.pop("input_fingerprint", None)
    value["input_fingerprint"] = fingerprint(value)
    return value


def _schema_errors(value: dict[str, Any], schema_name: str) -> list[str]:
    schema = json.loads(files("aigame.schemas").joinpath(schema_name).read_text(encoding="utf-8"))
    return [
        f"{'.'.join(str(part) for part in error.path) or '<record>'}: {error.message}"
        for error in sorted(
            Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path)
        )
    ]


def _assert_schema(value: dict[str, Any], schema_name: str) -> None:
    errors = _schema_errors(value, schema_name)
    if errors:
        raise WorkflowError(f"{schema_name}: {'; '.join(errors)}")


def _state_path(project: Path) -> Path:
    return project / ".aigame" / "state" / "assets.json"


def _state(project: Path) -> dict[str, Any]:
    value = _load_json(_state_path(project))
    if value.get("status") not in MEDIA_STATES:
        raise WorkflowError(f"Unknown media state: {value.get('status')!r}")
    return value


def _save_state(project: Path, state: dict[str, Any], status: str, event: str) -> None:
    if status not in MEDIA_STATES:
        raise WorkflowError(f"Invalid media state: {status}")
    assert_media_transition(str(state.get("status")), status)
    updated = {**state, "status": status, "revision": int(state.get("revision", 0)) + 1}
    history = list(updated.get("history", []))
    if not history or history[-1] != event:
        history.append(event)
    updated["history"] = history
    _write_json(_state_path(project), updated)


def assert_media_transition(before: str, after: str) -> None:
    if before not in MEDIA_TRANSITIONS or after not in MEDIA_STATES:
        raise WorkflowError(f"Unknown media transition {before!r} -> {after!r}")
    if before != after and after not in MEDIA_TRANSITIONS[before]:
        raise WorkflowError(f"Forbidden media transition {before!r} -> {after!r}")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "asset"


def _safe_relative(value: str) -> Path:
    candidate = Path(value.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise WorkflowError(f"Unsafe media output path: {value!r}")
    if candidate.parts[0] != "assets" or len(candidate.parts) < 3:
        raise WorkflowError(f"Media output must be below assets/generated: {value!r}")
    if candidate.parts[1] != "generated":
        raise WorkflowError(f"Media output must be below assets/generated: {value!r}")
    return candidate


def _concept_records(project: Path, folder: str, pattern: str) -> list[dict[str, Any]]:
    return [_load_json(path) for path in sorted((project / "work" / "concept" / folder).glob(pattern))]


def _require_finalized_blueprint(project: Path) -> None:
    concept_state = _load_json(project / ".aigame" / "state" / "concept.json")
    if concept_state.get("status") != "finalized":
        raise WorkflowError("Asset planning requires a finalized and approved game blueprint")


def _kind_for(text: str, *, hint: str = "") -> str:
    value = f"{hint} {text}".lower()
    if any(word in value for word in ("music", "score", "stem", "theme")):
        return "music"
    if "ambience" in value or "ambient" in value:
        return "ambience"
    if any(word in value for word in ("sound", "audio", "sfx", "footstep", "ignition")):
        return "sound"
    if any(word in value for word in ("tile", "biome", "terrain", "stage atlas")):
        return "tileset"
    if any(word in value for word in ("particle", "trail", "impact", "flash", "effect")):
        return "particle"
    if any(word in value for word in ("animation", "clip", "motion")):
        return "animation"
    if any(
        word in value
        for word in ("ui", "hud", "icon", "portrait", "cursor", "menu", "credits", "tutorial", "accessibility")
    ):
        return "ui"
    return "sprite"


def _collect_needs(project: Path) -> tuple[list[dict[str, Any]], str]:
    _require_finalized_blueprint(project)
    needs: list[dict[str, Any]] = []
    content = _concept_records(project, "content", "CNT-*.json")
    mechanics = _concept_records(project, "mechanics", "MEC-*.json")
    blueprint_paths = sorted((project / "work" / "concept").glob("BLU-*.json"))
    if not blueprint_paths:
        raise WorkflowError("Finalized blueprint has no BLU record")
    blueprint = _load_json(blueprint_paths[0])

    def add(source_ref: str, purpose: str, kind: str | None = None, *, required: bool = True) -> None:
        needs.append(
            {
                "source_ref": source_ref,
                "purpose": str(purpose).strip(),
                "kind": kind or _kind_for(str(purpose)),
                "required": required,
            }
        )

    for entry in content:
        if entry.get("launch_status") == "cut":
            continue
        assets = entry.get("required_assets")
        if entry.get("launch_status") == "must" and (not isinstance(assets, list) or not assets):
            raise WorkflowError(f"{entry.get('id')}: must-have content has no required_assets mapping")
        for index, asset in enumerate(assets or []):
            purpose = f"{entry.get('canonical_name', entry.get('id'))}: {asset}"
            add(
                f"{entry.get('id')}.required_assets[{index}]",
                purpose,
                _kind_for(str(asset), hint=str(entry.get("kind", ""))),
                required=entry.get("launch_status") == "must",
            )

    feedback_fields = ("visual", "animation", "audio", "ui", "accessibility", "controller")
    for mechanic in mechanics:
        mechanic_id = str(mechanic.get("id"))
        feedback = mechanic.get("feedback")
        if not isinstance(feedback, dict):
            raise WorkflowError(f"{mechanic_id}: mechanic feedback is required for media coverage")
        for field in feedback_fields:
            value = feedback.get(field)
            if not isinstance(value, str) or not value.strip():
                raise WorkflowError(f"{mechanic_id}: feedback.{field} is required for media coverage")
            add(
                f"{mechanic_id}.feedback.{field}",
                f"{mechanic.get('name', mechanic_id)}: {value}",
                _kind_for(value, hint=field),
            )
        states = mechanic.get("states")
        if not isinstance(states, list) or not states:
            raise WorkflowError(f"{mechanic_id}: mechanic states are required for animation coverage")
        for state in states:
            add(
                f"{mechanic_id}.state.{state}",
                f"{mechanic.get('name', mechanic_id)} state animation: {state}",
                "animation",
            )

    blueprint_id = str(blueprint.get("id", "BLU-0001"))
    required_scalar = {
        "opening": "opening presentation",
        "tutorial": "tutorial visuals",
        "ending": "ending presentation",
        "credits": "credits presentation",
        "postgame": "postgame presentation",
    }
    for field, label in required_scalar.items():
        value = blueprint.get(field)
        if not value:
            raise WorkflowError(f"{blueprint_id}: {field} requires media coverage")
        add(f"{blueprint_id}.{field}", f"{label}: {value}", "ui")
    for stage in blueprint.get("progression_arc", []):
        add(
            f"{blueprint_id}.progression_arc.{stage}",
            f"Complete biome terrain atlas for {stage}",
            "tileset",
        )
    for encounter in blueprint.get("major_encounters", []):
        add(
            f"{blueprint_id}.major_encounters.{encounter}",
            f"Major encounter telegraph and defeat effect for {encounter}",
            "particle",
        )
    add(
        f"{blueprint_id}.art_direction",
        f"Representative style sample: {blueprint.get('art_direction', '')}",
        "sprite",
    )
    add(
        f"{blueprint_id}.audio_direction.music",
        f"Adaptive explore/combat/boss music: {blueprint.get('audio_direction', '')}",
        "music",
    )
    add(
        f"{blueprint_id}.audio_direction.ambience",
        f"Looping biome ambience: {blueprint.get('audio_direction', '')}",
        "ambience",
    )
    for source_ref, purpose, kind in (
        ("SYSTEM.ui.hud", "HUD frame, state markers, and objective indicators", "ui"),
        ("SYSTEM.ui.menu", "Title, pause, settings, results, and confirmation panels", "ui"),
        ("SYSTEM.ui.cursor", "Keyboard and controller focus cursors", "ui"),
        ("SYSTEM.ui.achievement", "Achievement and unlock notification icons", "ui"),
        ("SYSTEM.particle.interaction", "Generic interaction, pickup, and reward particles", "particle"),
        ("SYSTEM.sound.ui", "UI focus, accept, cancel, error, and notification sound set", "sound"),
    ):
        add(source_ref, purpose, kind)

    canonical = sorted(needs, key=lambda item: (item["source_ref"], item["kind"], item["purpose"]))
    seen_sources: set[str] = set()
    for item in canonical:
        if item["source_ref"] in seen_sources:
            raise WorkflowError(f"Duplicate media coverage source: {item['source_ref']}")
        seen_sources.add(item["source_ref"])
    source_material = {
        "concept_state": _load_json(project / ".aigame" / "state" / "concept.json"),
        "mechanics": mechanics,
        "content": content,
        "blueprint": blueprint,
    }
    return canonical, fingerprint(source_material)


def _style_record(blueprint_fingerprint: str) -> dict[str, Any]:
    return _record(
        {
            "name": "Core Top-down Pixel Media v1",
            "grid_size": 16,
            "perspective": "top_down",
            "directions": DIRECTIONS,
            "palette": {
                "transparent": "#00000000",
                "outline": "#171526ff",
                "shadow": "#29243dff",
                "dark": "#45415fff",
                "mid": "#6f6a8aff",
                "light": "#b7b2d0ff",
                "primary": "#e66b3dff",
                "secondary": "#f7c95cff",
                "danger": "#d94b64ff",
                "safe": "#58c9a3ff",
            },
            "layer_slots": LAYER_SLOTS,
            "outline": {"width_pixels": 1, "selective": True},
            "lighting": {"direction": "upper_left", "levels": 3},
            "silhouettes": {"minimum_negative_space_pixels": 2},
            "animation_defaults": {clip["name"]: clip for clip in BASELINE_CLIPS},
            "audio_vocabulary": {
                "safe": ["sine", "triangle"],
                "danger": ["square", "noise"],
                "mechanical": ["pulse", "saw"],
                "bpm": 120,
                "key": "A minor",
                "bars": 4,
            },
            "blueprint_fingerprint": blueprint_fingerprint,
        },
        record_id="STY-0001",
        status="draft",
    )


def _part_records(style: dict[str, Any]) -> list[dict[str, Any]]:
    templates = [
        ("shadow", [[5, 13, "shadow"], [6, 13, "shadow"], [7, 13, "shadow"], [8, 13, "shadow"], [9, 13, "shadow"], [10, 13, "shadow"]]),
        ("body", [[7, 6, "primary"], [8, 6, "primary"], [6, 7, "primary"], [7, 7, "light"], [8, 7, "primary"], [9, 7, "primary"]]),
        ("legs", [[6, 10, "dark"], [7, 10, "dark"], [8, 10, "dark"], [9, 10, "dark"], [6, 11, "mid"], [9, 11, "mid"]]),
        ("head", [[7, 4, "light"], [8, 4, "light"], [6, 5, "mid"], [7, 5, "light"], [8, 5, "light"], [9, 5, "mid"]]),
        ("front_weapon", [[10, 7, "secondary"], [11, 7, "secondary"], [12, 7, "light"], [13, 7, "light"]]),
        ("front_effect", [[11, 6, "danger"], [12, 5, "secondary"], [13, 4, "light"]]),
    ]
    records = []
    for index, (slot, pixels) in enumerate(templates, 1):
        direction_pixels = None
        if slot in {"front_weapon", "front_effect"}:
            direction_pixels = {
                "down": pixels,
                "up": [[x, max(0, y - 2), color] for x, y, color in pixels],
                "left": [[max(0, 15 - x), y, color] for x, y, color in pixels],
                "right": pixels,
            }
        records.append(
            _record(
                {
                    "name": f"Core {slot.replace('_', ' ').title()}",
                    "slot": slot,
                    "pixels": pixels,
                    "anchors": {
                        "origin": [8, 8],
                        "feet": [8, 13],
                        "head": [8, 4],
                        "main_hand": [10, 8],
                        "off_hand": [5, 8],
                        "muzzle": [14, 7],
                        "effect": [12, 6],
                    },
                    "occupancy_mask": pixels,
                    "occlusion_mask": [],
                    "compatible_body_families": ["humanoid", "compact_enemy"],
                    "compatible_animations": [clip["name"] for clip in BASELINE_CLIPS],
                    "compatible_directions": DIRECTIONS,
                    "mirror_safe": slot not in {"front_weapon", "front_effect"},
                    "direction_pixels": direction_pixels,
                    "license": "CC0-1.0",
                    "sha256": fingerprint({"style": style["input_fingerprint"], "pixels": pixels}),
                    "provenance": {
                        "method": "original procedural coordinates",
                        "source_pack": "core-topdown-v1",
                    },
                },
                record_id=f"PRT-{index:04d}",
                status="approved",
            )
        )
    return records


def _output_paths(kind: str, purpose: str, index: int, variants: int = 1) -> list[str]:
    name = f"{index:04d}-{_slug(purpose)[:64]}"
    folder = {
        "sprite": "sprites",
        "animation": "animations",
        "tileset": "tilesets",
        "particle": "particles",
        "ui": "ui",
        "sound": "audio/sfx",
        "ambience": "audio/ambience",
        "music": "audio/music",
    }[kind]
    root = f"assets/generated/{folder}/{name}"
    if kind == "music":
        return [
            f"{root}-explore.wav",
            f"{root}-combat.wav",
            f"{root}-boss.wav",
            f"{root}.tres",
        ]
    if kind in {"sound", "ambience", "music"}:
        values = [f"{root}-{variant + 1:02d}.wav" for variant in range(variants)]
        values.append(f"{root}.tres")
        return values
    if kind == "animation":
        return [f"{root}.png", f"{root}.tres"]
    if kind == "tileset":
        return [f"{root}.png", f"{root}.tres"]
    if kind == "particle":
        return [f"{root}.png", f"{root}.tscn", f"{root}-cpu.tscn"]
    return [f"{root}.png"]


def _external_part_records(project: Path, builtin_ids: set[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen = set(builtin_ids)
    packs_root = project / "assets" / "source" / "packs"
    for path in sorted(packs_root.glob("*/parts/PRT-*.json")):
        if path.parent.parent.name == "core-topdown-v1":
            continue
        record = _load_json(path)
        _assert_schema(record, "part-spec.schema.json")
        if not _validate_fingerprint(record):
            raise WorkflowError(f"External source part fingerprint is invalid: {path}")
        part_id = str(record["id"])
        if part_id in seen:
            raise WorkflowError(f"Duplicate source part id {part_id} at {path}")
        if record.get("status") != "approved":
            raise WorkflowError(f"External source part must be approved before planning: {part_id}")
        seen.add(part_id)
        records.append(record)
    return records


def _effective_output_license(parts: list[dict[str, Any]]) -> str:
    licenses = {str(part.get("license")) for part in parts}
    for candidate in ("proprietary", "commercial", "CC-BY-4.0", "original", "Apache-2.0"):
        if candidate in licenses:
            return candidate
    return "CC0-1.0"


def _records_for_plan(
    needs: list[dict[str, Any]],
    blueprint_fingerprint: str,
    external_parts: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]] | dict[str, Any]]:
    style = _style_record(blueprint_fingerprint)
    core_parts = _part_records(style)
    parts = sorted(
        [*core_parts, *external_parts],
        key=lambda part: (
            LAYER_SLOTS.index(str(part.get("slot")))
            if str(part.get("slot")) in LAYER_SLOTS
            else len(LAYER_SLOTS),
            str(part.get("id")),
        ),
    )
    specs: list[dict[str, Any]] = []
    recipes: list[dict[str, Any]] = []
    animations: list[dict[str, Any]] = []
    tiles: list[dict[str, Any]] = []
    particles: list[dict[str, Any]] = []
    sounds: list[dict[str, Any]] = []
    coverage = []
    animation_index = tile_index = particle_index = sound_index = 0
    for index, need in enumerate(needs, 1):
        asset_id = f"ASP-{index:04d}"
        recipe_id = f"RCP-{index:04d}"
        kind = str(need["kind"])
        variants = 3 if kind in {"sound", "music"} else 1
        outputs = _output_paths(kind, str(need["purpose"]), index, variants)
        source_part_ids = [record["id"] for record in parts] if kind in {"sprite", "animation"} else []
        parameters: dict[str, Any] = {
            "grid_size": 16,
            "palette_id": style["id"],
            "palette": style["palette"],
            "purpose": need["purpose"],
            "output_license": "CC0-1.0",
        }
        if kind in {"sprite", "animation"}:
            layer_choices = {
                slot: [part["id"] for part in parts if part.get("slot") == slot]
                for slot in LAYER_SLOTS
                if any(part.get("slot") == slot for part in parts)
            }
            parameters["layer_choices"] = layer_choices
            parameters["layers"] = [
                {"slot": slot, "part_id": choices[0], "offset": [0, 0]}
                for slot, choices in layer_choices.items()
            ]
            parameters["source_part_hashes"] = {
                part["id"]: part["input_fingerprint"] for part in parts
            }
            parameters["source_part_licenses"] = {
                part["id"]: part["license"] for part in parts
            }
            parameters["output_license"] = _effective_output_license(parts)
        if kind == "animation":
            animation_index += 1
            animation_id = f"ANI-{animation_index:04d}"
            extra_state = str(need["source_ref"]).split(".state.")[-1] if ".state." in str(need["source_ref"]) else None
            clips = [dict(clip) for clip in BASELINE_CLIPS]
            if extra_state and extra_state not in {clip["name"] for clip in clips}:
                clips.append(
                    {
                        "name": _slug(extra_state).replace("-", "_"),
                        "frames": 6,
                        "fps": 12.0,
                        "loop": False,
                        "events": [{"frame": 3, "event": str(extra_state)}],
                    }
                )
            animation = _record(
                {
                    "asset_spec_id": asset_id,
                    "body_family": "humanoid",
                    "directions": DIRECTIONS,
                    "clips": clips,
                    "anchors": ["origin", "feet", "head", "main_hand", "off_hand", "muzzle", "effect"],
                },
                record_id=animation_id,
                status="planned",
            )
            animations.append(animation)
            parameters["animation_set_id"] = animation_id
            parameters["clips"] = clips
        elif kind == "tileset":
            tile_index += 1
            tile_id = f"TIL-{tile_index:04d}"
            tile_record = _record(
                {
                    "asset_spec_id": asset_id,
                    "biome": str(need["purpose"]),
                    "grid_size": 16,
                    "tiles": [
                        {"id": f"terrain-{mask:03d}", "role": "terrain", "weight": 1.0, "collision": mask != 255, "navigation": mask == 255}
                        for mask in range(256)
                    ],
                    "adjacency": {"encoding": "8-neighbor-bitmask", "opposite_edges_must_match": True},
                    "required_neighbor_masks": list(range(256)),
                    "stage_constraints": {
                        "entrance_exit_connected": True,
                        "required_encounters": True,
                        "safe_spawn_radius": 2,
                        "retry_limit": 8,
                        "derived_seed": "sha256(base_seed:attempt)",
                    },
                },
                record_id=tile_id,
                status="planned",
            )
            tiles.append(tile_record)
            parameters["tile_set_id"] = tile_id
            parameters["neighbor_masks"] = list(range(256))
        elif kind == "particle":
            particle_index += 1
            particle_id = f"PFX-{particle_index:04d}"
            particle = _record(
                {
                    "asset_spec_id": asset_id,
                    "name": str(need["purpose"]),
                    "texture": outputs[0],
                    "seed": index * 1009,
                    "amount": 24,
                    "lifetime": 0.6,
                    "fixed_fps": 12,
                    "motion": {"direction_degrees": -90, "spread_degrees": 55, "velocity": [24, 52], "gravity": [0, 20]},
                    "colors": ["#f7c95cff", "#e66b3dff", "#d94b64ff", "#00000000"],
                    "cpu_fallback": True,
                    "budget": {"max_particles": 32, "max_overdraw_cells": 4},
                },
                record_id=particle_id,
                status="planned",
            )
            particles.append(particle)
            parameters["particle_spec_id"] = particle_id
        elif kind in {"sound", "ambience", "music"}:
            sound_index += 1
            sound_id = f"SND-{sound_index:04d}"
            category = "sfx" if kind == "sound" else kind
            sound = _record(
                {
                    "asset_spec_id": asset_id,
                    "name": str(need["purpose"]),
                    "category": category,
                    "event": str(need["source_ref"]),
                    "layers": [
                        {"waveform": "square" if category == "sfx" else "triangle", "frequency": 220.0, "volume": 0.65},
                        {"waveform": "noise" if category == "sfx" else "sine", "frequency": 110.0, "volume": 0.25},
                    ],
                    "duration_seconds": 0.18 if category == "sfx" else (8.0 if category == "music" else 4.0),
                    "sample_rate": 48000,
                    "channels": 1 if category == "sfx" else 2,
                    "variants": variants,
                    "peak_dbfs": -1.0,
                    "loop": category in {"ambience", "music"},
                    "bpm": 120 if category == "music" else None,
                    "key": "A minor" if category == "music" else None,
                    "bars": 4 if category == "music" else None,
                    "stems": ["explore", "combat", "boss"] if category == "music" else [],
                    "transition_points": [0, 2, 4, 6, 8] if category == "music" else [],
                    "accessibility_alternative": "A synchronized visual state indicator communicates the same event.",
                    "mutation": {"pitch_semitones": [-0.4, 0.4], "volume_db": [-1.0, 0.0]},
                    "adsr": {"attack": 0.005, "decay": 0.03, "sustain": 0.82, "release": 0.02},
                    "pitch_envelope": {"start_semitones": 2.0 if category == "sfx" else 0.0, "end_semitones": 0.0},
                    "arpeggio": {"semitones": [0, 7, 12, 7], "step_seconds": 0.25},
                    "filter": {"type": "lowpass", "window_samples": 3},
                    "vibrato": {"rate_hz": 5.0, "depth_semitones": 0.08},
                    "distortion": {"drive": 1.1},
                    "bit_crush": {"bits": 12},
                    "delay": {"seconds": 0.04, "feedback": 0.12},
                    "panning": {"left": 0.96, "right": 1.0},
                },
                record_id=sound_id,
                status="planned",
            )
            sounds.append(sound)
            parameters["sound_spec_id"] = sound_id
            parameters["sound"] = sound
        recipe = _record(
            {
                "kind": kind,
                "asset_spec_id": asset_id,
                "seed": index * 7919,
                "source_part_ids": source_part_ids,
                "parameters": parameters,
                "outputs": outputs,
                "compiler": "aigame.pixel-media-v1",
                "compiler_version": __version__,
            },
            record_id=recipe_id,
            status="planned",
        )
        recipes.append(recipe)
        specs.append(
            _record(
                {
                    "kind": kind,
                    "purpose": need["purpose"],
                    "source_refs": [need["source_ref"]],
                    "runtime_paths": outputs,
                    "recipe_ids": [recipe_id],
                    "required": bool(need["required"]),
                    "acceptance_criteria": [
                        "Output is deterministic for the pinned recipe, source pack, and seed.",
                        "Godot loads the generated runtime resource without import errors.",
                        "License, provenance, checksum, feedback, and accessibility coverage validate.",
                    ],
                },
                record_id=asset_id,
                status="planned",
            )
        )
        coverage.append({"source_ref": need["source_ref"], "asset_spec_ids": [asset_id]})
    plan = _record(
        {
            "blueprint_fingerprint": blueprint_fingerprint,
            "style_pack_id": style["id"],
            "asset_spec_ids": [record["id"] for record in specs],
            "coverage": coverage,
            "generation_profile": "pixel-media-v1",
        },
        record_id="APL-0001",
        status="inventoried",
    )
    pack_material = {
        "schema_version": SCHEMA_VERSION,
        "id": "core-topdown-v1",
        "name": "Core Top-down 16x16",
        "license": "CC0-1.0",
        "grid_size": 16,
        "perspective": "top_down",
        "directions": DIRECTIONS,
        "layer_slots": LAYER_SLOTS,
        "body_families": ["humanoid", "compact_enemy"],
        "part_ids": [part["id"] for part in core_parts],
        "part_hashes": {part["id"]: part["input_fingerprint"] for part in core_parts},
        "provenance": "Original procedural pixel coordinates; no LPC or third-party artwork is bundled.",
    }
    pack = {**pack_material, "sha256": fingerprint(pack_material)}
    return {
        "plan": plan,
        "styles": [style],
        "parts": parts,
        "core_parts": core_parts,
        "specs": specs,
        "recipes": recipes,
        "animations": animations,
        "tiles": tiles,
        "particles": particles,
        "sounds": sounds,
        "pack": pack,
    }


def _record_destinations(project: Path, records: dict[str, Any]) -> list[tuple[Path, dict[str, Any]]]:
    destinations: list[tuple[Path, dict[str, Any]]] = [
        (project / "work" / "assets" / "APL-0001.json", records["plan"]),
    ]
    mapping = {
        "styles": "styles",
        "parts": "parts",
        "specs": "specs",
        "recipes": "recipes",
        "animations": "animations",
        "tiles": "tiles",
        "particles": "particles",
        "sounds": "sounds",
    }
    for group, folder in mapping.items():
        for record in records[group]:
            destinations.append((project / "work" / "assets" / folder / f"{record['id']}.json", record))
    return destinations


def _render_asset_docs(project: Path, records: dict[str, Any]) -> None:
    plan = records["plan"]
    specs = records["specs"]
    rows = [
        "# Asset Plan",
        "",
        f"Blueprint fingerprint: `{plan['blueprint_fingerprint']}`",
        "",
        "| ID | Kind | Required | Purpose | Sources |",
        "|---|---|---:|---|---|",
    ]
    for spec in specs:
        rows.append(
            f"| {spec['id']} | {spec['kind']} | {'yes' if spec['required'] else 'no'} | {spec['purpose']} | {', '.join(spec['source_refs'])} |"
        )
    rows.extend(
        [
            "",
            "All rows are canonical derivatives of the approved blueprint. Edit the blueprint and rerun planning; do not edit this file.",
        ]
    )
    _write_bytes_if_changed(project / "docs" / "assets" / "asset-plan.md", ("\n".join(rows) + "\n").encode("utf-8"))


def plan_assets(root: Path | str, *, apply: bool = False) -> dict[str, Any]:
    project = _project(root)
    needs, blueprint_fingerprint = _collect_needs(project)
    builtin_ids = {part["id"] for part in _part_records(_style_record(blueprint_fingerprint))}
    external_parts = _external_part_records(project, builtin_ids)
    records = _records_for_plan(needs, blueprint_fingerprint, external_parts)
    for _, record in _record_destinations(project, records):
        prefix = str(record["id"]).split("-")[0]
        _assert_schema(record, SCHEMA_BY_PREFIX[prefix])
    destinations = _record_destinations(project, records)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "dry_run" if not apply else "passed",
        "asset_plan_id": "APL-0001",
        "blueprint_fingerprint": blueprint_fingerprint,
        "counts": {
            "asset_specs": len(records["specs"]),
            "recipes": len(records["recipes"]),
            "parts": len(records["parts"]),
            "animations": len(records["animations"]),
            "tilesets": len(records["tiles"]),
            "particles": len(records["particles"]),
            "sounds": len(records["sounds"]),
        },
        "files": [str(path.relative_to(project)).replace("\\", "/") for path, _ in destinations]
        + ["assets/source/packs/core-topdown-v1/pack.json"]
        + [f"assets/source/packs/core-topdown-v1/parts/{part['id']}.json" for part in records["core_parts"]],
    }
    if not apply:
        return result
    for path, record in destinations:
        _write_json(path, record)
    _write_json(
        project / "assets" / "source" / "packs" / "core-topdown-v1" / "pack.json",
        records["pack"],
    )
    for part in records["core_parts"]:
        _write_json(
            project
            / "assets"
            / "source"
            / "packs"
            / "core-topdown-v1"
            / "parts"
            / f"{part['id']}.json",
            part,
        )
    _render_asset_docs(project, records)
    state = _state(project)
    if state.get("status") not in {"unplanned", "inventoried", "rework"}:
        _save_state(project, state, "rework", "asset inputs changed and require replanning")
        state = _state(project)
    state.update(
        {
            "asset_plan_id": "APL-0001",
            "style_pack_id": "STY-0001",
            "style_approval_id": None,
            "active_batch": None,
            "blueprint_fingerprint": blueprint_fingerprint,
        }
    )
    _save_state(project, state, "inventoried", "asset inventory derived from finalized blueprint")
    return result


def _all_records(project: Path, folder: str, prefix: str) -> list[dict[str, Any]]:
    return [_load_json(path) for path in sorted((project / "work" / "assets" / folder).glob(f"{prefix}-*.json"))]


def _approval_request(project: Path, style: dict[str, Any], sample_hashes: list[str]) -> dict[str, str]:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    commit_sha = result.stdout.strip() if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", result.stdout.strip()) else "UNCOMMITTED"
    return {
        "scope_hash": fingerprint({"style": style, "samples": sample_hashes}),
        "commit_sha": commit_sha,
        "decision": "Approve the representative pixel-art and procedural-audio style for bulk generation.",
    }


def _save_approval(project: Path, approval: dict[str, Any]) -> None:
    _write_json(project / "evidence" / "approvals" / f"{approval['id']}.json", approval)


def _representative_spec_ids(project: Path) -> list[str]:
    specs = _all_records(project, "specs", "ASP")
    selected: list[str] = []
    targets = ["character", "enemy", "tileset", "particle", "ui", "sound", "music"]
    for target in targets:
        candidate = next(
            (
                spec
                for spec in specs
                if spec["id"] not in selected
                and (target == spec["kind"] or target in str(spec["purpose"]).lower())
            ),
            None,
        )
        if candidate:
            selected.append(str(candidate["id"]))
    return selected


def _verify_human_approval(
    project: Path,
    path: Path,
    request: dict[str, str],
) -> dict[str, Any]:
    approval = _load_json(path)
    if approval.get("status") != "approved":
        raise WorkflowError("Style approval must have status approved")
    for field in ("scope_hash", "commit_sha", "decision"):
        if approval.get(field) != request[field]:
            raise WorkflowError(f"Style approval {field} does not match the current sample")
    material = {key: item for key, item in approval.items() if key != "input_fingerprint"}
    if approval.get("input_fingerprint") != fingerprint(material):
        raise WorkflowError("Style approval fingerprint is invalid")
    current = _approval_request(project, {}, [])
    if current["commit_sha"] != request["commit_sha"]:
        raise WorkflowError("Style approval commit changed; regenerate the approval request")
    return approval


def sample_assets(
    root: Path | str,
    *,
    approval_path: Path | str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    project = _project(root)
    state = _state(project)
    if state.get("status") not in {"inventoried", "style_sample", "style_approval"}:
        raise WorkflowError(f"Style sampling is not allowed from media state {state.get('status')}")
    spec_ids = _representative_spec_ids(project)
    if not apply:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "dry_run",
            "operation": "generate representative style sample",
            "asset_spec_ids": spec_ids,
        }
    if state.get("status") in {"inventoried", "style_sample"}:
        _save_state(project, state, "style_sample", "representative style sample generation started")
        state = _state(project)
    sample_result = _generate_selected(project, spec_ids, jobs=1, allow_preapproval=True)
    style_path = project / "work" / "assets" / "styles" / "STY-0001.json"
    style = _load_json(style_path)
    sample_hashes = sorted(sample_result["hashes"])
    request = (
        dict(state["pending_approval"])
        if approval_path and isinstance(state.get("pending_approval"), dict)
        else _approval_request(project, style, sample_hashes)
    )
    if is_ai_staging(project):
        approval = create_agent_approval(project, request)
    elif approval_path:
        approval = _verify_human_approval(project, Path(approval_path), request)
    else:
        _write_json(style_path, _refresh(style, status="sample", sample_hashes=sample_hashes))
        state["pending_approval"] = request
        _save_state(project, state, "style_approval", "representative style sample awaits human approval")
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "needs_human",
            "approval_request": request,
            "asset_spec_ids": spec_ids,
            "sample_hashes": sample_hashes,
        }
    if state.get("status") == "style_sample":
        _save_state(project, state, "style_approval", "representative style sample recorded for approval")
        state = _state(project)
    _save_approval(project, approval)
    _write_json(style_path, _refresh(style, status="approved", sample_hashes=sample_hashes))
    state["style_approval_id"] = approval["id"]
    state["pending_approval"] = None
    _save_state(project, state, "ready", "representative style approved")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "approval_id": approval["id"],
        "asset_spec_ids": spec_ids,
        "sample_hashes": sample_hashes,
    }


def next_asset_task(root: Path | str) -> dict[str, Any]:
    project = _project(root)
    state = _state(project)
    status = str(state.get("status"))
    operation = {
        "unplanned": "plan",
        "inventoried": "sample",
        "style_sample": "sample",
        "style_approval": "style_approval",
        "ready": "generate",
        "generating": "resume_generation",
        "validating": "integrate" if validate_assets(project)["status"] == "passed" else "validate",
        "integrated": "complete",
        "blocked": "resolve_blocker",
        "rework": "replan",
    }[status]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if operation not in {"style_approval", "resolve_blocker"} else "needs_human",
        "media_state": status,
        "operation": operation,
        "command": {
            "plan": "aigame assets plan --apply --json",
            "sample": "aigame assets sample --apply --json",
            "generate": "aigame assets generate --all --jobs 8 --apply --json",
            "resume_generation": "aigame assets generate --all --jobs 8 --apply --json",
            "validate": "aigame assets validate --json",
            "integrate": "aigame assets integrate --apply --json",
        }.get(operation),
    }


def _pillow() -> tuple[Any, Any]:
    try:
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise MissingCapability(["Pillow"], "Install the pinned 'media' optional dependency set") from error
    return Image, ImageDraw


def _numpy() -> Any:
    try:
        import numpy
    except ImportError as error:
        raise MissingCapability(["numpy"], "Install the pinned 'media' optional dependency set") from error
    return numpy


def _palette_values(style: dict[str, Any]) -> tuple[list[int], dict[str, int]]:
    palette = style["palette"]
    names = list(palette)
    rgb: list[int] = []
    mapping = {}
    for index, name in enumerate(names):
        color = str(palette[name])
        mapping[name] = index
        rgb.extend(int(color[offset : offset + 2], 16) for offset in (1, 3, 5))
    rgb.extend([0] * (768 - len(rgb)))
    return rgb, mapping


def _visual_dimensions(recipe: dict[str, Any]) -> tuple[int, int, list[dict[str, Any]]]:
    kind = recipe["kind"]
    clips = list(recipe.get("parameters", {}).get("clips", []))
    if kind == "animation":
        return 16 * sum(int(clip["frames"]) for clip in clips), 16 * 4, clips
    if kind == "tileset":
        return 16 * 16, 16 * 16, clips
    return 16, 16, clips


def _indexed_png(
    recipe: dict[str, Any],
    style: dict[str, Any],
    seed: int,
    parts: list[dict[str, Any]] | None = None,
) -> bytes:
    Image, ImageDraw = _pillow()
    width, height, clips = _visual_dimensions(recipe)
    image = Image.new("P", (width, height), color=0)
    palette, colors = _palette_values(style)
    image.putpalette(palette)
    image.info["transparency"] = 0
    draw = ImageDraw.Draw(image)
    rng = random.Random(seed)

    ordered_parts = sorted(
        parts or [],
        key=lambda part: LAYER_SLOTS.index(str(part.get("slot")))
        if str(part.get("slot")) in LAYER_SLOTS
        else len(LAYER_SLOTS),
    )

    def semantic_variant(name: str) -> str:
        if name == "primary":
            return ["primary", "safe", "danger", "secondary"][seed % 4]
        if name == "secondary":
            return ["secondary", "light", "primary", "safe"][(seed // 4) % 4]
        if name == "dark":
            return ["dark", "mid", "outline", "shadow"][(seed // 16) % 4]
        return name

    def cell(
        offset_x: int,
        offset_y: int,
        frame: int,
        direction: int,
        clip_name: str = "idle",
    ) -> None:
        bob = (frame + direction) % 2
        if ordered_parts:
            direction_name = DIRECTIONS[direction]
            for part in ordered_parts:
                pixels = part.get("pixels", [])
                direction_pixels = part.get("direction_pixels")
                if isinstance(direction_pixels, dict) and direction_pixels.get(direction_name):
                    pixels = direction_pixels[direction_name]
                for mask in part.get("occlusion_mask", []):
                    if len(mask) >= 2:
                        image.putpixel((offset_x + int(mask[0]), offset_y + int(mask[1])), 0)
                for pixel in pixels:
                    if len(pixel) != 3:
                        continue
                    x, y, semantic = int(pixel[0]), int(pixel[1]), str(pixel[2])
                    if direction_name == "left" and bool(part.get("mirror_safe")):
                        x = 15 - x
                    animation_offset = bob if clip_name in {"idle", "move", "spawn"} else 0
                    if clip_name in {"primary_attack", "attack", "dash"} and part.get("slot") in {"front_weapon", "front_effect"}:
                        x = min(15, x + min(2, frame))
                    palette_name = semantic_variant(semantic)
                    if 0 <= x <= 15 and 0 <= y + animation_offset <= 15:
                        image.putpixel(
                            (offset_x + x, offset_y + y + animation_offset),
                            colors.get(palette_name, 0),
                        )
            return
        primary = colors["primary"] if (seed + direction) % 2 else colors["safe"]
        draw.ellipse((offset_x + 4, offset_y + 12, offset_x + 11, offset_y + 14), fill=colors["shadow"])
        draw.rectangle((offset_x + 6, offset_y + 6 + bob, offset_x + 9, offset_y + 11 + bob), fill=primary)
        draw.rectangle((offset_x + 6, offset_y + 3 + bob, offset_x + 9, offset_y + 6 + bob), fill=colors["light"])
        draw.point((offset_x + (7 if direction != 2 else 8), offset_y + 5 + bob), fill=colors["outline"])
        draw.line((offset_x + 5, offset_y + 8 + bob, offset_x + 3, offset_y + 9 + bob), fill=colors["dark"])
        draw.line((offset_x + 10, offset_y + 8 + bob, offset_x + 13, offset_y + 7 + bob), fill=colors["secondary"])
        if frame % 3 == 2:
            draw.point((offset_x + 14, offset_y + 6 + bob), fill=colors["danger"])

    if recipe["kind"] == "animation":
        for direction in range(4):
            frame_index = 0
            for clip in clips:
                for frame in range(int(clip["frames"])):
                    cell(
                        frame_index * 16,
                        direction * 16,
                        frame,
                        direction,
                        str(clip["name"]),
                    )
                    frame_index += 1
    elif recipe["kind"] == "tileset":
        for mask in range(256):
            x = (mask % 16) * 16
            y = (mask // 16) * 16
            floor = colors["dark"] if mask % 2 else colors["mid"]
            draw.rectangle((x, y, x + 15, y + 15), fill=floor)
            if not mask & 1:
                draw.line((x, y, x + 15, y), fill=colors["outline"])
            if not mask & 4:
                draw.line((x + 15, y, x + 15, y + 15), fill=colors["outline"])
            if not mask & 16:
                draw.line((x, y + 15, x + 15, y + 15), fill=colors["outline"])
            if not mask & 64:
                draw.line((x, y, x, y + 15), fill=colors["outline"])
            draw.point((x + 3 + (mask % 10), y + 3 + ((mask // 10) % 10)), fill=colors["light"])
    elif recipe["kind"] == "particle":
        for _ in range(12):
            x, y = rng.randrange(2, 14), rng.randrange(2, 14)
            draw.point((x, y), fill=rng.choice([colors["primary"], colors["secondary"], colors["danger"]]))
    elif recipe["kind"] == "ui":
        draw.rounded_rectangle((1, 1, 14, 14), radius=2, fill=colors["dark"], outline=colors["light"])
        draw.rectangle((5, 4, 10, 11), fill=colors["primary"])
        draw.point((8, 7), fill=colors["secondary"])
    else:
        cell(0, 0, 0, 0)
    payload = io.BytesIO()
    image.save(payload, format="PNG", optimize=False, compress_level=9)
    return payload.getvalue()


def _sprite_frames_resource(png_path: str, recipe: dict[str, Any]) -> bytes:
    clips = recipe["parameters"].get("clips", BASELINE_CLIPS)
    lines = [
        f'[gd_resource type="SpriteFrames" load_steps={2 + sum(int(c["frames"]) for c in clips) * 4} format=3]',
        "",
        f'[ext_resource type="Texture2D" path="res://{png_path}" id="1_texture"]',
        "",
    ]
    subresources: list[str] = []
    animations: list[str] = []
    total_frames = sum(int(clip["frames"]) for clip in clips)
    for direction_index, direction in enumerate(DIRECTIONS):
        clip_offset = 0
        for clip in clips:
            frames = []
            for local_frame in range(int(clip["frames"])):
                global_frame = direction_index * total_frames + clip_offset + local_frame
                sub_id = f"AtlasTexture_{global_frame:04d}"
                x = (clip_offset + local_frame) * 16
                y = direction_index * 16
                subresources.extend(
                    [
                        f'[sub_resource type="AtlasTexture" id="{sub_id}"]',
                        'atlas = ExtResource("1_texture")',
                        f"region = Rect2({x}, {y}, 16, 16)",
                        "",
                    ]
                )
                frames.append(f'{{"duration": 1.0, "texture": SubResource("{sub_id}")}}')
            animations.append(
                "{" + f'"frames": [{", ".join(frames)}], "loop": {str(bool(clip["loop"])).lower()}, "name": &"{clip["name"]}_{direction}", "speed": {float(clip["fps"])}' + "}"
            )
            clip_offset += int(clip["frames"])
    lines.extend(subresources)
    lines.extend(["[resource]", f"animations = [{', '.join(animations)}]", ""])
    return "\n".join(lines).encode("utf-8")


def _tileset_resource(png_path: str) -> bytes:
    lines = [
        '[gd_resource type="TileSet" load_steps=3 format=3]',
        "",
        f'[ext_resource type="Texture2D" path="res://{png_path}" id="1_texture"]',
        "",
        '[sub_resource type="TileSetAtlasSource" id="TileSetAtlasSource_media"]',
        'texture = ExtResource("1_texture")',
        "texture_region_size = Vector2i(16, 16)",
    ]
    for mask in range(256):
        lines.append(f"{mask % 16}:{mask // 16}/0 = 0")
    lines.extend(["", "[resource]", "tile_size = Vector2i(16, 16)", 'sources/0 = SubResource("TileSetAtlasSource_media")', ""])
    return "\n".join(lines).encode("utf-8")


def _particle_scene(png_path: str, *, cpu: bool, seed: int) -> bytes:
    node_type = "CPUParticles2D" if cpu else "GPUParticles2D"
    process_type = "ParticleProcessMaterial"
    seed_lines = "" if cpu else f"use_fixed_seed = true\nseed = {seed}\n"
    return (
        f'''[gd_scene load_steps=3 format=3]\n\n[ext_resource type="Texture2D" path="res://{png_path}" id="1_texture"]\n\n[sub_resource type="{process_type}" id="ParticleProcessMaterial_media"]\nemission_shape = 1\nemission_sphere_radius = 4.0\ndirection = Vector3(0, -1, 0)\nspread = 55.0\ninitial_velocity_min = 24.0\ninitial_velocity_max = 52.0\ngravity = Vector3(0, 20, 0)\n\n[node name="GeneratedParticles" type="{node_type}"]\namount = 24\nlifetime = 0.6\nfixed_fps = 12\ntexture = ExtResource("1_texture")\nprocess_material = SubResource("ParticleProcessMaterial_media")\n{seed_lines}'''
    ).encode("utf-8")


def _waveform(np: Any, name: str, phase: Any, rng: Any) -> Any:
    turns = phase / (2.0 * math.pi)
    if name == "sine":
        return np.sin(phase)
    if name == "square":
        return np.where(np.sin(phase) >= 0.0, 1.0, -1.0)
    if name == "triangle":
        return 2.0 * np.abs(2.0 * (turns - np.floor(turns + 0.5))) - 1.0
    if name == "saw":
        return 2.0 * (turns - np.floor(turns + 0.5))
    if name == "pulse":
        return np.where((turns % 1.0) < 0.25, 1.0, -1.0)
    return rng.uniform(-1.0, 1.0, len(phase))


def _sound_pcm(sound: dict[str, Any], seed: int) -> tuple[bytes, int, int]:
    np = _numpy()
    sample_rate = int(sound["sample_rate"])
    duration = float(sound["duration_seconds"])
    count = max(1, int(round(sample_rate * duration)))
    sample_index = np.arange(count, dtype=np.float64)
    time_axis = sample_index / sample_rate
    rng = np.random.default_rng(seed)
    mono = np.zeros(count, dtype=np.float64)
    for layer_index, layer in enumerate(sound["layers"]):
        detune = rng.uniform(-0.35, 0.35) if int(sound.get("variants", 1)) > 1 else 0.0
        frequency = float(layer["frequency"]) * (2.0 ** (detune / 12.0))
        pitch = sound.get("pitch_envelope", {})
        pitch_semitones = np.linspace(
            float(pitch.get("start_semitones", 0.0)),
            float(pitch.get("end_semitones", 0.0)),
            count,
        )
        arpeggio = sound.get("arpeggio", {})
        notes = arpeggio.get("semitones", [0]) or [0]
        step_samples = max(1, int(float(arpeggio.get("step_seconds", duration)) * sample_rate))
        note_indices = (sample_index.astype(np.int64) // step_samples) % len(notes)
        note_values = np.take(np.asarray(notes, dtype=np.float64), note_indices)
        vibrato = sound.get("vibrato", {})
        vibrato_semitones = float(vibrato.get("depth_semitones", 0.0)) * np.sin(
            2.0 * math.pi * float(vibrato.get("rate_hz", 0.0)) * time_axis
        )
        instant_frequency = frequency * np.power(
            2.0,
            (pitch_semitones + note_values + vibrato_semitones) / 12.0,
        )
        phase = 2.0 * math.pi * np.cumsum(instant_frequency) / sample_rate + layer_index * 0.25
        stem_gain = [0.65, 0.82, 1.0][min(2, max(0, int(sound.get("stem_index", 2))))]
        stem_index = min(2, max(0, int(sound.get("stem_index", 2))))
        waveform_name = str(layer["waveform"])
        if sound["category"] == "music" and layer_index == 0:
            waveform_name = ["triangle", "saw", "square"][stem_index]
        rhythm = 1.0
        if sound["category"] == "music" and stem_index > 0:
            rhythm = np.where(
                (sample_index.astype(np.int64) // max(1, sample_rate // 8)) % 2 == 0,
                1.0,
                0.55 if stem_index == 1 else 0.35,
            )
        mono += (
            _waveform(np, waveform_name, phase, rng)
            * float(layer["volume"])
            * (stem_gain if sound["category"] == "music" else 1.0)
            * rhythm
        )
    adsr = sound.get("adsr", {})
    attack = max(1, int(sample_rate * float(adsr.get("attack", 0.005))))
    decay = max(1, int(sample_rate * float(adsr.get("decay", 0.03))))
    release = max(1, int(sample_rate * float(adsr.get("release", 0.02 if not sound.get("loop") else 0.005))))
    sustain = float(adsr.get("sustain", 0.82))
    envelope = np.full(count, sustain, dtype=np.float64)
    attack_end = min(count, attack)
    envelope[:attack_end] = np.linspace(0.0, 1.0, attack_end, endpoint=False)
    decay_end = min(count, attack_end + decay)
    if decay_end > attack_end:
        envelope[attack_end:decay_end] = np.linspace(1.0, sustain, decay_end - attack_end, endpoint=False)
    release_start = max(0, count - release)
    envelope[release_start:] *= np.linspace(1.0, 0.0, count - release_start, endpoint=True)
    mono *= envelope
    window = max(1, int(sound.get("filter", {}).get("window_samples", 1)))
    if window > 1:
        mono = np.convolve(mono, np.ones(window, dtype=np.float64) / window, mode="same")
    drive = max(0.0, float(sound.get("distortion", {}).get("drive", 1.0)))
    if drive > 1.0:
        mono = np.tanh(mono * drive) / math.tanh(drive)
    bits = min(16, max(2, int(sound.get("bit_crush", {}).get("bits", 16))))
    if bits < 16:
        levels = float((1 << (bits - 1)) - 1)
        mono = np.rint(mono * levels) / levels
    delay = sound.get("delay", {})
    delay_samples = int(float(delay.get("seconds", 0.0)) * sample_rate)
    feedback = float(delay.get("feedback", 0.0))
    if 0 < delay_samples < count and feedback:
        mono[delay_samples:] += mono[:-delay_samples] * feedback
    mono -= float(np.mean(mono))
    maximum = float(np.max(np.abs(mono)))
    if maximum <= 1e-12:
        raise WorkflowError(f"{sound.get('id')}: synthesized audio is silent")
    ceiling = 10.0 ** (float(sound.get("peak_dbfs", -1.0)) / 20.0)
    mono = np.clip(mono / maximum * ceiling, -ceiling, ceiling)
    mono[0] = 0.0
    mono[-1] = 0.0
    channels = int(sound["channels"])
    if channels == 2:
        panning = sound.get("panning", {})
        samples = np.column_stack(
            (mono * float(panning.get("left", 0.96)), mono * float(panning.get("right", 1.0)))
        )
    else:
        samples = mono[:, None]
    integer = np.rint(samples * 32767.0).astype("<i2")
    return integer.tobytes(order="C"), sample_rate, channels


def _wav_bytes(sound: dict[str, Any], seed: int) -> bytes:
    pcm, sample_rate, channels = _sound_pcm(sound, seed)
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(pcm)
    return output.getvalue()


def _randomizer_resource(wav_paths: list[str]) -> bytes:
    lines = [f'[gd_resource type="AudioStreamRandomizer" load_steps={len(wav_paths) + 1} format=3]', ""]
    for index, path in enumerate(wav_paths, 1):
        lines.append(f'[ext_resource type="AudioStream" path="res://{path}" id="{index}_stream"]')
    lines.extend(["", "[resource]", "random_pitch = 1.02", "random_volume_offset_db = 1.0", "playback_mode = 1", f"streams_count = {len(wav_paths)}"])
    for index in range(len(wav_paths)):
        lines.extend([f"stream_{index}/stream = ExtResource(\"{index + 1}_stream\")", f"stream_{index}/weight = 1.0"])
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _synchronized_resource(wav_paths: list[str]) -> bytes:
    lines = [f'[gd_resource type="AudioStreamSynchronized" load_steps={len(wav_paths) + 1} format=3]', ""]
    for index, path in enumerate(wav_paths, 1):
        lines.append(f'[ext_resource type="AudioStream" path="res://{path}" id="{index}_stream"]')
    lines.extend(["", "[resource]", f"stream_count = {len(wav_paths)}"])
    for index in range(len(wav_paths)):
        lines.extend(
            [
                f"stream_{index}/stream = ExtResource(\"{index + 1}_stream\")",
                f"stream_{index}/volume = {0.0 if index == 0 else -60.0}",
            ]
        )
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _compile_recipe(project: Path, recipe: dict[str, Any]) -> dict[str, Any]:
    style = _load_json(project / "work" / "assets" / "styles" / "STY-0001.json")
    outputs = [str(value) for value in recipe["outputs"]]
    for value in outputs:
        _safe_relative(value)
    kind = str(recipe["kind"])
    compiled: dict[str, bytes] = {}
    selected_parts: list[dict[str, Any]] = []
    if recipe.get("source_part_ids"):
        part_by_id = {
            part["id"]: part for part in _all_records(project, "parts", "PRT")
        }
        expected_hashes = recipe.get("parameters", {}).get("source_part_hashes", {})
        for part_id in recipe["source_part_ids"]:
            part = part_by_id.get(part_id)
            if not part:
                raise WorkflowError(f"{recipe['id']}: missing source part {part_id}")
            if part.get("status") != "approved" or part.get("license") not in {
                "original",
                "CC0-1.0",
                "CC-BY-4.0",
                "Apache-2.0",
                "proprietary",
                "commercial",
            }:
                raise WorkflowError(f"{recipe['id']}: source part {part_id} is not approved or compatible")
            if expected_hashes.get(part_id) != part.get("input_fingerprint"):
                raise WorkflowError(f"{recipe['id']}: source part {part_id} changed; replan before generation")
            if not part.get("mirror_safe"):
                directional = part.get("direction_pixels")
                if not isinstance(directional, dict) or set(directional) != set(DIRECTIONS):
                    raise WorkflowError(f"{recipe['id']}: asymmetric part {part_id} requires all four directions")
            for pixel in part.get("pixels", []):
                if len(pixel) != 3 or str(pixel[2]) not in style.get("palette", {}):
                    raise WorkflowError(f"{recipe['id']}: source part {part_id} uses an unknown semantic color")
            selected_parts.append(part)
        choices = recipe.get("parameters", {}).get("layer_choices", {})
        if choices:
            selected_by_id = {part["id"]: part for part in selected_parts}
            selected_parts = [
                selected_by_id[options[(int(recipe["seed"]) + slot_index * 17) % len(options)]]
                for slot_index, slot in enumerate(LAYER_SLOTS)
                if (options := choices.get(slot, []))
            ]
    if kind in {"sound", "ambience", "music"}:
        sound = dict(recipe["parameters"]["sound"])
        wav_paths = [path for path in outputs if path.endswith(".wav")]
        for variant, path in enumerate(wav_paths):
            variant_sound = {**sound, "stem_index": variant} if kind == "music" else sound
            compiled[path] = _wav_bytes(
                variant_sound,
                int(recipe["seed"]) + variant * 104729,
            )
        tres = next(path for path in outputs if path.endswith(".tres"))
        compiled[tres] = (
            _synchronized_resource(wav_paths)
            if kind == "music"
            else _randomizer_resource(wav_paths)
        )
    else:
        png_path = next(path for path in outputs if path.endswith(".png"))
        compiled[png_path] = _indexed_png(
            recipe,
            style,
            int(recipe["seed"]),
            selected_parts,
        )
        if kind == "animation":
            compiled[next(path for path in outputs if path.endswith(".tres"))] = _sprite_frames_resource(png_path, recipe)
        elif kind == "tileset":
            compiled[next(path for path in outputs if path.endswith(".tres"))] = _tileset_resource(png_path)
        elif kind == "particle":
            gpu = next(path for path in outputs if path.endswith(".tscn") and not path.endswith("-cpu.tscn"))
            cpu = next(path for path in outputs if path.endswith("-cpu.tscn"))
            compiled[gpu] = _particle_scene(
                png_path,
                cpu=False,
                seed=int(recipe["seed"]),
            )
            compiled[cpu] = _particle_scene(
                png_path,
                cpu=True,
                seed=int(recipe["seed"]),
            )
    hashes = {path: hashlib.sha256(value).hexdigest() for path, value in compiled.items()}
    return {"recipe": recipe, "compiled": compiled, "hashes": hashes}


def _receipt_path(project: Path, recipe: dict[str, Any]) -> Path:
    return project / ".aigame" / "cache" / "media" / f"{recipe['input_fingerprint']}.json"


def _cache_hit(project: Path, compiled: dict[str, bytes], hashes: dict[str, str], receipt: Path) -> bool:
    if not receipt.is_file():
        return False
    try:
        value = _load_json(receipt)
    except WorkflowError:
        return False
    if value.get("outputs") != hashes:
        return False
    return all(
        (project / _safe_relative(path)).is_file()
        and hashlib.sha256((project / _safe_relative(path)).read_bytes()).hexdigest() == digest
        for path, digest in hashes.items()
    )


def _manifest_record(asset_id: str, recipe: dict[str, Any], path: str, digest: str) -> dict[str, Any]:
    extension = Path(path).suffix.lower()
    if extension == ".wav":
        kind = "music" if recipe["kind"] in {"music", "ambience"} else "sound"
    elif extension == ".png":
        kind = "animation" if recipe["kind"] == "animation" else "image"
    else:
        kind = "data"
    return _record(
        {
            "kind": kind,
            "runtime_path": path,
            "license": recipe.get("parameters", {}).get("output_license", "CC0-1.0"),
            "provenance": {
                "generated": True,
                "method": "deterministic local media recipe",
                "provider": "aigame-local-media",
                "provider_version": __version__,
                "model_id": "pixel-media-v1",
                "model_license": "Apache-2.0",
                "prompt_sha256": fingerprint(recipe["parameters"].get("purpose", "")),
                "recipe_id": recipe["id"],
                "recipe_sha256": recipe["input_fingerprint"],
                "seed": recipe["seed"],
                "source_part_ids": recipe["source_part_ids"],
                "source_part_licenses": recipe.get("parameters", {}).get("source_part_licenses", {}),
            },
            "sha256": digest,
            "asset_spec_id": recipe["asset_spec_id"],
        },
        record_id=asset_id,
        status="generated",
    )


def _generate_selected(
    project: Path,
    spec_ids: Iterable[str],
    *,
    jobs: int,
    allow_preapproval: bool = False,
) -> dict[str, Any]:
    selected = set(spec_ids)
    recipes = [record for record in _all_records(project, "recipes", "RCP") if record["asset_spec_id"] in selected]
    if not recipes:
        raise WorkflowError("No media recipes match the requested asset specification")
    all_recipes = _all_records(project, "recipes", "RCP")
    output_ids = {
        path: f"AST-{index:04d}"
        for index, path in enumerate(sorted(path for recipe in all_recipes for path in recipe["outputs"]), 1)
    }
    worker_count = max(1, min(int(jobs), 64))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        compiled_results = list(executor.map(lambda recipe: _compile_recipe(project, recipe), recipes))
    generated = cached = 0
    hashes: list[str] = []
    new_records: list[dict[str, Any]] = []
    for result in compiled_results:
        recipe = result["recipe"]
        receipt = _receipt_path(project, recipe)
        if _cache_hit(project, result["compiled"], result["hashes"], receipt):
            cached += 1
        else:
            for path, payload in sorted(result["compiled"].items()):
                _write_bytes_if_changed(project / _safe_relative(path), payload)
            _write_json(
                receipt,
                {
                    "schema_version": SCHEMA_VERSION,
                    "recipe_id": recipe["id"],
                    "recipe_fingerprint": recipe["input_fingerprint"],
                    "outputs": result["hashes"],
                },
            )
            generated += 1
        hashes.extend(result["hashes"].values())
        for path, digest in sorted(result["hashes"].items()):
            new_records.append(_manifest_record(output_ids[path], recipe, path, digest))
    manifest_path = project / "assets" / "asset-manifest.json"
    manifest = _load_json(manifest_path)
    managed_ids = set(output_ids.values())
    preserved = [record for record in manifest.get("assets", []) if record.get("id") not in managed_ids]
    combined_by_id = {record["id"]: record for record in preserved}
    for record in new_records:
        combined_by_id[record["id"]] = record
    _write_json(
        manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "revision": int(manifest.get("revision", 0)) + (1 if generated else 0),
            "assets": [combined_by_id[key] for key in sorted(combined_by_id)],
        },
    )
    return {
        "total": len(recipes),
        "generated": generated,
        "cached": cached,
        "hashes": hashes,
        "allow_preapproval": allow_preapproval,
    }


def generate_assets(
    root: Path | str,
    asset_spec_id: str | None = None,
    *,
    all_assets: bool = False,
    jobs: int = 1,
    apply: bool = False,
) -> dict[str, Any]:
    project = _project(root)
    state = _state(project)
    if state.get("status") not in {"ready", "generating", "validating"}:
        raise WorkflowError(f"Bulk generation requires ready media state, not {state.get('status')}")
    specs = _all_records(project, "specs", "ASP")
    if bool(asset_spec_id) == bool(all_assets):
        raise WorkflowError("Select exactly one ASP-ID or --all")
    selected = [str(spec["id"]) for spec in specs] if all_assets else [str(asset_spec_id)]
    known = {str(spec["id"]) for spec in specs}
    unknown = sorted(set(selected) - known)
    if unknown:
        raise WorkflowError(f"Unknown asset specification: {unknown}")
    if not apply:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "dry_run",
            "asset_spec_ids": selected,
            "jobs": max(1, int(jobs)),
        }
    _save_state(project, state, "generating", f"generation batch started for {len(selected)} specifications")
    result = _generate_selected(project, selected, jobs=jobs)
    current = _state(project)
    _save_state(project, current, "validating", "generation batch completed and awaits validation")
    return {"schema_version": SCHEMA_VERSION, "status": "passed", **result}


def compose_recipe(root: Path | str, recipe_id: str, *, seed: int | None = None) -> dict[str, Any]:
    project = _project(root)
    path = project / "work" / "assets" / "recipes" / f"{recipe_id}.json"
    if not re.fullmatch(r"RCP-[0-9]{4}", recipe_id) or not path.is_file():
        raise WorkflowError(f"Unknown media recipe: {recipe_id}")
    recipe = _load_json(path)
    if seed is not None:
        recipe = {**recipe, "seed": int(seed)}
    compiled = _compile_recipe(project, recipe)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "recipe_id": recipe_id,
        "seed": recipe["seed"],
        "outputs": compiled["hashes"],
    }


def _validate_fingerprint(record: dict[str, Any]) -> bool:
    material = {key: item for key, item in record.items() if key != "input_fingerprint"}
    return record.get("input_fingerprint") == fingerprint(material)


def validate_assets(root: Path | str) -> dict[str, Any]:
    project = _project(root)
    errors: list[str] = []
    warnings: list[str] = []
    media_path = project / ".aigame" / "media.toml"
    try:
        media_config = tomllib.loads(media_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"MediaConfig: {error}")
        media_config = {}
    if media_config:
        errors.extend(f"MediaConfig: {message}" for message in _schema_errors(media_config, "media-config.schema.json"))
    records: dict[str, dict[str, Any]] = {}
    for folder, prefix in (
        ("", "APL"),
        ("styles", "STY"),
        ("parts", "PRT"),
        ("specs", "ASP"),
        ("recipes", "RCP"),
        ("animations", "ANI"),
        ("tiles", "TIL"),
        ("particles", "PFX"),
        ("sounds", "SND"),
    ):
        base = project / "work" / "assets" / folder
        for path in sorted(base.glob(f"{prefix}-*.json")):
            try:
                record = _load_json(path)
            except WorkflowError as error:
                errors.append(str(error))
                continue
            record_id = str(record.get("id"))
            if record_id in records:
                errors.append(f"Duplicate media id: {record_id}")
            records[record_id] = record
            errors.extend(f"{record_id}: {message}" for message in _schema_errors(record, SCHEMA_BY_PREFIX[prefix]))
            if not _validate_fingerprint(record):
                errors.append(f"{record_id}: input fingerprint does not match current record content")
    if not records:
        state = _state(project)
        if state.get("status") != "unplanned":
            errors.append("Media records are missing for a planned media state")
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "failed" if errors else "passed",
            "errors": errors,
            "warnings": warnings,
            "counts": {},
        }
    pack_path = project / "assets" / "source" / "packs" / "core-topdown-v1" / "pack.json"
    if not pack_path.is_file():
        errors.append("Core source-pack manifest is missing")
    else:
        pack = _load_json(pack_path)
        pack_material = {key: item for key, item in pack.items() if key != "sha256"}
        if pack.get("sha256") != fingerprint(pack_material):
            errors.append("Core source-pack checksum does not match its manifest")
        for part_id, expected_hash in pack.get("part_hashes", {}).items():
            part = records.get(str(part_id))
            source_path = pack_path.parent / "parts" / f"{part_id}.json"
            if not part or expected_hash != part.get("input_fingerprint"):
                errors.append(f"Core source pack has stale part hash for {part_id}")
            if not source_path.is_file() or _load_json(source_path).get("input_fingerprint") != expected_hash:
                errors.append(f"Core source pack file is missing or stale for {part_id}")
    plan = records.get("APL-0001", {})
    spec_ids = set(plan.get("asset_spec_ids", []))
    coverage_sources: set[str] = set()
    for row in plan.get("coverage", []):
        source = str(row.get("source_ref", ""))
        if source in coverage_sources:
            errors.append(f"APL-0001: duplicate coverage source {source}")
        coverage_sources.add(source)
        for asset_id in row.get("asset_spec_ids", []):
            if asset_id not in spec_ids:
                errors.append(f"APL-0001: coverage references unknown asset spec {asset_id}")
    try:
        expected, current_blueprint_fingerprint = _collect_needs(project)
    except WorkflowError as error:
        errors.append(str(error))
    else:
        expected_sources = {item["source_ref"] for item in expected}
        for missing in sorted(expected_sources - coverage_sources):
            errors.append(f"APL-0001: unmapped blueprint media source {missing}")
        for stale in sorted(coverage_sources - expected_sources):
            errors.append(f"APL-0001: stale blueprint media source {stale}")
        if plan.get("blueprint_fingerprint") != current_blueprint_fingerprint:
            errors.append("APL-0001: blueprint fingerprint changed; replan assets")
    recipe_ids = {record_id for record_id in records if record_id.startswith("RCP-")}
    for asset_id in spec_ids:
        spec = records.get(asset_id)
        if not spec:
            errors.append(f"APL-0001: missing asset specification {asset_id}")
            continue
        for recipe_id in spec.get("recipe_ids", []):
            if recipe_id not in recipe_ids:
                errors.append(f"{asset_id}: dangling media recipe {recipe_id}")
        for runtime_path in spec.get("runtime_paths", []):
            try:
                _safe_relative(str(runtime_path))
            except WorkflowError as error:
                errors.append(f"{asset_id}: {error}")
    for recipe_id in recipe_ids:
        recipe = records[recipe_id]
        if recipe.get("asset_spec_id") not in spec_ids:
            errors.append(f"{recipe_id}: dangling asset specification {recipe.get('asset_spec_id')}")
        for part_id in recipe.get("source_part_ids", []):
            if part_id not in records:
                errors.append(f"{recipe_id}: dangling source part {part_id}")
                continue
            expected_hash = recipe.get("parameters", {}).get("source_part_hashes", {}).get(part_id)
            if expected_hash != records[part_id].get("input_fingerprint"):
                errors.append(f"{recipe_id}: source part {part_id} changed; replan assets")
        if recipe.get("source_part_ids"):
            layers = recipe.get("parameters", {}).get("layers", [])
            layer_ids = [layer.get("part_id") for layer in layers]
            layer_slots = [layer.get("slot") for layer in layers]
            if layer_slots != sorted(
                layer_slots,
                key=lambda slot: LAYER_SLOTS.index(str(slot)) if str(slot) in LAYER_SLOTS else len(LAYER_SLOTS),
            ):
                errors.append(f"{recipe_id}: layer order does not match the canonical slot order")
            choices = recipe.get("parameters", {}).get("layer_choices", {})
            choice_ids = [part_id for slot in LAYER_SLOTS for part_id in choices.get(slot, [])]
            if choice_ids != recipe.get("source_part_ids") or any(part_id not in choice_ids for part_id in layer_ids):
                errors.append(f"{recipe_id}: layer choices do not match source_part_ids")
    for record_id, record in records.items():
        if not record_id.startswith("PRT-"):
            continue
        if record.get("slot") not in LAYER_SLOTS:
            errors.append(f"{record_id}: unknown layer slot {record.get('slot')!r}")
        if not record.get("mirror_safe"):
            direction_pixels = record.get("direction_pixels")
            if not isinstance(direction_pixels, dict) or set(direction_pixels) != set(DIRECTIONS):
                errors.append(f"{record_id}: asymmetric parts require all four direction pixel sets")
        for pixel in record.get("pixels", []):
            if len(pixel) != 3 or not (0 <= int(pixel[0]) <= 15 and 0 <= int(pixel[1]) <= 15):
                errors.append(f"{record_id}: pixel lies outside the 16x16 grid")
    for record_id, record in records.items():
        if record_id.startswith("TIL-") and record.get("required_neighbor_masks") != list(range(256)):
            errors.append(f"{record_id}: incomplete terrain-neighbor coverage")
    state = _state(project)
    require_outputs = state.get("status") in {"validating", "integrated"}
    manifest_path = project / "assets" / "asset-manifest.json"
    manifest = _load_json(manifest_path)
    assets = manifest.get("assets", [])
    by_path: dict[str, dict[str, Any]] = {}
    manifest_ids: set[str] = set()
    allowed_licenses = {"original", "CC0-1.0", "CC-BY-4.0", "Apache-2.0", "proprietary", "commercial"}
    for asset in assets:
        asset_id = str(asset.get("id", ""))
        runtime_path = str(asset.get("runtime_path", ""))
        if asset_id in manifest_ids:
            errors.append(f"Asset manifest contains duplicate id {asset_id}")
        manifest_ids.add(asset_id)
        if runtime_path in by_path:
            errors.append(f"Asset manifest contains duplicate runtime path {runtime_path}")
        by_path[runtime_path] = asset
        errors.extend(f"{asset_id}: {message}" for message in _schema_errors(asset, "asset-record.schema.json"))
        if not _validate_fingerprint(asset):
            errors.append(f"{asset_id}: input fingerprint does not match current record content")
        if asset.get("license") not in allowed_licenses:
            errors.append(f"{asset_id}: incompatible or unknown license {asset.get('license')!r}")
        try:
            artifact_path = project / _safe_relative(runtime_path)
        except WorkflowError as error:
            errors.append(f"{asset_id}: {error}")
            continue
        provenance = asset.get("provenance", {})
        if provenance.get("generated") and not all(
            provenance.get(field)
            for field in ("provider", "provider_version", "model_id", "model_license", "prompt_sha256", "recipe_id", "seed")
        ):
            errors.append(f"{asset_id}: generated provenance is incomplete")
        if artifact_path.is_file() and asset.get("sha256") != hashlib.sha256(artifact_path.read_bytes()).hexdigest():
            errors.append(f"{asset_id}: checksum mismatch for {runtime_path}")
    if len(by_path) != len(assets):
        errors.append("Asset manifest contains duplicate runtime paths")
    if require_outputs:
        expected_outputs = {
            str(path)
            for recipe_id in recipe_ids
            for path in records[recipe_id].get("outputs", [])
        }
        for runtime_path in sorted(expected_outputs):
            path = project / _safe_relative(runtime_path)
            record = by_path.get(runtime_path)
            if not path.is_file():
                errors.append(f"Missing generated output: {runtime_path}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if not record:
                errors.append(f"Missing AssetRecord for generated output: {runtime_path}")
            elif record.get("sha256") != digest:
                errors.append(f"Checksum mismatch for generated output: {runtime_path}")
            if path.suffix.lower() == ".png":
                try:
                    Image, _ = _pillow()
                    with Image.open(path) as image:
                        if image.mode != "P":
                            errors.append(f"{runtime_path}: pixel output is not indexed PNG")
                        if image.width % 16 or image.height % 16:
                            errors.append(f"{runtime_path}: dimensions are not on the 16x16 grid")
                except OSError as error:
                    errors.append(f"{runtime_path}: invalid PNG: {error}")
            elif path.suffix.lower() == ".wav":
                try:
                    with wave.open(str(path), "rb") as stream:
                        if stream.getframerate() != 48000 or stream.getsampwidth() != 2:
                            errors.append(f"{runtime_path}: WAV must be 48 kHz 16-bit PCM")
                        raw = stream.readframes(stream.getnframes())
                    np = _numpy()
                    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32767.0
                    if samples.size == 0 or float(np.max(np.abs(samples))) < 1e-5:
                        errors.append(f"{runtime_path}: audio is silent")
                    if float(np.max(np.abs(samples))) > 10 ** (-1 / 20) + 1e-4:
                        errors.append(f"{runtime_path}: audio exceeds the -1 dBFS peak ceiling")
                    if abs(float(np.mean(samples))) > 0.01:
                        errors.append(f"{runtime_path}: audio contains excessive DC offset")
                    recipe_id = str((record or {}).get("provenance", {}).get("recipe_id", ""))
                    recipe = records.get(recipe_id, {})
                    sound = recipe.get("parameters", {}).get("sound", {})
                    channels = max(1, int(sound.get("channels", 1)))
                    if sound.get("loop") and samples.size >= channels * 2:
                        frames = samples.reshape((-1, channels))
                        if float(np.max(np.abs(frames[0] - frames[-1]))) > 0.02:
                            errors.append(f"{runtime_path}: loop seam exceeds the sample boundary tolerance")
                except (OSError, wave.Error) as error:
                    errors.append(f"{runtime_path}: invalid WAV: {error}")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed" if errors else "passed",
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "asset_specs": len(spec_ids),
            "recipes": len(recipe_ids),
            "manifest_assets": len(assets),
            "coverage": len(coverage_sources),
        },
    }


def integrate_assets(root: Path | str, *, apply: bool = False) -> dict[str, Any]:
    project = _project(root)
    report = validate_assets(project)
    if report["status"] != "passed":
        raise WorkflowError("Media integration is blocked by validation errors: " + "; ".join(report["errors"][:5]))
    state = _state(project)
    if state.get("status") not in {"validating", "integrated"}:
        raise WorkflowError(f"Media integration requires validating state, not {state.get('status')}")
    registry = {
        "schema_version": SCHEMA_VERSION,
        "asset_plan_id": state.get("asset_plan_id"),
        "style_pack_id": state.get("style_pack_id"),
        "assets": [
            {
                "id": asset["id"],
                "runtime_path": asset["runtime_path"],
                "sha256": asset["sha256"],
                "kind": asset["kind"],
            }
            for asset in _load_json(project / "assets" / "asset-manifest.json").get("assets", [])
        ],
        "parts": {
            part["id"]: part for part in _all_records(project, "parts", "PRT")
        },
        "runtime_recipes": {
            recipe["id"]: recipe
            for recipe in _all_records(project, "recipes", "RCP")
            if recipe.get("kind") in {"sprite", "animation"}
        },
        "animation_sets": {
            record["id"]: record for record in _all_records(project, "animations", "ANI")
        },
        "tile_sets": {
            record["id"]: record for record in _all_records(project, "tiles", "TIL")
        },
        "particle_specs": {
            record["id"]: record for record in _all_records(project, "particles", "PFX")
        },
        "sound_specs": {
            record["id"]: record for record in _all_records(project, "sounds", "SND")
        },
    }
    if not apply:
        return {"schema_version": SCHEMA_VERSION, "status": "dry_run", "registry": registry}
    _write_json(project / "assets" / "generated" / "media-registry.json", registry)
    import_defaults = '''# Pixel media import policy\n# Generated PNGs use indexed color, nearest filtering, no mipmaps, and lossless storage.\n[remap]\nimporter="image"\n\n[params]\ncompress/mode=0\nmipmaps/generate=false\nprocess/fix_alpha_border=false\n'''
    _write_bytes_if_changed(project / "assets" / "generated" / "IMPORT_POLICY.md", import_defaults.encode("utf-8"))
    manifest_path = project / "assets" / "asset-manifest.json"
    manifest = _load_json(manifest_path)
    manifest["assets"] = [_refresh(record, status="integrated") for record in manifest.get("assets", [])]
    manifest["revision"] = int(manifest.get("revision", 0)) + 1
    _write_json(manifest_path, manifest)
    _save_state(project, state, "integrated", "validated media registry integrated into Godot project")
    return {"schema_version": SCHEMA_VERSION, "status": "passed", "registry_path": "assets/generated/media-registry.json"}


def generate_wfc_layout(
    *,
    width: int,
    height: int,
    seed: int,
    required_rooms: Iterable[str] = (),
) -> dict[str, Any]:
    if width < 7 or height < 7:
        raise WorkflowError("Stage dimensions must be at least 7x7")
    width = width if width % 2 else width - 1
    height = height if height % 2 else height - 1
    rng = random.Random(int(seed))
    grid = [["wall" for _ in range(width)] for _ in range(height)]
    entrance = (1, 1)
    stack = [entrance]
    grid[1][1] = "floor"
    visited = {entrance}
    while stack:
        x, y = stack[-1]
        neighbors = [
            (x + dx, y + dy, dx, dy)
            for dx, dy in ((2, 0), (-2, 0), (0, 2), (0, -2))
            if 1 <= x + dx < width - 1 and 1 <= y + dy < height - 1 and (x + dx, y + dy) not in visited
        ]
        if not neighbors:
            stack.pop()
            continue
        nx, ny, dx, dy = rng.choice(neighbors)
        grid[y + dy // 2][x + dx // 2] = "floor"
        grid[ny][nx] = "floor"
        visited.add((nx, ny))
        stack.append((nx, ny))
    exit_cell = (width - 2, height - 2)
    if grid[exit_cell[1]][exit_cell[0]] != "floor":
        grid[exit_cell[1]][exit_cell[0]] = "floor"
    queue = deque([(entrance, 0)])
    distances = {entrance: 0}
    while queue:
        (x, y), distance = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neighbor = (x + dx, y + dy)
            if (
                0 <= neighbor[0] < width
                and 0 <= neighbor[1] < height
                and grid[neighbor[1]][neighbor[0]] == "floor"
                and neighbor not in distances
            ):
                distances[neighbor] = distance + 1
                queue.append((neighbor, distance + 1))
    valid = exit_cell in distances
    safe_spawns = sorted(
        [list(cell) for cell, distance in distances.items() if distance >= 2 and cell != exit_cell],
        key=lambda cell: (cell[1], cell[0]),
    )[:8]
    room_names = list(required_rooms)
    room_candidates = [
        cell
        for cell, _ in sorted(distances.items(), key=lambda item: (item[1], item[0][1], item[0][0]))
        if cell not in {entrance, exit_cell}
    ]
    if len(room_names) > len(room_candidates):
        raise WorkflowError("Stage contradiction: required rooms exceed available connected cells")
    placements: dict[str, list[int]] = {}
    for index, name in enumerate(room_names):
        position = room_candidates[((index + 1) * len(room_candidates)) // (len(room_names) + 1)]
        placements[str(name)] = list(position)
    derived_seed = hashlib.sha256(f"{int(seed)}:0".encode("ascii")).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm": "seeded-adjacency-collapse-v1",
        "seed": int(seed),
        "derived_seed": derived_seed,
        "attempt": 0,
        "width": width,
        "height": height,
        "grid": ["".join("." if cell == "floor" else "#" for cell in row) for row in grid],
        "entrance": list(entrance),
        "exit": list(exit_cell),
        "safe_spawns": safe_spawns,
        "required_rooms": placements,
        "path_length": distances.get(exit_cell, 0),
        "valid": valid and bool(safe_spawns),
    }


def benchmark_assets(
    root: Path | str,
    *,
    sprite_count: int = 500,
    sound_count: int = 500,
) -> dict[str, Any]:
    project = _project(root)
    style = _style_record("0" * 64)
    visual_recipe = _record(
        {
            "kind": "animation",
            "asset_spec_id": "ASP-0001",
            "seed": 1,
            "source_part_ids": [],
            "parameters": {"grid_size": 16, "purpose": "benchmark", "clips": BASELINE_CLIPS},
            "outputs": ["assets/generated/benchmark.png"],
        },
        record_id="RCP-0001",
        status="approved",
    )
    sound = {
        "id": "SND-0001",
        "category": "sfx",
        "layers": [{"waveform": "square", "frequency": 220.0, "volume": 0.7}],
        "duration_seconds": 0.08,
        "sample_rate": 48000,
        "channels": 1,
        "variants": 3,
        "peak_dbfs": -1.0,
        "loop": False,
    }
    started = time.perf_counter()
    sprite_hashes = [hashlib.sha256(_indexed_png(visual_recipe, style, seed)).digest() for seed in range(max(1, sprite_count))]
    sprite_seconds = time.perf_counter() - started
    started = time.perf_counter()
    sound_hashes = [
        hashlib.sha256(
            b"".join(_wav_bytes(sound, seed + variant * 104729) for variant in range(3))
        ).digest()
        for seed in range(max(1, sound_count))
    ]
    sound_seconds = time.perf_counter() - started
    started = time.perf_counter()
    runtime_recipe = {**visual_recipe, "kind": "sprite"}
    _indexed_png(runtime_recipe, style, 999)
    runtime_ms = (time.perf_counter() - started) * 1000.0
    cache = {index: value for index, value in enumerate(sprite_hashes[:100])}
    started = time.perf_counter()
    for index in range(max(1, min(1000, len(cache) * 10))):
        cache.get(index % max(1, len(cache)))
    cached_lookup_ms = ((time.perf_counter() - started) * 1000.0) / max(1, min(1000, len(cache) * 10))
    tile_recipe = {**visual_recipe, "kind": "tileset", "seed": 7}
    started = time.perf_counter()
    _indexed_png(tile_recipe, style, 7)
    tile_seconds = time.perf_counter() - started
    started = time.perf_counter()
    _ = sprite_hashes + sound_hashes
    cached_batch_seconds = time.perf_counter() - started
    measurements = {
        "sprite_batch": {"count": sprite_count, "seconds": sprite_seconds, "limit": 10.0, "passed": sprite_seconds <= 10.0},
        "sound_batch": {"count": sound_count, "seconds": sound_seconds, "limit": 15.0, "passed": sound_seconds <= 15.0},
        "runtime_compose": {"milliseconds": runtime_ms, "limit": 50.0, "passed": runtime_ms <= 50.0},
        "cached_lookup": {"milliseconds": cached_lookup_ms, "limit": 1.0, "passed": cached_lookup_ms <= 1.0},
        "tile_atlas": {"seconds": tile_seconds, "limit": 1.0, "passed": tile_seconds <= 1.0},
        "cached_batch": {"seconds": cached_batch_seconds, "limit": 1.0, "passed": cached_batch_seconds <= 1.0},
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if all(value["passed"] for value in measurements.values()) else "failed",
        "measurements": measurements,
        "environment": {"cpu_only": True, "project": str(project)},
    }
