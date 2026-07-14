from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .automation import (
    get_automation_policy,
    initialize_staging,
    is_ai_staging,
    prepare_staging_merge,
    set_automation_mode,
)
from .assets import build_lfs_plan
from .concept import (
    finalize_concept,
    next_concept_task,
    render_concept,
    revise_concept,
    select_concept_pitch,
    start_concept,
    submit_concept_task,
    validate_concept,
)
from .context import build_context
from .core import (
    HumanRequired,
    MissingCapability,
    WorkflowError,
    choose_next,
    workflow_snapshot_checksum,
)
from .generator import create_game, initialize_game
from .github import build_sync_plan, sync_github
from .release import ApprovalRequired, prepare_release_candidate, promote_release
from .project import configure_project
from .repository import configure_game_repository, configure_repository
from .state import checkpoint, claim_work_item
from .upgrade import upgrade_workflow
from .validation import validate_project


def _doctor(project: Path | str | None = None) -> dict[str, Any]:
    root = Path(project).resolve() if project is not None else Path.cwd().resolve()
    policy_path = root / ".aigame" / "automation.json"
    policy = get_automation_policy(root) if policy_path.is_file() else None
    return {
        "schema_version": "1.0",
        "status": "passed",
        "workflow_version": __version__,
        "workflow_source_sha256": workflow_snapshot_checksum(Path(__file__).resolve().parent),
        "automation_mode": policy.get("mode") if policy else None,
        "integration_branch": policy.get("integration_branch") if policy else None,
        "capabilities": {
            "filesystem": True,
            "shell": True,
            "git": shutil.which("git") is not None,
            "github": shutil.which("gh") is not None,
            "godot": any(shutil.which(name) for name in ("godot", "godot4", "godot.cmd")),
            "browser": False,
            "image_generation": False,
            "audio_generation": False,
            "network": os.environ.get("AIGAME_OFFLINE", "0") != "1",
            "human_approval": True,
        },
        "python": sys.version.split()[0],
        "cwd": str(Path.cwd()),
        "project": str(root),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aigame")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="Detect portable workflow capabilities")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.add_argument("--project", type=Path, default=Path.cwd())

    new = subparsers.add_parser("new", help="Create a new portable Godot game repository")
    new.add_argument("name")
    new.add_argument("--destination", type=Path, required=True)
    new.add_argument("--godot-version", required=True)
    new.add_argument("--github", help="Create and push owner/repository after local initialization")
    visibility = new.add_mutually_exclusive_group()
    visibility.add_argument("--public", action="store_true")
    visibility.add_argument("--private", action="store_true")
    new.add_argument("--apply", action="store_true")
    new.add_argument("--json", action="store_true", dest="as_json")

    init = subparsers.add_parser("init", help="Initialize an existing empty Git repository")
    init.add_argument("name")
    init.add_argument("--project", type=Path, default=Path.cwd())
    init.add_argument("--godot-version", required=True)
    init.add_argument("--apply", action="store_true")
    init.add_argument("--json", action="store_true", dest="as_json")

    next_command = subparsers.add_parser("next", help="Select the next deterministic work item")
    next_command.add_argument("--project", type=Path, default=Path.cwd())
    next_command.add_argument("--json", action="store_true", dest="as_json")

    context = subparsers.add_parser("context", help="Build a minimal task context packet")
    context.add_argument("work_item_id")
    context.add_argument("--project", type=Path, default=Path.cwd())
    context.add_argument("--json", action="store_true", dest="as_json")

    claim = subparsers.add_parser("claim", help="Claim the serial active work item")
    claim.add_argument("work_item_id")
    claim.add_argument("--project", type=Path, default=Path.cwd())
    claim.add_argument("--apply", action="store_true")
    claim.add_argument("--json", action="store_true", dest="as_json")

    validate = subparsers.add_parser("validate", help="Validate workflow contracts and traceability")
    validate.add_argument("work_item_id", nargs="?")
    validate.add_argument("--project", type=Path, default=Path.cwd())
    validate.add_argument("--json", action="store_true", dest="as_json")

    checkpoint_command = subparsers.add_parser("checkpoint", help="Persist a resumable run result")
    checkpoint_command.add_argument("work_item_id")
    checkpoint_command.add_argument("--result", type=Path, required=True)
    checkpoint_command.add_argument("--project", type=Path, default=Path.cwd())
    checkpoint_command.add_argument("--apply", action="store_true")
    checkpoint_command.add_argument("--json", action="store_true", dest="as_json")

    sync = subparsers.add_parser("sync", help="Synchronize canonical state to a remote mirror")
    sync_sub = sync.add_subparsers(dest="sync_target", required=True)
    sync_github = sync_sub.add_parser("github")
    sync_github.add_argument("--project", type=Path, default=Path.cwd())
    sync_github.add_argument("--apply", action="store_true")
    sync_github.add_argument("--dry-run", action="store_true")
    sync_github.add_argument("--json", action="store_true", dest="as_json")

    rc = subparsers.add_parser("rc", help="Manage immutable release candidates")
    rc_sub = rc.add_subparsers(dest="rc_command", required=True)
    rc_prepare = rc_sub.add_parser("prepare")
    rc_prepare.add_argument("--artifact", type=Path, required=True)
    rc_prepare.add_argument("--name", required=True)
    rc_prepare.add_argument("--project", type=Path, default=Path.cwd())
    rc_prepare.add_argument("--apply", action="store_true")
    rc_prepare.add_argument("--json", action="store_true", dest="as_json")

    release = subparsers.add_parser("release", help="Promote a verified release candidate")
    release_sub = release.add_subparsers(dest="release_command", required=True)
    release_promote = release_sub.add_parser("promote")
    release_promote.add_argument("--manifest", type=Path, required=True)
    release_promote.add_argument("--approval", type=Path, required=True)
    release_promote.add_argument("--project", type=Path, default=Path.cwd())
    release_promote.add_argument("--apply", action="store_true")
    release_promote.add_argument("--json", action="store_true", dest="as_json")

    upgrade = subparsers.add_parser("upgrade", help="Update the pinned workflow version")
    upgrade.add_argument("--workflow-version", required=True)
    upgrade.add_argument("--commit", required=True)
    upgrade.add_argument("--sha256", required=True)
    upgrade.add_argument("--project", type=Path, default=Path.cwd())
    upgrade.add_argument("--apply", action="store_true")
    upgrade.add_argument("--json", action="store_true", dest="as_json")

    repository = subparsers.add_parser("repository", help="Configure GitHub repository policy")
    repository_sub = repository.add_subparsers(dest="repository_command", required=True)
    repository_configure = repository_sub.add_parser("configure")
    repository_configure.add_argument("repository")
    repository_configure.add_argument("--apply", action="store_true")
    repository_configure.add_argument("--json", action="store_true", dest="as_json")
    repository_configure_game = repository_sub.add_parser(
        "configure-game",
        help="Configure a generated private game repository without changing its visibility",
    )
    repository_configure_game.add_argument("repository")
    repository_configure_game.add_argument("--project", type=Path, default=Path.cwd())
    repository_configure_game.add_argument("--apply", action="store_true")
    repository_configure_game.add_argument("--json", action="store_true", dest="as_json")

    project = subparsers.add_parser("project", help="Configure a GitHub Project v2 board")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    project_configure = project_sub.add_parser("configure")
    project_configure.add_argument("repository")
    project_configure.add_argument("--apply", action="store_true")
    project_configure.add_argument("--json", action="store_true", dest="as_json")

    concept = subparsers.add_parser("concept", help="Build a complete game blueprint from a prompt")
    concept_sub = concept.add_subparsers(dest="concept_command", required=True)

    concept_start = concept_sub.add_parser("start", help="Create the canonical concept intake")
    prompt_group = concept_start.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file", type=Path)
    concept_start.add_argument("--profile", action="append", default=[])
    concept_start.add_argument("--from-existing-docs", action="store_true")
    concept_start.add_argument("--project", type=Path, default=Path.cwd())
    concept_start.add_argument("--apply", action="store_true")
    concept_start.add_argument("--json", action="store_true", dest="as_json")

    concept_next = concept_sub.add_parser("next", help="Return the next resumable concept task")
    concept_next.add_argument("--project", type=Path, default=Path.cwd())
    concept_next.add_argument("--json", action="store_true", dest="as_json")

    concept_submit = concept_sub.add_parser("submit", help="Validate and checkpoint a concept task result")
    concept_submit.add_argument("task_id")
    concept_submit.add_argument("--result", type=Path, required=True)
    concept_submit.add_argument("--approval", type=Path)
    concept_submit.add_argument("--project", type=Path, default=Path.cwd())
    concept_submit.add_argument("--apply", action="store_true")
    concept_submit.add_argument("--json", action="store_true", dest="as_json")

    concept_select = concept_sub.add_parser("select", help="Select one approved concept pitch")
    concept_select.add_argument("pitch_id")
    concept_select.add_argument("--approval", type=Path)
    concept_select.add_argument("--project", type=Path, default=Path.cwd())
    concept_select.add_argument("--apply", action="store_true")
    concept_select.add_argument("--json", action="store_true", dest="as_json")

    concept_validate = concept_sub.add_parser("validate", help="Validate concept contracts and traceability")
    concept_validate.add_argument("--project", type=Path, default=Path.cwd())
    concept_validate.add_argument("--final", action="store_true", dest="require_final")
    concept_validate.add_argument("--json", action="store_true", dest="as_json")

    concept_render = concept_sub.add_parser("render", help="Render deterministic human-readable blueprint documents")
    concept_render.add_argument("--project", type=Path, default=Path.cwd())
    render_mode = concept_render.add_mutually_exclusive_group()
    render_mode.add_argument("--check", action="store_true")
    render_mode.add_argument("--apply", action="store_true")
    concept_render.add_argument("--json", action="store_true", dest="as_json")

    concept_finalize = concept_sub.add_parser("finalize", help="Approve and materialize the complete blueprint")
    concept_finalize.add_argument("--approval", type=Path)
    concept_finalize.add_argument("--project", type=Path, default=Path.cwd())
    concept_finalize.add_argument("--apply", action="store_true")
    concept_finalize.add_argument("--json", action="store_true", dest="as_json")

    concept_revise = concept_sub.add_parser("revise", help="Invalidate approvals and open bounded blueprint rework")
    concept_revise.add_argument("--change-file", type=Path, required=True)
    concept_revise.add_argument("--project", type=Path, default=Path.cwd())
    concept_revise.add_argument("--apply", action="store_true")
    concept_revise.add_argument("--json", action="store_true", dest="as_json")

    mode = subparsers.add_parser("mode", help="Inspect or change the project automation mode")
    mode_sub = mode.add_subparsers(dest="mode_command", required=True)
    mode_show = mode_sub.add_parser("show", help="Show the current automation policy")
    mode_show.add_argument("--project", type=Path, default=Path.cwd())
    mode_show.add_argument("--json", action="store_true", dest="as_json")
    mode_set = mode_sub.add_parser("set", help="Set human-gated or AI staging mode")
    mode_set.add_argument("mode", choices=["human-gated", "ai-staging"])
    mode_set.add_argument("--confirm")
    mode_set.add_argument("--project", type=Path, default=Path.cwd())
    mode_set.add_argument("--apply", action="store_true")
    mode_set.add_argument("--json", action="store_true", dest="as_json")

    staging = subparsers.add_parser("staging", help="Integrate verified pull requests into staging")
    staging_sub = staging.add_subparsers(dest="staging_command", required=True)
    staging_init = staging_sub.add_parser("init", help="Create staging from the default branch")
    staging_init.add_argument("--project", type=Path, default=Path.cwd())
    staging_init.add_argument("--apply", action="store_true")
    staging_init.add_argument("--json", action="store_true", dest="as_json")
    staging_merge = staging_sub.add_parser(
        "merge",
        help="Return the checked, SHA-bound staging approval comment for the repository owner",
    )
    staging_merge.add_argument("pull_request", type=int)
    staging_merge.add_argument("--project", type=Path, default=Path.cwd())
    staging_merge.add_argument("--apply", action="store_true")
    staging_merge.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = _doctor(args.project)
        elif args.command == "new":
            payload = create_game(
                args.destination, args.name, godot_version=args.godot_version, apply=args.apply
            )
            selected_visibility = "public" if args.public else "private"
            if args.github:
                payload["github"] = {
                    "repository": args.github,
                    "visibility": selected_visibility,
                    "action": "create_and_push" if args.apply else "preview",
                }
            if args.apply:
                initialized = subprocess.run(
                    ["git", "init", "-b", "main"],
                    cwd=args.destination,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if initialized.returncode != 0:
                    raise RuntimeError(initialized.stderr.strip() or "Unable to initialize Git")
                identity_defaults = {
                    "user.name": "AI Game Workflow",
                    "user.email": "aigame@users.noreply.github.com",
                }
                for key, default in identity_defaults.items():
                    current = subprocess.run(
                        ["git", "config", "--get", key],
                        cwd=args.destination,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    if current.returncode != 0 or not current.stdout.strip():
                        configured = subprocess.run(
                            ["git", "config", "--local", key, default],
                            cwd=args.destination,
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        if configured.returncode != 0:
                            raise RuntimeError(
                                configured.stderr.strip() or f"Unable to configure {key}"
                            )
                git_commands: list[list[str]] = []
                if shutil.which("git-lfs") or subprocess.run(
                    ["git", "lfs", "version"], capture_output=True, check=False
                ).returncode == 0:
                    git_commands.append(["git", "lfs", "install", "--local"])
                    git_commands.extend(build_lfs_plan(args.destination)["commands"])
                    payload["lfs"] = {"configured": True, "patterns": build_lfs_plan(args.destination)["patterns"]}
                else:
                    payload["lfs"] = {"configured": False, "warning": "Git LFS is unavailable"}
                git_commands.extend(
                    [
                        ["git", "add", "."],
                        ["git", "commit", "-m", "Initialize portable AI game workflow"],
                    ]
                )
                for command in git_commands:
                    result = subprocess.run(
                        command,
                        cwd=args.destination,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(result.stderr.strip() or f"Failed command: {command}")
                payload["git"] = {"branch": "main", "committed": True}
                if args.github:
                    create = subprocess.run(
                        [
                            "gh", "repo", "create", args.github,
                            f"--{selected_visibility}", "--source", str(args.destination),
                            "--remote", "origin", "--push",
                        ],
                        cwd=args.destination,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    if create.returncode != 0:
                        raise RuntimeError(create.stderr.strip() or "Unable to create GitHub repository")
                    payload["github"]["url"] = create.stdout.strip()
                    payload["github"]["policy"] = configure_game_repository(
                        args.github,
                        cwd=args.destination,
                        apply=True,
                    )
                    payload["github"]["project"] = configure_project(
                        args.github, apply=True
                    )
        elif args.command == "init":
            payload = initialize_game(
                args.project, args.name, godot_version=args.godot_version, apply=args.apply
            )
        elif args.command == "next":
            concept_payload = next_concept_task(args.project)
            if concept_payload.get("status") != "finalized":
                payload = concept_payload
            else:
                capabilities = {
                    name
                    for name, enabled in _doctor(args.project)["capabilities"].items()
                    if enabled
                }
                items = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted((args.project / "work" / "items").glob("WI-*.json"))
                ]
                payload = {
                    "schema_version": "1.0",
                    "status": "passed",
                    "work_item": choose_next(
                        items,
                        capabilities,
                        allow_red_unknowns=is_ai_staging(args.project),
                    ),
                }
        elif args.command == "context":
            payload = build_context(args.project, args.work_item_id)
        elif args.command == "claim":
            payload = claim_work_item(args.project, args.work_item_id, apply=args.apply)
        elif args.command == "validate":
            payload = validate_project(args.project)
        elif args.command == "checkpoint":
            payload = checkpoint(args.project, args.work_item_id, args.result, apply=args.apply)
        elif args.command == "sync" and args.sync_target == "github":
            payload = sync_github(args.project, apply=args.apply)
        elif args.command == "rc" and args.rc_command == "prepare":
            payload = prepare_release_candidate(
                args.project, args.artifact, args.name, apply=args.apply
            )
        elif args.command == "release" and args.release_command == "promote":
            payload = promote_release(
                args.project, args.manifest, args.approval, apply=args.apply
            )
        elif args.command == "upgrade":
            payload = upgrade_workflow(
                args.project,
                args.workflow_version,
                args.commit,
                args.sha256,
                apply=args.apply,
            )
        elif args.command == "repository" and args.repository_command == "configure":
            payload = configure_repository(args.repository, apply=args.apply)
        elif args.command == "repository" and args.repository_command == "configure-game":
            payload = configure_game_repository(
                args.repository,
                cwd=args.project,
                apply=args.apply,
            )
        elif args.command == "project" and args.project_command == "configure":
            payload = configure_project(args.repository, apply=args.apply)
        elif args.command == "concept" and args.concept_command == "start":
            prompt = args.prompt
            if args.prompt_file:
                prompt = args.prompt_file.read_text(encoding="utf-8")
            payload = start_concept(
                args.project,
                prompt=prompt,
                profiles=args.profile,
                from_existing_docs=args.from_existing_docs,
                apply=args.apply,
            )
        elif args.command == "concept" and args.concept_command == "next":
            payload = next_concept_task(args.project)
        elif args.command == "concept" and args.concept_command == "submit":
            payload = submit_concept_task(
                args.project,
                args.task_id,
                args.result,
                approval_path=args.approval,
                apply=args.apply,
            )
        elif args.command == "concept" and args.concept_command == "select":
            payload = select_concept_pitch(
                args.project, args.pitch_id, args.approval, apply=args.apply
            )
        elif args.command == "concept" and args.concept_command == "validate":
            payload = validate_concept(args.project, require_final=args.require_final)
        elif args.command == "concept" and args.concept_command == "render":
            payload = render_concept(args.project, apply=args.apply, check=args.check)
        elif args.command == "concept" and args.concept_command == "finalize":
            payload = finalize_concept(args.project, args.approval, apply=args.apply)
        elif args.command == "concept" and args.concept_command == "revise":
            payload = revise_concept(args.project, args.change_file, apply=args.apply)
        elif args.command == "mode" and args.mode_command == "show":
            payload = {
                "schema_version": "1.0",
                "status": "passed",
                "policy": get_automation_policy(args.project),
            }
        elif args.command == "mode" and args.mode_command == "set":
            payload = set_automation_mode(
                args.project,
                args.mode.replace("-", "_"),
                confirmation=args.confirm,
                apply=args.apply,
            )
        elif args.command == "staging" and args.staging_command == "init":
            payload = initialize_staging(args.project, apply=args.apply)
        elif args.command == "staging" and args.staging_command == "merge":
            payload = prepare_staging_merge(
                args.project,
                args.pull_request,
                apply=args.apply,
            )
        else:
            payload = {"schema_version": "1.0", "status": "failed", "error": "Unknown command"}
        print(json.dumps(payload, ensure_ascii=False))
        if payload.get("status") == "failed":
            return 2
        if payload.get("status") == "needs_human":
            return 3
        return 0
    except MissingCapability as error:
        print(json.dumps({"schema_version": "1.0", "status": "blocked", "error": str(error), "missing": error.missing}))
        return error.exit_code
    except HumanRequired as error:
        print(json.dumps({"schema_version": "1.0", "status": "needs_human", "error": str(error)}))
        return error.exit_code
    except WorkflowError as error:
        print(json.dumps({"schema_version": "1.0", "status": "failed", "error": str(error)}))
        return error.exit_code
    except ApprovalRequired as error:
        print(json.dumps({"schema_version": "1.0", "status": "needs_human", "error": str(error)}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
