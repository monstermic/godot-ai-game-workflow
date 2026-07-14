from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class WorkflowError(RuntimeError):
    exit_code = 2


class InvalidTransition(WorkflowError):
    exit_code = 5


class HumanRequired(WorkflowError):
    exit_code = 3


class MissingCapability(WorkflowError):
    exit_code = 4

    def __init__(self, missing: Iterable[str]):
        self.missing = sorted(set(missing))
        super().__init__(f"Missing capabilities: {', '.join(self.missing)}")


TRANSITIONS: dict[str, set[str]] = {
    "draft": {"ready", "blocked", "cancelled"},
    "ready": {"claimed", "blocked", "cancelled"},
    "claimed": {"implementing", "blocked", "rework", "cancelled"},
    "implementing": {"validating", "blocked", "rework", "cancelled"},
    "validating": {"review", "blocked", "rework", "cancelled"},
    "review": {"playtest", "done", "blocked", "rework", "cancelled"},
    "playtest": {"done", "blocked", "rework", "cancelled"},
    "blocked": {"ready", "cancelled"},
    "rework": {"ready", "cancelled"},
    "done": set(),
    "cancelled": set(),
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def workflow_snapshot_checksum(root: Path | str) -> str:
    package = Path(root)
    entries: dict[str, str] = {}
    for path in sorted(package.glob("*.py")):
        entries[path.name] = path.read_text(encoding="utf-8").rstrip() + "\n"
    for path in sorted((package / "schemas").glob("*.json")):
        entries[f"schemas/{path.name}"] = path.read_text(encoding="utf-8").rstrip() + "\n"
    for path in sorted((package / "profiles").glob("*.json")):
        entries[f"profiles/{path.name}"] = path.read_text(encoding="utf-8").rstrip() + "\n"
    return fingerprint(entries)


def transition(item: dict[str, Any], target: str) -> dict[str, Any]:
    current = item.get("status")
    if target not in TRANSITIONS.get(str(current), set()):
        raise InvalidTransition(f"Cannot transition {current!r} to {target!r}")
    updated = copy.deepcopy(item)
    updated["status"] = target
    updated["revision"] = int(updated.get("revision", 0)) + 1
    updated["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    material = {key: value for key, value in updated.items() if key != "input_fingerprint"}
    updated["input_fingerprint"] = fingerprint(material)
    return updated


def choose_next(
    items: Iterable[dict[str, Any]],
    capabilities: set[str],
    *,
    allow_red_unknowns: bool = False,
) -> dict[str, Any]:
    candidates = [copy.deepcopy(item) for item in items]
    statuses = {str(item.get("id")): item.get("status") for item in candidates}
    ready = [
        item
        for item in candidates
        if item.get("status") == "ready"
        and all(statuses.get(str(dependency)) == "done" for dependency in item.get("dependencies", []))
    ]
    if not ready:
        raise HumanRequired("No ready work item is available")
    ready.sort(
        key=lambda item: (
            not bool(item.get("release_blocker", False)),
            -int(item.get("risk", 0)),
            -int(item.get("unblocks", 0)),
            -int(item.get("player_value", 0)),
            int(item.get("estimate", 0)),
            str(item.get("id", "")),
        )
    )
    selected = ready[0]
    red_unknowns = [
        unknown
        for unknown in selected.get("unknowns", [])
        if str(unknown.get("level", "")).lower() == "red"
    ]
    if red_unknowns and not allow_red_unknowns:
        raise HumanRequired(f"{selected['id']} has unresolved red unknowns")
    required = set(selected.get("required_capabilities", []))
    missing = required - capabilities
    if missing:
        raise MissingCapability(missing)
    return selected
