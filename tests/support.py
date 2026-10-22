from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

GIT_AVAILABLE = shutil.which("git") is not None


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def init_repo(path: Path) -> None:
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test User")


def commit_all(path: Path, message: str = "initial") -> None:
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", message)


def make_pushed_repo(root: Path, name: str = "app") -> Path:
    project = root / name
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("", encoding="utf-8")
    (project / "main.py").write_text("print('hi')\n", encoding="utf-8")
    init_repo(project)
    commit_all(project)
    remote = root / f"{name}-remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(project, "remote", "add", "origin", str(remote))
    git(project, "push", "-q", "-u", "origin", "main")
    git(
        project,
        "remote",
        "set-url",
        "origin",
        f"https://github.com/acme/{name}.git",
    )
    return project
