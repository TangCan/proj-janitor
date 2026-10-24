from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from proj_janitor import backup as backup_module
from proj_janitor.backup import run_backup, sha256_file
from proj_janitor.scanner import scan_project
from tests.support import GIT_AVAILABLE, git, make_pushed_repo


def make_project(root: Path, name: str = "app") -> Path:
    project = root / name
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
    def test_backup_api_names_destination_backup_dest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backup_dest = root / "backups"

            manifest_paths, plans = backup_module.prepare_backup(
                [scan_project(project)], backup_dest=backup_dest
            )

            self.assertEqual(manifest_paths[0].parent, backup_dest / "app")
            self.assertEqual(plans[0].project.name, "app")
            self.assertFalse(backup_dest.exists())

    def test_backup_api_rejects_legacy_dest_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = scan_project(make_project(Path(tmp)))

            with self.assertRaisesRegex(TypeError, "unexpected keyword argument"):
                backup_module.prepare_backup(
                    [project], dest=Path(tmp) / "backups"
                )
            with self.assertRaisesRegex(TypeError, "unexpected keyword argument"):
                run_backup([project], dest=Path(tmp) / "backups")

    def test_backup_writes_archive_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            dest = root / "backups"

            manifest_paths, records, _ = run_backup(
                [scan_project(project)],
                dest,
            )

            self.assertEqual(len(records), 1)
            self.assertEqual(len(manifest_paths), 1)
            manifest_path = manifest_paths[0]
            self.assertTrue(manifest_path.is_file())
            self.assertEqual(manifest_path.parent, dest / "app")
            self.assertTrue(manifest_path.name.startswith("manifest-"))
            archive = Path(records[0].archive)
            self.assertEqual(archive.parent, dest / "app")
            self.assertTrue(archive.is_file())
            self.assertTrue(archive.name.startswith("app-"))
            self.assertEqual(records[0].sha256, sha256_file(archive))

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["tool"], "proj-janitor")
            self.assertEqual(len(manifest["records"]), 1)
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

            manifest_paths, records, _ = run_backup(
                [scan_project(project)],
                dest,
                include_artifacts=True,
            )

            with tarfile.open(records[0].archive, "r:gz") as tar:
                names = tar.getnames()

            self.assertTrue(any("node_modules" in name for name in names))
            self.assertTrue(manifest_paths[0].is_file())
            self.assertEqual(Path(records[0].archive).parent, dest / "app")

    def test_coverage_root_artifact_is_excluded_but_nested_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            root_coverage = project / "coverage"
            root_coverage.mkdir()
            (root_coverage / "lcov.info").write_text("", encoding="utf-8")
            nested_coverage = project / "src" / "coverage"
            nested_coverage.mkdir()
            (nested_coverage / "lcov.info").write_text("", encoding="utf-8")
            dest = root / "backups"

            _, records, _ = run_backup([scan_project(project)], dest)

            with tarfile.open(records[0].archive, "r:gz") as tar:
                names = tar.getnames()

            self.assertFalse(any(name.startswith("app/coverage/") for name in names))
            self.assertIn("app/src/coverage/lcov.info", names)

    def test_coverage_without_marker_is_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            coverage = project / "coverage"
            coverage.mkdir()
            (coverage / "notes.txt").write_text("", encoding="utf-8")
            dest = root / "backups"

            _, records, _ = run_backup([scan_project(project)], dest)

            with tarfile.open(records[0].archive, "r:gz") as tar:
                names = tar.getnames()

            self.assertIn("app/coverage/notes.txt", names)

    def test_dry_run_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            dest = root / "backups"

            manifest_paths, records, _ = run_backup(
                [scan_project(project)],
                dest,
                dry_run=True,
            )

            self.assertEqual(records, [])
            self.assertTrue(manifest_paths)
            self.assertFalse(any(path.exists() for path in manifest_paths))
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
            manifest_paths, records, _ = run_backup(
                [scan_project(first), scan_project(second)],
                dest,
            )

            archive_dirs = [Path(record.archive).parent for record in records]
            self.assertEqual(len(set(archive_dirs)), 2)
            self.assertTrue(all(path.parent == dest for path in archive_dirs))
            self.assertTrue(all(path.name.startswith("app-") for path in archive_dirs))
            self.assertEqual(len({path.name for path in archive_dirs}), 2)
            self.assertEqual(
                [path.parent for path in manifest_paths],
                archive_dirs,
            )
            for record in records:
                self.assertTrue(Path(record.archive).is_file())
            for path in manifest_paths:
                self.assertTrue(path.is_file())

    def test_repeated_archives_share_project_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            dest = root / "backups"

            first_manifests, first_records, _ = run_backup(
                [scan_project(project)],
                dest,
            )
            second_manifests, second_records, _ = run_backup(
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
            self.assertEqual(first_manifests[0].parent, dest / "app")
            self.assertEqual(second_manifests[0].parent, dest / "app")
            self.assertNotEqual(first_manifests[0], second_manifests[0])
            self.assertEqual(len(list((dest / "app").glob("manifest-*.json"))), 2)

    def test_batch_run_writes_manifest_per_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            dest = root / "backups"

            manifest_paths, records, _ = run_backup(
                [scan_project(first), scan_project(second)],
                dest,
            )

            self.assertEqual(len(records), 2)
            self.assertEqual(len(manifest_paths), 2)
            self.assertEqual(
                {path.parent.name for path in manifest_paths},
                {"first", "second"},
            )
            for path in manifest_paths:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(len(manifest["records"]), 1)

    def test_batch_run_isolates_project_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = make_project(root, "good")
            bad = make_project(root, "bad")
            dest = root / "backups"
            real_backup_project = backup_module.backup_project

            def fail_after_partial_archive(
                project,
                archive_dir,
                patterns=(),
                archive_name=None,
                fallback_reason=None,
            ):
                if project.name == "bad":
                    (archive_dir / archive_name).write_bytes(b"partial")
                    raise OSError("disk full")
                return real_backup_project(
                    project,
                    archive_dir,
                    patterns=patterns,
                    archive_name=archive_name,
                    fallback_reason=fallback_reason,
                )

            with mock.patch(
                "proj_janitor.backup.backup_project",
                side_effect=fail_after_partial_archive,
            ):
                manifest_paths, records, failures = run_backup(
                    [scan_project(good), scan_project(bad)],
                    dest,
                )

            self.assertEqual([record.project for record in records], ["good"])
            self.assertEqual(
                [path.parent.name for path in manifest_paths],
                ["good"],
            )
            self.assertEqual(failures, [("bad", "disk full")])
            self.assertFalse(list((dest / "bad").glob("manifest-*.json")))
            self.assertFalse(list((dest / "bad").glob("*.tar.gz")))
            self.assertTrue((dest / "good").is_dir())


@unittest.skipUnless(GIT_AVAILABLE, "git is not available")
class GitStrategyBackupTests(unittest.TestCase):
    def test_pushed_repo_is_recorded_without_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            dest = root / "backups"
            head = git(project, "rev-parse", "HEAD")

            manifest_paths, records, _ = run_backup(
                [scan_project(project)],
                dest,
            )

            self.assertEqual(len(records), 1)
            self.assertEqual(len(manifest_paths), 1)
            manifest_path = manifest_paths[0]
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
            self.assertEqual(manifest_path.parent, dest / "app")
            self.assertEqual(list(dest.iterdir()), [dest / "app"])
            self.assertEqual(list((dest / "app").iterdir()), [manifest_path])

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

            manifest_paths, records, _ = run_backup(
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

            manifest = manifest_paths[0].read_text(encoding="utf-8")
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

            manifest_paths, records, _ = run_backup(
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
                manifest_paths[0].read_text(encoding="utf-8"),
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

            manifest_paths, records, _ = run_backup(
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
                manifest_paths[0].read_text(encoding="utf-8"),
            )
            self.assertTrue(manifest["always_archive"])

    def test_include_artifacts_forces_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            dest = root / "backups"

            manifest_paths, records, _ = run_backup(
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
                manifest_paths[0].read_text(encoding="utf-8"),
            )
            self.assertTrue(manifest["include_artifacts"])


if __name__ == "__main__":
    unittest.main()
