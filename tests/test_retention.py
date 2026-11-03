from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from proj_janitor.cli import main
from proj_janitor.retention import RetentionError, apply_retention_plan, build_retention_plan


def write_manifest(
    directory: Path,
    stamp: str,
    strategy: str,
    artifact: str | None = None,
) -> Path:
    record = {
        "project": "demo",
        "strategy": strategy,
        "archive": str(directory / artifact) if artifact else None,
        "archive_bytes": 0,
        "source_bytes": 0,
        "sha256": "0" * 64 if artifact else None,
    }
    path = directory / f"manifest-{stamp}.json"
    path.write_text(
        json.dumps({"schema_version": 2, "records": [record]}),
        encoding="utf-8",
    )
    return path


class RetentionTests(unittest.TestCase):
    def test_plan_keeps_newest_and_only_deletes_manifest_referenced_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "demo"
            directory.mkdir()
            old_archive = "demo-20261101-100000.tar.gz"
            old_manifest = write_manifest(directory, "20261101-100000", "archive", old_archive)
            (directory / old_archive).write_bytes(b"old archive")
            git_manifest = write_manifest(directory, "20261101-110000", "git")
            latest_bundle = "demo-20261101-110000-2.bundle"
            latest_manifest = write_manifest(
                directory, "20261101-110000-2", "git_bundle", latest_bundle
            )
            (directory / latest_bundle).write_bytes(b"latest bundle")
            orphan = directory / "orphan.bundle"
            orphan.write_bytes(b"unreferenced")

            plan = build_retention_plan(root, keep=1)

            self.assertEqual(plan.kept_manifests, 1)
            self.assertEqual(
                {item.path for item in plan.files},
                {old_manifest, git_manifest, directory / old_archive},
            )
            self.assertTrue(old_manifest.exists())
            self.assertTrue((directory / old_archive).exists())
            self.assertTrue(latest_manifest.exists())

            result = apply_retention_plan(plan)

            self.assertEqual(result.errors, ())
            self.assertEqual(
                {item.path for item in result.deleted},
                {old_manifest, git_manifest, directory / old_archive},
            )
            self.assertFalse(old_manifest.exists())
            self.assertFalse(git_manifest.exists())
            self.assertFalse((directory / old_archive).exists())
            self.assertTrue(latest_manifest.exists())
            self.assertTrue((directory / latest_bundle).exists())
            self.assertTrue(orphan.exists())

    def test_prune_cli_previews_by_default_and_requires_explicit_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "demo"
            directory.mkdir()
            old_archive = "demo-20261101-100000.tar.gz"
            old_manifest = write_manifest(directory, "20261101-100000", "archive", old_archive)
            (directory / old_archive).write_bytes(b"old archive")
            latest = write_manifest(directory, "20261102-090000", "git")

            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["prune", str(root), "--keep", "0"])
            self.assertEqual(error.exception.code, 2)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["prune", str(root), "--keep", "1", "--json"]), 0)
            preview = json.loads(stdout.getvalue())
            self.assertTrue(preview["dry_run"])
            self.assertEqual(len(preview["files"]), 2)
            self.assertTrue(old_manifest.exists())
            self.assertTrue((directory / old_archive).exists())

            with mock.patch("sys.stdin", io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    main(["prune", str(root), "--keep", "1", "--apply"]), 2
                )
            self.assertTrue(old_manifest.exists())

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    main(["prune", str(root), "--keep", "1", "--apply", "--yes", "--json"]),
                    0,
                )
            result = json.loads(stdout.getvalue())
            self.assertFalse(result["dry_run"])
            self.assertEqual(len(result["deleted"]), 2)
            self.assertFalse(old_manifest.exists())
            self.assertFalse((directory / old_archive).exists())
            self.assertTrue(latest.exists())

    def test_invalid_manifest_refuses_to_plan_any_deletions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "demo"
            directory.mkdir()
            write_manifest(directory, "20261101-100000", "git")
            (directory / "manifest-20261102-090000.json").write_text("{bad", encoding="utf-8")

            with self.assertRaisesRegex(RetentionError, "cannot read manifest"):
                build_retention_plan(root, keep=1)

    def test_unsafe_artifact_path_refuses_to_plan_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "demo"
            directory.mkdir()
            manifest = write_manifest(directory, "20261101-100000", "archive", "demo-20261101-100000.tar.gz")
            record = json.loads(manifest.read_text(encoding="utf-8"))
            record["records"][0]["archive"] = str(root / "outside.tar.gz")
            manifest.write_text(json.dumps(record), encoding="utf-8")

            with self.assertRaisesRegex(RetentionError, "unexpected artifact path"):
                build_retention_plan(root, keep=1)

    def test_unknown_manifest_strategy_refuses_to_plan_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "demo"
            directory.mkdir()
            manifest = write_manifest(directory, "20261101-100000", "future_strategy")
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["records"][0]["strategy"] = []
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(RetentionError, "unsupported strategy"):
                build_retention_plan(root, keep=1)

