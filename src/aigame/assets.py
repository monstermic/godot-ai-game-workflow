from __future__ import annotations

import json
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Any, Sequence

from jsonschema import Draft202012Validator

from .core import fingerprint


PROVENANCE_FIELDS = {
    "provider",
    "provider_version",
    "model_id",
    "model_checksum",
    "model_license",
    "workflow_checksum",
    "prompt_sha256",
    "seed",
}


def _validate_contract(value: dict[str, Any], schema_name: str, title: str) -> None:
    schema = json.loads(files("aigame.schemas").joinpath(schema_name).read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        details = "; ".join(error.message for error in errors)
        raise ValueError(f"{title} does not match the public schema: {details}")


def generate_asset(
    brief: dict[str, Any], adapter: Sequence[str] | None
) -> dict[str, Any]:
    _validate_contract(brief, "asset-brief.schema.json", "AssetBrief")
    def placeholder(reason: str | None = None) -> dict[str, Any]:
        result = {
            "schema_version": "1.0",
            "status": "placeholder",
            "asset_id": brief["id"],
            "kind": brief["kind"],
            "runtime_path": brief["runtime_path"],
            "license": "CC0-1.0",
            "provenance": {
                "generated": False,
                "method": "procedural-placeholder",
                "brief_sha256": fingerprint(brief),
            },
        }
        if reason:
            result["provenance"]["adapter_failure"] = reason
        _validate_contract(
            result, "asset-generation-result.schema.json", "AssetGenerationResult"
        )
        return result
    if not adapter:
        return placeholder()
    try:
        completed = subprocess.run(
            list(adapter),
            input=json.dumps(brief),
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return placeholder(str(error))
    if completed.returncode != 0:
        return placeholder(completed.stderr.strip() or "Asset adapter failed")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"Asset adapter returned invalid JSON: {error}") from error
    if result.get("status") == "generated":
        provenance = result.get("provenance", {})
        missing = sorted(field for field in PROVENANCE_FIELDS if provenance.get(field) is None)
        if missing:
            raise ValueError(f"Generated asset provenance is incomplete: {missing}")
    if result.get("asset_id") != brief.get("id"):
        raise ValueError("Asset adapter result does not match the brief id")
    _validate_contract(result, "asset-generation-result.schema.json", "AssetGenerationResult")
    return result


def build_lfs_plan(root: Path | str) -> dict[str, Any]:
    project = Path(root)
    patterns = [
        "assets/source/**/*.psd",
        "assets/source/**/*.kra",
        "assets/source/**/*.blend",
        "assets/source/**/*.fbx",
        "assets/source/**/*.wav",
        "assets/source/**/*.flac",
        "assets/source/**/*.mp4",
    ]
    return {
        "schema_version": "1.0",
        "status": "dry_run",
        "root": str(project),
        "patterns": patterns,
        "commands": [["git", "lfs", "track", pattern] for pattern in patterns],
    }
