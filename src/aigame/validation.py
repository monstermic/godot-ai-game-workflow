from __future__ import annotations

import json
import re
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .core import fingerprint, workflow_snapshot_checksum


COMMON_FIELDS = {
    "schema_version",
    "id",
    "revision",
    "status",
    "created_at",
    "updated_at",
    "input_fingerprint",
}
ACTIVE = {"claimed", "implementing", "validating", "review", "playtest"}
ALLOWED_LICENSES = {
    "original",
    "CC0-1.0",
    "CC-BY-4.0",
    "Apache-2.0",
    "proprietary",
    "commercial",
}


def _load(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"{path}: invalid JSON: {error}")
        return None


def _records(folder: Path, pattern: str, errors: list[str]) -> list[dict[str, Any]]:
    records = []
    for path in sorted(folder.glob(pattern)):
        record = _load(path, errors)
        if record is None:
            continue
        missing = COMMON_FIELDS - record.keys()
        if missing:
            errors.append(f"{path}: missing common fields {sorted(missing)}")
        if not re.fullmatch(r"[A-Z]+-\d{4}", str(record.get("id", ""))):
            errors.append(f"{path}: invalid stable id {record.get('id')!r}")
        material = {key: value for key, value in record.items() if key != "input_fingerprint"}
        if record.get("input_fingerprint") != fingerprint(material):
            errors.append(f"{path}: input fingerprint does not match current record content")
        records.append(record)
    return records


def _schema_errors(record: dict[str, Any], schema_name: str) -> list[str]:
    schema_path = files("aigame.schemas").joinpath(schema_name)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    return [error.message for error in sorted(validator.iter_errors(record), key=lambda item: list(item.path))]


def validate_project(root: Path | str) -> dict[str, Any]:
    project = Path(root).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    required_files = [
        project / "AGENTS.md",
        project / ".aigame" / "project.toml",
        project / ".aigame" / "workflow.lock.json",
        project / "project.godot",
        project / "assets" / "asset-manifest.json",
    ]
    for path in required_files:
        if not path.is_file():
            errors.append(f"Missing required file: {path.relative_to(project)}")

    config_path = project / ".aigame" / "project.toml"
    if config_path.is_file():
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            errors.append(f"ProjectConfig: invalid TOML: {error}")
        else:
            errors.extend(
                f"ProjectConfig: {message}"
                for message in _schema_errors(config, "project-config.schema.json")
            )

    lock_path = project / ".aigame" / "workflow.lock.json"
    if lock_path.is_file():
        lock = _load(lock_path, errors)
        vendor_root = project / ".aigame" / "vendor" / "aigame"
        if lock is not None and vendor_root.is_dir():
            actual_checksum = workflow_snapshot_checksum(vendor_root)
            if lock.get("source_sha256") != actual_checksum:
                errors.append("Pinned workflow snapshot checksum does not match vendored source")

    requirements = _records(project / "work" / "requirements", "REQ-*.json", errors)
    items = _records(project / "work" / "items", "WI-*.json", errors)
    decisions = _records(project / "work" / "decisions", "DEC-*.json", errors)
    experiments = _records(project / "work" / "experiments", "EXP-*.json", errors)
    approvals = _records(project / "evidence" / "approvals", "APR-*.json", errors)
    evidence = _records(project / "evidence" / "records", "EVD-*.json", errors)
    for requirement in requirements:
        errors.extend(f"{requirement.get('id')}: {message}" for message in _schema_errors(requirement, "requirement.schema.json"))
    for item in items:
        errors.extend(f"{item.get('id')}: {message}" for message in _schema_errors(item, "work-item.schema.json"))
    for decision in decisions:
        errors.extend(
            f"{decision.get('id')}: {message}"
            for message in _schema_errors(decision, "decision.schema.json")
        )
    for experiment in experiments:
        errors.extend(
            f"{experiment.get('id')}: {message}"
            for message in _schema_errors(experiment, "experiment.schema.json")
        )
    for approval in approvals:
        errors.extend(
            f"{approval.get('id')}: {message}"
            for message in _schema_errors(approval, "approval.schema.json")
        )
    for record in evidence:
        errors.extend(
            f"{record.get('id')}: {message}"
            for message in _schema_errors(record, "evidence-record.schema.json")
        )
    requirement_ids = {record.get("id") for record in requirements}
    item_ids = {record.get("id") for record in items}
    for item in items:
        for requirement_id in item.get("requirements", []):
            if requirement_id not in requirement_ids:
                errors.append(f"{item.get('id')}: dangling requirement {requirement_id}")
        for dependency_id in item.get("dependencies", []):
            if dependency_id not in item_ids:
                errors.append(f"{item.get('id')}: dangling dependency {dependency_id}")
    current_evidence_items: set[str] = set()
    for record in evidence:
        work_item_id = record.get("work_item_id")
        if work_item_id not in item_ids:
            errors.append(f"{record.get('id')}: dangling work item {work_item_id}")
        for requirement_id in record.get("requirement_ids", []):
            if requirement_id not in requirement_ids:
                errors.append(f"{record.get('id')}: dangling requirement {requirement_id}")
        if record.get("status") == "current":
            current_evidence_items.add(str(work_item_id))
    for item in items:
        if item.get("status") == "done" and item.get("id") not in current_evidence_items:
            errors.append(f"{item.get('id')}: done work item has no current evidence")
    active = [item.get("id") for item in items if item.get("status") in ACTIVE]
    if len(active) > 1:
        errors.append(f"Serial active-slice violation: {active}")
    state_path = project / ".aigame" / "state" / "current.json"
    state = _load(state_path, errors) if state_path.is_file() else None
    projected = state.get("active_work_item") if state else None
    actual = active[0] if len(active) == 1 else None
    if projected != actual:
        errors.append(
            f"Serial state projection mismatch: state projection={projected!r}, active record={actual!r}"
        )

    manifest_path = project / "assets" / "asset-manifest.json"
    manifest = _load(manifest_path, errors) if manifest_path.is_file() else None
    if manifest:
        seen_assets: set[str] = set()
        for asset in manifest.get("assets", []):
            asset_id = str(asset.get("id", ""))
            if asset_id in seen_assets:
                errors.append(f"Duplicate asset id: {asset_id}")
            seen_assets.add(asset_id)
            missing = COMMON_FIELDS - asset.keys()
            if missing:
                errors.append(f"{asset_id}: missing common fields {sorted(missing)}")
            material = {
                key: value for key, value in asset.items() if key != "input_fingerprint"
            }
            if asset.get("input_fingerprint") != fingerprint(material):
                errors.append(f"{asset_id}: input fingerprint does not match current record content")
            errors.extend(
                f"{asset_id}: {message}"
                for message in _schema_errors(asset, "asset-record.schema.json")
            )
            license_name = str(asset.get("license", "unknown"))
            if license_name not in ALLOWED_LICENSES:
                errors.append(f"{asset_id}: asset license {license_name!r} is not allowed")
            runtime_path = str(asset.get("runtime_path", ""))
            if runtime_path.startswith(("/", "\\")) or ".." in Path(runtime_path).parts:
                errors.append(f"{asset_id}: unsafe runtime path {runtime_path!r}")
            provenance = asset.get("provenance", {})
            if provenance.get("generated") and not all(
                provenance.get(field)
                for field in ("provider", "model_id", "model_license", "prompt_sha256")
            ):
                errors.append(f"{asset_id}: generated asset provenance is incomplete")

    return {
        "schema_version": "1.0",
        "status": "failed" if errors else "passed",
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "requirements": len(requirements),
            "work_items": len(items),
            "decisions": len(decisions),
            "experiments": len(experiments),
            "approvals": len(approvals),
            "evidence": len(evidence),
        },
    }
