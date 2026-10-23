from __future__ import annotations

import hashlib
import json
import tarfile
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable

from proj_janitor import __version__
from proj_janitor.config import artifact_patterns
from proj_janitor.models import BackupPlan, BackupRecord, Project
from proj_janitor.scanner import inspect_git_recoverability


def prepare_backup(
    projects: Iterable[Project],
    dest: Path,
    include_artifacts: bool = False,
    always_archive: bool = False,
) -> tuple[Path, list[BackupPlan]]:
    dest = dest.expanduser().resolve()
    project_list = list(projects)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    manifest_path = _unique_file(dest, f"manifest-{timestamp}.json")
    patterns = artifact_patterns(include_artifacts)
    plans: list[BackupPlan] = []
    project_dirs = _project_directory_names(project_list)
    used_archives: set[Path] = set()

    for project in project_list:
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
            else:
                strategy = "archive"
                reason = recovery.reason or "not recoverable from GitHub"

        archive_name = None
        archive_dir = None
        if strategy == "archive":
            archive_dir = project_dirs[project.path]
            archive_name = _unique_archive_name(
                project.name or "project",
                timestamp,
                dest / archive_dir,
                used_archives,
            )
        plans.append(
            BackupPlan(
                project=project,
                archive_name=archive_name,
                strategy=strategy,
                archive_dir=archive_dir,
                recovery=recovery,
                reason=reason,
            )
        )
    return manifest_path, plans


def run_backup(
    projects: Iterable[Project],
    dest: Path,
    include_artifacts: bool = False,
    dry_run: bool = False,
    always_archive: bool = False,
) -> tuple[Path, list[BackupRecord]]:
    manifest_path, plans = prepare_backup(
        projects,
        dest,
        include_artifacts=include_artifacts,
        always_archive=always_archive,
    )
    if dry_run:
        return manifest_path, []

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    patterns = artifact_patterns(include_artifacts)
    records: list[BackupRecord] = []
    for plan in plans:
        if plan.strategy == "git":
            recovery = plan.recovery
            if recovery is None:
                raise RuntimeError("git backup plan is missing recovery details")
            records.append(
                BackupRecord(
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
            )
            continue
        if plan.archive_name is None or plan.archive_dir is None:
            raise RuntimeError("archive backup plan is missing an archive path")
        archive_dir = manifest_path.parent / plan.archive_dir
        archive_dir.mkdir(parents=True, exist_ok=True)
        records.append(
            backup_project(
                plan.project,
                archive_dir,
                patterns=patterns,
                archive_name=plan.archive_name,
                fallback_reason=plan.reason,
            )
        )

    manifest = {
        "schema_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool": "proj-janitor",
        "version": __version__,
        "include_artifacts": include_artifacts,
        "always_archive": always_archive,
        "records": [asdict(record) for record in records],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path, records


def backup_project(
    project: Project,
    archive_dir: Path,
    patterns: Iterable[str] = (),
    archive_name: str | None = None,
    fallback_reason: str | None = None,
) -> BackupRecord:
    name = archive_name or f"{project.name}.tar.gz"
    archive = archive_dir / name
    patterns = tuple(patterns)

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(
            project.path,
            arcname=project.name,
            filter=_make_filter(project.name, patterns),
        )

    return BackupRecord(
        project=project.name,
        source=str(project.path),
        archive=str(archive),
        archive_bytes=archive.stat().st_size,
        source_bytes=project.size_bytes,
        sha256=sha256_file(archive),
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
):
    if not patterns:
        return None

    def exclude(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        relative = parts[1:] if parts and parts[0] == arc_root else parts
        for part in relative:
            if any(fnmatch(part, pattern) for pattern in patterns):
                return None
        return info

    return exclude


def _unique_file(parent: Path, name: str) -> Path:
    candidate = parent / name
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while candidate.exists():
        candidate = parent / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def _project_directory_names(projects: Iterable[Project]) -> dict[Path, str]:
    project_list = list(projects)
    counts = Counter(project.name or "project" for project in project_list)
    names: dict[Path, str] = {}
    for project in project_list:
        base = project.name or "project"
        if counts[base] > 1:
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
