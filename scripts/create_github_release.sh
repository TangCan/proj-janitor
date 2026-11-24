#!/usr/bin/env bash
# Create or update a GitHub Release from rendered notes (CR-207).
# Usage: create_github_release.sh <tag> <notes-file> <title>
set -euo pipefail

tag="${1:?usage: create_github_release.sh <tag> <notes-file> <title>}"
notes="${2:?usage: create_github_release.sh <tag> <notes-file> <title>}"
title="${3:?usage: create_github_release.sh <tag> <notes-file> <title>}"

case "$tag" in
  python-v*) latest=(--latest=false) ;;
  *) latest=(--latest) ;;
esac

if gh release view "$tag" >/dev/null 2>&1; then
  gh release edit "$tag" --title "$title" --notes-file "$notes" "${latest[@]}"
else
  gh release create "$tag" --title "$title" --notes-file "$notes" --verify-tag "${latest[@]}"
fi
