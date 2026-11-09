from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable, Sequence

from proj_janitor import __version__
from proj_janitor.backup import (
    archive_space_insufficient,
    available_disk_bytes,
    estimate_archive_stats,
    find_unsafe_archive_paths,
    format_insecure_dest_refusal,
    format_space_refusal,
    format_unsafe_archive_refusal,
    insecure_dir_mode,
    prepare_backup,
    run_backup,
)
from proj_janitor.config import (
    ConfigError,
    init_config,
    load_config,
    resolve_check_remote,
    resolve_tri_state,
)
from proj_janitor.secrets import (
    find_possible_secret_paths,
    format_secret_exclusion,
    format_secret_refusal,
    format_secret_warning,
)
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
    format_elapsed,
    human_size,
)
from proj_janitor.restore import (
    PartialRestoreError,
    RestoreError,
    execute_restore,
    plan_restore,
    redact_remote_url,
    verify_manifest,
)
from proj_janitor.retention import (
    RetentionError,
    RetentionPlan,
    apply_retention_plan,
    build_retention_plan,
)
from proj_janitor.scanner import (
    discover_projects,
    format_git_unknown_warnings,
    format_projects,
    list_project_paths,
    scan_project,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "scan":
        return _cmd_scan(args)
    if args.command == "backup":
        return _cmd_backup(args)
    if args.command == "clean":
        return _cmd_clean(args)
    if args.command == "prune":
        return _cmd_prune(args)
    if args.command == "restore":
        return _cmd_restore(args)
    if args.command == "verify":
        return _cmd_verify(args)
    if args.command == "init":
        return _cmd_init(args)

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
    scan.add_argument(
        "roots",
        nargs="+",
        type=Path,
        help="project roots or parent directories to scan",
    )
    scan.add_argument(
        "--all",
        action="store_true",
        help="include directories without a known project marker",
    )
    scan.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to a .proj-janitor.toml config file",
    )
    scan_progress = scan.add_mutually_exclusive_group()
    scan_progress.add_argument(
        "--progress",
        action="store_true",
        dest="progress",
        help=(
            "print per-project scan progress with elapsed time on stderr "
            "(overrides [scan].progress)"
        ),
    )
    scan_progress.add_argument(
        "--no-progress",
        action="store_false",
        dest="progress",
        help="disable scan progress even when [scan].progress is true",
    )
    scan.set_defaults(progress=None)
    scan.add_argument("--json", action="store_true", help="emit JSON")

    backup = subparsers.add_parser(
        "backup",
        help="record Git remotes or archive projects into a destination",
    )
    backup.add_argument(
        "roots",
        nargs="+",
        type=Path,
        help="project roots or parent directories to back up",
    )
    backup.add_argument(
        "--backup-dest",
        default=None,
        type=_non_empty_path,
        help="backup root directory (default: BACKUP_DEST env, then [backup].dest)",
    )
    backup.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to a .proj-janitor.toml config file",
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
    backup.add_argument(
        "--keep",
        type=_positive_integer,
        default=None,
        metavar="N",
        help="after a successful backup, retain only N newest manifests per project "
        "(default: [backup].keep)",
    )
    backup.add_argument(
        "--yes",
        action="store_true",
        help="skip confirmation when deleting old backups via --keep",
    )
    backup.add_argument(
        "--incremental",
        action="store_true",
        help="record a delta snapshot (changed/new files) instead of a full archive "
        "(default: [backup].incremental)",
    )
    backup.set_defaults(
        fail_on_secrets=None,
        exclude_secrets=None,
        progress=None,
        check_space=None,
        fail_on_insecure_dest=None,
    )
    backup_secrets = backup.add_mutually_exclusive_group()
    backup_secrets.add_argument(
        "--fail-on-secrets",
        action="store_true",
        dest="fail_on_secrets",
        help=(
            "refuse archive backups when common secret paths are detected "
            "(overrides [backup].fail_on_secrets)"
        ),
    )
    backup_secrets.add_argument(
        "--no-fail-on-secrets",
        action="store_false",
        dest="fail_on_secrets",
        help="warn about secret paths but still archive (overrides config)",
    )
    backup_exclude_secrets = backup.add_mutually_exclusive_group()
    backup_exclude_secrets.add_argument(
        "--exclude-secrets",
        action="store_true",
        dest="exclude_secrets",
        help=(
            "omit common secret paths from archive backups "
            "(overrides [backup].exclude_secrets)"
        ),
    )
    backup_exclude_secrets.add_argument(
        "--no-exclude-secrets",
        action="store_false",
        dest="exclude_secrets",
        help="include secret paths in archives and only warn (overrides config)",
    )
    backup_progress = backup.add_mutually_exclusive_group()
    backup_progress.add_argument(
        "--progress",
        action="store_true",
        dest="progress",
        help=(
            "print per-project and archive packing progress with elapsed time "
            "on stderr; with --dry-run, estimate file count and bytes after "
            "excludes (overrides [backup].progress)"
        ),
    )
    backup_progress.add_argument(
        "--no-progress",
        action="store_false",
        dest="progress",
        help="disable progress output even when [backup].progress is true",
    )
    backup_space = backup.add_mutually_exclusive_group()
    backup_space.add_argument(
        "--check-space",
        action="store_true",
        dest="check_space",
        help=(
            "refuse archive backups when free space on the backup dest is below "
            "the uncompressed size estimate (overrides [backup].check_space)"
        ),
    )
    backup_space.add_argument(
        "--no-check-space",
        action="store_false",
        dest="check_space",
        help="skip free-space preflight even when [backup].check_space is true",
    )
    backup_dest_perms = backup.add_mutually_exclusive_group()
    backup_dest_perms.add_argument(
        "--fail-on-insecure-dest",
        action="store_true",
        dest="fail_on_insecure_dest",
        help=(
            "refuse backup when the destination directory is group/world-accessible "
            "on Unix (overrides [backup].fail_on_insecure_dest)"
        ),
    )
    backup_dest_perms.add_argument(
        "--no-fail-on-insecure-dest",
        action="store_false",
        dest="fail_on_insecure_dest",
        help="allow group/world-accessible backup destinations (overrides config)",
    )
    backup.add_argument(
        "--encrypt-passphrase-file",
        type=Path,
        dest="encrypt_passphrase_file",
        help=(
            "encrypt archive/bundle payloads with openssl using this passphrase file "
            "(overrides [backup].encrypt_passphrase_file)"
        ),
    )
    backup.add_argument(
        "--no-encrypt",
        action="store_true",
        help="disable archive encryption even when [backup].encrypt_passphrase_file is set",
    )

    clean = subparsers.add_parser(
        "clean",
        help="find and remove known build artifacts (dry-run by default)",
    )
    clean.add_argument(
        "roots",
        nargs="*",
        type=Path,
        help="project roots to clean (optional with --list-rules)",
    )
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
    clean.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to a .proj-janitor.toml config file",
    )
    clean_progress = clean.add_mutually_exclusive_group()
    clean_progress.add_argument(
        "--progress",
        action="store_true",
        dest="progress",
        help=(
            "print per-project clean planning and deletion progress with "
            "elapsed time on stderr (overrides [clean].progress)"
        ),
    )
    clean_progress.add_argument(
        "--no-progress",
        action="store_false",
        dest="progress",
        help="disable clean progress even when [clean].progress is true",
    )
    clean.set_defaults(progress=None)
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
    restore.add_argument(
        "manifest",
        type=Path,
        help="manifest JSON produced by backup",
    )
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
        help=(
            "skip confirmation when restoring to original locations "
            "or overwriting existing targets with --force"
        ),
    )
    restore.set_defaults(
        check_remote=None,
        require_dest=None,
        force=None,
        keep_replaced=None,
        continue_on_error=None,
        skip_existing=None,
        progress=None,
    )
    restore_remote = restore.add_mutually_exclusive_group()
    restore_remote.add_argument(
        "--check-remote",
        action="store_true",
        dest="check_remote",
        help=(
            "fail before restoring a Git-only project when its remote, submodule, "
            "or LFS object is unreachable (overrides [restore].check_remote)"
        ),
    )
    restore_remote.add_argument(
        "--no-check-remote",
        action="store_false",
        dest="check_remote",
        help="disable remote probing even when [restore].check_remote is true",
    )
    restore_dest_policy = restore.add_mutually_exclusive_group()
    restore_dest_policy.add_argument(
        "--require-dest",
        action="store_true",
        dest="require_dest",
        help=(
            "require --dest; refuse original-location restore "
            "(overrides [restore].require_dest)"
        ),
    )
    restore_dest_policy.add_argument(
        "--no-require-dest",
        action="store_false",
        dest="require_dest",
        help="allow original-location restore even when [restore].require_dest is true",
    )
    restore_force = restore.add_mutually_exclusive_group()
    restore_force.add_argument(
        "--force",
        action="store_true",
        dest="force",
        help=(
            "replace an existing restore target directory after staging succeeds "
            "(overrides [restore].force); still refuses symlinks and non-directories"
        ),
    )
    restore_force.add_argument(
        "--no-force",
        action="store_false",
        dest="force",
        help="refuse existing restore targets even when [restore].force is true",
    )
    restore_keep_replaced = restore.add_mutually_exclusive_group()
    restore_keep_replaced.add_argument(
        "--keep-replaced",
        action="store_true",
        dest="keep_replaced",
        default=None,
        help=(
            "when --force replaces a target, rename the old directory aside "
            "instead of deleting it (overrides [restore].keep_replaced)"
        ),
    )
    restore_keep_replaced.add_argument(
        "--no-keep-replaced",
        action="store_false",
        dest="keep_replaced",
        help="delete replaced targets even when [restore].keep_replaced is true",
    )
    restore_continue = restore.add_mutually_exclusive_group()
    restore_continue.add_argument(
        "--continue-on-error",
        action="store_true",
        dest="continue_on_error",
        help=(
            "keep restoring remaining projects after a per-project failure "
            "(overrides [restore].continue_on_error); still exits non-zero"
        ),
    )
    restore_continue.add_argument(
        "--no-continue-on-error",
        action="store_false",
        dest="continue_on_error",
        help=(
            "stop at the first per-project failure even when "
            "[restore].continue_on_error is true"
        ),
    )
    restore_skip = restore.add_mutually_exclusive_group()
    restore_skip.add_argument(
        "--skip-existing",
        action="store_true",
        dest="skip_existing",
        help=(
            "skip projects whose restore target directory already exists "
            "(overrides [restore].skip_existing); conflicts with --force"
        ),
    )
    restore_skip.add_argument(
        "--no-skip-existing",
        action="store_false",
        dest="skip_existing",
        help=(
            "refuse existing restore targets even when "
            "[restore].skip_existing is true"
        ),
    )
    restore_progress = restore.add_mutually_exclusive_group()
    restore_progress.add_argument(
        "--progress",
        action="store_true",
        dest="progress",
        help=(
            "print per-project restore progress with elapsed time on stderr, "
            "including SHA-256 hashing, remote probes, archive extract, git "
            "fetch, LFS pull, and submodule update status "
            "(overrides [restore].progress)"
        ),
    )
    restore_progress.add_argument(
        "--no-progress",
        action="store_false",
        dest="progress",
        help="disable restore progress even when [restore].progress is true",
    )
    restore.add_argument(
        "--decrypt-passphrase-file",
        type=Path,
        dest="decrypt_passphrase_file",
        help=(
            "passphrase file for openssl-encrypted archives "
            "(overrides [restore].decrypt_passphrase_file)"
        ),
    )
    restore.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to a .proj-janitor.toml config file",
    )
    restore.add_argument("--json", action="store_true", help="emit JSON")

    verify = subparsers.add_parser(
        "verify",
        help="verify backup archives against a manifest",
    )
    verify.add_argument(
        "manifest",
        type=Path,
        help="manifest JSON produced by backup",
    )
    verify.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="NAME",
        help="verify only this project (repeatable)",
    )
    verify.set_defaults(check_remote=None, progress=None)
    verify_remote = verify.add_mutually_exclusive_group()
    verify_remote.add_argument(
        "--check-remote",
        action="store_true",
        dest="check_remote",
        help=(
            "contact recorded remotes, submodules, and LFS endpoints "
            "(overrides [verify].check_remote)"
        ),
    )
    verify_remote.add_argument(
        "--no-check-remote",
        action="store_false",
        dest="check_remote",
        help="disable remote probing even when [verify].check_remote is true",
    )
    verify_progress = verify.add_mutually_exclusive_group()
    verify_progress.add_argument(
        "--progress",
        action="store_true",
        dest="progress",
        help=(
            "print per-project verify progress with elapsed time on stderr, "
            "including SHA-256 hashing and remote/submodule/LFS probes when "
            "--check-remote is set (overrides [verify].progress)"
        ),
    )
    verify_progress.add_argument(
        "--no-progress",
        action="store_false",
        dest="progress",
        help="disable verify progress even when [verify].progress is true",
    )
    verify.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to a .proj-janitor.toml config file",
    )
    verify.add_argument("--json", action="store_true", help="emit JSON")

    prune = subparsers.add_parser(
        "prune",
        help="preview or remove old backups while retaining the newest snapshots",
    )
    prune.add_argument(
        "backup_root",
        type=Path,
        help="backup destination root that contains per-project snapshots",
    )
    prune.add_argument(
        "--keep",
        type=_positive_integer,
        metavar="N",
        help="keep this many newest manifests per project",
    )
    prune.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to a .proj-janitor.toml config file",
    )
    prune.add_argument("--apply", action="store_true", help="delete the planned backups")
    prune.add_argument("--yes", action="store_true", help="skip interactive confirmation")
    prune_progress = prune.add_mutually_exclusive_group()
    prune_progress.add_argument(
        "--progress",
        action="store_true",
        dest="progress",
        help=(
            "print prune deletion progress with elapsed time on stderr "
            "(overrides [prune].progress)"
        ),
    )
    prune_progress.add_argument(
        "--no-progress",
        action="store_false",
        dest="progress",
        help="disable prune progress even when [prune].progress is true",
    )
    prune.set_defaults(progress=None)
    prune.add_argument("--json", action="store_true", help="emit JSON")

    init = subparsers.add_parser(
        "init",
        help="write a starter .proj-janitor.toml in the current (or given) directory",
    )
    init.add_argument(
        "--dir",
        type=Path,
        default=Path("."),
        help="directory that should receive .proj-janitor.toml (default: .)",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing .proj-janitor.toml",
    )
    return parser


def _cmd_init(args: argparse.Namespace) -> int:
    try:
        path = init_config(args.dir, force=args.force)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {path}")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    progress = resolve_tri_state(
        args.progress,
        config.scan.progress if config else False,
    )

    candidates: list[Path] = []
    seen: set[Path] = set()
    failures = 0
    for root in args.roots:
        try:
            found = list_project_paths(root, args.all)
        except (OSError, NotADirectoryError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            failures += 1
            continue
        for path in found:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(path)

    total = len(candidates)
    progress_started = time.monotonic() if progress else None
    if progress:
        print(f"progress: scanning {total} project(s)", file=sys.stderr)

    projects: list[Project] = []
    for index, path in enumerate(candidates):
        if progress:
            print(
                f"progress: scanning {index + 1}/{total}: {path.name}",
                file=sys.stderr,
            )
        try:
            projects.append(scan_project(path))
        except (OSError, NotADirectoryError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            failures += 1

    if progress:
        elapsed = (
            "0s"
            if progress_started is None
            else format_elapsed(time.monotonic() - progress_started)
        )
        print(
            f"progress: done, scanned {len(projects)} ({elapsed})",
            file=sys.stderr,
        )

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
    for warning in format_git_unknown_warnings(projects):
        print(warning, file=sys.stderr)
    return 1 if failures else 0


def _retain_after_backup(backup_root: Path, keep: int, yes: bool) -> int:
    """Apply automatic retention after a backup, mirroring the Rust behavior."""
    try:
        plan = build_retention_plan(backup_root, keep)
    except (OSError, RetentionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    files = list(plan.files)
    if not files:
        return 0
    total = sum(item.size_bytes for item in files)
    print(f"Retention (keep {keep}):")
    for item in files:
        print(f"  [{item.kind}] {item.path}")
    print(f"{len(files)} file(s), {human_size(total)} would be removed.")
    if not yes:
        if not sys.stdin.isatty():
            print("Non-interactive; skipping retention (use --yes to delete).")
            return 0
        try:
            answer = input(f"Delete {len(files)} file(s)? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            print("Retention skipped.")
            return 0
    result = apply_retention_plan(plan)
    print(f"Deleted {len(result.deleted)} file(s).")
    for err in result.errors:
        print(f"error: {err}", file=sys.stderr)
    return 1 if result.errors else 0


def _cmd_backup(args: argparse.Namespace) -> int:
    projects, failures = _collect_projects(args.roots, include_unmarked=False)
    if not projects:
        print("No projects found.", file=sys.stderr)
        return 1

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    backup_dest = args.backup_dest or os.environ.get("BACKUP_DEST") or None
    if backup_dest is None:
        backup_dest = config.backup.dest if config else None
    if backup_dest is None:
        print(
            "error: set BACKUP_DEST, pass --backup-dest, or configure "
            "[backup].dest",
            file=sys.stderr,
        )
        return 1
    keep = (
        args.keep
        if args.keep is not None
        else (config.backup.keep if config else None)
    )
    exclude_patterns = config.backup.exclude if config else ()
    incremental = args.incremental or (config.backup.incremental if config else False)
    fail_on_secrets = resolve_tri_state(
        args.fail_on_secrets,
        config.backup.fail_on_secrets if config else False,
    )
    exclude_secrets = resolve_tri_state(
        args.exclude_secrets,
        config.backup.exclude_secrets if config else False,
    )
    progress = resolve_tri_state(
        args.progress,
        config.backup.progress if config else False,
    )
    check_space = resolve_tri_state(
        args.check_space,
        config.backup.check_space if config else False,
    )
    fail_on_insecure_dest = resolve_tri_state(
        args.fail_on_insecure_dest,
        config.backup.fail_on_insecure_dest if config else False,
    )
    if getattr(args, "no_encrypt", False):
        encrypt_passphrase_file = None
    else:
        encrypt_passphrase_file = args.encrypt_passphrase_file or (
            config.backup.encrypt_passphrase_file if config else None
        )

    backup_dest = Path(backup_dest).expanduser()
    if backup_dest.exists() and not backup_dest.is_dir():
        print(
            f"error: destination is not a directory: {backup_dest}",
            file=sys.stderr,
        )
        return 1
    if fail_on_insecure_dest:
        insecure = insecure_dir_mode(backup_dest)
        if insecure is not None:
            print(
                format_insecure_dest_refusal(backup_dest, insecure),
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
                exclude_git_metadata=args.exclude_git_metadata,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if progress:
            print(
                f"progress: would backup {len(plans)} project(s)",
                file=sys.stderr,
            )
        safe_manifest_paths = [
            manifest_path
            for manifest_path, plan in zip(manifest_paths, plans, strict=True)
            if plan.strategy != "blocked"
        ]
        _print_dry_run_manifests(safe_manifest_paths)
        blocked = False
        for manifest_path, plan in zip(manifest_paths, plans, strict=True):
            if plan.strategy == "git":
                recovery = plan.recovery
                url = recovery.remote_url if recovery else "-"
                commit = recovery.commit[:12] if recovery and recovery.commit else "-"
                print(f"  [git] {plan.project.name}: {url} @ {commit}")
            elif plan.strategy == "git_bundle":
                recovery = plan.recovery
                commit = recovery.commit[:12] if recovery and recovery.commit else "-"
                print(
                    f"  [git-bundle] {plan.project.name}: {commit}"
                    f" -> {manifest_path.parent / plan.archive_name} ({plan.reason})"
                )
            elif plan.strategy == "blocked":
                print(f"  [error] {plan.project.name}: {plan.reason}")
                blocked = True
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
                    + (
                        " [would encrypt with openssl]"
                        if encrypt_passphrase_file is not None
                        else ""
                    )
                )
                stats = None
                if progress or check_space:
                    stats = estimate_archive_stats(
                        plan.project.path,
                        include_artifacts=args.include_artifacts,
                        exclude_git_metadata=args.exclude_git_metadata,
                        exclude_patterns=exclude_patterns,
                        exclude_secrets=exclude_secrets,
                    )
                if progress and stats is not None:
                    files, nbytes = stats
                    print(
                        f"progress: {plan.project.name}: would pack {files} files, "
                        f"{human_size(nbytes)} after excludes",
                        file=sys.stderr,
                    )
                if check_space and stats is not None:
                    _, nbytes = stats
                    try:
                        available = available_disk_bytes(backup_dest)
                    except OSError as exc:
                        print(
                            f"error: {plan.project.name}: {exc}",
                            file=sys.stderr,
                        )
                        blocked = True
                    else:
                        if archive_space_insufficient(nbytes, available):
                            print(
                                format_space_refusal(
                                    plan.project.name, nbytes, available
                                ),
                                file=sys.stderr,
                            )
                            blocked = True
                secrets = find_possible_secret_paths(
                    plan.project.path,
                    include_artifacts=args.include_artifacts,
                    exclude_git_metadata=args.exclude_git_metadata,
                    exclude_patterns=exclude_patterns,
                )
                if secrets:
                    if exclude_secrets:
                        print(
                            format_secret_exclusion(plan.project.name, secrets),
                            file=sys.stderr,
                        )
                    elif fail_on_secrets:
                        print(
                            format_secret_refusal(plan.project.name, secrets),
                            file=sys.stderr,
                        )
                        blocked = True
                    else:
                        print(
                            format_secret_warning(plan.project.name, secrets),
                            file=sys.stderr,
                        )
                unsafe_paths = find_unsafe_archive_paths(
                    plan.project.path,
                    include_artifacts=args.include_artifacts,
                    exclude_git_metadata=args.exclude_git_metadata,
                    exclude_patterns=exclude_patterns,
                    exclude_secrets=exclude_secrets,
                )
                if unsafe_paths:
                    print(
                        format_unsafe_archive_refusal(plan.project.name, unsafe_paths),
                        file=sys.stderr,
                    )
                    blocked = True
        return 1 if failures or blocked else 0

    try:
        manifest_paths, records, backup_failures = run_backup(
            projects,
            backup_dest,
            include_artifacts=args.include_artifacts,
            always_archive=args.always_archive,
            exclude_git_metadata=args.exclude_git_metadata,
            exclude_patterns=exclude_patterns,
            incremental=incremental,
            fail_on_secrets=fail_on_secrets,
            exclude_secrets=exclude_secrets,
            progress=progress,
            check_space=check_space,
            fail_on_insecure_dest=fail_on_insecure_dest,
            encrypt_passphrase_file=encrypt_passphrase_file,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Backed up {len(records)} project(s).")
    for record in records:
        if record.strategy == "git":
            commit = record.commit[:12] if record.commit else "-"
            print(f"  [git] {record.project}: {record.remote_url} @ {commit}")
        elif record.strategy == "git_bundle":
            commit = record.commit[:12] if record.commit else "-"
            digest = record.sha256[:12] if record.sha256 else "-"
            print(
                f"  [git-bundle] {record.project}: {commit}"
                f"  sha256:{digest}  {record.archive}"
            )
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
    if (
        keep is not None
        and not args.dry_run
        and not failures
        and not backup_failures
    ):
        return _retain_after_backup(backup_dest, keep, args.yes)
    return 1 if failures or backup_failures else 0


def _print_dry_run_manifests(manifest_paths: Sequence[Path]) -> None:
    if not manifest_paths:
        return
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
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    check_remote = resolve_check_remote(
        args.check_remote,
        config.restore.check_remote if config else False,
    )
    require_dest = resolve_tri_state(
        args.require_dest,
        config.restore.require_dest if config else False,
    )
    force = resolve_tri_state(
        args.force,
        config.restore.force if config else False,
    )
    keep_replaced = resolve_tri_state(
        args.keep_replaced,
        config.restore.keep_replaced if config else False,
    )
    continue_on_error = resolve_tri_state(
        args.continue_on_error,
        config.restore.continue_on_error if config else False,
    )
    skip_existing = resolve_tri_state(
        args.skip_existing,
        config.restore.skip_existing if config else False,
    )
    progress = resolve_tri_state(
        args.progress,
        config.restore.progress if config else False,
    )
    decrypt_passphrase_file = args.decrypt_passphrase_file or (
        config.restore.decrypt_passphrase_file if config else None
    )
    if force and skip_existing:
        print(
            "error: --force and --skip-existing cannot be combined",
            file=sys.stderr,
        )
        return 1
    if require_dest and args.dest is None:
        print(
            "error: --dest is required when --require-dest or "
            "[restore].require_dest is enabled",
            file=sys.stderr,
        )
        return 1
    try:
        plans = plan_restore(
            args.manifest,
            args.dest,
            projects=args.project,
            verify=not args.no_verify if args.dry_run else False,
            force=force,
            skip_existing=skip_existing,
        )
    except RestoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    in_place = args.dest is None
    mode = "original" if in_place else "dest"
    overwrites = [plan for plan in plans if plan.overwrite]

    if args.dry_run:
        if args.json:
            payload = {
                "dry_run": True,
                "mode": mode,
                "projects": [
                    _restore_plan_json(plan, keep_replaced=keep_replaced)
                    for plan in plans
                ],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"Restore plan: {len(plans)} project(s)")
        if in_place:
            print("Targets are the original backup locations:")
        for plan in plans:
            _print_restore_plan(plan, keep_replaced=keep_replaced)
        if progress:
            skips = sum(1 for plan in plans if plan.skip)
            print(
                f"progress: would restore {len(plans)} project(s)"
                + (f" ({skips} skip existing)" if skips else ""),
                file=sys.stderr,
            )
        print("Dry run. Re-run without --dry-run to restore.")
        return 0

    needs_confirm = (in_place or bool(overwrites)) and not args.yes
    if needs_confirm:
        reason = "original-location restore"
        if overwrites and not in_place:
            reason = "overwrite restore"
        elif overwrites and in_place:
            reason = "original-location overwrite restore"
        if args.json:
            print(
                f"error: --yes is required for {reason} with --json",
                file=sys.stderr,
            )
            return 2
        if not sys.stdin.isatty():
            print(
                f"error: refusing {reason} without --yes "
                "in a non-interactive session",
                file=sys.stderr,
            )
            return 2
        if overwrites:
            print("About to replace existing restore targets:")
            for plan in overwrites:
                print(f"  {plan.target}")
        elif in_place:
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

    results: list[RestoreResult] = []
    failures: list[tuple[str, str]] = []
    try:
        results = execute_restore(
            plans,
            verify=not args.no_verify,
            check_remote=check_remote,
            decrypt_passphrase_file=decrypt_passphrase_file,
            force=force,
            keep_replaced=keep_replaced,
            continue_on_error=continue_on_error,
            progress=progress,
        )
    except PartialRestoreError as exc:
        # Per-project errors were already printed during execute_restore (CR-132).
        results = exc.results
        failures = list(exc.failures)
    except RestoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "dry_run": False,
            "mode": mode,
            "projects": [_restore_result_json(result) for result in results],
        }
        if failures:
            payload["failures"] = [
                {"project": project, "error": error} for project, error in failures
            ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if failures else 0

    restored = [result for result in results if not result.skipped]
    skipped = [result for result in results if result.skipped]
    print(f"Restored {len(restored)} project(s).")
    for result in restored:
        print(f"  [{result.strategy}] {result.project} -> {result.target}")
    if skipped:
        print(f"Skipped {len(skipped)} existing project(s).")
        for result in skipped:
            print(f"  [skipped] {result.project} -> {result.target}")
    if failures:
        print(
            f"error: one or more restore operations failed ({len(failures)})",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    check_remote = resolve_check_remote(
        args.check_remote,
        config.verify.check_remote if config else False,
    )
    progress = resolve_tri_state(
        args.progress,
        config.verify.progress if config else False,
    )
    try:
        results = verify_manifest(
            args.manifest,
            projects=args.project,
            check_remote=check_remote,
            progress=progress,
        )
    except RestoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "manifest": str(args.manifest.expanduser().resolve()),
            "projects": results,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return _verify_exit_code(results)

    for result in results:
        if result["strategy"] in ("archive", "git_bundle"):
            label = "git-bundle" if result["strategy"] == "git_bundle" else "archive"
            print(
                f"[{label}] {result['project']}: SHA-256 verified "
                f"{result['archive']}"
            )
        elif check_remote:
            print(_verify_remote_line(result))
        else:
            print(
                f"[git] {result['project']}: manifest metadata valid "
                f"(commit {str(result['commit'])[:12]}; remote not checked)"
            )
    print(f"Verified {len(results)} project(s).")
    return _verify_exit_code(results)


def _verify_remote_line(result: dict) -> str:
    project = result["project"]
    commit = str(result["commit"] or "")[:12]
    remote = result.get("remote")
    if not isinstance(remote, dict):
        return f"[git] {project}: manifest metadata valid (commit {commit})"
    url = str(remote["url"])
    status = str(result["status"])
    if status == "remote-verified":
        refs = [str(ref) for ref in remote["refs"]]
        shown = ", ".join(refs[:3])
        if len(refs) > 3:
            shown += f" (+{len(refs) - 3} more)"
        line = f"[git] {project}: commit {commit} is at {shown} on {url}"
    elif status == "remote-moved":
        line = (
            f"[git] {project}: remote reachable, but no remote ref points at "
            f"commit {commit} on {url}; restore fetches by SHA, so the commit "
            "may still be reachable from another ref"
        )
    elif status == "remote-unreachable":
        line = f"[git] {project}: remote {remote['reason']} for {url}"
    elif status == "lfs-unavailable":
        line = f"[git] {project}: LFS objects unavailable on {url}"
    else:
        line = f"[git] {project}: remote {remote['reason']}; not contacted"
    for sub in result.get("submodules") or []:
        if not isinstance(sub, dict):
            continue
        line += (
            f"\n  submodule {sub.get('path')}: {sub.get('status')} "
            f"on {sub.get('url')}"
        )
    for obj in result.get("lfs") or []:
        if not isinstance(obj, dict):
            continue
        oid = str(obj.get("oid") or "")
        short = oid[:12]
        status = str(obj.get("status") or "lfs-skipped")
        reason = obj.get("reason")
        if status == "lfs-verified":
            detail = f"available ({short})"
        elif status == "lfs-missing":
            detail = f"missing ({short}; {reason or 'object-not-found'})"
        elif status == "lfs-unreachable":
            detail = f"unreachable ({short}; {reason or 'lfs-probe-failed'})"
        else:
            detail = f"skipped ({short}; {reason or 'not-probed'})"
        line += f"\n  lfs {detail}"
    return line


def _verify_exit_code(results: list[dict]) -> int:
    if any(
        result["status"] in ("remote-unreachable", "lfs-unavailable")
        for result in results
    ):
        sys.stdout.flush()
        print(
            "error: one or more remotes or LFS objects could not be verified",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    progress = resolve_tri_state(
        args.progress,
        config.prune.progress if config else False,
    )
    keep = args.keep
    if keep is None:
        keep = config.prune.keep if config else None
    if keep is None:
        print(
            "error: set --keep or configure [prune].keep",
            file=sys.stderr,
        )
        return 1
    try:
        plan = build_retention_plan(args.backup_root, keep)
    except (OSError, RetentionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    files = plan.files
    if not args.apply:
        if args.json:
            print(
                json.dumps(
                    {"dry_run": True, **_retention_plan_json(plan)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif files:
            print(f"Prune plan for {plan.backup_root} (keep {plan.keep} per project):")
            for item in files:
                print(f"  [{item.kind}] {item.path}")
            print(
                f"{len(plan.manifests)} manifest(s), {len(plan.artifacts)} artifact(s), "
                f"{human_size(sum(item.size_bytes for item in files))} reclaimable."
            )
            print("Dry run. Re-run with --apply to delete these files.")
        else:
            print("Nothing to prune.")
        return 0

    if not files:
        if args.json:
            print(
                json.dumps(
                    {
                        "dry_run": False,
                        **_retention_plan_json(plan),
                        "deleted": [],
                        "errors": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print("Nothing to prune.")
        return 0
    if args.json and not args.yes:
        print("error: --yes is required for prune --apply with --json", file=sys.stderr)
        return 2
    if not args.json:
        print("Files to delete:")
        for item in files:
            print(f"  [{item.kind}] {item.path}")
    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "error: refusing to prune without --yes in a non-interactive session",
                file=sys.stderr,
            )
            return 2
        try:
            answer = input(f"Delete {len(files)} file(s)? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            print("Aborted.")
            return 2

    result = apply_retention_plan(plan, progress=progress)
    if args.json:
        print(
            json.dumps(
                {
                    "dry_run": False,
                    **_retention_plan_json(plan),
                    "deleted": [
                        {
                            "project": item.project,
                            "kind": item.kind,
                            "path": str(item.path),
                        }
                        for item in result.deleted
                    ],
                    "errors": list(result.errors),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            f"Deleted {len(result.deleted)} file(s), "
            f"freed {human_size(sum(item.size_bytes for item in result.deleted))}."
        )
        for error in result.errors:
            print(f"error: {error}", file=sys.stderr)
    return 1 if result.errors else 0


def _retention_plan_json(plan: RetentionPlan) -> dict:
    return {
        "backup_root": str(plan.backup_root),
        "keep": plan.keep,
        "kept_manifests": plan.kept_manifests,
        "files": [
            {
                "project": item.project,
                "kind": item.kind,
                "path": str(item.path),
                "size_bytes": item.size_bytes,
            }
            for item in plan.files
        ],
    }


def _print_restore_plan(plan: RestorePlan, *, keep_replaced: bool = False) -> None:
    if plan.skip:
        suffix = " (skip existing)"
    elif plan.overwrite and keep_replaced:
        suffix = " (overwrite, keep replaced)"
    elif plan.overwrite:
        suffix = " (overwrite)"
    else:
        suffix = ""
    if plan.strategy == "git":
        remote = redact_remote_url(plan.remote_url or "")
        commit = plan.commit[:12] if plan.commit else "-"
        print(
            f"  [git] {plan.project}: {remote} @ {commit} "
            f"-> {plan.target}{suffix}"
        )
        return
    if plan.strategy == "git_bundle":
        commit = plan.commit[:12] if plan.commit else "-"
        verified = "sha256 verified" if plan.hash_verified else "sha256 unchecked"
        print(
            f"  [git-bundle] {plan.project}: {commit} {plan.archive} "
            f"({verified}) -> {plan.target}{suffix}"
        )
        return
    verified = "sha256 verified" if plan.hash_verified else "sha256 unchecked"
    print(
        f"  [archive] {plan.project}: {plan.archive} "
        f"({verified}) -> {plan.target}{suffix}"
    )


def _restore_plan_json(plan: RestorePlan, *, keep_replaced: bool = False) -> dict:
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
        "overwrite": plan.overwrite,
        "skip": plan.skip,
        "keep_replaced": bool(plan.overwrite and keep_replaced),
    }


def _restore_result_json(result: RestoreResult) -> dict:
    payload = {
        "project": result.project,
        "strategy": result.strategy,
        "target": str(result.target),
        "skipped": result.skipped,
    }
    if result.replaced_aside is not None:
        payload["replaced_aside"] = str(result.replaced_aside)
    return payload


def _cmd_clean(args: argparse.Namespace) -> int:
    if args.list_rules:
        print(format_rules())
        return 0

    if not args.roots:
        print("error: at least one root is required", file=sys.stderr)
        return 2

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    progress = resolve_tri_state(
        args.progress,
        config.clean.progress if config else False,
    )

    projects, failures = _collect_projects(args.roots, include_unmarked=False)
    total_projects = len(projects)
    progress_started = time.monotonic() if progress else None
    if progress:
        print(f"progress: cleaning {total_projects} project(s)", file=sys.stderr)
    actions: list[CleanupAction] = []
    for index, project in enumerate(projects):
        if progress:
            print(
                f"progress: planning {index + 1}/{total_projects}: {project.name}",
                file=sys.stderr,
            )
        try:
            actions.extend(build_cleanup_plan(project.path, args.rules))
        except (OSError, ValueError) as exc:
            print(f"error: {project.name}: {exc}", file=sys.stderr)
            failures += 1

    if progress and not args.apply:
        elapsed = (
            "0s"
            if progress_started is None
            else format_elapsed(time.monotonic() - progress_started)
        )
        print(
            f"progress: done, planned {len(actions)} item(s) ({elapsed})",
            file=sys.stderr,
        )

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

    result = apply_cleanup(actions, progress=progress)
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
        else {
            "branch": project.git.branch,
            "dirty": project.git.dirty,
            **(
                {"detail": project.git.detail}
                if project.git.detail is not None
                else {}
            ),
        },
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


def _positive_integer(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if count < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return count


def _rule_list(value: str) -> list[str]:
    rules = [item.strip() for item in value.split(",") if item.strip()]
    if not rules:
        raise argparse.ArgumentTypeError("at least one rule is required")
    return rules
