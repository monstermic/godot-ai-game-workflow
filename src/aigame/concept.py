from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from . import __version__
from .automation import create_agent_approval, is_ai_staging
from .core import WorkflowError, fingerprint


SCHEMA_VERSION = "1.0"
CONCEPT_STAGES = (
    "pitching",
    "product_identity",
    "mechanics",
    "content_catalog",
    "complete_game_arc",
    "quality_audit",
)
STAGE_CONTRACTS = {
    "pitching": "concept-pitch.schema.json (exactly three pitches)",
    "product_identity": "product-identity.schema.json",
    "mechanics": "mechanic-spec.schema.json[]",
    "content_catalog": "name-registry.schema.json + content-entry.schema.json[]",
    "complete_game_arc": "game-blueprint.schema.json",
    "quality_audit": "quality-assessment + game-definition-of-done + game-roadmap",
}
STAGE_INSTRUCTIONS = {
    "pitching": [
        "Extract the audience, genre, facts, assumptions, contradictions, non-goals, and risks into the intake contract before pitching.",
        "Create exactly three materially different, scope-feasible game directions.",
        "Each pitch must state the player fantasy, unique identity, core loop, beginning, and ending.",
        "Score identity, coherence, core-loop strength, scope feasibility, and player fit from 1 to 5.",
        "Recommend one pitch with a concise rationale; do not select it on the human's behalf.",
    ],
    "product_identity": [
        "Define the final development title, subtitle, descriptions, package slug, executable, save namespace, and edition.",
        "Define branding vocabulary and deterministic naming rules for every launch catalog entry.",
        "Return a product identity that can be bound to explicit human approval.",
    ],
    "mechanics": [
        "Enumerate every launch mechanic using stable MEC identifiers.",
        "Specify inputs, states, transitions, formulas, defaults, tuning ranges, processing order, interactions, edge cases, feedback, persistence, determinism, debug hooks, tests, and playtest hypotheses.",
        "Use executable baseline values; no required value may be TBD or a placeholder.",
    ],
    "content_catalog": [
        "Enumerate every authored launch entry using stable CNT identifiers and final names.",
        "Link each entry to mechanics, assets, milestone, Definition of Done, unlock/spawn rules, acceptance criteria, and playtest hypotheses.",
        "For procedural content, name and specify every generator, template, pool, affix, modifier, weight, constraint, and seed rule.",
    ],
    "complete_game_arc": [
        "Define the complete player journey from opening and first minute through final challenge, ending, credits, postgame, replay, and endgame.",
        "Link the release scope to every mechanic and content record and classify entries as must, optional, or cut.",
        "Narrative games require complete macro arcs; non-narrative games require an explicit not-applicable rationale and complete experiential arc.",
    ],
    "quality_audit": [
        "Classify every active profile item as required, optional, or not applicable with rationale.",
        "Create a game-level Definition of Done covering every must-scope mechanic and content entry.",
        "Create an acyclic whole-game roadmap; only the first milestone may contain task-level detail.",
        "Subjective claims require playtest evidence and cannot be marked complete by the producing agent.",
    ],
}
SCHEMA_BY_PREFIX = {
    "CIN": "concept-intake.schema.json",
    "PIT": "concept-pitch.schema.json",
    "PRD": "product-identity.schema.json",
    "MEC": "mechanic-spec.schema.json",
    "CNT": "content-entry.schema.json",
    "NAM": "name-registry.schema.json",
    "BLU": "game-blueprint.schema.json",
    "QAS": "quality-assessment.schema.json",
    "GDD": "game-definition-of-done.schema.json",
    "RMP": "game-roadmap.schema.json",
    "CTK": "concept-task.schema.json",
}
PROCEDURAL_KINDS = {
    "generator",
    "room_template",
    "encounter_template",
    "affix",
    "modifier",
    "pool_entry",
    "procedural_template",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Invalid JSON at {path}: {error}") from error
    if not isinstance(value, dict):
        raise WorkflowError(f"Expected a JSON object at {path}")
    return value


def _record(value: dict[str, Any], *, record_id: str, status: str) -> dict[str, Any]:
    created_at = str(value.get("created_at") or _now())
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": record_id,
        "revision": int(value.get("revision", 1)),
        "status": status,
        "created_at": created_at,
        "updated_at": str(value.get("updated_at") or created_at),
        **{
            key: item
            for key, item in value.items()
            if key
            not in {
                "schema_version",
                "id",
                "revision",
                "status",
                "created_at",
                "updated_at",
                "input_fingerprint",
            }
        },
    }
    record["input_fingerprint"] = fingerprint(record)
    return record


def _refresh(record: dict[str, Any], **changes: Any) -> dict[str, Any]:
    updated = {**record, **changes}
    updated["revision"] = int(record.get("revision", 0)) + 1
    updated["updated_at"] = _now()
    updated.pop("input_fingerprint", None)
    updated["input_fingerprint"] = fingerprint(updated)
    return updated


def _schema_errors(record: dict[str, Any], schema_name: str) -> list[str]:
    schema = json.loads(files("aigame.schemas").joinpath(schema_name).read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    return [
        f"{'.'.join(str(part) for part in error.path) or '<record>'}: {error.message}"
        for error in sorted(validator.iter_errors(record), key=lambda item: list(item.path))
    ]


def _assert_schema(record: dict[str, Any], schema_name: str) -> None:
    errors = _schema_errors(record, schema_name)
    if errors:
        raise WorkflowError(f"{schema_name}: {'; '.join(errors)}")


def _project(root: Path | str) -> Path:
    project = Path(root).resolve()
    if not (project / ".aigame" / "project.toml").is_file():
        raise WorkflowError(f"Not an initialized aigame project: {project}")
    return project


def _state_path(project: Path) -> Path:
    return project / ".aigame" / "state" / "concept.json"


def _state(project: Path) -> dict[str, Any]:
    path = _state_path(project)
    if not path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
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
        }
    return _load_json(path)


def _save_state(project: Path, state: dict[str, Any]) -> None:
    state["revision"] = int(state.get("revision", 0)) + 1
    _write_json(_state_path(project), state)


def _task_path(project: Path, task_id: str) -> Path:
    return project / "work" / "concept" / "tasks" / f"{task_id}.json"


def _create_task(project: Path, state: dict[str, Any], stage: str) -> dict[str, Any]:
    if stage not in CONCEPT_STAGES:
        raise WorkflowError(f"Unknown concept stage: {stage}")
    number = int(state.get("next_task_number", 1))
    task_id = f"CTK-{number:04d}"
    context_refs = ["work/concept/CIN-0001.json", ".aigame/project.toml"]
    context_refs.extend(
        {
            "product_identity": ["work/concept/PIT-0001.json", "work/concept/PIT-0002.json", "work/concept/PIT-0003.json"],
            "mechanics": ["work/concept/PRD-0001.json"],
            "content_catalog": ["work/concept/mechanics"],
            "complete_game_arc": ["work/concept/mechanics", "work/concept/content"],
            "quality_audit": ["work/concept", ".aigame/profiles"],
        }.get(stage, [])
    )
    task = _record(
        {
            "stage": stage,
            "instructions": STAGE_INSTRUCTIONS[stage],
            "context_refs": context_refs,
            "result_contract": STAGE_CONTRACTS[stage],
            "required_capabilities": ["filesystem"],
        },
        record_id=task_id,
        status="ready",
    )
    _assert_schema(task, "concept-task.schema.json")
    _write_json(_task_path(project, task_id), task)
    state["active_task_id"] = task_id
    state["next_task_number"] = number + 1
    return task


def _complete_task(project: Path, task: dict[str, Any]) -> None:
    _write_json(_task_path(project, str(task["id"])), _refresh(task, status="submitted"))


def _commit_sha(project: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    candidate = result.stdout.strip()
    return candidate if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", candidate) else "UNCOMMITTED"


def _approval_request(project: Path, scope_hash: str, decision: str) -> dict[str, Any]:
    return {"scope_hash": scope_hash, "commit_sha": _commit_sha(project), "decision": decision}


def _approval_commit_errors(
    project: Path,
    approval: dict[str, Any],
    *,
    input_paths: Iterable[Path] = (),
) -> list[str]:
    repository = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    if repository.returncode != 0:
        return [] if approval.get("commit_sha") == "UNCOMMITTED" else ["approval commit cannot be verified outside Git"]
    commit_sha = str(approval.get("commit_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        return ["approval commit is not a committed Git SHA"]
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{commit_sha}^{{commit}}"],
        cwd=project,
        capture_output=True,
        check=False,
    )
    if exists.returncode != 0:
        return ["approval commit does not exist in this repository"]
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit_sha, "HEAD"],
        cwd=project,
        capture_output=True,
        check=False,
    )
    if ancestor.returncode != 0:
        return ["approval commit is not an ancestor of the current commit"]
    relative_paths = [str(path.relative_to(project)).replace("\\", "/") for path in input_paths]
    if relative_paths:
        comparison = subprocess.run(
            ["git", "diff", "--quiet", commit_sha, "--", *relative_paths],
            cwd=project,
            capture_output=True,
            check=False,
        )
        if comparison.returncode == 1:
            return ["approval commit does not contain the current approved inputs"]
        if comparison.returncode != 0:
            return ["approval commit inputs could not be verified"]
    return []


def _blueprint_input_paths(project: Path) -> list[Path]:
    task_folder = project / "work" / "concept" / "tasks"
    return [
        path
        for path in sorted((project / "work" / "concept").rglob("*.json"))
        if task_folder not in path.parents
    ]


def _blueprint_inputs_are_committed(project: Path) -> bool:
    repository = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=project,
        capture_output=True,
        check=False,
    )
    if repository.returncode != 0:
        return True
    relative_paths = [
        str(path.relative_to(project)).replace("\\", "/")
        for path in _blueprint_input_paths(project)
    ]
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", *relative_paths],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode != 0:
        raise WorkflowError("Could not verify whether blueprint inputs are committed")
    return not status.stdout.strip()


def _validate_approval(
    project: Path,
    approval_path: Path | str | None,
    request: dict[str, Any],
) -> dict[str, Any]:
    if approval_path is None:
        if is_ai_staging(project):
            return create_agent_approval(project, request)
        raise WorkflowError("A checksum-bound human approval is required")
    approval = _load_json(Path(approval_path).resolve())
    errors = _schema_errors(approval, "approval.schema.json")
    material = {key: value for key, value in approval.items() if key != "input_fingerprint"}
    if approval.get("input_fingerprint") != fingerprint(material):
        errors.append("input_fingerprint does not match approval content")
    if approval.get("status") != "approved":
        errors.append("approval status must be approved")
    for key in ("scope_hash", "commit_sha", "decision"):
        if approval.get(key) != request.get(key):
            errors.append(f"approval {key} does not match the current request")
    if errors:
        raise WorkflowError(f"Invalid approval: {'; '.join(errors)}")
    return approval


def _persist_approval(project: Path, approval: dict[str, Any]) -> None:
    destination = project / "evidence" / "approvals" / f"{approval['id']}.json"
    if destination.is_file() and _load_json(destination) != approval:
        raise WorkflowError(f"Approval id already exists with different content: {approval['id']}")
    _write_json(destination, approval)


def _profile(project: Path, profile_id: str) -> dict[str, Any]:
    path = project / ".aigame" / "profiles" / f"{profile_id}.json"
    if not path.is_file():
        raise WorkflowError(f"Unknown quality profile: {profile_id}")
    value = _load_json(path)
    _assert_schema(value, "quality-profile.schema.json")
    return value


def start_concept(
    root: Path | str,
    *,
    prompt: str,
    profiles: Iterable[str] = (),
    from_existing_docs: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    project = _project(root)
    prompt = prompt.strip()
    if not prompt:
        raise WorkflowError("The concept prompt cannot be empty")
    if len(prompt) > 20000:
        raise WorkflowError("The concept prompt exceeds the 20,000 character limit")
    state = _state(project)
    if state.get("status") != "not_started":
        raise WorkflowError(f"Concept work has already started with status {state.get('status')}")
    selected_profiles = ["core-game-v1"]
    for profile_id in profiles:
        if profile_id not in selected_profiles:
            selected_profiles.append(profile_id)
    if re.search(r"\brogue[- ]?lite\b", prompt, re.IGNORECASE) and "roguelite-v1" not in selected_profiles:
        selected_profiles.append("roguelite-v1")
    for profile_id in selected_profiles:
        _profile(project, profile_id)
    source_documents = []
    if from_existing_docs:
        for path in sorted((project / "docs").glob("*.md")):
            source_documents.append(
                {
                    "path": path.relative_to(project).as_posix(),
                    "sha256": fingerprint(path.read_text(encoding="utf-8")),
                }
            )
    constraints = []
    config_text = (project / ".aigame" / "project.toml").read_text(encoding="utf-8")
    for key in ("godot_version", "renderer", "primary_target", "max_binary_mb"):
        match = re.search(rf"^{key}\s*=\s*(.+)$", config_text, re.MULTILINE)
        if match:
            constraints.append(f"{key}={match.group(1).strip()}")
    intake = _record(
        {
            "prompt": prompt,
            "constraints": constraints,
            "audience": "unspecified until the pitch set is approved",
            "genre_tags": ["roguelite"] if "roguelite-v1" in selected_profiles else ["unspecified"],
            "facts": [],
            "assumptions": [],
            "contradictions": [],
            "non_goals": [],
            "risks": [],
            "quality_profiles": selected_profiles,
            "source_documents": source_documents,
        },
        record_id="CIN-0001",
        status="accepted",
    )
    _assert_schema(intake, "concept-intake.schema.json")
    if not apply:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "dry_run",
            "intake": intake,
            "next_stage": "pitching",
        }
    _write_json(project / "work" / "concept" / "CIN-0001.json", intake)
    state.update(
        {
            "status": "pitching",
            "intake_id": "CIN-0001",
            "pending_approval": None,
            "pending_approval_options": None,
        }
    )
    task = _create_task(project, state, "pitching")
    state["history"].append({"stage": "intake", "completed_at": _now()})
    _save_state(project, state)
    return {"schema_version": SCHEMA_VERSION, "status": "passed", "concept_task": task}


def next_concept_task(root: Path | str) -> dict[str, Any]:
    project = _project(root)
    state = _state(project)
    if state.get("status") == "finalized":
        return {"schema_version": SCHEMA_VERSION, "status": "finalized", "concept_task": None}
    if state.get("status") in {"direction_selection", "blueprint_approval"}:
        approval_request = state.get("pending_approval")
        approval_requests = state.get("pending_approval_options")
        if state.get("status") == "direction_selection":
            records = _concept_records(project)
            approval_requests = {
                pitch["id"]: _approval_request(
                    project,
                    _pitch_scope(records["pitches"], records["intakes"][0], str(pitch["id"])),
                    f"select_concept_direction:{pitch['id']}",
                )
                for pitch in records["pitches"]
            }
            selected_id = str(state.get("selected_pitch_id") or "")
            approval_request = approval_requests.get(selected_id) or state.get("pending_approval")
        elif state.get("status") == "blueprint_approval":
            approval_request = _approval_request(
                project,
                _blueprint_scope(project),
                "approve_complete_game_blueprint",
            )
            if is_ai_staging(project):
                if not _blueprint_inputs_are_committed(project):
                    return {
                        "schema_version": SCHEMA_VERSION,
                        "status": "blocked",
                        "gate": "commit_blueprint_inputs",
                        "message": "Commit the canonical blueprint inputs, then run aigame concept finalize --apply.",
                    }
                return {
                    "schema_version": SCHEMA_VERSION,
                    "status": "passed",
                    "gate": "agent_finalize",
                    "approval_request": approval_request,
                    "next_action": "aigame concept finalize --apply --json",
                }
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "needs_human",
            "gate": state.get("status"),
            "approval_request": approval_request,
        }
        if approval_requests:
            result["approval_requests"] = approval_requests
        return result
    task_id = state.get("active_task_id")
    if not task_id:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "needs_human",
            "gate": "concept_start",
            "message": "Run aigame concept start with a user prompt.",
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "concept_task": _load_json(_task_path(project, str(task_id))),
    }


def _normalise_many(
    values: Any,
    *,
    prefix: str,
    status: str,
    schema_name: str,
) -> list[dict[str, Any]]:
    if not isinstance(values, list) or not values:
        raise WorkflowError(f"{prefix} submission must contain at least one record")
    records = []
    seen_ids: set[str] = set()
    for index, value in enumerate(values, 1):
        if not isinstance(value, dict):
            raise WorkflowError(f"{prefix} record {index} must be an object")
        record_id = str(value.get("id") or f"{prefix}-{index:04d}")
        if not re.fullmatch(rf"{prefix}-[0-9]{{4}}", record_id):
            raise WorkflowError(f"Invalid {prefix} id: {record_id}")
        if record_id in seen_ids:
            raise WorkflowError(f"Duplicate {prefix} id: {record_id}")
        seen_ids.add(record_id)
        payload = dict(value)
        if prefix == "MEC":
            payload.setdefault("launch_status", "must")
        record = _record(payload, record_id=record_id, status=status)
        _assert_schema(record, schema_name)
        records.append(record)
    return records


def _mechanic_semantic_errors(mechanics: Iterable[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for mechanic in mechanics:
        for formula in mechanic.get("formulas", []):
            formula_name = str(formula.get("name", "<unnamed>"))
            minimum = formula.get("minimum")
            maximum = formula.get("maximum")
            tuning_range = formula.get("tuning_range")
            if not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float)):
                errors.append(f"{mechanic['id']} {formula_name}: formula bounds must be numeric")
                continue
            if minimum > maximum:
                errors.append(f"{mechanic['id']} {formula_name}: minimum exceeds maximum")
            if (
                not isinstance(tuning_range, list)
                or len(tuning_range) != 2
                or not all(isinstance(value, (int, float)) for value in tuning_range)
            ):
                errors.append(f"{mechanic['id']} {formula_name}: tuning range must contain two numbers")
                continue
            lower, upper = tuning_range
            if lower > upper:
                errors.append(f"{mechanic['id']} {formula_name}: tuning range is reversed")
            if lower < minimum or upper > maximum:
                errors.append(f"{mechanic['id']} {formula_name}: tuning range exceeds formula bounds")
    return errors


def _blueprint_scope_errors(
    blueprint: dict[str, Any],
    mechanics: Iterable[dict[str, Any]],
    content: Iterable[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    scopes = [*mechanics, *content]
    all_ids = {str(record["id"]) for record in scopes}
    release_scope = blueprint.get("release_scope", {})
    bucket_by_id: dict[str, list[str]] = {}
    for bucket in ("must", "optional", "cut"):
        for scope_id in release_scope.get(bucket, []):
            bucket_by_id.setdefault(str(scope_id), []).append(bucket)
    for scope_id in sorted(all_ids):
        buckets = bucket_by_id.get(scope_id, [])
        if len(buckets) != 1:
            errors.append(f"{scope_id}: release scope must classify the record exactly once")
            continue
        record = next(value for value in scopes if str(value["id"]) == scope_id)
        if record.get("launch_status", "must") != buckets[0]:
            errors.append(
                f"{scope_id}: blueprint release scope does not match the record launch status"
            )
    for unknown_id in sorted(set(bucket_by_id) - all_ids):
        errors.append(f"{unknown_id}: release scope references an unknown mechanic or content entry")
    return errors


def _persist_many(project: Path, folder: str, records: Iterable[dict[str, Any]]) -> None:
    for record in records:
        _write_json(project / "work" / "concept" / folder / f"{record['id']}.json", record)


def _replace_many(
    project: Path,
    folder: str,
    prefix: str,
    records: Iterable[dict[str, Any]],
) -> None:
    values = list(records)
    current_ids = {str(record["id"]) for record in values}
    target = project / "work" / "concept" / folder
    for path in target.glob(f"{prefix}-*.json"):
        if path.stem not in current_ids:
            path.unlink()
    _persist_many(project, folder, values)


def _write_singleton(project: Path, prefix: str, record: dict[str, Any]) -> None:
    target = project / "work" / "concept"
    for path in target.glob(f"{prefix}-*.json"):
        if path.stem != record["id"]:
            path.unlink()
    _write_json(target / f"{record['id']}.json", record)


def _scope_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return only decision-bearing values, excluding mutable record bookkeeping."""
    excluded = {
        "schema_version",
        "id",
        "revision",
        "status",
        "created_at",
        "updated_at",
        "input_fingerprint",
        "approval_id",
    }
    return {key: value for key, value in record.items() if key not in excluded}


def _pitch_scope(
    pitches: list[dict[str, Any]],
    intake: dict[str, Any],
    selected_pitch_id: str,
) -> str:
    return fingerprint(
        {
            "decision": "select_concept_direction",
            "selected_pitch_id": selected_pitch_id,
            "intake": _scope_record(intake),
            "pitches": [_scope_record(pitch) for pitch in pitches],
        }
    )


def _product_scope(product: dict[str, Any]) -> str:
    return fingerprint(
        {"decision": "approve_product_identity", "result": _scope_record(product)}
    )


def _concept_records(project: Path) -> dict[str, list[dict[str, Any]]]:
    locations = {
        "intakes": (project / "work" / "concept", "CIN-*.json"),
        "pitches": (project / "work" / "concept", "PIT-*.json"),
        "products": (project / "work" / "concept", "PRD-*.json"),
        "mechanics": (project / "work" / "concept" / "mechanics", "MEC-*.json"),
        "content": (project / "work" / "concept" / "content", "CNT-*.json"),
        "names": (project / "work" / "concept", "NAM-*.json"),
        "blueprints": (project / "work" / "concept", "BLU-*.json"),
        "quality": (project / "work" / "concept", "QAS-*.json"),
        "dod": (project / "work" / "concept", "GDD-*.json"),
        "roadmaps": (project / "work" / "concept", "RMP-*.json"),
        "tasks": (project / "work" / "concept" / "tasks", "CTK-*.json"),
    }
    return {
        name: [_load_json(path) for path in sorted(folder.glob(pattern))]
        for name, (folder, pattern) in locations.items()
    }


def _blueprint_scope(project: Path) -> str:
    records = _concept_records(project)
    return fingerprint(
        {
            key: records[key]
            for key in ("intakes", "pitches", "products", "mechanics", "content", "names", "blueprints", "quality", "dod", "roadmaps")
        }
    )


def _next_stage(project: Path, state: dict[str, Any], task: dict[str, Any], stage: str) -> dict[str, Any]:
    state["history"].append({"stage": task["stage"], "completed_at": _now()})
    state["status"] = stage
    return _create_task(project, state, stage)


def submit_concept_task(
    root: Path | str,
    task_id: str,
    result: dict[str, Any] | Path | str,
    *,
    approval_path: Path | str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    project = _project(root)
    state = _state(project)
    if task_id != state.get("active_task_id"):
        raise WorkflowError(f"{task_id} is not the active concept task")
    task = _load_json(_task_path(project, task_id))
    if task.get("status") != "ready":
        raise WorkflowError(f"{task_id} is not ready")
    payload = _load_json(Path(result).resolve()) if isinstance(result, (str, Path)) else result
    if not isinstance(payload, dict):
        raise WorkflowError("Concept task result must be a JSON object")
    stage = str(task["stage"])

    if stage == "pitching":
        intake_value = payload.get("intake")
        if not isinstance(intake_value, dict):
            raise WorkflowError("Pitch submission must include the extracted intake")
        intake_fields = (
            "audience",
            "genre_tags",
            "facts",
            "assumptions",
            "contradictions",
            "non_goals",
            "risks",
        )
        missing_intake = [key for key in intake_fields if key not in intake_value]
        if missing_intake:
            raise WorkflowError(f"Pitch intake is missing fields: {', '.join(missing_intake)}")
        current_intake = _load_json(project / "work" / "concept" / "CIN-0001.json")
        intake = _refresh(current_intake, **{key: intake_value[key] for key in intake_fields})
        _assert_schema(intake, "concept-intake.schema.json")
        proposed = payload.get("pitches")
        if not isinstance(proposed, list) or len(proposed) != 3:
            raise WorkflowError("Pitch submission must contain exactly three pitches")
        recommended = payload.get("recommended_index")
        if not isinstance(recommended, int) or recommended not in range(3):
            raise WorkflowError("recommended_index must select one of the three pitches")
        pitches = []
        for index, value in enumerate(proposed, 1):
            if not isinstance(value, dict):
                raise WorkflowError("Each pitch must be an object")
            record = _record(
                {
                    **value,
                    "recommended": index - 1 == recommended,
                    "recommendation_rationale": payload.get("recommendation_rationale", ""),
                },
                record_id=f"PIT-{index:04d}",
                status="proposed",
            )
            _assert_schema(record, "concept-pitch.schema.json")
            pitches.append(record)
        approval_requests = {
            pitch["id"]: _approval_request(
                project,
                _pitch_scope(pitches, intake, str(pitch["id"])),
                f"select_concept_direction:{pitch['id']}",
            )
            for pitch in pitches
        }
        recommended_id = str(pitches[recommended]["id"])
        request = approval_requests[recommended_id]
        if not apply:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "dry_run",
                "intake": intake,
                "pitches": pitches,
                "approval_request": request,
                "approval_requests": approval_requests,
            }
        _write_json(project / "work" / "concept" / "CIN-0001.json", intake)
        _replace_many(project, "", "PIT", pitches)
        if is_ai_staging(project):
            approval = create_agent_approval(project, request)
            _persist_approval(project, approval)
            for pitch in pitches:
                changes: dict[str, Any] = {
                    "status": "selected" if pitch["id"] == recommended_id else "rejected"
                }
                if pitch["id"] == recommended_id:
                    changes["approval_id"] = approval["id"]
                _write_json(
                    project / "work" / "concept" / f"{pitch['id']}.json",
                    _refresh(pitch, **changes),
                )
            state.update(
                {
                    "status": "product_identity",
                    "selected_pitch_id": recommended_id,
                    "direction_approval_id": approval["id"],
                    "pending_approval": None,
                    "pending_approval_options": None,
                }
            )
            state["history"].extend(
                [
                    {"stage": "pitching", "completed_at": _now()},
                    {
                        "stage": "direction_selection",
                        "completed_at": _now(),
                        "authority": "agent",
                    },
                ]
            )
            next_task = _create_task(project, state, "product_identity")
            _save_state(project, state)
            _complete_task(project, task)
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "passed",
                "selected_pitch_id": recommended_id,
                "approval_id": approval["id"],
                "concept_task": next_task,
            }
        state.update(
            {
                "status": "direction_selection",
                "active_task_id": None,
                "pending_approval": request,
                "pending_approval_options": approval_requests,
            }
        )
        state["history"].append({"stage": "pitching", "completed_at": _now()})
        _save_state(project, state)
        _complete_task(project, task)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "needs_human",
            "pitches": pitches,
            "approval_request": request,
            "approval_requests": approval_requests,
        }

    if stage == "product_identity":
        provisional_product = _record(
            {**payload, "approval_id": "APR-0000"},
            record_id="PRD-0001",
            status="approved",
        )
        _assert_schema(provisional_product, "product-identity.schema.json")
        request = _approval_request(
            project,
            _product_scope(payload),
            "approve_product_identity",
        )
        if approval_path is None and not is_ai_staging(project):
            if apply:
                pending_task = _refresh(
                    task,
                    pending_result=payload,
                    approval_request=request,
                )
                _write_json(_task_path(project, task_id), pending_task)
                state["pending_approval"] = request
                state["pending_approval_options"] = None
                _save_state(project, state)
            return {"schema_version": SCHEMA_VERSION, "status": "needs_human", "approval_request": request}
        approval = _validate_approval(project, approval_path, request)
        product = _record(
            {**payload, "approval_id": approval["id"]},
            record_id="PRD-0001",
            status="approved",
        )
        _assert_schema(product, "product-identity.schema.json")
        if not apply:
            return {"schema_version": SCHEMA_VERSION, "status": "dry_run", "product_identity": product, "approval_request": request}
        _persist_approval(project, approval)
        _write_singleton(project, "PRD", product)
        state["product_identity_id"] = "PRD-0001"
        state["product_identity_approval_id"] = approval["id"]
        state["pending_approval"] = None
        state["pending_approval_options"] = None
        next_task = _next_stage(project, state, task, "mechanics")
        _save_state(project, state)
        _complete_task(project, task)
        return {"schema_version": SCHEMA_VERSION, "status": "passed", "concept_task": next_task}

    if stage == "mechanics":
        mechanics = _normalise_many(
            payload.get("mechanics"),
            prefix="MEC",
            status="approved",
            schema_name="mechanic-spec.schema.json",
        )
        names = [str(value["name"]).casefold() for value in mechanics]
        if len(names) != len(set(names)):
            raise WorkflowError("Duplicate mechanic name")
        semantic_errors = _mechanic_semantic_errors(mechanics)
        if semantic_errors:
            raise WorkflowError("; ".join(semantic_errors))
        if not apply:
            return {"schema_version": SCHEMA_VERSION, "status": "dry_run", "mechanics": mechanics}
        _replace_many(project, "mechanics", "MEC", mechanics)
        next_task = _next_stage(project, state, task, "content_catalog")
        _save_state(project, state)
        _complete_task(project, task)
        return {"schema_version": SCHEMA_VERSION, "status": "passed", "concept_task": next_task}

    if stage == "content_catalog":
        content = _normalise_many(
            payload.get("content"),
            prefix="CNT",
            status="approved",
            schema_name="content-entry.schema.json",
        )
        for field, label in (("canonical_name", "canonical name"), ("display_name", "display name"), ("localization_key", "localization key")):
            values = [str(record[field]).casefold() for record in content]
            if len(values) != len(set(values)):
                raise WorkflowError(f"Duplicate {label}")
        registry_value = payload.get("name_registry")
        if not isinstance(registry_value, dict):
            raise WorkflowError("content_catalog requires name_registry")
        registry = _record(registry_value, record_id=str(registry_value.get("id", "NAM-0001")), status="approved")
        _assert_schema(registry, "name-registry.schema.json")
        registry_ids = [entry.get("content_id") for entry in registry["entries"]]
        content_ids = [record["id"] for record in content]
        if sorted(registry_ids) != sorted(content_ids):
            raise WorkflowError("Name registry must contain exactly one entry for every content record")
        content_by_id = {str(record["id"]): record for record in content}
        for entry in registry["entries"]:
            content_record = content_by_id[str(entry["content_id"])]
            for field in ("canonical_name", "display_name", "localization_key", "category"):
                if entry.get(field) != content_record.get(field):
                    raise WorkflowError(
                        f"Name registry {field} for {entry['content_id']} does not match content"
                    )
        mechanic_ids = {record["id"] for record in _concept_records(project)["mechanics"]}
        for record in content:
            missing = set(record.get("mechanic_ids", [])) - mechanic_ids
            if missing:
                raise WorkflowError(f"{record['id']} references unknown mechanics: {sorted(missing)}")
            if record.get("kind") in PROCEDURAL_KINDS and not record.get("generation_rules"):
                raise WorkflowError(f"{record['id']} procedural content requires generation_rules")
        if not apply:
            return {"schema_version": SCHEMA_VERSION, "status": "dry_run", "content": content, "name_registry": registry}
        _replace_many(project, "content", "CNT", content)
        _write_singleton(project, "NAM", registry)
        next_task = _next_stage(project, state, task, "complete_game_arc")
        _save_state(project, state)
        _complete_task(project, task)
        return {"schema_version": SCHEMA_VERSION, "status": "passed", "concept_task": next_task}

    if stage == "complete_game_arc":
        blueprint = _record(payload, record_id=str(payload.get("id", "BLU-0001")), status="approved")
        _assert_schema(blueprint, "game-blueprint.schema.json")
        current = _concept_records(project)
        mechanics = {record["id"] for record in current["mechanics"]}
        content = {record["id"] for record in current["content"]}
        if set(blueprint["mechanic_ids"]) != mechanics:
            raise WorkflowError("Game blueprint must reference every mechanic exactly once")
        if set(blueprint["content_ids"]) != content:
            raise WorkflowError("Game blueprint must reference every content entry exactly once")
        scope_errors = _blueprint_scope_errors(
            blueprint,
            current["mechanics"],
            current["content"],
        )
        if scope_errors:
            raise WorkflowError("; ".join(scope_errors))
        if not apply:
            return {"schema_version": SCHEMA_VERSION, "status": "dry_run", "blueprint": blueprint}
        _write_singleton(project, "BLU", blueprint)
        next_task = _next_stage(project, state, task, "quality_audit")
        _save_state(project, state)
        _complete_task(project, task)
        return {"schema_version": SCHEMA_VERSION, "status": "passed", "concept_task": next_task}

    if stage == "quality_audit":
        components = (
            ("quality_assessment", "QAS-0001", "complete", "quality-assessment.schema.json"),
            ("definition_of_done", "GDD-0001", "approved", "game-definition-of-done.schema.json"),
            ("roadmap", "RMP-0001", "approved", "game-roadmap.schema.json"),
        )
        records: dict[str, dict[str, Any]] = {}
        for key, default_id, status, schema_name in components:
            value = payload.get(key)
            if not isinstance(value, dict):
                raise WorkflowError(f"quality_audit requires {key}")
            record = _record(value, record_id=str(value.get("id", default_id)), status=status)
            _assert_schema(record, schema_name)
            records[key] = record
        if not apply:
            return {"schema_version": SCHEMA_VERSION, "status": "dry_run", **records}
        prior_records: dict[Path, str] = {}
        for prefix in ("QAS", "GDD", "RMP"):
            for path in (project / "work" / "concept").glob(f"{prefix}-*.json"):
                prior_records[path] = path.read_text(encoding="utf-8")
        for key, record in records.items():
            prefix = {
                "quality_assessment": "QAS",
                "definition_of_done": "GDD",
                "roadmap": "RMP",
            }[key]
            _write_singleton(project, prefix, record)
        report = validate_concept(project, require_final=True)
        if report["status"] != "passed":
            for prefix in ("QAS", "GDD", "RMP"):
                for path in (project / "work" / "concept").glob(f"{prefix}-*.json"):
                    path.unlink()
            for path, content in prior_records.items():
                path.write_text(content, encoding="utf-8", newline="\n")
            raise WorkflowError("Concept audit failed: " + "; ".join(report["errors"]))
        state.update({"status": "blueprint_approval", "active_task_id": None})
        state["history"].append({"stage": "quality_audit", "completed_at": _now()})
        request = _approval_request(project, _blueprint_scope(project), "approve_complete_game_blueprint")
        if is_ai_staging(project):
            if not _blueprint_inputs_are_committed(project):
                state["pending_approval"] = None
                state["pending_approval_options"] = None
                _save_state(project, state)
                _complete_task(project, task)
                return {
                    "schema_version": SCHEMA_VERSION,
                    "status": "blocked",
                    "gate": "commit_blueprint_inputs",
                    "message": "Commit the canonical blueprint inputs, then run aigame concept finalize --apply.",
                    "validation": report,
                }
            approval = create_agent_approval(project, request)
            _persist_approval(project, approval)
            result = _apply_finalization(project, state, approval, request["scope_hash"])
            _complete_task(project, task)
            result["validation"] = report
            return result
        state["pending_approval"] = request
        state["pending_approval_options"] = None
        _save_state(project, state)
        _complete_task(project, task)
        return {"schema_version": SCHEMA_VERSION, "status": "needs_human", "approval_request": request, "validation": report}

    raise WorkflowError(f"Unsupported concept task stage: {stage}")


def select_concept_pitch(
    root: Path | str,
    pitch_id: str,
    approval_path: Path | str | None,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    project = _project(root)
    state = _state(project)
    if state.get("status") != "direction_selection":
        raise WorkflowError("The concept is not waiting for direction selection")
    pitches = _concept_records(project)["pitches"]
    selected = next((pitch for pitch in pitches if pitch["id"] == pitch_id), None)
    if selected is None:
        raise WorkflowError(f"Unknown concept pitch: {pitch_id}")
    intake = _concept_records(project)["intakes"][0]
    request = _approval_request(
        project,
        _pitch_scope(pitches, intake, pitch_id),
        f"select_concept_direction:{pitch_id}",
    )
    approval = _validate_approval(project, approval_path, request)
    if not apply:
        return {"schema_version": SCHEMA_VERSION, "status": "dry_run", "selected_pitch_id": pitch_id}
    _persist_approval(project, approval)
    for pitch in pitches:
        changes: dict[str, Any] = {"status": "selected" if pitch["id"] == pitch_id else "rejected"}
        if pitch["id"] == pitch_id:
            changes["approval_id"] = approval["id"]
        _write_json(project / "work" / "concept" / f"{pitch['id']}.json", _refresh(pitch, **changes))
    state.update(
        {
            "status": "product_identity",
            "selected_pitch_id": pitch_id,
            "direction_approval_id": approval["id"],
            "pending_approval": None,
            "pending_approval_options": None,
        }
    )
    task = _create_task(project, state, "product_identity")
    state["history"].append({"stage": "direction_selection", "completed_at": _now()})
    _save_state(project, state)
    return {"schema_version": SCHEMA_VERSION, "status": "passed", "selected_pitch_id": pitch_id, "concept_task": task}


def _placeholder_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"input_fingerprint", "updated_at", "created_at"}:
                continue
            paths.extend(_placeholder_paths(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_placeholder_paths(item, f"{prefix}[{index}]"))
    elif isinstance(value, str) and value.strip().casefold() in {"tbd", "todo", "placeholder", "unknown"}:
        paths.append(prefix)
    return paths


def _roadmap_cycle(milestones: list[dict[str, Any]]) -> list[str] | None:
    graph = {str(value.get("id")): list(value.get("dependencies", [])) for value in milestones}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: list[str]) -> list[str] | None:
        if node in visiting:
            return trail + [node]
        if node in visited:
            return None
        visiting.add(node)
        for dependency in graph.get(node, []):
            cycle = visit(str(dependency), trail + [node])
            if cycle:
                return cycle
        visiting.remove(node)
        visited.add(node)
        return None

    for milestone_id in graph:
        cycle = visit(milestone_id, [])
        if cycle:
            return cycle
    return None


def validate_concept(root: Path | str, *, require_final: bool = False) -> dict[str, Any]:
    project = _project(root)
    state = _state(project)
    errors: list[str] = []
    records = _concept_records(project)
    if state.get("status") == "not_started":
        if require_final:
            errors.append("Concept work has not started")
        return {"schema_version": SCHEMA_VERSION, "status": "failed" if errors else "passed", "errors": errors, "counts": {key: len(value) for key, value in records.items()}}
    for group in records.values():
        for record in group:
            record_id = str(record.get("id", ""))
            prefix = record_id.split("-", 1)[0]
            schema_name = SCHEMA_BY_PREFIX.get(prefix)
            if schema_name:
                errors.extend(f"{record_id}: {message}" for message in _schema_errors(record, schema_name))
            material = {key: value for key, value in record.items() if key != "input_fingerprint"}
            if record.get("input_fingerprint") != fingerprint(material):
                errors.append(f"{record_id}: input fingerprint does not match current content")
    if not require_final:
        return {"schema_version": SCHEMA_VERSION, "status": "failed" if errors else "passed", "errors": errors, "counts": {key: len(value) for key, value in records.items()}}
    expected_singletons = {
        "intakes": "Concept intake",
        "products": "Product identity",
        "names": "Name registry",
        "blueprints": "Game blueprint",
        "quality": "Quality assessment",
        "dod": "Game Definition of Done",
        "roadmaps": "Game roadmap",
    }
    for key, label in expected_singletons.items():
        if len(records[key]) != 1:
            errors.append(f"{label} must contain exactly one current record")
    if len(records["pitches"]) != 3:
        errors.append("Concept must contain exactly three pitches")
    if not records["mechanics"]:
        errors.append("Concept must define at least one mechanic")
    if not records["content"]:
        errors.append("Concept must define at least one named content entry")
    red_assumptions = [
        assumption
        for intake in records["intakes"]
        for assumption in intake.get("assumptions", [])
        if assumption.get("risk") == "red"
    ]
    red_risks = [
        risk
        for intake in records["intakes"]
        for risk in intake.get("risks", [])
        if risk.get("level") == "red"
    ]
    if (red_assumptions or red_risks) and not is_ai_staging(project):
        errors.append("Unresolved red assumptions or risks require human resolution")
    for key in ("products", "mechanics", "content", "names", "blueprints", "quality", "dod", "roadmaps"):
        for record in records[key]:
            for placeholder in _placeholder_paths(record):
                errors.append(f"{record['id']}: required value is a placeholder at {placeholder}")
    mechanic_ids = {record.get("id") for record in records["mechanics"]}
    content_ids = {record.get("id") for record in records["content"]}
    errors.extend(_mechanic_semantic_errors(records["mechanics"]))
    for field, label, group in (
        ("name", "mechanic name", records["mechanics"]),
        ("canonical_name", "canonical name", records["content"]),
        ("display_name", "display name", records["content"]),
        ("localization_key", "localization key", records["content"]),
    ):
        values = [str(record.get(field, "")).casefold() for record in group]
        if len(values) != len(set(values)):
            errors.append(f"Duplicate {label}")
    for content in records["content"]:
        for mechanic_id in content.get("mechanic_ids", []):
            if mechanic_id not in mechanic_ids:
                errors.append(f"{content['id']}: dangling mechanic {mechanic_id}")
        if content.get("kind") in PROCEDURAL_KINDS and not content.get("generation_rules"):
            errors.append(f"{content['id']}: procedural content requires generation_rules")
    if records["names"]:
        entries = records["names"][0].get("entries", [])
        registry_ids = [entry.get("content_id") for entry in entries]
        if sorted(registry_ids) != sorted(content_ids):
            errors.append("Name registry does not cover every content entry exactly once")
        content_by_id = {str(record["id"]): record for record in records["content"]}
        for entry in entries:
            content_record = content_by_id.get(str(entry.get("content_id")))
            if content_record is None:
                continue
            for field in ("canonical_name", "display_name", "localization_key", "category"):
                if entry.get(field) != content_record.get(field):
                    errors.append(
                        f"Name registry {field} for {entry.get('content_id')} does not match content"
                    )
    must_scope = {
        str(record["id"])
        for record in records["mechanics"] + records["content"]
        if record.get("launch_status", "must") == "must"
    }
    if records["blueprints"]:
        blueprint = records["blueprints"][0]
        errors.extend(
            _blueprint_scope_errors(
                blueprint,
                records["mechanics"],
                records["content"],
            )
        )
        if set(blueprint.get("mechanic_ids", [])) != mechanic_ids:
            errors.append("Game blueprint mechanic references do not match the mechanic catalog")
        if set(blueprint.get("content_ids", [])) != content_ids:
            errors.append("Game blueprint content references do not match the launch catalog")
        if not must_scope.issubset(set(blueprint.get("release_scope", {}).get("must", []))):
            errors.append("Game blueprint must-scope list omits required mechanics or content")
    if records["dod"]:
        covered = {
            str(scope_ref)
            for criterion in records["dod"][0].get("criteria", [])
            for scope_ref in criterion.get("scope_refs", [])
            if criterion.get("release_blocking")
        }
        if not must_scope.issubset(covered):
            errors.append("Game Definition of Done does not cover every must-scope mechanic and content entry")
    if records["roadmaps"]:
        milestones = records["roadmaps"][0].get("milestones", [])
        milestone_ids = {value.get("id") for value in milestones}
        for milestone in milestones:
            for dependency in milestone.get("dependencies", []):
                if dependency not in milestone_ids:
                    errors.append(f"{milestone.get('id')}: dangling milestone dependency {dependency}")
        cycle = _roadmap_cycle(milestones)
        if cycle:
            errors.append(f"Game roadmap contains a dependency cycle: {' -> '.join(cycle)}")
        if milestones and milestones[0].get("detail_level") != "task":
            errors.append("The first roadmap milestone must have task-level detail")
        if any(value.get("detail_level") != "milestone" for value in milestones[1:]):
            errors.append("Only the first roadmap milestone may have task-level detail")
        deliverables = {str(item) for value in milestones for item in value.get("deliverables", [])}
        if not must_scope.issubset(deliverables):
            errors.append("Game roadmap does not schedule every must-scope mechanic and content entry")
    if records["quality"] and records["intakes"]:
        expected: set[tuple[str, str]] = set()
        for profile_id in records["intakes"][0].get("quality_profiles", []):
            try:
                profile = _profile(project, profile_id)
            except WorkflowError as error:
                errors.append(str(error))
                continue
            expected.update((profile_id, str(item["id"])) for item in profile.get("items", []))
        actual_pairs = [
            (str(item.get("profile_id")), str(item.get("item_id")))
            for item in records["quality"][0].get("assessments", [])
        ]
        if len(actual_pairs) != len(set(actual_pairs)):
            errors.append("Quality assessment contains duplicate profile item classifications")
        for missing in sorted(expected - set(actual_pairs)):
            errors.append(f"Missing quality profile item classification: {missing[0]}/{missing[1]}")
        for extra in sorted(set(actual_pairs) - expected):
            errors.append(f"Unknown quality profile item classification: {extra[0]}/{extra[1]}")
    approval_paths = {
        path.stem: path for path in (project / "evidence" / "approvals").glob("APR-*.json")
    }
    approval_ids = set(approval_paths)
    if state.get("direction_approval_id") not in approval_ids:
        errors.append("Current direction approval is missing")
    elif records["intakes"] and records["pitches"]:
        approval = _load_json(approval_paths[str(state["direction_approval_id"])])
        selected_pitch_id = str(state.get("selected_pitch_id") or "")
        expected_scope = _pitch_scope(
            records["pitches"],
            records["intakes"][0],
            selected_pitch_id,
        )
        if approval.get("status") != "approved" or approval.get("scope_hash") != expected_scope:
            errors.append("Current direction approval scope does not match the approved direction inputs")
        errors.extend(
            f"Current direction {message}"
            for message in _approval_commit_errors(project, approval)
        )
    if state.get("product_identity_approval_id") not in approval_ids:
        errors.append("Current product identity approval is missing")
    elif records["products"]:
        approval = _load_json(approval_paths[str(state["product_identity_approval_id"])])
        expected_scope = _product_scope(records["products"][0])
        if approval.get("status") != "approved" or approval.get("scope_hash") != expected_scope:
            errors.append("Current product identity approval scope does not match the approved identity")
        errors.extend(
            f"Current product identity {message}"
            for message in _approval_commit_errors(project, approval)
        )
    if state.get("status") == "finalized":
        if state.get("blueprint_approval_id") not in approval_ids:
            errors.append("Current blueprint approval is missing")
        else:
            approval = _load_json(approval_paths[str(state["blueprint_approval_id"])])
            expected_scope = _blueprint_scope(project)
            if approval.get("status") != "approved" or approval.get("scope_hash") != expected_scope:
                errors.append("Current blueprint approval scope does not match the current blueprint inputs")
            errors.extend(
                f"Current blueprint {message}"
                for message in _approval_commit_errors(
                    project,
                    approval,
                    input_paths=_blueprint_input_paths(project),
                )
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed" if errors else "passed",
        "errors": errors,
        "counts": {key: len(value) for key, value in records.items()},
    }


def _bullets(values: Iterable[Any]) -> str:
    items = list(values)
    return "\n".join(f"- {value}" for value in items) if items else "- None"


def _value_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _labeled_values(record: dict[str, Any], fields: Iterable[str]) -> list[str]:
    return [
        f"**{field.replace('_', ' ').title()}:** {_value_text(record.get(field))}"
        for field in fields
    ]


def _render_documents(project: Path) -> dict[str, str]:
    records = _concept_records(project)
    documents: dict[str, str] = {}
    if records["products"]:
        value = records["products"][0]
        documents["product-identity.md"] = "\n".join(
            [
                f"# {value['display_name']}",
                "",
                f"**Subtitle:** {value['subtitle']}",
                f"**Edition:** {value['edition']}",
                f"**Package:** `{value['package_slug']}`",
                f"**Executable:** `{value['executable_name']}`",
                f"**Save namespace:** `{value['save_namespace']}`",
                "",
                value["long_description"],
                "",
                "## Naming rules",
                "",
                _bullets(value["naming_rules"]),
            ]
        )
    if records["blueprints"]:
        value = records["blueprints"][0]
        narrative = value["narrative"]
        documents["complete-game-blueprint.md"] = "\n".join(
            [
                "# Complete Game Blueprint",
                "",
                value["identity"],
                "",
                f"**Audience:** {value['audience']}",
                "",
                "## Pillars",
                "",
                _bullets(value["pillars"]),
                "",
                "## Product promises and scope",
                "",
                *_labeled_values(value, ("promises", "non_goals", "release_scope", "player_verbs")),
                "",
                "## Complete player journey",
                "",
                f"**Opening:** {value['opening']}",
                f"**First minute:** {value['first_minute']}",
                f"**Tutorial:** {value['tutorial']}",
                f"**First meaningful decision:** {value['first_meaningful_decision']}",
                f"**Moment-to-moment loop:** {value['moment_to_moment_loop']}",
                f"**Session loop:** {value['session_loop']}",
                f"**Long-term loop:** {value['long_term_loop']}",
                "",
                "### Progression",
                "",
                _bullets(value["progression_arc"]),
                "",
                "### Major encounters",
                "",
                _bullets(value["major_encounters"]),
                "",
                f"**Final challenge:** {value['final_challenge']}",
                f"**Final prerequisites:** {_value_text(value['final_prerequisites'])}",
                f"**Win conditions:** {_value_text(value['win_conditions'])}",
                f"**Loss conditions:** {_value_text(value['loss_conditions'])}",
                f"**Alternate endings:** {_value_text(value['alternate_endings'])}",
                f"**Recovery:** {value['recovery']}",
                f"**Ending:** {value['ending']}",
                f"**Credits:** {value['credits']}",
                f"**Postgame:** {value['postgame']}",
                f"**Replay:** {value['replay']}",
                f"**Endgame:** {value['endgame']}",
                "",
                "## Narrative",
                "",
                f"**Applicability:** {narrative['applicability']}",
                f"**Premise:** {narrative['premise']}",
                f"**Character arcs:** {_value_text(narrative['character_arcs'])}",
                f"**Acts:** {_value_text(narrative['acts'])}",
                f"**Resolution:** {narrative['resolution']}",
                "",
                "## Whole-game systems",
                "",
                *_labeled_values(
                    value,
                    (
                        "systems_overview",
                        "progression_system",
                        "economy",
                        "balance_strategy",
                        "difficulty",
                        "ux_information",
                        "art_direction",
                        "audio_direction",
                        "save_and_recovery",
                        "performance_budgets",
                        "platform_and_input",
                        "accessibility",
                        "technical_constraints",
                        "risks",
                        "production_risks",
                        "cut_rules",
                    ),
                ),
            ]
        )
    if records["mechanics"]:
        lines = ["# Implementation-Ready Mechanics", ""]
        for value in records["mechanics"]:
            lines.extend(
                [
                    f"## {value['id']}: {value['name']}",
                    "",
                    f"**Player intent:** {value['player_intent']}",
                    f"**Expected experience:** {value['expected_experience']}",
                    f"**Launch status:** {value['launch_status']}",
                    "",
                    "### States",
                    "",
                    _bullets(value["states"]),
                    "",
                    "### Rules and processing order",
                    "",
                    _bullets(value["rules"] + value["processing_order"]),
                    "",
                    "### Formulas",
                    "",
                ]
            )
            for formula in value["formulas"]:
                lines.append(
                    f"- **{formula['name']}**: `{formula['expression']}` ({formula['unit']}), "
                    f"defaults `{json.dumps(formula['default_values'], sort_keys=True)}`, "
                    f"minimum `{formula['minimum']}`, maximum `{formula['maximum']}`, "
                    f"tuning range `{formula['tuning_range']}`"
                )
            lines.extend(
                [
                    "",
                    "### Complete implementation contract",
                    "",
                    *_labeled_values(
                        value,
                        (
                            "inputs",
                            "preconditions",
                            "outputs",
                            "failure_conditions",
                            "transitions",
                            "timing",
                            "probabilities",
                            "resource_costs",
                            "rewards",
                            "interactions",
                            "stacking",
                            "priority",
                            "cancellation",
                            "immunities",
                            "conflicts",
                            "npc_use",
                            "edge_cases",
                            "invalid_states",
                            "feedback",
                            "persistence",
                            "determinism",
                            "debug_instrumentation",
                            "success_criteria",
                        ),
                    ),
                    "",
                    "### Acceptance tests",
                    "",
                    _bullets(value["acceptance_tests"]),
                    "",
                    "### Human playtest hypotheses",
                    "",
                    _bullets(value["playtest_hypotheses"]),
                    "",
                ]
            )
        documents["mechanics.md"] = "\n".join(lines)
    if records["content"]:
        lines = ["# Named Launch Content Catalog", ""]
        for value in records["content"]:
            lines.extend(
                [
                    f"## {value['id']}: {value['display_name']}",
                    "",
                    f"**Canonical name:** {value['canonical_name']}",
                    f"**Localization key:** `{value['localization_key']}`",
                    f"**Kind:** {value['kind']}",
                    f"**Launch status:** {value['launch_status']}",
                    f"**Role:** {value['gameplay_role']}",
                    f"**Purpose:** {value['design_purpose']}",
                    f"**Mechanics:** {', '.join(value['mechanic_ids']) or 'None'}",
                    "",
                    value["description"],
                    "",
                    *_labeled_values(
                        value,
                        (
                            "aliases",
                            "pronunciation",
                            "category",
                            "player_behavior",
                            "stats",
                            "parameters",
                            "rarity",
                            "tier",
                            "acquisition",
                            "unlock_conditions",
                            "spawn_conditions",
                            "reward_conditions",
                            "discovery_conditions",
                            "relationships",
                            "required_assets",
                            "milestone_id",
                            "dod_ids",
                            "acceptance_criteria",
                            "playtest_hypotheses",
                            "generation_rules",
                        ),
                    ),
                    "",
                ]
            )
        documents["launch-content-catalog.md"] = "\n".join(lines)
    if records["quality"]:
        lines = ["# Quality Assessment", ""]
        for item in records["quality"][0]["assessments"]:
            lines.append(
                f"- **{item['profile_id']}/{item['item_id']} - {item['disposition']}**: "
                f"{item['rationale']} Scope: {_value_text(item['scope_refs'])}"
            )
        documents["quality-assessment.md"] = "\n".join(lines)
    if records["dod"]:
        lines = ["# Game Definition of Done", ""]
        for item in records["dod"][0]["criteria"]:
            lines.extend(
                [
                    f"## {item['id']}: {item['title']}",
                    "",
                    *_labeled_values(
                        item,
                        ("scope_refs", "verification", "evidence_types", "release_blocking"),
                    ),
                    "",
                    "### Acceptance criteria",
                    "",
                    _bullets(item["acceptance_criteria"]),
                    "",
                    "### Human playtest hypotheses",
                    "",
                    _bullets(item["playtest_hypotheses"]),
                    "",
                ]
            )
        documents["definition-of-done.md"] = "\n".join(lines)
    if records["roadmaps"]:
        lines = ["# Whole-Game Roadmap", ""]
        for item in records["roadmaps"][0]["milestones"]:
            lines.extend(
                [
                    f"## {item['id']}: {item['name']}",
                    "",
                    f"**Dependencies:** {', '.join(item['dependencies']) or 'None'}",
                    f"**Detail:** {item['detail_level']}",
                    f"**Budget:** {_value_text(item['budget'])}",
                    "",
                    "### Deliverables",
                    "",
                    _bullets(item["deliverables"]),
                    "",
                    "### Exit gates",
                    "",
                    _bullets(item["exit_gates"]),
                    "",
                    "### Cut lines",
                    "",
                    _bullets(item["cut_lines"]),
                    "",
                ]
            )
        documents["roadmap.md"] = "\n".join(lines)
    if records["intakes"]:
        intake = records["intakes"][0]
        documents["assumptions-and-decisions.md"] = "\n".join(
            [
                "# Assumptions and Decisions",
                "",
                "## Source prompt",
                "",
                intake["prompt"],
                "",
                "## Assumptions",
                "",
                _bullets(f"[{item['risk']}] {item['text']}" for item in intake["assumptions"]),
                "",
                "## Contradictions",
                "",
                _bullets(intake["contradictions"]),
            ]
        )
    return {name: content.rstrip() + "\n" for name, content in documents.items()}


def render_concept(
    root: Path | str,
    *,
    apply: bool = False,
    check: bool = False,
) -> dict[str, Any]:
    project = _project(root)
    documents = _render_documents(project)
    target = project / "docs" / "blueprint"
    drift = []
    for name, content in documents.items():
        path = target / name
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            drift.append(name)
    if check:
        return {"schema_version": SCHEMA_VERSION, "status": "failed" if drift else "passed", "drift": drift}
    if not apply:
        return {"schema_version": SCHEMA_VERSION, "status": "dry_run", "files": sorted(documents), "drift": drift}
    for name, content in documents.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    return {"schema_version": SCHEMA_VERSION, "status": "passed", "files": sorted(documents)}


def _next_numeric_id(folder: Path, prefix: str) -> int:
    values = []
    for path in folder.glob(f"{prefix}-*.json"):
        match = re.fullmatch(rf"{prefix}-([0-9]{{4}})\.json", path.name)
        if match:
            values.append(int(match.group(1)))
    return max(values, default=0) + 1


def _clear_partial_materialization(project: Path, scope_hash: str) -> None:
    for folder, pattern in (
        (project / "work" / "requirements", "REQ-*.json"),
        (project / "work" / "items", "WI-*.json"),
        (project / "evidence" / "records", "EVD-*.json"),
    ):
        for path in folder.glob(pattern):
            record = _load_json(path)
            if record.get("materialization_key") == scope_hash:
                path.unlink()


def _materialize_work(project: Path, scope_hash: str) -> dict[str, Any]:
    _clear_partial_materialization(project, scope_hash)
    records = _concept_records(project)
    requirements_dir = project / "work" / "requirements"
    items_dir = project / "work" / "items"
    req_number = _next_numeric_id(requirements_dir, "REQ")
    wi_number = _next_numeric_id(items_dir, "WI")
    requirement_map: dict[str, str] = {}
    quality_requirement_map: dict[str, list[str]] = {}
    created_requirements: list[str] = []
    for scope in [*records["mechanics"], *records["content"]]:
        if scope.get("launch_status", "must") != "must":
            continue
        req_id = f"REQ-{req_number:04d}"
        req_number += 1
        title = str(scope.get("name") or scope.get("display_name") or scope["id"])
        requirement = _record(
            {
                "title": f"Implement {title}",
                "player_value": str(scope.get("player_intent") or scope.get("design_purpose") or "Deliver approved launch scope."),
                "acceptance_criteria": list(scope.get("acceptance_tests") or scope.get("acceptance_criteria") or [f"{scope['id']} matches the approved blueprint."]),
                "playtest_hypotheses": list(scope.get("playtest_hypotheses", [])),
                "non_goals": ["Unapproved scope expansion."],
                "concept_refs": [scope["id"]],
                "materialization_key": scope_hash,
            },
            record_id=req_id,
            status="approved",
        )
        _assert_schema(requirement, "requirement.schema.json")
        _write_json(requirements_dir / f"{req_id}.json", requirement)
        requirement_map[str(scope["id"])] = req_id
        created_requirements.append(req_id)
    profile_items: dict[tuple[str, str], dict[str, Any]] = {}
    for profile_id in records["intakes"][0].get("quality_profiles", []):
        profile = _profile(project, str(profile_id))
        for item in profile.get("items", []):
            profile_items[(str(profile_id), str(item["id"]))] = item
    for assessment in records["quality"][0].get("assessments", []):
        if assessment.get("disposition") != "required":
            continue
        profile_id = str(assessment["profile_id"])
        item_id = str(assessment["item_id"])
        profile_item = profile_items[(profile_id, item_id)]
        req_id = f"REQ-{req_number:04d}"
        req_number += 1
        evidence_types = {str(value) for value in profile_item.get("evidence", [])}
        requirement = _record(
            {
                "title": f"Satisfy {profile_id}/{item_id}: {profile_item['title']}",
                "player_value": str(profile_item["guidance"]),
                "acceptance_criteria": [
                    f"Provide the required {', '.join(sorted(evidence_types)) or 'review'} evidence for {profile_id}/{item_id}.",
                    str(assessment["rationale"]),
                ],
                "playtest_hypotheses": (
                    [f"Human playtesting confirms {profile_item['title'].lower()} supports the approved player promise."]
                    if "playtest" in evidence_types
                    else []
                ),
                "non_goals": ["Treating an optional or not-applicable quality item as release blocking."],
                "quality_profile_ref": f"{profile_id}/{item_id}",
                "concept_refs": [records["quality"][0]["id"], *assessment.get("scope_refs", [])],
                "materialization_key": scope_hash,
            },
            record_id=req_id,
            status="approved",
        )
        _assert_schema(requirement, "requirement.schema.json")
        _write_json(requirements_dir / f"{req_id}.json", requirement)
        created_requirements.append(req_id)
        for scope_ref in assessment.get("scope_refs", []):
            quality_requirement_map.setdefault(str(scope_ref), []).append(req_id)
    first_milestone = records["roadmaps"][0]["milestones"][0]
    previous_item: str | None = None
    created_items: list[str] = []
    for scope in [*records["mechanics"], *records["content"]]:
        scope_id = str(scope["id"])
        if scope_id not in requirement_map or scope_id not in first_milestone.get("deliverables", []):
            continue
        work_id = f"WI-{wi_number:04d}"
        wi_number += 1
        title = str(scope.get("name") or scope.get("display_name") or scope_id)
        work_item = _record(
            {
                "title": f"Implement {scope_id}: {title}",
                "type": "mechanic" if scope_id.startswith("MEC-") else "content",
                "milestone": first_milestone["id"],
                "requirements": [
                    requirement_map[scope_id],
                    *quality_requirement_map.get(scope_id, []),
                ],
                "dependencies": [previous_item] if previous_item else [],
                "release_blocker": False,
                "risk": 4 if scope_id.startswith("MEC-") else 3,
                "unblocks": 1,
                "player_value": 5,
                "estimate": 1,
                "required_capabilities": ["filesystem", "shell", "git", "godot"],
                "unknowns": [],
                "scope": [f"work/concept/{'mechanics' if scope_id.startswith('MEC-') else 'content'}/{scope_id}.json"],
                "tests": list(scope.get("acceptance_tests") or scope.get("acceptance_criteria") or ["aigame validate"]),
                "playtest_required": bool(scope.get("playtest_hypotheses")),
                "definition_of_done": [f"{scope_id} is implemented and current evidence satisfies {requirement_map[scope_id]}."],
                "concept_refs": [scope_id, records["blueprints"][0]["id"], records["dod"][0]["id"]],
                "materialization_key": scope_hash,
            },
            record_id=work_id,
            status="ready",
        )
        _assert_schema(work_item, "work-item.schema.json")
        _write_json(items_dir / f"{work_id}.json", work_item)
        created_items.append(work_id)
        previous_item = work_id
    for milestone in records["roadmaps"][0]["milestones"][1:]:
        work_id = f"WI-{wi_number:04d}"
        wi_number += 1
        work_item = _record(
            {
                "title": f"Plan milestone {milestone['id']}: {milestone['name']}",
                "type": "milestone_planning",
                "milestone": milestone["id"],
                "requirements": [],
                "dependencies": [previous_item] if previous_item else [],
                "release_blocker": False,
                "risk": 2,
                "unblocks": len(milestone.get("deliverables", [])),
                "player_value": 3,
                "estimate": 1,
                "required_capabilities": ["filesystem", "shell", "git"],
                "unknowns": [],
                "scope": ["work/concept/RMP-0001.json"],
                "tests": ["aigame validate"],
                "playtest_required": False,
                "definition_of_done": ["Create schema-valid work items for this milestone without detailing later milestones."],
                "concept_refs": [records["roadmaps"][0]["id"]],
                "materialization_key": scope_hash,
            },
            record_id=work_id,
            status="ready",
        )
        _assert_schema(work_item, "work-item.schema.json")
        _write_json(items_dir / f"{work_id}.json", work_item)
        created_items.append(work_id)
        previous_item = work_id
    initial_item_path = items_dir / "WI-0001.json"
    if initial_item_path.is_file():
        initial = _load_json(initial_item_path)
        _write_json(initial_item_path, _refresh(initial, status="done"))
        evidence = _record(
            {
                "kind": "approval",
                "work_item_id": "WI-0001",
                "requirement_ids": ["REQ-0001"],
                "artifact_sha256": scope_hash,
                "materialization_key": scope_hash,
            },
            record_id=f"EVD-{_next_numeric_id(project / 'evidence' / 'records', 'EVD'):04d}",
            status="current",
        )
        _assert_schema(evidence, "evidence-record.schema.json")
        _write_json(project / "evidence" / "records" / f"{evidence['id']}.json", evidence)
    initial_requirement_path = requirements_dir / "REQ-0001.json"
    if initial_requirement_path.is_file():
        initial_requirement = _load_json(initial_requirement_path)
        _write_json(initial_requirement_path, _refresh(initial_requirement, status="approved"))
    current_state_path = project / ".aigame" / "state" / "current.json"
    current_state = _load_json(current_state_path)
    current_state.update({"active_work_item": None, "active_branch": None, "last_checkpoint": None})
    _write_json(current_state_path, current_state)
    return {"requirements": created_requirements, "work_items": created_items}


def finalize_concept(
    root: Path | str,
    approval_path: Path | str | None,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    project = _project(root)
    state = _state(project)
    if state.get("status") != "blueprint_approval":
        raise WorkflowError("The concept is not ready for blueprint approval")
    report = validate_concept(project, require_final=True)
    if report["status"] != "passed":
        raise WorkflowError("Blueprint Ready validation failed: " + "; ".join(report["errors"]))
    request = _approval_request(project, _blueprint_scope(project), "approve_complete_game_blueprint")
    approval = _validate_approval(project, approval_path, request)
    if not apply:
        return {"schema_version": SCHEMA_VERSION, "status": "dry_run", "approval_request": request, "validation": report}
    _persist_approval(project, approval)
    return _apply_finalization(project, state, approval, request["scope_hash"])


def _apply_finalization(
    project: Path,
    state: dict[str, Any],
    approval: dict[str, Any],
    scope_hash: str,
) -> dict[str, Any]:
    materialized = _materialize_work(project, scope_hash)
    rendered = render_concept(project, apply=True)
    state.update(
        {
            "status": "finalized",
            "blueprint_approval_id": approval["id"],
            "pending_approval": None,
            "pending_approval_options": None,
            "active_task_id": None,
        }
    )
    state["history"].append({"stage": "blueprint_approval", "completed_at": _now()})
    config_path = project / ".aigame" / "project.toml"
    config = config_path.read_text(encoding="utf-8")
    config = re.sub(
        r'^workflow_version = "[^"]+"$',
        f'workflow_version = "{__version__}"',
        config,
        flags=re.MULTILINE,
    )
    config = re.sub(r'^current_milestone = "[^"]+"$', 'current_milestone = "experiment"', config, flags=re.MULTILINE)
    config_path.write_text(config, encoding="utf-8", newline="\n")
    _save_state(project, state)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "blueprint_approval_id": approval["id"],
        "materialized": materialized,
        "rendered": rendered,
    }


def _expire_approval(project: Path, approval_id: str | None) -> None:
    if not approval_id:
        return
    path = project / "evidence" / "approvals" / f"{approval_id}.json"
    if path.is_file():
        _write_json(path, _refresh(_load_json(path), status="expired"))


def _invalidate_materialized_work(project: Path) -> dict[str, list[str]]:
    deprecated_requirements: list[str] = []
    cancelled_work_items: list[str] = []
    stale_evidence: list[str] = []
    for path in sorted((project / "work" / "requirements").glob("REQ-*.json")):
        requirement = _load_json(path)
        if not (requirement.get("concept_refs") or requirement.get("quality_profile_ref")):
            continue
        if requirement.get("status") != "deprecated":
            _write_json(path, _refresh(requirement, status="deprecated"))
        deprecated_requirements.append(str(requirement["id"]))
    for path in sorted((project / "work" / "items").glob("WI-*.json")):
        work_item = _load_json(path)
        if not work_item.get("concept_refs"):
            continue
        if work_item.get("status") not in {"done", "cancelled"}:
            _write_json(path, _refresh(work_item, status="cancelled"))
        cancelled_work_items.append(str(work_item["id"]))
    affected_requirements = set(deprecated_requirements)
    affected_items = set(cancelled_work_items)
    for path in sorted((project / "evidence" / "records").glob("EVD-*.json")):
        evidence = _load_json(path)
        if evidence.get("status") != "current":
            continue
        if (
            str(evidence.get("work_item_id")) in affected_items
            or affected_requirements.intersection(evidence.get("requirement_ids", []))
        ):
            _write_json(path, _refresh(evidence, status="stale"))
            stale_evidence.append(str(evidence["id"]))
    current_state_path = project / ".aigame" / "state" / "current.json"
    current_state = _load_json(current_state_path)
    if current_state.get("active_work_item") in affected_items:
        current_state.update(
            {"active_work_item": None, "active_branch": None, "last_checkpoint": None}
        )
        _write_json(current_state_path, current_state)
    return {
        "requirements": deprecated_requirements,
        "work_items": cancelled_work_items,
        "evidence": stale_evidence,
    }


def revise_concept(
    root: Path | str,
    change_path: Path | str,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    project = _project(root)
    change = _load_json(Path(change_path).resolve())
    summary = str(change.get("summary", "")).strip()
    if not summary:
        raise WorkflowError("A concept revision requires a non-empty summary")
    requested_stage = str(change.get("restart_stage", "complete_game_arc"))
    if requested_stage not in CONCEPT_STAGES:
        raise WorkflowError(
            f"Invalid restart_stage {requested_stage!r}; expected one of {', '.join(CONCEPT_STAGES)}"
        )
    identity_change = bool(change.get("identity_change", False)) or requested_stage in {
        "pitching",
        "product_identity",
    }
    stage = "pitching" if identity_change else requested_stage
    if not apply:
        return {"schema_version": SCHEMA_VERSION, "status": "dry_run", "restart_stage": stage, "change": change}
    state = _state(project)
    invalidated = _invalidate_materialized_work(project)
    _expire_approval(project, state.get("blueprint_approval_id"))
    if identity_change:
        _expire_approval(project, state.get("direction_approval_id"))
        _expire_approval(project, state.get("product_identity_approval_id"))
        state["direction_approval_id"] = None
        state["product_identity_approval_id"] = None
        state["selected_pitch_id"] = None
    state["blueprint_approval_id"] = None
    state["pending_approval"] = None
    state["pending_approval_options"] = None
    state["status"] = "rework"
    task = _create_task(project, state, stage)
    state["history"].append({"stage": "rework", "completed_at": _now(), "summary": summary, "identity_change": identity_change})
    _save_state(project, state)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "rework",
        "concept_task": task,
        "invalidated": invalidated,
    }
