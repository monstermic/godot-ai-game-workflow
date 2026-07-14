from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from aigame.generator import create_game
from aigame.github import sync_github
from aigame.mcp import handle_request


class FakeGitHubClient:
    def __init__(self) -> None:
        self.issues: list[dict] = []
        self.created = 0
        self.updated = 0
        self.project_syncs = 0

    def list_issues(self) -> list[dict]:
        return list(self.issues)

    def ensure_labels(self, labels: list[str]) -> None:
        del labels

    def create_issue(self, issue: dict) -> None:
        self.created += 1
        self.issues.append({**issue, "number": len(self.issues) + 1})

    def update_issue(self, number: int, issue: dict) -> None:
        self.updated += 1
        index = next(i for i, value in enumerate(self.issues) if value["number"] == number)
        self.issues[index] = {**issue, "number": number}

    def sync_project(self, issues: list[dict], plan: dict) -> None:
        del issues, plan
        self.project_syncs += 1


class DistributionTests(unittest.TestCase):
    def test_generated_game_contains_schemas_ci_adapters_and_full_reference_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            required = [
                ".aigame/schemas/work-item.schema.json",
                ".aigame/schemas/requirement.schema.json",
                ".aigame/schemas/agent-capabilities.schema.json",
                ".aigame/schemas/run-result.schema.json",
                ".aigame/schemas/approval.schema.json",
                ".aigame/schemas/asset-record.schema.json",
                ".aigame/schemas/evidence-record.schema.json",
                ".aigame/schemas/decision.schema.json",
                ".aigame/schemas/experiment.schema.json",
                ".aigame/schemas/project-config.schema.json",
                ".aigame/schemas/asset-brief.schema.json",
                ".aigame/schemas/asset-generation-result.schema.json",
                ".aigame/schemas/game-blueprint.schema.json",
                ".aigame/schemas/mechanic-spec.schema.json",
                ".aigame/schemas/content-entry.schema.json",
                ".aigame/schemas/automation-policy.schema.json",
                ".aigame/automation.json",
                ".aigame/profiles/core-game-v1.json",
                ".aigame/profiles/roguelite-v1.json",
                ".aigame/agent-skills/build-game-concept/SKILL.md",
                ".aigame/vendor/aigame/cli.py",
                ".github/workflows/quality.yml",
                ".github/workflows/github-sync.yml",
                ".github/workflows/intake-proposal.yml",
                ".github/workflows/release-candidate.yml",
                ".github/workflows/release.yml",
                ".github/workflows/staging-merge.yml",
                ".github/PULL_REQUEST_TEMPLATE.md",
                "LICENSE",
                "LICENSES/workflow-Apache-2.0.txt",
                "game/game_state.gd",
                "tests/run_tests.gd",
                "work/decisions/README.md",
                "work/experiments/README.md",
                "work/milestones/README.md",
                ".aigame/capability-packs/3d.toml",
                ".aigame/capability-packs/narrative.toml",
                ".aigame/capability-packs/localization.toml",
                ".aigame/capability-packs/mobile.toml",
                ".aigame/capability-packs/persistence.toml",
                ".aigame/capability-packs/networking.toml",
            ]
            for relative in required:
                self.assertTrue((root / relative).is_file(), relative)
            workflow = (root / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
            self.assertIn("branches: [main, staging]", workflow)
            references = re.findall(r"uses:\s+[^\s]+@([^\s]+)", workflow)
            self.assertGreaterEqual(len(references), 2)
            self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", value) for value in references))
            staging = (root / ".github" / "workflows" / "staging-merge.yml").read_text(
                encoding="utf-8"
            )
            self.assertIn("issue_comment:", staging)
            self.assertIn(".github/workflows/staging-merge.yml@", staging)
            self.assertIn("statuses: write", staging)
            state = (root / "game" / "game_state.gd").read_text(encoding="utf-8")
            self.assertIn("func evaluate_collisions", state)
            self.assertIn("score += 100", state)


class McpTests(unittest.TestCase):
    def test_initialize_negotiates_protocol_and_capabilities(self) -> None:
        response = handle_request(
            Path.cwd(),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
        )
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", response["result"]["capabilities"])
        self.assertIn("resources", response["result"]["capabilities"])

    def test_tools_list_exposes_portable_core_operations(self) -> None:
        response = handle_request(
            Path.cwd(), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "aigame_context",
                "aigame_next",
                "aigame_validate",
                "aigame_concept_next",
                "aigame_concept_validate",
                "aigame_mode_get",
                "aigame_mode_set",
                "aigame_staging_init",
                "aigame_staging_merge",
            },
        )

    def test_mode_tool_returns_the_canonical_automation_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            response = handle_request(
                root,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "aigame_mode_get", "arguments": {}},
                },
            )
            self.assertEqual(response["result"]["structuredContent"]["mode"], "human_gated")


class GitHubApplyTests(unittest.TestCase):
    def test_sync_is_idempotent_and_updates_changed_canonical_item(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            client = FakeGitHubClient()
            first = sync_github(root, client=client, apply=True)
            self.assertEqual(first["created"], 1)
            self.assertEqual(client.project_syncs, 1)
            second = sync_github(root, client=client, apply=True)
            self.assertEqual(second["created"], 0)
            self.assertEqual(second["updated"], 0)
            self.assertEqual(client.project_syncs, 2)

            item_path = root / "work" / "items" / "WI-0001.json"
            item = json.loads(item_path.read_text(encoding="utf-8"))
            item["title"] = "Revised discovery"
            item_path.write_text(json.dumps(item), encoding="utf-8")
            third = sync_github(root, client=client, apply=True)
            self.assertEqual(third["updated"], 1)
            self.assertEqual(len(client.issues), 1)


if __name__ == "__main__":
    unittest.main()
