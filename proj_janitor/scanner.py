from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from proj_janitor.config import (
    COVERAGE_DIR,
    PROJECT_MARKERS,
    is_coverage_artifact,
)
from proj_janitor.models import GitRecovery, GitState, Project, human_size


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


def is_github_remote_url(remote: str) -> bool:
    if not remote or remote != remote.strip():
        return False
    if any(character.isspace() for character in remote):
        return False
    if remote.startswith("git@github.com:"):
        return _valid_github_path(remote[len("git@github.com:") :])
    if remote.startswith("ssh://"):
        try:
            parsed = urlsplit(remote)
        except ValueError:
            return False
        if parsed.scheme != "ssh" or parsed.hostname != "github.com":
            return False
        if parsed.username != "git" or parsed.password is not None:
            return False
        if parsed.port not in (None, 22):
            return False
        if parsed.query or parsed.fragment:
            return False
        return _valid_github_path(parsed.path.lstrip("/"))
    if remote.startswith("https://"):
        try:
            parsed = urlsplit(remote)
        except ValueError:
            return False
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            return False
        if parsed.port not in (None, 443):
            return False
        if parsed.query or parsed.fragment:
            return False
        # Accept embedded PATs for private remotes, but callers must persist
        # only the sanitized URL.
        return _valid_github_path(parsed.path.lstrip("/"))
    return False


def sanitize_remote_url(remote: str) -> str:
    """Return a URL safe to persist, without HTTPS userinfo."""
    if not remote.startswith("https://"):
        return remote
    try:
        parsed = urlsplit(remote)
    except ValueError:
        return remote
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        return remote
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit(("https", host, parsed.path, "", ""))


def _valid_github_path(path: str) -> bool:
    if not path or path.endswith("/"):
        return False
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2:
        return False
    owner, repository = parts
    if not re.fullmatch(r"[A-Za-z0-9._-]+", owner):
        return False
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repository):
        return False
    return owner not in (".", "..") and repository not in (".", "..")


def inspect_git_recoverability(
    project: Project,
    patterns: Iterable[str] = (),
) -> GitRecovery:
    path = project.path
    if not (path / ".git").exists():
        return GitRecovery(False, "not a git repository")

    branch = _git_output(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        return GitRecovery(False, "detached HEAD")

    status = _git_output(
        path,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    if status is None:
        return GitRecovery(False, "git status failed")
    if status:
        return GitRecovery(False, "working tree is not clean")

    stashes = _git_output(path, "stash", "list")
    if stashes is None:
        return GitRecovery(False, "cannot inspect stashes")
    if stashes:
        return GitRecovery(False, "stash entries are not included")

    remote = _git_output(path, "config", "--get", f"branch.{branch}.remote")
    if not remote or remote == ".":
        return GitRecovery(False, "branch has no upstream remote")

    upstream = _git_output(
        path,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
    )
    if not upstream:
        return GitRecovery(False, "branch has no upstream")

    remote_url = _git_output(path, "remote", "get-url", remote)
    if not remote_url:
        return GitRecovery(False, "remote URL is unavailable")
    if not is_github_remote_url(remote_url):
        return GitRecovery(False, "remote is not a supported GitHub URL")
    safe_remote_url = sanitize_remote_url(remote_url)

    head = _git_output(path, "rev-parse", "HEAD")
    if not head:
        return GitRecovery(False, "HEAD is unavailable")
    if not _git_ok(path, "merge-base", "--is-ancestor", "HEAD", "@{upstream}"):
        return GitRecovery(False, "HEAD is not contained in upstream")

    ahead = _git_count(path, "rev-list", "--count", "@{upstream}..HEAD")
    if ahead is None:
        return GitRecovery(False, "cannot compare HEAD with upstream")
    if ahead != 0:
        return GitRecovery(False, "HEAD is ahead of upstream")

    if (path / ".gitmodules").exists():
        return GitRecovery(False, "submodules are not supported")
    submodules = _git_output(path, "submodule", "status")
    if submodules is None:
        return GitRecovery(False, "cannot inspect submodules")
    if submodules:
        return GitRecovery(False, "submodules are not supported")

    if (path / ".lfsconfig").exists():
        return GitRecovery(False, "Git LFS is not supported")
    lfs = _git_run(
        path,
        "grep",
        "-l",
        "filter=lfs",
        "--",
        "*.gitattributes",
    )
    if lfs is None or lfs.returncode not in (0, 1):
        return GitRecovery(False, "cannot inspect Git LFS attributes")
    if lfs.stdout.strip():
        return GitRecovery(False, "Git LFS is not supported")

    ignored = _git_run(
        path,
        "-c",
        "core.quotePath=false",
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        "-z",
    )
    if ignored is None or ignored.returncode != 0:
        return GitRecovery(False, "cannot inspect ignored files")
    allowed_patterns = tuple(patterns)
    for entry in ignored.stdout.split("\0"):
        ignored_path = entry.rstrip("/")
        if not ignored_path:
            continue
        if not _ignored_path_is_artifact(
            path,
            ignored_path,
            allowed_patterns,
        ):
            return GitRecovery(
                False,
                f"ignored path is not an artifact: {ignored_path}",
            )

    branches = _git_output(
        path,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads",
    )
    if branches is None:
        return GitRecovery(False, "cannot list local branches")
    remote_refs = _git_output(
        path,
        "for-each-ref",
        "--format=%(refname)",
        f"refs/remotes/{remote}/",
    )
    if remote_refs is None:
        return GitRecovery(False, "cannot list remote-tracking refs")
    remote_ref_names = [
        reference for reference in remote_refs.splitlines() if reference
    ]
    if not remote_ref_names:
        return GitRecovery(False, "no remote-tracking refs")
    for local_branch in branches.splitlines():
        if not local_branch:
            continue
        count = _git_count(
            path,
            "rev-list",
            "--count",
            local_branch,
            "--not",
            *remote_ref_names,
        )
        if count is None:
            return GitRecovery(
                False,
                f"cannot inspect local branch: {local_branch}",
            )
        if count != 0:
            return GitRecovery(
                False,
                f"local branch is not fully pushed: {local_branch}",
            )

    tag_error = _verify_local_tags(path, remote)
    if tag_error is not None:
        return GitRecovery(False, tag_error)

    return GitRecovery(
        True,
        None,
        remote=remote,
        remote_url=safe_remote_url,
        branch=branch,
        commit=head,
        upstream=upstream,
        verified="local-ref",
    )


def _verify_local_tags(path: Path, remote: str) -> str | None:
    local_tags = _git_output(
        path,
        "for-each-ref",
        "--format=%(refname:short) %(objectname)",
        "refs/tags",
    )
    if local_tags is None:
        return "cannot list tags"
    if not local_tags:
        return None

    remote_tags = _remote_tag_refs(path, remote)
    if remote_tags is None:
        return "cannot verify local tags on remote"

    for line in local_tags.splitlines():
        name, separator, sha = line.partition(" ")
        if not separator or not name or not sha:
            return "cannot list tags"
        remote_sha = remote_tags.get(name)
        if remote_sha is None:
            return f"local tag is not pushed: {name}"
        if remote_sha != sha:
            return f"local tag differs from remote: {name}"
    return None


def _remote_tag_refs(path: Path, remote: str) -> dict[str, str] | None:
    output = _git_output(path, "ls-remote", "--tags", "--refs", remote)
    if output is None:
        return None

    refs: dict[str, str] = {}
    prefix = "refs/tags/"
    for line in output.splitlines():
        sha, separator, reference = line.partition("\t")
        if not separator:
            return None
        if reference.startswith(prefix):
            refs[reference[len(prefix) :]] = sha
    return refs


def _git_run(path: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _ignored_path_is_artifact(
    project_path: Path,
    ignored_path: str,
    patterns: tuple[str, ...],
) -> bool:
    for index, part in enumerate(Path(ignored_path).parts):
        if part == COVERAGE_DIR:
            return (
                index == 0
                and is_coverage_artifact(project_path / COVERAGE_DIR)
            )
        if any(
            fnmatch(part, pattern)
            for pattern in patterns
            if pattern != COVERAGE_DIR
        ):
            return True
    return False


def _git_output(path: Path, *args: str) -> str | None:
    result = _git_run(path, *args)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_ok(path: Path, *args: str) -> bool:
    result = _git_run(path, *args)
    return result is not None and result.returncode == 0


def _git_count(path: Path, *args: str) -> int | None:
    output = _git_output(path, *args)
    if output is None:
        return None
    try:
        return int(output)
    except ValueError:
        return None


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
