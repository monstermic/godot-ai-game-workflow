from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from aigame.generator import initialize_game
from aigame.repository import build_repository_plan, configure_repository
from aigame.review import ReviewRejected, validate_review


ROOT = Path(__file__).resolve().parents[1]


class RepositoryLayoutTests(unittest.TestCase):
    def test_distribution_contains_documentation_packaging_and_reusable_workflows(self) -> None:
        required = [
            "README.md",
            "LICENSE",
            "NOTICE",
            "pyproject.toml",
            "SECURITY.md",
            "docs/architecture.md",
            "docs/workflow.md",
            "docs/adapter-contract.md",
            "docs/media-policy.md",
            "adapters/generic/AGENTS.md",
            "adapters/codex/AGENTS.md",
            "adapters/claude/CLAUDE.md",
            "prompts/implement-work-item.md",
            ".github/workflows/ci.yml",
            ".github/workflows/contract.yml",
            ".github/workflows/godot-quality.yml",
            ".github/workflows/asset-provenance.yml",
            ".github/workflows/independent-review.yml",
            ".github/workflows/release-candidate.yml",
            ".github/workflows/release.yml",
            ".github/ISSUE_TEMPLATE/intake.yml",
            ".github/PULL_REQUEST_TEMPLATE.md",
        ]
        for relative in required:
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_all_external_actions_are_pinned_to_full_commit_shas(self) -> None:
        unpinned: list[str] = []
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
            text = workflow.read_text(encoding="utf-8")
            for line in text.splitlines():
                match = re.search(r"uses:\s+([^./\s][^\s]*)@([^\s]+)", line)
                if match and not re.fullmatch(r"[0-9a-f]{40}", match.group(2)):
                    unpinned.append(f"{workflow.name}: {line.strip()}")
        self.assertEqual(unpinned, [])

    def test_pyproject_exposes_aigame_console_command(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('aigame = "aigame.cli:main"', text)
        self.assertIn('requires-python = ">=3.11"', text)

    def test_release_candidate_validates_name_and_computes_metadata_after_build(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(encoding="utf-8")
        self.assertNotIn("hashFiles", text)
        self.assertIn("Invalid RC name", text)

    def test_dispatch_inputs_are_not_interpolated_directly_into_shell_scripts(self) -> None:
        unsafe: list[str] = []
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
            document = yaml.load(workflow.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
            for job_name, job in document.get("jobs", {}).items():
                for index, step in enumerate(job.get("steps", [])):
                    if "${{ inputs." in step.get("run", ""):
                        unsafe.append(f"{workflow.name}:{job_name}:{index}")
        self.assertEqual(unsafe, [])

    def test_release_gh_commands_bind_to_the_current_repository(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("GH_REPO: ${{ github.repository }}", text)


class InitializeTests(unittest.TestCase):
    def test_init_populates_an_existing_empty_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            preview = initialize_game(root, "Existing Game", godot_version="4.7", apply=False)
            self.assertEqual(preview["status"], "dry_run")
            result = initialize_game(root, "Existing Game", godot_version="4.7", apply=True)
            self.assertEqual(result["status"], "passed")
            self.assertTrue((root / "project.godot").is_file())


class ReviewTests(unittest.TestCase):
    def _report(self) -> dict:
        return {
            "schema_version": "1.0",
            "reviewed_commit": "a" * 40,
            "producer": "codex-builder",
            "reviewer": "human-owner",
            "overall": 94,
            "dimensions": {"requirements": 95, "correctness": 94, "tests": 92, "maintainability": 95},
            "findings": [],
            "evidence_hashes": ["b" * 64],
        }

    def test_valid_independent_review_passes(self) -> None:
        result = validate_review(self._report(), expected_commit="a" * 40)
        self.assertEqual(result["status"], "passed")

    def test_same_producer_and_reviewer_is_rejected(self) -> None:
        report = self._report()
        report["reviewer"] = report["producer"]
        with self.assertRaises(ReviewRejected):
            validate_review(report, expected_commit="a" * 40)

    def test_low_dimension_or_open_finding_is_rejected(self) -> None:
        report = self._report()
        report["dimensions"]["tests"] = 89
        report["findings"] = [{"severity": "warning", "message": "Missing edge case"}]
        with self.assertRaises(ReviewRejected):
            validate_review(report, expected_commit="a" * 40)


class FakeRepositoryClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def edit_repository(self, settings: dict) -> None:
        self.calls.append(("edit_repository", settings))

    def ensure_labels(self, labels: list[dict]) -> None:
        self.calls.append(("ensure_labels", labels))

    def ensure_ruleset(self, ruleset: dict) -> None:
        self.calls.append(("ensure_ruleset", ruleset))

    def configure_actions(self, policy: dict) -> None:
        self.calls.append(("configure_actions", policy))

    def enable_security_features(self) -> None:
        self.calls.append(("enable_security_features", None))

    def ensure_environment(self, environment: dict) -> None:
        self.calls.append(("ensure_environment", environment))


class RepositoryConfigurationTests(unittest.TestCase):
    def test_configuration_is_previewed_before_remote_writes(self) -> None:
        plan = build_repository_plan("monstermic/godot-ai-game-workflow")
        self.assertEqual(plan["repository"]["visibility"], "public")
        self.assertTrue(plan["repository"]["is_template"])
        self.assertIn("contract", plan["ruleset"]["required_checks"])
        self.assertIn("godot-quality", plan["ruleset"]["required_checks"])
        self.assertEqual(
            plan["project"]["statuses"],
            ["Intake", "Ready", "Active", "Review", "Playtest", "Blocked", "Done"],
        )
        self.assertIn("Work ID", plan["project"]["fields"])
        self.assertIn("Build", plan["project"]["fields"])
        self.assertEqual(plan["actions"]["default_workflow_permissions"], "read")
        self.assertEqual(plan["environment"]["name"], "production")

        client = FakeRepositoryClient()
        preview = configure_repository(
            "monstermic/godot-ai-game-workflow", client=client, apply=False
        )
        self.assertEqual(preview["status"], "dry_run")
        self.assertEqual(client.calls, [])
        applied = configure_repository(
            "monstermic/godot-ai-game-workflow", client=client, apply=True
        )
        self.assertEqual(applied["status"], "passed")
        self.assertEqual(
            [name for name, _ in client.calls],
            [
                "edit_repository",
                "ensure_labels",
                "configure_actions",
                "enable_security_features",
                "ensure_environment",
                "ensure_ruleset",
            ],
        )


if __name__ == "__main__":
    unittest.main()
