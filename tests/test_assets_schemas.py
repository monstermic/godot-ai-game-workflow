from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from aigame.assets import build_lfs_plan, generate_asset
from aigame.generator import create_game
from aigame.validation import validate_project


class AssetAdapterTests(unittest.TestCase):
    def _v2_brief(self) -> dict:
        return {
            "schema_version": "2.0",
            "id": "ABR-0001",
            "input_fingerprint": "b" * 64,
            "family": "music",
            "subtype": "adaptive_music",
            "purpose": "Adaptive route music",
            "request_id": "ARQ-0001",
            "request_fingerprint": "c" * 64,
            "project_context": {
                "media_direction_id": "MDR-0001",
                "media_direction_fingerprint": "a" * 64,
            },
            "source_refs": ["BLU-0001.audio_direction.music"],
            "dependencies": [],
            "references": {"canonical": [], "structural": [], "stylistic": [], "negative": []},
            "control": {
                "locked": ["tempo", "meter", "key"],
                "variable": [],
                "generator_decides": [],
                "negative_constraints": [],
                "seed": 42,
            },
            "specification": {
                "tempo_bpm": 120,
                "meter": "4/4",
                "key": "A minor",
                "instrument_tags": ["mechanical", "ember"],
                "stems": ["explore", "combat", "boss"],
                "loop_bars": 4,
                "transition_quantization": "bar",
                "peak_dbfs": -1.0,
            },
            "output_contract": {
                "runtime_paths": [
                    "assets/generated/audio/music/route-explore.wav",
                    "assets/generated/audio/music/route-combat.wav",
                    "assets/generated/audio/music/route-boss.wav",
                    "assets/generated/audio/music/route.tres",
                ],
                "runtime_formats": ["wav", "tres"],
                "metadata_format": "json",
                "validation_profile": "pixel-media-v1",
            },
            "accessibility": {"alternatives": []},
            "acceptance_criteria": ["All stems are bar-aligned and loop cleanly."],
            "license_allowlist": ["CC0-1.0"],
        }
    def test_v2_multi_output_brief_uses_brief_identity(self) -> None:
        brief = self._v2_brief()
        result = generate_asset(brief, adapter=None)
        self.assertEqual("ABR-0001", result["brief_id"])
        self.assertEqual(4, len(result["outputs"]))
        self.assertTrue(all(output["status"] == "placeholder" for output in result["outputs"]))

    def test_v2_adapter_result_must_match_the_exact_brief_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "adapter.py"
            script.write_text(
                "import json,sys\nbrief=json.load(sys.stdin)\n"
                "provenance={'provider':'fixture','provider_version':'1','model_id':'fixture','model_checksum':'a'*64,'model_license':'CC0-1.0','workflow_checksum':'b'*64,'prompt_sha256':'c'*64,'seed':42,'brief_sha256':'0'*64}\n"
                "outputs=[{'status':'generated','runtime_path':path,'license':'CC0-1.0','sha256':'d'*64} for path in brief['output_contract']['runtime_paths']]\n"
                "json.dump({'schema_version':'2.0','status':'generated','brief_id':brief['id'],'outputs':outputs,'provenance':provenance},sys.stdout)\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "brief fingerprint"):
                generate_asset(
                    self._v2_brief(),
                    adapter=[sys.executable, str(script)],
                )

    def test_asset_brief_is_schema_validated_before_an_adapter_can_run(self) -> None:
        with self.assertRaisesRegex(ValueError, "AssetBrief"):
            generate_asset({"schema_version": "1.0", "id": "AST-0001"}, adapter=None)

    def test_missing_adapter_returns_a_placeholder_result(self) -> None:
        brief = {
            "schema_version": "1.0",
            "id": "AST-0001",
            "kind": "image",
            "purpose": "Player placeholder",
            "runtime_path": "assets/player.svg",
        }
        result = generate_asset(brief, adapter=None)
        self.assertEqual(result["status"], "placeholder")
        self.assertEqual(result["license"], "CC0-1.0")
        self.assertEqual(result["asset_id"], "AST-0001")

    def test_stdio_adapter_must_return_complete_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "adapter.py"
            script.write_text(
                "import json,sys\nbrief=json.load(sys.stdin)\n"
                "json.dump({'status':'generated','asset_id':brief['id'],'license':'CC0-1.0','sha256':'a'*64,'provenance':{}},sys.stdout)\n",
                encoding="utf-8",
            )
            brief = {
                "schema_version": "1.0",
                "id": "AST-0001",
                "kind": "image",
                "purpose": "Player",
                "runtime_path": "assets/player.png",
            }
            with self.assertRaisesRegex(ValueError, "provenance"):
                generate_asset(brief, adapter=[sys.executable, str(script)])

    def test_failed_optional_provider_falls_back_without_changing_scope(self) -> None:
        brief = {
            "schema_version": "1.0",
            "id": "AST-0042",
            "kind": "image",
            "purpose": "Approved source-part fallback",
            "runtime_path": "assets/source/fallback.png",
        }
        result = generate_asset(
            brief,
            adapter=[sys.executable, "-c", "import sys; sys.exit(7)"],
        )
        self.assertEqual("placeholder", result["status"])
        self.assertEqual(brief["id"], result["asset_id"])
        self.assertEqual(brief["runtime_path"], result["runtime_path"])
        self.assertIn("adapter_failure", result["provenance"])

    def test_lfs_is_planned_only_for_generated_game_repositories(self) -> None:
        plan = build_lfs_plan(Path("game"))
        self.assertIn("assets/source/**/*.psd", plan["patterns"])
        self.assertIn("assets/source/**/*.wav", plan["patterns"])
        self.assertEqual(plan["status"], "dry_run")


class SchemaValidationTests(unittest.TestCase):
    def test_media_direction_supports_locked_pixel_sizes_through_128(self) -> None:
        schema_root = Path(__file__).parents[1] / "src" / "aigame" / "schemas"
        schema = json.loads((schema_root / "media-direction.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        direction = {
            "schema_version": "1.0",
            "id": "MDR-0001",
            "revision": 1,
            "status": "approved",
            "created_at": "2026-07-16T00:00:00Z",
            "updated_at": "2026-07-16T00:00:00Z",
            "input_fingerprint": "a" * 64,
            "art": {
                "style": "indexed pixel art",
                "perspective": "top_down",
                "frame_size": [128, 128],
                "tile_size": [128, 128],
                "outline": "one logical pixel",
                "shading": "three values",
                "light_direction": "upper_left",
                "palette": {"transparent": "#00000000", "outline": "#171526ff"},
                "reserved_colors": {},
                "readability": "distinct silhouettes",
            },
            "audio": {
                "identity": "mechanical ember",
                "sample_rate": 48000,
                "bit_depth": 16,
                "peak_dbfs": -1,
                "spatialization": "event-defined",
                "looping": "seamless when requested",
            },
            "accessibility": {
                "color_independent_cues": True,
                "reduced_flash": True,
                "reduced_motion": True,
                "audio_alternatives": True,
            },
            "technical": {
                "engine": "Godot 4.7",
                "image_format": "png",
                "image_mode": "indexed",
                "filter": "nearest",
                "background": "transparent",
                "budgets": {},
            },
            "references": {"canonical": [], "structural": [], "stylistic": [], "negative": []},
            "control": {
                "locked": ["art.frame_size", "art.tile_size"],
                "variable": [],
                "generator_decides": [],
                "negative_constraints": ["no dimensions above 128 pixels"],
            },
        }
        self.assertEqual([], list(validator.iter_errors(direction)))
        invalid = json.loads(json.dumps(direction))
        invalid["art"]["frame_size"] = [129, 129]
        self.assertTrue(list(validator.iter_errors(invalid)))

    def test_persisted_record_schemas_require_common_identity_and_timestamps(self) -> None:
        schema_root = Path(__file__).parents[1] / "src" / "aigame" / "schemas"
        for name in (
            "requirement.schema.json",
            "work-item.schema.json",
            "approval.schema.json",
            "asset-record.schema.json",
            "evidence-record.schema.json",
            "decision.schema.json",
            "experiment.schema.json",
        ):
            schema = json.loads((schema_root / name).read_text(encoding="utf-8"))
            with self.subTest(schema=name):
                self.assertTrue(
                    {
                        "schema_version",
                        "id",
                        "revision",
                        "status",
                        "created_at",
                        "updated_at",
                        "input_fingerprint",
                    }.issubset(schema["required"])
                )

    def test_work_item_missing_title_fails_schema_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            item_path = root / "work" / "items" / "WI-0001.json"
            item = json.loads(item_path.read_text(encoding="utf-8"))
            del item["title"]
            item_path.write_text(json.dumps(item), encoding="utf-8")
            report = validate_project(root)
            self.assertTrue(any("title" in error for error in report["errors"]))

    def test_generated_asset_without_model_metadata_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            manifest_path = root / "assets" / "asset-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["assets"] = [
                {
                    "schema_version": "1.0",
                    "id": "AST-0001",
                    "revision": 1,
                    "status": "generated",
                    "input_fingerprint": "a" * 64,
                    "kind": "image",
                    "runtime_path": "assets/player.png",
                    "license": "CC0-1.0",
                    "sha256": "b" * 64,
                    "provenance": {"generated": True, "provider": "test"},
                }
            ]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            report = validate_project(root)
            self.assertTrue(any("provenance" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
