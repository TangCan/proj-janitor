from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from proj_janitor.backup import run_backup, sha256_file
from proj_janitor.scanner import scan_project
from tests.support import GIT_AVAILABLE, git, make_pushed_repo


def make_project(root: Path) -> Path:
    project = root / "app"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (project / "node_modules" / "pkg").mkdir(parents=True)
    (project / "node_modules" / "pkg" / "index.js").write_text(
        "x" * 32,
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text("", encoding="utf-8")
    return project


class BackupTests(unittest.TestCase):
    def test_backup_writes_archive_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            dest = root / "backups"

            run_dir, records = run_backup([scan_project(project)], dest)

            self.assertEqual(len(records), 1)
            self.assertTrue(run_dir.is_dir())
            archive = Path(records[0].archive)
            manifest_path = run_dir / "manifest.json"
            self.assertTrue(archive.is_file())
            self.assertTrue(manifest_path.is_file())
            self.assertEqual(records[0].sha256, sha256_file(archive))

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["tool"], "proj-janitor")
            self.assertEqual(manifest["records"][0]["sha256"], records[0].sha256)

            with tarfile.open(archive, "r:gz") as tar:
                names = tar.getnames()

            self.assertIn("app/src/main.py", names)
            self.assertFalse(
                any("node_modules" in name for name in names),
                names,
            )

    def test_backup_can_include_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)

            run_dir, records = run_backup(
                [scan_project(project)],
                root / "backups",
                include_artifacts=True,
            )

            with tarfile.open(records[0].archive, "r:gz") as tar:
                names = tar.getnames()

            self.assertTrue(any("node_modules" in name for name in names))
            self.assertTrue((run_dir / "manifest.json").is_file())

    def test_dry_run_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            dest = root / "backups"

            run_dir, records = run_backup(
                [scan_project(project)],
                dest,
                dry_run=True,
            )

            self.assertEqual(records, [])
            self.assertFalse(run_dir.exists())
            self.assertFalse(dest.exists())

    def test_duplicate_names_are_disambiguated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "one" / "app"
            second = root / "two" / "app"
            for project in (first, second):
                project.mkdir(parents=True)
                (project / "pyproject.toml").write_text("", encoding="utf-8")

            _, records = run_backup(
                [scan_project(first), scan_project(second)],
                root / "backups",
            )

            self.assertEqual(
                [Path(record.archive).name for record in records],
                ["app.tar.gz", "app-2.tar.gz"],
            )


@unittest.skipUnless(GIT_AVAILABLE, "git is not available")
class GitStrategyBackupTests(unittest.TestCase):
    def test_pushed_repo_is_recorded_without_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            head = git(project, "rev-parse", "HEAD")

            run_dir, records = run_backup(
                [scan_project(project)],
                root / "backups",
            )

            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record.strategy, "git")
            self.assertIsNone(record.archive)
            self.assertEqual(record.archive_bytes, 0)
            self.assertIsNone(record.sha256)
            self.assertEqual(record.remote, "origin")
            self.assertEqual(
                record.remote_url,
                "https://github.com/acme/app.git",
            )
            self.assertEqual(record.branch, "main")
            self.assertEqual(record.commit, head)
            self.assertEqual(record.upstream, "origin/main")
            self.assertEqual(record.verified, "local-ref")
            self.assertIsNone(record.fallback_reason)
            self.assertEqual(list(run_dir.glob("*.tar.gz")), [])

            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8"),
            )
            self.assertEqual(manifest["schema_version"], 2)
            entry = manifest["records"][0]
            self.assertEqual(entry["strategy"], "git")
            self.assertIsNone(entry["archive"])
            self.assertIsNone(entry["sha256"])
            self.assertEqual(entry["commit"], head)

    def test_dirty_repo_falls_back_to_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            (project / "main.py").write_text("print('dirty')\n", encoding="utf-8")

            run_dir, records = run_backup(
                [scan_project(project)],
                root / "backups",
            )

            record = records[0]
            self.assertEqual(record.strategy, "archive")
            self.assertEqual(
                record.fallback_reason,
                "working tree is not clean",
            )
            self.assertTrue(Path(record.archive).is_file())
            self.assertIsNotNone(record.sha256)

            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8"),
            )
            entry = manifest["records"][0]
            self.assertEqual(entry["strategy"], "archive")
            self.assertEqual(
                entry["fallback_reason"],
                "working tree is not clean",
            )

    def test_always_archive_overrides_git_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)

            run_dir, records = run_backup(
                [scan_project(project)],
                root / "backups",
                always_archive=True,
            )

            record = records[0]
            self.assertEqual(record.strategy, "archive")
            self.assertEqual(record.fallback_reason, "always_archive")
            self.assertTrue(Path(record.archive).is_file())

            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8"),
            )
            self.assertTrue(manifest["always_archive"])

    def test_include_artifacts_forces_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)

            run_dir, records = run_backup(
                [scan_project(project)],
                root / "backups",
                include_artifacts=True,
            )

            record = records[0]
            self.assertEqual(record.strategy, "archive")
            self.assertEqual(record.fallback_reason, "include_artifacts")
            self.assertTrue(Path(record.archive).is_file())

            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8"),
            )
            self.assertTrue(manifest["include_artifacts"])


if __name__ == "__main__":
    unittest.main()
