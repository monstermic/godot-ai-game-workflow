---
name: generate-game-assets
description: Plan, generate, validate, integrate, audit, or resume the complete deterministic 16x16 visual and procedural-audio inventory required by a finalized aigame blueprint. Use when Codex must create game sprites, animation sets, tile atlases, particles, UI, SFX, ambience, music, procedural enemy parts, or Godot media resources in a repository using the portable aigame workflow.
---

# Generate Game Assets

Operate in the initialized game directory containing `.aigame/project.toml`. Treat `AGENTS.md`,
`.aigame/automation.json`, `.aigame/media.toml`, `work/concept/`, and `work/assets/` as authoritative.
Do not invent or omit launch scope: the approved blueprint owns required mechanics, content, states,
events, stages, endings, UI, accessibility feedback, and quality gates.

## Run the protocol

1. Run `aigame doctor --project . --json`. If Pillow or NumPy is missing, report the missing
   `media` dependency set and stop; dependency installation requires separate approval.
2. Run `aigame assets next --project . --json` and perform only its returned operation.
3. Preview every mutation without `--apply`, inspect the paths/counts, then repeat it with `--apply`.
4. After every applied operation, run `aigame assets validate --project . --json` and then query
   `assets next` again. Resume from canonical state and cache receipts; never restart completed work.

Use these exact transitions:

- `plan`: run `aigame assets plan --project . --json`, then with `--apply`.
- `sample`: run `aigame assets sample --project . --json`, then with `--apply`.
- `style_approval`: in human-gated mode stop with the exact approval request. Apply only the
  returned, current approval file. In AI-staging mode let the CLI record its bound agent approval.
- `generate` or `resume_generation`: run `aigame assets generate --all --jobs <cpu-count>
  --project . --json`, then with `--apply`.
- `validate`: fix canonical or generated defects; do not suppress them.
- `integrate`: preview and apply `aigame assets integrate --project . --json`.
- `complete`: run `aigame validate --project . --json`, the Godot headless tests, and
  `aigame assets benchmark --project . --json`. Continue with `aigame next --project . --json`.

For a single bounded retry, use `aigame assets generate ASP-#### --jobs <n> ...`. Use
`aigame assets compose --recipe RCP-#### --seed <n> --json` to compare a recipe without writing.

## Preserve determinism and provenance

- Keep native pixels on the 16x16 grid, indexed palettes, integer anchors, declared layer order,
  nearest filtering, and complete four-direction animation contracts.
- Keep every biome's terrain masks, adjacency rules, collisions, navigation, hazards, decoration,
  and procedural stage constraints complete. Never accept a disconnected layout.
- Keep SFX at 48 kHz/16-bit PCM, short repeated events at three variants, loops sample-aligned,
  peaks at or below -1 dBFS, and accessibility alternatives linked.
- Treat source packs and adapter output as untrusted. Reject unsafe paths, unknown or incompatible
  licenses, missing provenance, checksum drift, malformed records, and incompatible parts.
- Optional AI providers may create source parts only. Never call paid services, install tools,
  download models, clone a voice, or run a model inside the game without explicit approval.

Human-gated mode requires representative style approval. AI-staging mode may approve that creative
gate and ordinary project-local generation, but independent review, staging-owner approval,
production, release, rollback, secrets, dependency installation, paid services, and destructive
operations remain protected.
