from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from proj_janitor.config import PROJECT_MARKERS
from proj_janitor.models import GitState, Project, human_size


def is_project(path: Path) -> bool:
    return any((path / marker).exists() for marker in PROJECT_MARKERS)


def discover_projects(
    root: Path,
    include_unmarked: bool = False,
) -> list[Project]:
    root = root.expanduser()
    if not root.exists():
        raise FileNotFoundError(f"root does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"root is not a directory: {root}")

    if is_project(root):
        candidates = [root]
    else:
        candidates = sorted(
            child
            for child in root.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and not child.name.startswith(".")
            and (include_unmarked or is_project(child))
            and any(child.iterdir())
        )

    return [scan_project(candidate) for candidate in candidates]


def scan_project(path: Path) -> Project:
    path = path.resolve()
    markers = tuple(
        marker for marker in PROJECT_MARKERS if (path / marker).exists()
    )
    size_bytes, last_modified = measure_tree(path)
    return Project(
        path=path,
        name=path.name,
        markers=markers,
        size_bytes=size_bytes,
        last_modified=last_modified,
        git=read_git_state(path),
    )


def measure_tree(path: Path) -> tuple[int, datetime | None]:
    total = 0
    latest: float | None = None
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [
            name
            for name in dirnames
            if not (Path(dirpath) / name).is_symlink()
        ]
        for filename in filenames:
            entry = Path(dirpath) / filename
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not entry.is_symlink() and entry.is_file():
                total += stat.st_size
            if latest is None or stat.st_mtime > latest:
                latest = stat.st_mtime
    modified = (
        datetime.fromtimestamp(latest, tz=timezone.utc).astimezone()
        if latest is not None
        else None
    )
    return total, modified


def read_git_state(path: Path) -> GitState | None:
    if not (path / ".git").exists():
        return None
    branch = _git_output(path, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git_output(path, "status", "--porcelain")
    dirty = None if status is None else bool(status.strip())
    return GitState(branch=branch, dirty=dirty)


def _git_output(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def format_projects(projects: Iterable[Project]) -> str:
    rows = [
        (
            project.name,
            human_size(project.size_bytes),
            project.branch_label,
            project.dirty_label,
            ", ".join(project.markers) or "-",
            project.last_modified.strftime("%Y-%m-%d %H:%M")
            if project.last_modified
            else "-",
        )
        for project in projects
    ]
    headers = ("PROJECT", "SIZE", "BRANCH", "STATE", "MARKERS", "MODIFIED")
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        if rows
        else len(headers[index])
        for index in range(len(headers))
    ]

    def line(values: tuple[str, ...]) -> str:
        return "  ".join(
            value.ljust(widths[index])
            for index, value in enumerate(values)
        ).rstrip()

    return "\n".join([line(headers), line(tuple("-" * w for w in widths))] + [
        line(row) for row in rows
    ])
