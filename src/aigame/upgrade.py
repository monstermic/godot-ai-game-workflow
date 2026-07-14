from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import __version__
from .automation import default_automation_policy
from .core import workflow_snapshot_checksum


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8", newline="\n")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_text(path, json.dumps(value, indent=2, ensure_ascii=False))


def _staging_caller(commit: str) -> str:
    return f'''name: Staging Merge Approval

on:
  issue_comment:
    types: [created]

permissions:
  contents: write
  pull-requests: write
  statuses: write

jobs:
  merge:
    if: github.event.issue.pull_request && startsWith(github.event.comment.body, '/aigame merge staging ')
    uses: monstermic/godot-ai-game-workflow/.github/workflows/staging-merge.yml@{commit}
    with:
      pull-request: ${{{{ github.event.issue.number }}}}
      actor: ${{{{ github.actor }}}}
      comment-body: ${{{{ github.event.comment.body }}}}
'''


def _install_plan(project: Path, source_root: Path) -> list[str]:
    paths = [
        ".aigame/automation.json",
        ".aigame/state/concept.json",
        ".aigame/agent-skills/build-game-concept/SKILL.md",
        ".github/workflows/staging-merge.yml",
    ]
    for source in sorted(source_root.glob("*.py")):
        paths.append(f".aigame/vendor/aigame/{source.name}")
    for folder in ("schemas", "profiles"):
        for source in sorted((source_root / folder).glob("*")):
            if source.is_file() and (source.suffix in {".json", ".py"}):
                paths.append(f".aigame/{folder}/{source.name}")
                paths.append(f".aigame/vendor/aigame/{folder}/{source.name}")
    return sorted(set(paths))


def upgrade_workflow(
    root: Path | str,
    version: str,
    commit: str,
    source_sha256: str,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise ValueError("Workflow version must be semantic version syntax")
    if version != __version__:
        raise ValueError(
            f"Installed aigame is {__version__}; it cannot install workflow snapshot {version}"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Workflow commit must be a full 40-character SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("Workflow source checksum must be SHA-256")

    project = Path(root).resolve()
    source_root = Path(__file__).resolve().parent
    actual_source_checksum = workflow_snapshot_checksum(source_root)
    if source_sha256 != actual_source_checksum:
        raise ValueError(
            "Requested source checksum does not match the installed workflow snapshot"
        )
    lock_path = project / ".aigame" / "workflow.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    before = lock.copy()
    lock.update(
        {
            "workflow_version": version,
            "source_commit": commit,
            "source_sha256": source_sha256,
        }
    )
    install = _install_plan(project, source_root)
    if not apply:
        return {
            "schema_version": "1.0",
            "status": "dry_run",
            "path": str(lock_path),
            "before": before,
            "after": lock,
            "install": install,
            "preserved": ["docs/**", "work/**", "assets/**"],
        }

    vendor_root = project / ".aigame" / "vendor" / "aigame"
    for source in sorted(source_root.glob("*.py")):
        _write_text(vendor_root / source.name, source.read_text(encoding="utf-8"))
    for folder in ("schemas", "profiles"):
        source_folder = source_root / folder
        for source in sorted(source_folder.glob("*")):
            if not source.is_file() or source.suffix not in {".json", ".py"}:
                continue
            content = source.read_text(encoding="utf-8")
            _write_text(project / ".aigame" / folder / source.name, content)
            _write_text(vendor_root / folder / source.name, content)

    automation_path = project / ".aigame" / "automation.json"
    if not automation_path.is_file():
        _write_json(automation_path, default_automation_policy())

    concept_state_path = project / ".aigame" / "state" / "concept.json"
    if not concept_state_path.is_file():
        _write_json(
            concept_state_path,
            {
                "schema_version": "1.0",
                "revision": 1,
                "status": "not_started",
                "active_task_id": None,
                "next_task_number": 1,
                "intake_id": None,
                "selected_pitch_id": None,
                "direction_approval_id": None,
                "product_identity_id": None,
                "product_identity_approval_id": None,
                "blueprint_approval_id": None,
                "pending_approval": None,
                "pending_approval_options": None,
                "history": [],
            },
        )

    concept_readme = project / "work" / "concept" / "README.md"
    if not concept_readme.is_file():
        _write_text(
            concept_readme,
            "# Concept records\n\nCanonical JSON records are managed through `aigame concept`.",
        )
    blueprint_readme = project / "docs" / "blueprint" / "README.md"
    if not blueprint_readme.is_file():
        _write_text(
            blueprint_readme,
            "# Generated blueprint\n\nRendered from canonical `work/concept/` records.",
        )
    skill_path = project / ".aigame" / "agent-skills" / "build-game-concept" / "SKILL.md"
    if not skill_path.is_file():
        _write_text(
            skill_path,
            """---
name: build-game-concept
description: Build or resume the complete portable game blueprint before implementation.
---

# Build Game Concept

Read `AGENTS.md` and `.aigame/automation.json`. Complete one schema-valid `aigame concept`
task at a time. Human-gated mode stops for approval; AI-staging mode records agent decisions
and continues. Commit canonical concept records at `commit_blueprint_inputs` before finalization.
Only the authenticated owner may post the staging approval comment. Never bypass
independent review, production, release, or rollback.
""",
        )

    config_path = project / ".aigame" / "project.toml"
    config = config_path.read_text(encoding="utf-8")
    config = re.sub(
        r'^workflow_version = "[^"]+"$',
        f'workflow_version = "{version}"',
        config,
        flags=re.MULTILINE,
    )
    _write_text(config_path, config)

    workflows = project / ".github" / "workflows"
    workflow_reference = re.compile(
        r"(monstermic/godot-ai-game-workflow/\.github/workflows/[^@\s]+@)[0-9a-f]{40}"
    )
    for workflow in sorted(workflows.glob("*.yml")):
        content = workflow.read_text(encoding="utf-8")
        _write_text(workflow, workflow_reference.sub(rf"\g<1>{commit}", content))
    _write_text(workflows / "staging-merge.yml", _staging_caller(commit))

    agents_path = project / "AGENTS.md"
    agents = agents_path.read_text(encoding="utf-8")
    marker = "## Workflow automation mode"
    if marker not in agents:
        agents += f'''\n\n{marker}

Read `.aigame/automation.json`. Human-gated mode stops at approval gates. AI-staging mode records
agent concept decisions and targets `staging`. Run `aigame staging merge` to obtain the exact
approval comment, then stop for the authenticated repository owner to post it. Never bypass
independent review, production, release, or rollback.
'''
        _write_text(agents_path, agents)

    installed_checksum = workflow_snapshot_checksum(vendor_root)
    if installed_checksum != source_sha256:
        raise RuntimeError("Installed workflow snapshot checksum does not match the requested source")
    _write_json(lock_path, lock)
    return {
        "schema_version": "1.0",
        "status": "passed",
        "path": str(lock_path),
        "before": before,
        "after": lock,
        "install": install,
        "preserved": ["docs/**", "work/**", "assets/**"],
    }
