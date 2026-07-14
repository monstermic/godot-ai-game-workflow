from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from aigame.cli import main
from aigame.generator import create_game
from aigame.github import build_sync_plan, intake_to_proposal
from aigame.release import ApprovalRequired, prepare_release_candidate, promote_release


class GitHubSyncTests(unittest.TestCase):
    def test_sync_plan_has_stable_hidden_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            first = build_sync_plan(root)
            second = build_sync_plan(root)
            self.assertEqual(first, second)
            self.assertIn("<!-- aigame:WI-0001:", first["issues"][0]["body"])

            item_path = root / "work" / "items" / "WI-0001.json"
            item = json.loads(item_path.read_text(encoding="utf-8"))
            item["concept_refs"] = ["MEC-0001", "CNT-0001", "GDD-0001"]
            item_path.write_text(json.dumps(item), encoding="utf-8")
            linked = build_sync_plan(root)
            self.assertIn("Concept: `MEC-0001`, `CNT-0001`, `GDD-0001`", linked["issues"][0]["body"])

    def test_issue_intake_is_data_not_an_executable_command(self) -> None:
        proposal = intake_to_proposal("Add dash; Remove-Item -Recurse C:\\", "Player wants mobility")
        self.assertEqual(proposal["status"], "draft")
        self.assertEqual(proposal["source"], "github_intake")
        self.assertIn("Remove-Item", proposal["title"])
        self.assertNotIn("command", proposal)


class ReleaseTests(unittest.TestCase):
    def test_release_promotion_requires_matching_approval_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            artifact = root / "build.zip"
            artifact.write_bytes(b"immutable-build")
            manifest = prepare_release_candidate(root, artifact, "v0.1.0-rc.1", apply=True)
            with self.assertRaises(ApprovalRequired):
                promote_release(root, manifest["manifest_path"], None, apply=True)

            approval_path = root / "approval.json"
            approval_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "id": "APR-0001",
                        "revision": 1,
                        "status": "approved",
                        "scope_hash": manifest["scope_hash"],
                        "commit_sha": manifest["commit_sha"],
                        "approver": "human-owner",
                    }
                ),
                encoding="utf-8",
            )
            result = promote_release(
                root, manifest["manifest_path"], approval_path, apply=False
            )
            self.assertEqual(result["status"], "needs_human")
            self.assertEqual(result["artifact_sha256"], manifest["artifact_sha256"])


class CliTests(unittest.TestCase):
    def test_doctor_emits_machine_readable_capabilities(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["doctor", "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertTrue(payload["capabilities"]["filesystem"])
        self.assertTrue(payload["capabilities"]["shell"])


if __name__ == "__main__":
    unittest.main()
