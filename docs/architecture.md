# Architecture

## Authority

Accepted intent, concept records, requirements, work definitions, decisions, asset records, and completed evidence are canonical Git files. GitHub owns only live claims, PR heads, checks, reviews, merges, workflow artifacts, and releases. GitHub intake becomes authoritative only after a generated proposal PR merges.

No local database is required. `.aigame/automation.json` selects human-gated or AI-staging authority, `.aigame/state/concept.json` projects the resumable concept stage, and `.aigame/state/current.json` projects the single active implementation slice. Canonical concept records live in `work/concept/`, pinned profiles in `.aigame/profiles/`, deterministic reading documents in `docs/blueprint/`, and immutable evidence in `evidence/`. Input fingerprints invalidate approvals and evidence when their bound records change.

## Portable layers

1. Markdown explains product intent to humans and agents.
2. TOML config pins the engine, target, policies, and capability packs.
3. JSON Schema defines concept, work, evidence, and adapter records.
4. The Python CLI supplies deterministic concept orchestration, rendering, scheduling, and validation.
5. GitHub mirrors work and independently verifies pull requests and releases.
6. Thin vendor adapters point back to `AGENTS.md`; they never redefine policy.

AI mode is a repository contract, not a vendor preference. Every agent reads the same automation record. Agent approvals identify `aigame-agent` and bind to the same scope, fingerprint, and commit as human approvals. The integration boundary remains a protected `staging` branch; `main`, production environments, release promotion, and rollback are outside agent authority.

The optional MCP server exposes recursive blueprint documentation as resources and context, concept state, scheduling, and validation as tools. Agents without MCP use the same files and CLI directly.

## Trust boundaries

Prompts, issue bodies, and adapter output are data, never executable commands. Only commands allowlisted in project config may be invoked by an agent. External media remains untrusted until provenance, license, path, checksum, and runtime validation pass. Release promotion consumes the exact artifact and digest produced by the approved candidate.
