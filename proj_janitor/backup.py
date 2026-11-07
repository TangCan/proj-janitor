from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, Iterable

from proj_janitor import __version__
from proj_janitor.config import (
    COVERAGE_DIR,
    artifact_patterns,
    is_coverage_artifact,
)
from proj_janitor.models import BackupPlan, BackupRecord, GitRecovery, Project, human_size
from proj_janitor.crypto import (
    OPENSSL_AES_256_CBC_PBKDF2,
    encrypted_sibling,
    openssl_encrypt_file,
    validate_passphrase_file,
)
from proj_janitor.secrets import (
    SecretPathsError,
    find_possible_secret_paths,
    format_secret_exclusion,
    format_secret_refusal_detail,
    format_secret_warning,
    path_looks_like_secret,
)
from proj_janitor.scanner import (
    collect_lfs_objects,
    collect_submodules,
    inspect_git_bundle_recoverability,
    inspect_git_recoverability,
)

_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_WINDOWS_INVALID_CHARS = '<>:"|?*'
_BLOCKED_BACKUP_REASONS = (
    "remote URL contains credentials or query data",
    "submodule URL contains credentials or query data",
    "Git config contains HTTP extraHeader",
    "Git config contains credential passwords",
    "Git config contains an inline credential helper",
    "Git config contains sensitive credential helper arguments",
    "Git config contains a custom SSH command",
    "Git config points to a credential file inside the project",
    "Git config contains credentials or query data in a service URL",
    "Git config contains credentials in a URL rewrite",
    "LFS config contains credentials or query data",
    "cannot safely inspect LFS config for credentials",
    "cannot safely inspect .gitmodules for credentials",
    "cannot safely inspect Git config for credentials",
)


def _is_blocked_backup_reason(reason: str | None) -> bool:
    return reason is not None and reason.startswith(_BLOCKED_BACKUP_REASONS)


def prepare_backup(
    projects: Iterable[Project],
    backup_dest: Path,
    include_artifacts: bool = False,
    always_archive: bool = False,
    exclude_git_metadata: bool = False,
) -> tuple[list[Path], list[BackupPlan]]:
    backup_dest = backup_dest.expanduser().resolve()
    project_list = list(projects)
    for project in project_list:
        try:
            str(project.path).encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"project path is not valid UTF-8: {project.path!r}") from exc
        if not safe_archive_member(project.name, project.name):
            raise ValueError(f"unsafe project name: {project.name!r}")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    patterns = artifact_patterns(include_artifacts)
    plans: list[BackupPlan] = []
    manifest_paths: list[Path] = []
    project_dirs = _project_directory_names(project_list)
    used_archives: set[Path] = set()
    used_manifests: set[Path] = set()

    for project in project_list:
        project_dir = project_dirs[project.path]
        output_dir = backup_dest / project_dir
        if output_dir.resolve().is_relative_to(project.path):
            raise ValueError(
                f"backup destination is inside project: {output_dir} -> {project.path}"
            )
        manifest_path = _unique_file(
            output_dir,
            f"manifest-{timestamp}.json",
            used_manifests,
        )
        used_manifests.add(manifest_path)
        manifest_paths.append(manifest_path)

        recovery = None
        if include_artifacts:
            strategy = "archive"
            reason = "include_artifacts"
        elif always_archive:
            strategy = "archive"
            reason = "always_archive"
        else:
            recovery = inspect_git_recoverability(project, patterns)
            if recovery.eligible:
                strategy = "git"
                reason = None
            elif _is_blocked_backup_reason(recovery.reason):
                strategy = "blocked"
                reason = recovery.reason
            elif not exclude_git_metadata and (
                bundle := inspect_git_bundle_recoverability(project, patterns)
            ).eligible:
                strategy = "git_bundle"
                reason = recovery.reason or "not recoverable from GitHub"
                recovery = bundle
            else:
                strategy = "archive"
                reason = recovery.reason or "not recoverable from GitHub"

        archive_name = None
        if strategy in ("archive", "git_bundle"):
            archive_name = _unique_artifact_name(
                project.name or "project",
                timestamp,
                backup_dest / project_dir,
                used_archives,
                ".bundle" if strategy == "git_bundle" else ".tar.gz",
            )
        plans.append(
            BackupPlan(
                project=project,
                archive_name=archive_name,
                strategy=strategy,
                recovery=recovery,
                reason=reason,
            )
        )
    return manifest_paths, plans


def run_backup(
    projects: Iterable[Project],
    backup_dest: Path,
    include_artifacts: bool = False,
    dry_run: bool = False,
    always_archive: bool = False,
    exclude_git_metadata: bool = False,
    exclude_patterns: Iterable[str] = (),
    incremental: bool = False,
    fail_on_secrets: bool = False,
    exclude_secrets: bool = False,
    progress: bool = False,
    check_space: bool = False,
    fail_on_insecure_dest: bool = False,
    encrypt_passphrase_file: Path | None = None,
) -> tuple[list[Path], list[BackupRecord], list[tuple[str, str]]]:
    backup_dest = backup_dest.expanduser().resolve()
    if encrypt_passphrase_file is not None:
        validate_passphrase_file(encrypt_passphrase_file)
    if fail_on_insecure_dest:
        insecure = insecure_dir_mode(backup_dest)
        if insecure is not None:
            raise ValueError(
                format_insecure_dest_refusal(backup_dest, insecure).removeprefix(
                    "error: "
                )
            )
    manifest_paths, plans = prepare_backup(
        projects,
        backup_dest,
        include_artifacts=include_artifacts,
        always_archive=always_archive,
        exclude_git_metadata=exclude_git_metadata,
    )
    if dry_run:
        failures = [
            (plan.project.name, plan.reason or "unsafe to archive")
            for plan in plans
            if plan.strategy == "blocked"
        ]
        return manifest_paths, [], failures

    if manifest_paths and not backup_dest.exists():
        backup_dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    patterns = artifact_patterns(include_artifacts)
    records: list[BackupRecord] = []
    completed_paths: list[Path] = []
    failures: list[tuple[str, str]] = []
    created_at = datetime.now(timezone.utc).isoformat()
    for manifest_path, plan in zip(manifest_paths, plans, strict=True):
        if plan.strategy == "blocked":
            failures.append(
                (plan.project.name, plan.reason or "unsafe to archive")
            )
            continue
        if plan.strategy == "archive" and fail_on_secrets and not exclude_secrets:
            secrets = find_possible_secret_paths(
                plan.project.path,
                include_artifacts=include_artifacts,
                exclude_git_metadata=exclude_git_metadata,
                exclude_patterns=tuple(exclude_patterns),
            )
            if secrets:
                failures.append(
                    (plan.project.name, format_secret_refusal_detail(secrets))
                )
                continue
        if plan.strategy == "archive" and check_space:
            _, nbytes = estimate_archive_stats(
                plan.project.path,
                include_artifacts=include_artifacts,
                exclude_git_metadata=exclude_git_metadata,
                exclude_patterns=tuple(exclude_patterns),
                exclude_secrets=exclude_secrets,
            )
            try:
                available = available_disk_bytes(backup_dest)
            except OSError as exc:
                failures.append((plan.project.name, str(exc)))
                continue
            if archive_space_insufficient(nbytes, available):
                failures.append(
                    (
                        plan.project.name,
                        format_space_refusal(
                            plan.project.name, nbytes, available
                        ).removeprefix(f"error: {plan.project.name}: "),
                    )
                )
                continue
        manifest_identity: tuple[int, int] | None = None
        try:
            manifest_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with _open_new_file(manifest_path) as manifest_file:
                reserved = os.fstat(manifest_file.fileno())
                manifest_identity = reserved.st_dev, reserved.st_ino
        except OSError as exc:
            if manifest_identity is not None:
                _discard_partial_file_if_same(manifest_path, manifest_identity)
            failures.append((plan.project.name, str(exc)))
            continue
        except BaseException:
            if manifest_identity is not None:
                _discard_partial_file_if_same(manifest_path, manifest_identity)
            raise
        assert manifest_identity is not None
        try:
            record = _backup_one(
                plan,
                manifest_path,
                manifest_identity,
                patterns,
                created_at,
                include_artifacts,
                always_archive,
                exclude_git_metadata,
                exclude_patterns,
                incremental,
                fail_on_secrets=False,
                exclude_secrets=exclude_secrets,
                progress=progress,
                encrypt_passphrase_file=encrypt_passphrase_file,
            )
        except (OSError, tarfile.TarError) as exc:
            _discard_partial_file_if_same(manifest_path, manifest_identity)
            failures.append((plan.project.name, str(exc)))
            continue
        except BaseException:
            _discard_partial_file_if_same(manifest_path, manifest_identity)
            raise
        records.append(record)
        completed_paths.append(manifest_path)
    return completed_paths, records, failures


def _backup_one(
    plan: BackupPlan,
    manifest_path: Path,
    manifest_identity: tuple[int, int],
    patterns: tuple[str, ...],
    created_at: str,
    include_artifacts: bool,
    always_archive: bool,
    exclude_git_metadata: bool,
    exclude_patterns: Iterable[str] = (),
    incremental: bool = False,
    fail_on_secrets: bool = False,
    exclude_secrets: bool = False,
    progress: bool = False,
    encrypt_passphrase_file: Path | None = None,
) -> BackupRecord:
    archive_path: Path | None = None
    archive_identity: tuple[int, int] | None = None
    if plan.strategy in ("git", "git_bundle"):
        recovery = plan.recovery
        if recovery is None:
            raise RuntimeError("git backup plan is missing recovery details")
        if plan.strategy == "git":
            record = BackupRecord(
                project=plan.project.name,
                source=str(plan.project.path),
                archive=None,
                archive_bytes=0,
                source_bytes=plan.project.size_bytes,
                sha256=None,
                strategy="git",
                remote=recovery.remote,
                remote_url=recovery.remote_url,
                branch=recovery.branch,
                commit=recovery.commit,
                upstream=recovery.upstream,
                verified=recovery.verified,
                submodules=collect_submodules(plan.project.path, recovery.commit),
                lfs_objects=collect_lfs_objects(plan.project.path),
            )
        else:
            if plan.archive_name is None or recovery.commit is None:
                raise RuntimeError("git bundle plan is incomplete")
            archive_path = manifest_path.parent / plan.archive_name
            record, archive_identity = _create_git_bundle(
                plan.project, archive_path, recovery, plan.reason
            )
            if encrypt_passphrase_file is not None:
                record, archive_identity = _encrypt_backup_payload(
                    record, Path(record.archive), archive_identity, encrypt_passphrase_file
                )
                archive_path = Path(record.archive) if record.archive else archive_path
    else:
        if plan.archive_name is None:
            raise RuntimeError("archive backup plan is missing an archive name")
        secrets = find_possible_secret_paths(
            plan.project.path,
            include_artifacts=include_artifacts,
            exclude_git_metadata=exclude_git_metadata,
            exclude_patterns=tuple(exclude_patterns),
        )
        if secrets:
            if fail_on_secrets and not exclude_secrets:
                raise SecretPathsError(format_secret_refusal_detail(secrets))
            if exclude_secrets:
                print(
                    format_secret_exclusion(plan.project.name, secrets),
                    file=sys.stderr,
                )
            else:
                print(
                    format_secret_warning(plan.project.name, secrets),
                    file=sys.stderr,
                )
        archive_dir = manifest_path.parent
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / plan.archive_name
        base_name: str | None = None
        previous: dict[str, str] | None = None
        if incremental:
            base_name, previous = _previous_inventory(archive_dir)
        record, archive_identity = _backup_project_with_identity(
            plan.project,
            archive_dir,
            patterns=patterns,
            archive_name=plan.archive_name,
            fallback_reason=plan.reason,
            exclude_git_metadata=exclude_git_metadata,
            exclude_patterns=exclude_patterns,
            incremental=incremental,
            base_name=base_name,
            previous=previous,
            exclude_secrets=exclude_secrets,
            progress=progress,
            encrypt_passphrase_file=encrypt_passphrase_file,
        )
        if record.archive:
            archive_path = Path(record.archive)

    manifest = {
        "schema_version": 2,
        "created_at": created_at,
        "tool": "proj-janitor",
        "version": __version__,
        "include_artifacts": include_artifacts,
        "always_archive": always_archive,
        "exclude_git_metadata": exclude_git_metadata and record.strategy == "archive",
        "records": [asdict(record)],
    }
    try:
        if archive_path is not None and archive_identity is not None:
            current = archive_path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != archive_identity:
                raise OSError("archive path changed before manifest was written")
        _write_reserved_file(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
            manifest_identity,
        )
        if archive_path is not None and archive_identity is not None:
            current = archive_path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != archive_identity:
                raise OSError("archive path changed while manifest was written")
    except BaseException:
        if archive_path is not None and archive_identity is not None:
            _discard_partial_file_if_same(archive_path, archive_identity)
        raise
    return record


def _write_reserved_file(path: Path, contents: bytes, identity: tuple[int, int]) -> None:
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != identity:
            raise OSError("manifest path changed before it was written")
        with os.fdopen(descriptor, "r+b") as manifest_file:
            descriptor = -1
            manifest_file.seek(0)
            manifest_file.write(contents)
            manifest_file.truncate()
        current = path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            raise OSError("manifest path changed while it was written")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _discard_partial_file_if_same(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) == identity:
            path.unlink()
    except OSError:
        pass


def _open_new_file(path: Path, binary: bool = False):
    flags = (os.O_RDWR if binary else os.O_WRONLY) | os.O_CREAT | os.O_EXCL
    if binary:
        flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        created = os.fstat(descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        # shortcut: leave an empty file if fstat fails; use handle-based cleanup if observable.
        raise
    identity = created.st_dev, created.st_ino
    try:
        if binary:
            return os.fdopen(descriptor, "w+b")
        return os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        _discard_partial_file_if_same(path, identity)
        raise


def backup_project(
    project: Project,
    archive_dir: Path,
    patterns: Iterable[str] = (),
    archive_name: str | None = None,
    fallback_reason: str | None = None,
    exclude_git_metadata: bool = False,
) -> BackupRecord:
    record, _ = _backup_project_with_identity(
        project,
        archive_dir,
        patterns=patterns,
        archive_name=archive_name,
        fallback_reason=fallback_reason,
        exclude_git_metadata=exclude_git_metadata,
    )
    return record


def _create_git_bundle(
    project: Project,
    bundle_path: Path,
    recovery: GitRecovery,
    fallback_reason: str | None,
) -> tuple[BackupRecord, tuple[int, int]]:
    temporary = Path(
        tempfile.mkdtemp(prefix=".projjan-bundle-", dir=bundle_path.parent)
    )
    temporary_bundle = temporary / "bundle"
    identity: tuple[int, int] | None = None
    try:
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(project.path),
                    "bundle",
                    "create",
                    str(temporary_bundle),
                    "--all",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=600,
                check=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError("git bundle creation timed out") from exc
        if result.returncode:
            raise OSError(result.stderr.strip() or "git bundle creation failed")
        with temporary_bundle.open("rb") as source, _open_new_file(
            bundle_path, binary=True
        ) as target:
            created = os.fstat(target.fileno())
            identity = created.st_dev, created.st_ino
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            bundle_bytes = os.fstat(target.fileno()).st_size
            target.seek(0)
            digest = sha256_stream(target)
            current = bundle_path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != identity:
                raise OSError("bundle path changed while it was being written")
    except BaseException:
        if identity is not None:
            _discard_partial_file_if_same(bundle_path, identity)
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    assert identity is not None
    return (
        BackupRecord(
            project=project.name,
            source=str(project.path),
            archive=str(bundle_path),
            archive_bytes=bundle_bytes,
            source_bytes=project.size_bytes,
            sha256=digest,
            strategy="git_bundle",
            branch=recovery.branch,
            commit=recovery.commit,
            verified="local-bundle",
            fallback_reason=fallback_reason,
        ),
        identity,
    )


def _backup_project_with_identity(
    project: Project,
    archive_dir: Path,
    patterns: Iterable[str] = (),
    archive_name: str | None = None,
    fallback_reason: str | None = None,
    exclude_git_metadata: bool = False,
    exclude_patterns: Iterable[str] = (),
    incremental: bool = False,
    base_name: str | None = None,
    previous: dict[str, str] | None = None,
    exclude_secrets: bool = False,
    progress: bool = False,
    encrypt_passphrase_file: Path | None = None,
) -> tuple[BackupRecord, tuple[int, int]]:
    name = archive_name or f"{project.name}.tar.gz"
    archive = archive_dir / name
    patterns = tuple(patterns)
    exclude_patterns = tuple(exclude_patterns)

    digest, archive_bytes, archive_identity, inventory, deleted = (
        _create_incremental_archive(
            project,
            archive,
            patterns,
            exclude_git_metadata,
            exclude_patterns,
            previous,
            delta=incremental,
            exclude_secrets=exclude_secrets,
            progress_label=project.name if progress else None,
        )
    )
    incremental_info = (
        {"base": base_name, "deleted": deleted} if incremental else None
    )
    record = BackupRecord(
        project=project.name,
        source=str(project.path),
        archive=str(archive),
        archive_bytes=archive_bytes,
        source_bytes=project.size_bytes,
        sha256=digest,
        fallback_reason=fallback_reason,
        inventory=inventory,
        incremental=incremental_info,
    )
    if encrypt_passphrase_file is not None:
        record, archive_identity = _encrypt_backup_payload(
            record, archive, archive_identity, encrypt_passphrase_file
        )
    return record, archive_identity


def _encrypt_backup_payload(
    record: BackupRecord,
    archive: Path,
    archive_identity: tuple[int, int],
    passphrase_file: Path,
) -> tuple[BackupRecord, tuple[int, int]]:
    enc_path = encrypted_sibling(archive)
    digest, size = openssl_encrypt_file(archive, enc_path, passphrase_file)
    try:
        current = archive.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) == archive_identity:
            archive.unlink()
    except OSError:
        pass
    enc_stat = enc_path.stat(follow_symlinks=False)
    enc_identity = (enc_stat.st_dev, enc_stat.st_ino)
    return (
        BackupRecord(
            project=record.project,
            source=record.source,
            archive=str(enc_path),
            archive_bytes=size,
            source_bytes=record.source_bytes,
            sha256=digest,
            strategy=record.strategy,
            remote=record.remote,
            remote_url=record.remote_url,
            branch=record.branch,
            commit=record.commit,
            upstream=record.upstream,
            verified=record.verified,
            fallback_reason=record.fallback_reason,
            inventory=record.inventory,
            incremental=record.incremental,
            submodules=record.submodules,
            lfs_objects=record.lfs_objects,
            encryption=OPENSSL_AES_256_CBC_PBKDF2,
        ),
        enc_identity,
    )


def _previous_inventory(backup_dir: Path) -> tuple[str | None, dict[str, str] | None]:
    """Return the (manifest filename, path->sha256 inventory) of the newest
    complete snapshot in `backup_dir`, or (None, None) when there is none.
    Mirrors the Rust `previous_inventory`. Incomplete manifests (empty or
    unparseable, e.g. a manifest reserved by an in-progress backup) are
    skipped so they never become a base."""
    if not backup_dir.is_dir():
        return None, None
    try:
        manifests = sorted(
            (
                path
                for path in backup_dir.glob("manifest-*.json")
                if path.is_file() and path.stat().st_size > 0
            ),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return None, None
    for newest in reversed(manifests):
        try:
            data = json.loads(newest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        records = data.get("records") or []
        if not records:
            continue
        inventory = records[0].get("inventory")
        if not isinstance(inventory, list):
            continue
        mapping: dict[str, str] = {}
        for entry in inventory:
            if (
                isinstance(entry, dict)
                and "path" in entry
                and "sha256" in entry
            ):
                mapping[str(entry["path"])] = str(entry["sha256"])
        return newest.name, (mapping or None)
    return None, None


def _create_incremental_archive(
    project: Project,
    archive: Path,
    patterns: tuple[str, ...],
    exclude_git_metadata: bool,
    exclude_patterns: tuple[str, ...],
    previous: dict[str, str] | None,
    delta: bool,
    exclude_secrets: bool = False,
    progress_label: str | None = None,
) -> tuple[str, int, tuple[int, int], list[dict], list[str]]:
    """Create an archive recording the full file inventory and, when `delta`
    is true, only changed/new files (plus symlinks) plus the sorted deleted
    paths. Mirrors the Rust `create_incremental_archive`."""
    name = project.name
    root = project.path
    exclude_filter = _make_filter(
        name,
        patterns,
        root,
        exclude_git_metadata,
        exclude_patterns,
        exclude_secrets,
    )
    inventory: list[dict] = []
    seen_files = 0
    seen_bytes = 0
    archive_file = _open_new_file(archive, binary=True)
    archive_identity: tuple[int, int] | None = None
    try:
        with archive_file:
            created = os.fstat(archive_file.fileno())
            archive_identity = created.st_dev, created.st_ino
            with tarfile.open(fileobj=archive_file, mode="w:gz") as tar:
                root_info = tar.gettarinfo(root, arcname=name)
                root_filtered = exclude_filter(root_info)
                if root_filtered is None:
                    raise tarfile.ExtractError(f"archive path excluded: {name}")
                tar.addfile(root_filtered)

                def rel_parts(abs_path: str) -> tuple[str, ...]:
                    rel = os.path.relpath(abs_path, root)
                    if rel == ".":
                        return ()
                    return tuple(
                        PurePosixPath(rel.replace(os.sep, "/")).parts
                    )

                def add_entry(abs_path: str) -> None:
                    nonlocal seen_files, seen_bytes
                    parts = rel_parts(abs_path)
                    rel_str = "/".join(parts)
                    info = tar.gettarinfo(
                        abs_path, arcname=f"{name}/{rel_str}"
                    )
                    filtered = exclude_filter(info)
                    if filtered is None:
                        return
                    if info.issym():
                        target = os.readlink(abs_path)
                        digest = hashlib.sha256(
                            target.encode("utf-8", "surrogateescape")
                        ).hexdigest()
                        if not (
                            delta
                            and previous is not None
                            and previous.get(rel_str) == digest
                        ):
                            tar.addfile(filtered)
                        inventory.append(
                            {"path": rel_str, "sha256": digest, "size": 0}
                        )
                        seen_files += 1
                        _report_archive_progress(
                            progress_label, seen_files, seen_bytes
                        )
                        return
                    if info.isdir():
                        tar.addfile(filtered)
                        return
                    with open(abs_path, "rb") as handle:
                        digest = sha256_stream(handle)
                    if not (
                        delta
                        and previous is not None
                        and previous.get(rel_str) == digest
                    ):
                        with open(abs_path, "rb") as handle:
                            tar.addfile(filtered, handle)
                    inventory.append(
                        {
                            "path": rel_str,
                            "sha256": digest,
                            "size": info.size,
                        }
                    )
                    seen_files += 1
                    seen_bytes += int(info.size or 0)
                    _report_archive_progress(
                        progress_label, seen_files, seen_bytes
                    )

                for dirpath, dirnames, filenames in os.walk(
                    root, topdown=True, followlinks=False
                ):
                    kept: list[str] = []
                    for d in dirnames:
                        full = os.path.join(dirpath, d)
                        if os.path.islink(full):
                            add_entry(full)
                            continue
                        rel = os.path.relpath(full, root)
                        if rel == ".":
                            continue
                        info = tar.gettarinfo(
                            full, arcname=f"{name}/{rel.replace(os.sep, '/')}"
                        )
                        if exclude_filter(info) is None:
                            continue
                        tar.addfile(info)
                        kept.append(d)
                    dirnames[:] = kept
                    for f in filenames:
                        add_entry(os.path.join(dirpath, f))

                current = {entry["path"] for entry in inventory}
                deleted = (
                    sorted(set(previous) - current)
                    if delta and previous is not None
                    else []
                )
            if progress_label is not None:
                print(
                    f"progress: {progress_label}: done, {seen_files} files, "
                    f"{human_size(seen_bytes)} scanned",
                    file=sys.stderr,
                )
            archive_file.flush()
            archive_bytes = os.fstat(archive_file.fileno()).st_size
            archive_file.seek(0)
            digest = sha256_stream(archive_file)
            current_stat = archive.stat(follow_symlinks=False)
            if (current_stat.st_dev, current_stat.st_ino) != archive_identity:
                raise OSError("archive path changed while it was being written")
    except BaseException:
        if archive_identity is not None:
            _discard_partial_file_if_same(archive, archive_identity)
        raise

    return digest, archive_bytes, archive_identity, inventory, deleted


ARCHIVE_SPACE_SLACK = 1024 * 1024


def insecure_dir_mode(path: Path) -> int | None:
    """Return mode bits when path exists and is group/other-accessible (Unix)."""
    if os.name != "posix":
        return None
    target = path.expanduser()
    if not target.exists():
        return None
    mode = target.stat().st_mode & 0o777
    return mode if mode & 0o077 else None


def format_insecure_dest_refusal(path: Path, mode: int) -> str:
    return (
        f"error: backup dest has insecure permissions {mode:04o} "
        f"(group/other access); use chmod 700 or pass "
        f"--no-fail-on-insecure-dest: {path}"
    )


def available_disk_bytes(path: Path) -> int:
    """Return free bytes on the filesystem containing path (or an ancestor)."""
    probe = path.expanduser()
    while True:
        try:
            return shutil.disk_usage(probe).free
        except FileNotFoundError:
            parent = probe.parent
            if parent == probe:
                raise
            probe = parent


def archive_space_insufficient(needed: int, available: int) -> bool:
    return available < needed + ARCHIVE_SPACE_SLACK


def format_space_refusal(project: str, needed: int, available: int) -> str:
    return (
        f"error: {project}: not enough free space on backup dest "
        f"(need ~{human_size(needed)} uncompressed estimate + "
        f"{human_size(ARCHIVE_SPACE_SLACK)}, {human_size(available)} available)"
    )


def estimate_archive_stats(
    project: Path,
    *,
    include_artifacts: bool = False,
    exclude_git_metadata: bool = False,
    exclude_patterns: tuple[str, ...] = (),
    exclude_secrets: bool = False,
) -> tuple[int, int]:
    """Return (file_count, byte_count) after applying archive exclusion rules."""
    root = project.expanduser()
    if root.exists():
        root = root.resolve()
    patterns = artifact_patterns(include_artifacts)
    files = 0
    bytes_total = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        parts = (
            ()
            if rel_dir == "."
            else tuple(PurePosixPath(rel_dir.replace(os.sep, "/")).parts)
        )
        kept: list[str] = []
        for dirname in dirnames:
            child = parts + (dirname,)
            full = Path(dirpath) / dirname
            if full.is_symlink():
                # Symlinked dirs are treated as entries, not descended into.
                if not _archive_entry_excluded(
                    child, patterns, exclude_git_metadata, exclude_patterns, exclude_secrets
                ):
                    files += 1
                continue
            if _archive_entry_excluded(
                child, patterns, exclude_git_metadata, exclude_patterns, exclude_secrets
            ):
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for filename in filenames:
            child = parts + (filename,)
            if _archive_entry_excluded(
                child, patterns, exclude_git_metadata, exclude_patterns, exclude_secrets
            ):
                continue
            files += 1
            try:
                bytes_total += (Path(dirpath) / filename).stat().st_size
            except OSError:
                pass
    return files, bytes_total


def _archive_entry_excluded(
    parts: tuple[str, ...],
    patterns: tuple[str, ...],
    exclude_git_metadata: bool,
    exclude_patterns: tuple[str, ...],
    exclude_secrets: bool,
) -> bool:
    from proj_janitor.secrets import _component_excluded

    return _component_excluded(
        parts, patterns, exclude_git_metadata, exclude_patterns, exclude_secrets
    )


def _report_archive_progress(
    label: str | None, files: int, bytes_total: int
) -> None:
    if label is None:
        return
    if files == 1 or files % 100 == 0:
        print(
            f"progress: {label}: {files} files, {human_size(bytes_total)} scanned",
            file=sys.stderr,
        )


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return sha256_stream(handle)


def sha256_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _make_filter(
    arc_root: str,
    patterns: tuple[str, ...],
    project_path: Path,
    exclude_git_metadata: bool = False,
    exclude_patterns: tuple[str, ...] = (),
    exclude_secrets: bool = False,
):
    coverage_artifact = is_coverage_artifact(project_path / COVERAGE_DIR)
    seen_archive_paths: dict[tuple[str, ...], tuple[str, ...]] = {}

    def exclude(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = PurePosixPath(info.name).parts
        relative = parts[1:] if parts and parts[0] == arc_root else parts
        if exclude_git_metadata and ".git" in relative:
            return None
        if any(
            fnmatch(part, pattern)
            for part in relative
            for pattern in exclude_patterns
        ):
            return None
        if exclude_secrets and path_looks_like_secret("/".join(relative)):
            return None
        for index, part in enumerate(relative if patterns else ()):
            if part == COVERAGE_DIR:
                if index == 0 and coverage_artifact:
                    return None
                continue
            if any(
                fnmatch(part, pattern)
                for pattern in patterns
                if pattern != COVERAGE_DIR
            ):
                return None
        if info.ischr() or info.isblk() or info.isfifo():
            return None
        if info.islnk():
            # Preserve file contents without requiring restore to create hard links.
            info.type = tarfile.REGTYPE
            info.linkname = ""
            info.size = project_path.joinpath(*parts[1:]).stat().st_size
        if not safe_archive_member(info.name, arc_root):
            raise tarfile.ExtractError(format_unsafe_archive_path(info.name))
        if not register_archive_path(parts, seen_archive_paths):
            raise tarfile.ExtractError(
                "archive paths collide on case-insensitive filesystems "
                f"(including Unicode normalization): {info.name}"
            )
        if info.issym() and not safe_archive_link(info.name, info.linkname):
            raise tarfile.ExtractError(
                f"archive symlink escapes project: {info.name} -> {info.linkname}"
            )
        return info

    return exclude


def safe_archive_member(name: str, project: str) -> bool:
    if "\x00" in name or "\\" in name or ":" in name:
        return False
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        return False
    posix_parts = posix.parts
    windows_parts = windows.parts
    return (
        bool(posix_parts)
        and bool(windows_parts)
        and posix_parts[0] == project
        and windows_parts[0] == project
        and ".." not in posix_parts
        and ".." not in windows_parts
        and all(_safe_windows_component(part) for part in windows_parts)
    )


def format_unsafe_archive_path(name: str) -> str:
    """Explain why an archive member path was rejected (cross-platform portability)."""
    if ":" in name:
        return (
            "unsafe archive path (contains ':'; not Windows-portable — "
            f"often a stray file named like 'file.rs:line:col'): {name}"
        )
    return f"unsafe archive path (not Windows-portable): {name}"


def safe_archive_link(member_name: str, link_name: str) -> bool:
    if "\x00" in link_name or ":" in link_name:
        return False
    member = PurePosixPath(member_name)
    link_posix = PurePosixPath(link_name)
    link_windows = PureWindowsPath(link_name)
    if link_posix.is_absolute() or link_windows.is_absolute() or link_windows.drive:
        return False
    if not all(
        part in {".", ".."} or _safe_windows_component(part)
        for part in link_windows.parts
    ):
        return False

    for parts in (link_posix.parts, link_windows.parts):
        resolved = list(member.parent.parts)
        if not resolved:
            return False
        for part in parts:
            if part in ("", "."):
                continue
            if part == "..":
                if len(resolved) <= 1:
                    return False
                resolved.pop()
            else:
                resolved.append(part)
        if not resolved or resolved[0] != member.parts[0]:
            return False
    return True


def register_archive_path(
    parts: tuple[str, ...],
    seen: dict[tuple[str, ...], tuple[str, ...]],
) -> bool:
    for length in range(1, len(parts) + 1):
        original = parts[:length]
        folded = tuple(_portable_path_key(part) for part in original)
        previous = seen.get(folded)
        if previous is not None and previous != original:
            return False
        seen[folded] = original
    return True


def _portable_path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value.lower())


def _safe_windows_component(part: str) -> bool:
    if (
        not part
        or part.endswith((" ", "."))
        or any(0xD800 <= ord(char) <= 0xDFFF for char in part)
        or any(
            ord(char) < 32
            or ord(char) == 127
            or char in _WINDOWS_INVALID_CHARS
            for char in part
        )
    ):
        return False
    stem = part.split(".", 1)[0].rstrip(" .").upper()
    return stem not in _WINDOWS_RESERVED_NAMES and not re.fullmatch(
        r"(?:COM|LPT)[1-9¹²³]", stem
    )


def _unique_file(
    parent: Path,
    name: str,
    used: set[Path] | None = None,
) -> Path:
    candidate = parent / name
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while candidate.exists() or (used is not None and candidate in used):
        candidate = parent / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def _project_directory_names(projects: Iterable[Project]) -> dict[Path, str]:
    project_list = list(projects)
    counts = Counter(
        _portable_path_key(project.name or "project") for project in project_list
    )
    names: dict[Path, str] = {}
    for project in project_list:
        base = project.name or "project"
        if counts[_portable_path_key(base)] > 1:
            digest = hashlib.sha256(
                str(project.path).encode("utf-8"),
            ).hexdigest()[:8]
            names[project.path] = f"{base}-{digest}"
        else:
            names[project.path] = base
    return names


def _unique_artifact_name(
    project_name: str,
    timestamp: str,
    artifact_dir: Path,
    used: set[Path],
    suffix: str,
) -> str:
    base = f"{project_name}-{timestamp}{suffix}"
    candidate = artifact_dir / base
    counter = 2
    while candidate in used or candidate.exists():
        candidate = artifact_dir / f"{project_name}-{timestamp}-{counter}{suffix}"
        counter += 1
    used.add(candidate)
    return candidate.name
