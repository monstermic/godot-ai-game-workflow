from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .cli import _doctor
from .context import build_context
from .core import choose_next
from .validation import validate_project


PROTOCOL_VERSION = "2025-06-18"


TOOLS = [
    {
        "name": "aigame_context",
        "title": "Build work-item context",
        "description": "Return the minimal canonical context packet for one work item.",
        "inputSchema": {
            "type": "object",
            "properties": {"work_item_id": {"type": "string", "pattern": "^WI-[0-9]{4}$"}},
            "required": ["work_item_id"],
        },
    },
    {
        "name": "aigame_next",
        "title": "Select next work item",
        "description": "Select the next ready work item using the deterministic scheduler.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "aigame_validate",
        "title": "Validate project",
        "description": "Validate schemas, traceability, active-slice state, and asset provenance.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _response(request: dict[str, Any], result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}


def handle_request(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method")
    if method == "initialize":
        requested = request.get("params", {}).get("protocolVersion", PROTOCOL_VERSION)
        version = PROTOCOL_VERSION if requested == PROTOCOL_VERSION else PROTOCOL_VERSION
        return _response(
            request,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "aigame", "version": "1.0.0"},
            },
        )
    if method == "tools/list":
        return _response(request, {"tools": TOOLS})
    if method == "resources/list":
        resources = [
            {
                "uri": path.resolve().as_uri(),
                "name": path.stem,
                "mimeType": "text/markdown",
            }
            for path in sorted((root / "docs").glob("*.md"))
        ]
        return _response(request, {"resources": resources})
    if method == "resources/read":
        uri = request.get("params", {}).get("uri", "")
        path = Path(uri.removeprefix("file:///")) if uri.startswith("file:///") else Path(uri)
        resolved = path.resolve()
        if root.resolve() not in resolved.parents and resolved != root.resolve():
            raise ValueError("Resource path escapes the project root")
        return _response(
            request,
            {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": resolved.read_text(encoding="utf-8")}]},
        )
    if method == "prompts/list":
        return _response(
            request,
            {
                "prompts": [
                    {"name": "implement_work_item", "description": "Implement exactly one claimed work item."},
                    {"name": "review_work_item", "description": "Independently review evidence for one work item."},
                ]
            },
        )
    if method == "tools/call":
        params = request.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == "aigame_validate":
            value = validate_project(root)
        elif name == "aigame_context":
            value = build_context(root, arguments["work_item_id"])
        elif name == "aigame_next":
            items = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((root / "work" / "items").glob("WI-*.json"))]
            capabilities = {key for key, enabled in _doctor()["capabilities"].items() if enabled}
            value = choose_next(items, capabilities)
        else:
            raise ValueError(f"Unknown MCP tool: {name}")
        return _response(
            request,
            {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value},
        )
    if method == "notifications/initialized":
        return {"jsonrpc": "2.0", "result": {}}
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve(root: Path | str = Path.cwd()) -> int:
    project = Path(root).resolve()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = handle_request(project, json.loads(line))
        except Exception as error:  # JSON-RPC boundary
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(error)}}
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
