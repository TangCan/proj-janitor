from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable, Sequence

from proj_janitor import __version__
from proj_janitor.backup import prepare_backup, run_backup
from proj_janitor.cleaner import (
    apply_cleanup,
    build_cleanup_plan,
    format_cleanup_plan,
    format_rules,
)
from proj_janitor.models import (
    CleanupAction,
    Project,
    RestorePlan,
    RestoreResult,
    human_size,
)
from proj_janitor.restore import (
    RestoreError,
    execute_restore,
    plan_restore,
    redact_remote_url,
    verify_manifest,
)
from proj_janitor.scanner import discover_projects, format_projects


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "scan":
        return _cmd_scan(args)
    if args.command == "backup":
        return _cmd_backup(args)
    if args.command == "clean":
        return _cmd_clean(args)
    if args.command == "restore":
        return _cmd_restore(args)
    if args.command == "verify":
        return _cmd_verify(args)

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="projjan",
        description="Scan, back up, restore, and clean old projects safely.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser(
        "scan",
        help="discover projects and report size and git state",
    )
    scan.add_argument("roots", nargs="+", type=Path)
    scan.add_argument(
        "--all",
        action="store_true",
        help="include directories without a known project marker",
    )
    scan.add_argument("--json", action="store_true", help="emit JSON")

    backup = subparsers.add_parser(
        "backup",
        help="record Git remotes or archive projects into a destination",
    )
    backup.add_argument("roots", nargs="+", type=Path)
    backup.add_argument(
        "--backup-dest",
        default=os.environ.get("BACKUP_DEST") or None,
        required=not bool(os.environ.get("BACKUP_DEST")),
        type=_non_empty_path,
        help="backup root directory (default: BACKUP_DEST)",
    )
    backup.add_argument(
        "--dry-run",
        action="store_true",
        help="show the plan without writing archives",
    )
    backup.add_argument(
        "--include-artifacts",
        action="store_true",
        help="archive build artifacts and dependency caches too",
    )
    backup.add_argument(
        "--always-archive",
        action="store_true",
        help="archive every project even when it can be recovered from GitHub",
    )
    backup.add_argument(
        "--exclude-git-metadata",
        action="store_true",
        help="omit .git metadata from archives; restored archives have no Git history",
    )

    clean = subparsers.add_parser(
        "clean",
        help="find and remove known build artifacts (dry-run by default)",
    )
    clean.add_argument("roots", nargs="*", type=Path)
    clean.add_argument(
        "--rules",
        type=_rule_list,
        default=None,
        help="comma-separated rules to apply (default: all)",
    )
    clean.add_argument(
        "--apply",
        action="store_true",
        help="actually delete planned items",
    )
    clean.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation",
    )
    clean.add_argument("--json", action="store_true", help="emit JSON")
    clean.add_argument(
        "--list-rules",
        action="store_true",
        help="print available cleanup rules and exit",
    )

    restore = subparsers.add_parser(
        "restore",
        help="restore projects from a backup manifest",
    )
    restore.add_argument("manifest", type=Path)
    restore.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="restore parent directory (default: original location in manifest)",
    )
    restore.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="NAME",
        help="restore only this project (repeatable)",
    )
    restore.add_argument(
        "--dry-run",
        action="store_true",
        help="show the restore plan without writing files",
    )
    restore.add_argument(
        "--no-verify",
        action="store_true",
        help="skip archive SHA-256 verification",
    )
    restore.add_argument(
        "--yes",
        action="store_true",
        help="skip confirmation when restoring to original locations",
    )
    restore.add_argument("--json", action="store_true", help="emit JSON")

    verify = subparsers.add_parser(
        "verify",
        help="verify backup archives against a manifest",
    )
    verify.add_argument("manifest", type=Path)
    verify.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="NAME",
        help="verify only this project (repeatable)",
    )
    verify.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def _cmd_scan(args: argparse.Namespace) -> int:
    projects, failures = _collect_projects(args.roots, args.all)
    if args.json:
        print(
            json.dumps(
                [_project_json(project) for project in projects],
                ensure_ascii=False,
                indent=2,
            )
        )
    elif projects:
        print(format_projects(projects))
    else:
        print("No projects found.", file=sys.stderr)
    return 1 if failures else 0


def _cmd_backup(args: argparse.Namespace) -> int:
    projects, failures = _collect_projects(args.roots, include_unmarked=False)
    if not projects:
        print("No projects found.", file=sys.stderr)
        return 1

    backup_dest = args.backup_dest.expanduser()
    if backup_dest.exists() and not backup_dest.is_dir():
        print(
            f"error: destination is not a directory: {backup_dest}",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        try:
            manifest_paths, plans = prepare_backup(
                projects,
                backup_dest,
                include_artifacts=args.include_artifacts,
                always_archive=args.always_archive,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _print_dry_run_manifests(manifest_paths)
        for manifest_path, plan in zip(manifest_paths, plans, strict=True):
            if plan.strategy == "git":
                recovery = plan.recovery
                url = recovery.remote_url if recovery else "-"
                commit = recovery.commit[:12] if recovery and recovery.commit else "-"
                print(f"  [git] {plan.project.name}: {url} @ {commit}")
            else:
                archive_name = plan.archive_name or f"{plan.project.name}.tar.gz"
                archive = manifest_path.parent / archive_name
                print(
                    f"  [archive] {plan.project.name}: "
                    f"{human_size(plan.project.size_bytes)}"
                    f" -> {archive} ({plan.reason})"
                    + (
                        " [Git metadata exclusion enabled; restored archive has no Git metadata]"
                        if args.exclude_git_metadata
                        else ""
                    )
                )
        return 1 if failures else 0

    try:
        manifest_paths, records, backup_failures = run_backup(
            projects,
            backup_dest,
            include_artifacts=args.include_artifacts,
            always_archive=args.always_archive,
            exclude_git_metadata=args.exclude_git_metadata,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Backed up {len(records)} project(s).")
    for record in records:
        if record.strategy == "git":
            commit = record.commit[:12] if record.commit else "-"
            print(f"  [git] {record.project}: {record.remote_url} @ {commit}")
        else:
            digest = record.sha256[:12] if record.sha256 else "-"
            reason = (
                f"  ({record.fallback_reason})"
                if record.fallback_reason
                else ""
            )
            print(
                f"  [archive] {record.project}: "
                f"{human_size(record.archive_bytes)}"
                f"  sha256:{digest}  {record.archive}{reason}"
                + (
                    " [Git metadata exclusion enabled; restored archive has no Git metadata]"
                    if args.exclude_git_metadata
                    else ""
                )
            )
    if manifest_paths:
        _print_manifests(manifest_paths)
    for project, error in backup_failures:
        print(f"error: {project}: {error}", file=sys.stderr)
    return 1 if failures or backup_failures else 0


def _print_dry_run_manifests(manifest_paths: Sequence[Path]) -> None:
    if len(manifest_paths) == 1:
        print(f"Dry run. Manifest would be: {manifest_paths[0]}")
        return
    print("Dry run. Manifests would be:")
    for manifest_path in manifest_paths:
        print(f"  {manifest_path}")


def _print_manifests(manifest_paths: Sequence[Path]) -> None:
    if len(manifest_paths) == 1:
        print(f"Manifest: {manifest_paths[0]}")
        return
    print("Manifests:")
    for manifest_path in manifest_paths:
        print(f"  {manifest_path}")


def _cmd_restore(args: argparse.Namespace) -> int:
    try:
        plans = plan_restore(
            args.manifest,
            args.dest,
            projects=args.project,
            verify=not args.no_verify,
        )
    except RestoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    in_place = args.dest is None
    mode = "original" if in_place else "dest"

    if args.dry_run:
        if args.json:
            payload = {
                "dry_run": True,
                "mode": mode,
                "projects": [_restore_plan_json(plan) for plan in plans],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"Restore plan: {len(plans)} project(s)")
        if in_place:
            print("Targets are the original backup locations:")
        for plan in plans:
            _print_restore_plan(plan)
        print("Dry run. Re-run without --dry-run to restore.")
        return 0

    if in_place and not args.yes:
        if args.json:
            print(
                "error: --yes is required for original-location restore "
                "with --json",
                file=sys.stderr,
            )
            return 2
        if not sys.stdin.isatty():
            print(
                "error: refusing to restore to original locations without "
                "--yes in a non-interactive session",
                file=sys.stderr,
            )
            return 2
        print("About to restore to original locations:")
        for plan in plans:
            print(f"  {plan.target}")
        try:
            answer = input(
                f"Restore {len(plans)} project(s)? [y/N] "
            ).strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            print("Aborted.")
            return 2

    try:
        results = execute_restore(plans)
    except RestoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "dry_run": False,
            "mode": mode,
            "projects": [_restore_result_json(result) for result in results],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Restored {len(results)} project(s).")
    for result in results:
        print(f"  [{result.strategy}] {result.project} -> {result.target}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        results = verify_manifest(args.manifest, projects=args.project)
    except RestoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "manifest": str(args.manifest.expanduser().resolve()),
            "projects": results,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    for result in results:
        if result["strategy"] == "archive":
            print(
                f"[archive] {result['project']}: SHA-256 verified "
                f"{result['archive']}"
            )
        else:
            print(
                f"[git] {result['project']}: manifest metadata valid "
                f"(commit {result['commit'][:12]}; remote not checked)"
            )
    print(f"Verified {len(results)} project(s).")
    return 0


def _print_restore_plan(plan: RestorePlan) -> None:
    if plan.strategy == "git":
        remote = redact_remote_url(plan.remote_url or "")
        commit = plan.commit[:12] if plan.commit else "-"
        print(
            f"  [git] {plan.project}: {remote} @ {commit} "
            f"-> {plan.target}"
        )
        return
    verified = "sha256 verified" if plan.hash_verified else "sha256 unchecked"
    print(
        f"  [archive] {plan.project}: {plan.archive} "
        f"({verified}) -> {plan.target}"
    )


def _restore_plan_json(plan: RestorePlan) -> dict:
    return {
        "project": plan.project,
        "strategy": plan.strategy,
        "target": str(plan.target),
        "archive": str(plan.archive) if plan.archive else None,
        "remote_url": redact_remote_url(plan.remote_url or "")
        if plan.remote_url
        else None,
        "commit": plan.commit,
        "sha256": plan.sha256,
        "sha256_verified": plan.hash_verified,
        "in_place": plan.in_place,
    }


def _restore_result_json(result: RestoreResult) -> dict:
    return {
        "project": result.project,
        "strategy": result.strategy,
        "target": str(result.target),
    }


def _cmd_clean(args: argparse.Namespace) -> int:
    if args.list_rules:
        print(format_rules())
        return 0

    if not args.roots:
        print("error: at least one root is required", file=sys.stderr)
        return 2

    projects, failures = _collect_projects(args.roots, include_unmarked=False)
    actions: list[CleanupAction] = []
    for project in projects:
        try:
            actions.extend(build_cleanup_plan(project.path, args.rules))
        except (OSError, ValueError) as exc:
            print(f"error: {project.name}: {exc}", file=sys.stderr)
            failures += 1

    if args.json:
        print(
            json.dumps(
                [_action_json(action) for action in actions],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(format_cleanup_plan(actions))

    if args.apply and failures:
        return 1

    if not args.apply:
        if actions and not args.json:
            print("Dry run. Re-run with --apply to delete these items.")
        return 1 if failures else 0
    if not actions:
        return 1 if failures else 0

    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "error: refusing to delete without --yes in a non-interactive session",
                file=sys.stderr,
            )
            return 2
        prompt = f"Delete {len(actions)} item(s)? [y/N] "
        if args.json:
            print(prompt, end="", file=sys.stderr)
            answer = input().strip().lower()
        else:
            answer = input(prompt).strip().lower()
        if answer not in {"y", "yes"}:
            print("Aborted.", file=sys.stderr if args.json else sys.stdout)
            return 2

    result = apply_cleanup(actions)
    summary = (
        f"Removed {result.removed} item(s), "
        f"freed {human_size(result.freed_bytes)}."
    )
    print(summary, file=sys.stderr if args.json else sys.stdout)
    for error in result.errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if failures or result.errors else 0


def _collect_projects(
    roots: Iterable[Path],
    include_unmarked: bool,
) -> tuple[list[Project], int]:
    projects: list[Project] = []
    failures = 0
    seen: set[Path] = set()
    for root in roots:
        try:
            found = discover_projects(root, include_unmarked)
        except (OSError, NotADirectoryError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            failures += 1
            continue
        for project in found:
            if project.path in seen:
                continue
            seen.add(project.path)
            projects.append(project)
    return projects, failures


def _project_json(project: Project) -> dict:
    return {
        "name": project.name,
        "path": str(project.path),
        "size_bytes": project.size_bytes,
        "last_modified": project.last_modified.isoformat()
        if project.last_modified
        else None,
        "markers": list(project.markers),
        "git": None
        if project.git is None
        else {"branch": project.git.branch, "dirty": project.git.dirty},
    }


def _action_json(action: CleanupAction) -> dict:
    return {
        "project": str(action.project),
        "path": str(action.path),
        "rule": action.rule,
        "kind": action.kind,
        "size_bytes": action.size_bytes,
    }


def _non_empty_path(value: str) -> Path:
    if not value:
        raise argparse.ArgumentTypeError("path must not be empty")
    return Path(value)


def _rule_list(value: str) -> list[str]:
    rules = [item.strip() for item in value.split(",") if item.strip()]
    if not rules:
        raise argparse.ArgumentTypeError("at least one rule is required")
    return rules
