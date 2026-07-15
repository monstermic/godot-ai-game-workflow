# AI staging mode

`ai_staging` is an explicit repository policy for unattended creative and implementation decisions up to a protected integration branch. The default remains `human_gated`.

## Enable or disable

Preview every policy change before applying it:

```text
aigame mode show --json
aigame mode set ai-staging --confirm ENABLE-AI-STAGING --json
aigame mode set ai-staging --confirm ENABLE-AI-STAGING --apply --json
aigame staging init --json
aigame staging init --apply --json
```

Return to the default with `aigame mode set human-gated --apply --json`.

The canonical record is `.aigame/automation.json`. Its checksum-protected policy gives the agent decision authority, permits ordinary project operations, sets `staging` as the integration branch, and lists the gates that cannot be bypassed.

## Agent authority

The agent chooses one of the three pitches, establishes product identity, completes and approves the blueprint, approves representative generated media, makes implementation choices within approved scope, runs allowlisted local tools, and prepares commits and pull requests. Concept and media approvals are durable `APR-` records with `approval_kind: agent` and `automation_mode: ai_staging`.

When concept generation returns `commit_blueprint_inputs`, the agent commits the canonical records, runs `aigame concept next --json` to bind the refreshed request to that commit, and then runs `aigame concept finalize --apply --json`. This is an automated agent step, not a human creative gate.

The agent must still produce complete mechanics, named launch content, a beginning-to-ending journey, exhaustive asset coverage, deterministic recipes, valid Godot resources, evidence, and playtest hypotheses. Automatic approval is accountability metadata, not permission to omit contract fields, call providers, or claim subjective fun.

## Staging integration

Feature PRs target `staging`. Run:

```text
aigame staging merge 123 --json
```

The command refuses a draft, stale or unmergeable PR, a non-staging base, or missing or failed required checks, including the independent-review check. When the PR is eligible, it returns `needs_human` and an `approval_comment` bound to its number, head SHA, checks, and scope hash, for example:

```text
/aigame merge staging head=<40-character-sha> scope=<64-character-scope-hash>
```

The authenticated repository owner must post the returned comment exactly on that pull request. The agent must not post it. The issue-comment workflow verifies that the commenter is the repository owner, revalidates the current PR, required checks, independent-review result, and exact head SHA, then publishes the `aigame-staging-owner-approval` status with the GitHub Actions App and enables squash auto-merge. The staging ruleset requires that status from that specific app, so a local file, direct merge command, or look-alike status cannot satisfy the gate. Changed code, checks, or SHA fails closed and requires a fresh comment.

For an upgraded game repository, apply the remote policy separately with `aigame repository configure-game OWNER/REPOSITORY --project . --apply --json`; local upgrades never mutate GitHub silently.

## Protected boundary

Independent review, the exact staging-merge approval, production, release, and rollback remain protected. Dependencies, paid APIs, model downloads, networking, secrets, destructive external changes, and deployments remain subject to project risk policy. Promotion from staging toward `main` or a release is not implied by AI mode.
