# Workflow and gates

The product lifecycle is bootstrap → concept blueprint → pixel media inventory/integration → experiment → first playable → vertical slice → production → alpha/beta → release candidate → release/post-release.

Concept work moves through `intake → pitching → direction selection → product identity → mechanics → content catalog → complete game arc → asset specification → quality audit → blueprint approval → finalized`. The asset-specification stage locks `MDR-0001` and exhaustive approved `ARQ-####` requests before the quality audit can approve the final blueprint. The agent completes one schema-valid `CTK-` task at a time. In `human_gated` mode, direction, final product identity, and complete blueprint are checksum-bound human gates. In `ai_staging` mode, the agent records checksum-bound agent approvals and continues.

Media work moves through `unplanned → inventoried → style sample → style approval → ready → generating → validating → integrated`. It cannot start before blueprint finalization. Every concept media source must map to an `AssetSpec` and immutable recipe. Human-gated projects stop for representative character, enemy, tile, particle, UI, SFX, and music approval; AI-staging projects record a bound agent approval.

Implementation work remains `draft → ready → claimed → implementing → validating → review → playtest → done`; blocked, rework, and cancelled are explicit side states. Exactly one implementation slice may be active. A ready item is eligible only after all dependencies are done.

`aigame next` returns concept work first, then the media gate, and only then implementation. Eligible implementation work is ordered by release blocker, risk, dependency fan-out, player value, smallest estimate, then ID. A missing capability, invalid state, stale approval, or protected operation stops safely. In AI staging mode, ordinary project decisions and operations continue automatically; dependency installation, paid providers, model downloads, protected staging, production, release, rollback, secret, and destructive external gates do not.

Blueprint Ready requires approved product identity, implementation-ready mechanics, a final named launch catalog, explicit opening-to-ending journey, complete profile classification, acyclic roadmap, game Definition of Done coverage, no required placeholders, and current approval.

Media Ready requires exhaustive blueprint-to-request coverage, approved style, complete family-specific animation and terrain contracts, deterministic 16–128 px generated files, compatible licenses and provenance, passing Godot import/runtime tests, current brief/request hashes, and an integrated runtime registry.

Game Done requires implemented and evidenced must-scope mechanics/content, a playable opening-to-ending path, working terminal states and recovery, accessibility/performance/compatibility/provenance gates, human evidence for subjective claims, and promotion of the approved immutable release candidate without rebuilding.
