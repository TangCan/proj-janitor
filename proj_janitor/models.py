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
class GitRecovery:
    eligible: bool
    reason: str | None = None
    remote: str | None = None
    remote_url: str | None = None
    branch: str | None = None
    commit: str | None = None
    upstream: str | None = None
    verified: str | None = None


@dataclass(frozen=True)
class BackupRecord:
    project: str
    source: str
    archive: str | None
    archive_bytes: int
    source_bytes: int
    sha256: str | None
    strategy: str = "archive"
    remote: str | None = None
    remote_url: str | None = None
    branch: str | None = None
    commit: str | None = None
    upstream: str | None = None
    verified: str | None = None
    fallback_reason: str | None = None


@dataclass(frozen=True)
class BackupPlan:
    project: Project
    archive_name: str | None
    strategy: str
    recovery: GitRecovery | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RestorePlan:
    project: str
    strategy: str
    target: Path
    archive: Path | None = None
    remote_url: str | None = None
    commit: str | None = None
    sha256: str | None = None
    hash_verified: bool = False


@dataclass(frozen=True)
class RestoreResult:
    project: str
    strategy: str
    target: Path


def human_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"
