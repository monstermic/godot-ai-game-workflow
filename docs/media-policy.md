# Media policy

The `pixel-media-v1` factory derives an exhaustive `AssetPlan` from approved `MDR-0001` media direction and `ARQ-####` requests before implementation. It compiles indexed 16–128 px sprites, four-direction animation sets, terrain-mask atlases, particles, UI, 48 kHz SFX/ambience, and adaptive music stems from pinned source parts and seeded recipes. Free-text purpose never selects a renderer or actor family. Optional AI providers may create approved source parts only; bulk and runtime composition remain local and deterministic.

Use procedural placeholders during discovery and prototypes. Human-gated projects approve a fingerprint-bound bundle covering every used actor family and active visual/audio subtype before batches. AI-staging projects record the same checksum-bound gate as an agent approval.

Default preference is existing procedural content, verified CC0, then an explicitly configured generator. Original, CC0-1.0, CC-BY-4.0, compatible commercial, proprietary, and Apache-2.0 records are accepted. Unknown, unverifiable, non-commercial, and no-derivatives terms are blocked by default.

Synthetic voice may not impersonate or clone a real person. Critical gameplay and accessibility information must remain available without audio or generated imagery.

The workflow template contains no LFS objects. Generated game repositories configure LFS only for matching files below `assets/source/` (PSD, Krita, Blender, FBX, WAV, FLAC, and video). Optimized runtime assets in `assets/generated/` stay in ordinary Git unless project policy explicitly moves large files to LFS. Builds belong in GitHub releases, not source history.
