from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from aigame.cli import main
from aigame.generator import create_game
from aigame.state import claim_work_item
from aigame.upgrade import upgrade_workflow


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


class ClaimTests(unittest.TestCase):
    def test_claim_is_serial_dry_run_safe_and_creates_ai_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            git(root, "init", "-b", "main")
            git(root, "config", "user.name", "Test User")
            git(root, "config", "user.email", "test@example.com")
            git(root, "add", ".")
            git(root, "commit", "-m", "initial")

            preview = claim_work_item(root, "WI-0001", apply=False)
            self.assertEqual(preview["status"], "dry_run")
            self.assertEqual(git(root, "branch", "--show-current"), "main")

            claimed = claim_work_item(root, "WI-0001", apply=True)
            self.assertEqual(claimed["status"], "passed")
            self.assertEqual(git(root, "branch", "--show-current"), "ai/WI-0001-complete-structured-game-discovery")
            state = json.loads(
                (root / ".aigame" / "state" / "current.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["active_work_item"], "WI-0001")
            item = json.loads(
                (root / "work" / "items" / "WI-0001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(item["status"], "claimed")

    def test_second_active_claim_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            state_path = root / ".aigame" / "state" / "current.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["active_work_item"] = "WI-9999"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "active"):
                claim_work_item(root, "WI-0001", apply=False)


class CommandTests(unittest.TestCase):
    def _run(self, arguments: list[str]) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(arguments)
        return code, json.loads(output.getvalue())

    def test_next_context_and_validate_commands_emit_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            code, selected = self._run(["next", "--project", str(root), "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(selected["work_item"]["id"], "WI-0001")

            code, packet = self._run(
                ["context", "WI-0001", "--project", str(root), "--json"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(packet["work_item"]["id"], "WI-0001")

            code, report = self._run(
                ["validate", "WI-0001", "--project", str(root), "--json"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "passed")

    def test_new_command_previews_then_generates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "new-game"
            code, preview = self._run(
                ["new", "New Game", "--destination", str(root), "--godot-version", "4.7", "--json"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(preview["status"], "dry_run")
            self.assertFalse(root.exists())

            code, result = self._run(
                ["new", "New Game", "--destination", str(root), "--godot-version", "4.7", "--apply", "--json"]
            )
            self.assertEqual(code, 0)
            self.assertTrue((root / "project.godot").is_file())
            self.assertTrue((root / ".git").is_dir())
            attributes = (root / ".gitattributes").read_text(encoding="utf-8")
            self.assertIn("*.psd filter=lfs", attributes)
            self.assertIn("*.wav filter=lfs", attributes)
            self.assertEqual(result["status"], "passed")

    def test_new_remote_repository_is_previewed_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "remote-game"
            code, preview = self._run(
                [
                    "new", "Remote Game", "--destination", str(root),
                    "--godot-version", "4.7", "--github", "monstermic/remote-game", "--json"
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(preview["github"]["repository"], "monstermic/remote-game")
            self.assertEqual(preview["github"]["visibility"], "private")
            self.assertFalse(root.exists())

    def test_init_and_repository_configuration_commands_are_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "existing"
            root.mkdir()
            git(root, "init", "-b", "main")
            code, preview = self._run(
                ["init", "Existing", "--project", str(root), "--godot-version", "4.7", "--json"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(preview["status"], "dry_run")
            code, result = self._run(
                ["init", "Existing", "--project", str(root), "--godot-version", "4.7", "--apply", "--json"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "passed")

        code, repository = self._run(
            ["repository", "configure", "monstermic/godot-ai-game-workflow", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(repository["status"], "dry_run")
        self.assertTrue(repository["repository"]["is_template"])

        code, project = self._run(
            ["project", "configure", "monstermic/godot-ai-game-workflow", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(project["status"], "dry_run")
        self.assertIn("Workflow Status", project["fields"])


class UpgradeTests(unittest.TestCase):
    def test_upgrade_requires_apply_and_updates_pinned_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            lock_path = root / ".aigame" / "workflow.lock.json"
            before = lock_path.read_text(encoding="utf-8")
            preview = upgrade_workflow(root, "1.1.0", "a" * 40, "b" * 64, apply=False)
            self.assertEqual(preview["status"], "dry_run")
            self.assertEqual(lock_path.read_text(encoding="utf-8"), before)
            result = upgrade_workflow(root, "1.1.0", "a" * 40, "b" * 64, apply=True)
            self.assertEqual(result["status"], "passed")
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(lock["workflow_version"], "1.1.0")
            self.assertEqual(lock["source_commit"], "a" * 40)


if __name__ == "__main__":
    unittest.main()
