from __future__ import annotations

import hashlib
import io
import json
import math
import random
import re
import subprocess
import tempfile
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
BODY_FAMILY_DEFINITIONS = {
    "humanoid": {
        "required_slots": ["shadow", "body", "legs", "head"],
        "forbidden_slots": [],
        "allowed_tags": ["biped", "cloth", "upright", "weapon", "magic"],
        "occupancy_signature": "upright_two_leg",
    },
    "serpentine": {
        "required_slots": ["shadow", "body", "head"],
        "forbidden_slots": ["legs", "front_weapon", "offhand"],
        "allowed_tags": ["elongated", "limbless", "scaled", "s_curve"],
        "occupancy_signature": "continuous_s_curve",
    },
    "quadruped": {
        "required_slots": ["shadow", "body", "legs", "head"],
        "forbidden_slots": ["front_weapon", "offhand"],
        "allowed_tags": ["four_legged", "furred", "low_profile"],
        "occupancy_signature": "four_contact_low_body",
    },
    "winged": {
        "required_slots": ["shadow", "rear_effect", "body", "head"],
        "forbidden_slots": ["legs", "front_weapon", "offhand"],
        "allowed_tags": ["winged", "feathered", "wide_span"],
        "occupancy_signature": "bilateral_wing_span",
    },
    "amorphous": {
        "required_slots": ["shadow", "body"],
        "forbidden_slots": ["legs", "head", "front_weapon", "offhand"],
        "allowed_tags": ["blob", "gelatinous", "irregular"],
        "occupancy_signature": "single_irregular_mass",
    },
    "mechanical_vehicle": {
        "required_slots": ["shadow", "body", "torso"],
        "forbidden_slots": ["legs", "head", "front_weapon", "offhand"],
        "allowed_tags": ["wheeled", "metal", "chassis"],
        "occupancy_signature": "wide_chassis",
    },
}
NEUTRAL_ANCHORS = [
    "origin",
    "ground",
    "center",
    "head",
    "action_primary",
    "action_secondary",
    "projectile",
    "effect",
]
LEGACY_ANCHOR_MAP = {
    "feet": "ground",
    "main_hand": "action_primary",
    "off_hand": "action_secondary",
    "muzzle": "projectile",
}
STYLE_GENERATION_FIELDS = (
    "grid_size",
    "frame_size",
    "tile_size",
    "perspective",
    "directions",
    "palette",
    "layer_slots",
    "outline",
    "lighting",
    "silhouettes",
    "animation_defaults",
    "audio_vocabulary",
    "blueprint_fingerprint",
    "media_direction_fingerprint",
)
MEDIA_TERMS = re.compile(
    r"\b(visual|audiovisual|animation|sprite|portrait|icon|ui|hud|menu|cursor|tile|terrain|"
    r"particle|effect|telegraph|audio|sound|sfx|music|ambience|controller|rumble|feedback)\b",
    re.IGNORECASE,
)


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


def _safe_asset_relative(value: str) -> Path:
    candidate = Path(value.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or len(candidate.parts) < 2:
        raise WorkflowError(f"Unsafe asset path: {value!r}")
    if candidate.parts[0] != "assets":
        raise WorkflowError(f"Runtime asset must be below assets/: {value!r}")
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


def _collect_needs(
    project: Path,
    *,
    require_finalized: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    if require_finalized:
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
        "moment_to_moment_loop": "moment-to-moment feedback",
        "session_loop": "session-loop progress presentation",
        "long_term_loop": "long-term progression presentation",
        "opening": "opening presentation",
        "first_minute": "first-minute onboarding presentation",
        "tutorial": "tutorial visuals",
        "first_meaningful_decision": "first meaningful decision presentation",
        "final_challenge": "final challenge presentation and telegraph",
        "recovery": "loss recovery presentation",
        "ending": "ending presentation",
        "credits": "credits presentation",
        "postgame": "postgame presentation",
        "replay": "replay presentation",
        "endgame": "endgame presentation",
        "save_and_recovery": "save, interruption, and recovery presentation",
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

    def add_nested(field: str, value: Any, label: str, default_kind: str = "ui", path: str = "") -> None:
        source = f"{blueprint_id}.{field}{path}"
        if isinstance(value, dict):
            for key in sorted(value):
                add_nested(field, value[key], label, default_kind, f"{path}.{key}")
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                add_nested(field, item, label, default_kind, f"{path}[{index}]")
            return
        if value is None or (isinstance(value, str) and not value.strip()):
            return
        purpose = f"{label}: {value}"
        add(source, purpose, _kind_for(purpose, hint=default_kind))

    for field, label in (
        ("player_verbs", "player-verb animation and feedback"),
        ("setbacks", "setback feedback and recovery cue"),
        ("final_prerequisites", "final-challenge prerequisite indicator"),
        ("win_conditions", "win-state presentation"),
        ("loss_conditions", "loss-state presentation"),
        ("alternate_endings", "alternate-ending presentation"),
        ("ux_information", "required HUD information"),
        ("systems_overview", "system-state feedback"),
        ("accessibility", "accessible non-exclusive feedback"),
    ):
        add_nested(field, blueprint.get(field, []), label, "ui")
    narrative = blueprint.get("narrative", {})
    if isinstance(narrative, dict) and narrative.get("applicability") != "not_applicable":
        for field in ("premise", "character_arcs", "acts", "resolution"):
            add_nested("narrative", narrative.get(field), "narrative presentation", "ui", f".{field}")
    add_nested("progression_system", blueprint.get("progression_system", {}), "progression feedback", "ui")
    add_nested("economy", blueprint.get("economy", {}), "economy icon and feedback", "ui")
    add_nested("difficulty", blueprint.get("difficulty", {}), "difficulty and assist feedback", "ui")
    add_nested("platform_and_input", blueprint.get("platform_and_input", {}), "platform and input glyph", "ui")
    for generator_field in (
        "procedural_generators",
        "stage_generators",
        "enemy_generators",
        "procedural_pools",
    ):
        if generator_field in blueprint:
            add_nested(
                generator_field,
                blueprint[generator_field],
                "procedural generator source pool",
                "tileset" if "stage" in generator_field else "sprite",
            )

    dod_records = [_load_json(path) for path in sorted((project / "work" / "concept").glob("GDD-*.json"))]
    for dod in dod_records:
        for criterion in dod.get("criteria", []):
            text = " ".join(
                [str(criterion.get("title", "")), *map(str, criterion.get("acceptance_criteria", []))]
            )
            if criterion.get("release_blocking") and MEDIA_TERMS.search(text):
                add(
                    f"{dod.get('id')}.criteria.{criterion.get('id')}",
                    f"Release-blocking media Definition of Done: {text}",
                    _kind_for(text),
                )
    requirement_records = [
        _load_json(path)
        for path in sorted((project / "work" / "requirements").glob("REQ-*.json"))
        if "materialization_key" not in _load_json(path)
    ]
    for requirement in requirement_records:
        if requirement.get("status") == "deprecated":
            continue
        text = " ".join(
            [
                str(requirement.get("title", "")),
                str(requirement.get("player_value", "")),
                *map(str, requirement.get("acceptance_criteria", [])),
            ]
        )
        if MEDIA_TERMS.search(text):
            add(
                f"{requirement.get('id')}.media",
                f"Requirement-bound media: {text}",
                _kind_for(text),
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
        "definition_of_done": dod_records,
        "requirements": requirement_records,
    }
    return canonical, fingerprint(source_material)


def _structured_media_contract(
    project: Path,
    needs: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
    direction_path = project / "work" / "concept" / "MDR-0001.json"
    request_paths = sorted(
        (project / "work" / "concept" / "media_requests").glob("ARQ-*.json")
    )
    errors: list[str] = []
    missing_fields: list[str] = []
    schemas_valid = True
    direction: dict[str, Any] | None = None
    if not direction_path.is_file():
        schemas_valid = False
        errors.append("MDR-0001 media direction is missing")
        missing_fields.append("MDR-0001")
    else:
        try:
            direction = _load_json(direction_path)
        except WorkflowError as error:
            schemas_valid = False
            errors.append(str(error))
        else:
            direction_errors = _schema_errors(direction, "media-direction.schema.json")
            schemas_valid = schemas_valid and not direction_errors
            errors.extend(f"MDR-0001: {message}" for message in direction_errors)
            missing_fields.extend(f"MDR-0001:{message.split(':', 1)[0]}" for message in direction_errors)
            if not _validate_fingerprint(direction):
                errors.append("MDR-0001: input fingerprint does not match current content")
            if direction.get("status") != "approved":
                errors.append("MDR-0001: media direction is not approved")
    requests: list[dict[str, Any]] = []
    for path in request_paths:
        try:
            request = _load_json(path)
        except WorkflowError as error:
            schemas_valid = False
            errors.append(str(error))
            continue
        request_id = str(request.get("id", path.stem))
        request_errors = _schema_errors(request, "media-request.schema.json")
        schemas_valid = schemas_valid and not request_errors
        errors.extend(f"{request_id}: {message}" for message in request_errors)
        missing_fields.extend(
            f"{request_id}:{message.split(':', 1)[0]}" for message in request_errors
        )
        if not _validate_fingerprint(request):
            errors.append(f"{request_id}: input fingerprint does not match current content")
        if request.get("status") != "approved":
            errors.append(f"{request_id}: media request is not approved")
        requests.append(request)
    mapped: dict[str, list[str]] = {}
    for request in requests:
        for source_ref in request.get("source_refs", []):
            mapped.setdefault(str(source_ref), []).append(str(request.get("id")))
    expected_sources = {str(need["source_ref"]) for need in needs}
    missing_request_ids = [
        f"ARQ-{index:04d}"
        for index, need in enumerate(needs, 1)
        if str(need["source_ref"]) not in mapped
    ]
    for source_ref in sorted(expected_sources - set(mapped)):
        errors.append(f"unmapped blueprint media source {source_ref}")
    for source_ref in sorted(set(mapped) - expected_sources):
        errors.append(f"stale blueprint media source {source_ref}")
    for source_ref, request_ids in sorted(mapped.items()):
        if len(request_ids) != 1:
            errors.append(
                f"duplicate blueprint media source {source_ref}: {', '.join(request_ids)}"
            )
    if schemas_valid and direction is not None and requests:
        try:
            from .concept import _media_contract_errors

            errors.extend(_media_contract_errors(project, direction, requests))
        except (ImportError, AttributeError, KeyError, TypeError, ValueError) as error:
            errors.append(f"Could not validate structured media contracts: {error}")
    if errors:
        return direction, requests, {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked",
            "operation": "plan",
            "restart_stage": "asset_specification",
            "missing_request_ids": sorted(set(missing_request_ids)),
            "missing_fields": sorted(set(missing_fields)),
            "errors": sorted(set(errors)),
        }
    return direction, requests, None


def _planning_requests(
    needs: list[dict[str, Any]],
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    need_by_source = {str(need["source_ref"]): need for need in needs}
    planned: list[dict[str, Any]] = []
    for request in sorted(requests, key=lambda value: str(value["id"])):
        sources = sorted(map(str, request["source_refs"]))
        planned.append(
            {
                "source_ref": sources[0],
                "source_refs": sources,
                "purpose": str(request["purpose"]),
                "kind": str(request["family"]),
                "required": any(bool(need_by_source[source]["required"]) for source in sources),
                "request": request,
            }
        )
    return planned


def _style_record(
    blueprint_fingerprint: str,
    direction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    art = (direction or {}).get("art", {})
    audio = (direction or {}).get("audio", {})
    palette = art.get("palette") or {
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
    }
    return _record(
        {
            "name": "Core Top-down Pixel Media v1 (16-128 px)",
            "grid_size": 16,
            "frame_size": art.get("frame_size", [16, 16]),
            "tile_size": art.get("tile_size", [16, 16]),
            "perspective": "top_down",
            "directions": DIRECTIONS,
            "palette": palette,
            "layer_slots": LAYER_SLOTS,
            "outline": {"description": art.get("outline", "one logical pixel"), "width_pixels": 1, "selective": True},
            "lighting": {"direction": art.get("light_direction", "upper_left"), "levels": 3},
            "silhouettes": {"minimum_negative_space_pixels": 2},
            "animation_defaults": {clip["name"]: clip for clip in BASELINE_CLIPS},
            "audio_vocabulary": {
                "safe": ["sine", "triangle"],
                "danger": ["square", "noise"],
                "mechanical": ["pulse", "saw"],
                "bpm": 120,
                "key": "A minor",
                "bars": 4,
                "identity": audio.get("identity", "mechanical pixel media"),
            },
            "blueprint_fingerprint": blueprint_fingerprint,
            "media_direction_id": (direction or {}).get("id"),
            "media_direction_fingerprint": (direction or {}).get("input_fingerprint"),
        },
        record_id="STY-0001",
        status="draft",
    )


def _style_generation_material(style: dict[str, Any]) -> dict[str, Any]:
    return {field: style.get(field) for field in STYLE_GENERATION_FIELDS}


def _style_generation_fingerprint(style: dict[str, Any]) -> str:
    return fingerprint(_style_generation_material(style))


def _part_records(style: dict[str, Any]) -> list[dict[str, Any]]:
    templates = [
        ("humanoid", "shadow", [[5, 13, "shadow"], [6, 13, "shadow"], [7, 13, "shadow"], [8, 13, "shadow"], [9, 13, "shadow"], [10, 13, "shadow"]], ["biped", "upright"], True),
        ("humanoid", "body", [[6, 7, "primary"], [7, 6, "light"], [8, 6, "primary"], [9, 7, "primary"], [7, 8, "primary"], [8, 8, "primary"]], ["biped", "cloth", "upright"], True),
        ("humanoid", "legs", [[6, 10, "dark"], [7, 10, "dark"], [8, 10, "dark"], [9, 10, "dark"], [6, 11, "mid"], [9, 11, "mid"]], ["biped", "cloth", "upright"], True),
        ("humanoid", "head", [[7, 4, "light"], [8, 4, "light"], [6, 5, "mid"], [7, 5, "light"], [8, 5, "light"], [9, 5, "mid"]], ["biped", "upright"], True),
        ("humanoid", "front_weapon", [[10, 7, "secondary"], [11, 7, "secondary"], [12, 7, "light"], [13, 7, "light"]], ["weapon"], False),
        ("humanoid", "front_effect", [[11, 6, "danger"], [12, 5, "secondary"], [13, 4, "light"]], ["magic"], False),
        ("serpentine", "shadow", [[3, 13, "shadow"], [4, 13, "shadow"], [5, 13, "shadow"], [6, 13, "shadow"], [7, 13, "shadow"], [8, 13, "shadow"], [9, 13, "shadow"], [10, 13, "shadow"], [11, 13, "shadow"], [12, 13, "shadow"]], ["elongated", "limbless", "s_curve"], True),
        ("serpentine", "body", [[4, 11, "primary"], [5, 11, "light"], [6, 10, "primary"], [7, 9, "primary"], [8, 9, "light"], [9, 10, "primary"], [10, 11, "primary"], [11, 11, "mid"], [12, 10, "mid"], [3, 12, "dark"], [4, 12, "primary"]], ["elongated", "limbless", "scaled", "s_curve"], True),
        ("serpentine", "head", [[7, 6, "light"], [8, 6, "light"], [6, 7, "primary"], [7, 7, "primary"], [8, 7, "primary"], [9, 7, "primary"], [9, 6, "outline"]], ["elongated", "limbless", "scaled", "s_curve"], True),
        ("quadruped", "shadow", [[3, 12, "shadow"], [4, 12, "shadow"], [5, 12, "shadow"], [6, 12, "shadow"], [7, 12, "shadow"], [8, 12, "shadow"], [9, 12, "shadow"], [10, 12, "shadow"], [11, 12, "shadow"], [12, 12, "shadow"]], ["four_legged", "low_profile"], True),
        ("quadruped", "body", [[4, 7, "primary"], [5, 7, "light"], [6, 7, "primary"], [7, 7, "primary"], [8, 7, "primary"], [9, 7, "primary"], [10, 7, "dark"], [4, 8, "primary"], [5, 8, "primary"], [6, 8, "primary"], [7, 8, "primary"], [8, 8, "primary"], [9, 8, "primary"], [10, 8, "dark"]], ["four_legged", "furred", "low_profile"], True),
        ("quadruped", "legs", [[4, 9, "dark"], [4, 10, "mid"], [6, 9, "dark"], [6, 10, "mid"], [9, 9, "dark"], [9, 10, "mid"], [11, 9, "dark"], [11, 10, "mid"]], ["four_legged", "furred", "low_profile"], True),
        ("quadruped", "head", [[11, 6, "light"], [12, 6, "primary"], [11, 7, "primary"], [12, 7, "primary"], [13, 7, "outline"]], ["four_legged", "furred", "low_profile"], True),
        ("winged", "shadow", [[4, 13, "shadow"], [5, 13, "shadow"], [6, 13, "shadow"], [7, 13, "shadow"], [8, 13, "shadow"], [9, 13, "shadow"], [10, 13, "shadow"], [11, 13, "shadow"]], ["winged", "wide_span"], True),
        ("winged", "rear_effect", [[1, 6, "mid"], [2, 5, "light"], [3, 6, "mid"], [4, 7, "primary"], [11, 7, "primary"], [12, 6, "mid"], [13, 5, "light"], [14, 6, "mid"]], ["winged", "feathered", "wide_span"], True),
        ("winged", "body", [[7, 7, "primary"], [8, 7, "primary"], [6, 8, "primary"], [7, 8, "light"], [8, 8, "primary"], [9, 8, "primary"]], ["winged", "feathered", "wide_span"], True),
        ("winged", "head", [[7, 5, "light"], [8, 5, "light"], [7, 6, "primary"], [8, 6, "primary"], [9, 6, "outline"]], ["winged", "feathered", "wide_span"], True),
        ("amorphous", "shadow", [[4, 13, "shadow"], [5, 13, "shadow"], [6, 13, "shadow"], [7, 13, "shadow"], [8, 13, "shadow"], [9, 13, "shadow"], [10, 13, "shadow"], [11, 13, "shadow"]], ["blob", "irregular"], True),
        ("amorphous", "body", [[5, 8, "primary"], [6, 7, "primary"], [7, 6, "light"], [8, 7, "primary"], [9, 6, "primary"], [10, 8, "primary"], [4, 10, "dark"], [5, 9, "primary"], [6, 9, "primary"], [7, 9, "light"], [8, 9, "primary"], [9, 9, "primary"], [10, 9, "primary"], [11, 10, "dark"], [5, 11, "mid"], [6, 11, "mid"], [7, 11, "mid"], [8, 11, "mid"], [9, 11, "mid"], [10, 11, "mid"]], ["blob", "gelatinous", "irregular"], True),
        ("mechanical_vehicle", "shadow", [[2, 13, "shadow"], [3, 13, "shadow"], [4, 13, "shadow"], [5, 13, "shadow"], [6, 13, "shadow"], [7, 13, "shadow"], [8, 13, "shadow"], [9, 13, "shadow"], [10, 13, "shadow"], [11, 13, "shadow"], [12, 13, "shadow"], [13, 13, "shadow"]], ["wheeled", "chassis"], True),
        ("mechanical_vehicle", "body", [[3, 8, "dark"], [4, 7, "primary"], [5, 7, "primary"], [6, 7, "primary"], [7, 7, "light"], [8, 7, "primary"], [9, 7, "primary"], [10, 7, "primary"], [11, 8, "dark"], [3, 9, "mid"], [4, 9, "primary"], [5, 9, "primary"], [6, 9, "primary"], [7, 9, "primary"], [8, 9, "primary"], [9, 9, "primary"], [10, 9, "primary"], [11, 9, "mid"]], ["wheeled", "metal", "chassis"], True),
        ("mechanical_vehicle", "torso", [[5, 5, "dark"], [6, 5, "mid"], [7, 5, "light"], [8, 5, "mid"], [9, 5, "dark"], [6, 6, "primary"], [7, 6, "primary"], [8, 6, "primary"]], ["wheeled", "metal", "chassis"], True),
    ]
    anchors = {
        "origin": [8, 8],
        "ground": [8, 13],
        "center": [8, 8],
        "head": [8, 4],
        "action_primary": [10, 8],
        "action_secondary": [5, 8],
        "projectile": [14, 7],
        "effect": [12, 6],
    }
    records = []
    for index, (family, slot, pixels, tags, required) in enumerate(templates, 1):
        coordinates = sorted({(int(pixel[0]), int(pixel[1])) for pixel in pixels})
        direction_occupancy = {
            "down": [[x, y] for x, y in coordinates],
            "left": [[15 - x, y] for x, y in coordinates],
            "right": [[x, y] for x, y in coordinates],
            "up": [[x, max(0, y - 1)] for x, y in coordinates],
        }
        occupancy = sorted(
            {
                (int(point[0]), int(point[1]))
                for points in direction_occupancy.values()
                for point in points
            }
        )
        records.append(
            _record(
                {
                    "name": f"Core {family.replace('_', ' ').title()} {slot.replace('_', ' ').title()}",
                    "slot": slot,
                    "pixels": pixels,
                    "anchors": anchors,
                    "occupancy_mask": [[x, y] for x, y in occupancy],
                    "direction_occupancy": direction_occupancy,
                    "occupancy_signature": BODY_FAMILY_DEFINITIONS[family]["occupancy_signature"],
                    "occlusion_mask": [],
                    "compatible_body_families": [family],
                    "compatible_animations": ["*"],
                    "compatible_directions": DIRECTIONS,
                    "mirror_safe": True,
                    "direction_pixels": None,
                    "tags": tags,
                    "required_for_family": required,
                    "license": "CC0-1.0",
                    "sha256": fingerprint({"style": style["input_fingerprint"], "family": family, "pixels": pixels}),
                    "provenance": {
                        "method": "original procedural coordinates",
                        "source_pack": "core-topdown-v2",
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


def _external_body_family_definitions(project: Path) -> dict[str, dict[str, Any]]:
    definitions: dict[str, dict[str, Any]] = {}
    allowed_licenses = {
        "original",
        "CC0-1.0",
        "CC-BY-4.0",
        "Apache-2.0",
        "proprietary",
        "commercial",
    }
    packs_root = project / "assets" / "source" / "packs"
    for path in sorted(packs_root.glob("*/pack.json")):
        if path.parent.name == "core-topdown-v1":
            continue
        pack = _load_json(path)
        family_values = pack.get("body_families")
        if not isinstance(family_values, dict):
            continue
        pack_id = str(pack.get("id", path.parent.name))
        material = {key: value for key, value in pack.items() if key != "sha256"}
        if (
            pack.get("status") != "approved"
            or pack.get("license") not in allowed_licenses
            or pack.get("sha256") != fingerprint(material)
            or not isinstance(pack.get("provenance"), (dict, str))
        ):
            raise WorkflowError(
                f"External source pack {pack_id} is not approved or has invalid license/provenance/checksum"
            )
        for family, value in family_values.items():
            family_name = str(family)
            if family_name in BODY_FAMILY_DEFINITIONS or family_name in definitions:
                raise WorkflowError(
                    f"External source pack {pack_id} duplicates body family {family_name}"
                )
            if not isinstance(value, dict):
                raise WorkflowError(f"External body family {family_name} must be an object")
            required = {
                "required_slots",
                "forbidden_slots",
                "allowed_tags",
                "occupancy_signature",
            }
            if required - set(value):
                raise WorkflowError(
                    f"External body family {family_name} is incomplete: {sorted(required - set(value))}"
                )
            definitions[family_name] = {
                "required_slots": list(map(str, value["required_slots"])),
                "forbidden_slots": list(map(str, value["forbidden_slots"])),
                "allowed_tags": list(map(str, value["allowed_tags"])),
                "occupancy_signature": str(value["occupancy_signature"]),
                "source_pack": pack_id,
            }
    return definitions


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
    direction: dict[str, Any],
    family_definitions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]] | dict[str, Any]]:
    style = _style_record(blueprint_fingerprint, direction)
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
    briefs: list[dict[str, Any]] = []
    recipes: list[dict[str, Any]] = []
    animations: list[dict[str, Any]] = []
    tiles: list[dict[str, Any]] = []
    particles: list[dict[str, Any]] = []
    sounds: list[dict[str, Any]] = []
    coverage = []
    animation_index = tile_index = particle_index = sound_index = 0
    for index, need in enumerate(needs, 1):
        asset_id = f"ASP-{index:04d}"
        brief_id = f"ABR-{index:04d}"
        recipe_id = f"RCP-{index:04d}"
        kind = str(need["kind"])
        request = dict(need["request"])
        request_specification = dict(request["specification"])
        variants = (
            int(request_specification.get("variants", 1))
            if kind in {"sound", "ambience", "music"}
            else 1
        )
        outputs = _output_paths(kind, str(need["purpose"]), index, variants)
        actual_formats = {Path(path).suffix.lower().lstrip(".") for path in outputs}
        declared_formats = set(map(str, request["output_contract"]["runtime_formats"]))
        if actual_formats != declared_formats:
            raise WorkflowError(
                f"{request['id']}: output formats {sorted(declared_formats)} do not match supported {kind} outputs {sorted(actual_formats)}"
            )
        body_family = str(request_specification.get("body_family") or "")
        subject_kind = str(request_specification.get("subject_kind") or "")
        if kind in {"sprite", "animation"} and subject_kind == "actor":
            family_definition = (family_definitions or BODY_FAMILY_DEFINITIONS).get(body_family)
            if not family_definition:
                raise WorkflowError(
                    f"{request['id']}: unsupported body family {body_family!r}; provide an approved compatible source pack"
                )
            requested_tags = {
                str(tag)
                for field in (
                    "body_tags",
                    "surface_tags",
                    "silhouette_tags",
                    "equipment_tags",
                )
                for tag in request_specification.get(field, [])
            }
            disallowed_tags = sorted(
                requested_tags - set(map(str, family_definition["allowed_tags"]))
            )
            if disallowed_tags:
                raise WorkflowError(
                    f"{request['id']}: body family {body_family} does not allow tags {disallowed_tags}"
                )
            available_tags = {
                str(tag)
                for part in parts
                if body_family in part.get("compatible_body_families", [])
                for tag in part.get("tags", [])
            }
            unsupported_tags = sorted(requested_tags - available_tags)
            if unsupported_tags:
                raise WorkflowError(
                    f"{request['id']}: no compatible {body_family} parts provide tags {unsupported_tags}"
                )
        else:
            family_definition = None
            requested_tags = set()
        compatible_parts = [
            record
            for record in parts
            if body_family in record.get("compatible_body_families", [])
            and (
                bool(record.get("required_for_family"))
                or bool(requested_tags & set(map(str, record.get("tags", []))))
            )
        ]
        if family_definition:
            selected_slots = {str(part.get("slot")) for part in compatible_parts}
            missing_slots = set(map(str, family_definition["required_slots"])) - selected_slots
            forbidden_slots = set(map(str, family_definition["forbidden_slots"])) & selected_slots
            if missing_slots or forbidden_slots:
                raise WorkflowError(
                    f"{request['id']}: incompatible {body_family} part contract; "
                    f"missing slots {sorted(missing_slots)}, forbidden slots {sorted(forbidden_slots)}"
                )
            for part in compatible_parts:
                direction_occupancy = part.get("direction_occupancy")
                if (
                    not isinstance(direction_occupancy, dict)
                    or set(direction_occupancy) != set(DIRECTIONS)
                    or any(not direction_occupancy[direction] for direction in DIRECTIONS)
                    or part.get("occupancy_signature")
                    != family_definition["occupancy_signature"]
                ):
                    raise WorkflowError(
                        f"{request['id']}: source part {part['id']} does not satisfy the {body_family} four-direction occupancy signature"
                    )
        source_part_ids = (
            [record["id"] for record in compatible_parts]
            if kind in {"sprite", "animation"} and subject_kind == "actor"
            else []
        )
        frame_dimensions = list(request_specification.get("dimensions", style["frame_size"]))
        parameters: dict[str, Any] = {
            "grid_size": 16,
            "frame_dimensions": frame_dimensions,
            "palette_id": style["id"],
            "palette": style["palette"],
            "style_fingerprint": _style_generation_fingerprint(style),
            "body_family": body_family,
            "subject_kind": subject_kind,
            "subtype": request["subtype"],
            "required_directions": DIRECTIONS,
            "purpose": need["purpose"],
            "request_id": request["id"],
            "request_fingerprint": request["input_fingerprint"],
            "media_direction_fingerprint": direction["input_fingerprint"],
            "output_license": "CC0-1.0",
        }
        if kind in {"sprite", "animation"} and subject_kind == "actor":
            layer_choices = {
                slot: [part["id"] for part in compatible_parts if part.get("slot") == slot]
                for slot in LAYER_SLOTS
                if any(part.get("slot") == slot for part in compatible_parts)
            }
            parameters["layer_choices"] = layer_choices
            parameters["layers"] = [
                {"slot": slot, "part_id": choices[0], "offset": [0, 0]}
                for slot, choices in layer_choices.items()
            ]
            parameters["source_part_hashes"] = {
                part["id"]: part["input_fingerprint"] for part in compatible_parts
            }
            parameters["source_part_licenses"] = {
                part["id"]: part["license"] for part in compatible_parts
            }
            parameters["output_license"] = _effective_output_license(compatible_parts)
        if kind == "animation":
            animation_index += 1
            animation_id = f"ANI-{animation_index:04d}"
            clips = [dict(clip) for clip in request_specification["clips"]]
            animation = _record(
                {
                    "asset_spec_id": asset_id,
                    "brief_id": brief_id,
                    "body_family": body_family,
                    "directions": DIRECTIONS,
                    "clips": clips,
                    "anchors": NEUTRAL_ANCHORS,
                    "root_motion": request_specification["root_motion"],
                    "interruptibility": request_specification["interruptibility"],
                    "mechanic_state_bindings": request_specification["mechanic_state_bindings"],
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
                    "brief_id": brief_id,
                    "biome": str(request_specification["biome"]),
                    "grid_size": int(frame_dimensions[0]),
                    "tile_size": frame_dimensions,
                    "terrain_roles": request_specification["terrain_roles"],
                    "materials": request_specification["materials"],
                    "transitions": request_specification["transitions"],
                    "hazards": request_specification["hazards"],
                    "variations": request_specification["variations"],
                    "tiles": [
                        {"id": f"terrain-{mask:03d}", "role": "terrain", "weight": 1.0, "collision": mask != 255, "navigation": mask == 255}
                        for mask in range(256)
                    ],
                    "adjacency": {"encoding": "8-neighbor-bitmask", "opposite_edges_must_match": True},
                    "required_neighbor_masks": list(range(256)),
                    "stage_constraints": {
                        **request_specification["stage_constraints"],
                        "entrance_exit_connected": True,
                        "required_encounters": [],
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
                    "brief_id": brief_id,
                    "name": str(need["purpose"]),
                    "texture": outputs[0],
                    "seed": index * 1009,
                    "amount": min(32, int(request_specification["budgets"].get("max_particles", 24))),
                    "lifetime": max(request_specification["event_timing"].values()) or 0.6,
                    "fixed_fps": 12,
                    "motion": {"direction_degrees": -90, "spread_degrees": 55, "velocity": [24, 52], "gravity": [0, 20]},
                    "colors": ["#f7c95cff", "#e66b3dff", "#d94b64ff", "#00000000"],
                    "cpu_fallback": True,
                    "ownership": request_specification["ownership"],
                    "phases": request_specification["phases"],
                    "event_timing": request_specification["event_timing"],
                    "shape_cues": request_specification["shape_cues"],
                    "budget": request_specification["budgets"],
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
                    "brief_id": brief_id,
                    "name": str(need["purpose"]),
                    "category": category,
                    "event": str(need["source_ref"]),
                    "layers": [
                        {"waveform": "square" if category == "sfx" else "triangle", "frequency": 220.0, "volume": 0.65},
                        {"waveform": "noise" if category == "sfx" else "sine", "frequency": 110.0, "volume": 0.25},
                    ],
                    "event_tags": request_specification["event_tags"],
                    "material_tags": request_specification["material_tags"],
                    "duration_seconds": float(request_specification["duration_seconds"]),
                    "sample_rate": 48000,
                    "channels": 1 if category == "sfx" else 2,
                    "variants": int(request_specification["variants"]),
                    "peak_dbfs": -1.0,
                    "loop": bool(request_specification["loop"]),
                    "bpm": request_specification.get("tempo"),
                    "meter": request_specification.get("meter"),
                    "key": request_specification.get("key"),
                    "bars": 4 if category == "music" else None,
                    "stems": request_specification.get("stems", []),
                    "transition_points": request_specification.get("transitions", []),
                    "loudness_lufs": request_specification.get("loudness_lufs"),
                    "synchronization": request_specification["synchronization"],
                    "spatial_behavior": request_specification["spatial_behavior"],
                    "priority": request_specification["priority"],
                    "concurrency": request_specification["concurrency"],
                    "accessibility_alternative": "A synchronized visual state indicator communicates the same event.",
                    "mutation": {"pitch_semitones": [-0.4, 0.4], "volume_db": [-1.0, 0.0]},
                    "adsr": request_specification["envelope"],
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
        brief = {
            "schema_version": "2.0",
            "id": brief_id,
            "family": kind,
            "subtype": request["subtype"],
            "purpose": request["purpose"],
            "project_context": {
                "media_direction_id": direction["id"],
                "media_direction_fingerprint": direction["input_fingerprint"],
            },
            "request_id": request["id"],
            "request_fingerprint": request["input_fingerprint"],
            "source_refs": list(need["source_refs"]),
            "dependencies": request["dependencies"],
            "references": request["references"],
            "control": request["control"],
            "specification": request_specification,
            "output_contract": {
                **request["output_contract"],
                "runtime_paths": outputs,
            },
            "accessibility": request["accessibility"],
            "acceptance_criteria": request["acceptance_criteria"],
            "license_allowlist": [
                "original",
                "CC0-1.0",
                "CC-BY-4.0",
                "Apache-2.0",
                "proprietary",
                "commercial",
            ],
        }
        brief["input_fingerprint"] = fingerprint(brief)
        _assert_schema(brief, "asset-brief.schema.json")
        briefs.append(brief)
        recipe = _record(
            {
                "kind": kind,
                "asset_spec_id": asset_id,
                "brief_id": brief_id,
                "brief_fingerprint": brief["input_fingerprint"],
                "seed": int(request["control"]["seed"]),
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
                    "brief_id": brief_id,
                    "brief_fingerprint": brief["input_fingerprint"],
                    "request_id": request["id"],
                    "purpose": need["purpose"],
                    "source_refs": list(need["source_refs"]),
                    "runtime_paths": outputs,
                    "recipe_ids": [recipe_id],
                    "required": bool(need["required"]),
                    "acceptance_criteria": request["acceptance_criteria"],
                },
                record_id=asset_id,
                status="planned",
            )
        )
        coverage.extend(
            {"source_ref": source_ref, "request_id": request["id"], "asset_spec_ids": [asset_id]}
            for source_ref in need["source_refs"]
        )
    plan = _record(
        {
            "blueprint_fingerprint": blueprint_fingerprint,
            "style_pack_id": style["id"],
            "asset_spec_ids": [record["id"] for record in specs],
            "brief_ids": [record["id"] for record in briefs],
            "media_direction_id": direction["id"],
            "media_direction_fingerprint": direction["input_fingerprint"],
            "coverage": coverage,
            "generation_profile": "pixel-media-v1",
        },
        record_id="APL-0001",
        status="inventoried",
    )
    pack_material = {
        "schema_version": SCHEMA_VERSION,
        "id": "core-topdown-v1",
        "name": "Core Top-down Pixel Parts (16-128 px outputs)",
        "pack_revision": 2,
        "license": "CC0-1.0",
        "grid_size": 16,
        "perspective": "top_down",
        "directions": DIRECTIONS,
        "layer_slots": LAYER_SLOTS,
        "body_families": BODY_FAMILY_DEFINITIONS,
        "part_ids": [part["id"] for part in core_parts],
        "part_hashes": {part["id"]: part["input_fingerprint"] for part in core_parts},
        "provenance": "Original procedural pixel coordinates; no LPC or third-party artwork is bundled.",
    }
    pack = {**pack_material, "sha256": fingerprint(pack_material)}
    return {
        "plan": plan,
        "styles": [style],
        "briefs": briefs,
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
        "briefs": "briefs",
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


def _reconcile_planned_records(
    project: Path,
    records: dict[str, Any],
    destinations: list[tuple[Path, dict[str, Any]]],
) -> None:
    previous_recipes = {
        recipe["id"]: recipe for recipe in _all_records(project, "recipes", "RCP")
    }
    desired_paths = {path.resolve() for path, _ in destinations}
    managed = {
        "briefs": "ABR-*.json",
        "styles": "STY-*.json",
        "parts": "PRT-*.json",
        "specs": "ASP-*.json",
        "recipes": "RCP-*.json",
        "animations": "ANI-*.json",
        "tiles": "TIL-*.json",
        "particles": "PFX-*.json",
        "sounds": "SND-*.json",
    }
    for folder, pattern in managed.items():
        for path in (project / "work" / "assets" / folder).glob(pattern):
            if path.resolve() not in desired_paths:
                path.unlink()

    current_recipes = {recipe["id"]: recipe for recipe in records["recipes"]}
    current_outputs = {
        str(path): (str(recipe["id"]), str(recipe["input_fingerprint"]))
        for recipe in records["recipes"]
        for path in recipe.get("outputs", [])
    }
    for recipe_id, previous in previous_recipes.items():
        current = current_recipes.get(recipe_id)
        if current and current.get("input_fingerprint") == previous.get("input_fingerprint"):
            continue
        for runtime_path in previous.get("outputs", []):
            artifact = project / _safe_relative(str(runtime_path))
            if artifact.is_file():
                artifact.unlink()
    manifest_path = project / "assets" / "asset-manifest.json"
    manifest = _load_json(manifest_path)
    kept_assets: list[dict[str, Any]] = []
    for asset in manifest.get("assets", []):
        provenance = asset.get("provenance", {})
        if provenance.get("provider") != "aigame-local-media":
            kept_assets.append(asset)
            continue
        recipe_id = str(provenance.get("recipe_id", ""))
        recipe = current_recipes.get(recipe_id)
        runtime_path = str(asset.get("runtime_path", ""))
        if (
            recipe
            and provenance.get("recipe_sha256") == recipe.get("input_fingerprint")
            and current_outputs.get(runtime_path)
            == (recipe_id, str(recipe.get("input_fingerprint")))
        ):
            kept_assets.append(asset)
    if kept_assets != manifest.get("assets", []):
        _write_json(
            manifest_path,
            {
                **manifest,
                "revision": int(manifest.get("revision", 0)) + 1,
                "assets": kept_assets,
            },
        )


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
    direction, requests, blocked = _structured_media_contract(project, needs)
    if blocked:
        return blocked
    assert direction is not None
    planned_requests = _planning_requests(needs, requests)
    builtin_ids = {
        part["id"]
        for part in _part_records(_style_record(blueprint_fingerprint, direction))
    }
    try:
        external_parts = _external_part_records(project, builtin_ids)
        family_definitions = {
            **BODY_FAMILY_DEFINITIONS,
            **_external_body_family_definitions(project),
        }
    except (WorkflowError, ValueError, TypeError, KeyError, AttributeError) as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked",
            "operation": "plan",
            "restart_stage": "asset_specification",
            "missing_request_ids": [],
            "missing_fields": [],
            "errors": [f"External source part or family contract is invalid: {error}"],
        }
    try:
        records = _records_for_plan(
            planned_requests,
            blueprint_fingerprint,
            external_parts,
            direction,
            family_definitions,
        )
    except (WorkflowError, ValueError, TypeError, KeyError, AttributeError) as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked",
            "operation": "plan",
            "restart_stage": "asset_specification",
            "missing_request_ids": [],
            "missing_fields": [],
            "errors": [str(error)],
        }
    for _, record in _record_destinations(project, records):
        prefix = str(record["id"]).split("-")[0]
        _assert_schema(
            record,
            "asset-brief.schema.json" if prefix == "ABR" else SCHEMA_BY_PREFIX[prefix],
        )
    destinations = _record_destinations(project, records)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "dry_run" if not apply else "passed",
        "asset_plan_id": "APL-0001",
        "blueprint_fingerprint": blueprint_fingerprint,
        "counts": {
            "asset_specs": len(records["specs"]),
            "briefs": len(records["briefs"]),
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
    _reconcile_planned_records(project, records, destinations)
    for path, record in destinations:
        _write_json(path, record)
    _write_json(
        project / "assets" / "source" / "packs" / "core-topdown-v1" / "pack.json",
        records["pack"],
    )
    core_parts_folder = (
        project / "assets" / "source" / "packs" / "core-topdown-v1" / "parts"
    )
    desired_core_part_ids = {str(part["id"]) for part in records["core_parts"]}
    for path in core_parts_folder.glob("PRT-*.json"):
        if path.stem not in desired_core_part_ids:
            path.unlink()
    for part in records["core_parts"]:
        _write_json(
            core_parts_folder / f"{part['id']}.json",
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
    representative_ids = _representative_spec_ids(project)
    specs = {
        str(spec["id"]): spec for spec in _all_records(project, "specs", "ASP")
    }
    briefs = {
        str(brief["id"]): brief for brief in _all_records(project, "briefs", "ABR")
    }
    brief_fingerprints = {
        str(specs[spec_id].get("brief_id")): briefs.get(
            str(specs[spec_id].get("brief_id")), {}
        ).get("input_fingerprint")
        for spec_id in representative_ids
        if spec_id in specs
    }
    return {
        "scope_hash": fingerprint(
            {
                "style": _style_generation_material(style),
                "brief_fingerprints": brief_fingerprints,
                "samples": sample_hashes,
            }
        ),
        "commit_sha": commit_sha,
        "decision": "Approve the representative pixel-art and procedural-audio style for bulk generation.",
    }


def _save_approval(project: Path, approval: dict[str, Any]) -> None:
    _write_json(project / "evidence" / "approvals" / f"{approval['id']}.json", approval)


def _representative_spec_ids(project: Path) -> list[str]:
    specs = _all_records(project, "specs", "ASP")
    briefs = {
        str(brief["id"]): brief for brief in _all_records(project, "briefs", "ABR")
    }
    selected_by_contract: dict[tuple[str, ...], str] = {}
    for spec in sorted(specs, key=lambda value: str(value["id"])):
        brief = briefs.get(str(spec.get("brief_id")), {})
        family = str(brief.get("family", spec.get("kind", "")))
        subtype = str(brief.get("subtype", ""))
        body_family = str(brief.get("specification", {}).get("body_family", ""))
        key = (
            (family, subtype, body_family)
            if family in {"sprite", "animation"} and body_family
            else (family, subtype)
        )
        selected_by_contract.setdefault(key, str(spec["id"]))
    return [selected_by_contract[key] for key in sorted(selected_by_contract)]


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
    request = _approval_request(project, style, sample_hashes)
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
    try:
        needs, _ = _collect_needs(project)
    except WorkflowError:
        needs = []
    if needs:
        _, _, blocked = _structured_media_contract(project, needs)
        if blocked:
            return {**blocked, "media_state": status}
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
    parameters = recipe.get("parameters", {})
    clips = list(parameters.get("clips", []))
    dimensions = parameters.get("frame_dimensions", [16, 16])
    frame_width, frame_height = int(dimensions[0]), int(dimensions[1])
    if kind == "animation":
        return frame_width * sum(int(clip["frames"]) for clip in clips), frame_height * 4, clips
    if kind == "tileset":
        return frame_width * 16, frame_height * 16, clips
    return frame_width, frame_height, clips


def _indexed_png(
    recipe: dict[str, Any],
    style: dict[str, Any],
    seed: int,
    parts: list[dict[str, Any]] | None = None,
) -> bytes:
    Image, ImageDraw = _pillow()
    target_width, target_height, clips = _visual_dimensions(recipe)
    if recipe["kind"] == "animation":
        width, height = 16 * sum(int(clip["frames"]) for clip in clips), 16 * 4
    elif recipe["kind"] == "tileset":
        width, height = 16 * 16, 16 * 16
    else:
        width, height = 16, 16
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
        subtype = str(recipe.get("parameters", {}).get("subtype", ""))
        if subtype == "icon":
            draw.rectangle((3, 3, 12, 12), fill=colors["dark"], outline=colors["light"])
            draw.rectangle((6, 5, 9, 10), fill=colors["primary"])
        elif subtype == "cursor":
            draw.polygon([(2, 2), (2, 13), (6, 10), (9, 14), (11, 13), (8, 9), (13, 8)], fill=colors["light"], outline=colors["outline"])
        elif subtype == "marker":
            draw.polygon([(8, 1), (14, 7), (8, 14), (2, 7)], fill=colors["primary"], outline=colors["light"])
            draw.rectangle((7, 5, 8, 9), fill=colors["secondary"])
        elif subtype == "frame":
            draw.rectangle((1, 1, 14, 14), outline=colors["light"], width=2)
            draw.rectangle((3, 3, 12, 12), outline=colors["dark"])
        elif subtype == "nine_slice":
            draw.rectangle((1, 1, 14, 14), fill=colors["dark"], outline=colors["light"])
            for x, y in ((2, 2), (12, 2), (2, 12), (12, 12)):
                draw.rectangle((x, y, x + 1, y + 1), fill=colors["secondary"])
            draw.line((4, 2, 11, 2), fill=colors["mid"])
            draw.line((4, 13, 11, 13), fill=colors["mid"])
        elif subtype == "panel":
            draw.rounded_rectangle((1, 1, 14, 14), radius=2, fill=colors["dark"], outline=colors["light"])
            draw.rectangle((5, 4, 10, 11), fill=colors["primary"])
            draw.point((8, 7), fill=colors["secondary"])
        else:
            raise WorkflowError(f"{recipe['id']}: unsupported UI subtype {subtype!r}")
    elif recipe.get("parameters", {}).get("subject_kind") == "style_sample":
        swatches = ["outline", "shadow", "dark", "mid", "light", "primary", "secondary", "danger", "safe"]
        for swatch_index, semantic in enumerate(swatches):
            x = 1 + (swatch_index % 3) * 5
            y = 1 + (swatch_index // 3) * 5
            draw.rectangle((x, y, x + 3, y + 3), fill=colors[semantic])
    else:
        cell(0, 0, 0, 0)
    if image.size != (target_width, target_height):
        image = image.resize((target_width, target_height), resample=Image.Resampling.NEAREST)
    payload = io.BytesIO()
    image.save(payload, format="PNG", optimize=False, compress_level=9)
    return payload.getvalue()


def _sprite_frames_resource(png_path: str, recipe: dict[str, Any]) -> bytes:
    clips = recipe["parameters"].get("clips", BASELINE_CLIPS)
    frame_width, frame_height = map(
        int, recipe["parameters"].get("frame_dimensions", [16, 16])
    )
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
                x = (clip_offset + local_frame) * frame_width
                y = direction_index * frame_height
                subresources.extend(
                    [
                        f'[sub_resource type="AtlasTexture" id="{sub_id}"]',
                        'atlas = ExtResource("1_texture")',
                        f"region = Rect2({x}, {y}, {frame_width}, {frame_height})",
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


def _tileset_resource(png_path: str, recipe: dict[str, Any]) -> bytes:
    tile_width, tile_height = map(
        int, recipe["parameters"].get("frame_dimensions", [16, 16])
    )
    lines = [
        '[gd_resource type="TileSet" load_steps=3 format=3]',
        "",
        f'[ext_resource type="Texture2D" path="res://{png_path}" id="1_texture"]',
        "",
        '[sub_resource type="TileSetAtlasSource" id="TileSetAtlasSource_media"]',
        'texture = ExtResource("1_texture")',
        f"texture_region_size = Vector2i({tile_width}, {tile_height})",
    ]
    for mask in range(256):
        lines.append(f"{mask % 16}:{mask // 16}/0 = 0")
    lines.extend(["", "[resource]", f"tile_size = Vector2i({tile_width}, {tile_height})", 'sources/0 = SubResource("TileSetAtlasSource_media")', ""])
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
    lines.extend(["", "[resource]", "random_pitch = 1.02", "random_volume_offset_db = 1.0", "playback_mode = 0", f"streams_count = {len(wav_paths)}"])
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


def _recipe_inputs(
    project: Path,
    recipe: dict[str, Any],
    *,
    style: dict[str, Any] | None = None,
    part_by_id: dict[str, dict[str, Any]] | None = None,
    parts_prevalidated: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    current_style = style or _load_json(project / "work" / "assets" / "styles" / "STY-0001.json")
    if not _validate_fingerprint(recipe):
        raise WorkflowError(f"{recipe.get('id')}: recipe input fingerprint is invalid")
    if not _validate_fingerprint(current_style):
        raise WorkflowError(f"{current_style.get('id')}: style input fingerprint is invalid")
    parameters = recipe.get("parameters", {})
    expected_style = parameters.get("style_fingerprint")
    if expected_style != _style_generation_fingerprint(current_style):
        raise WorkflowError(f"{recipe['id']}: style pack changed; replan before generation")
    pinned_palette = parameters.get("palette")
    if not isinstance(pinned_palette, dict) or pinned_palette != current_style.get("palette"):
        raise WorkflowError(f"{recipe['id']}: style pack changed; replan before generation")
    compile_style = {**current_style, "palette": pinned_palette}
    selected_parts: list[dict[str, Any]] = []
    if recipe.get("source_part_ids"):
        part_by_id = part_by_id or {
            part["id"]: part for part in _all_records(project, "parts", "PRT")
        }
        expected_hashes = recipe.get("parameters", {}).get("source_part_hashes", {})
        body_family = str(parameters.get("body_family", ""))
        required_directions = set(map(str, parameters.get("required_directions", DIRECTIONS)))
        required_animations = {
            str(clip.get("name")) for clip in parameters.get("clips", [])
        }
        anchor_reference: dict[str, Any] | None = None
        for part_id in recipe["source_part_ids"]:
            part = part_by_id.get(part_id)
            if not part:
                raise WorkflowError(f"{recipe['id']}: missing source part {part_id}")
            if not parts_prevalidated:
                if not _validate_fingerprint(part):
                    raise WorkflowError(f"{part_id}: source part input fingerprint is invalid")
                _assert_schema(part, "part-spec.schema.json")
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
            if body_family not in part.get("compatible_body_families", []):
                raise WorkflowError(
                    f"{recipe['id']}: source part {part_id} is incompatible with body family {body_family}"
                )
            if not required_directions.issubset(set(map(str, part.get("compatible_directions", [])))):
                raise WorkflowError(
                    f"{recipe['id']}: source part {part_id} is incompatible with required directions"
                )
            compatible_animations = set(map(str, part.get("compatible_animations", [])))
            if "*" not in compatible_animations and not required_animations.issubset(compatible_animations):
                raise WorkflowError(
                    f"{recipe['id']}: source part {part_id} is incompatible with required animations"
                )
            raw_anchors = part.get("anchors", {})
            anchors = {
                LEGACY_ANCHOR_MAP.get(str(name), str(name)): point
                for name, point in raw_anchors.items()
            }
            if "center" not in anchors and "origin" in anchors:
                anchors["center"] = anchors["origin"]
            if set(anchors) != set(NEUTRAL_ANCHORS):
                raise WorkflowError(
                    f"{recipe['id']}: source part {part_id} has invalid anchors"
                )
            if anchor_reference is None:
                anchor_reference = dict(anchors)
            elif anchors != anchor_reference:
                raise WorkflowError(
                    f"{recipe['id']}: source part {part_id} breaks anchor alignment"
                )
            occupancy = {
                (int(point[0]), int(point[1])) for point in part.get("occupancy_mask", [])
            }
            painted_pixels = list(part.get("pixels", []))
            directional = part.get("direction_pixels")
            if isinstance(directional, dict):
                for direction in DIRECTIONS:
                    painted_pixels.extend(directional.get(direction, []))
            painted_coordinates = {
                (int(point[0]), int(point[1])) for point in painted_pixels
            }
            if not painted_coordinates.issubset(occupancy):
                raise WorkflowError(
                    f"{recipe['id']}: source part {part_id} occupancy mask omits painted pixels"
                )
            if not part.get("mirror_safe"):
                if not isinstance(directional, dict) or set(directional) != set(DIRECTIONS):
                    raise WorkflowError(f"{recipe['id']}: asymmetric part {part_id} requires all four directions")
            for pixel in painted_pixels:
                if len(pixel) != 3 or str(pixel[2]) not in compile_style.get("palette", {}):
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
    return compile_style, selected_parts


def _compile_recipe(
    project: Path,
    recipe: dict[str, Any],
    *,
    style: dict[str, Any] | None = None,
    part_by_id: dict[str, dict[str, Any]] | None = None,
    parts_prevalidated: bool = False,
) -> dict[str, Any]:
    style, selected_parts = _recipe_inputs(
        project,
        recipe,
        style=style,
        part_by_id=part_by_id,
        parts_prevalidated=parts_prevalidated,
    )
    outputs = [str(value) for value in recipe["outputs"]]
    for value in outputs:
        _safe_relative(value)
    kind = str(recipe["kind"])
    compiled: dict[str, bytes] = {}
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
            compiled[next(path for path in outputs if path.endswith(".tres"))] = _tileset_resource(png_path, recipe)
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


def _cached_recipe_result(
    project: Path,
    recipe: dict[str, Any],
) -> dict[str, Any] | None:
    receipt = _receipt_path(project, recipe)
    if not receipt.is_file():
        return None
    try:
        value = _load_json(receipt)
    except WorkflowError:
        return None
    hashes = value.get("outputs")
    if (
        value.get("recipe_id") != recipe.get("id")
        or value.get("recipe_fingerprint") != recipe.get("input_fingerprint")
        or value.get("brief_id") != recipe.get("brief_id")
        or value.get("brief_fingerprint") != recipe.get("brief_fingerprint")
        or not isinstance(hashes, dict)
        or set(map(str, hashes)) != set(map(str, recipe.get("outputs", [])))
    ):
        return None
    for path, digest in hashes.items():
        artifact = project / _safe_relative(str(path))
        if (
            not artifact.is_file()
            or not isinstance(digest, str)
            or hashlib.sha256(artifact.read_bytes()).hexdigest() != digest
        ):
            return None
    return {
        "recipe": recipe,
        "compiled": {},
        "hashes": {str(path): str(digest) for path, digest in hashes.items()},
        "cache_hit": True,
    }


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
                "brief_id": recipe.get("brief_id"),
                "brief_sha256": recipe.get("brief_fingerprint"),
                "request_id": recipe.get("parameters", {}).get("request_id"),
                "request_sha256": recipe.get("parameters", {}).get("request_fingerprint"),
                "seed": recipe["seed"],
                "source_part_ids": recipe["source_part_ids"],
                "source_part_licenses": recipe.get("parameters", {}).get("source_part_licenses", {}),
            },
            "sha256": digest,
            "asset_spec_id": recipe["asset_spec_id"],
            "brief_id": recipe.get("brief_id"),
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
    manifest_path = project / "assets" / "asset-manifest.json"
    manifest = _load_json(manifest_path)
    existing_assets = list(manifest.get("assets", []))
    used_ids = {str(asset.get("id")) for asset in existing_assets}
    reusable_ids = {
        str(asset.get("runtime_path")): str(asset.get("id"))
        for asset in existing_assets
        if asset.get("provenance", {}).get("provider") == "aigame-local-media"
    }
    output_ids: dict[str, str] = {}
    next_asset_number = 1
    for path in sorted(path for recipe in all_recipes for path in recipe["outputs"]):
        reusable = reusable_ids.get(str(path))
        if reusable:
            output_ids[str(path)] = reusable
            continue
        while f"AST-{next_asset_number:04d}" in used_ids:
            next_asset_number += 1
        asset_id = f"AST-{next_asset_number:04d}"
        output_ids[str(path)] = asset_id
        used_ids.add(asset_id)
        next_asset_number += 1
    style = _load_json(project / "work" / "assets" / "styles" / "STY-0001.json")
    part_by_id = {part["id"]: part for part in _all_records(project, "parts", "PRT")}
    for part_id, part in part_by_id.items():
        if not _validate_fingerprint(part):
            raise WorkflowError(f"{part_id}: source part input fingerprint is invalid")
        _assert_schema(part, "part-spec.schema.json")
    for recipe in recipes:
        _recipe_inputs(
            project,
            recipe,
            style=style,
            part_by_id=part_by_id,
            parts_prevalidated=True,
        )
    cached_results: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    for recipe in recipes:
        hit = _cached_recipe_result(project, recipe)
        if hit:
            cached_results.append(hit)
        else:
            misses.append(recipe)
    worker_count = max(1, min(int(jobs), 64))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        compiled_results = list(
            executor.map(
                lambda recipe: {
                    **_compile_recipe(
                        project,
                        recipe,
                        style=style,
                        part_by_id=part_by_id,
                        parts_prevalidated=True,
                    ),
                    "cache_hit": False,
                },
                misses,
            )
        )
    results_by_id = {
        result["recipe"]["id"]: result for result in [*cached_results, *compiled_results]
    }
    compiled_results = [results_by_id[recipe["id"]] for recipe in recipes]
    generated = cached = 0
    hashes: list[str] = []
    new_records: list[dict[str, Any]] = []
    for result in compiled_results:
        recipe = result["recipe"]
        receipt = _receipt_path(project, recipe)
        if result["cache_hit"]:
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
                    "brief_id": recipe["brief_id"],
                    "brief_fingerprint": recipe["brief_fingerprint"],
                    "outputs": result["hashes"],
                },
            )
            generated += 1
        hashes.extend(result["hashes"].values())
        for path, digest in sorted(result["hashes"].items()):
            new_records.append(_manifest_record(output_ids[path], recipe, path, digest))
    selected_output_paths = {
        str(path) for result in compiled_results for path in result["hashes"]
    }
    managed_ids = {output_ids[path] for path in selected_output_paths}
    preserved = [
        record
        for record in manifest.get("assets", [])
        if record.get("id") not in managed_ids
        and str(record.get("runtime_path")) not in selected_output_paths
    ]
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
        recipe.pop("input_fingerprint", None)
        recipe["input_fingerprint"] = fingerprint(recipe)
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
        ("briefs", "ABR"),
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
            schema_name = "asset-brief.schema.json" if prefix == "ABR" else SCHEMA_BY_PREFIX[prefix]
            errors.extend(f"{record_id}: {message}" for message in _schema_errors(record, schema_name))
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
    brief_ids = set(plan.get("brief_ids", []))
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
        direction, requests, blocked = _structured_media_contract(project, expected)
        if blocked:
            errors.extend(blocked["errors"])
        elif direction and plan.get("media_direction_fingerprint") != direction.get("input_fingerprint"):
            errors.append("APL-0001: media direction changed; replan assets")
    recipe_ids = {record_id for record_id in records if record_id.startswith("RCP-")}
    current_style = records.get(str(plan.get("style_pack_id", "STY-0001")))
    current_style_fingerprint = (
        _style_generation_fingerprint(current_style) if isinstance(current_style, dict) else None
    )
    current_parts = {
        record_id: record for record_id, record in records.items() if record_id.startswith("PRT-")
    }
    for asset_id in spec_ids:
        spec = records.get(asset_id)
        if not spec:
            errors.append(f"APL-0001: missing asset specification {asset_id}")
            continue
        brief_id = str(spec.get("brief_id", ""))
        brief = records.get(brief_id)
        if brief_id not in brief_ids or not brief:
            errors.append(f"{asset_id}: missing derived asset brief {brief_id}")
        elif spec.get("brief_fingerprint") != brief.get("input_fingerprint"):
            errors.append(f"{asset_id}: asset brief changed; replan assets")
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
        parameters = recipe.get("parameters", {})
        if parameters.get("style_fingerprint") != current_style_fingerprint:
            errors.append(f"{recipe_id}: style pack changed; replan assets")
        if not current_style or parameters.get("palette") != current_style.get("palette"):
            errors.append(f"{recipe_id}: pinned palette does not match the current style pack")
        if recipe.get("asset_spec_id") not in spec_ids:
            errors.append(f"{recipe_id}: dangling asset specification {recipe.get('asset_spec_id')}")
        brief_id = str(recipe.get("brief_id", ""))
        brief = records.get(brief_id)
        if brief_id not in brief_ids or not brief:
            errors.append(f"{recipe_id}: dangling asset brief {brief_id}")
        elif recipe.get("brief_fingerprint") != brief.get("input_fingerprint"):
            errors.append(f"{recipe_id}: asset brief changed; replan assets")
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
        if current_style:
            try:
                _recipe_inputs(
                    project,
                    recipe,
                    style=current_style,
                    part_by_id=current_parts,
                )
            except WorkflowError as error:
                errors.append(str(error))
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
            artifact_path = project / _safe_asset_relative(runtime_path)
        except WorkflowError as error:
            errors.append(f"{asset_id}: {error}")
            continue
        provenance = asset.get("provenance", {})
        if provenance.get("generated") and not all(
            provenance.get(field)
            for field in ("provider", "provider_version", "model_id", "model_license", "prompt_sha256", "recipe_id", "seed")
        ):
            errors.append(f"{asset_id}: generated provenance is incomplete")
        if provenance.get("provider") == "aigame-local-media":
            brief_id = str(provenance.get("brief_id", ""))
            brief = records.get(brief_id)
            if not brief or provenance.get("brief_sha256") != brief.get("input_fingerprint"):
                errors.append(f"{asset_id}: generated brief provenance is missing or stale")
            request_id = str(provenance.get("request_id", ""))
            request_path = project / "work" / "concept" / "media_requests" / f"{request_id}.json"
            if (
                not request_path.is_file()
                or provenance.get("request_sha256")
                != _load_json(request_path).get("input_fingerprint")
            ):
                errors.append(f"{asset_id}: generated request provenance is missing or stale")
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
                        recipe_id = str((record or {}).get("provenance", {}).get("recipe_id", ""))
                        recipe = records.get(recipe_id, {})
                        if recipe:
                            expected_width, expected_height, _ = _visual_dimensions(recipe)
                            if image.size != (expected_width, expected_height):
                                errors.append(
                                    f"{runtime_path}: dimensions {image.size} do not match the locked contract {(expected_width, expected_height)}"
                                )
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
            "briefs": len(brief_ids),
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
    tile_set: dict[str, Any] | Path | str | None = None,
    stage_constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if width < 7 or height < 7:
        raise WorkflowError("Stage dimensions must be at least 7x7")
    width = width if width % 2 else width - 1
    height = height if height % 2 else height - 1
    if tile_set is None:
        tile_contract: dict[str, Any] = {
            "id": "TIL-BUILTIN",
            "tiles": [
                {
                    "id": f"terrain-{mask:03d}",
                    "weight": 1.0,
                    "collision": mask != 255,
                    "navigation": mask == 255,
                }
                for mask in range(256)
            ],
            "adjacency": {
                "encoding": "8-neighbor-bitmask",
                "opposite_edges_must_match": True,
            },
            "required_neighbor_masks": list(range(256)),
            "stage_constraints": {
                "entrance_exit_connected": True,
                "safe_spawn_radius": 2,
                "retry_limit": 8,
            },
        }
    elif isinstance(tile_set, (str, Path)):
        tile_contract = _load_json(Path(tile_set))
    else:
        tile_contract = dict(tile_set)
    adjacency = tile_contract.get("adjacency", {})
    if (
        adjacency.get("encoding") != "8-neighbor-bitmask"
        or adjacency.get("opposite_edges_must_match") is not True
    ):
        raise WorkflowError("TileSetSpec requires 8-neighbor opposite-edge adjacency rules")
    tile_by_mask: dict[int, dict[str, Any]] = {}
    for tile in tile_contract.get("tiles", []):
        match = re.search(r"([0-9]{3})$", str(tile.get("id", "")))
        mask = tile.get("neighbor_mask")
        if mask is None and match:
            mask = int(match.group(1))
        if not isinstance(mask, int) or not 0 <= mask <= 255 or mask in tile_by_mask:
            raise WorkflowError("TileSetSpec contains invalid or duplicate neighbor masks")
        tile_by_mask[mask] = dict(tile)
    required_masks = set(tile_contract.get("required_neighbor_masks", []))
    if required_masks != set(range(256)) or set(tile_by_mask) != required_masks:
        raise WorkflowError("TileSetSpec must provide complete terrain neighbor masks 0 through 255")
    weights = {
        mask: max(0.0, float(tile.get("weight", 1.0)))
        for mask, tile in tile_by_mask.items()
    }
    eligible_masks = {mask for mask, weight in weights.items() if weight > 0.0}
    navigation_masks = {
        mask
        for mask, tile in tile_by_mask.items()
        if bool(tile.get("navigation")) and mask in eligible_masks
    }
    if not navigation_masks:
        raise WorkflowError("TileSetSpec has no positive-weight navigable terrain tile")
    constraints = {**tile_contract.get("stage_constraints", {}), **(stage_constraints or {})}
    retry_limit = max(1, int(constraints.get("retry_limit", 8)))
    safe_radius = max(1, int(constraints.get("safe_spawn_radius", 2)))
    entrance_value = constraints.get("entrance", [1, 1])
    exit_value = constraints.get("exit", [width - 2, height - 2])
    entrance = (int(entrance_value[0]), int(entrance_value[1]))
    exit_cell = (int(exit_value[0]), int(exit_value[1]))
    if not all(1 <= x < width - 1 and 1 <= y < height - 1 for x, y in (entrance, exit_cell)):
        raise WorkflowError("Stage entrance and exit must be inside the terrain boundary")
    room_names = list(dict.fromkeys(map(str, required_rooms)))
    configured_rooms = constraints.get("required_rooms", [])
    if isinstance(configured_rooms, list):
        room_names = list(dict.fromkeys([*room_names, *map(str, configured_rooms)]))
    configured_encounters = constraints.get("required_encounters", [])
    if isinstance(configured_encounters, dict):
        encounter_names = list(map(str, configured_encounters))
    elif isinstance(configured_encounters, (list, tuple, set)):
        encounter_names = list(map(str, configured_encounters))
    elif isinstance(configured_encounters, str) and configured_encounters.strip():
        encounter_names = [configured_encounters.strip()]
    else:
        encounter_names = []
    encounter_names = list(dict.fromkeys(encounter_names))
    placement_names = list(dict.fromkeys([*room_names, *encounter_names]))

    directions = (
        (0, -1),
        (1, -1),
        (1, 0),
        (1, 1),
        (0, 1),
        (-1, 1),
        (-1, 0),
        (-1, -1),
    )
    bit_sets = {
        (direction, value): {
            mask for mask in eligible_masks if ((mask >> direction) & 1) == value
        }
        for direction in range(8)
        for value in (0, 1)
    }
    def weighted_choice(domain: set[int], rng: random.Random) -> int:
        ordered = sorted(domain)
        total = sum(weights[mask] for mask in ordered)
        if total <= 0:
            raise WorkflowError("Stage contradiction: every remaining tile has zero weight")
        target = rng.random() * total
        for mask in ordered:
            target -= weights[mask]
            if target <= 0:
                return mask
        return ordered[-1]

    last_reason = "unknown contradiction"
    for attempt in range(retry_limit):
        derived_seed = hashlib.sha256(f"{int(seed)}:{attempt}".encode("ascii")).hexdigest()
        rng = random.Random(int(derived_seed[:16], 16))
        domains = {
            (x, y): set(eligible_masks)
            for y in range(height)
            for x in range(width)
        }
        for (x, y), domain in domains.items():
            for direction, (dx, dy) in enumerate(directions):
                if not (0 <= x + dx < width and 0 <= y + dy < height):
                    domain.intersection_update(bit_sets[(direction, 0)])

        route = [entrance]
        x, y = entrance
        while (x, y) != exit_cell:
            options: list[tuple[int, int]] = []
            if x != exit_cell[0]:
                options.append((x + (1 if exit_cell[0] > x else -1), y))
            if y != exit_cell[1]:
                options.append((x, y + (1 if exit_cell[1] > y else -1)))
            x, y = rng.choice(options)
            route.append((x, y))
        for cell in route:
            domains[cell].intersection_update(navigation_masks)

        queue = deque(
            cell for cell, domain in domains.items() if len(domain) < len(eligible_masks)
        )

        def propagate() -> bool:
            nonlocal last_reason
            while queue:
                cell = queue.popleft()
                domain = domains[cell]
                if not domain:
                    last_reason = f"empty domain at {cell}"
                    return False
                x, y = cell
                for direction, (dx, dy) in enumerate(directions):
                    neighbor = (x + dx, y + dy)
                    if neighbor not in domains:
                        continue
                    possible_bits = {(mask >> direction) & 1 for mask in domain}
                    allowed = set()
                    for value in possible_bits:
                        allowed.update(bit_sets[((direction + 4) % 8, value)])
                    reduced = domains[neighbor].intersection(allowed)
                    if not reduced:
                        last_reason = f"adjacency contradiction between {cell} and {neighbor}"
                        return False
                    if reduced != domains[neighbor]:
                        domains[neighbor] = reduced
                        queue.append(neighbor)
            return True

        if not propagate():
            continue
        contradicted = False
        while True:
            unresolved = [
                (len(domain), cell)
                for cell, domain in domains.items()
                if len(domain) > 1
            ]
            if not unresolved:
                break
            entropy = min(value[0] for value in unresolved)
            candidates = [cell for size, cell in unresolved if size == entropy]
            cell = rng.choice(sorted(candidates, key=lambda value: (value[1], value[0])))
            try:
                domains[cell] = {weighted_choice(domains[cell], rng)}
            except WorkflowError as error:
                last_reason = str(error)
                contradicted = True
                break
            queue.append(cell)
            if not propagate():
                contradicted = True
                break
        if contradicted:
            continue
        collapsed = {cell: next(iter(domain)) for cell, domain in domains.items()}
        walkable = {cell for cell, mask in collapsed.items() if mask in navigation_masks}
        distances = {entrance: 0}
        queue = deque([entrance])
        while queue:
            x, y = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbor = (x + dx, y + dy)
                if neighbor in walkable and neighbor not in distances:
                    distances[neighbor] = distances[(x, y)] + 1
                    queue.append(neighbor)
        if exit_cell not in distances:
            last_reason = "entrance and exit are not connected"
            continue
        safe_spawns = sorted(
            [
                list(cell)
                for cell, distance in distances.items()
                if distance >= safe_radius and cell != exit_cell
            ],
            key=lambda cell: (cell[1], cell[0]),
        )[:8]
        room_candidates = [
            cell
            for cell, _ in sorted(
                distances.items(),
                key=lambda item: (item[1], item[0][1], item[0][0]),
            )
            if cell not in {entrance, exit_cell}
        ]
        if not safe_spawns or len(placement_names) > len(room_candidates):
            last_reason = "required rooms or safe spawns exceed connected navigable cells"
            continue
        all_placements: dict[str, list[int]] = {}
        for index, name in enumerate(placement_names):
            position = room_candidates[
                ((index + 1) * len(room_candidates)) // (len(placement_names) + 1)
            ]
            all_placements[name] = list(position)
        placements = {name: all_placements[name] for name in room_names}
        encounter_placements = {name: all_placements[name] for name in encounter_names}
        tile_grid = [
            [str(tile_by_mask[collapsed[(x, y)]]["id"]) for x in range(width)]
            for y in range(height)
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "algorithm": "wave-function-collapse-v1",
            "tile_set_id": str(tile_contract.get("id", "TIL-UNKNOWN")),
            "adjacency": adjacency,
            "seed": int(seed),
            "derived_seed": derived_seed,
            "attempt": attempt,
            "width": width,
            "height": height,
            "tile_grid": tile_grid,
            "grid": [
                "".join("." if collapsed[(x, y)] in navigation_masks else "#" for x in range(width))
                for y in range(height)
            ],
            "entrance": list(entrance),
            "exit": list(exit_cell),
            "safe_spawns": safe_spawns,
            "required_rooms": placements,
            "required_encounters": encounter_placements,
            "path_length": distances[exit_cell],
            "valid": True,
        }
    raise WorkflowError(
        f"Stage contradiction after {retry_limit} deterministic WFC attempts: {last_reason}"
    )


def benchmark_assets(
    root: Path | str,
    *,
    sprite_count: int = 500,
    sound_count: int = 500,
) -> dict[str, Any]:
    project = _project(root)
    style = _style_record("0" * 64)
    benchmark_parts = _part_records(style)
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
    sprite_hashes = [
        hashlib.sha256(
            _indexed_png(visual_recipe, style, seed, benchmark_parts)
        ).digest()
        for seed in range(max(1, sprite_count))
    ]
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
    _indexed_png(runtime_recipe, style, 999, benchmark_parts)
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
    cached_count = max(1, sprite_count + sound_count)
    rewritten_files = 0
    with tempfile.TemporaryDirectory() as temporary:
        benchmark_root = Path(temporary)
        _write_json(
            benchmark_root / "work" / "assets" / "styles" / "STY-0001.json",
            style,
        )
        benchmark_part = benchmark_parts[0]
        _write_json(
            benchmark_root
            / "work"
            / "assets"
            / "parts"
            / f"{benchmark_part['id']}.json",
            benchmark_part,
        )
        _write_json(
            benchmark_root / "assets" / "asset-manifest.json",
            {"schema_version": SCHEMA_VERSION, "revision": 0, "assets": []},
        )
        benchmark_specs: list[str] = []
        tracked_paths: list[Path] = [benchmark_root / "assets" / "asset-manifest.json"]
        for index in range(cached_count):
            spec_id = f"ASP-{index + 1:04d}"
            benchmark_specs.append(spec_id)
            output = f"assets/generated/benchmark/cache-{index + 1:04d}.bin"
            recipe = _record(
                {
                    "kind": "sprite",
                    "asset_spec_id": spec_id,
                    "seed": index,
                    "source_part_ids": [benchmark_part["id"]],
                    "parameters": {
                        "grid_size": 16,
                        "palette_id": style["id"],
                        "palette": style["palette"],
                        "style_fingerprint": _style_generation_fingerprint(style),
                        "body_family": "humanoid",
                        "required_directions": DIRECTIONS,
                        "layers": [
                            {
                                "slot": benchmark_part["slot"],
                                "part_id": benchmark_part["id"],
                                "offset": [0, 0],
                            }
                        ],
                        "layer_choices": {
                            benchmark_part["slot"]: [benchmark_part["id"]]
                        },
                        "source_part_hashes": {
                            benchmark_part["id"]: benchmark_part["input_fingerprint"]
                        },
                        "source_part_licenses": {
                            benchmark_part["id"]: benchmark_part["license"]
                        },
                        "purpose": "cached path benchmark",
                        "output_license": "CC0-1.0",
                    },
                    "outputs": [output],
                    "compiler": "aigame.pixel-media-v1",
                    "compiler_version": __version__,
                },
                record_id=f"RCP-{index + 1:04d}",
                status="approved",
            )
            _write_json(
                benchmark_root / "work" / "assets" / "recipes" / f"{recipe['id']}.json",
                recipe,
            )
            payload = hashlib.sha256(f"cached:{index}".encode("ascii")).digest()
            output_path = benchmark_root / _safe_relative(output)
            _write_bytes_if_changed(output_path, payload)
            receipt = _receipt_path(benchmark_root, recipe)
            _write_json(
                receipt,
                {
                    "schema_version": SCHEMA_VERSION,
                    "recipe_id": recipe["id"],
                    "recipe_fingerprint": recipe["input_fingerprint"],
                    "outputs": {output: hashlib.sha256(payload).hexdigest()},
                },
            )
            if index == 0:
                tracked_paths.extend([output_path, receipt])
        warm = _generate_selected(benchmark_root, benchmark_specs, jobs=1)
        if warm["generated"] or warm["cached"] != cached_count:
            raise WorkflowError("Cached benchmark fixture did not enter the cache fast path")
        before = {path: path.stat().st_mtime_ns for path in tracked_paths}
        started = time.perf_counter()
        cached_result = _generate_selected(benchmark_root, benchmark_specs, jobs=1)
        cached_batch_seconds = time.perf_counter() - started
        after = {path: path.stat().st_mtime_ns for path in tracked_paths}
        rewritten_files = sum(before[path] != after[path] for path in tracked_paths)
        if cached_result["generated"] or cached_result["cached"] != cached_count:
            rewritten_files += 1
    measurements = {
        "sprite_batch": {"count": sprite_count, "seconds": sprite_seconds, "limit": 10.0, "passed": sprite_seconds <= 10.0},
        "sound_batch": {"count": sound_count, "seconds": sound_seconds, "limit": 15.0, "passed": sound_seconds <= 15.0},
        "runtime_compose": {"milliseconds": runtime_ms, "limit": 50.0, "passed": runtime_ms <= 50.0},
        "cached_lookup": {"milliseconds": cached_lookup_ms, "limit": 1.0, "passed": cached_lookup_ms <= 1.0},
        "tile_atlas": {"seconds": tile_seconds, "limit": 1.0, "passed": tile_seconds <= 1.0},
        "cached_batch": {
            "count": cached_count,
            "seconds": cached_batch_seconds,
            "limit": 1.0,
            "rewritten_files": rewritten_files,
            "passed": cached_batch_seconds <= 1.0 and rewritten_files == 0,
        },
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if all(value["passed"] for value in measurements.values()) else "failed",
        "measurements": measurements,
        "environment": {"cpu_only": True, "project": str(project)},
    }
