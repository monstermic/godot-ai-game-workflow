# Workflow and gates

The product lifecycle is bootstrap → discovery → experiment → first playable → vertical slice → production → alpha/beta → release candidate → release/post-release.

Work items move through `draft → ready → claimed → implementing → validating → review → playtest → done`; blocked, rework, and cancelled are explicit side states. V1 permits one active implementation slice per game repository.

`aigame next` orders eligible work by release blocker, risk, dependency fan-out, player value, smallest estimate, then ID. A red unknown, missing capability, or approval-triggering operation stops with `needs_human`.

Human gates cover vision and scope, prototype continuation, vertical-slice quality and production budget, every merge, subjective playtest claims, release promotion, and rollback. Dependencies, paid services, model downloads, secrets, networking, save migrations, destructive changes, deployment, and bulk media require approval before mutation.

Definition of Ready requires player value, acceptance behavior, dependencies, risks, capabilities, non-goals, tests, and playtest intent. Definition of Done requires merged behavior, deterministic checks, runtime evidence, media provenance, documentation, independent review, human playtest when subjective, and a downloadable build.
