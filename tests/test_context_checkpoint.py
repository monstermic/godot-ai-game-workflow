from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aigame.context import build_context
from aigame.generator import create_game
from aigame.state import checkpoint


class ContextTests(unittest.TestCase):
    def test_context_contains_only_linked_requirements_and_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            packet = build_context(root, "WI-0001")
            self.assertEqual(packet["work_item"]["id"], "WI-0001")
            self.assertEqual([record["id"] for record in packet["requirements"]], ["REQ-0001"])
            self.assertIn("game_brief", packet["documents"])
            self.assertNotIn("environment", packet)


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_rejects_a_run_result_that_does_not_match_the_public_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            result_path = Path(temporary) / "invalid-run-result.json"
            result_path.write_text(
                json.dumps({"schema_version": "1.0", "work_item_id": "WI-0001"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "RunResult"):
                checkpoint(root, "WI-0001", result_path, apply=False)

    def test_checkpoint_is_dry_run_by_default_and_persists_when_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            result_path = root / "run-result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "status": "blocked",
                        "work_item_id": "WI-0001",
                        "changed_artifacts": [],
                        "evidence": [],
                        "next_actions": ["Install Godot"],
                    }
                ),
                encoding="utf-8",
            )

            preview = checkpoint(root, "WI-0001", result_path, apply=False)
            self.assertEqual(preview["status"], "dry_run")
            self.assertFalse((root / "evidence" / "WI-0001" / "checkpoint.json").exists())

            persisted = checkpoint(root, "WI-0001", result_path, apply=True)
            self.assertEqual(persisted["status"], "passed")
            checkpoint_path = root / "evidence" / "WI-0001" / "checkpoint.json"
            self.assertTrue(checkpoint_path.is_file())
            saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["run_result"]["status"], "blocked")
            self.assertEqual(len(saved["run_result_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
