#!/usr/bin/env bash
# Measure V-02 single-tree capacity on the local disk (CR-251).
# Usage: scripts/measure_single_tree.sh [buckets] [files_per_bucket] [payload_bytes]
# Defaults: 500 x 1000 x 64 = 500k files, enough to cover the >400k boundary.
# Requires /usr/bin/time (Linux) for elapsed/RSS; falls back to plain timing.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

buckets="${1:-500}"
files_per_bucket="${2:-1000}"
payload_bytes="${3:-64}"
for value in "$buckets" "$files_per_bucket" "$payload_bytes"; do
  case "$value" in
    ''|*[!0-9]*)
      echo "expected positive integers, got: $buckets $files_per_bucket $payload_bytes" >&2
      exit 2
      ;;
  esac
done
total_files=$((buckets * files_per_bucket))

WORK="$ROOT/.stress-single-tree"
DEST_PY="$WORK/backups-py"
DEST_RS="$WORK/backups-rs"
rm -rf "$WORK"
mkdir -p "$WORK"

extract() {
  # Print elapsed and peak RSS lines from a GNU time -v output file.
  echo "elapsed_line: $(grep -E 'Elapsed \(wall clock\)' "$1" | sed 's/^[[:space:]]*//')"
  echo "maxrss_line: $(grep -E 'Maximum resident set size' "$1" | sed 's/^[[:space:]]*//')"
}

echo "==> creating $total_files files ($buckets buckets x $files_per_bucket, ${payload_bytes}B payload)"
python3 - "$WORK" "$buckets" "$files_per_bucket" "$payload_bytes" <<'PY'
import sys
from pathlib import Path

work = Path(sys.argv[1])
buckets = int(sys.argv[2])
files_per_bucket = int(sys.argv[3])
payload = b"x" * int(sys.argv[4])
root = work / "tree"
root.mkdir(parents=True)
(root / "pyproject.toml").write_text("[project]\nname='big'\n", encoding="utf-8")
for bucket in range(buckets):
    directory = root / "data" / f"b{bucket:04d}"
    directory.mkdir(parents=True)
    for index in range(files_per_bucket):
        (directory / f"f{index:04d}.bin").write_bytes(payload)
print("files:", sum(1 for path in root.rglob("*") if path.is_file()))
PY

echo "==> tree size: $(du -sh "$WORK/tree" | cut -f1)"

echo "==> Python backup"
/usr/bin/time -v -o "$WORK/py.time" python3 -m proj_janitor backup "$WORK/tree" \
  --backup-dest "$DEST_PY" --always-archive --progress \
  >"$WORK/py.out" 2>"$WORK/py.err"
extract "$WORK/py.time"

echo "==> Python verify"
PY_MANIFEST="$(ls "$DEST_PY"/tree/manifest-*.json | sort | tail -1)"
/usr/bin/time -v -o "$WORK/pyverify.time" python3 -m proj_janitor verify "$PY_MANIFEST" \
  >"$WORK/pyverify.out" 2>"$WORK/pyverify.err"
extract "$WORK/pyverify.time"
head -2 "$WORK/pyverify.out"

echo "==> Rust release build"
cargo build --release --locked --quiet

echo "==> Rust backup"
/usr/bin/time -v -o "$WORK/rs.time" target/release/projjan backup "$WORK/tree" \
  --backup-dest "$DEST_RS" --always-archive --progress \
  >"$WORK/rs.out" 2>"$WORK/rs.err"
extract "$WORK/rs.time"

echo "==> Rust verify"
RS_MANIFEST="$(ls "$DEST_RS"/tree/manifest-*.json | sort | tail -1)"
/usr/bin/time -v -o "$WORK/rsverify.time" target/release/projjan verify "$RS_MANIFEST" \
  >"$WORK/rsverify.out" 2>"$WORK/rsverify.err"
extract "$WORK/rsverify.time"
head -2 "$WORK/rsverify.out"

echo "==> artifacts"
du -sh "$DEST_PY" "$DEST_RS"
ls -l "$DEST_PY"/tree/*.tar.gz "$DEST_RS"/tree/*.tar.gz | awk '{print $5, $9}'

echo "==> progress tails"
tail -1 "$WORK/py.err" || true
tail -1 "$WORK/rs.err" || true

echo "==> cleanup"
rm -rf "$WORK"
echo "done"
