from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aigame
from aigame import __version__
from aigame.cli import main
from aigame.concept import next_concept_task
from aigame.core import workflow_snapshot_checksum
from aigame.generator import create_game
from aigame.state import claim_work_item
from aigame.upgrade import upgrade_workflow
from aigame.validation import validate_project


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
            self.assertEqual(git(root, "branch", "--show-current"), "ai/WI-0001-create-complete-game-blueprint-from-user-prompt")
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
            self.assertEqual(code, 3)
            self.assertEqual(selected["gate"], "concept_start")

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

    def test_new_command_sets_a_repository_local_identity_when_git_has_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "identity-game"
            isolated_global = Path(temporary) / "missing-global-gitconfig"
            with patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": str(isolated_global),
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            ):
                code, result = self._run(
                    [
                        "new", "Identity Game", "--destination", str(root),
                        "--godot-version", "4.7", "--apply", "--json",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(git(root, "config", "--local", "user.name"), "AI Game Workflow")
            self.assertEqual(
                git(root, "config", "--local", "user.email"),
                "aigame@users.noreply.github.com",
            )

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

        code, game_repository = self._run(
            ["repository", "configure-game", "monstermic/private-game", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(game_repository["status"], "dry_run")
        self.assertNotIn("visibility", game_repository["repository"])
        self.assertNotIn("is_template", game_repository["repository"])

        code, project = self._run(
            ["project", "configure", "monstermic/godot-ai-game-workflow", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(project["status"], "dry_run")
        self.assertIn("Workflow Status", project["fields"])


class UpgradeTests(unittest.TestCase):
    def test_upgrade_reports_structured_asset_specification_remediation_for_legacy_games(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Legacy Game", godot_version="4.7", apply=True)
            concept_state_path = root / ".aigame/state/concept.json"
            concept_state = json.loads(concept_state_path.read_text(encoding="utf-8"))
            concept_state["status"] = "finalized"
            concept_state["blueprint_approval_id"] = "APR-0001"
            concept_state_path.write_text(json.dumps(concept_state), encoding="utf-8")
            source_root = Path(aigame.__file__).resolve().parent
            source_checksum = workflow_snapshot_checksum(source_root)
            result = upgrade_workflow(
                root,
                __version__,
                "b" * 40,
                source_checksum,
                apply=False,
            )
            self.assertTrue(result["requires_concept_revision"])
            self.assertEqual("asset_specification", result["remediation"]["restart_stage"])
            self.assertIn("aigame concept revise", result["remediation"]["command"])

    def test_upgrade_rejects_existing_but_invalid_structured_media_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Invalid Media Contract", godot_version="4.7", apply=True)
            concept_state_path = root / ".aigame/state/concept.json"
            concept_state = json.loads(concept_state_path.read_text(encoding="utf-8"))
            concept_state["status"] = "finalized"
            concept_state_path.write_text(json.dumps(concept_state), encoding="utf-8")
            direction_path = root / "work/concept/MDR-0001.json"
            direction_path.parent.mkdir(parents=True, exist_ok=True)
            direction_path.write_text("{}", encoding="utf-8")
            request_path = root / "work/concept/media_requests/ARQ-0001.json"
            request_path.parent.mkdir(parents=True, exist_ok=True)
            request_path.write_text("{}", encoding="utf-8")

            source_root = Path(aigame.__file__).resolve().parent
            result = upgrade_workflow(
                root,
                __version__,
                "c" * 40,
                workflow_snapshot_checksum(source_root),
                apply=False,
            )

            self.assertTrue(result["requires_concept_revision"])
            self.assertTrue(result["remediation"]["contract_errors"])

    def test_upgrade_routes_legacy_quality_audit_back_to_asset_specification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Legacy Mid Concept", godot_version="4.7", apply=True)
            concept_state_path = root / ".aigame/state/concept.json"
            concept_state = json.loads(concept_state_path.read_text(encoding="utf-8"))
            concept_state["status"] = "quality_audit"
            concept_state_path.write_text(json.dumps(concept_state), encoding="utf-8")

            source_root = Path(aigame.__file__).resolve().parent
            result = upgrade_workflow(
                root,
                __version__,
                "d" * 40,
                workflow_snapshot_checksum(source_root),
                apply=False,
            )

            self.assertTrue(result["requires_concept_revision"])
            self.assertEqual("asset_specification", result["remediation"]["restart_stage"])

    def test_upgrade_is_additive_and_installs_the_complete_current_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            automation_path = root / ".aigame" / "automation.json"
            automation_path.unlink()
            concept_state_path = root / ".aigame" / "state" / "concept.json"
            concept_state_path.unlink()
            media_state_path = root / ".aigame" / "state" / "assets.json"
            media_state_path.unlink()
            media_config_path = root / ".aigame" / "media.toml"
            media_config_path.unlink()
            media_skill_path = root / ".aigame" / "agent-skills" / "generate-game-assets" / "SKILL.md"
            media_skill_path.unlink()
            runtime_composer_path = root / "addons" / "aigame_media" / "runtime_composer.gd"
            runtime_composer_path.unlink()
            (root / ".aigame" / "schemas" / "automation-policy.schema.json").unlink()
            (root / ".aigame" / "vendor" / "aigame" / "automation.py").unlink()
            (root / ".aigame" / "vendor" / "aigame" / "schemas" / "automation-policy.schema.json").unlink()
            staging_workflow = root / ".github" / "workflows" / "staging-merge.yml"
            staging_workflow.unlink()
            config_path = root / ".aigame" / "project.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    f'workflow_version = "{__version__}"',
                    'workflow_version = "1.1.0"',
                ),
                encoding="utf-8",
            )
            design_path = root / "docs" / "game_design.md"
            design_path.write_text("# Owner design\n\nNever overwrite this.\n", encoding="utf-8")
            lock_path = root / ".aigame" / "workflow.lock.json"
            before = lock_path.read_text(encoding="utf-8")
            source_root = Path(aigame.__file__).resolve().parent
            source_checksum = workflow_snapshot_checksum(source_root)
            preview = upgrade_workflow(
                root,
                __version__,
                "a" * 40,
                source_checksum,
                apply=False,
            )
            self.assertEqual(preview["status"], "dry_run")
            self.assertIn(".aigame/automation.json", preview["install"])
            self.assertIn(".aigame/state/assets.json", preview["install"])
            self.assertIn(".aigame/media.toml", preview["install"])
            self.assertEqual(lock_path.read_text(encoding="utf-8"), before)
            result = upgrade_workflow(
                root,
                __version__,
                "a" * 40,
                source_checksum,
                apply=True,
            )
            self.assertEqual(result["status"], "passed")
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(lock["workflow_version"], __version__)
            self.assertEqual(lock["source_commit"], "a" * 40)
            self.assertEqual(lock["source_sha256"], source_checksum)
            self.assertTrue(automation_path.is_file())
            self.assertTrue(concept_state_path.is_file())
            self.assertTrue(media_state_path.is_file())
            self.assertTrue(media_config_path.is_file())
            self.assertTrue(media_skill_path.is_file())
            self.assertTrue(runtime_composer_path.is_file())
            self.assertEqual(next_concept_task(root)["gate"], "concept_start")
            self.assertTrue((root / ".aigame" / "schemas" / "automation-policy.schema.json").is_file())
            self.assertTrue((root / ".aigame" / "vendor" / "aigame" / "automation.py").is_file())
            self.assertTrue(staging_workflow.is_file())
            self.assertIn(
                f'workflow_version = "{__version__}"',
                config_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                design_path.read_text(encoding="utf-8"),
                "# Owner design\n\nNever overwrite this.\n",
            )
            self.assertEqual(validate_project(root)["status"], "passed")


if __name__ == "__main__":
    unittest.main()
