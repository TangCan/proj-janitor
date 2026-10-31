from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Mapping

from proj_janitor.config import (
    ARTIFACT_RULES,
    COVERAGE_DIR,
    is_coverage_artifact,
)
from proj_janitor.models import CleanupAction, human_size
from proj_janitor.scanner import measure_tree

_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_DIRFD_CLEANUP = (
    os.name == "posix"
    and bool(getattr(os, "O_DIRECTORY", 0))
    and bool(getattr(os, "O_NOFOLLOW", 0))
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.unlink in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
    and os.listdir in os.supports_fd
)


@dataclass(frozen=True)
class CleanupResult:
    removed: int
    freed_bytes: int
    errors: tuple[str, ...]


def resolve_rules(
    rule_names: Iterable[str] | None = None,
) -> Mapping[str, tuple[str, ...]]:
    if rule_names is None:
        return ARTIFACT_RULES
    names = list(rule_names)
    unknown = sorted(set(names) - set(ARTIFACT_RULES))
    if unknown:
        raise ValueError(
            f"unknown rule(s): {', '.join(unknown)}; "
            f"available: {', '.join(ARTIFACT_RULES)}"
        )
    return {name: ARTIFACT_RULES[name] for name in names}


def build_cleanup_plan(
    project: Path,
    rule_names: Iterable[str] | None = None,
) -> list[CleanupAction]:
    project = project.resolve()
    project_stat = project.stat()
    rules = resolve_rules(rule_names)
    pairs = [
        (rule, pattern)
        for rule, patterns in rules.items()
        for pattern in patterns
    ]

    actions: list[CleanupAction] = []
    def raise_walk_error(error: OSError) -> None:
        raise error

    for dirpath, dirnames, filenames in os.walk(
        project,
        followlinks=False,
        onerror=raise_walk_error,
    ):
        current = Path(dirpath)
        kept: list[str] = []
        for dirname in dirnames:
            entry = current / dirname
            if dirname == ".git" or entry.is_symlink():
                continue
            coverage_dir = (
                dirname == COVERAGE_DIR
                and current == project
                and is_coverage_artifact(entry)
            )
            match = _match(dirname, pairs, coverage_dir=coverage_dir)
            if match is None:
                kept.append(dirname)
                continue
            actions.append(
                _action(project, entry, match, "dir", project_stat.st_dev, project_stat.st_ino)
            )
        dirnames[:] = kept

        for filename in filenames:
            entry = current / filename
            if entry.is_symlink():
                continue
            match = _match(filename, pairs)
            if match is not None:
                actions.append(
                    _action(project, entry, match, "file", project_stat.st_dev, project_stat.st_ino)
                )

    return sorted(actions, key=lambda action: (str(action.project), str(action.path)))


def apply_cleanup(actions: Iterable[CleanupAction]) -> CleanupResult:
    removed = 0
    freed = 0
    errors: list[str] = []
    for action in actions:
        try:
            project = action.project
            if _DIRFD_CLEANUP:
                try:
                    project_fd = os.open(project, _DIR_FLAGS)
                except FileNotFoundError:
                    continue
                try:
                    root_stat = os.fstat(project_fd)
                    if (root_stat.st_dev, root_stat.st_ino) != (
                        action.project_device,
                        action.project_inode,
                    ):
                        raise ValueError("project root changed since cleanup plan")
                    relative = action.path.relative_to(project)
                    removed_now = _remove_cleanup_target_at(project_fd, relative)
                finally:
                    os.close(project_fd)
                if not removed_now:
                    continue
            else:
                # shortcut: platforms without dir_fd still use path checks; add native handles to resist concurrent writers.
                root_stat = project.stat()
                if (root_stat.st_dev, root_stat.st_ino) != (
                    action.project_device,
                    action.project_inode,
                ):
                    raise ValueError("project root changed since cleanup plan")
                target = action.path.resolve()
                if target == project or not target.is_relative_to(project):
                    raise ValueError("target escapes project root")
                if action.path.is_symlink():
                    raise ValueError("refusing to follow symlink")
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                else:
                    continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{action.path}: {exc}")
            continue
        except ValueError as exc:
            errors.append(f"{action.path}: {exc}")
            continue
        removed += 1
        freed += action.size_bytes
    return CleanupResult(
        removed=removed,
        freed_bytes=freed,
        errors=tuple(errors),
    )


def _remove_cleanup_target_at(project_fd: int, relative: Path) -> bool:
    parts = relative.parts
    if relative.is_absolute() or not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("unsafe cleanup target")
    parent_fd = os.dup(project_fd)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                return False
            os.close(parent_fd)
            parent_fd = next_fd
        return _remove_entry_at(parent_fd, parts[-1], refuse_symlink=True)
    finally:
        os.close(parent_fd)


def _remove_entry_at(parent_fd: int, name: str, *, refuse_symlink: bool) -> bool:
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(entry_stat.st_mode):
        if refuse_symlink:
            raise ValueError("refusing to follow symlink")
        os.unlink(name, dir_fd=parent_fd)
    elif stat.S_ISDIR(entry_stat.st_mode):
        try:
            directory_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            return False
        try:
            for child in os.listdir(directory_fd):
                _remove_entry_at(directory_fd, child, refuse_symlink=False)
        finally:
            os.close(directory_fd)
        current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current_stat.st_dev, current_stat.st_ino) != (
            entry_stat.st_dev,
            entry_stat.st_ino,
        ):
            raise ValueError("cleanup target changed during deletion")
        os.rmdir(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)
    return True


def format_cleanup_plan(actions: Iterable[CleanupAction]) -> str:
    actions = list(actions)
    if not actions:
        return "Nothing to clean."
    rows = [
        (
            action.project.name,
            action.rule,
            action.kind,
            human_size(action.size_bytes),
            str(action.path),
        )
        for action in actions
    ]
    headers = ("PROJECT", "RULE", "KIND", "SIZE", "PATH")
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def line(values: tuple[str, ...]) -> str:
        return "  ".join(
            value.ljust(widths[index])
            for index, value in enumerate(values)
        ).rstrip()

    body = "\n".join(line(row) for row in rows)
    total = sum(action.size_bytes for action in actions)
    return "\n".join(
        [
            line(headers),
            line(tuple("-" * width for width in widths)),
            body,
            "",
            f"{len(actions)} item(s), {human_size(total)} reclaimable.",
        ]
    )


def format_rules() -> str:
    lines = []
    for name, patterns in ARTIFACT_RULES.items():
        lines.append(f"{name}: {', '.join(patterns)}")
    return "\n".join(lines)


def _match(
    name: str,
    pairs: list[tuple[str, str]],
    *,
    coverage_dir: bool = False,
) -> str | None:
    for rule, pattern in pairs:
        if pattern == COVERAGE_DIR:
            if coverage_dir:
                return rule
            continue
        if fnmatch(name, pattern):
            return rule
    return None


def _action(
    project: Path,
    path: Path,
    rule: str,
    kind: str,
    project_device: int | None = None,
    project_inode: int | None = None,
) -> CleanupAction:
    if kind == "dir":
        size_bytes, _ = measure_tree(path)
    else:
        size_bytes = path.stat(follow_symlinks=False).st_size
    project_stat = project.stat() if project_device is None or project_inode is None else None
    return CleanupAction(
        project=project,
        path=path,
        rule=rule,
        kind=kind,
        size_bytes=size_bytes,
        project_device=project_device if project_stat is None else project_stat.st_dev,
        project_inode=project_inode if project_stat is None else project_stat.st_ino,
    )
