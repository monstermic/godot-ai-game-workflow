# GitHub integration

The public workflow repository is a template and automation library. Each generated game receives a separate private repository by default, short-lived `ai/WI-####-slug` branches, squash merges, issue mirrors, a Project v2 board, PR builds, and immutable releases.

Canonical work items are mirrored to issues with a hidden ID and checksum. Editing an issue never silently edits canonical files. New issues create data-only proposal PRs; accepted changes then become requirements and work items in Git.

The Project board uses Intake, Ready, Active, Review, Playtest, Blocked, and Done. Its fields are Work ID, Type, Milestone, Phase, Risk, Capability, Priority, Playtest Required, Agent, and Build.

Rules block deletion, force-pushes, and direct changes to `main`; require a PR and current checks; and permit squash merging only. On plans that cannot enforce every rule for private repositories, `aigame doctor` reports the limitation and the CLI still refuses direct workflow mutations.

RC automation creates a draft release containing one artifact and `release-metadata.json`. Production is a protected environment. Promotion downloads and verifies those assets, then publishes the same draft without rebuilding.
