from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

_MANIFEST_RE = re.compile(r"manifest-(\d{8}-\d{6})(?:-(\d+))?\.json\Z")
_LEGACY_RUN_DIR_RE = re.compile(r"backup-\d{8}-\d{6}\Z")


class RetentionError(ValueError):
    pass


@dataclass(frozen=True)
class PlannedFile:
    project: str
    kind: str
    path: Path
    device: int
    inode: int
    size_bytes: int


@dataclass(frozen=True)
class PlannedArtifact:
    file: PlannedFile
    manifests: tuple[Path, ...]


@dataclass(frozen=True)
class RetentionPlan:
    backup_root: Path
    keep: int
    kept_manifests: int
    manifests: tuple[PlannedFile, ...]
    artifacts: tuple[PlannedArtifact, ...]

    @property
    def files(self) -> tuple[PlannedFile, ...]:
        return self.manifests + tuple(item.file for item in self.artifacts)


@dataclass(frozen=True)
class RetentionResult:
    deleted: tuple[PlannedFile, ...]
    errors: tuple[str, ...]


def build_retention_plan(backup_root: Path, keep: int) -> RetentionPlan:
    if keep < 1:
        raise RetentionError("--keep must be at least 1")
    root = backup_root.expanduser().resolve()
    if not root.exists():
        raise RetentionError(f"backup root does not exist: {root}")
    if not root.is_dir():
        raise RetentionError(f"backup root is not a directory: {root}")

    retained_artifacts: set[Path] = set()
    manifest_rows: list[tuple[Path, str, Path | None, tuple[int, int], int]] = []
    kept_count = 0
    for project_dir in sorted(root.iterdir()):
        if _LEGACY_RUN_DIR_RE.fullmatch(project_dir.name):
            continue
        try:
            directory_stat = project_dir.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(directory_stat.st_mode):
            continue
        manifests: list[tuple[tuple[str, int], Path, dict, tuple[int, int], int]] = []
        for path in project_dir.iterdir():
            match = _MANIFEST_RE.fullmatch(path.name)
            if match is None:
                continue
            if match.group(2) is not None and (
                int(match.group(2)) < 2 or str(int(match.group(2))) != match.group(2)
            ):
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RetentionError(f"cannot read manifest {path}: {exc}") from exc
            record = _manifest_record(document, path)
            stamp = match.group(1), int(match.group(2) or "1")
            manifests.append(
                (stamp, path, record, (info.st_dev, info.st_ino), info.st_size)
            )
        if len({row[2]["project"] for row in manifests}) > 1:
            raise RetentionError(f"multiple projects share a backup directory: {project_dir}")
        manifests.sort(key=lambda row: row[0], reverse=True)
        kept = manifests[:keep]
        kept_count += len(kept)
        for _, path, record, _, _ in kept:
            artifact = _artifact_for_manifest(project_dir, path, record)
            if artifact is not None:
                retained_artifacts.add(artifact)
        for _, path, record, identity, size in manifests[keep:]:
            artifact = _artifact_for_manifest(project_dir, path, record)
            manifest_rows.append((path, record["project"], artifact, identity, size))

    planned_manifests = tuple(
        PlannedFile(project, "manifest", path, identity[0], identity[1], size)
        for path, project, _, identity, size in sorted(manifest_rows)
    )
    artifact_references: dict[Path, tuple[PlannedFile, set[Path]]] = {}
    for manifest_path, project, artifact_path, _, _ in manifest_rows:
        if artifact_path is None or artifact_path in retained_artifacts:
            continue
        try:
            info = artifact_path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise RetentionError(f"refusing to prune non-regular artifact: {artifact_path}")
        kind = "bundle" if artifact_path.suffix == ".bundle" else "archive"
        planned = PlannedFile(
            project,
            kind,
            artifact_path,
            info.st_dev,
            info.st_ino,
            info.st_size,
        )
        existing = artifact_references.get(artifact_path)
        if existing is None:
            artifact_references[artifact_path] = planned, {manifest_path}
        else:
            existing[1].add(manifest_path)
    planned_artifacts = tuple(
        PlannedArtifact(file, tuple(sorted(manifests)))
        for file, manifests in sorted(
            artifact_references.values(), key=lambda item: str(item[0].path)
        )
    )
    return RetentionPlan(
        root,
        keep,
        kept_count,
        planned_manifests,
        planned_artifacts,
    )


def apply_retention_plan(
    plan: RetentionPlan,
) -> RetentionResult:
    deleted: list[PlannedFile] = []
    errors: list[str] = []
    deleted_manifests: set[Path] = set()
    for item in plan.manifests:
        if _remove_if_same(item, errors):
            deleted.append(item)
            deleted_manifests.add(item.path)
    for artifact in plan.artifacts:
        if not set(artifact.manifests).issubset(deleted_manifests):
            continue
        if _remove_if_same(artifact.file, errors):
            deleted.append(artifact.file)
    return RetentionResult(tuple(deleted), tuple(errors))


def _manifest_record(document: object, path: Path) -> dict:
    if not isinstance(document, dict) or document.get("schema_version") != 2:
        raise RetentionError(f"unsupported manifest in backup root: {path}")
    records = document.get("records")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise RetentionError(f"invalid manifest records in backup root: {path}")
    record = records[0]
    project = record.get("project")
    if (
        not isinstance(project, str)
        or not project
        or project in {".", ".."}
        or "/" in project
        or "\\" in project
        or "\x00" in project
    ):
        raise RetentionError(f"invalid project in manifest: {path}")
    strategy = record.get("strategy")
    if not isinstance(strategy, str) or strategy not in {"git", "archive", "git_bundle"}:
        raise RetentionError(f"unsupported strategy in manifest: {path}")
    return record


def _artifact_for_manifest(
    project_dir: Path,
    manifest_path: Path,
    record: dict,
) -> Path | None:
    strategy = record["strategy"]
    if strategy == "git":
        return None
    raw = record.get("archive")
    if not isinstance(raw, str) or not raw:
        raise RetentionError(f"missing artifact path in manifest: {manifest_path}")
    match = _MANIFEST_RE.fullmatch(manifest_path.name)
    assert match is not None
    stamp = match.group(1) + (f"-{match.group(2)}" if match.group(2) else "")
    suffix = ".bundle" if strategy == "git_bundle" else ".tar.gz"
    expected = f"{record['project']}-{stamp}{suffix}"
    if Path(raw).name != expected:
        raise RetentionError(f"unexpected artifact path in manifest: {manifest_path}")
    artifact = project_dir / expected
    raw_path = Path(raw)
    if not raw_path.is_absolute() and len(raw_path.parts) != 1:
        raise RetentionError(f"artifact path is not a sibling of its manifest: {manifest_path}")
    if raw_path.is_absolute() and raw_path.exists():
        try:
            raw_parent = raw_path.parent.resolve(strict=True)
            resolved_project_dir = project_dir.resolve(strict=True)
        except OSError as exc:
            raise RetentionError(
                f"cannot resolve artifact parent in {manifest_path}: {exc}"
            ) from exc
        if raw_parent != resolved_project_dir:
            raise RetentionError(f"artifact is outside its project directory: {manifest_path}")
    if raw_path.parent == project_dir and raw_path != artifact:
        raise RetentionError(f"artifact path does not match its project directory: {manifest_path}")
    return artifact


def _remove_if_same(item: PlannedFile, errors: list[str]) -> bool:
    try:
        info = item.path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(info.st_mode)
        or (info.st_dev, info.st_ino) != (item.device, item.inode)
    ):
        errors.append(f"{item.path}: changed since prune plan")
        return False
    try:
        # shortcut: a same-user replacement can race this identity check; use dirfd unlink when concurrent writers need isolation.
        item.path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        errors.append(f"{item.path}: {exc}")
        return False
    return True
