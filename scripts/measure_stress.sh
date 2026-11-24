#!/usr/bin/env bash
# Measure V-02 capacity baselines on the local disk (CR-204).
# Usage: scripts/measure_stress.sh [scale]
# Default scale is 20 (about 20k small files for the Python crowded-tree case).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

scale="${1:-${PROJ_JANITOR_STRESS_SCALE:-20}}"
case "$scale" in
  ''|*[!0-9]*)
    echo "scale must be a positive integer: $scale" >&2
    exit 2
    ;;
esac
if [ "$scale" -lt 1 ]; then
  echo "scale must be >= 1: $scale" >&2
  exit 2
fi

python_bin="${PYTHON:-python3}"
time_cmd=()
if [ -x /usr/bin/time ] && /usr/bin/time -v true >/dev/null 2>&1; then
  time_cmd=(/usr/bin/time -v)
fi

run() {
  local label="$1"
  shift
  echo ""
  echo "==> $label (PROJ_JANITOR_STRESS_SCALE=$scale)"
  if [ "${#time_cmd[@]}" -gt 0 ]; then
    "${time_cmd[@]}" env PROJ_JANITOR_STRESS_SCALE="$scale" "$@"
  else
    env PROJ_JANITOR_STRESS_SCALE="$scale" "$@"
  fi
}

run "Python capacity suite" "$python_bin" -m unittest tests.test_stress_capacity
run "Rust crowded-tree tests" cargo test --locked --test rust_cli crowded
run "Rust scan-many test" cargo test --locked --test rust_cli scan_many
