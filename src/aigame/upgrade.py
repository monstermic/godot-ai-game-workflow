from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import __version__
from .automation import default_automation_policy
from .core import fingerprint, workflow_snapshot_checksum
from .generator import runtime_composer_source


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8", newline="\n")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_text(path, json.dumps(value, indent=2, ensure_ascii=False))


def _staging_caller(commit: str) -> str:
    return f'''name: Staging Merge Approval

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
    uses: monstermic/godot-ai-game-workflow/.github/workflows/staging-merge.yml@{commit}
    with:
      pull-request: ${{{{ github.event.issue.number }}}}
      actor: ${{{{ github.actor }}}}
      comment-body: ${{{{ github.event.comment.body }}}}
'''


def _install_plan(project: Path, source_root: Path) -> list[str]:
    paths = [
        ".aigame/automation.json",
        ".aigame/state/concept.json",
        ".aigame/state/assets.json",
        ".aigame/media.toml",
        ".aigame/agent-skills/build-game-concept/SKILL.md",
        ".aigame/agent-skills/generate-game-assets/SKILL.md",
        ".aigame/capability-packs/pixel-media-v1.toml",
        "assets/source/packs/core-topdown-v1/pack.json",
        "addons/aigame_media/runtime_composer.gd",
        "work/assets/README.md",
        "docs/assets/README.md",
        "LICENSES/core-pixel-media-CC0-1.0.txt",
        ".github/workflows/staging-merge.yml",
    ]
    for source in sorted(source_root.glob("*.py")):
        paths.append(f".aigame/vendor/aigame/{source.name}")
    for folder in ("schemas", "profiles"):
        for source in sorted((source_root / folder).glob("*")):
            if source.is_file() and (source.suffix in {".json", ".py"}):
                paths.append(f".aigame/{folder}/{source.name}")
                paths.append(f".aigame/vendor/aigame/{folder}/{source.name}")
    return sorted(set(paths))


def upgrade_workflow(
    root: Path | str,
    version: str,
    commit: str,
    source_sha256: str,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise ValueError("Workflow version must be semantic version syntax")
    if version != __version__:
        raise ValueError(
            f"Installed aigame is {__version__}; it cannot install workflow snapshot {version}"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Workflow commit must be a full 40-character SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("Workflow source checksum must be SHA-256")

    project = Path(root).resolve()
    source_root = Path(__file__).resolve().parent
    actual_source_checksum = workflow_snapshot_checksum(source_root)
    if source_sha256 != actual_source_checksum:
        raise ValueError(
            "Requested source checksum does not match the installed workflow snapshot"
        )
    lock_path = project / ".aigame" / "workflow.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    before = lock.copy()
    lock.update(
        {
            "workflow_version": version,
            "source_commit": commit,
            "source_sha256": source_sha256,
        }
    )
    install = _install_plan(project, source_root)
    if not apply:
        return {
            "schema_version": "1.0",
            "status": "dry_run",
            "path": str(lock_path),
            "before": before,
            "after": lock,
            "install": install,
            "preserved": ["docs/**", "work/**", "assets/**"],
        }

    vendor_root = project / ".aigame" / "vendor" / "aigame"
    for source in sorted(source_root.glob("*.py")):
        _write_text(vendor_root / source.name, source.read_text(encoding="utf-8"))
    for folder in ("schemas", "profiles"):
        source_folder = source_root / folder
        for source in sorted(source_folder.glob("*")):
            if not source.is_file() or source.suffix not in {".json", ".py"}:
                continue
            content = source.read_text(encoding="utf-8")
            _write_text(project / ".aigame" / folder / source.name, content)
            _write_text(vendor_root / folder / source.name, content)

    automation_path = project / ".aigame" / "automation.json"
    if not automation_path.is_file():
        _write_json(automation_path, default_automation_policy())
    else:
        automation = json.loads(automation_path.read_text(encoding="utf-8"))
        protections = list(automation.get("protected_gates", []))
        for protection in ("dependencies", "paid_providers", "model_downloads"):
            if protection not in protections:
                protections.append(protection)
        if protections != automation.get("protected_gates", []):
            automation["protected_gates"] = protections
            automation["revision"] = int(automation.get("revision", 0)) + 1
            automation.pop("input_fingerprint", None)
            automation["input_fingerprint"] = fingerprint(automation)
            _write_json(automation_path, automation)

    concept_state_path = project / ".aigame" / "state" / "concept.json"
    if not concept_state_path.is_file():
        _write_json(
            concept_state_path,
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

    media_state_path = project / ".aigame" / "state" / "assets.json"
    if not media_state_path.is_file():
        _write_json(
            media_state_path,
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
    media_config_path = project / ".aigame" / "media.toml"
    if not media_config_path.is_file():
        _write_text(
            media_config_path,
            '''schema_version = "1.0"
revision = 1
grid_size = 16
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
    pixel_pack_path = project / ".aigame" / "capability-packs" / "pixel-media-v1.toml"
    if not pixel_pack_path.is_file():
        _write_text(
            pixel_pack_path,
            '''schema_version = "1.0"
id = "pixel-media-v1"
status = "implemented"
implemented = true
summary = "Deterministic 16x16 visual and 48 kHz procedural-audio media factory"
activation = "aigame assets plan --apply --json"
''',
        )

    concept_readme = project / "work" / "concept" / "README.md"
    if not concept_readme.is_file():
        _write_text(
            concept_readme,
            "# Concept records\n\nCanonical JSON records are managed through `aigame concept`.",
        )
    blueprint_readme = project / "docs" / "blueprint" / "README.md"
    if not blueprint_readme.is_file():
        _write_text(
            blueprint_readme,
            "# Generated blueprint\n\nRendered from canonical `work/concept/` records.",
        )
    asset_readme = project / "work" / "assets" / "README.md"
    if not asset_readme.is_file():
        _write_text(
            asset_readme,
            "# Asset records\n\nCanonical media inventory, source parts, specifications, recipes, and runtime contracts are managed through `aigame assets`.",
        )
    asset_docs_readme = project / "docs" / "assets" / "README.md"
    if not asset_docs_readme.is_file():
        _write_text(
            asset_docs_readme,
            "# Generated asset documentation\n\nRendered deterministically from canonical `work/assets/` records.",
        )
    skill_path = project / ".aigame" / "agent-skills" / "build-game-concept" / "SKILL.md"
    if not skill_path.is_file():
        _write_text(
            skill_path,
            """---
name: build-game-concept
description: Build or resume the complete portable game blueprint before implementation.
---

# Build Game Concept

Read `AGENTS.md` and `.aigame/automation.json`. Complete one schema-valid `aigame concept`
task at a time. Human-gated mode stops for approval; AI-staging mode records agent decisions
and continues. Commit canonical concept records at `commit_blueprint_inputs` before finalization.
Only the authenticated owner may post the staging approval comment. Never bypass
independent review, production, release, or rollback.
""",
        )

    media_skill_path = project / ".aigame" / "agent-skills" / "generate-game-assets" / "SKILL.md"
    if not media_skill_path.is_file():
        _write_text(
            media_skill_path,
            '''---
name: generate-game-assets
description: Plan, generate, validate, and integrate deterministic visual and audio assets from an approved aigame blueprint.
---

# Generate Game Assets

Read `AGENTS.md`, `.aigame/media.toml`, `.aigame/automation.json`, and `.aigame/state/assets.json`.
Run `aigame doctor --project . --json`, then `aigame assets next --project . --json`. Preview every
mutation, apply only the returned operation, validate, and query `next` again. Human-gated mode
stops at representative style approval; AI-staging mode records a CLI-bound agent approval. Resume
from cache receipts. Reject unsafe paths, licenses, provenance, checksums, palettes, incompatible
parts, or invalid Godot resources. Never install dependencies, call paid providers, download models,
or bypass independent review, staging-owner approval, production, release, rollback, or secrets.
''',
        )
    source_pack_path = project / "assets" / "source" / "packs" / "core-topdown-v1" / "pack.json"
    if not source_pack_path.is_file():
        source_pack = {
            "schema_version": "1.0",
            "id": "core-topdown-v1",
            "name": "Core Top-down 16x16",
            "license": "CC0-1.0",
            "grid_size": 16,
            "perspective": "top_down",
            "directions": ["down", "left", "right", "up"],
            "body_families": ["humanoid", "compact_enemy"],
            "parts": ["shadow", "body", "legs", "head", "front_weapon", "front_effect"],
            "provenance": "Original procedural coordinates; no LPC or third-party artwork is bundled.",
        }
        source_pack["sha256"] = fingerprint(source_pack)
        _write_json(
            source_pack_path,
            source_pack,
        )
    runtime_composer_path = project / "addons" / "aigame_media" / "runtime_composer.gd"
    if not runtime_composer_path.is_file():
        _write_text(runtime_composer_path, runtime_composer_source())
    media_license_path = project / "LICENSES" / "core-pixel-media-CC0-1.0.txt"
    if not media_license_path.is_file():
        _write_text(
            media_license_path,
            "The original core-topdown-v1 procedural coordinates and their unmodified deterministic outputs are dedicated under CC0 1.0.\nSee https://creativecommons.org/publicdomain/zero/1.0/\nOutputs composed with project-supplied parts retain the license recorded in asset-manifest.json.",
        )

    config_path = project / ".aigame" / "project.toml"
    config = config_path.read_text(encoding="utf-8")
    config = re.sub(
        r'^workflow_version = "[^"]+"$',
        f'workflow_version = "{version}"',
        config,
        flags=re.MULTILINE,
    )
    capability_match = re.search(r'^capability_packs = \[(.*)\]$', config, flags=re.MULTILINE)
    if capability_match and '"pixel-media-v1"' not in capability_match.group(1):
        existing = capability_match.group(1).strip()
        replacement = f'capability_packs = [{existing}, "pixel-media-v1"]' if existing else 'capability_packs = ["pixel-media-v1"]'
        config = config[: capability_match.start()] + replacement + config[capability_match.end() :]
    _write_text(config_path, config)

    workflows = project / ".github" / "workflows"
    workflow_reference = re.compile(
        r"(monstermic/godot-ai-game-workflow/\.github/workflows/[^@\s]+@)[0-9a-f]{40}"
    )
    for workflow in sorted(workflows.glob("*.yml")):
        content = workflow.read_text(encoding="utf-8")
        _write_text(workflow, workflow_reference.sub(rf"\g<1>{commit}", content))
    _write_text(workflows / "staging-merge.yml", _staging_caller(commit))

    agents_path = project / "AGENTS.md"
    agents = agents_path.read_text(encoding="utf-8")
    marker = "## Workflow automation mode"
    if marker not in agents:
        agents += f'''\n\n{marker}

Read `.aigame/automation.json`. Human-gated mode stops at approval gates. AI-staging mode records
agent concept decisions and targets `staging`. Run `aigame staging merge` to obtain the exact
approval comment, then stop for the authenticated repository owner to post it. Never bypass
independent review, production, release, or rollback.
'''
        _write_text(agents_path, agents)
    media_marker = "## Pixel media workflow"
    if media_marker not in agents:
        agents += f'''

{media_marker}

After concept finalization, run `aigame assets next --project . --json` until media state is
`integrated`. Human-gated mode stops for representative style approval; AI-staging mode records a
bound agent approval. Preview all mutations. Never auto-install media dependencies, call paid
providers, download models, or bypass provenance and Godot runtime validation.
'''
        _write_text(agents_path, agents)

    installed_checksum = workflow_snapshot_checksum(vendor_root)
    if installed_checksum != source_sha256:
        raise RuntimeError("Installed workflow snapshot checksum does not match the requested source")
    _write_json(lock_path, lock)
    return {
        "schema_version": "1.0",
        "status": "passed",
        "path": str(lock_path),
        "before": before,
        "after": lock,
        "install": install,
        "preserved": ["docs/**", "work/**", "assets/**"],
    }
