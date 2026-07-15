from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import __version__
from .automation import default_automation_policy
from .core import fingerprint, workflow_snapshot_checksum


GODOT_RELEASE = "4.7-stable"
GODOT_LINUX_SHA512 = "b639ca9c1ddea39bb3df89bd5283a51ca6047467abe6b25e9436566f2b2082ede633025073989ecf39c7d5d3c2493d80ea13e3af6dd5e261bbf89e462d6d2214"
GODOT_WINDOWS_SHA512 = "41645a908eb3181d6f2d1201ed7b6d6f095f6a23aaed8903d5d255277cc8d142814f3e6817f865b3cac142c39b8aff99280091d3bbdaa301517730b3ba0522b9"
GODOT_TEMPLATES_SHA512 = "1035dfde4edcc2472bb0c0b9610ce3ee9302642c2b9957e9066372f9f6bb759ab250c8887551a66f0bc5f51bbd9a58bb45e33a0f29844e97615a9b1138c1120e"


def runtime_composer_source() -> str:
    return '''class_name AIGameRuntimeComposer
extends RefCounted

const GRID_SIZE := 16
const LAYER_SLOTS := [
    "shadow", "rear_effect", "rear_weapon", "body", "legs", "torso",
    "clothing", "armor", "head", "face", "hair", "front_weapon",
    "offhand", "front_effect"
]

var _cache: Dictionary = {}

func cache_key(recipe: Dictionary, seed: int, parts: Dictionary = {}) -> String:
    var context := HashingContext.new()
    context.start(HashingContext.HASH_SHA256)
    context.update((JSON.stringify(recipe) + ":" + str(seed)).to_utf8_buffer())
    var part_ids: Array = parts.keys()
    part_ids.sort()
    for part_id in part_ids:
        context.update(("|" + str(part_id) + "|").to_utf8_buffer())
        var source = parts[part_id]
        var part_image: Image
        if source is Texture2D:
            part_image = source.get_image()
        elif source is Image:
            part_image = source
        elif source is Dictionary:
            context.update(JSON.stringify(source).to_utf8_buffer())
        else:
            context.update(str(source).to_utf8_buffer())
        if part_image != null:
            context.update(
                (str(part_image.get_width()) + "x" + str(part_image.get_height()) + ":" + str(part_image.get_format())).to_utf8_buffer()
            )
            context.update(part_image.get_data())
    return context.finish().hex_encode()

func get_cached(recipe: Dictionary, seed: int, parts: Dictionary = {}) -> Texture2D:
    return _cache.get(cache_key(recipe, seed, parts))

func compose(recipe: Dictionary, parts: Dictionary, seed: int) -> Texture2D:
    var key := cache_key(recipe, seed, parts)
    if _cache.has(key):
        return _cache[key]
    var parameters: Dictionary = recipe.get("parameters", {})
    var dimensions: Array = parameters.get("frame_dimensions", [GRID_SIZE, GRID_SIZE])
    var output_size := Vector2i(int(dimensions[0]), int(dimensions[1]))
    if output_size.x < GRID_SIZE or output_size.y < GRID_SIZE or output_size.x > 128 or output_size.y > 128 or output_size.x % GRID_SIZE != 0 or output_size.y % GRID_SIZE != 0:
        push_error("AIGame media recipe has unsupported frame dimensions %s" % output_size)
        return null
    var image := Image.create(GRID_SIZE, GRID_SIZE, false, Image.FORMAT_RGBA8)
    image.fill(Color.TRANSPARENT)
    var palette: Dictionary = parameters.get("palette", recipe.get("palette", {}))
    var direction: String = recipe.get("direction", parameters.get("direction", "down"))
    var layers: Array = recipe.get("layers", parameters.get("layers", []))
    var layer_choices: Dictionary = parameters.get("layer_choices", {})
    if not layer_choices.is_empty():
        layers = []
        for slot_index in range(LAYER_SLOTS.size()):
            var slot: String = LAYER_SLOTS[slot_index]
            var options: Array = layer_choices.get(slot, [])
            if not options.is_empty():
                layers.append({"slot": slot, "part_id": options[(seed + slot_index * 17) % options.size()], "offset": [0, 0]})
    for slot in LAYER_SLOTS:
        for layer in layers:
            if layer.get("slot", "") != slot:
                continue
            var part_id: String = layer.get("part_id", "")
            if not parts.has(part_id):
                push_error("AIGame media recipe references missing part %s" % part_id)
                continue
            var source = parts[part_id]
            var part_image: Image
            if source is Texture2D:
                part_image = source.get_image()
            elif source is Image:
                part_image = source
            elif source is Dictionary:
                part_image = _part_image(source, palette, seed, direction)
            if part_image == null:
                continue
            var offset_value: Array = layer.get("offset", [0, 0])
            var offset := Vector2i(int(offset_value[0]), int(offset_value[1]))
            image.blend_rect(part_image, Rect2i(Vector2i.ZERO, part_image.get_size()), offset)
    if output_size != Vector2i(GRID_SIZE, GRID_SIZE):
        image.resize(output_size.x, output_size.y, Image.INTERPOLATE_NEAREST)
    var texture := ImageTexture.create_from_image(image)
    _cache[key] = texture
    return texture

func _part_image(part: Dictionary, palette: Dictionary, seed: int, direction: String) -> Image:
    var image := Image.create(GRID_SIZE, GRID_SIZE, false, Image.FORMAT_RGBA8)
    image.fill(Color.TRANSPARENT)
    var pixels: Array = part.get("pixels", [])
    var direction_pixels = part.get("direction_pixels")
    if direction_pixels is Dictionary and direction_pixels.has(direction):
        pixels = direction_pixels[direction]
    for pixel in pixels:
        if pixel.size() != 3:
            continue
        var color_name: String = str(pixel[2])
        if color_name == "primary":
            color_name = ["primary", "safe", "danger", "secondary"][seed % 4]
        elif color_name == "secondary":
            color_name = ["secondary", "light", "primary", "safe"][int(seed / 4) % 4]
        elif color_name == "dark":
            color_name = ["dark", "mid", "outline", "shadow"][int(seed / 16) % 4]
        var color_value: String = palette.get(color_name, "#00000000")
        var x := int(pixel[0])
        if direction == "left" and bool(part.get("mirror_safe", false)):
            x = GRID_SIZE - 1 - x
        image.set_pixel(x, int(pixel[1]), Color(color_value))
    return image

func clear_cache() -> void:
    _cache.clear()

func cache_size() -> int:
    return _cache.size()
'''


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8", newline="\n")


def _record(value: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    value.setdefault("created_at", now)
    value.setdefault("updated_at", value["created_at"])
    material = {key: item for key, item in value.items() if key != "input_fingerprint"}
    return {**value, "input_fingerprint": fingerprint(material)}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "game"


def _workflow_commit() -> str:
    configured = os.environ.get("AIGAME_WORKFLOW_COMMIT", "")
    if re.fullmatch(r"[0-9a-f]{40}", configured):
        return configured
    distribution_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "-C", str(distribution_root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    candidate = result.stdout.strip()
    return candidate if re.fullmatch(r"[0-9a-f]{40}", candidate) else "0" * 40


def create_game(
    destination: Path | str,
    name: str,
    *,
    godot_version: str,
    apply: bool = False,
) -> dict[str, Any]:
    root = Path(destination).resolve()
    planned = [
        "AGENTS.md",
        ".aigame/project.toml",
        ".aigame/automation.json",
        ".aigame/workflow.lock.json",
        ".aigame/state/concept.json",
        ".aigame/state/assets.json",
        ".aigame/media.toml",
        ".aigame/profiles/core-game-v1.json",
        ".aigame/profiles/roguelite-v1.json",
        "project.godot",
        "work/requirements/REQ-0001.json",
        "work/items/WI-0001.json",
        "assets/asset-manifest.json",
    ]
    if not apply:
        return {"schema_version": "1.0", "status": "dry_run", "root": str(root), "files": planned}
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Destination is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)

    workflow_commit = _workflow_commit()

    project_toml = f'''schema_version = "1.0"
revision = 1
name = "{name.replace('"', '')}"
slug = "{_slug(name)}"
visibility = "private"
source_license = "proprietary"
workflow_version = "{__version__}"
current_milestone = "bootstrap"
godot_version = "{godot_version}"
godot_release = "{GODOT_RELEASE}"
godot_linux_sha512 = "{GODOT_LINUX_SHA512}"
godot_windows_sha512 = "{GODOT_WINDOWS_SHA512}"
godot_templates_sha512 = "{GODOT_TEMPLATES_SHA512}"
renderer = "gl_compatibility"
primary_target = "windows-x86_64"
serial_active_slice = true

capability_packs = ["core-2d", "pixel-media-v1"]
available_capability_packs = ["3d", "narrative", "localization", "mobile", "persistence", "networking"]
allowed_commands = ["python", "git", "gh", "godot"]

[budgets]
target_fps = 60
target_width = 1920
target_height = 1080
max_binary_mb = 50

[risk]
human_approval = ["vision", "scope", "dependencies", "paid-api", "model-download", "secrets", "save-migration", "networking", "destructive", "bulk-media", "merge", "release", "rollback"]
'''
    _text(root / ".aigame" / "project.toml", project_toml)
    _json(root / ".aigame" / "automation.json", default_automation_policy())
    declared_packs = {
        "3d": "3D scenes, meshes, lighting, physics, and performance budgets",
        "narrative": "branching narrative structures, dialogue, and content validation",
        "localization": "translation catalogs, locale QA, and text expansion budgets",
        "mobile": "touch input, mobile exports, device matrices, and store packaging",
        "persistence": "versioned save formats, migrations, backup, and recovery",
        "networking": "authority models, protocol compatibility, abuse controls, and online tests",
    }
    for pack_id, summary in declared_packs.items():
        _text(
            root / ".aigame" / "capability-packs" / f"{pack_id}.toml",
            f'''schema_version = "1.0"
id = "{pack_id}"
status = "declared"
implemented = false
summary = "{summary}"
activation = "requires a workflow upgrade, capability detection, tests, and human approval"
''',
        )
    workflow_lock = {
        "schema_version": "1.0",
        "workflow_version": __version__,
        "source": "monstermic/godot-ai-game-workflow",
        "source_commit": workflow_commit,
        "source_sha256": "",
    }
    _json(
        root / ".aigame" / "state" / "current.json",
        {
            "schema_version": "1.0",
            "active_work_item": None,
            "active_branch": None,
            "last_checkpoint": None,
        },
    )
    _json(
        root / ".aigame" / "state" / "concept.json",
        {
            "schema_version": "1.0",
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
        },
    )
    _json(
        root / ".aigame" / "state" / "assets.json",
        {
            "schema_version": "1.0",
            "revision": 1,
            "status": "unplanned",
            "asset_plan_id": None,
            "style_pack_id": None,
            "style_approval_id": None,
            "active_batch": None,
            "history": [],
        },
    )
    _text(
        root / ".aigame" / "media.toml",
        '''schema_version = "1.0"
revision = 1
grid_size = 16
max_output_dimension = 128
perspective = "top_down"
directions = ["down", "left", "right", "up"]
runtime_generation = "build_and_runtime"
image_format = "png"
audio_sample_rate = 48000
pillow_version = "12.3.0"
numpy_version = "2.4.6"
sprite_batch_budget_seconds = 10.0
sound_batch_budget_seconds = 15.0
runtime_compose_budget_ms = 50.0
cached_lookup_budget_ms = 1.0
tile_atlas_budget_seconds = 1.0
''',
    )
    _text(
        root / ".aigame" / "capability-packs" / "pixel-media-v1.toml",
        '''schema_version = "1.0"
id = "pixel-media-v1"
status = "implemented"
implemented = true
summary = "Structured, fail-closed deterministic 16-128 px sprites, animations, tile sets, particles, UI, sound effects, ambience, and adaptive loops"
activation = "aigame assets plan --apply --json"
''',
    )
    _text(
        root / "AGENTS.md",
        """# Agent entrypoint

Read `.aigame/project.toml`, then run `aigame doctor --json` and `aigame next --json`.
Read `.aigame/automation.json` before making any approval or integration decision.
When concept work has not started, run `aigame concept start --prompt-file <file> --apply`, then
repeat `aigame concept next --json` and schema-valid `aigame concept submit` operations.
In `human_gated` mode stop for direction, product identity, and blueprint approval. In
`ai_staging` mode make those decisions, retain the generated agent approvals, and continue. At
`commit_blueprint_inputs`, commit the canonical concept records before running concept finalization.
Do not write implementation code until concept state is `finalized`.
After concept finalization, run `aigame assets plan --apply --json` and complete the asset workflow
before implementing work that depends on final media.
Never push directly to `main`, approve your own independent review, or publish a release.
Before changing files, claim exactly one work item with `aigame claim WI-#### --apply`.
Use `aigame context WI-#### --json`, implement only that packet, run `aigame validate WI-####`,
and save resumable evidence with `aigame checkpoint WI-#### --result run-result.json --apply`.
AI-mode pull requests target `staging`. `aigame staging merge` returns an exact approval comment;
only the authenticated repository owner may post it. The agent never posts or bypasses that gate.
Production, release, and rollback remain separately protected.
All mutating workflow commands are previews unless `--apply` is supplied.
""",
    )
    _text(root / "CLAUDE.md", "Read and follow `AGENTS.md`; it is the canonical agent contract.")
    _text(root / ".github" / "copilot-instructions.md", "Read and follow `AGENTS.md`; it is canonical.")
    _text(root / ".cursor" / "rules" / "aigame.mdc", "---\nalwaysApply: true\n---\nRead and follow `AGENTS.md`.")
    schema_source = files("aigame.schemas")
    for schema_entry in schema_source.iterdir():
        if not schema_entry.name.endswith(".json"):
            continue
        _text(
            root / ".aigame" / "schemas" / schema_entry.name,
            schema_entry.read_text(encoding="utf-8"),
        )
    profile_source = files("aigame.profiles")
    for profile_entry in profile_source.iterdir():
        if not profile_entry.name.endswith(".json"):
            continue
        _text(
            root / ".aigame" / "profiles" / profile_entry.name,
            profile_entry.read_text(encoding="utf-8"),
        )
    package_source = files("aigame")
    for module in package_source.iterdir():
        if module.name.endswith(".py"):
            _text(
                root / ".aigame" / "vendor" / "aigame" / module.name,
                module.read_text(encoding="utf-8"),
            )
    _text(root / ".aigame" / "vendor" / "aigame" / "schemas" / "__init__.py", '"""Vendored schemas."""')
    for schema_entry in schema_source.iterdir():
        if schema_entry.name.endswith(".json"):
            _text(
                root / ".aigame" / "vendor" / "aigame" / "schemas" / schema_entry.name,
                schema_entry.read_text(encoding="utf-8"),
            )
    _text(root / ".aigame" / "vendor" / "aigame" / "profiles" / "__init__.py", '"""Vendored quality profiles."""')
    for profile_entry in profile_source.iterdir():
        if profile_entry.name.endswith(".json"):
            _text(
                root / ".aigame" / "vendor" / "aigame" / "profiles" / profile_entry.name,
                profile_entry.read_text(encoding="utf-8"),
            )
    workflow_lock["source_sha256"] = workflow_snapshot_checksum(
        root / ".aigame" / "vendor" / "aigame"
    )
    _json(root / ".aigame" / "workflow.lock.json", workflow_lock)
    _text(
        root / ".github" / "workflows" / "quality.yml",
        f'''name: Quality

on:
  pull_request:
  pull_request_review:
    types: [submitted, dismissed]
  push:
    branches: [main, staging]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  contract:
    uses: monstermic/godot-ai-game-workflow/.github/workflows/contract.yml@{workflow_commit}
    with:
      project-path: .
  godot-quality:
    uses: monstermic/godot-ai-game-workflow/.github/workflows/godot-quality.yml@{workflow_commit}
    with:
      project-path: .
  asset-provenance:
    uses: monstermic/godot-ai-game-workflow/.github/workflows/asset-provenance.yml@{workflow_commit}
    with:
      project-path: .
  independent-review:
    if: github.event_name == 'pull_request' || github.event_name == 'pull_request_review'
    permissions:
      contents: read
      pull-requests: read
    uses: monstermic/godot-ai-game-workflow/.github/workflows/independent-review.yml@{workflow_commit}
''',
    )
    _text(
        root / ".github" / "workflows" / "github-sync.yml",
        f'''name: GitHub Sync
on:
  push:
    branches: [main]
    paths: ['work/**']
permissions:
  contents: read
  issues: write
jobs:
  sync:
    uses: monstermic/godot-ai-game-workflow/.github/workflows/github-sync.yml@{workflow_commit}
    with:
      project-path: .
''',
    )
    _text(
        root / ".github" / "workflows" / "intake-proposal.yml",
        f'''name: Intake Proposal
on:
  issues:
    types: [opened]
permissions:
  contents: write
  pull-requests: write
jobs:
  proposal:
    uses: monstermic/godot-ai-game-workflow/.github/workflows/intake-proposal.yml@{workflow_commit}
''',
    )
    _text(
        root / ".github" / "workflows" / "release-candidate.yml",
        f'''name: Release Candidate
on:
  workflow_dispatch:
    inputs:
      name:
        description: Immutable candidate name such as v0.1.0-rc.1
        required: true
        type: string
permissions:
  contents: write
  id-token: write
  attestations: write
jobs:
  candidate:
    uses: monstermic/godot-ai-game-workflow/.github/workflows/release-candidate.yml@{workflow_commit}
    with:
      project-path: .
      name: ${{{{ inputs.name }}}}
''',
    )
    _text(
        root / ".github" / "workflows" / "staging-merge.yml",
        f'''name: Staging Merge Approval

on:
  issue_comment:
    types: [created]

permissions:
  contents: write
  pull-requests: write
  statuses: write

jobs:
  merge:
    if: github.event.issue.pull_request && startsWith(github.event.comment.body, '/aigame merge staging ')
    uses: monstermic/godot-ai-game-workflow/.github/workflows/staging-merge.yml@{workflow_commit}
    with:
      pull-request: ${{{{ github.event.issue.number }}}}
      actor: ${{{{ github.actor }}}}
      comment-body: ${{{{ github.event.comment.body }}}}
''',
    )
    _text(
        root / ".github" / "workflows" / "release.yml",
        f'''name: Release
on:
  workflow_dispatch:
    inputs:
      name:
        required: true
        type: string
      artifact-sha256:
        required: true
        type: string
permissions:
  contents: write
jobs:
  release:
    uses: monstermic/godot-ai-game-workflow/.github/workflows/release.yml@{workflow_commit}
    with:
      name: ${{{{ inputs.name }}}}
      artifact-sha256: ${{{{ inputs.artifact-sha256 }}}}
''',
    )
    _text(
        root / ".github" / "PULL_REQUEST_TEMPLATE.md",
        """## Work item

Closes WI-

## Player value and changes

## Evidence

- [ ] `aigame validate` passes
- [ ] Godot tests and export pass
- [ ] Asset provenance is complete or not applicable
- [ ] Independent reviewer is not the producer
- [ ] Human playtest is attached or not applicable
- [ ] Save/performance/accessibility impacts are documented

## Integration gate

Human-gated projects are merged by the owner. In AI-staging projects, `aigame staging merge` returns an exact SHA-bound approval comment. Only the authenticated repository owner may post it; GitHub then revalidates checks and sets the required Actions-App-bound status before squash auto-merge.
""",
    )
    _text(root / "LICENSE", "Copyright (c) Game Owner. All rights reserved.\n\nNo license is granted except by written permission.")
    _text(
        root / "LICENSES" / "workflow-Apache-2.0.txt",
        "The embedded AI Game Workflow tooling is licensed under Apache License 2.0.\nSee https://www.apache.org/licenses/LICENSE-2.0",
    )
    _text(
        root / "LICENSES" / "core-pixel-media-CC0-1.0.txt",
        "The original core-topdown-v1 procedural coordinates and their unmodified deterministic outputs are dedicated under CC0 1.0.\nSee https://creativecommons.org/publicdomain/zero/1.0/\nOutputs composed with project-supplied parts retain the license recorded in asset-manifest.json.",
    )
    _text(root / "docs" / "game_brief.md", f"# {name}\n\nStatus: concept blueprint required.\n")
    _text(root / "docs" / "game_design.md", "# Game design\n\nDefine the player fantasy, pillars, and core loop.")
    _text(root / "docs" / "technical_design.md", "# Technical design\n\nGodot 4, GDScript, offline 2D baseline.")
    _text(root / "docs" / "creative_bible.md", "# Creative bible\n\nPlaceholders only until the vertical-slice style gate.")
    _text(
        root / "docs" / "blueprint" / "README.md",
        "# Generated blueprint\n\nThese files are rendered deterministically from `work/concept/`. Do not edit them by hand.",
    )
    _text(root / "docs" / "playtests" / "README.md", "# Playtests\n\nHuman evidence for subjective claims belongs here.")

    requirement = _record(
        {
            "schema_version": "1.0",
            "id": "REQ-0001",
            "revision": 1,
            "status": "draft",
            "title": "Approve the complete game blueprint and scope",
            "player_value": "An implementation-ready, named, beginning-to-ending game contract.",
            "acceptance_criteria": ["The owner approves the complete blueprint, launch catalog, mechanics, roadmap, and game Definition of Done."],
            "playtest_hypotheses": [],
            "non_goals": ["Production implementation before discovery approval."],
        }
    )
    item = _record(
        {
            "schema_version": "1.0",
            "id": "WI-0001",
            "revision": 1,
            "status": "ready",
            "title": "Create complete game blueprint from user prompt",
            "type": "concept_blueprint",
            "milestone": "bootstrap",
            "requirements": ["REQ-0001"],
            "dependencies": [],
            "release_blocker": False,
            "risk": 5,
            "unblocks": 1,
            "player_value": 5,
            "estimate": 1,
            "required_capabilities": ["filesystem", "shell", "git"],
            "unknowns": [],
            "scope": ["work/concept", "docs/blueprint"],
            "tests": ["aigame concept validate --json", "aigame validate WI-0001"],
            "playtest_required": False,
            "definition_of_done": ["Direction, product identity, and complete blueprint approvals are recorded and current."],
        }
    )
    _json(root / "work" / "requirements" / "REQ-0001.json", requirement)
    _json(root / "work" / "items" / "WI-0001.json", item)
    _text(
        root / "work" / "concept" / "README.md",
        "# Concept records\n\nJSON records in this directory are canonical. Use `aigame concept` commands to create or revise them.",
    )
    _text(
        root / "work" / "assets" / "README.md",
        "# Asset records\n\nCanonical asset plans, specifications, style packs, parts, recipes, animation sets, tile sets, particles, and sounds live here.",
    )
    _text(
        root / "docs" / "assets" / "README.md",
        "# Generated asset documentation\n\nRendered from canonical `work/assets/` records.",
    )
    _text(
        root / ".aigame" / "agent-skills" / "build-game-concept" / "SKILL.md",
        """---
name: build-game-concept
description: Turn a user game prompt into the complete portable aigame blueprint before implementation.
---

# Build Game Concept

Read the generated `AGENTS.md` and `.aigame/automation.json`. Drive concept work through the
pinned vendored workflow and produce one schema-valid result at a time. In `human_gated` mode,
stop at every approval and use the selected pitch's exact request. In `ai_staging` mode, make the
creative decisions and accept CLI-generated agent approvals. At `commit_blueprint_inputs`, commit
the canonical concept records, run `aigame concept next --json`, then run concept finalization.
After finalization, complete one work item and target `staging`. Run `aigame staging merge` to obtain
the exact approval comment, then stop for the authenticated repository owner to post it. The agent
must not post that comment. Independent review remains mandatory.
Never bypass production, release, rollback, secrets, paid services, or destructive external gates.
""",
    )
    _text(
        root / ".aigame" / "agent-skills" / "generate-game-assets" / "SKILL.md",
        '''---
name: generate-game-assets
description: Plan, generate, validate, and integrate every deterministic visual and audio asset required by an approved aigame blueprint.
---

# Generate Game Assets

Read `AGENTS.md`, `.aigame/media.toml`, `.aigame/automation.json`, and
`.aigame/state/assets.json`. Run `aigame doctor --project . --json`, then `aigame assets next
--project . --json` and perform only the returned operation. Preview every mutation before
`--apply`; validate and query `next` again after every applied step. Human-gated mode stops at the
representative style approval. AI-staging mode lets the CLI record a bound agent approval. Resume
from cache receipts and canonical state rather than restarting. Treat source packs and adapter
output as untrusted until license, provenance, checksum, palette, path, and Godot runtime validation
pass. Never call a paid provider, install dependencies, download models, or bypass independent
review, staging-owner approval, production, release, rollback, secrets, or destructive gates.
''',
    )
    source_pack = {
        "schema_version": "1.0",
        "id": "core-topdown-v1",
        "name": "Core Top-down Pixel Parts (16-128 px outputs)",
        "pack_revision": 2,
        "license": "CC0-1.0",
        "grid_size": 16,
        "perspective": "top_down",
        "directions": ["down", "left", "right", "up"],
        "body_families": ["humanoid", "serpentine", "quadruped", "winged", "amorphous", "mechanical_vehicle"],
        "parts": [],
        "provenance": "Original procedural coordinates; no LPC or third-party artwork is bundled.",
    }
    source_pack["sha256"] = fingerprint(source_pack)
    _json(
        root / "assets" / "source" / "packs" / "core-topdown-v1" / "pack.json",
        source_pack,
    )
    _text(
        root / "addons" / "aigame_media" / "runtime_composer.gd",
        runtime_composer_source(),
    )
    _text(
        root / "work" / "decisions" / "README.md",
        "# Decisions\n\nUse `DEC-####.json` records for durable design and technical choices. Red decisions require a bound approval.",
    )
    _text(
        root / "work" / "experiments" / "README.md",
        "# Experiments\n\nUse `EXP-####.json` records for falsifiable gameplay and technical hypotheses.",
    )
    _text(
        root / "work" / "milestones" / "README.md",
        "# Milestones\n\nKeep the current milestone detailed and future milestones intentionally coarse.",
    )
    _json(
        root / "assets" / "asset-manifest.json",
        {"schema_version": "1.0", "revision": 1, "assets": []},
    )
    _text(root / "evidence" / "README.md", "# Evidence\n\nOnly checksum-bound test, review, playtest, approval, and release evidence belongs here.")

    _text(
        root / ".gitignore",
        """.godot/
*.translation
.aigame/cache/
.aigame/runs/
builds/
export_credentials.cfg
.env
.env.*
""",
    )
    _text(
        root / ".gitattributes",
        """* text=auto eol=lf
*.png binary
*.jpg binary
*.ogg binary
*.glb binary
""",
    )
    _text(
        root / "project.godot",
        f'''[application]
config/name="{name.replace('"', '')}"
run/main_scene="res://game/main.tscn"

[display]
window/size/viewport_width=960
window/size/viewport_height=540
window/size/window_width_override=1280
window/size/window_height_override=720

[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"
textures/canvas_textures/default_texture_filter=0
textures/default_filters/use_nearest_mipmap_filter=false

[editor]
import/use_multiple_threads=false

[input]
move_left={{"deadzone":0.5,"events":[Object(InputEventKey,"physical_keycode":65)]}}
move_right={{"deadzone":0.5,"events":[Object(InputEventKey,"physical_keycode":68)]}}
move_up={{"deadzone":0.5,"events":[Object(InputEventKey,"physical_keycode":87)]}}
move_down={{"deadzone":0.5,"events":[Object(InputEventKey,"physical_keycode":83)]}}
''',
    )
    _text(
        root / "game" / "main.tscn",
        """[gd_scene load_steps=2 format=3]

[ext_resource path="res://game/main.gd" type="Script" id="1"]

[node name="Main" type="Node2D"]
script = ExtResource("1")
""",
    )
    _text(
        root / "game" / "game_state.gd",
        """class_name ReferenceGameState
extends RefCounted

const SPEED := 240.0
const START := Vector2(96, 270)
const ARENA := Rect2(32, 64, 896, 444)

var player_position := START
var goal_position := Vector2(864, 270)
var enemy_position := Vector2(480, 270)
var lives := 3
var score := 0
var completed := false
var failed := false

func move_player(direction: Vector2, delta: float) -> void:
    if completed or failed:
        return
    player_position += direction.normalized() * SPEED * delta
    player_position.x = clampf(player_position.x, ARENA.position.x, ARENA.end.x)
    player_position.y = clampf(player_position.y, ARENA.position.y, ARENA.end.y)

func evaluate_collisions() -> void:
    if completed or failed:
        return
    if player_position.distance_to(goal_position) <= 28.0:
        completed = true
        score += 100
        return
    if player_position.distance_to(enemy_position) <= 28.0:
        lives -= 1
        if lives <= 0:
            failed = true
        else:
            player_position = START

func restart() -> void:
    player_position = START
    lives = 3
    score = 0
    completed = false
    failed = false
""",
    )
    _text(
        root / "game" / "main.gd",
        """extends Node2D

var state := ReferenceGameState.new()

func _process(delta: float) -> void:
    if Input.is_key_pressed(KEY_R) and (state.completed or state.failed):
        state.restart()
    var direction := Input.get_vector("move_left", "move_right", "move_up", "move_down")
    state.move_player(direction, delta)
    state.evaluate_collisions()
    queue_redraw()

func _draw() -> void:
    draw_rect(ReferenceGameState.ARENA, Color("172033"))
    draw_circle(state.goal_position, 24, Color("f7c948"))
    draw_circle(state.enemy_position, 24, Color("ef476f"))
    draw_rect(Rect2(state.player_position - Vector2(16, 16), Vector2(32, 32)), Color("55d6be"))
    var message := "Reach gold · avoid red · WASD/Arrows | Lives %d | Score %d" % [state.lives, state.score]
    if state.completed:
        message = "You win! Reward +100 · Press R to restart"
    elif state.failed:
        message = "Game over · Press R to restart"
    draw_string(ThemeDB.fallback_font, Vector2(24, 36), message, HORIZONTAL_ALIGNMENT_LEFT, -1, 22, Color.WHITE)
""",
    )
    _text(
        root / "tests" / "run_tests.gd",
        """extends SceneTree

var failures := 0

func expect(condition: bool, message: String) -> void:
    if not condition:
        failures += 1
        push_error(message)

func _initialize() -> void:
    var GameState = load("res://game/game_state.gd")
    expect(GameState != null, "game state script loads")
    if GameState != null:
        var state = GameState.new()
        var start = state.player_position
        state.move_player(Vector2.RIGHT, 0.5)
        expect(state.player_position.x > start.x, "player moves right")
        state.player_position = state.goal_position
        state.evaluate_collisions()
        expect(state.completed and state.score == 100, "goal wins and grants reward")
        state.restart()
        expect(not state.completed and state.score == 0, "restart resets")
        state.lives = 1
        state.player_position = state.enemy_position
        state.evaluate_collisions()
        expect(state.failed, "last enemy collision loses")
    print("AIGAME_TESTS failures=%d" % failures)
    quit(failures)
""",
    )
    _text(
        root / "export_presets.cfg",
        """[preset.0]
name="Windows Desktop"
platform="Windows Desktop"
runnable=true
export_filter="all_resources"
include_filter=""
exclude_filter=""
export_path="builds/reference-game.exe"

[preset.0.options]
binary_format/architecture="x86_64"
""",
    )
    return {"schema_version": "1.0", "status": "passed", "root": str(root), "files": planned}


def initialize_game(
    root: Path | str,
    name: str,
    *,
    godot_version: str,
    apply: bool = False,
) -> dict[str, Any]:
    project = Path(root).resolve()
    allowed_existing = {".git"}
    existing = {path.name for path in project.iterdir()} if project.exists() else set()
    unexpected = existing - allowed_existing
    if unexpected:
        raise FileExistsError(f"Project is not empty: {sorted(unexpected)}")
    if not apply:
        return {
            "schema_version": "1.0",
            "status": "dry_run",
            "root": str(project),
            "preserved": sorted(existing),
        }
    project.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aigame-init-") as temporary:
        generated = Path(temporary) / "game"
        create_game(generated, name, godot_version=godot_version, apply=True)
        for source in generated.iterdir():
            destination = project / source.name
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
    return {
        "schema_version": "1.0",
        "status": "passed",
        "root": str(project),
        "preserved": sorted(existing),
    }
