from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .core import fingerprint


class ApprovalRequired(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _commit_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "UNCOMMITTED"


def prepare_release_candidate(
    root: Path | str,
    artifact: Path | str,
    name: str,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    project = Path(root).resolve()
    artifact_path = Path(artifact).resolve()
    if not re.fullmatch(r"v\d+\.\d+\.\d+-rc\.\d+", name):
        raise ValueError("Release candidate name must match vMAJOR.MINOR.PATCH-rc.NUMBER")
    if not artifact_path.is_file():
        raise FileNotFoundError(artifact_path)
    artifact_sha = _sha256(artifact_path)
    commit_sha = _commit_sha(project)
    scope = {
        "name": name,
        "commit_sha": commit_sha,
        "artifact": artifact_path.name,
        "artifact_sha256": artifact_sha,
    }
    scope_hash = fingerprint(scope)
    manifest_path = project / "evidence" / "releases" / name / "release-metadata.json"
    manifest = {
        "schema_version": "1.0",
        **scope,
        "scope_hash": scope_hash,
        "immutable": True,
    }
    if apply:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {
        "schema_version": "1.0",
        "status": "passed" if apply else "dry_run",
        **manifest,
        "manifest_path": str(manifest_path),
    }


def promote_release(
    root: Path | str,
    manifest_path: Path | str,
    approval_path: Path | str | None,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    project = Path(root).resolve()
    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if approval_path is None:
        raise ApprovalRequired("Release promotion requires a checksum-bound human approval")
    approval = json.loads(Path(approval_path).read_text(encoding="utf-8"))
    if approval.get("status") != "approved":
        raise ApprovalRequired("Approval is not approved")
    if approval.get("scope_hash") != manifest.get("scope_hash"):
        raise ApprovalRequired("Approval scope does not match the release candidate")
    if approval.get("commit_sha") != manifest.get("commit_sha"):
        raise ApprovalRequired("Approval commit does not match the release candidate")
    if _commit_sha(project) != manifest.get("commit_sha"):
        raise ApprovalRequired("The project commit changed after release-candidate approval")
    if apply:
        dispatch = subprocess.run(
            [
                "gh",
                "workflow",
                "run",
                "release.yml",
                "-f",
                f"name={manifest['name']}",
                "-f",
                f"artifact-sha256={manifest['artifact_sha256']}",
            ],
            cwd=project,
            text=True,
            capture_output=True,
            check=False,
        )
        if dispatch.returncode != 0:
            raise RuntimeError(dispatch.stderr.strip() or "Unable to dispatch protected release")
    return {
        "schema_version": "1.0",
        "status": "passed" if apply else "needs_human",
        "name": manifest["name"],
        "artifact_sha256": manifest["artifact_sha256"],
        "scope_hash": manifest["scope_hash"],
        "action": "protected_release_dispatched" if apply else "approval_verified",
    }
