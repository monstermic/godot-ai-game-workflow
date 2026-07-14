# Godot AI Game Workflow

An executable, agent-neutral workflow for turning a game idea into small, reviewable, playable Godot releases. Product intent and completed history live in Git; GitHub supplies the operational queue, pull requests, checks, builds, and releases.

The workflow does not promise that an AI can determine whether a game is fun. It automates context assembly, deterministic scheduling, code and asset delivery, evidence collection, and release integrity while reserving creative and irreversible decisions for people.

## Quick start

Requirements: Python 3.11+, Git, and Godot 4.7 for runtime validation. GitHub automation additionally requires `gh`.

```powershell
python -m pip install -e .
aigame doctor --json
aigame new "My Game" --destination ../my-game --godot-version 4.7 --apply --json
cd ../my-game
aigame next --json
```

`aigame new` initializes and commits an unrelated-history `main` branch. Add
`--github OWNER/REPOSITORY` to create and push the default-private game repository.

For an existing empty Git repository:

```powershell
aigame init "My Game" --godot-version 4.7 --apply --json
```

All mutations preview by default. `--apply` is mandatory for repository writes.

## Portable agent loop

1. Read `AGENTS.md` and `.aigame/project.toml`.
2. Run `aigame doctor --json`.
3. Run `aigame next --json`; stop when it reports missing capabilities or human decisions.
4. Claim one item with `aigame claim WI-#### --apply`.
5. Request the bounded task packet with `aigame context WI-#### --json`.
6. Implement and run `aigame validate WI-#### --json` plus Godot tests.
7. Persist a `RunResult` through `aigame checkpoint`.
8. Open a PR. A distinct reviewer and the owner’s manual merge remain mandatory.

Codex, Claude Code, Cursor, and Copilot adapters are instruction shims only. The schemas, CLI behavior, Git history, and evidence records are authoritative. Hosts supporting Model Context Protocol can run `python -m aigame.mcp`.

## What is included

- Versioned JSON contracts and stable `REQ-`, `DEC-`, `EXP-`, `WI-`, `AST-`, `EVD-`, and `APR-` IDs.
- Deterministic next-work selection and one active implementation slice per game.
- Resumable context and checkpoint records.
- Safe, idempotent GitHub issue mirroring.
- Optional JSON-over-stdio image/audio adapters with strict provenance.
- A tested 2D Godot reference slice with movement, hazard, win/loss, reward, and restart.
- Reusable GitHub Actions for contracts, Godot tests/export, provenance, review, RCs, and same-artifact release.
- Apache-2.0 workflow tooling with separately licensed generated game output.

See [architecture](docs/architecture.md), [workflow gates](docs/workflow.md), [adapter contract](docs/adapter-contract.md), and [media policy](docs/media-policy.md).

## Status

V1 targets offline 2D GDScript games and Windows desktop exports. Capability packs describe 3D, narrative, localization, persistence, mobile, networking, and other future extensions without pretending those pipelines are already implemented.
