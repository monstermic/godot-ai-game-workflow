# Godot AI Game Workflow

An executable, agent-neutral workflow for turning a game idea into a complete blueprint and then into small, reviewable, playable Godot releases. Product intent and completed history live in Git; GitHub supplies the operational queue, pull requests, checks, builds, and releases.

The workflow does not promise that an AI can determine whether a game is fun. It automates concept generation, context assembly, deterministic scheduling, code and asset delivery, evidence collection, and release integrity. Human-gated mode reserves creative decisions for people; opt-in AI staging mode delegates those decisions to the agent while retaining independent review and production controls.

## Quick start

Requirements: Python 3.11+, Git, and Godot 4.7 for runtime validation. Install the pinned `media` extra for deterministic pixel and audio generation. GitHub automation additionally requires `gh`.

```powershell
python -m pip install -e ".[media]"
aigame doctor --json
aigame new "My Game" --destination ../my-game --godot-version 4.7 --apply --json
cd ../my-game
aigame concept start --prompt "A small complete game about..." --apply --json
aigame concept next --json
```

`aigame new` initializes and commits an unrelated-history `main` branch. Add `--github OWNER/REPOSITORY` to create and push the default-private game repository.

For an existing empty Git repository:

```powershell
aigame init "My Game" --godot-version 4.7 --apply --json
```

All mutations preview by default. `--apply` is mandatory for repository writes.

## AI staging mode

Enable the agent-owned decision path explicitly in a generated game:

```powershell
aigame mode set ai-staging --confirm ENABLE-AI-STAGING --json
aigame mode set ai-staging --confirm ENABLE-AI-STAGING --apply --json
aigame staging init --apply --json
```

In this mode the agent selects the recommended pitch, fixes the product identity, approves the complete blueprint, and proceeds through ordinary project-local edits, commands, tests, commits, and PR preparation without asking for each permission. Pull requests target `staging`.

Staging integration is deliberately protected. `aigame staging merge 123 --json` validates the exact PR head, required checks, independent-review evidence, and scope hash, then returns an `approval_comment`. The authenticated repository owner must post that exact comment on the PR. GitHub revalidates the head and checks, publishes an Actions-App-bound required status on that SHA, and enables squash auto-merge. The agent cannot post or forge this approval. Production, release, rollback, destructive external operations, paid services, secrets, and subjective playtest claims remain protected. See [AI staging mode](docs/ai-staging-mode.md).

After upgrading an existing game, preview and apply its remote policy with `aigame repository configure-game OWNER/REPOSITORY --project . --apply --json`. This preserves the repository's visibility and template status.

## Portable agent loop

1. Read `AGENTS.md` and `.aigame/project.toml`.
2. Run `aigame doctor --json`.
3. Start concept work from the user's prompt, then complete one `aigame concept next --json` task at a time.
4. In human-gated mode, stop for direction, product identity, and complete-blueprint approval. In AI staging mode, record agent approvals and continue automatically.
5. Follow `aigame assets next --json` through exhaustive planning, style sampling, deterministic generation, validation, and Godot integration.
6. Run `aigame next --json`, claim one eligible item, and request its bounded context packet.
7. Implement and run `aigame validate WI-#### --json` plus Godot tests.
8. Persist a `RunResult` through `aigame checkpoint` and open a PR. Human-gated projects use owner merge; AI staging projects use the checked, approval-bound staging merge command.

Codex, OpenCode, Claude Code, Cursor, and Copilot adapters are instruction shims only. The schemas, CLI behavior, Git history, and evidence records are authoritative. Hosts supporting Model Context Protocol can run `python -m aigame.mcp`.

## What is included

- Versioned JSON contracts and stable requirement, concept, mechanic, content, roadmap, work, asset, evidence, and approval IDs.
- A prompt-to-blueprint protocol with three pitches, approved product identity, implementation-ready mechanics, an exhaustive named launch catalog, complete game arc, quality profiles, game Definition of Done, and whole-game roadmap.
- Deterministic next-work selection and one active implementation slice per game.
- Resumable concept, context, and checkpoint records.
- Safe, idempotent GitHub issue mirroring.
- Optional JSON-over-stdio image/audio adapters with strict provenance.
- A CPU-only 16x16 media factory for modular sprites, complete animation sets, terrain atlases, seeded connected stages, particles, UI, SFX variants, ambience, and adaptive music stems.
- A tested 2D Godot reference slice with movement, hazard, win/loss, reward, and restart.
- Reusable GitHub Actions for contracts, Godot tests/export, provenance, review, RCs, and same-artifact release.
- Apache-2.0 workflow tooling with separately licensed generated game output.

See [concept blueprint](docs/concept-blueprint.md), [pixel media factory](docs/pixel-media-factory.md), [AI staging mode](docs/ai-staging-mode.md), [architecture](docs/architecture.md), [workflow gates](docs/workflow.md), [adapter contract](docs/adapter-contract.md), and [media policy](docs/media-policy.md).

## Status

V1.3 targets offline top-down 2D GDScript games, 16x16 pixel media, procedural chiptune audio, and Windows desktop exports. The concept engine is genre-neutral and ships universal plus roguelite quality profiles. Capability packs describe 3D, narrative, localization, persistence, mobile, networking, and other future extensions without pretending those pipelines are already implemented.
