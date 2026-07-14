from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .core import fingerprint
from .core import transition


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=root, text=True, capture_output=True, check=False
    )


def claim_work_item(
    root: Path | str, work_item_id: str, *, apply: bool = False
) -> dict[str, Any]:
    project = Path(root).resolve()
    state_path = project / ".aigame" / "state" / "current.json"
    item_path = project / "work" / "items" / f"{work_item_id}.json"
    if not state_path.is_file() or not item_path.is_file():
        raise FileNotFoundError(f"Cannot claim unknown work item {work_item_id}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    active = state.get("active_work_item")
    if active and active != work_item_id:
        raise RuntimeError(f"Another work item is active: {active}")
    item = json.loads(item_path.read_text(encoding="utf-8"))
    updated = transition(item, "claimed") if item.get("status") == "ready" else item
    slug = re.sub(r"[^a-z0-9]+", "-", str(item.get("title", "work")).lower()).strip("-")
    branch = f"ai/{work_item_id}-{slug}"
    if not apply:
        return {
            "schema_version": "1.0",
            "status": "dry_run",
            "work_item_id": work_item_id,
            "branch": branch,
        }
    if _git(project, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise RuntimeError("Claiming a work item requires an initialized Git repository")
    existing = _git(project, "show-ref", "--verify", f"refs/heads/{branch}")
    checkout = _git(project, "checkout", branch if existing.returncode == 0 else "-b", *(()) if existing.returncode == 0 else (branch,))
    if checkout.returncode != 0:
        raise RuntimeError(checkout.stderr.strip() or "Unable to create work branch")
    item_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    state.update(
        {
            "active_work_item": work_item_id,
            "active_branch": branch,
            "last_checkpoint": None,
        }
    )
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return {
        "schema_version": "1.0",
        "status": "passed",
        "work_item_id": work_item_id,
        "branch": branch,
    }


def checkpoint(
    root: Path | str,
    work_item_id: str,
    result_path: Path | str,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    project = Path(root).resolve()
    result_file = Path(result_path).resolve()
    run_result = json.loads(result_file.read_text(encoding="utf-8"))
    schema = json.loads(
        files("aigame.schemas").joinpath("run-result.schema.json").read_text(encoding="utf-8")
    )
    schema_errors = list(Draft202012Validator(schema).iter_errors(run_result))
    if schema_errors:
        details = "; ".join(error.message for error in schema_errors)
        raise ValueError(f"RunResult does not match the public schema: {details}")
    if run_result.get("work_item_id") != work_item_id:
        raise ValueError("Run result work_item_id does not match the checkpoint target")
    destination = project / "evidence" / work_item_id / "checkpoint.json"
    payload = {
        "schema_version": "1.0",
        "work_item_id": work_item_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "run_result_sha256": fingerprint(run_result),
        "run_result": run_result,
    }
    if not apply:
        return {"schema_version": "1.0", "status": "dry_run", "path": str(destination), "payload": payload}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {"schema_version": "1.0", "status": "passed", "path": str(destination)}
