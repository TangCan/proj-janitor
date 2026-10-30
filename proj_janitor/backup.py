from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import unicodedata
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable

from proj_janitor import __version__
from proj_janitor.config import (
    COVERAGE_DIR,
    artifact_patterns,
    is_coverage_artifact,
)
from proj_janitor.models import BackupPlan, BackupRecord, Project
from proj_janitor.scanner import inspect_git_recoverability

_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_WINDOWS_INVALID_CHARS = '<>:"|?*'


def prepare_backup(
    projects: Iterable[Project],
    backup_dest: Path,
    include_artifacts: bool = False,
    always_archive: bool = False,
) -> tuple[list[Path], list[BackupPlan]]:
    backup_dest = backup_dest.expanduser().resolve()
    project_list = list(projects)
    for project in project_list:
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
            elif recovery.reason and recovery.reason.startswith(
                (
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
            ):
                strategy = "blocked"
                reason = recovery.reason
            else:
                strategy = "archive"
                reason = recovery.reason or "not recoverable from GitHub"

        archive_name = None
        if strategy == "archive":
            archive_name = _unique_archive_name(
                project.name or "project",
                timestamp,
                backup_dest / project_dir,
                used_archives,
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
) -> tuple[list[Path], list[BackupRecord], list[tuple[str, str]]]:
    backup_dest = backup_dest.expanduser().resolve()
    manifest_paths, plans = prepare_backup(
        projects,
        backup_dest,
        include_artifacts=include_artifacts,
        always_archive=always_archive,
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
        manifest_reserved = False
        try:
            manifest_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with _open_new_file(manifest_path):
                manifest_reserved = True
        except OSError as exc:
            if manifest_reserved:
                _discard_partial_file(manifest_path)
            failures.append((plan.project.name, str(exc)))
            continue
        except BaseException:
            if manifest_reserved:
                _discard_partial_file(manifest_path)
            raise
        try:
            record = _backup_one(
                plan,
                manifest_path,
                patterns,
                created_at,
                include_artifacts,
                always_archive,
                exclude_git_metadata,
            )
        except (OSError, tarfile.TarError) as exc:
            _discard_partial_file(manifest_path)
            failures.append((plan.project.name, str(exc)))
            continue
        except BaseException:
            _discard_partial_file(manifest_path)
            raise
        records.append(record)
        completed_paths.append(manifest_path)
    return completed_paths, records, failures


def _backup_one(
    plan: BackupPlan,
    manifest_path: Path,
    patterns: tuple[str, ...],
    created_at: str,
    include_artifacts: bool,
    always_archive: bool,
    exclude_git_metadata: bool,
) -> BackupRecord:
    archive_path: Path | None = None
    if plan.strategy == "git":
        recovery = plan.recovery
        if recovery is None:
            raise RuntimeError("git backup plan is missing recovery details")
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
        )
    else:
        if plan.archive_name is None:
            raise RuntimeError("archive backup plan is missing an archive name")
        archive_dir = manifest_path.parent
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / plan.archive_name
        record = backup_project(
            plan.project,
            archive_dir,
            patterns=patterns,
            archive_name=plan.archive_name,
            fallback_reason=plan.reason,
            exclude_git_metadata=exclude_git_metadata,
        )

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
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except BaseException:
        if archive_path is not None:
            _discard_partial_file(archive_path)
        raise
    return record


def _discard_partial_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _open_new_file(path: Path, binary: bool = False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if binary:
        flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if binary:
            return os.fdopen(descriptor, "wb")
        return os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        _discard_partial_file(path)
        raise


def backup_project(
    project: Project,
    archive_dir: Path,
    patterns: Iterable[str] = (),
    archive_name: str | None = None,
    fallback_reason: str | None = None,
    exclude_git_metadata: bool = False,
) -> BackupRecord:
    name = archive_name or f"{project.name}.tar.gz"
    archive = archive_dir / name
    patterns = tuple(patterns)

    archive_file = _open_new_file(archive, binary=True)
    try:
        with archive_file:
            with tarfile.open(fileobj=archive_file, mode="w:gz") as tar:
                tar.add(
                    project.path,
                    arcname=project.name,
                    filter=_make_filter(
                        project.name, patterns, project.path, exclude_git_metadata
                    ),
                )
        archive_bytes = archive.stat().st_size
        digest = sha256_file(archive)
    except BaseException:
        _discard_partial_file(archive)
        raise

    return BackupRecord(
        project=project.name,
        source=str(project.path),
        archive=str(archive),
        archive_bytes=archive_bytes,
        source_bytes=project.size_bytes,
        sha256=digest,
        fallback_reason=fallback_reason,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_filter(
    arc_root: str,
    patterns: tuple[str, ...],
    project_path: Path,
    exclude_git_metadata: bool = False,
):
    coverage_artifact = is_coverage_artifact(project_path / COVERAGE_DIR)
    seen_archive_paths: dict[tuple[str, ...], tuple[str, ...]] = {}

    def exclude(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = PurePosixPath(info.name).parts
        relative = parts[1:] if parts and parts[0] == arc_root else parts
        if exclude_git_metadata and ".git" in relative:
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
            raise tarfile.ExtractError(f"archive contains an unsafe path: {info.name}")
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
        or any(ord(char) < 32 or char in _WINDOWS_INVALID_CHARS for char in part)
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


def _unique_archive_name(
    project_name: str,
    timestamp: str,
    archive_dir: Path,
    used: set[Path],
) -> str:
    base = f"{project_name}-{timestamp}.tar.gz"
    candidate = archive_dir / base
    counter = 2
    while candidate in used or candidate.exists():
        candidate = archive_dir / f"{project_name}-{timestamp}-{counter}.tar.gz"
        counter += 1
    used.add(candidate)
    return candidate.name
