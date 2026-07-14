from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import aigame.generator as generator
from aigame.generator import create_game
from aigame.core import fingerprint
from aigame.validation import validate_project


class GeneratorTests(unittest.TestCase):
    def test_workflow_commit_is_resolved_from_the_distribution_not_the_callers_cwd(self) -> None:
        completed = Mock(stdout="a" * 40)
        with patch.dict(os.environ, {}, clear=True), patch(
            "aigame.generator.subprocess.run", return_value=completed
        ) as run:
            self.assertEqual(generator._workflow_commit(), "a" * 40)

        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["git", "-C"])
        self.assertEqual(Path(command[2]), Path(generator.__file__).resolve().parents[2])

    def test_create_game_writes_portable_project_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "my-game"
            preview = create_game(root, "My Game", godot_version="4.7", apply=False)
            self.assertEqual(preview["status"], "dry_run")
            self.assertFalse(root.exists())

            result = create_game(root, "My Game", godot_version="4.7", apply=True)
            self.assertEqual(result["status"], "passed")
            self.assertTrue((root / "AGENTS.md").is_file())
            self.assertTrue((root / ".aigame" / "project.toml").is_file())
            self.assertTrue((root / "project.godot").is_file())
            self.assertTrue((root / "assets" / "asset-manifest.json").is_file())
            self.assertTrue((root / "work" / "items" / "WI-0001.json").is_file())

            config = (root / ".aigame" / "project.toml").read_text(encoding="utf-8")
            self.assertIn('visibility = "private"', config)
            self.assertIn('source_license = "proprietary"', config)
            self.assertIn('godot_release = "4.7-stable"', config)
            self.assertRegex(config, r'godot_linux_sha512 = "[0-9a-f]{128}"')
            self.assertRegex(config, r'godot_windows_sha512 = "[0-9a-f]{128}"')
            self.assertRegex(config, r'godot_templates_sha512 = "[0-9a-f]{128}"')
            self.assertNotIn("filter=lfs", (root / ".gitattributes").read_text(encoding="utf-8"))

            report = validate_project(root)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["errors"], [])

    def test_unknown_asset_license_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            manifest_path = root / "assets" / "asset-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["assets"].append(
                {
                    "schema_version": "1.0",
                    "id": "AST-0001",
                    "revision": 1,
                    "status": "draft",
                    "input_fingerprint": "pending",
                    "kind": "image",
                    "runtime_path": "assets/mystery.png",
                    "license": "unknown",
                    "provenance": {},
                    "sha256": "",
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            report = validate_project(root)
            self.assertEqual(report["status"], "failed")
            self.assertTrue(any("license" in error.lower() for error in report["errors"]))

    def test_project_toml_is_validated_against_the_project_config_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            config_path = root / ".aigame" / "project.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    'renderer = "gl_compatibility"', 'renderer = "imaginary"'
                ),
                encoding="utf-8",
            )
            report = validate_project(root)
            self.assertTrue(any("ProjectConfig" in error for error in report["errors"]))

    def test_dangling_requirement_reference_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            item_path = root / "work" / "items" / "WI-0001.json"
            item = json.loads(item_path.read_text(encoding="utf-8"))
            item["requirements"] = ["REQ-9999"]
            item_path.write_text(json.dumps(item), encoding="utf-8")
            report = validate_project(root)
            self.assertTrue(any("REQ-9999" in error for error in report["errors"]))

    def test_changed_record_invalidates_its_input_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            item_path = root / "work" / "items" / "WI-0001.json"
            item = json.loads(item_path.read_text(encoding="utf-8"))
            item["title"] = "Changed without accepting the new inputs"
            item_path.write_text(json.dumps(item), encoding="utf-8")
            report = validate_project(root)
            self.assertTrue(any("fingerprint" in error for error in report["errors"]))

    def test_vendored_workflow_snapshot_must_match_the_locked_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            module_path = root / ".aigame" / "vendor" / "aigame" / "core.py"
            module_path.write_text(
                module_path.read_text(encoding="utf-8") + "\n# unreviewed drift\n",
                encoding="utf-8",
            )
            report = validate_project(root)
            self.assertTrue(any("workflow snapshot checksum" in error for error in report["errors"]))

    def test_active_item_must_match_the_serial_state_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            item_path = root / "work" / "items" / "WI-0001.json"
            item = json.loads(item_path.read_text(encoding="utf-8"))
            item["status"] = "claimed"
            item["revision"] += 1
            item["input_fingerprint"] = fingerprint(
                {key: value for key, value in item.items() if key != "input_fingerprint"}
            )
            item_path.write_text(json.dumps(item), encoding="utf-8")
            report = validate_project(root)
            self.assertTrue(any("state projection" in error for error in report["errors"]))

    def test_evidence_references_and_done_item_traceability_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            item_path = root / "work" / "items" / "WI-0001.json"
            item = json.loads(item_path.read_text(encoding="utf-8"))
            item["status"] = "done"
            item["revision"] += 1
            item["input_fingerprint"] = fingerprint(
                {key: value for key, value in item.items() if key != "input_fingerprint"}
            )
            item_path.write_text(json.dumps(item), encoding="utf-8")

            report = validate_project(root)
            self.assertTrue(any("has no current evidence" in error for error in report["errors"]))

            created_at = "2026-07-14T00:00:00Z"
            evidence = {
                "schema_version": "1.0",
                "id": "EVD-0001",
                "revision": 1,
                "status": "current",
                "created_at": created_at,
                "updated_at": created_at,
                "kind": "test",
                "work_item_id": "WI-0001",
                "requirement_ids": ["REQ-9999"],
                "artifact_sha256": "a" * 64,
            }
            evidence["input_fingerprint"] = fingerprint(evidence)
            evidence_path = root / "evidence" / "records" / "EVD-0001.json"
            evidence_path.parent.mkdir(parents=True)
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            report = validate_project(root)
            self.assertTrue(any("dangling requirement REQ-9999" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
