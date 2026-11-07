"""CR-107: systematic capacity / many-small-files regressions for R-08 / NFR-007.

Default fixture sizes stay CI-friendly. Multiply with PROJ_JANITOR_STRESS_SCALE
(integer >= 1) for heavier local runs; scale does not change assertions that
compare estimated counts to the tree we built.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from proj_janitor.backup import estimate_archive_stats
from proj_janitor.cli import main
from proj_janitor.scanner import discover_projects


def _stress_scale() -> int:
    raw = os.environ.get("PROJ_JANITOR_STRESS_SCALE", "1").strip()
    try:
        scale = int(raw)
    except ValueError:
        return 1
    return max(1, scale)


def make_crowded_project(
    root: Path,
    *,
    buckets: int,
    files_per_bucket: int,
    payload: bytes = b"x" * 64,
) -> tuple[Path, int, int]:
    """Build a project with many small files under data/; return path and expected pack stats."""
    project = root / "crowded"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='crowded'\n", encoding="utf-8")
    # Artifact tree must be excluded by default so estimate reflects only packable files.
    junk = project / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "index.js").write_bytes(b"artifact" * 1024)

    expected_files = 1  # pyproject.toml
    expected_bytes = (project / "pyproject.toml").stat().st_size
    data = project / "data"
    for bucket in range(buckets):
        bucket_dir = data / f"b{bucket:04d}"
        bucket_dir.mkdir(parents=True)
        for index in range(files_per_bucket):
            path = bucket_dir / f"f{index:04d}.bin"
            path.write_bytes(payload)
            expected_files += 1
            expected_bytes += len(payload)
    return project, expected_files, expected_bytes


def run_cli(*argv: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(list(argv))
    return code, stdout.getvalue(), stderr.getvalue()


class CapacityStressTests(unittest.TestCase):
    def test_estimate_matches_crowded_tree_and_excludes_artifacts(self) -> None:
        scale = _stress_scale()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, expected_files, expected_bytes = make_crowded_project(
                root,
                buckets=25 * scale,
                files_per_bucket=40,
            )
            files, nbytes = estimate_archive_stats(project)
            self.assertEqual(files, expected_files)
            self.assertEqual(nbytes, expected_bytes)
            # Artifact subtree must remain excluded from the estimate.
            self.assertTrue((project / "node_modules" / "pkg" / "index.js").is_file())

    def test_progress_dry_run_reports_crowded_pack_estimate(self) -> None:
        scale = _stress_scale()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, expected_files, _expected_bytes = make_crowded_project(
                root,
                buckets=20 * scale,
                files_per_bucket=25,
            )
            dest = root / "backups"
            code, _out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
                "--dry-run",
                "--progress",
                "--check-space",
            )
            self.assertEqual(code, 0, err)
            match = re.search(r"would pack (\d+) files,", err)
            self.assertIsNotNone(match, err)
            self.assertEqual(int(match.group(1)), expected_files)
            self.assertIn("after excludes", err)
            self.assertFalse(dest.exists())

    def test_archive_backup_and_verify_crowded_tree(self) -> None:
        scale = _stress_scale()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, expected_files, _expected_bytes = make_crowded_project(
                root,
                buckets=15 * scale,
                files_per_bucket=20,
            )
            dest = root / "backups"
            started = time.monotonic()
            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
                "--progress",
                "--check-space",
            )
            elapsed = time.monotonic() - started
            self.assertEqual(code, 0, err)
            self.assertIn("archive", out)
            self.assertIn("done,", err)
            self.assertRegex(err, rf"done, {expected_files} files,")
            # Soft budget: CI-sized trees should finish well under a minute.
            self.assertLess(elapsed, 90.0, f"backup took {elapsed:.1f}s; err={err}")

            manifests = sorted((dest / "crowded").glob("*.json"))
            self.assertEqual(len(manifests), 1, manifests)
            v_code, _vout, verr = run_cli("verify", str(manifests[0]))
            self.assertEqual(v_code, 0, verr)

            files, _nbytes = estimate_archive_stats(project)
            self.assertEqual(files, expected_files)

    def test_scan_many_sibling_projects(self) -> None:
        scale = _stress_scale()
        count = 40 * scale
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            for index in range(count):
                project = root / f"proj{index:04d}"
                project.mkdir()
                (project / "pyproject.toml").write_text(
                    f"[project]\nname='proj{index:04d}'\n",
                    encoding="utf-8",
                )
                (project / "main.py").write_text("print(1)\n", encoding="utf-8")
            started = time.monotonic()
            projects = discover_projects(root)
            elapsed = time.monotonic() - started
            self.assertEqual(len(projects), count)
            self.assertLess(elapsed, 30.0, f"scan took {elapsed:.1f}s")


if __name__ == "__main__":
    unittest.main()
