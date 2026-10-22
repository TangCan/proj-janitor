from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class GitState:
    branch: str | None
    dirty: bool | None


@dataclass(frozen=True)
class Project:
    path: Path
    name: str
    markers: tuple[str, ...]
    size_bytes: int
    last_modified: datetime | None
    git: GitState | None

    @property
    def dirty_label(self) -> str:
        if self.git is None:
            return "-"
        if self.git.dirty is None:
            return "unknown"
        return "dirty" if self.git.dirty else "clean"

    @property
    def branch_label(self) -> str:
        if self.git is None or self.git.branch is None:
            return "-"
        return self.git.branch


@dataclass(frozen=True)
class CleanupAction:
    project: Path
    path: Path
    rule: str
    kind: str
    size_bytes: int


@dataclass(frozen=True)
class BackupRecord:
    project: str
    source: str
    archive: str
    archive_bytes: int
    source_bytes: int
    sha256: str


def human_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"
