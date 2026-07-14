from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .core import WorkflowError, fingerprint
from .repository import REQUIRED_CHECKS


AI_MODE_CONFIRMATION = "ENABLE-AI-STAGING"
PROTECTED_GATES = [
    "independent_review",
    "staging_owner_approval",
    "production",
    "release",
    "rollback",
]
AUTO_APPROVED_OPERATIONS = [
    "creative_decisions",
    "project_file_writes",
    "allowlisted_shell",
    "tests",
    "git_branch",
    "git_commit",
    "pull_request_prepare",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _path(root: Path | str) -> Path:
    project = Path(root).resolve()
    path = project / ".aigame" / "automation.json"
    if not path.is_file():
        raise WorkflowError(f"Automation policy is missing: {path}")
    return path


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _schema_errors(value: dict[str, Any], schema_name: str) -> list[str]:
    schema = json.loads(files("aigame.schemas").joinpath(schema_name).read_text(encoding="utf-8"))
    return [
        f"{'.'.join(str(part) for part in error.path) or '<record>'}: {error.message}"
        for error in Draft202012Validator(schema).iter_errors(value)
    ]


def _validate_fingerprint(value: dict[str, Any]) -> list[str]:
    material = {key: item for key, item in value.items() if key != "input_fingerprint"}
    if value.get("input_fingerprint") != fingerprint(material):
        return ["input_fingerprint does not match current content"]
    return []


def _policy(mode: str, *, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    if mode not in {"human_gated", "ai_staging"}:
        raise WorkflowError("Automation mode must be human_gated or ai_staging")
    created_at = str((previous or {}).get("created_at") or _now())
    value = {
        "schema_version": "1.0",
        "id": "AUT-0001",
        "revision": int((previous or {}).get("revision", 0)) + 1,
        "status": "current",
        "created_at": created_at,
        "updated_at": _now(),
        "mode": mode,
        "decision_authority": "agent" if mode == "ai_staging" else "human",
        "permission_policy": (
            "auto_approve_project_operations" if mode == "ai_staging" else "ask"
        ),
        "auto_approved_operations": AUTO_APPROVED_OPERATIONS if mode == "ai_staging" else [],
        "integration_branch": "staging",
        "auto_merge_staging": mode == "ai_staging",
        "protected_gates": PROTECTED_GATES,
    }
    value["input_fingerprint"] = fingerprint(value)
    errors = _schema_errors(value, "automation-policy.schema.json")
    if errors:
        raise WorkflowError("Invalid automation policy: " + "; ".join(errors))
    return value


def default_automation_policy() -> dict[str, Any]:
    return _policy("human_gated")


def get_automation_policy(root: Path | str) -> dict[str, Any]:
    path = _path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Invalid automation policy at {path}: {error}") from error
    if not isinstance(value, dict):
        raise WorkflowError(f"Automation policy must be a JSON object: {path}")
    errors = _schema_errors(value, "automation-policy.schema.json") + _validate_fingerprint(value)
    if errors:
        raise WorkflowError("Invalid automation policy: " + "; ".join(errors))
    return value


def is_ai_staging(root: Path | str) -> bool:
    return get_automation_policy(root).get("mode") == "ai_staging"


def set_automation_mode(
    root: Path | str,
    mode: str,
    *,
    confirmation: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    path = _path(root)
    current = get_automation_policy(root)
    proposed = _policy(mode, previous=current)
    if not apply:
        return {
            "schema_version": "1.0",
            "status": "dry_run",
            "before": current,
            "policy": proposed,
            "required_confirmation": AI_MODE_CONFIRMATION if mode == "ai_staging" else None,
        }
    if mode == "ai_staging" and confirmation != AI_MODE_CONFIRMATION:
        raise WorkflowError(
            f"AI staging mode requires explicit confirmation {AI_MODE_CONFIRMATION!r}"
        )
    _write_json(path, proposed)
    return {"schema_version": "1.0", "status": "passed", "policy": proposed}


def _next_approval_id(project: Path) -> str:
    values = []
    for path in (project / "evidence" / "approvals").glob("APR-*.json"):
        match = re.fullmatch(r"APR-([0-9]{4})\.json", path.name)
        if match:
            values.append(int(match.group(1)))
    return f"APR-{max(values, default=0) + 1:04d}"


def create_agent_approval(root: Path | str, request: dict[str, Any]) -> dict[str, Any]:
    project = Path(root).resolve()
    if not is_ai_staging(project):
        raise WorkflowError("Agent approvals require ai_staging mode")
    now = _now()
    approval = {
        "schema_version": "1.0",
        "id": _next_approval_id(project),
        "revision": 1,
        "status": "approved",
        "created_at": now,
        "updated_at": now,
        "scope_hash": request["scope_hash"],
        "commit_sha": request["commit_sha"],
        "approver": "aigame-agent",
        "decision": request["decision"],
        "approved_at": now,
        "approval_kind": "agent",
        "automation_mode": "ai_staging",
    }
    approval["input_fingerprint"] = fingerprint(approval)
    errors = _schema_errors(approval, "approval.schema.json")
    if errors:
        raise WorkflowError("Invalid agent approval: " + "; ".join(errors))
    return approval


class GhStagingClient:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()

    def _run(self, *arguments: str) -> str:
        result = subprocess.run(
            ["gh", *arguments],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise WorkflowError(result.stderr.strip() or "GitHub CLI command failed")
        return result.stdout

    def ensure_staging_branch(self) -> dict[str, Any]:
        repository = json.loads(
            self._run("repo", "view", "--json", "nameWithOwner,defaultBranchRef")
        )
        name_with_owner = str(repository.get("nameWithOwner", ""))
        default_branch = str((repository.get("defaultBranchRef") or {}).get("name", ""))
        if not name_with_owner or not default_branch:
            raise WorkflowError("Could not discover the GitHub repository default branch")
        destination = f"repos/{name_with_owner}/git/ref/heads/staging"
        result = subprocess.run(
            ["gh", "api", destination],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            current = json.loads(result.stdout)
            return {
                "branch": "staging",
                "created": False,
                "sha": str((current.get("object") or {}).get("sha", "")),
            }
        if "404" not in result.stderr and "Not Found" not in result.stderr:
            raise WorkflowError(result.stderr.strip() or "Could not inspect staging branch")
        source = json.loads(
            self._run(
                "api",
                f"repos/{name_with_owner}/git/ref/heads/{default_branch}",
            )
        )
        source_sha = str((source.get("object") or {}).get("sha", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise WorkflowError("Default branch head SHA is invalid")
        created = json.loads(
            self._run(
                "api",
                "--method",
                "POST",
                f"repos/{name_with_owner}/git/refs",
                "-f",
                "ref=refs/heads/staging",
                "-f",
                f"sha={source_sha}",
            )
        )
        return {
            "branch": "staging",
            "created": True,
            "sha": str((created.get("object") or {}).get("sha", source_sha)),
        }

    def view_pull_request(self, number: int) -> dict[str, Any]:
        return json.loads(
            self._run(
                "pr",
                "view",
                str(number),
                "--json",
                "number,url,state,isDraft,baseRefName,headRefName,headRefOid,mergeStateStatus,reviewDecision,statusCheckRollup",
            )
        )

def initialize_staging(
    root: Path | str,
    *,
    client: Any | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    project = Path(root).resolve()
    policy = get_automation_policy(project)
    if policy.get("mode") != "ai_staging":
        raise WorkflowError("Staging initialization requires ai_staging mode")
    if not apply:
        return {
            "schema_version": "1.0",
            "status": "dry_run",
            "operation": "ensure_staging_branch",
            "branch": policy.get("integration_branch"),
        }
    github = client or GhStagingClient(project)
    result = github.ensure_staging_branch()
    return {"schema_version": "1.0", "status": "passed", **result}


def _staging_request(metadata: dict[str, Any]) -> dict[str, Any]:
    head_oid = str(metadata.get("headRefOid", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", head_oid):
        raise WorkflowError("Pull request head SHA is invalid")
    checks = sorted(
        (
            {
                "name": str(value.get("name") or value.get("context") or ""),
                "result": str(value.get("conclusion") or value.get("state") or "").upper(),
            }
            for value in metadata.get("statusCheckRollup", [])
        ),
        key=lambda value: (value["name"], value["result"]),
    )
    material = {
        "number": metadata.get("number"),
        "url": metadata.get("url"),
        "base": metadata.get("baseRefName"),
        "head": metadata.get("headRefName"),
        "head_oid": head_oid,
        "checks": checks,
        "review_decision": metadata.get("reviewDecision"),
    }
    return {
        "pull_request": int(metadata["number"]),
        "scope_hash": fingerprint(material),
        "commit_sha": head_oid,
        "decision": f"merge_to_staging:PR-{metadata.get('number')}",
    }


def prepare_staging_merge(
    root: Path | str,
    pull_request: int,
    *,
    client: Any | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    project = Path(root).resolve()
    policy = get_automation_policy(project)
    if policy.get("mode") != "ai_staging" or not policy.get("auto_merge_staging"):
        raise WorkflowError("Staging auto-merge requires ai_staging mode")
    github = client or GhStagingClient(project)
    metadata = github.view_pull_request(pull_request)
    if metadata.get("state") != "OPEN" or metadata.get("isDraft"):
        raise WorkflowError("Staging integration requires an open, non-draft pull request")
    if metadata.get("baseRefName") != policy.get("integration_branch"):
        raise WorkflowError("Pull request must target the configured staging branch")
    if metadata.get("mergeStateStatus") not in {"CLEAN", "HAS_HOOKS", "UNSTABLE"}:
        raise WorkflowError("Pull request is not mergeable against staging")
    checks = {
        str(value.get("name") or value.get("context")): str(
            value.get("conclusion") or value.get("state")
        ).upper()
        for value in metadata.get("statusCheckRollup", [])
    }
    missing_or_failed = [
        name for name in REQUIRED_CHECKS if checks.get(name) not in {"SUCCESS", "NEUTRAL"}
    ]
    if missing_or_failed:
        raise WorkflowError(
            "Required staging checks are missing or failing: " + ", ".join(missing_or_failed)
        )
    request = _staging_request(metadata)
    approval_comment = (
        "/aigame merge staging "
        f"head={request['commit_sha']} scope={request['scope_hash']}"
    )
    return {
        "schema_version": "1.0",
        "status": "needs_human",
        "gate": "github_owner_comment",
        "pull_request": pull_request,
        "head_sha": metadata["headRefOid"],
        "scope_hash": request["scope_hash"],
        "approval_comment": approval_comment,
        "approval_location": metadata.get("url"),
        "automatic_action": "none",
        "message": (
            "The repository owner must post approval_comment on this pull request. "
            "The authenticated issue-comment workflow will revalidate and merge it."
        ),
    }
