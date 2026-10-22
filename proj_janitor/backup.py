from __future__ import annotations

import hashlib
import json
import tarfile
from dataclasses import asdict
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable

from proj_janitor import __version__
from proj_janitor.config import artifact_patterns
from proj_janitor.models import BackupRecord, Project


def run_backup(
    projects: Iterable[Project],
    dest: Path,
    include_artifacts: bool = False,
    dry_run: bool = False,
) -> tuple[Path, list[BackupRecord]]:
    dest = dest.expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = _unique_directory(dest, f"backup-{timestamp}")
    if dry_run:
        return run_dir, []

    run_dir.mkdir(parents=True)
    patterns = artifact_patterns(include_artifacts)
    records: list[BackupRecord] = []
    used_names: set[str] = set()
    for project in projects:
        archive_name = _unique_archive_name(project.name, used_names)
        used_names.add(archive_name)
        records.append(
            backup_project(
                project,
                run_dir,
                patterns=patterns,
                archive_name=archive_name,
            )
        )

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool": "proj-janitor",
        "version": __version__,
        "include_artifacts": include_artifacts,
        "records": [asdict(record) for record in records],
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir, records


def backup_project(
    project: Project,
    run_dir: Path,
    patterns: Iterable[str] = (),
    archive_name: str | None = None,
) -> BackupRecord:
    name = archive_name or f"{project.name}.tar.gz"
    archive = run_dir / name
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


def _unique_directory(parent: Path, name: str) -> Path:
    candidate = parent / name
    counter = 2
    while candidate.exists():
        candidate = parent / f"{name}-{counter}"
        counter += 1
    return candidate


def _unique_archive_name(project_name: str, used: set[str]) -> str:
    base = f"{project_name}.tar.gz"
    if base not in used:
        return base
    counter = 2
    while f"{project_name}-{counter}.tar.gz" in used:
        counter += 1
    return f"{project_name}-{counter}.tar.gz"
