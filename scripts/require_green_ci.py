#!/usr/bin/env python3
"""Fail unless a commit has a successful CI run (CR-206).

Used by the Python and Rust publish workflows. Environment:
  COMMIT              full commit SHA
  GITHUB_REPOSITORY   owner/repo
  GITHUB_TOKEN        token with actions:read
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

WORKFLOW = "ci.yml"


def main() -> int:
    commit = os.environ["COMMIT"].strip()
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    url = (
        f"https://api.github.com/repos/{repository}/actions/workflows/"
        f"{WORKFLOW}/runs?head_sha={commit}&per_page=100"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "proj-janitor-release-gate",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            runs = json.load(response).get("workflow_runs", [])
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print(f"error: cannot query CI runs for {commit}: {exc}", file=sys.stderr)
        return 1

    green = [run for run in runs if run.get("conclusion") == "success"]
    if not green:
        print(f"error: no successful CI run for {commit}", file=sys.stderr)
        for run in runs:
            print(
                f"  {run.get('name')} [{run.get('head_branch')}] "
                f"{run.get('status')}/{run.get('conclusion')} "
                f"{run.get('html_url')}",
                file=sys.stderr,
            )
        return 1
    print(f"CI green for {commit}: {green[0]['html_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
