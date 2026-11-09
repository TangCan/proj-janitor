#!/usr/bin/env bash
# Measure Python and Rust line-coverage baselines for proj-janitor (CR-130 / V-01).
# Not wired as a CI gate: run locally or during maintenance reviews.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python_bin="${PYTHON:-python3}"
if ! "$python_bin" -c "import coverage" >/dev/null 2>&1; then
  echo "Python coverage package missing. Install with:" >&2
  echo "  $python_bin -m pip install 'coverage>=7'" >&2
  exit 1
fi

echo "==> Python coverage (unittest discover -s tests)"
"$python_bin" -m coverage erase
"$python_bin" -m coverage run -m unittest discover -s tests -q
"$python_bin" -m coverage report --include='proj_janitor/*'

if ! command -v cargo-llvm-cov >/dev/null 2>&1 && ! cargo llvm-cov --version >/dev/null 2>&1; then
  echo "" >&2
  echo "cargo-llvm-cov not found; skipping Rust coverage." >&2
  echo "Install: cargo install cargo-llvm-cov && rustup component add llvm-tools-preview" >&2
  exit 0
fi

echo ""
echo "==> Rust coverage (cargo llvm-cov --locked --summary-only)"
cargo llvm-cov --locked --summary-only
