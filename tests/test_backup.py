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

            manifest_path, records = run_backup([scan_project(project)], dest)

            self.assertEqual(len(records), 1)
            self.assertTrue(manifest_path.is_file())
            self.assertEqual(manifest_path.parent, dest)
            self.assertTrue(manifest_path.name.startswith("manifest-"))
            archive = Path(records[0].archive)
            self.assertEqual(archive.parent, dest / "app")
            self.assertTrue(archive.is_file())
            self.assertTrue(archive.name.startswith("app-"))
            self.assertEqual(records[0].sha256, sha256_file(archive))

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["tool"], "proj-janitor")
            self.assertEqual(manifest["records"][0]["sha256"], records[0].sha256)
            self.assertEqual(
                manifest["records"][0]["archive"],
                str(archive),
            )

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
            dest = root / "backups"

            manifest_path, records = run_backup(
                [scan_project(project)],
                dest,
                include_artifacts=True,
            )

            with tarfile.open(records[0].archive, "r:gz") as tar:
                names = tar.getnames()

            self.assertTrue(any("node_modules" in name for name in names))
            self.assertTrue(manifest_path.is_file())
            self.assertEqual(Path(records[0].archive).parent, dest / "app")

    def test_dry_run_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            dest = root / "backups"

            manifest_path, records = run_backup(
                [scan_project(project)],
                dest,
                dry_run=True,
            )

            self.assertEqual(records, [])
            self.assertFalse(manifest_path.exists())
            self.assertFalse(dest.exists())

    def test_duplicate_names_are_disambiguated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "one" / "app"
            second = root / "two" / "app"
            for project in (first, second):
                project.mkdir(parents=True)
                (project / "pyproject.toml").write_text("", encoding="utf-8")

            dest = root / "backups"
            _, records = run_backup(
                [scan_project(first), scan_project(second)],
                dest,
            )

            archive_dirs = [Path(record.archive).parent for record in records]
            self.assertEqual(len(set(archive_dirs)), 2)
            self.assertTrue(all(path.parent == dest for path in archive_dirs))
            self.assertTrue(all(path.name.startswith("app-") for path in archive_dirs))
            self.assertEqual(len({path.name for path in archive_dirs}), 2)
            for record in records:
                self.assertTrue(Path(record.archive).is_file())

    def test_repeated_archives_share_project_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            dest = root / "backups"

            first_manifest, first_records = run_backup(
                [scan_project(project)],
                dest,
            )
            second_manifest, second_records = run_backup(
                [scan_project(project)],
                dest,
            )

            first_archive = Path(first_records[0].archive)
            second_archive = Path(second_records[0].archive)
            self.assertEqual(first_archive.parent, dest / "app")
            self.assertEqual(second_archive.parent, dest / "app")
            self.assertNotEqual(first_archive, second_archive)
            self.assertTrue(first_archive.is_file())
            self.assertTrue(second_archive.is_file())
            self.assertTrue(first_manifest.is_file())
            self.assertTrue(second_manifest.is_file())
            self.assertEqual(len(list(dest.glob("manifest-*.json"))), 2)


@unittest.skipUnless(GIT_AVAILABLE, "git is not available")
class GitStrategyBackupTests(unittest.TestCase):
    def test_pushed_repo_is_recorded_without_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            dest = root / "backups"
            head = git(project, "rev-parse", "HEAD")

            manifest_path, records = run_backup(
                [scan_project(project)],
                dest,
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
            self.assertEqual(list(dest.iterdir()), [manifest_path])

            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(manifest["schema_version"], 2)
            entry = manifest["records"][0]
            self.assertEqual(entry["strategy"], "git")
            self.assertIsNone(entry["archive"])
            self.assertIsNone(entry["sha256"])
            self.assertEqual(entry["commit"], head)

    def test_credentialed_remote_is_recorded_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            git(
                project,
                "remote",
                "set-url",
                "origin",
                "https://user:secret-token@github.com/acme/app.git",
            )
            dest = root / "backups"

            manifest_path, records = run_backup(
                [scan_project(project)],
                dest,
            )

            record = records[0]
            self.assertEqual(record.strategy, "git")
            self.assertEqual(
                record.remote_url,
                "https://github.com/acme/app.git",
            )
            self.assertNotIn("secret-token", record.remote_url or "")

            manifest = manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("secret-token", manifest)
            self.assertIn(
                '"remote_url": "https://github.com/acme/app.git"',
                manifest,
            )

    def test_dirty_repo_falls_back_to_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            (project / "main.py").write_text("print('dirty')\n", encoding="utf-8")
            dest = root / "backups"

            manifest_path, records = run_backup(
                [scan_project(project)],
                dest,
            )

            record = records[0]
            self.assertEqual(record.strategy, "archive")
            self.assertEqual(
                record.fallback_reason,
                "working tree is not clean",
            )
            self.assertTrue(Path(record.archive).is_file())
            self.assertEqual(Path(record.archive).parent, dest / "app")
            self.assertIsNotNone(record.sha256)

            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"),
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
            dest = root / "backups"

            manifest_path, records = run_backup(
                [scan_project(project)],
                dest,
                always_archive=True,
            )

            record = records[0]
            self.assertEqual(record.strategy, "archive")
            self.assertEqual(record.fallback_reason, "always_archive")
            self.assertTrue(Path(record.archive).is_file())
            self.assertEqual(Path(record.archive).parent, dest / "app")

            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertTrue(manifest["always_archive"])

    def test_include_artifacts_forces_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            dest = root / "backups"

            manifest_path, records = run_backup(
                [scan_project(project)],
                dest,
                include_artifacts=True,
            )

            record = records[0]
            self.assertEqual(record.strategy, "archive")
            self.assertEqual(record.fallback_reason, "include_artifacts")
            self.assertTrue(Path(record.archive).is_file())
            self.assertEqual(Path(record.archive).parent, dest / "app")

            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertTrue(manifest["include_artifacts"])


if __name__ == "__main__":
    unittest.main()
