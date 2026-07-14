from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .automation import (
    get_automation_policy,
    initialize_staging,
    is_ai_staging,
    prepare_staging_merge,
    set_automation_mode,
)
from .cli import _doctor
from .concept import next_concept_task, validate_concept
from .context import build_context
from .core import choose_next
from .validation import validate_project
from .media import (
    benchmark_assets,
    compose_recipe,
    generate_assets,
    integrate_assets,
    next_asset_task,
    plan_assets,
    sample_assets,
    validate_assets,
)


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
    {
        "name": "aigame_concept_next",
        "title": "Return next concept task",
        "description": "Return the active resumable concept task or exact human gate.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "aigame_concept_validate",
        "title": "Validate concept blueprint",
        "description": "Validate concept schemas, named scope, mechanics, journey, profiles, roadmap, and approvals.",
        "inputSchema": {
            "type": "object",
            "properties": {"final": {"type": "boolean", "default": False}},
        },
    },
    {
        "name": "aigame_mode_get",
        "title": "Read automation mode",
        "description": "Return the canonical human-gated or AI-staging policy.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "aigame_mode_set",
        "title": "Change automation mode",
        "description": "Preview or apply an automation policy change.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"enum": ["human_gated", "ai_staging"]},
                "confirmation": {"type": "string"},
                "apply": {"type": "boolean", "default": False},
            },
            "required": ["mode"],
        },
    },
    {
        "name": "aigame_staging_init",
        "title": "Initialize staging",
        "description": "Preview or create the protected staging integration branch.",
        "inputSchema": {
            "type": "object",
            "properties": {"apply": {"type": "boolean", "default": False}},
        },
    },
    {
        "name": "aigame_staging_merge",
        "title": "Integrate pull request into staging",
        "description": "Validate checks and return the SHA-bound approval comment that only the authenticated repository owner may post.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pull_request": {"type": "integer", "minimum": 1},
                "apply": {"type": "boolean", "default": False},
            },
            "required": ["pull_request"],
        },
    },
    {
        "name": "aigame_assets_plan",
        "title": "Plan complete game media",
        "description": "Derive the exhaustive visual and audio inventory from the finalized blueprint.",
        "inputSchema": {"type": "object", "properties": {"apply": {"type": "boolean", "default": False}}},
    },
    {
        "name": "aigame_assets_next",
        "title": "Return next media operation",
        "description": "Return the deterministic media state and next valid operation.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "aigame_assets_sample",
        "title": "Generate representative media samples",
        "description": "Compile the style sample and apply human or AI-staging approval policy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "approval_path": {"type": "string"},
                "apply": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "aigame_assets_generate",
        "title": "Generate deterministic media",
        "description": "Generate one asset specification or the complete resumable batch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_spec_id": {"type": "string", "pattern": "^ASP-[0-9]{4}$"},
                "all": {"type": "boolean", "default": False},
                "jobs": {"type": "integer", "minimum": 1, "maximum": 64, "default": 1},
                "apply": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "aigame_assets_compose",
        "title": "Compose media recipe",
        "description": "Compile a seeded recipe in memory and return deterministic hashes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipe_id": {"type": "string", "pattern": "^RCP-[0-9]{4}$"},
                "seed": {"type": "integer"},
            },
            "required": ["recipe_id", "seed"],
        },
    },
    {
        "name": "aigame_assets_validate",
        "title": "Validate game media",
        "description": "Validate contracts, coverage, licenses, hashes, formats, and generated resources.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "aigame_assets_benchmark",
        "title": "Benchmark media factory",
        "description": "Measure the CPU-only sprite, sound, tile, runtime, and cache gates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sprite_count": {"type": "integer", "minimum": 1, "default": 500},
                "sound_count": {"type": "integer", "minimum": 1, "default": 500},
            },
        },
    },
    {
        "name": "aigame_assets_integrate",
        "title": "Integrate validated media",
        "description": "Write the checksum-bound Godot media registry after validation.",
        "inputSchema": {"type": "object", "properties": {"apply": {"type": "boolean", "default": False}}},
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
                "serverInfo": {"name": "aigame", "version": __version__},
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
            for path in sorted((root / "docs").rglob("*.md"))
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
                    {"name": "build_complete_game_concept", "description": "Complete one active concept task and follow the canonical automation policy."},
                    {"name": "generate_complete_game_media", "description": "Plan and generate the approved blueprint's complete deterministic visual and audio inventory."},
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
            capabilities = {
                key for key, enabled in _doctor(root)["capabilities"].items() if enabled
            }
            value = choose_next(
                items,
                capabilities,
                allow_red_unknowns=is_ai_staging(root),
            )
        elif name == "aigame_concept_next":
            value = next_concept_task(root)
        elif name == "aigame_concept_validate":
            value = validate_concept(root, require_final=bool(arguments.get("final", False)))
        elif name == "aigame_mode_get":
            value = get_automation_policy(root)
        elif name == "aigame_mode_set":
            value = set_automation_mode(
                root,
                str(arguments["mode"]),
                confirmation=arguments.get("confirmation"),
                apply=bool(arguments.get("apply", False)),
            )
        elif name == "aigame_staging_init":
            value = initialize_staging(root, apply=bool(arguments.get("apply", False)))
        elif name == "aigame_staging_merge":
            value = prepare_staging_merge(
                root,
                int(arguments["pull_request"]),
                apply=bool(arguments.get("apply", False)),
            )
        elif name == "aigame_assets_plan":
            value = plan_assets(root, apply=bool(arguments.get("apply", False)))
        elif name == "aigame_assets_next":
            value = next_asset_task(root)
        elif name == "aigame_assets_sample":
            value = sample_assets(
                root,
                approval_path=arguments.get("approval_path"),
                apply=bool(arguments.get("apply", False)),
            )
        elif name == "aigame_assets_generate":
            value = generate_assets(
                root,
                arguments.get("asset_spec_id"),
                all_assets=bool(arguments.get("all", False)),
                jobs=int(arguments.get("jobs", 1)),
                apply=bool(arguments.get("apply", False)),
            )
        elif name == "aigame_assets_compose":
            value = compose_recipe(
                root,
                str(arguments["recipe_id"]),
                seed=int(arguments["seed"]),
            )
        elif name == "aigame_assets_validate":
            value = validate_assets(root)
        elif name == "aigame_assets_benchmark":
            value = benchmark_assets(
                root,
                sprite_count=int(arguments.get("sprite_count", 500)),
                sound_count=int(arguments.get("sound_count", 500)),
            )
        elif name == "aigame_assets_integrate":
            value = integrate_assets(root, apply=bool(arguments.get("apply", False)))
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
