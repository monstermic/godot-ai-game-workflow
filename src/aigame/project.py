from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


STATUSES = ["Intake", "Ready", "Active", "Review", "Playtest", "Blocked", "Done"]


def project_title(repository: str) -> str:
    return f"{repository.split('/', 1)[-1]} - AI Game Workflow"


def project_plan(repository: str) -> dict[str, Any]:
    owner = repository.split("/", 1)[0]
    return {
        "schema_version": "1.0",
        "status": "preview",
        "owner": owner,
        "repository": repository,
        "title": project_title(repository),
        "statuses": STATUSES,
        "fields": {
            "Workflow Status": {"type": "SINGLE_SELECT", "options": STATUSES},
            "Work ID": {"type": "TEXT"},
            "Type": {"type": "SINGLE_SELECT", "options": ["Discovery", "Experiment", "Feature", "Bug", "Content", "Release"]},
            "Milestone": {"type": "TEXT"},
            "Phase": {"type": "SINGLE_SELECT", "options": ["Bootstrap", "Discovery", "Experiment", "First Playable", "Vertical Slice", "Production", "Alpha", "Beta", "Release Candidate", "Post-release"]},
            "Risk": {"type": "NUMBER"},
            "Capability": {"type": "TEXT"},
            "Priority": {"type": "NUMBER"},
            "Playtest Required": {"type": "SINGLE_SELECT", "options": ["Yes", "No"]},
            "Agent": {"type": "TEXT"},
            "Build": {"type": "TEXT"},
        },
    }


class GhProjectClient:
    def __init__(self, owner: str, *, cwd: Path | str = Path.cwd()):
        self.owner = owner
        self.cwd = Path(cwd)

    def _run(self, *arguments: str) -> Any:
        result = subprocess.run(
            ["gh", *arguments], cwd=self.cwd, text=True, capture_output=True, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "GitHub Project command failed")
        return json.loads(result.stdout) if result.stdout.strip().startswith(("{", "[")) else result.stdout.strip()

    def ensure_project(self, title: str, repository: str) -> int:
        listing = self._run("project", "list", "--owner", self.owner, "--format", "json")
        projects = listing.get("projects", listing if isinstance(listing, list) else [])
        existing = next((project for project in projects if project.get("title") == title), None)
        if existing:
            number = int(existing["number"])
        else:
            created = self._run(
                "project", "create", "--owner", self.owner, "--title", title, "--format", "json"
            )
            number = int(created["number"])
        self._run("project", "link", str(number), "--owner", self.owner, "--repo", repository)
        return number

    def ensure_fields(self, number: int, fields: dict[str, dict[str, Any]]) -> None:
        listing = self._run(
            "project", "field-list", str(number), "--owner", self.owner, "--format", "json"
        )
        current = listing.get("fields", listing if isinstance(listing, list) else [])
        names = {field.get("name") for field in current}
        for name, definition in fields.items():
            if name in names:
                continue
            command = [
                "project", "field-create", str(number), "--owner", self.owner,
                "--name", name, "--data-type", definition["type"], "--format", "json",
            ]
            options = definition.get("options")
            if options:
                command.extend(["--single-select-options", ",".join(options)])
            self._run(*command)


def configure_project(
    repository: str,
    *,
    client: Any | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    plan = project_plan(repository)
    if not apply:
        return {**plan, "status": "dry_run"}
    github = client or GhProjectClient(plan["owner"])
    number = github.ensure_project(plan["title"], repository)
    github.ensure_fields(number, plan["fields"])
    return {
        "schema_version": "1.0",
        "status": "passed",
        "repository": repository,
        "project_number": number,
    }
