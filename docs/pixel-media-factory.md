# Pixel media factory

`pixel-media-v1` turns a finalized blueprint plus approved structured media direction and requests into the complete visual and audio inventory under `work/assets/`. Visual requests lock an exact logical-pixel output size from 16×16 through 128×128. It produces deterministic files under `assets/generated/` and a runtime registry for Godot. No neural model or proprietary service is required.

## Protocol

Run `aigame assets next --json` and follow the returned operation. Planning treats `_collect_needs` only as a coverage oracle: every content asset, mechanic state and feedback channel, stage, encounter, opening, ending, credits, postgame screen, HUD/menu requirement, accessibility alternative, ambience, and music direction must map exactly once to an approved `ARQ-####`. Missing, duplicate, stale, incomplete, or unsupported mappings return `status: blocked` with `restart_stage: asset_specification` before recipes or outputs exist.

Representative samples precede bulk generation. Human-gated mode requires a current approval; AI-staging mode records an agent approval. Bulk generation is content-addressed and resumable. Validation checks schemas, references, layer order, asymmetric directions, terrain masks, paths, licenses, provenance, PNG indexing/grid size, WAV rate/width/peak/DC/loop seams, hashes, and blueprint drift. Integration writes `assets/generated/media-registry.json` with runtime recipes, parts, animation events, tile rules, particles, and audio contracts.

## Output model

- Sprites use semantic palette indices, integer pixels, fixed layer order, neutral anchors, occupancy/occlusion masks, compatibility tags, and four-direction rules. Built-in actor families are humanoid, serpentine, quadruped, winged, amorphous, and mechanical vehicle; incompatible parts and unsupported tags block. Common launch assets are precompiled; approved part combinations can be composed and cached at runtime.
- Animation contracts include baseline idle, movement, attack, hit, death, and spawn clips plus every mechanic-specific state and gameplay-event frame.
- Each biome includes all 256 eight-neighbor terrain masks, tile roles, collision/navigation metadata, adjacency, weights, and stage constraints. The seeded adjacency-collapse reference generator proves connectivity, safe spawns, and required-room placement.
- Particle specifications emit fixed-seed GPU scenes plus CPU fallbacks and budget metadata.
- SFX use three deterministic variants and Godot randomizers. Ambience loops and synchronized explore/combat/boss music stems share sample rate, key, BPM, length, and transition points.

The architecture is informed by the modular layering and attribution model of the [Universal LPC generator](https://github.com/liberatedpixelcup/Universal-LPC-Spritesheet-Character-Generator), the constraints of [Wave Function Collapse](https://github.com/mxgmn/WaveFunctionCollapse), procedural synthesis patterns from [jsfxr](https://github.com/chr15m/jsfxr), and symbolic loop design from [BeepBox](https://github.com/johnnesky/beepbox). No third-party art is bundled. Optional hosted systems such as [PixelLab](https://api.pixellab.ai/v2/docs) are source-part adapters only.
