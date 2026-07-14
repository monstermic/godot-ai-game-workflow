# Agent and media adapter contract

An automated agent needs filesystem read/write, a shell, Git, and the ability to return JSON. GitHub, Godot, browser, image, and audio capabilities are negotiated separately by `aigame doctor`.

Agents begin at `AGENTS.md`, claim exactly one work item, request a bounded context packet, and emit a `RunResult`. Status is one of `passed`, `failed`, `blocked`, or `needs_human`. Every changed artifact and evidence record is identified and checksummed.

Media generators use JSON over standard input/output. Input is an `AssetBrief`; output is an `AssetGenerationResult`. Generated results must include provider and version, model ID and checksum, model license, workflow checksum, prompt checksum, seed, output checksum, final license, and runtime path. Adapters may not install generators, download weights, or spend money without approval.

Vendor instruction files are convenience shims. When an adapter conflicts with `AGENTS.md`, schemas, or project policy, the canonical repository contract wins.
