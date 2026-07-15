from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from aigame.assets import build_lfs_plan, generate_asset
from aigame.generator import create_game
from aigame.validation import validate_project


class AssetAdapterTests(unittest.TestCase):
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
