from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .core import MissingCapability, WorkflowError


def _inside(root: Path, value: Path) -> Path:
    resolved = value.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise WorkflowError(f"Aseprite path escapes the project root: {value}") from error
    return resolved


def aseprite_export(
    root: Path | str,
    source: Path | str,
    output_stem: Path | str,
    *,
    executable: str = "aseprite",
    apply: bool = False,
) -> dict[str, Any]:
    project = Path(root).resolve()
    source_path = _inside(project, Path(source) if Path(source).is_absolute() else project / source)
    output = _inside(
        project,
        Path(output_stem) if Path(output_stem).is_absolute() else project / output_stem,
    )
    if source_path.suffix.lower() not in {".ase", ".aseprite"}:
        raise WorkflowError("Aseprite source must use .ase or .aseprite")
    if not source_path.is_file():
        raise WorkflowError(f"Aseprite source does not exist: {source_path}")
    png_path = output.with_suffix(".png")
    data_path = output.with_suffix(".json")
    command = [
        executable,
        "--batch",
        str(source_path),
        "--sheet",
        str(png_path),
        "--data",
        str(data_path),
        "--format",
        "json-array",
        "--list-tags",
        "--list-slices",
    ]
    preview = {
        "schema_version": "1.0",
        "status": "dry_run" if not apply else "passed",
        "command": command,
        "outputs": [str(png_path.relative_to(project)), str(data_path.relative_to(project))],
    }
    if not apply:
        return preview
    resolved_executable = shutil.which(executable)
    if not resolved_executable:
        raise MissingCapability(["aseprite"], "Install or configure Aseprite separately, then retry")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [resolved_executable, *command[1:]],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise WorkflowError(result.stderr.strip() or "Aseprite batch export failed")
    if not png_path.is_file() or not data_path.is_file():
        raise WorkflowError("Aseprite did not produce both the PNG sheet and JSON metadata")
    try:
        metadata = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Aseprite metadata is invalid: {error}") from error
    if not isinstance(metadata, (dict, list)):
        raise WorkflowError("Aseprite metadata must be a JSON object or array")
    preview["sha256"] = {
        str(png_path.relative_to(project)).replace("\\", "/"): hashlib.sha256(png_path.read_bytes()).hexdigest(),
        str(data_path.relative_to(project)).replace("\\", "/"): hashlib.sha256(data_path.read_bytes()).hexdigest(),
    }
    return preview
