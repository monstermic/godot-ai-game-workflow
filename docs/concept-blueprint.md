# Prompt-to-complete-game blueprint

The concept protocol turns a user prompt into canonical, implementation-ready records before gameplay code begins. It is agent-neutral: an agent reads a `CTK-` task, returns schema-valid JSON, and checkpoints the result through the CLI. No embedded model provider or private database is required.

## Lifecycle

```text
intake → pitching → direction selection → product identity
       → mechanics → named content catalog → complete game arc
       → quality audit → blueprint approval → finalized
```

Direction, product identity, and final blueprint are checksum-bound human gates. Safe omissions may become labeled assumptions; contradictions, red risks, identity decisions, paid services, networking, legal sensitivity, or expensive scope return `needs_human`.

## Start and resume

```powershell
aigame concept start --prompt-file idea.md --profile roguelite-v1 --json
aigame concept start --prompt-file idea.md --profile roguelite-v1 --apply --json
aigame concept next --json
```

For each returned task, read `context_refs`, create one result JSON, and preview before applying:

```powershell
aigame concept submit CTK-0001 --result pitch-result.json --json
aigame concept submit CTK-0001 --result pitch-result.json --apply --json
```

In `human_gated` mode, when a command returns `needs_human`, its `approval_request` identifies the exact `scope_hash`, `commit_sha`, and `decision`. Pitching also returns `approval_requests` keyed by pitch ID; use the request for the direction the human actually selects. After the human explicitly decides, record an `APR-` document matching that request and use it with `concept select`, the product identity submission, or `concept finalize`.

In `ai_staging` mode, the pitching submission selects the recommended direction, product identity submission records its agent approval, and a valid quality audit records the blueprint approval and finalizes automatically. These are checksum-bound `APR-` records and remain invalidated by later input changes; no external approval file is required for those three creative decisions.

## Canonical outputs

`work/concept/` contains the intake, three pitches, approved product identity, implementation-ready mechanics, complete named launch catalog, name registry, full game blueprint, quality classification, game Definition of Done, roadmap, and resumable tasks. `.aigame/profiles/` contains the pinned universal profile and optional genre profiles.

`docs/blueprint/` is a deterministic human-readable rendering. Run:

```powershell
aigame concept validate --final --json
aigame concept render --apply --json
aigame concept render --check --json
```

Finalization converts must-scope mechanics and content into requirements, creates first-milestone implementation work, adds dependency-gated planning items for later milestones, and makes ordinary `aigame next` available.

For revisions, the change file may set `restart_stage` to the earliest affected concept stage. Identity or product-identity changes restart pitching and expire direction, product, and blueprint approvals. Other revisions preserve still-valid upstream approvals while invalidating generated requirements, implementation work, evidence, and the blueprint approval.

## Completeness rules

- Every mechanic has executable baseline values, state transitions, interaction rules, feedback, debugging, tests, and tuning ranges.
- Every authored launch entry has a final stable name and ID. Procedural combinations may be unbounded, but all authored generators and components are enumerated.
- The blueprint defines the opening, complete progression, terminal states, final challenge, endings, credits, recovery, replay, and postgame.
- Every active quality-profile item is classified; classification coverage is mandatory, feature inclusion is not.
- Every must-scope record maps to the game Definition of Done, roadmap, requirement, work item, and eventually current evidence.
- Subjective qualities remain playtest hypotheses until a human evaluates them.
