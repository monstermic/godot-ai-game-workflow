from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


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
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Workflow commit must be a full 40-character SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("Workflow source checksum must be SHA-256")
    lock_path = Path(root).resolve() / ".aigame" / "workflow.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    before = lock.copy()
    lock.update(
        {
            "workflow_version": version,
            "source_commit": commit,
            "source_sha256": source_sha256,
        }
    )
    if apply:
        lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return {
        "schema_version": "1.0",
        "status": "passed" if apply else "dry_run",
        "path": str(lock_path),
        "before": before,
        "after": lock,
    }
