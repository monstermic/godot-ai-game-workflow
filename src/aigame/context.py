from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_context(root: Path | str, work_item_id: str) -> dict[str, Any]:
    project = Path(root).resolve()
    item_path = project / "work" / "items" / f"{work_item_id}.json"
    if not item_path.is_file():
        raise FileNotFoundError(f"Unknown work item: {work_item_id}")
    item = _load(item_path)
    requirements = []
    for requirement_id in item.get("requirements", []):
        requirements.append(_load(project / "work" / "requirements" / f"{requirement_id}.json"))
    documents: dict[str, str] = {}
    for path in sorted((project / "docs").glob("*.md")):
        documents[path.stem] = path.read_text(encoding="utf-8")
    concept_records = []
    for concept_id in item.get("concept_refs", []):
        matches = sorted((project / "work" / "concept").rglob(f"{concept_id}.json"))
        if not matches:
            raise FileNotFoundError(f"Unknown concept record linked by {work_item_id}: {concept_id}")
        if len(matches) > 1:
            raise RuntimeError(f"Duplicate concept record path for {concept_id}")
        concept_records.append(_load(matches[0]))
    media_records = []
    media_spec_folder = project / "work" / "assets" / "specs"
    if media_spec_folder.is_dir():
        concept_refs = set(item.get("concept_refs", []))
        for path in sorted(media_spec_folder.glob("ASP-*.json")):
            spec = _load(path)
            if any(str(source).split(".", 1)[0] in concept_refs for source in spec.get("source_refs", [])):
                media_records.append(spec)
    return {
        "schema_version": "1.0",
        "status": "passed",
        "work_item": item,
        "requirements": requirements,
        "concept_records": concept_records,
        "media_records": media_records,
        "documents": documents,
    }
