from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .core import fingerprint
from .project import project_title


MARKER_PATTERN = re.compile(r"<!-- aigame:(WI-\d{4}):([0-9a-f]{64}) -->")
PROJECT_STATUS = {
    "draft": "Intake",
    "ready": "Ready",
    "claimed": "Active",
    "implementing": "Active",
    "validating": "Active",
    "review": "Review",
    "playtest": "Playtest",
    "blocked": "Blocked",
    "rework": "Ready",
    "done": "Done",
    "cancelled": "Done",
}


class GhClient:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()

    def _run(self, *arguments: str) -> str:
        result = subprocess.run(
            ["gh", *arguments], cwd=self.root, text=True, capture_output=True, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "GitHub CLI command failed")
        return result.stdout

    def list_issues(self) -> list[dict[str, Any]]:
        output = self._run(
            "issue", "list", "--state", "all", "--limit", "1000", "--json", "number,title,body,labels,url"
        )
        return json.loads(output)

    def ensure_labels(self, labels: list[str]) -> None:
        existing = json.loads(self._run("label", "list", "--limit", "1000", "--json", "name"))
        names = {label["name"] for label in existing}
        for label in labels:
            if label not in names:
                self._run("label", "create", label, "--color", "6f42c1", "--description", "Managed by aigame")

    def create_issue(self, issue: dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
            handle.write(issue["body"])
            body_path = handle.name
        try:
            arguments = ["issue", "create", "--title", issue["title"], "--body-file", body_path]
            for label in issue.get("labels", []):
                arguments.extend(["--label", label])
            self._run(*arguments)
        finally:
            Path(body_path).unlink(missing_ok=True)

    def update_issue(self, number: int, issue: dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
            handle.write(issue["body"])
            body_path = handle.name
        try:
            arguments = ["issue", "edit", str(number), "--title", issue["title"], "--body-file", body_path]
            for label in issue.get("labels", []):
                arguments.extend(["--add-label", label])
            self._run(*arguments)
        finally:
            Path(body_path).unlink(missing_ok=True)

    def sync_project(self, issues: list[dict[str, Any]], plan: dict[str, Any]) -> None:
        repository = json.loads(self._run("repo", "view", "--json", "nameWithOwner"))[
            "nameWithOwner"
        ]
        owner = repository.split("/", 1)[0]
        project_listing = json.loads(
            self._run("project", "list", "--owner", owner, "--format", "json")
        )
        project = next(
            (
                value
                for value in project_listing.get("projects", [])
                if value.get("title") == project_title(repository)
            ),
            None,
        )
        if project is None:
            raise RuntimeError(f"GitHub Project {project_title(repository)!r} is not configured")
        number = str(project["number"])
        project_id = str(project["id"])
        field_listing = json.loads(
            self._run("project", "field-list", number, "--owner", owner, "--format", "json")
        )
        fields = {field.get("name"): field for field in field_listing.get("fields", [])}
        item_listing = json.loads(
            self._run("project", "item-list", number, "--owner", owner, "--limit", "1000", "--format", "json")
        )
        project_items = {
            item.get("content", {}).get("url"): item
            for item in item_listing.get("items", [])
            if item.get("content", {}).get("url")
        }
        canonical = {entry["work_item_id"]: entry for entry in plan["issues"]}
        for issue in issues:
            marker = _marker(str(issue.get("body", "")))
            if marker is None or marker[0] not in canonical:
                continue
            issue_url = str(issue.get("url", ""))
            project_item = project_items.get(issue_url)
            if project_item is None:
                project_item = json.loads(
                    self._run(
                        "project", "item-add", number, "--owner", owner,
                        "--url", issue_url, "--format", "json",
                    )
                )
            values = canonical[marker[0]]["project_fields"]
            for field_name, value in values.items():
                field = fields.get(field_name)
                if field is None or value in (None, ""):
                    continue
                command = [
                    "project", "item-edit", "--id", str(project_item["id"]),
                    "--project-id", project_id, "--field-id", str(field["id"]),
                ]
                options = field.get("options") or []
                if options:
                    option = next(
                        (
                            option
                            for option in options
                            if str(option.get("name", "")).casefold()
                            == str(value).casefold()
                        ),
                        None,
                    )
                    if option is None:
                        continue
                    command.extend(["--single-select-option-id", str(option["id"])])
                elif isinstance(value, (int, float)):
                    command.extend(["--number", str(value)])
                else:
                    command.extend(["--text", str(value)])
                self._run(*command)


def build_sync_plan(root: Path | str) -> dict[str, Any]:
    project = Path(root).resolve()
    issues = []
    for path in sorted((project / "work" / "items").glob("WI-*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        checksum = fingerprint(item)
        marker = f"<!-- aigame:{item['id']}:{checksum} -->"
        body = "\n".join(
            [
                marker,
                f"Work item: `{item['id']}`",
                f"Milestone: `{item.get('milestone', 'unassigned')}`",
                f"Risk: `{item.get('risk', 0)}`",
                "",
                "This issue mirrors canonical repository data. Edit intent through a pull request.",
            ]
        )
        issues.append(
            {
                "work_item_id": item["id"],
                "title": f"[{item['id']}] {item.get('title', item['id'])}",
                "body": body,
                "labels": [
                    f"status:{item.get('status', 'draft')}",
                    f"type:{item.get('type', 'task')}",
                    f"risk:{item.get('risk', 0)}",
                ],
                "checksum": checksum,
                "project_fields": {
                    "Workflow Status": PROJECT_STATUS.get(item.get("status"), "Intake"),
                    "Work ID": item["id"],
                    "Type": str(item.get("type", "feature")).replace("_", " ").title(),
                    "Milestone": item.get("milestone", ""),
                    "Phase": str(item.get("milestone", "")).replace("_", " ").title(),
                    "Risk": item.get("risk", 0),
                    "Capability": ", ".join(item.get("required_capabilities", [])),
                    "Priority": item.get("priority", item.get("player_value", 0)),
                    "Playtest Required": "Yes" if item.get("playtest_required") else "No",
                    "Agent": item.get("agent", ""),
                    "Build": item.get("build", ""),
                },
            }
        )
    return {"schema_version": "1.0", "status": "preview", "issues": issues}


def intake_to_proposal(title: str, body: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "draft",
        "source": "github_intake",
        "title": title[:200],
        "body": body,
        "input_fingerprint": fingerprint({"title": title, "body": body}),
    }


def _marker(body: str) -> tuple[str, str] | None:
    match = MARKER_PATTERN.search(body or "")
    return (match.group(1), match.group(2)) if match else None


def sync_github(
    root: Path | str,
    *,
    client: Any | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    plan = build_sync_plan(root)
    if not apply:
        return plan
    github = client or GhClient(root)
    existing = github.list_issues()
    by_work_item = {
        marker[0]: issue
        for issue in existing
        if (marker := _marker(str(issue.get("body", "")))) is not None
    }
    labels = sorted({label for issue in plan["issues"] for label in issue.get("labels", [])})
    github.ensure_labels(labels)
    created = 0
    updated = 0
    unchanged = 0
    for issue in plan["issues"]:
        current = by_work_item.get(issue["work_item_id"])
        if current is None:
            github.create_issue(issue)
            created += 1
            continue
        current_marker = _marker(str(current.get("body", "")))
        if current_marker and current_marker[1] == issue["checksum"]:
            unchanged += 1
            continue
        github.update_issue(int(current["number"]), issue)
        updated += 1
    refreshed = github.list_issues()
    github.sync_project(refreshed, plan)
    return {
        "schema_version": "1.0",
        "status": "passed",
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "project_synced": True,
    }
