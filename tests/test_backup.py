from __future__ import annotations

import json
import os
import stat
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
    @unittest.skipUnless(GIT_AVAILABLE and os.name != "nt", "needs Git and POSIX names")
    def test_git_only_backup_rejects_a_windows_reserved_project_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root, "CON")
            backup_dest = root / "backups"

            with self.assertRaisesRegex(ValueError, "unsafe project name"):
                run_backup([scan_project(project)], backup_dest)

            self.assertFalse(backup_dest.exists())

    def test_backup_api_names_destination_backup_dest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backup_dest = root / "backups"

            manifest_paths, plans = backup_module.prepare_backup(
                [scan_project(project)], backup_dest=backup_dest
            )

            self.assertEqual(manifest_paths[0].parent, backup_dest.resolve() / "app")
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

    def test_backup_rejects_destination_inside_source_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = scan_project(make_project(Path(tmp)))
            backup_dest = project.path / "backups"

            with self.assertRaisesRegex(ValueError, "backup destination is inside project"):
                backup_module.prepare_backup([project], backup_dest)
            with self.assertRaisesRegex(ValueError, "backup destination is inside project"):
                run_backup([project], backup_dest)
            self.assertFalse(backup_dest.exists())

    def test_backup_rejects_absolute_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            outside = root / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            link = project / "src" / "outside-link"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            backup_dest = root / "backups"
            manifests, records, failures = run_backup(
                [scan_project(project)], backup_dest, always_archive=True
            )

            self.assertFalse(manifests)
            self.assertFalse(records)
            self.assertEqual(len(failures), 1)
            self.assertIn("archive symlink escapes project", failures[0][1])
            self.assertFalse(list(backup_dest.rglob("*.tar.gz")))

    def test_backup_excludes_artifact_symlink_before_validating_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            outside = root / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            link = project / "src" / "outside.pyc"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            _, records, failures = run_backup(
                [scan_project(project)], root / "backups", always_archive=True
            )

            self.assertFalse(failures)
            self.assertEqual(len(records), 1)
            with tarfile.open(records[0].archive, "r:gz") as archive:
                self.assertNotIn("app/src/outside.pyc", archive.getnames())

    @unittest.skipIf(os.name == "nt", "Windows cannot create backslash filenames")
    def test_backup_rejects_windows_traversal_member_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            (project / r"src\..\..\escape.txt").write_text("bad\n", encoding="utf-8")

            backup_dest = root / "backups"
            manifests, records, failures = run_backup(
                [scan_project(project)], backup_dest, always_archive=True
            )

            self.assertFalse(manifests)
            self.assertFalse(records)
            self.assertEqual(len(failures), 1)
            self.assertIn("unsafe path", failures[0][1])
            self.assertFalse(list(backup_dest.rglob("*.tar.gz")))

    def test_backup_does_not_overwrite_an_existing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = scan_project(make_project(root))
            archive_dir = root / "backups"
            archive_dir.mkdir()
            archive = archive_dir / "reserved.tar.gz"
            archive.write_bytes(b"keep existing backup")

            with self.assertRaises(FileExistsError):
                backup_module.backup_project(
                    project, archive_dir, archive_name=archive.name
                )

            self.assertEqual(archive.read_bytes(), b"keep existing backup")

    def test_backup_interrupt_cleans_partial_outputs(self) -> None:
        for failure in ("archive-hash", "manifest-write"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = scan_project(make_project(root))
                backup_dest = root / "backups"
                if failure == "archive-hash":
                    patch_target = mock.patch(
                        "proj_janitor.backup.sha256_file",
                        side_effect=KeyboardInterrupt,
                    )
                else:
                    patch_target = mock.patch.object(
                        Path, "write_text", side_effect=KeyboardInterrupt
                    )

                with patch_target:
                    with self.assertRaises(KeyboardInterrupt):
                        run_backup([project], backup_dest, always_archive=True)

                self.assertFalse(list(backup_dest.rglob("manifest-*.json")))
                self.assertFalse(list(backup_dest.rglob("*.tar.gz")))

    def test_backup_interrupt_while_reserving_manifest_cleans_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = scan_project(make_project(root))
            backup_dest = root / "backups"
            real_fdopen = os.fdopen

            class InterruptOnClose:
                def __init__(self, handle):
                    self.handle = handle

                def __enter__(self):
                    return self.handle.__enter__()

                def __exit__(self, *args):
                    self.handle.__exit__(*args)
                    raise KeyboardInterrupt

            def interrupt_manifest_close(fd, mode="r", *args, **kwargs):
                return InterruptOnClose(real_fdopen(fd, mode, *args, **kwargs))

            with mock.patch(
                "proj_janitor.backup.os.fdopen", side_effect=interrupt_manifest_close
            ):
                with self.assertRaises(KeyboardInterrupt):
                    run_backup([project], backup_dest, always_archive=True)

            self.assertFalse(list(backup_dest.rglob("manifest-*.json")))
            self.assertFalse(list(backup_dest.rglob("*.tar.gz")))

    def test_backup_fdopen_failure_cleans_private_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = scan_project(make_project(root))
            backup_dest = root / "backups"

            with mock.patch(
                "proj_janitor.backup.os.fdopen", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    run_backup([project], backup_dest, always_archive=True)

            self.assertFalse(list(backup_dest.rglob("manifest-*.json")))
            self.assertFalse(list(backup_dest.rglob("*.tar.gz")))

    def test_backup_cleanup_error_does_not_mask_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = scan_project(make_project(root))
            backup_dest = root / "backups"

            with (
                mock.patch(
                    "proj_janitor.backup.sha256_file",
                    side_effect=KeyboardInterrupt,
                ),
                mock.patch.object(
                    Path, "unlink", side_effect=PermissionError("unlink denied")
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    run_backup([project], backup_dest, always_archive=True)

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
            self.assertEqual(manifest_path.parent, dest.resolve() / "app")
            self.assertTrue(manifest_path.name.startswith("manifest-"))
            archive = Path(records[0].archive)
            self.assertEqual(archive.parent, dest.resolve() / "app")
            self.assertTrue(archive.is_file())
            self.assertTrue(archive.name.startswith("app-"))
            if os.name == "posix":
                for path in (dest.resolve(), manifest_path.parent):
                    mode = stat.S_IMODE(path.stat().st_mode)
                    self.assertEqual(mode & 0o077, 0)
                    self.assertEqual(mode & 0o700, 0o700)
                for path in (manifest_path, archive):
                    mode = stat.S_IMODE(path.stat().st_mode)
                    self.assertEqual(mode & 0o077, 0)
                    self.assertEqual(mode & 0o600, 0o600)
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
            self.assertEqual(Path(records[0].archive).parent, dest.resolve() / "app")

    def test_backup_can_exclude_git_metadata_file_and_records_the_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            (project / ".git").write_text("gitdir: ../.git/worktrees/app\n", encoding="utf-8")
            dest = root / "backups"

            manifests, records, failures = run_backup(
                [scan_project(project)],
                dest,
                always_archive=True,
                exclude_git_metadata=True,
            )

            self.assertFalse(failures)
            self.assertTrue(manifests[0].is_file())
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertTrue(manifest["exclude_git_metadata"])
            with tarfile.open(records[0].archive, "r:gz") as tar:
                names = tar.getnames()
            self.assertIn("app/src/main.py", names)
            self.assertFalse(any("/.git/" in name or name.endswith("/.git") for name in names))
            self.assertTrue((project / ".git").is_file())

    def test_git_exclusion_manifest_records_option_even_without_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            dest = root / "backups"

            manifests, records, failures = run_backup(
                [scan_project(project)],
                dest,
                always_archive=True,
                exclude_git_metadata=True,
            )

            self.assertFalse(failures)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertTrue(manifest["exclude_git_metadata"])
            with tarfile.open(records[0].archive, "r:gz") as tar:
                self.assertIn("app/src/main.py", tar.getnames())
            self.assertFalse((project / ".git").exists())

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
            self.assertTrue(all(path.parent == dest.resolve() for path in archive_dirs))
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
            self.assertEqual(first_archive.parent, dest.resolve() / "app")
            self.assertEqual(second_archive.parent, dest.resolve() / "app")
            self.assertNotEqual(first_archive, second_archive)
            self.assertTrue(first_archive.is_file())
            self.assertTrue(second_archive.is_file())
            self.assertEqual(first_manifests[0].parent, dest.resolve() / "app")
            self.assertEqual(second_manifests[0].parent, dest.resolve() / "app")
            self.assertNotEqual(first_manifests[0], second_manifests[0])
            self.assertEqual(len(list((dest.resolve() / "app").glob("manifest-*.json"))), 2)

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

    def test_case_variant_names_are_disambiguated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = [
                scan_project(make_project(root / "first", "App")),
                scan_project(make_project(root / "second", "app")),
            ]

            manifest_paths, records, failures = run_backup(
                projects, root / "backups", always_archive=True
            )

            self.assertFalse(failures)
            self.assertEqual(len(records), 2)
            names = {path.parent.name for path in manifest_paths}
            self.assertEqual(len(names), 2)
            self.assertEqual({name.split("-", 1)[0] for name in names}, {"App", "app"})
            self.assertTrue(
                all(
                    len(name.rsplit("-", 1)[1]) == 8
                    and all(char in "0123456789abcdef" for char in name.rsplit("-", 1)[1])
                    for name in names
                )
            )

    def test_batch_run_isolates_project_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = make_project(root, "good")
            bad = make_project(root, "bad")
            dest = root / "backups"
            real_sha256_file = backup_module.sha256_file

            def fail_hash_for_bad_project(path):
                if path.parent.name == "bad":
                    raise OSError("disk full")
                return real_sha256_file(path)

            with mock.patch(
                "proj_janitor.backup.sha256_file",
                side_effect=fail_hash_for_bad_project,
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
    def test_credentialed_submodule_url_never_falls_back_to_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            (project / ".gitmodules").write_text(
                '[submodule "private"]\n'
                "\tpath = vendor/private\n"
                "\turl = https://alice:submodule-secret@github.com/acme/private.git\n",
                encoding="utf-8",
            )
            git(project, "add", ".gitmodules")
            git(project, "commit", "-m", "configure submodule")
            git(project, "update-ref", "refs/remotes/origin/main", "HEAD")
            backup_dest = root / "backups"

            manifests, records, failures = run_backup(
                [scan_project(project)],
                backup_dest,
            )

            self.assertEqual(manifests, [])
            self.assertEqual(records, [])
            self.assertEqual(
                failures,
                [("app", "submodule URL contains credentials or query data")],
            )
            self.assertFalse((backup_dest / "app").exists())

            dry_manifests, dry_records, dry_failures = run_backup(
                [scan_project(project)],
                root / "dry-backups",
                dry_run=True,
            )
            self.assertEqual(len(dry_manifests), 1)
            self.assertEqual(dry_records, [])
            self.assertEqual(dry_failures, failures)
            self.assertFalse((root / "dry-backups").exists())

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
            self.assertEqual(manifest_path.parent, dest.resolve() / "app")
            self.assertEqual(list(dest.resolve().iterdir()), [dest.resolve() / "app"])
            self.assertEqual(list((dest.resolve() / "app").iterdir()), [manifest_path])

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

    def test_sensitive_unsupported_remote_never_falls_back_to_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            git(
                project,
                "remote",
                "set-url",
                "origin",
                "https://github.com/acme/app.git?token=remote-secret",
            )
            backup_dest = root / "backups"

            manifests, records, failures = run_backup(
                [scan_project(project)],
                backup_dest,
            )

            self.assertEqual(manifests, [])
            self.assertEqual(records, [])
            self.assertEqual(
                failures,
                [("app", "remote URL contains credentials or query data")],
            )
            self.assertFalse((backup_dest / "app").exists())

    def test_multi_url_credentialed_remote_never_falls_back_to_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            git(
                project,
                "remote",
                "set-url",
                "origin",
                "https://github.com/acme/app.git",
            )
            git(
                project,
                "config",
                "--add",
                "remote.origin.url",
                "https://user:remote-secret@github.com/acme/app.git",
            )
            (project / "local-change.txt").write_text("dirty\n", encoding="utf-8")
            backup_dest = root / "backups"

            manifests, records, failures = run_backup(
                [scan_project(project)],
                backup_dest,
            )

            self.assertEqual(manifests, [])
            self.assertEqual(records, [])
            self.assertEqual(
                failures,
                [("app", "remote URL contains credentials or query data")],
            )
            self.assertFalse((backup_dest / "app").exists())

    def test_credentialed_pushurl_never_falls_back_to_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            git(project, "remote", "set-url", "origin", "https://github.com/acme/app.git")
            git(
                project,
                "remote",
                "set-url",
                "--push",
                "origin",
                "https://token:push-secret@github.com/acme/app.git",
            )
            (project / "local-change.txt").write_text("dirty\n", encoding="utf-8")
            backup_dest = root / "backups"

            manifests, records, failures = run_backup(
                [scan_project(project)],
                backup_dest,
            )

            self.assertEqual(manifests, [])
            self.assertEqual(records, [])
            self.assertEqual(
                failures,
                [("app", "remote URL contains credentials or query data")],
            )
            self.assertFalse((backup_dest / "app").exists())

    def test_sensitive_git_auth_config_never_falls_back_to_archive(self) -> None:
        settings = (
            (
                "include.path",
                "credentials.inc",
                "Git config contains HTTP extraHeader",
                "Authorization: Basic extra-secret",
            ),
            (
                "credential.password",
                "config-secret",
                "Git config contains credential passwords",
                "config-secret",
            ),
            (
                "credential.helper",
                "!echo helper-secret",
                "Git config contains an inline credential helper",
                "helper-secret",
            ),
            (
                "credential.https://github.com.helper",
                "!echo url-helper-secret",
                "Git config contains an inline credential helper",
                "url-helper-secret",
            ),
            (
                "credential.helper",
                "manager --oauth-token=argument-secret",
                "Git config contains sensitive credential helper arguments",
                "argument-secret",
            ),
            (
                "credential.helper",
                "manager --config=ZXhhbXBsZQ==",
                "Git config contains sensitive credential helper arguments",
                "ZXhhbXBsZQ==",
            ),
            (
                "core.sshCommand",
                "sshpass -p ssh-password-secret ssh",
                "Git config contains a custom SSH command",
                "ssh-password-secret",
            ),
            (
                "core.sshCommand",
                "ssh -i .ssh/id_ed25519",
                "Git config contains a custom SSH command",
                "private-key",
            ),
            (
                "core.sshCommand",
                "sh -c 'echo helper-secret'",
                "Git config contains a custom SSH command",
                "helper-secret",
            ),
            (
                "credential.helper",
                "store --file=.git-credentials",
                "Git config points to a credential file inside the project",
                "store-secret",
            ),
            (
                "credential.helper",
                "store --file='.git-credentials'",
                "Git config contains sensitive credential helper arguments",
                "quoted-store-secret",
            ),
            (
                "credential.helper",
                "store --file $HOME/.git-credentials",
                "Git config contains sensitive credential helper arguments",
                "expanded-store-secret",
            ),
            (
                "http.proxy",
                "http://proxy-user:proxy-secret@proxy.example:8080",
                "Git config contains credentials or query data in a service URL",
                "proxy-secret",
            ),
            (
                "remote.origin.proxy",
                "http://remote-proxy-user:remote-proxy-secret@proxy.example:8080",
                "Git config contains credentials or query data in a service URL",
                "remote-proxy-secret",
            ),
            (
                "lfs.url",
                "https://lfs-user:lfs-secret@lfs.example/objects",
                "Git config contains credentials or query data in a service URL",
                "lfs-secret",
            ),
            (
                "url.https://rewrite-secret@github.com/.insteadOf",
                "https://public.example/",
                "Git config contains credentials in a URL rewrite",
                "rewrite-secret",
            ),
            (
                "url.https://github.com/.insteadOf",
                "https://token:rewrite-value-secret@public.example/",
                "Git config contains credentials in a URL rewrite",
                "rewrite-value-secret",
            ),
            (
                ".lfsconfig",
                "https://lfs-user:file-secret@lfs.example/objects",
                "LFS config contains credentials or query data",
                "file-secret",
            ),
            (
                ".lfsconfig.proxy",
                "http://proxy-user:lfs-proxy-secret@proxy.example:8080",
                "LFS config contains credentials or query data",
                "lfs-proxy-secret",
            ),
        )
        for key, value, reason, secret in settings:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = make_pushed_repo(root)
                git(project, "remote", "set-url", "origin", "https://github.com/acme/app.git")
                if key == "include.path":
                    (project / ".git" / value).write_text(
                        f"[http]\n\textraHeader = {secret}\n",
                        encoding="utf-8",
                    )
                elif key == ".lfsconfig":
                    (project / key).write_text(
                        f"[lfs]\n\turl = {value}\n",
                        encoding="utf-8",
                    )
                elif key == ".lfsconfig.proxy":
                    (project / ".lfsconfig").write_text(
                        f"[http]\n\tproxy = {value}\n",
                        encoding="utf-8",
                    )
                if value.startswith("store --file"):
                    (project / ".git-credentials").write_text(
                        f"https://user:{secret}@github.com\n",
                        encoding="utf-8",
                    )
                if not key.startswith(".lfsconfig"):
                    git(project, "config", key, value)
                (project / "local-change.txt").write_text("dirty\n", encoding="utf-8")
                backup_dest = root / "backups"

                manifests, records, failures = run_backup(
                    [scan_project(project)],
                    backup_dest,
                )

                self.assertEqual(manifests, [])
                self.assertEqual(records, [])
                self.assertEqual(failures, [("app", reason)])
                self.assertFalse((backup_dest / "app").exists())

    @unittest.skipUnless(GIT_AVAILABLE, "needs Git")
    def test_safe_credential_helper_arguments_allow_archive_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            git(project, "config", "credential.helper", "cache --timeout=3600")
            (project / "local-change.txt").write_text("dirty\n", encoding="utf-8")

            manifests, records, failures = run_backup(
                [scan_project(project)], root / "backups"
            )

            self.assertEqual(failures, [])
            self.assertEqual(len(manifests), 1)
            self.assertEqual(records[0].strategy, "archive")

    def test_dirty_project_with_credentialed_submodule_never_falls_back_to_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            (project / ".gitmodules").write_text(
                '[submodule "private"]\n'
                "\tpath = vendor/private\n"
                "\turl = https://github.com/acme/private.git\n"
                "\turl = https://token:module-secret@github.com/acme/private.git\n",
                encoding="utf-8",
            )
            backup_dest = root / "backups"

            manifests, records, failures = run_backup(
                [scan_project(project)],
                backup_dest,
            )

            self.assertEqual(manifests, [])
            self.assertEqual(records, [])
            self.assertEqual(
                failures,
                [("app", "submodule URL contains credentials or query data")],
            )
            self.assertFalse((backup_dest / "app").exists())

    def test_non_git_project_with_credentialed_submodule_never_falls_back_to_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "app"
            project.mkdir()
            (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (project / ".gitmodules").write_text(
                "malformed config line with token: https://token:module-secret@github.com/acme/private.git\n",
                encoding="utf-8",
            )
            backup_dest = root / "backups"

            manifests, records, failures = run_backup(
                [scan_project(project)],
                backup_dest,
            )

            self.assertEqual(manifests, [])
            self.assertEqual(records, [])
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0][0], "app")
            self.assertIn(
                failures[0][1],
                {
                    "submodule URL contains credentials or query data",
                    "cannot safely inspect .gitmodules for credentials",
                },
            )
            self.assertFalse((backup_dest / "app").exists())

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
            self.assertEqual(Path(record.archive).parent, dest.resolve() / "app")
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
            self.assertEqual(Path(record.archive).parent, dest.resolve() / "app")

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
            self.assertEqual(Path(record.archive).parent, dest.resolve() / "app")

            manifest = json.loads(
                manifest_paths[0].read_text(encoding="utf-8"),
            )
            self.assertTrue(manifest["include_artifacts"])


if __name__ == "__main__":
    unittest.main()
