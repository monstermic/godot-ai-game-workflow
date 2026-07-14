from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aigame import __version__
from aigame.automation import (
    AI_MODE_CONFIRMATION,
    get_automation_policy,
    initialize_staging,
    prepare_staging_merge,
    set_automation_mode,
)
from aigame.cli import _doctor, build_parser
from aigame.concept import next_concept_task, start_concept, submit_concept_task
from aigame.core import WorkflowError, choose_next, fingerprint
from aigame.generator import create_game
from aigame.validation import validate_project
from tests.test_concept_blueprint import (
    audit_result,
    catalog_result,
    game_arc_result,
    mechanic_result,
    pitch_result,
    product_identity_result,
)


class FakeStagingClient:
    def __init__(self) -> None:
        self.initialized = 0
        self.reverse_checks = False

    def ensure_staging_branch(self) -> dict:
        self.initialized += 1
        return {"branch": "staging", "created": True, "sha": "b" * 40}

    def view_pull_request(self, number: int) -> dict:
        value = {
            "number": number,
            "url": f"https://github.test/example/game/pull/{number}",
            "state": "OPEN",
            "isDraft": False,
            "baseRefName": "staging",
            "headRefName": "ai/WI-0002-dash",
            "headRefOid": "a" * 40,
            "mergeStateStatus": "CLEAN",
            "reviewDecision": None,
            "statusCheckRollup": [
                {"name": "contract / contract", "conclusion": "SUCCESS"},
                {"name": "godot-quality / godot-quality", "conclusion": "SUCCESS"},
                {"name": "asset-provenance / asset-provenance", "conclusion": "SUCCESS"},
                {"name": "independent-review / independent-review", "conclusion": "SUCCESS"},
            ],
        }
        if self.reverse_checks:
            value["statusCheckRollup"].reverse()
        return value

class AutomationModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "game"
        create_game(self.root, "Game", godot_version="4.7", apply=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ai_mode_is_explicit_and_preserves_non_bypassable_gates(self) -> None:
        policy = get_automation_policy(self.root)
        self.assertEqual(policy["mode"], "human_gated")
        before = (self.root / ".aigame" / "automation.json").read_text(encoding="utf-8")
        preview = set_automation_mode(self.root, "ai_staging", apply=False)
        self.assertEqual(preview["status"], "dry_run")
        self.assertEqual((self.root / ".aigame" / "automation.json").read_text(encoding="utf-8"), before)
        with self.assertRaisesRegex(WorkflowError, "confirmation"):
            set_automation_mode(self.root, "ai_staging", apply=True)
        enabled = set_automation_mode(
            self.root,
            "ai_staging",
            confirmation=AI_MODE_CONFIRMATION,
            apply=True,
        )
        self.assertEqual(enabled["policy"]["decision_authority"], "agent")
        self.assertEqual(_doctor(self.root)["automation_mode"], "ai_staging")
        self.assertEqual(enabled["policy"]["permission_policy"], "auto_approve_project_operations")
        self.assertEqual(
            enabled["policy"]["auto_approved_operations"],
            [
                "creative_decisions",
                "project_file_writes",
                "allowlisted_shell",
                "tests",
                "git_branch",
                "git_commit",
                "pull_request_prepare",
            ],
        )
        self.assertTrue(
            {"independent_review", "staging_owner_approval", "production", "rollback"}.issubset(
                enabled["policy"]["protected_gates"]
            )
        )
        selected = choose_next(
            [
                {
                    "id": "WI-0042",
                    "status": "ready",
                    "dependencies": [],
                    "unknowns": [{"level": "red", "question": "Choose the agent-owned default"}],
                    "required_capabilities": [],
                }
            ],
            set(),
            allow_red_unknowns=True,
        )
        self.assertEqual(selected["id"], "WI-0042")

    def test_project_validation_rejects_removed_ai_mode_protections(self) -> None:
        policy_path = self.root / ".aigame" / "automation.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["protected_gates"].remove("staging_owner_approval")
        policy["input_fingerprint"] = fingerprint(
            {key: value for key, value in policy.items() if key != "input_fingerprint"}
        )
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        report = validate_project(self.root)
        self.assertTrue(any("AutomationPolicy" in error for error in report["errors"]))

    def test_cli_exposes_mode_and_staging_commands(self) -> None:
        parser = build_parser()
        mode = parser.parse_args(["mode", "show", "--project", str(self.root), "--json"])
        self.assertEqual(mode.command, "mode")
        self.assertEqual(mode.mode_command, "show")
        staging = parser.parse_args(
            ["staging", "merge", "17", "--project", str(self.root), "--json"]
        )
        self.assertEqual(staging.command, "staging")
        self.assertEqual(staging.staging_command, "merge")
        staging_init = parser.parse_args(
            ["staging", "init", "--project", str(self.root), "--json"]
        )
        self.assertEqual(staging_init.staging_command, "init")

    def test_staging_branch_initialization_is_dry_run_by_default(self) -> None:
        set_automation_mode(
            self.root,
            "ai_staging",
            confirmation=AI_MODE_CONFIRMATION,
            apply=True,
        )
        client = FakeStagingClient()
        preview = initialize_staging(self.root, client=client, apply=False)
        self.assertEqual(preview["status"], "dry_run")
        self.assertEqual(client.initialized, 0)
        created = initialize_staging(self.root, client=client, apply=True)
        self.assertEqual(created["status"], "passed")
        self.assertEqual(client.initialized, 1)

    def test_ai_mode_automatically_decides_and_approves_the_complete_blueprint(self) -> None:
        set_automation_mode(
            self.root,
            "ai_staging",
            confirmation=AI_MODE_CONFIRMATION,
            apply=True,
        )
        start_concept(self.root, prompt="A small clockwork action roguelite", profiles=["roguelite-v1"], apply=True)
        agent_pitch = pitch_result()
        agent_pitch["intake"]["assumptions"][0]["risk"] = "red"
        agent_pitch["intake"]["risks"][0]["level"] = "red"
        pitched = submit_concept_task(self.root, "CTK-0001", agent_pitch, apply=True)
        self.assertEqual(pitched["concept_task"]["stage"], "product_identity")
        identity = submit_concept_task(self.root, "CTK-0002", product_identity_result(), apply=True)
        self.assertEqual(identity["concept_task"]["stage"], "mechanics")
        submit_concept_task(self.root, "CTK-0003", mechanic_result(), apply=True)
        submit_concept_task(self.root, "CTK-0004", catalog_result(), apply=True)
        submit_concept_task(self.root, "CTK-0005", game_arc_result(), apply=True)
        finalized = submit_concept_task(self.root, "CTK-0006", audit_result(self.root), apply=True)
        self.assertEqual(finalized["status"], "passed")
        self.assertEqual(next_concept_task(self.root)["status"], "finalized")
        self.assertIn(
            f'workflow_version = "{__version__}"',
            (self.root / ".aigame" / "project.toml").read_text(encoding="utf-8"),
        )
        approvals = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.root / "evidence" / "approvals").glob("APR-*.json")
        ]
        self.assertEqual(len(approvals), 3)
        self.assertTrue(all(value["approver"] == "aigame-agent" for value in approvals))
        self.assertTrue(all(value["approval_kind"] == "agent" for value in approvals))

    def test_staging_merge_requires_exact_authenticated_owner_comment(self) -> None:
        set_automation_mode(
            self.root,
            "ai_staging",
            confirmation=AI_MODE_CONFIRMATION,
            apply=True,
        )
        client = FakeStagingClient()
        gate = prepare_staging_merge(self.root, 17, client=client, apply=False)
        self.assertEqual(gate["status"], "needs_human")
        self.assertIn("/aigame merge staging head=", gate["approval_comment"])
        self.assertIn("scope=", gate["approval_comment"])
        client.reverse_checks = True
        still_gated = prepare_staging_merge(
            self.root,
            17,
            client=client,
            apply=True,
        )
        self.assertEqual(still_gated["status"], "needs_human")
        self.assertEqual(still_gated["gate"], "github_owner_comment")
        self.assertEqual(still_gated["automatic_action"], "none")

    def test_staging_merge_rejects_missing_independent_review(self) -> None:
        set_automation_mode(
            self.root,
            "ai_staging",
            confirmation=AI_MODE_CONFIRMATION,
            apply=True,
        )
        client = FakeStagingClient()
        original = client.view_pull_request

        def without_review(number: int) -> dict:
            value = original(number)
            value["statusCheckRollup"] = [
                check
                for check in value["statusCheckRollup"]
                if check["name"] != "independent-review / independent-review"
            ]
            return value

        client.view_pull_request = without_review  # type: ignore[method-assign]
        with self.assertRaisesRegex(WorkflowError, "independent-review"):
            prepare_staging_merge(self.root, 17, client=client, apply=False)


if __name__ == "__main__":
    unittest.main()
