from __future__ import annotations

import hashlib
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import tomllib
import unittest
import wave
from pathlib import Path
from unittest import mock

from PIL import Image
from jsonschema import Draft202012Validator

from aigame.automation import AI_MODE_CONFIRMATION, set_automation_mode
from aigame.core import WorkflowError, fingerprint
from aigame.cli import main
from aigame.generator import create_game
from aigame.media import (
    MEDIA_STATES,
    MEDIA_TRANSITIONS,
    assert_media_transition,
    benchmark_assets,
    compose_recipe,
    generate_assets,
    generate_wfc_layout,
    integrate_assets,
    next_asset_task,
    plan_assets,
    sample_assets,
    validate_assets,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _record(value: dict) -> dict:
    material = {
        "schema_version": "1.0",
        "revision": 1,
        "status": "approved",
        "created_at": "2026-07-15T00:00:00Z",
        "updated_at": "2026-07-15T00:00:00Z",
        **value,
    }
    material["input_fingerprint"] = fingerprint(material)
    return material


class MediaFactoryDistributionTests(unittest.TestCase):
    def test_generated_project_contains_media_factory_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Media Game", godot_version="4.7", apply=True)
            schemas = {
                "media-config.schema.json",
                "asset-plan.schema.json",
                "asset-spec.schema.json",
                "style-pack.schema.json",
                "part-spec.schema.json",
                "animation-set.schema.json",
                "tile-set-spec.schema.json",
                "particle-spec.schema.json",
                "sound-spec.schema.json",
                "media-recipe.schema.json",
            }
            self.assertTrue(all((root / ".aigame" / "schemas" / name).is_file() for name in schemas))
            for relative in (
                ".aigame/media.toml",
                ".aigame/state/assets.json",
                "assets/source/packs/core-topdown-v1/pack.json",
                "addons/aigame_media/runtime_composer.gd",
                ".aigame/agent-skills/generate-game-assets/SKILL.md",
            ):
                self.assertTrue((root / relative).is_file(), relative)


class MediaFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "game"
        create_game(self.root, "Media Game", godot_version="4.7", apply=True)
        self._install_finalized_blueprint(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _cli(self, arguments: list[str]) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(arguments)
        return code, json.loads(output.getvalue())

    def _install_finalized_blueprint(self, root: Path) -> None:
        concept_state = json.loads((root / ".aigame/state/concept.json").read_text(encoding="utf-8"))
        concept_state["status"] = "finalized"
        _write_json(root / ".aigame/state/concept.json", concept_state)
        _write_json(
            root / "work/concept/mechanics/MEC-0001.json",
            _record(
                {
                    "id": "MEC-0001",
                    "name": "Dash Strike",
                    "states": ["ready", "dash", "attack", "hit", "death"],
                    "feedback": {
                        "visual": "ember trail and impact flash",
                        "animation": "dash and attack clips",
                        "audio": "dash ignition and impact",
                        "ui": "cooldown icon",
                        "accessibility": "high contrast cooldown state",
                        "controller": "short bounded rumble",
                    },
                }
            ),
        )
        content = [
            {
                "id": "CNT-0001",
                "canonical_name": "Cinder Runner",
                "kind": "playable_character",
                "launch_status": "must",
                "required_assets": [
                    "character sprite",
                    "complete animation set",
                    "portrait icon",
                    "footstep sound set",
                ],
            },
            {
                "id": "CNT-0002",
                "canonical_name": "Brass Sentinel",
                "kind": "enemy",
                "launch_status": "must",
                "required_assets": ["enemy sprite", "complete animation set", "attack particles"],
            },
        ]
        for value in content:
            _write_json(root / "work/concept/content" / f"{value['id']}.json", _record(value))
        _write_json(
            root / "work/concept/BLU-0001.json",
            _record(
                {
                    "id": "BLU-0001",
                    "art_direction": "Readable ember silhouettes on dark brass.",
                    "audio_direction": "Mechanical rhythm with warm ember motifs.",
                    "moment_to_moment_loop": "Read threats, dash, attack, and collect charge.",
                    "session_loop": "Clear a route, choose rewards, defeat a keeper, and return.",
                    "long_term_loop": "Restore clock parts and unlock the final route.",
                    "player_verbs": ["move", "dash", "attack", "choose"],
                    "opening": "Wake beside the stopped World Clock.",
                    "first_minute": "Move through a safe ember gate and collect a reward.",
                    "tutorial": "Teach movement, dash, attack, and reward choice.",
                    "first_meaningful_decision": "Choose a safe reward or a dangerous shortcut.",
                    "progression_arc": ["Broken Outskirts"],
                    "major_encounters": ["Brass Sentinel"],
                    "setbacks": ["A failed run returns the player to the hub."],
                    "final_challenge": "Defeat the Last Horologist.",
                    "final_prerequisites": ["Restore the World Clock key."],
                    "win_conditions": ["The Last Horologist is defeated."],
                    "loss_conditions": ["Health reaches zero."],
                    "alternate_endings": ["Restart the Clock", "Break the Clock"],
                    "recovery": "Resume from the hub with permanent unlocks.",
                    "ending": "Restart or break the World Clock.",
                    "credits": "Replayable credits.",
                    "postgame": "New Cycle modifiers.",
                    "replay": "Seeds and reward combinations support repeat runs.",
                    "endgame": "Cycle modifiers and a superboss extend mastery.",
                    "narrative": {
                        "applicability": "required",
                        "premise": "A courier decides the fate of a clockwork world.",
                        "character_arcs": ["The courier becomes an independent decision-maker."],
                        "acts": ["Awakening", "Restoration", "Decision"],
                        "resolution": "The final clock choice resolves the courier's promise.",
                    },
                    "systems_overview": ["combat", "route selection", "ending selection"],
                    "progression_system": {"permanent": "Restore clock parts to unlock routes."},
                    "economy": {"currencies": ["Clock Parts"], "sinks": ["route unlocks"]},
                    "ux_information": ["health", "dash cooldown", "route risk"],
                    "save_and_recovery": "Persist at room completion and restore the room seed.",
                    "platform_and_input": {"platform": "Windows", "inputs": ["keyboard", "controller"]},
                    "accessibility": ["reduced flashes", "remapping", "non-audio danger cues"],
                    "procedural_generators": {
                        "enemy_pool": "Named enemy-part pool with seeded weights.",
                        "stage_pool": "Named room templates with adjacency rules.",
                    },
                }
            ),
        )
        _write_json(
            root / "work/concept/GDD-0001.json",
            _record(
                {
                    "id": "GDD-0001",
                    "criteria": [
                        {
                            "id": "DONE-0001",
                            "title": "Complete audiovisual feedback",
                            "scope_refs": ["MEC-0001", "CNT-0001"],
                            "verification": "human_playtest",
                            "evidence_types": ["runtime", "playtest"],
                            "release_blocking": True,
                            "acceptance_criteria": ["Every required visual and audio cue is present."],
                            "playtest_hypotheses": ["Players recognize danger without audio."],
                        }
                    ],
                }
            ),
        )

    def test_plan_is_exhaustive_schema_valid_and_deterministic(self) -> None:
        preview = plan_assets(self.root, apply=False)
        self.assertEqual("dry_run", preview["status"])
        self.assertGreaterEqual(preview["counts"]["asset_specs"], 10)
        self.assertEqual("passed", plan_assets(self.root, apply=True)["status"])
        self.assertEqual([], validate_assets(self.root)["errors"])
        plan_path = self.root / "work/assets/APL-0001.json"
        first_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        plan_assets(self.root, apply=True)
        self.assertEqual(first_hash, hashlib.sha256(plan_path.read_bytes()).hexdigest())
        sources = {row["source_ref"] for row in json.loads(plan_path.read_text())["coverage"]}
        self.assertIn("CNT-0001.required_assets[0]", sources)
        self.assertIn("MEC-0001.state.dash", sources)
        self.assertIn("BLU-0001.progression_arc.Broken Outskirts", sources)
        self.assertEqual(
            6,
            len(list((self.root / "assets/source/packs/core-topdown-v1/parts").glob("PRT-*.json"))),
        )

    def test_replanning_prunes_records_and_manifest_entries_removed_from_scope(self) -> None:
        set_automation_mode(self.root, "ai_staging", confirmation=AI_MODE_CONFIRMATION, apply=True)
        first = plan_assets(self.root, apply=True)
        sample_assets(self.root, apply=True)
        generate_assets(self.root, all_assets=True, jobs=2, apply=True)
        old_recipe_count = first["counts"]["recipes"]
        old_outputs = {
            output
            for path in (self.root / "work/assets/recipes").glob("RCP-*.json")
            for output in json.loads(path.read_text(encoding="utf-8"))["outputs"]
        }
        (self.root / "work/concept/content/CNT-0002.json").unlink()
        second = plan_assets(self.root, apply=True)
        self.assertLess(second["counts"]["recipes"], old_recipe_count)
        self.assertEqual(
            second["counts"]["recipes"],
            len(list((self.root / "work/assets/recipes").glob("RCP-*.json"))),
        )
        new_outputs = {
            output
            for path in (self.root / "work/assets/recipes").glob("RCP-*.json")
            for output in json.loads(path.read_text(encoding="utf-8"))["outputs"]
        }
        self.assertTrue(old_outputs - new_outputs)
        self.assertTrue(all(not (self.root / output).exists() for output in old_outputs - new_outputs))
        manifest = json.loads((self.root / "assets/asset-manifest.json").read_text(encoding="utf-8"))
        current_recipes = {
            path.stem for path in (self.root / "work/assets/recipes").glob("RCP-*.json")
        }
        self.assertTrue(
            all(
                asset.get("provenance", {}).get("recipe_id") in current_recipes
                for asset in manifest["assets"]
                if asset.get("provenance", {}).get("provider") == "aigame-local-media"
            )
        )
        self.assertEqual([], validate_assets(self.root)["errors"])

    def test_every_media_schema_rejects_missing_fields_and_incompatible_versions(self) -> None:
        plan_assets(self.root, apply=True)
        pairs = {
            "asset-plan.schema.json": "work/assets/APL-0001.json",
            "asset-spec.schema.json": "work/assets/specs/ASP-0001.json",
            "style-pack.schema.json": "work/assets/styles/STY-0001.json",
            "part-spec.schema.json": "work/assets/parts/PRT-0001.json",
            "animation-set.schema.json": "work/assets/animations/ANI-0001.json",
            "tile-set-spec.schema.json": "work/assets/tiles/TIL-0001.json",
            "particle-spec.schema.json": "work/assets/particles/PFX-0001.json",
            "sound-spec.schema.json": "work/assets/sounds/SND-0001.json",
            "media-recipe.schema.json": "work/assets/recipes/RCP-0001.json",
        }
        schema_root = Path(__file__).parents[1] / "src/aigame/schemas"
        for schema_name, record_name in pairs.items():
            schema = json.loads((schema_root / schema_name).read_text(encoding="utf-8"))
            record = json.loads((self.root / record_name).read_text(encoding="utf-8"))
            validator = Draft202012Validator(schema)
            with self.subTest(schema=schema_name, case="valid"):
                self.assertEqual([], list(validator.iter_errors(record)))
            missing = dict(record)
            missing.pop(schema["required"][-1])
            with self.subTest(schema=schema_name, case="missing"):
                self.assertTrue(list(validator.iter_errors(missing)))
            incompatible = {**record, "schema_version": "999.0"}
            with self.subTest(schema=schema_name, case="version"):
                self.assertTrue(list(validator.iter_errors(incompatible)))
        media_schema = json.loads((schema_root / "media-config.schema.json").read_text(encoding="utf-8"))
        media_config = tomllib.loads((self.root / ".aigame/media.toml").read_text(encoding="utf-8"))
        media_validator = Draft202012Validator(media_schema)
        self.assertEqual([], list(media_validator.iter_errors(media_config)))
        self.assertTrue(list(media_validator.iter_errors({**media_config, "schema_version": "999.0"})))

    def test_assets_cli_exposes_dry_run_safe_protocol(self) -> None:
        code, next_task = self._cli(["assets", "next", "--project", str(self.root), "--json"])
        self.assertEqual(0, code)
        self.assertEqual("plan", next_task["operation"])
        code, preview = self._cli(["assets", "plan", "--project", str(self.root), "--json"])
        self.assertEqual(0, code)
        self.assertEqual("dry_run", preview["status"])
        self.assertFalse((self.root / "work/assets/APL-0001.json").exists())
        code, planned = self._cli(
            ["assets", "plan", "--project", str(self.root), "--apply", "--json"]
        )
        self.assertEqual(0, code)
        self.assertEqual("passed", planned["status"])
        code, composed = self._cli(
            [
                "assets",
                "compose",
                "--recipe",
                "RCP-0001",
                "--seed",
                "42",
                "--project",
                str(self.root),
                "--json",
            ]
        )
        self.assertEqual(0, code)
        self.assertEqual("passed", composed["status"])

    def test_plan_blocks_must_content_without_asset_mapping(self) -> None:
        path = self.root / "work/concept/content/CNT-0001.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["required_assets"] = []
        record.pop("input_fingerprint")
        record["input_fingerprint"] = fingerprint(record)
        _write_json(path, record)
        with self.assertRaisesRegex(WorkflowError, "required_assets"):
            plan_assets(self.root, apply=False)

    def test_every_media_state_transition_is_explicit(self) -> None:
        for before in MEDIA_STATES:
            for after in MEDIA_STATES:
                if before == after or after in MEDIA_TRANSITIONS[before]:
                    assert_media_transition(before, after)
                else:
                    with self.assertRaisesRegex(WorkflowError, "Forbidden"):
                        assert_media_transition(before, after)

    def test_part_change_selectively_invalidates_dependent_recipes(self) -> None:
        plan_assets(self.root, apply=True)
        part_path = self.root / "work/assets/parts/PRT-0002.json"
        part = json.loads(part_path.read_text(encoding="utf-8"))
        part["pixels"].append([8, 8, "secondary"])
        part.pop("input_fingerprint")
        part["input_fingerprint"] = fingerprint(part)
        _write_json(part_path, part)
        report = validate_assets(self.root)
        self.assertEqual("failed", report["status"])
        self.assertTrue(any("source part PRT-0002 changed" in error for error in report["errors"]))
        dependent_recipe = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.root / "work/assets/recipes").glob("RCP-*.json")
            if "PRT-0002" in json.loads(path.read_text(encoding="utf-8"))["source_part_ids"]
        )
        with self.assertRaisesRegex(WorkflowError, "source part PRT-0002 changed"):
            compose_recipe(self.root, dependent_recipe["id"], seed=1)

    def test_style_change_invalidates_recipe_before_output_can_change(self) -> None:
        plan_assets(self.root, apply=True)
        recipe_path = self.root / "work/assets/recipes/RCP-0001.json"
        original_recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
        style_path = self.root / "work/assets/styles/STY-0001.json"
        style = json.loads(style_path.read_text(encoding="utf-8"))
        style["palette"]["primary"] = "#00ff00ff"
        style.pop("input_fingerprint")
        style["input_fingerprint"] = fingerprint(style)
        _write_json(style_path, style)
        report = validate_assets(self.root)
        self.assertTrue(any("style pack changed" in error for error in report["errors"]))
        with self.assertRaisesRegex(WorkflowError, "style pack changed"):
            compose_recipe(self.root, original_recipe["id"], seed=1)
        self.assertEqual(
            original_recipe["input_fingerprint"],
            json.loads(recipe_path.read_text(encoding="utf-8"))["input_fingerprint"],
        )

    def test_approved_external_source_part_enters_seeded_slot_choices_with_its_license(self) -> None:
        plan_assets(self.root, apply=True)
        source = json.loads((self.root / "work/assets/parts/PRT-0002.json").read_text())
        source.update(
            {
                "id": "PRT-1000",
                "name": "External Body Variant",
                "license": "CC-BY-4.0",
                "sha256": "a" * 64,
                "provenance": {"source_pack": "approved-external-v1", "author": "Fixture Artist"},
            }
        )
        source.pop("input_fingerprint")
        source["input_fingerprint"] = fingerprint(source)
        _write_json(
            self.root / "assets/source/packs/approved-external-v1/parts/PRT-1000.json",
            source,
        )
        plan_assets(self.root, apply=True)
        recipe = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.root / "work/assets/recipes").glob("RCP-*.json")
            if "PRT-1000" in json.loads(path.read_text(encoding="utf-8"))["source_part_ids"]
        )
        self.assertIn("PRT-1000", recipe["source_part_ids"])
        self.assertIn("PRT-1000", recipe["parameters"]["layer_choices"]["body"])
        self.assertEqual("CC-BY-4.0", recipe["parameters"]["output_license"])
        self.assertEqual([], validate_assets(self.root)["errors"])

    def test_unsafe_recipe_output_is_rejected_before_writing(self) -> None:
        plan_assets(self.root, apply=True)
        recipe_path = self.root / "work/assets/recipes/RCP-0001.json"
        recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
        recipe["outputs"] = ["../escape.png"]
        recipe.pop("input_fingerprint")
        recipe["input_fingerprint"] = fingerprint(recipe)
        _write_json(recipe_path, recipe)
        with self.assertRaisesRegex(WorkflowError, "Unsafe media output path"):
            compose_recipe(self.root, "RCP-0001", seed=1)

    def test_human_style_gate_and_ai_staging_auto_approval(self) -> None:
        plan_assets(self.root, apply=True)
        result = sample_assets(self.root, apply=True)
        self.assertEqual("needs_human", result["status"])
        self.assertEqual("style_approval", next_asset_task(self.root)["operation"])
        request = result["approval_request"]
        approval = _record(
            {
                "id": "APR-9900",
                "status": "approved",
                "scope_hash": request["scope_hash"],
                "commit_sha": request["commit_sha"],
                "approver": "Game Owner",
                "decision": request["decision"],
                "approved_at": "2026-07-15T00:00:00Z",
            }
        )
        approval_path = Path(self.temporary.name) / "style-approval.json"
        _write_json(approval_path, approval)
        accepted = sample_assets(self.root, approval_path=approval_path, apply=True)
        self.assertEqual("passed", accepted["status"])
        self.assertEqual("generate", next_asset_task(self.root)["operation"])

        second = Path(self.temporary.name) / "ai-game"
        create_game(second, "AI Media Game", godot_version="4.7", apply=True)
        self._install_finalized_blueprint(second)
        set_automation_mode(second, "ai_staging", confirmation=AI_MODE_CONFIRMATION, apply=True)
        plan_assets(second, apply=True)
        approved = sample_assets(second, apply=True)
        self.assertEqual("passed", approved["status"])
        self.assertEqual("generate", next_asset_task(second)["operation"])
        approval_path = second / "evidence/approvals" / f"{approved['approval_id']}.json"
        self.assertTrue(approval_path.is_file())

    def test_human_style_approval_is_bound_to_current_style_and_samples(self) -> None:
        plan_assets(self.root, apply=True)
        requested = sample_assets(self.root, apply=True)
        request = requested["approval_request"]
        approval = _record(
            {
                "id": "APR-9901",
                "status": "approved",
                "scope_hash": request["scope_hash"],
                "commit_sha": request["commit_sha"],
                "approver": "Game Owner",
                "decision": request["decision"],
                "approved_at": "2026-07-15T00:00:00Z",
            }
        )
        approval_path = Path(self.temporary.name) / "stale-style-approval.json"
        _write_json(approval_path, approval)
        style_path = self.root / "work/assets/styles/STY-0001.json"
        style = json.loads(style_path.read_text(encoding="utf-8"))
        style["palette"]["primary"] = "#00ff00ff"
        style.pop("input_fingerprint")
        style["input_fingerprint"] = fingerprint(style)
        _write_json(style_path, style)
        with self.assertRaisesRegex(WorkflowError, "style pack changed|scope_hash"):
            sample_assets(self.root, approval_path=approval_path, apply=True)

    def test_generation_is_valid_resumable_and_does_not_rewrite_cache_hits(self) -> None:
        set_automation_mode(self.root, "ai_staging", confirmation=AI_MODE_CONFIRMATION, apply=True)
        plan_assets(self.root, apply=True)
        sample_assets(self.root, apply=True)
        generated = generate_assets(self.root, all_assets=True, jobs=2, apply=True)
        self.assertEqual("passed", generated["status"])
        self.assertGreater(generated["generated"], 0)
        png = next((self.root / "assets/generated").rglob("*.png"))
        with Image.open(png) as image:
            self.assertEqual("P", image.mode)
            self.assertEqual(0, image.width % 16)
            self.assertEqual(0, image.height % 16)
        wav_path = next((self.root / "assets/generated").rglob("*.wav"))
        with wave.open(str(wav_path), "rb") as stream:
            self.assertEqual(48000, stream.getframerate())
            self.assertEqual(2, stream.getsampwidth())
        before = {path: path.stat().st_mtime_ns for path in (png, wav_path)}
        with mock.patch("aigame.media._compile_recipe", side_effect=AssertionError("cache miss")) as compiler:
            cached = generate_assets(self.root, all_assets=True, jobs=4, apply=True)
        compiler.assert_not_called()
        after = {path: path.stat().st_mtime_ns for path in (png, wav_path)}
        self.assertEqual(before, after)
        self.assertEqual(0, cached["generated"])
        self.assertEqual(cached["total"], cached["cached"])
        self.assertEqual([], validate_assets(self.root)["errors"])
        self.assertEqual("dry_run", integrate_assets(self.root, apply=False)["status"])
        self.assertEqual("passed", integrate_assets(self.root, apply=True)["status"])
        self.assertEqual("complete", next_asset_task(self.root)["operation"])
        self.assertTrue((self.root / "assets/generated/media-registry.json").is_file())

    def test_seeded_layouts_are_connected_and_repeatable(self) -> None:
        plan_assets(self.root, apply=True)
        tile_set = json.loads(next((self.root / "work/assets/tiles").glob("TIL-*.json")).read_text())
        for seed in range(1000):
            rooms = ["entrance_guard", "reward", "boss"]
            first = generate_wfc_layout(
                width=17,
                height=13,
                seed=seed,
                required_rooms=rooms,
                tile_set=tile_set,
            )
            self.assertEqual(
                first,
                generate_wfc_layout(
                    width=17,
                    height=13,
                    seed=seed,
                    required_rooms=rooms,
                    tile_set=tile_set,
                ),
            )
            self.assertEqual("wave-function-collapse-v1", first["algorithm"])
            self.assertEqual(tile_set["id"], first["tile_set_id"])
            self.assertTrue(first["valid"])
            self.assertGreater(first["path_length"], 0)
            self.assertNotEqual(first["entrance"], first["exit"])
            self.assertEqual(set(rooms), set(first["required_rooms"]))
            self.assertEqual(len(rooms), len({tuple(value) for value in first["required_rooms"].values()}))
            valid_tile_ids = {tile["id"] for tile in tile_set["tiles"]}
            self.assertTrue(all(tile_id in valid_tile_ids for row in first["tile_grid"] for tile_id in row))

    def test_asset_plan_covers_complete_journey_generators_and_media_dod(self) -> None:
        plan_assets(self.root, apply=True)
        plan = json.loads((self.root / "work/assets/APL-0001.json").read_text(encoding="utf-8"))
        sources = {row["source_ref"] for row in plan["coverage"]}
        required = {
            "BLU-0001.first_minute",
            "BLU-0001.first_meaningful_decision",
            "BLU-0001.final_challenge",
            "BLU-0001.win_conditions[0]",
            "BLU-0001.loss_conditions[0]",
            "BLU-0001.alternate_endings[0]",
            "BLU-0001.recovery",
            "BLU-0001.replay",
            "BLU-0001.endgame",
            "BLU-0001.narrative.resolution",
            "BLU-0001.platform_and_input.inputs[0]",
            "BLU-0001.accessibility[0]",
            "BLU-0001.procedural_generators.enemy_pool",
            "GDD-0001.criteria.DONE-0001",
        }
        self.assertEqual(set(), required - sources)

    def test_wfc_rejects_incomplete_tile_contract(self) -> None:
        plan_assets(self.root, apply=True)
        tile_set = json.loads(next((self.root / "work/assets/tiles").glob("TIL-*.json")).read_text())
        layout = generate_wfc_layout(width=17, height=13, seed=7, tile_set=tile_set)
        masks = {tile["id"]: int(tile["id"].rsplit("-", 1)[1]) for tile in tile_set["tiles"]}
        directions = ((0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1))
        for y, row in enumerate(layout["tile_grid"]):
            for x, tile_id in enumerate(row):
                for direction, (dx, dy) in enumerate(directions):
                    if 0 <= x + dx < layout["width"] and 0 <= y + dy < layout["height"]:
                        neighbor_id = layout["tile_grid"][y + dy][x + dx]
                        self.assertEqual(
                            (masks[tile_id] >> direction) & 1,
                            (masks[neighbor_id] >> ((direction + 4) % 8)) & 1,
                        )
        tile_set["tiles"] = tile_set["tiles"][:-1]
        with self.assertRaisesRegex(WorkflowError, "neighbor masks"):
            generate_wfc_layout(width=17, height=13, seed=7, tile_set=tile_set)

    def test_recipe_hashes_are_stable_across_seeded_replays(self) -> None:
        plan_assets(self.root, apply=True)
        hashes = []
        for seed in range(64):
            first = compose_recipe(self.root, "RCP-0001", seed=seed)
            second = compose_recipe(self.root, "RCP-0001", seed=seed)
            self.assertEqual(first["outputs"], second["outputs"])
            hashes.append(tuple(first["outputs"].values()))
        self.assertGreater(len(set(hashes)), 32)

    @unittest.skipUnless(
        any(shutil.which(name) for name in ("godot", "godot4", "godot.cmd")),
        "Godot is not installed",
    )
    def test_godot_headless_loads_every_generated_runtime_asset(self) -> None:
        set_automation_mode(self.root, "ai_staging", confirmation=AI_MODE_CONFIRMATION, apply=True)
        plan_assets(self.root, apply=True)
        sample_assets(self.root, apply=True)
        generate_assets(self.root, all_assets=True, jobs=2, apply=True)
        integrate_assets(self.root, apply=True)
        runner = self.root / "tests/media_runner.gd"
        runner.write_text(
            '''extends SceneTree

func _initialize() -> void:
    var failures := 0
    var value = JSON.parse_string(FileAccess.get_file_as_string("res://assets/generated/media-registry.json"))
    if not value is Dictionary:
        push_error("media registry failed to parse")
        quit(1)
        return
    for asset in value.get("assets", []):
        var runtime_path: String = asset.get("runtime_path", "")
        var resource = load("res://" + runtime_path)
        if resource == null:
            failures += 1
            push_error("failed to load generated media: " + runtime_path)
    var Composer = load("res://addons/aigame_media/runtime_composer.gd")
    if Composer == null:
        failures += 1
        push_error("runtime composer failed to load")
    else:
        var composer = Composer.new()
        var part := Image.create(16, 16, false, Image.FORMAT_RGBA8)
        part.fill(Color("e66b3d"))
        var recipe := {"layers": [{"slot": "body", "part_id": "body", "offset": [0, 0]}]}
        var texture = composer.compose(recipe, {"body": part}, 42)
        var cached = composer.compose(recipe, {"body": part}, 42)
        if texture == null or cached != texture or composer.cache_size() != 1:
            failures += 1
            push_error("runtime composition or cache failed")
        part.fill(Color("58c9a3"))
        var changed = composer.compose(recipe, {"body": part}, 42)
        if changed == null or changed == texture or composer.cache_size() != 2:
            failures += 1
            push_error("runtime cache key ignored changed source-part content")
        var runtime_recipes: Dictionary = value.get("runtime_recipes", {})
        if runtime_recipes.is_empty():
            failures += 1
            push_error("runtime recipe registry is empty")
        else:
            var runtime_recipe: Dictionary = runtime_recipes.values()[0]
            var generated = composer.compose(runtime_recipe, value.get("parts", {}), int(runtime_recipe.get("seed", 0)))
            if generated == null:
                failures += 1
                push_error("registered procedural composition failed")
    print("AIGAME_MEDIA_TESTS failures=%d" % failures)
    quit(failures)
''',
            encoding="utf-8",
        )
        godot = next(shutil.which(name) for name in ("godot", "godot4", "godot.cmd") if shutil.which(name))
        imported = subprocess.run(
            [godot, "--headless", "--editor", "--path", str(self.root), "--quit"],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(0, imported.returncode, imported.stdout + imported.stderr)
        result = subprocess.run(
            [godot, "--headless", "--path", str(self.root), "--script", "res://tests/media_runner.gd"],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("AIGAME_MEDIA_TESTS failures=0", result.stdout + result.stderr)

    def test_benchmark_reports_every_performance_gate(self) -> None:
        result = benchmark_assets(self.root, sprite_count=20, sound_count=20)
        self.assertEqual("passed", result["status"])
        self.assertEqual(
            {"sprite_batch", "sound_batch", "runtime_compose", "cached_lookup", "tile_atlas", "cached_batch"},
            set(result["measurements"]),
        )


if __name__ == "__main__":
    unittest.main()
