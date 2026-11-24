#!/usr/bin/env python3
"""Render GitHub Release notes from a release record (CR-207).

Usage: github_release_notes.py --tag v0.7.92 [--output notes.md]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "docs" / "发布记录"


def kind_and_version(tag: str) -> tuple[str, str]:
    if tag.startswith("python-v"):
        return "Python", tag[len("python-v") :]
    if tag.startswith("v"):
        return "Rust", tag[1:]
    raise SystemExit(f"unsupported tag: {tag}")


def render(tag: str) -> str:
    kind, version = kind_and_version(tag)
    record = RECORDS / f"{version}-{kind}版.md"
    if not record.is_file():
        raise SystemExit(f"missing release record: {record}")
    text = record.read_text(encoding="utf-8")
    start = text.find("## 更新内容")
    if start < 0:
        raise SystemExit(f"release record has no 更新内容 section: {record}")
    body = text[start:]
    body = re.sub(r"\n## 正式发布\n.*?(?=\n## |\Z)", "", body, flags=re.DOTALL)
    body = body.strip()

    if kind == "Python":
        registry = f"https://pypi.org/project/proj-janitor/{version}/"
        verify = (
            f"- PyPI：<{registry}>\n"
            f"- 安装：`pipx install proj-janitor` 或 `pip install proj-janitor=={version}`"
        )
    else:
        registry = f"https://crates.io/crates/proj-janitor/{version}"
        verify = (
            f"- crates.io：<{registry}>\n"
            f"- 安装：`cargo install --locked proj-janitor`"
        )
    return f"{body}\n\n## 验证\n\n{verify}\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", default="-")
    args = parser.parse_args()
    notes = render(args.tag)
    if args.output == "-":
        sys.stdout.write(notes)
    else:
        Path(args.output).write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
