from __future__ import annotations

import os
import shutil
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
            actions.append(_action(project, entry, match, "dir"))
        dirnames[:] = kept

        for filename in filenames:
            entry = current / filename
            if entry.is_symlink():
                continue
            match = _match(filename, pairs)
            if match is not None:
                actions.append(_action(project, entry, match, "file"))

    return sorted(actions, key=lambda action: (str(action.project), str(action.path)))


def apply_cleanup(actions: Iterable[CleanupAction]) -> CleanupResult:
    removed = 0
    freed = 0
    errors: list[str] = []
    for action in actions:
        try:
            project = action.project.resolve()
            target = action.path.resolve()
            if target == project or not target.is_relative_to(project):
                raise ValueError("target escapes project root")
            if action.path.is_symlink():
                raise ValueError("refusing to follow symlink")
            if action.path.is_dir():
                shutil.rmtree(action.path)
            elif action.path.exists():
                action.path.unlink()
            else:
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
) -> CleanupAction:
    if kind == "dir":
        size_bytes, _ = measure_tree(path)
    else:
        size_bytes = path.stat(follow_symlinks=False).st_size
    return CleanupAction(
        project=project,
        path=path,
        rule=rule,
        kind=kind,
        size_bytes=size_bytes,
    )
