# Agent and media adapter contract

An automated agent needs filesystem read/write, a shell, Git, and the ability to return JSON. GitHub, Godot, browser, image, and audio capabilities are negotiated separately by `aigame doctor`.

Agents begin at `AGENTS.md` and read `.aigame/automation.json`. Before implementation, they consume one `ConceptTask`, read its bounded `context_refs`, and return a JSON result matching `result_contract`. Human-gated projects stop at concept approvals; AI-staging projects accept CLI-generated agent approvals and continue. After finalization they claim exactly one work item, request a bounded context packet, and emit a `RunResult`. Status is one of `passed`, `failed`, `blocked`, or `needs_human`. Every changed artifact and evidence record is identified and checksummed.

In AI staging mode, adapters may auto-approve project-local file edits, allowlisted commands, tests, branches, commits, and PR preparation. They target `staging`, never `main`. `aigame staging merge` verifies independent-review evidence and required checks, then returns an exact SHA- and scope-bound comment for the authenticated repository owner to post on the PR. The GitHub workflow revalidates the PR and sets the required, GitHub-Actions-App-bound approval status before squash auto-merge. Adapters may not post the owner comment or reinterpret this boundary as permission to deploy, promote a release, roll back, expose secrets, buy services, or perform destructive external actions.

Media generators use JSON over standard input/output. Input is an `AssetBrief`; output is an `AssetGenerationResult`. Generated results must include provider and version, model ID and checksum, model license, workflow checksum, prompt checksum, seed, output checksum, final license, and runtime path. Adapters may not install generators, download weights, or spend money without approval.

Vendor instruction files and skills are convenience shims. When an adapter conflicts with `AGENTS.md`, schemas, concept state, or project policy, the canonical repository contract wins.

MCP hosts discover the same policy through `aigame_mode_get` and can preview or apply mode and staging operations through their corresponding tools. These are thin wrappers over the CLI contracts and do not grant additional authority.
