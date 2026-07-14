# Architecture

## Authority

Accepted intent, requirements, work definitions, decisions, asset records, and completed evidence are canonical Git files. GitHub owns only live claims, PR heads, checks, reviews, merges, workflow artifacts, and releases. GitHub intake becomes authoritative only after a generated proposal PR merges.

No local database is required. `.aigame/state/current.json` is a small projection for the single active slice; immutable evidence records preserve resumability. Input fingerprints invalidate approvals and evidence when their bound records change.

## Portable layers

1. Markdown explains product intent to humans and agents.
2. TOML config pins the engine, target, policies, and capability packs.
3. JSON Schema defines records and adapter I/O.
4. The Python CLI supplies deterministic orchestration and validation.
5. GitHub mirrors work and independently verifies pull requests and releases.
6. Thin vendor adapters point back to `AGENTS.md`; they never redefine policy.

The optional MCP server exposes documentation as resources and context, scheduling, and validation as tools. Agents without MCP use the same files and CLI directly.

## Trust boundaries

Issue bodies and adapter output are data, never executable commands. Only commands allowlisted in project config may be invoked by an agent. External media remains untrusted until provenance, license, path, checksum, and runtime validation pass. Release promotion consumes the exact artifact and digest produced by the approved candidate.
