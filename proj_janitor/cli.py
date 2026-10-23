from __future__ import annotations

import argparse
import json
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
from proj_janitor.models import CleanupAction, Project, human_size
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

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="projjan",
        description="Scan, back up, and clean old projects safely.",
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
        "--dest",
        required=True,
        type=Path,
        help="backup root directory",
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

    clean = subparsers.add_parser(
        "clean",
        help="find and remove known build artifacts (dry-run by default)",
    )
    clean.add_argument("roots", nargs="+", type=Path)
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

    dest = args.dest.expanduser()
    if dest.exists() and not dest.is_dir():
        print(f"error: destination is not a directory: {dest}", file=sys.stderr)
        return 1

    if args.dry_run:
        manifest_path, plans = prepare_backup(
            projects,
            dest,
            include_artifacts=args.include_artifacts,
            always_archive=args.always_archive,
        )
        print(f"Dry run. Manifest would be: {manifest_path}")
        for plan in plans:
            if plan.strategy == "git":
                recovery = plan.recovery
                url = recovery.remote_url if recovery else "-"
                commit = recovery.commit[:12] if recovery and recovery.commit else "-"
                print(f"  [git] {plan.project.name}: {url} @ {commit}")
            else:
                archive_name = plan.archive_name or f"{plan.project.name}.tar.gz"
                archive = manifest_path.parent / (plan.archive_dir or "")
                print(
                    f"  [archive] {plan.project.name}: "
                    f"{human_size(plan.project.size_bytes)}"
                    f" -> {archive / archive_name} ({plan.reason})"
                )
        return 1 if failures else 0

    manifest_path, records = run_backup(
        projects,
        dest,
        include_artifacts=args.include_artifacts,
        always_archive=args.always_archive,
    )
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
            )
    print(f"Manifest: {manifest_path}")
    return 1 if failures else 0


def _cmd_clean(args: argparse.Namespace) -> int:
    if args.list_rules:
        print(format_rules())
        return 0

    projects, failures = _collect_projects(args.roots, include_unmarked=False)
    actions: list[CleanupAction] = []
    for project in projects:
        try:
            actions.extend(build_cleanup_plan(project.path, args.rules))
        except ValueError as exc:
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

    if not args.apply:
        if actions:
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
        answer = input(f"Delete {len(actions)} item(s)? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Aborted.")
            return 2

    result = apply_cleanup(actions)
    print(
        f"Removed {result.removed} item(s), "
        f"freed {human_size(result.freed_bytes)}."
    )
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


def _rule_list(value: str) -> list[str]:
    rules = [item.strip() for item in value.split(",") if item.strip()]
    if not rules:
        raise argparse.ArgumentTypeError("at least one rule is required")
    return rules
