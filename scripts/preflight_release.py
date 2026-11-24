#!/usr/bin/env python3
"""Pre-tag release preflight (CR-214).

Checks the working tree, branch, sync with origin/main, version/release-record
consistency, and whether the release tags already exist. Run before creating
release tags: ``python3 scripts/preflight_release.py``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-existing-tags",
        action="store_true",
        help="do not fail when the release tags already exist",
    )
    parser.add_argument(
        "--skip-origin-check",
        action="store_true",
        help="skip comparison with origin/main (offline preflight)",
    )
    args = parser.parse_args()

    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_release_consistency.py")],
        cwd=ROOT,
        check=True,
    )

    status = run("git", "status", "--porcelain")
    if status:
        fail("working tree is dirty; commit or stash changes first")
    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        fail(f"release must be cut from main, current branch is {branch!r}")

    if not args.skip_origin_check:
        head = run("git", "rev-parse", "HEAD")
        try:
            origin = run("git", "rev-parse", "origin/main")
        except subprocess.CalledProcessError:
            fail("cannot resolve origin/main; fetch or pass --skip-origin-check")
        if head != origin:
            fail("HEAD differs from origin/main; push or pull first")

    py_version = run(
        sys.executable,
        "-c",
        "import re;print(re.search(r'^version = \"([^\"]+)\"', open('pyproject.toml').read(), re.M).group(1))",
    )
    rust_version = run(
        sys.executable,
        "-c",
        "import re;print(re.search(r'^version = \"([^\"]+)\"', open('Cargo.toml').read(), re.M).group(1))",
    )
    tags = (f"python-v{py_version}", f"v{rust_version}")
    existing = [tag for tag in tags if run("git", "tag", "-l", tag)]
    if existing and not args.allow_existing_tags:
        fail(f"release tags already exist: {', '.join(existing)}")
    if existing:
        print(f"note: tags already exist (allowed): {', '.join(existing)}")

    print(f"release preflight OK: Python {py_version}, Rust {rust_version}")
    print(f"next: git tag {' '.join(tags)} && git push origin main {' '.join(tags)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
