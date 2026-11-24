#!/usr/bin/env python3
"""Check release metadata consistency (CR-205).

Fails when the version numbers, release records, or README installation hints
drift apart. Run locally with ``python3 scripts/check_release_consistency.py``;
CI runs it on every push.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    cargo = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))
    py_version = pyproject["project"]["version"]
    rust_version = cargo["package"]["version"]

    init_text = (ROOT / "proj_janitor" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)
    if match is None:
        fail("proj_janitor/__init__.py has no __version__ literal")
    if match.group(1) != py_version:
        fail(f"__version__ {match.group(1)!r} != pyproject {py_version!r}")

    lock_text = (ROOT / "Cargo.lock").read_text(encoding="utf-8")
    lock_match = re.search(
        r'^name = "proj-janitor"\r?\nversion = "([^"]+)"',
        lock_text,
        re.MULTILINE,
    )
    if lock_match is None:
        fail("Cargo.lock has no proj-janitor package entry")
    if lock_match.group(1) != rust_version:
        fail(f"Cargo.lock version {lock_match.group(1)!r} != Cargo.toml {rust_version!r}")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        f"源码元数据版本为 `{py_version}`",
        f"pip install proj-janitor=={py_version}",
        f"Rust crates.io 已发布至 `{rust_version}`",
        f"最新已发布 PyPI 为 `{py_version}`",
    )
    for phrase in required:
        if phrase not in readme:
            fail(f"README is missing {phrase!r}")

    records = (
        ROOT / "docs" / "发布记录" / f"{py_version}-Python版.md",
        ROOT / "docs" / "发布记录" / f"{rust_version}-Rust版.md",
    )
    for record in records:
        if not record.is_file():
            fail(f"missing release record: {record.relative_to(ROOT)}")

    print(f"release metadata OK: Python {py_version}, Rust {rust_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
