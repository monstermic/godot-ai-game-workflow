from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


REQUIRED_CHECKS = [
    "contract / contract",
    "godot-quality / godot-quality",
    "asset-provenance / asset-provenance",
    "independent-review / independent-review",
]
STAGING_APPROVAL_CHECK = "aigame-staging-owner-approval"
STAGING_REQUIRED_CHECKS = [*REQUIRED_CHECKS, STAGING_APPROVAL_CHECK]
GITHUB_ACTIONS_APP_ID = 15368
REUSABLE_WORKFLOW_PATTERN = (
    "monstermic/godot-ai-game-workflow/.github/workflows/*@*"
)


def build_repository_plan(repository: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "preview",
        "repository_name": repository,
        "repository": {
            "visibility": "public",
            "is_template": True,
            "has_issues": True,
            "has_projects": True,
            "has_discussions": True,
            "has_wiki": False,
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "allow_auto_merge": True,
            "delete_branch_on_merge": True,
        },
        "labels": [
            {"name": "status:intake", "color": "d4c5f9"},
            {"name": "status:ready", "color": "0e8a16"},
            {"name": "status:active", "color": "fbca04"},
            {"name": "status:review", "color": "1d76db"},
            {"name": "status:playtest", "color": "5319e7"},
            {"name": "status:blocked", "color": "b60205"},
            {"name": "status:done", "color": "006b75"},
            {"name": "type:discovery", "color": "c5def5"},
            {"name": "type:feature", "color": "a2eeef"},
            {"name": "type:bug", "color": "d73a4a"},
            {"name": "review:required", "color": "e99695"},
        ],
        "project": {
            "title": f"{repository.split('/', 1)[-1]} - AI Game Workflow",
            "statuses": ["Intake", "Ready", "Active", "Review", "Playtest", "Blocked", "Done"],
            "fields": {
                "Work ID": "TEXT",
                "Type": "SINGLE_SELECT",
                "Milestone": "TEXT",
                "Phase": "SINGLE_SELECT",
                "Risk": "NUMBER",
                "Capability": "TEXT",
                "Priority": "NUMBER",
                "Playtest Required": "SINGLE_SELECT",
                "Agent": "TEXT",
                "Build": "TEXT",
            },
        },
        "actions": {
            "enabled": True,
            "allowed_actions": "selected",
            "github_owned_allowed": True,
            "verified_allowed": False,
            "patterns_allowed": [REUSABLE_WORKFLOW_PATTERN],
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        },
        "security": {
            "vulnerability_alerts": True,
            "automated_security_fixes": True,
        },
        "environment": {
            "name": "production",
            "required_reviewer": repository.split("/", 1)[0],
            "prevent_self_review": False,
        },
        "ruleset": {
            "name": "main-protection",
            "target": "branch",
            "enforcement": "active",
            "required_checks": REQUIRED_CHECKS,
            "require_pull_request": True,
            "block_deletion": True,
            "block_force_push": True,
        },
        "staging_ruleset": {
            "name": "staging-protection",
            "target": "branch",
            "enforcement": "active",
            "include": ["refs/heads/staging"],
            "allow_branch_creation": True,
            "required_checks": STAGING_REQUIRED_CHECKS,
            "required_check_integrations": {
                STAGING_APPROVAL_CHECK: GITHUB_ACTIONS_APP_ID,
            },
            "require_pull_request": True,
            "block_deletion": True,
            "block_force_push": True,
        },
    }


def build_game_repository_plan(repository: str) -> dict[str, Any]:
    plan = build_repository_plan(repository)
    plan["repository"] = {
        key: value
        for key, value in plan["repository"].items()
        if key not in {"visibility", "is_template"}
    }
    plan["repository_name"] = repository
    return plan


class GhRepositoryClient:
    def __init__(self, repository: str, *, cwd: Path | str = Path.cwd()):
        self.repository = repository
        self.cwd = Path(cwd)

    def _api(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        command = ["gh", "api", "-X", method, endpoint]
        if payload is not None:
            command.extend(["--input", "-"])
        result = subprocess.run(
            command,
            cwd=self.cwd,
            input=json.dumps(payload) if payload is not None else None,
            text=True,
            capture_output=True,
            check=False,
            env=None,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"GitHub API failed: {endpoint}")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def edit_repository(self, settings: dict[str, Any]) -> None:
        self._api("PATCH", f"repos/{self.repository}", settings)

    def ensure_labels(self, labels: list[dict[str, str]]) -> None:
        current = self._api("GET", f"repos/{self.repository}/labels?per_page=100") or []
        names = {label["name"] for label in current}
        for label in labels:
            if label["name"] in names:
                self._api("PATCH", f"repos/{self.repository}/labels/{label['name']}", label)
            else:
                self._api("POST", f"repos/{self.repository}/labels", label)

    def ensure_ruleset(self, ruleset: dict[str, Any]) -> None:
        current = self._api("GET", f"repos/{self.repository}/rulesets") or []
        existing = next((value for value in current if value.get("name") == ruleset["name"]), None)
        payload = {
            "name": ruleset["name"],
            "target": "branch",
            "enforcement": "active",
            "conditions": {
                "ref_name": {
                    "include": ruleset.get("include", ["~DEFAULT_BRANCH"]),
                    "exclude": [],
                }
            },
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {
                    "type": "pull_request",
                    "parameters": {
                        "dismiss_stale_reviews_on_push": True,
                        "require_code_owner_review": False,
                        "require_last_push_approval": False,
                        "required_approving_review_count": 0,
                        "required_review_thread_resolution": True,
                    },
                },
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "do_not_enforce_on_create": bool(
                            ruleset.get("allow_branch_creation", False)
                        ),
                        "required_status_checks": [
                            {
                                "context": check,
                                **(
                                    {"integration_id": integration_id}
                                    if (
                                        integration_id := ruleset.get(
                                            "required_check_integrations", {}
                                        ).get(check)
                                    )
                                    else {}
                                ),
                            }
                            for check in ruleset["required_checks"]
                        ],
                    },
                },
            ],
        }
        if existing:
            self._api("PUT", f"repos/{self.repository}/rulesets/{existing['id']}", payload)
        else:
            self._api("POST", f"repos/{self.repository}/rulesets", payload)

    def configure_actions(self, policy: dict[str, Any]) -> None:
        self._api(
            "PUT",
            f"repos/{self.repository}/actions/permissions",
            {"enabled": policy["enabled"], "allowed_actions": policy["allowed_actions"]},
        )
        self._api(
            "PUT",
            f"repos/{self.repository}/actions/permissions/selected-actions",
            {
                "github_owned_allowed": policy["github_owned_allowed"],
                "verified_allowed": policy["verified_allowed"],
                "patterns_allowed": policy["patterns_allowed"],
            },
        )
        self._api(
            "PUT",
            f"repos/{self.repository}/actions/permissions/workflow",
            {
                "default_workflow_permissions": policy["default_workflow_permissions"],
                "can_approve_pull_request_reviews": policy[
                    "can_approve_pull_request_reviews"
                ],
            },
        )

    def enable_security_features(self) -> None:
        self._api("PUT", f"repos/{self.repository}/vulnerability-alerts")
        self._api("PUT", f"repos/{self.repository}/automated-security-fixes")

    def ensure_environment(self, environment: dict[str, Any]) -> None:
        user = self._api("GET", f"users/{environment['required_reviewer']}")
        self._api(
            "PUT",
            f"repos/{self.repository}/environments/{environment['name']}",
            {
                "prevent_self_review": environment["prevent_self_review"],
                "reviewers": [{"type": "User", "id": user["id"]}],
                "deployment_branch_policy": None,
            },
        )


def configure_repository(
    repository: str,
    *,
    client: Any | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    plan = build_repository_plan(repository)
    if not apply:
        return {**plan, "status": "dry_run"}
    github = client or GhRepositoryClient(repository)
    github.edit_repository(plan["repository"])
    github.ensure_labels(plan["labels"])
    github.configure_actions(plan["actions"])
    github.enable_security_features()
    github.ensure_environment(plan["environment"])
    github.ensure_ruleset(plan["ruleset"])
    github.ensure_ruleset(plan["staging_ruleset"])
    return {
        "schema_version": "1.0",
        "status": "passed",
        "repository_name": repository,
        "required_checks": plan["ruleset"]["required_checks"],
        "staging_required_checks": plan["staging_ruleset"]["required_checks"],
    }


def configure_game_repository(
    repository: str,
    *,
    client: Any | None = None,
    cwd: Path | str = Path.cwd(),
    apply: bool = False,
) -> dict[str, Any]:
    plan = build_game_repository_plan(repository)
    if not apply:
        return {**plan, "status": "dry_run"}
    github = client or GhRepositoryClient(repository, cwd=cwd)
    github.edit_repository(plan["repository"])
    github.ensure_labels(plan["labels"])
    github.configure_actions(plan["actions"])
    github.enable_security_features()
    github.ensure_environment(plan["environment"])
    github.ensure_ruleset(plan["ruleset"])
    github.ensure_ruleset(plan["staging_ruleset"])
    return {
        "schema_version": "1.0",
        "status": "passed",
        "repository_name": repository,
        "required_checks": plan["ruleset"]["required_checks"],
        "staging_required_checks": plan["staging_ruleset"]["required_checks"],
        "environments": ["production"],
    }
