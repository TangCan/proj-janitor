#!/usr/bin/env bash
# Bounded soak test for V-02: repeated backup/verify/restore cycles (CR-258).
# Usage: scripts/soak.sh [cycles] [python|rust|both]
# Defaults: 50 cycles, both implementations (Rust binary target/release/projjan).
# Each cycle overwrites a file, backs up with --keep 5 --yes, verifies the latest
# manifest, restores to a fresh directory, checks content, then removes it.
# Fails on: non-zero CLI exit, wrong restored content, stray temp dirs, or a
# manifest count above the retention bound.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

cycles="${1:-50}"
impl="${2:-both}"
case "$cycles" in
  ''|*[!0-9]*)
    echo "cycles must be a positive integer: $cycles" >&2
    exit 2
    ;;
esac
if [ "$cycles" -lt 1 ]; then
  echo "cycles must be >= 1: $cycles" >&2
  exit 2
fi

python_cmd=(python3 -m proj_janitor)
case "$impl" in
  rust) rust_cmd=(target/release/projjan) ;;
  both) rust_cmd=(target/release/projjan) ;;
  python) rust_cmd=() ;;
  *)
    echo "impl must be python, rust, or both: $impl" >&2
    exit 2
    ;;
esac

if [ "${#rust_cmd[@]}" -gt 0 ] && [ ! -x "${rust_cmd[0]}" ]; then
  echo "missing Rust binary ${rust_cmd[0]}; run: cargo build --release --locked" >&2
  exit 1
fi

run_impl() {
  local label="$1"
  shift
  local command=("$@")
  local work="$ROOT/.soak-$label"
  local project="$work/soak"
  local dest="$work/backups"
  local started
  started="$(date +%s)"
  rm -rf "$work"
  mkdir -p "$project"
  chmod 700 "$work"
  printf '[project]\nname = "soak"\n' > "$project/pyproject.toml"
  printf 'cycle 0\n' > "$project/file.txt"

  local i
  for ((i = 1; i <= cycles; i++)); do
    printf 'cycle %s\n' "$i" > "$project/file.txt"
    "${command[@]}" backup "$project" --backup-dest "$dest" \
      --always-archive --keep 5 --yes >/dev/null
    local manifest
    manifest="$(ls -t "$dest/soak"/manifest-*.json | head -1)"
    "${command[@]}" verify "$manifest" >/dev/null
    local out="$work/restore-$i"
    "${command[@]}" restore "$manifest" --dest "$out" --yes >/dev/null
    if [ "$(cat "$out/soak/file.txt")" != "cycle $i" ]; then
      echo "FAIL: $label cycle $i restored wrong content" >&2
      exit 1
    fi
    rm -rf "$out"
  done

  local manifests
  manifests="$(find "$dest/soak" -maxdepth 1 -name 'manifest-*.json' | wc -l)"
  if [ "$manifests" -gt 5 ]; then
    echo "FAIL: $label kept $manifests manifests (expected <= 5)" >&2
    exit 1
  fi
  local leftovers
  leftovers="$(find "$work" -maxdepth 1 -name '.*-restore-*' | wc -l)"
  if [ "$leftovers" -ne 0 ]; then
    echo "FAIL: $label left $leftovers restore temp dir(s)" >&2
    exit 1
  fi
  local elapsed=$(( $(date +%s) - started ))
  echo "soak $label: $cycles cycles OK (${elapsed}s, $manifests manifest(s) retained)"
  rm -rf "$work"
}

if [ "$impl" = "both" ] || [ "$impl" = "python" ]; then
  run_impl python "${python_cmd[@]}"
fi
if [ "$impl" = "both" ] || [ "$impl" = "rust" ]; then
  run_impl rust "${rust_cmd[@]}"
fi
echo "soak passed"
