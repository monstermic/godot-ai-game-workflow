# OpenCode adapter

Load the global `build-game-concept` skill when the user asks to create, continue, audit, revise, or finalize a game concept. The target repository's root `AGENTS.md`, `.aigame/automation.json`, pinned schemas, and command results remain authoritative.

Skill activation and exact local workflow commands may be auto-approved. In `ai_staging` mode, use the global `ai-staging` agent profile, make concept decisions, record agent approvals, and continue through ordinary local operations. Target `staging`; run `aigame staging merge` to obtain the exact approval comment, then stop for the authenticated repository owner to post it on the PR. The agent must not post the comment. Production, release, rollback, secrets, paid services, and destructive external operations remain protected.
