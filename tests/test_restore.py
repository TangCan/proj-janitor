from __future__ import annotations

import io
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from proj_janitor.backup import run_backup, sha256_file
import proj_janitor.restore as restore_module
import proj_janitor.scanner as scanner_module
from proj_janitor.restore import (
    RestoreError,
    execute_restore,
    plan_restore,
    restore_from_manifest,
    verify_manifest,
)
from proj_janitor.scanner import scan_project
from tests.support import GIT_AVAILABLE, git, init_repo, make_pushed_repo

GIT_LFS_AVAILABLE = shutil.which("git-lfs") is not None


def remove_readonly(function, path: str, error_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


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


def write_manifest(path: Path, records: list[dict]) -> Path:
    manifest = {
        "schema_version": 2,
        "created_at": "2026-10-23T00:00:00+00:00",
        "tool": "proj-janitor",
        "version": "0.1.0",
        "include_artifacts": False,
        "always_archive": False,
        "records": records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def archive_backup(root: Path, name: str = "app") -> tuple[Path, Path, dict]:
    project = make_project(root, name)
    backups = root / "backups"
    manifest_paths, _, _ = run_backup([scan_project(project)], backups)
    manifest_path = manifest_paths[0]
    record = json.loads(manifest_path.read_text(encoding="utf-8"))["records"][0]
    return project, manifest_path, record


class RestoreTests(unittest.TestCase):
    def test_verify_manifest_checks_archive_without_restoring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, record = archive_backup(root)

            results = verify_manifest(manifest)

            self.assertEqual(results[0]["status"], "verified")
            self.assertEqual(results[0]["sha256"], record["sha256"])
            self.assertTrue(project.exists())

            with Path(record["archive"]).open("ab") as handle:
                handle.write(b"corruption")
            with self.assertRaisesRegex(RestoreError, "sha256 mismatch"):
                verify_manifest(manifest)

    def test_verify_manifest_reports_git_metadata_without_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "app",
                        "strategy": "git",
                        "remote_url": str(root / "app.git"),
                        "commit": "a" * 40,
                    }
                ],
            )

            results = verify_manifest(manifest)

            self.assertEqual(results[0]["status"], "metadata-only")
            self.assertEqual(results[0]["commit"], "a" * 40)

    def test_git_timeout_stops_process_and_windows_tree_kill_falls_back(self) -> None:
        process = mock.Mock(pid=123, stdout=None, stderr=None)
        process.communicate.return_value = (None, None)

        with (
            mock.patch.object(restore_module, "os", SimpleNamespace(name="nt")),
            mock.patch.object(
                restore_module.subprocess,
                "run",
                return_value=mock.Mock(returncode=1),
            ) as taskkill,
            mock.patch.object(restore_module, "_kill_git_process") as kill_git,
        ):
            restore_module._stop_git_process(process)

        taskkill.assert_called_once()
        kill_git.assert_called_once_with(process)
        process.communicate.assert_called_once_with(timeout=10)

        process = mock.Mock(pid=123)
        process.communicate.side_effect = restore_module.subprocess.TimeoutExpired(
            ["git", "status"], timeout=10
        )
        with (
            mock.patch.object(
                restore_module.subprocess,
                "Popen",
                return_value=process,
            ),
            mock.patch.object(restore_module, "_stop_git_process") as stop_git,
            self.assertRaisesRegex(RestoreError, "timed out after"),
        ):
            restore_module._run_git(["status"])
        stop_git.assert_called_once_with(process)

    def test_posix_git_timeout_kills_child_process_group(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX process groups are required")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            marker = root / "child-output"
            child_code = (
                "import pathlib,time; p=pathlib.Path(" + repr(str(marker)) + "); "
                "exec(\"while True:\\n with p.open('a') as f: f.write('x')\\n "
                "time.sleep(0.01)\")"
            )
            fake_git = bin_dir / "git"
            fake_git.write_text(
                "#!/bin/sh\n"
                f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)} &\n"
                "wait\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            real_popen = restore_module.subprocess.Popen
            processes = []

            def capture_process(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                processes.append(process)
                return process

            try:
                with (
                    mock.patch.object(restore_module.subprocess, "Popen", side_effect=capture_process),
                    mock.patch.object(restore_module, "_GIT_TIMEOUT_SECONDS", 0.5),
                    self.assertRaisesRegex(RestoreError, "timed out after"),
                ):
                    restore_module._run_git(
                        ["status"],
                        extra_env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                    )

                self.assertIsNotNone(processes[0].poll())
                self.assertTrue(marker.is_file())
                size = marker.stat().st_size
                time.sleep(0.1)
                self.assertEqual(marker.stat().st_size, size)
            finally:
                if processes and processes[0].poll() is None:
                    restore_module._stop_git_process(processes[0])

    def test_windows_git_timeout_kills_child_process_tree(self) -> None:
        if os.name != "nt" or shutil.which("taskkill") is None:
            self.skipTest("Windows taskkill is required")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "child-output"
            pid_file = root / "child.pid"
            child_code = (
                "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
                "exec(\"while True:\\n with p.open('a') as f: f.write('x')\\n "
                "time.sleep(0.02)\")"
            )
            parent_code = (
                "import pathlib,subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]], "
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                "pathlib.Path(sys.argv[3]).write_text(str(child.pid)); "
                "exec('while True: time.sleep(1)')"
            )
            parent = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    parent_code,
                    child_code,
                    str(marker),
                    str(pid_file),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child_pid = None
            try:
                deadline = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.is_file())
                child_pid = int(pid_file.read_text(encoding="utf-8"))
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertGreater(marker.stat().st_size, 0)

                restore_module._stop_git_process(parent)

                self.assertIsNotNone(parent.poll())
                size = marker.stat().st_size
                time.sleep(0.15)
                self.assertEqual(marker.stat().st_size, size)
            finally:
                if parent.poll() is None:
                    restore_module._stop_git_process(parent)
                if child_pid is not None:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(child_pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=10,
                    )

    def test_backup_and_restore_keep_parent_relative_internal_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            link = project / "src" / "project-config"
            try:
                link.symlink_to("../pyproject.toml")
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            backups = root / "backups"

            manifests, _, failures = run_backup(
                [scan_project(project)], backups, always_archive=True
            )
            self.assertFalse(failures)
            restored = root / "restored"
            restore_from_manifest(manifests[0], restored)

            restored_link = restored / "app" / "src" / "project-config"
            self.assertTrue(restored_link.is_symlink())
            self.assertEqual(restored_link.readlink(), Path("../pyproject.toml"))

    def test_restore_archive_into_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, record = archive_backup(root)
            restored = root / "restored"

            plans, results = restore_from_manifest(manifest, restored)

            target = restored / "app"
            self.assertEqual(len(plans), 1)
            self.assertEqual(len(results), 1)
            self.assertEqual(plans[0].strategy, "archive")
            self.assertTrue(plans[0].hash_verified)
            self.assertEqual(results[0].target, target.resolve())
            self.assertEqual(
                (target / "src" / "main.py").read_text(encoding="utf-8"),
                "print('hi')\n",
            )
            self.assertFalse((target / "node_modules").exists())
            self.assertEqual(Path(record["archive"]).parent, manifest.parent)
            self.assertTrue((project / "src" / "main.py").is_file())

    def test_restore_archive_rejects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, first_record = archive_backup(root / "first", "first")
            _, _, second_record = archive_backup(root / "second", "second")
            manifest = write_manifest(
                root / "manifest.json", [first_record, second_record]
            )
            archive = Path(second_record["archive"])
            with archive.open("ab") as handle:
                handle.write(b"corruption")
            restored = root / "restored"

            with self.assertRaisesRegex(RestoreError, "sha256 mismatch"):
                restore_from_manifest(manifest, restored)

            self.assertFalse(restored.exists())

            archive.write_bytes(b"not a gzip archive")
            second_record["sha256"] = sha256_file(archive)
            write_manifest(root / "manifest.json", [first_record, second_record])
            with self.assertRaisesRegex(RestoreError, "already restored: first"):
                restore_from_manifest(manifest, restored)

            self.assertTrue((restored / "first" / "src" / "main.py").is_file())
            self.assertFalse((restored / "second").exists())

    def test_restore_rechecks_archive_after_plan_before_extracting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest, record = archive_backup(root)
            target = root / "restored"
            plans, results = restore_from_manifest(manifest, target, dry_run=True)
            self.assertEqual(results, [])
            self.assertTrue(plans[0].hash_verified)

            Path(record["archive"]).write_bytes(b"replaced after planning")

            with self.assertRaisesRegex(RestoreError, "sha256 mismatch"):
                restore_module.execute_restore(plans)

            self.assertFalse(target.exists())

    def test_restore_rejects_non_integer_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = write_manifest(root / "manifest.json", [{"project": "app"}])
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["schema_version"] = 2.0
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(RestoreError, "schema_version"):
                restore_from_manifest(manifest, root / "restored")

    def test_restore_archive_refuses_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest, _ = archive_backup(root)
            restored = root / "restored"
            target = restored / "app"
            target.mkdir(parents=True)
            (target / "keep.txt").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(RestoreError, "already exists"):
                restore_from_manifest(manifest, restored)

            self.assertEqual(
                (target / "keep.txt").read_text(encoding="utf-8"),
                "keep",
            )

    def test_restore_archive_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "evil.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                payload = b"bad"
                info = tarfile.TarInfo("app/../../escape.txt")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "app",
                        "strategy": "archive",
                        "archive": str(archive),
                        "sha256": sha256_file(archive),
                    }
                ],
            )
            restored = root / "restored"

            with self.assertRaisesRegex(RestoreError, "unsafe path"):
                restore_from_manifest(manifest, restored)

            self.assertFalse((root / "escape.txt").exists())
            self.assertFalse((restored / "app").exists())

    def test_restore_archive_rejects_windows_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "windows-traversal.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                payload = b"bad"
                info = tarfile.TarInfo(r"app/src\..\..\escape.txt")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "app",
                        "strategy": "archive",
                        "archive": str(archive),
                        "sha256": sha256_file(archive),
                    }
                ],
            )
            restored = root / "restored"

            with self.assertRaisesRegex(RestoreError, "unsafe path"):
                restore_from_manifest(manifest, restored)

            self.assertFalse((root / "escape.txt").exists())
            self.assertFalse((restored / "app").exists())

    def test_restore_archive_rejects_hard_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "hardlink.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                payload = b"data"
                file = tarfile.TarInfo("app/file.txt")
                file.size = len(payload)
                tar.addfile(file, io.BytesIO(payload))
                hardlink = tarfile.TarInfo("app/hardlink.txt")
                hardlink.type = tarfile.LNKTYPE
                hardlink.linkname = "app/file.txt"
                tar.addfile(hardlink)
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "app",
                        "strategy": "archive",
                        "archive": str(archive),
                        "sha256": sha256_file(archive),
                    }
                ],
            )
            restored = root / "restored"

            with self.assertRaisesRegex(RestoreError, "unsupported hard link"):
                restore_from_manifest(manifest, restored)

            self.assertFalse((restored / "app").exists())

    def test_restore_archive_rejects_case_colliding_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "case-collision.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for name in ("app/Dir/one.txt", "app/dir/two.txt"):
                    info = tarfile.TarInfo(name)
                    info.size = 1
                    tar.addfile(info, io.BytesIO(b"x"))
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "app",
                        "strategy": "archive",
                        "archive": str(archive),
                        "sha256": sha256_file(archive),
                    }
                ],
            )
            restored = root / "restored"

            with self.assertRaisesRegex(
                RestoreError, "(case-insensitive filesystems|duplicate restore target)"
            ):
                restore_from_manifest(manifest, restored)

            self.assertFalse((restored / "app").exists())

    def test_restore_does_not_retry_extract_without_filter_after_type_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest, _ = archive_backup(root)
            restored = root / "restored"

            with mock.patch(
                "tarfile.TarFile.extractall",
                side_effect=TypeError("extract filter failed"),
            ) as extractall:
                with self.assertRaisesRegex(RestoreError, "cannot extract archive"):
                    restore_from_manifest(manifest, restored)

            self.assertEqual(extractall.call_count, 1)
            self.assertFalse((restored / "app").exists())

    def test_restore_archive_from_moved_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest, _ = archive_backup(root)
            moved = root / "moved" / "app"
            shutil.move(str(manifest.parent), str(moved))
            moved_manifest = moved / manifest.name
            restored = root / "restored"

            _, results = restore_from_manifest(moved_manifest, restored)

            self.assertEqual(len(results), 1)
            self.assertTrue((restored / "app" / "pyproject.toml").is_file())

    def test_restore_can_select_one_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, first_manifest, first = archive_backup(root / "first", "one")
            _, second_manifest, second = archive_backup(root / "second", "two")
            del first_manifest, second_manifest
            manifest = write_manifest(
                root / "combined.json",
                [first, second],
            )
            restored = root / "restored"

            plans, _ = restore_from_manifest(
                manifest,
                restored,
                projects=["one"],
            )

            self.assertEqual([plan.project for plan in plans], ["one"])
            self.assertTrue((restored / "one").is_dir())
            self.assertFalse((restored / "two").exists())

    def test_restore_dest_rejects_project_names_that_differ_only_by_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": name,
                        "source": str(root / "original" / name),
                        "strategy": "git",
                        "remote_url": "https://github.com/team/demo.git",
                        "commit": "a" * 40,
                    }
                    for name in ("App", "app")
                ],
            )
            dest = root / "restored"

            for destination in (dest, None):
                with self.assertRaisesRegex(
                    RestoreError, "(case-insensitive filesystems|duplicate restore target)"
                ):
                    restore_from_manifest(manifest, destination)
            self.assertFalse(dest.exists())

    def test_restore_rejects_unsafe_project_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "empty.tar.gz"
            with tarfile.open(archive, "w:gz"):
                pass
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "../escape",
                        "strategy": "archive",
                        "archive": str(archive),
                        "sha256": sha256_file(archive),
                    }
                ],
            )

            with self.assertRaisesRegex(RestoreError, "unsafe project name"):
                restore_from_manifest(manifest, root / "restored")

            self.assertFalse((root / "escape").exists())

    def test_restore_git_rejects_windows_invalid_project_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, name in enumerate(
                ("CON", "CONIN$", "CONOUT$", "bad:name", "trailing.")
            ):
                with self.subTest(name=name):
                    manifest = write_manifest(
                        root / f"manifest-{index}.json",
                        [
                            {
                                "project": name,
                                "source": str(root / "original" / name),
                                "strategy": "git",
                                "remote_url": "https://github.com/team/demo.git",
                                "commit": "a" * 40,
                            }
                        ],
                    )

                    with self.assertRaisesRegex(RestoreError, "unsafe project name"):
                        restore_from_manifest(
                            manifest, root / "restored", dry_run=True
                        )

    def test_restore_archive_to_original_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, _ = archive_backup(root)
            original = project.resolve()
            shutil.rmtree(project, onerror=remove_readonly)

            plans, results = restore_from_manifest(manifest, None)

            self.assertTrue(plans[0].in_place)
            self.assertEqual(plans[0].target, original)
            self.assertEqual(results[0].target, original)
            self.assertEqual(
                (original / "src" / "main.py").read_text(encoding="utf-8"),
                "print('hi')\n",
            )
            self.assertFalse((original / "node_modules").exists())

    def test_restore_original_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, _ = archive_backup(root)
            original = project.resolve()
            shutil.rmtree(project, onerror=remove_readonly)

            plans, results = restore_from_manifest(
                manifest,
                None,
                dry_run=True,
            )

            self.assertEqual(results, [])
            self.assertTrue(plans[0].in_place)
            self.assertFalse(original.exists())

    def test_restore_original_refuses_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest, _ = archive_backup(root)

            with self.assertRaisesRegex(RestoreError, "already exists"):
                restore_from_manifest(manifest, None)

    def test_restore_original_requires_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, _ = archive_backup(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            del payload["records"][0]["source"]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            shutil.rmtree(project, onerror=remove_readonly)

            with self.assertRaisesRegex(RestoreError, "has no source"):
                restore_from_manifest(manifest, None)

    def test_restore_original_rejects_relative_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, _ = archive_backup(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["records"][0]["source"] = "app"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            shutil.rmtree(project, onerror=remove_readonly)

            with self.assertRaisesRegex(RestoreError, "must be absolute"):
                restore_from_manifest(manifest, None)

    def test_restore_original_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, _ = archive_backup(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["records"][0]["source"] = str(
                root / "nested" / ".." / "app"
            )
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            shutil.rmtree(project, onerror=remove_readonly)

            with self.assertRaisesRegex(RestoreError, "unsafe source path"):
                restore_from_manifest(manifest, None)

    def test_restore_original_rejects_source_name_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, _ = archive_backup(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["records"][0]["source"] = str(root / "other")
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            shutil.rmtree(project, onerror=remove_readonly)

            with self.assertRaisesRegex(
                RestoreError,
                "does not match project name",
            ):
                restore_from_manifest(manifest, None)

    def test_restore_original_rejects_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, _ = archive_backup(root)
            original = project.resolve()
            shutil.rmtree(project, onerror=remove_readonly)
            original.symlink_to(root / "elsewhere", target_is_directory=True)

            with self.assertRaisesRegex(RestoreError, "symlink"):
                restore_from_manifest(manifest, None)


@unittest.skipUnless(GIT_AVAILABLE, "git is not available")
class GitRestoreTests(unittest.TestCase):
    def test_git_bundle_restores_unpushed_branch_and_all_local_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            git(project, "switch", "-c", "feature/local")
            (project / "feature.py").write_text("local history\n", encoding="utf-8")
            git(project, "add", "feature.py")
            git(project, "commit", "-m", "local-only commit")
            head = git(project, "rev-parse", "HEAD")
            manifests, records, failures = run_backup(
                [scan_project(project)], root / "backups"
            )
            self.assertFalse(failures)
            self.assertEqual(records[0].strategy, "git_bundle")
            self.assertEqual(verify_manifest(manifests[0])[0]["status"], "verified")

            restored = root / "restored"
            plans, results = restore_from_manifest(manifests[0], restored)
            target = restored / "app"

            self.assertEqual(plans[0].strategy, "git_bundle")
            self.assertEqual(results[0].strategy, "git_bundle")
            self.assertEqual(git(target, "rev-parse", "HEAD"), head)
            self.assertEqual(git(target, "branch", "--show-current"), "feature/local")
            self.assertIn("main", git(target, "branch", "--list"))
            self.assertEqual(git(target, "remote"), "")
            self.assertEqual((target / "feature.py").read_text(encoding="utf-8"), "local history\n")

            with Path(records[0].archive or "").open("ab") as bundle:
                bundle.write(b"corruption")
            with self.assertRaisesRegex(RestoreError, "sha256 mismatch"):
                verify_manifest(manifests[0])

    def test_git_bundle_restores_when_branch_matches_init_default(self) -> None:
        """Regression: fetch must not target the checked-out default branch name.

        Also run restore with cwd outside any git work tree so `git bundle verify`
        cannot accidentally inherit a repository from the caller's directory.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "app"
            project.mkdir()
            (project / "pyproject.toml").write_text("", encoding="utf-8")
            (project / "main.py").write_text("print('master')\n", encoding="utf-8")
            git(project, "init", "-q", "-b", "master")
            git(project, "config", "user.email", "test@example.com")
            git(project, "config", "user.name", "Test User")
            git(project, "add", ".")
            git(project, "commit", "-q", "-m", "initial")
            head = git(project, "rev-parse", "HEAD")

            manifests, records, failures = run_backup(
                [scan_project(project)], root / "backups"
            )
            self.assertFalse(failures)
            self.assertEqual(records[0].strategy, "git_bundle")
            self.assertEqual(records[0].branch, "master")

            restored = root / "restored"
            outside_git = root / "outside-git-cwd"
            outside_git.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(outside_git)
                plans, results = restore_from_manifest(manifests[0], restored)
            finally:
                os.chdir(previous)
            target = restored / "app"
            self.assertEqual(plans[0].strategy, "git_bundle")
            self.assertEqual(results[0].strategy, "git_bundle")
            self.assertEqual(git(target, "rev-parse", "HEAD"), head)
            self.assertEqual(git(target, "branch", "--show-current"), "master")
            self.assertEqual(
                (target / "main.py").read_text(encoding="utf-8"),
                "print('master')\n",
            )

    def test_git_bundle_restore_surfaces_git_stderr_on_invalid_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "backups" / "app"
            backup_dir.mkdir(parents=True)
            bundle = backup_dir / "app-20261107-120000.bundle"
            bundle.write_text("not a git bundle\n", encoding="utf-8")
            manifest = backup_dir / "manifest-20261107-120000.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "created_at": "2026-11-07T00:00:00+00:00",
                        "tool": "proj-janitor",
                        "version": "0.1.0",
                        "include_artifacts": False,
                        "always_archive": False,
                        "exclude_git_metadata": False,
                        "records": [
                            {
                                "project": "app",
                                "source": str(root / "app"),
                                "archive": str(bundle),
                                "archive_bytes": 16,
                                "source_bytes": 1,
                                "sha256": "00",
                                "strategy": "git_bundle",
                                "remote": None,
                                "remote_url": None,
                                "branch": "master",
                                "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                "upstream": None,
                                "verified": "local-bundle",
                                "fallback_reason": "branch has no upstream remote",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RestoreError,
                r"invalid Git bundle for app: .+",
            ):
                restore_from_manifest(manifest, root / "restored", verify=False)

    def test_git_failure_detail_redacts_credentialed_urls(self) -> None:
        self.assertEqual(
            restore_module._redact_git_message(
                "fatal: could not read Username for 'https://user:token@example.com/repo'"
            ),
            "remote URL details redacted",
        )
        self.assertEqual(
            restore_module._redact_git_message("fatal: need a repository to verify a bundle"),
            "fatal: need a repository to verify a bundle",
        )

    @unittest.skipUnless(
        GIT_LFS_AVAILABLE and os.name != "nt",
        "git-lfs or a local-file LFS endpoint is not available",
    )
    def test_backup_and_restore_git_lfs_objects_with_git_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "lfs-app"
            project.mkdir()
            (project / "pyproject.toml").write_text("", encoding="utf-8")
            init_repo(project)
            (project / ".gitignore").write_text(".husky/_/\n", encoding="utf-8")
            (project / ".husky" / "_").mkdir(parents=True)
            (project / ".husky" / "pre-commit").write_text(
                "npm test\n",
                encoding="utf-8",
            )
            (project / ".husky" / "_" / "husky.sh").write_text(
                "# generated hook helper\n",
                encoding="utf-8",
            )
            git(project, "lfs", "install", "--local")
            git(project, "lfs", "track", "*.bin")
            payload = b"a tracked Git LFS payload\n"
            (project / "payload.bin").write_bytes(payload)
            git(project, "add", ".")
            git(project, "commit", "-m", "LFS project")
            commit = git(project, "rev-parse", "HEAD")

            remote = root / "lfs-app-remote.git"
            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
            )
            git(project, "remote", "add", "origin", str(remote))
            git(project, "push", "-u", "origin", "main")
            git(project, "remote", "set-url", "origin", "https://github.com/acme/lfs-app.git")

            git_run = scanner_module._git_run

            def without_git_lfs(path: Path, *args: str):
                if args == ("lfs", "version"):
                    return subprocess.CompletedProcess(
                        ["git", *args], 1, "", "git-lfs is unavailable"
                    )
                return git_run(path, *args)

            with mock.patch.object(
                scanner_module,
                "_git_run",
                side_effect=without_git_lfs,
            ):
                _, fallback_records, fallback_failures = run_backup(
                    [scan_project(project)], root / "without-lfs"
                )
            self.assertFalse(fallback_failures)
            self.assertEqual(fallback_records[0].strategy, "archive")
            self.assertEqual(
                fallback_records[0].fallback_reason,
                "Git LFS is not installed",
            )

            manifests, records, failures = run_backup(
                [scan_project(project)], root / "backups"
            )
            self.assertFalse(failures)
            self.assertEqual(records[0].strategy, "git", records[0].fallback_reason)
            self.assertIsNone(records[0].archive)
            object_hash = hashlib.sha256(payload).hexdigest()
            self.assertIn(object_hash, records[0].lfs_objects or [])

            restored_dest = root / "restored"
            remote_url = remote.resolve().as_uri()
            git_config = {
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": f"url.{remote_url}.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/acme/lfs-app.git",
                "GIT_CONFIG_KEY_1": "protocol.file.allow",
                "GIT_CONFIG_VALUE_1": "always",
            }
            with mock.patch.dict(os.environ, git_config):
                _, results = restore_from_manifest(manifests[0], restored_dest)

            self.assertEqual(results[0].strategy, "git")
            self.assertEqual(
                (restored_dest / "lfs-app" / "payload.bin").read_bytes(), payload
            )
            object_hash = hashlib.sha256(payload).hexdigest()
            lfs_object = (
                remote
                / "lfs"
                / "objects"
                / object_hash[:2]
                / object_hash[2:4]
                / object_hash
            )
            lfs_object.unlink()
            failed_dest = root / "missing-lfs-restore"
            with mock.patch.dict(os.environ, git_config):
                with self.assertRaises(RestoreError):
                    restore_from_manifest(manifests[0], failed_dest)
            self.assertFalse((failed_dest / "lfs-app").exists())

    def test_backup_and_restore_uninitialized_submodule_with_git_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = make_pushed_repo(root, "child")
            child_commit = git(child, "rev-parse", "HEAD")
            child_remote = root / "child-remote.git"

            project = root / "app"
            project.mkdir()
            (project / "pyproject.toml").write_text("", encoding="utf-8")
            (project / ".gitmodules").write_text(
                '[submodule "vendor/child"]\n'
                "\tpath = vendor/child\n"
                "\turl = https://github.com/acme/child.git\n",
                encoding="utf-8",
            )
            init_repo(project)
            git(project, "add", ".")
            git(project, "commit", "-m", "parent")
            git(
                project,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{child_commit},vendor/child",
            )
            git(project, "commit", "-m", "add submodule")
            (project / "vendor" / "child").mkdir(parents=True)
            stray = project / "vendor" / "child" / "local-data.txt"
            stray.write_text("keep me\n", encoding="utf-8")

            parent_remote = root / "app-remote.git"
            subprocess.run(
                ["git", "init", "--bare", str(parent_remote)],
                check=True,
                capture_output=True,
            )
            git(project, "remote", "add", "origin", str(parent_remote))
            git(project, "push", "-u", "origin", "main")
            git(project, "remote", "set-url", "origin", "https://github.com/acme/app.git")

            backup_dir = root / "backups"
            stray_manifests, stray_records, stray_failures = run_backup(
                [scan_project(project)], root / "stray-backups"
            )
            self.assertFalse(stray_failures)
            self.assertEqual(stray_records[0].strategy, "archive")
            self.assertIn("uninitialized submodule path is not empty", stray_records[0].fallback_reason)
            stray.unlink()

            manifests, records, failures = run_backup(
                [scan_project(project)], backup_dir
            )

            self.assertFalse(failures)
            self.assertEqual(records[0].strategy, "git", records[0].fallback_reason)
            self.assertIsNone(records[0].archive)
            self.assertEqual(len(list((backup_dir / "app").glob("manifest-*.json"))), 1)

            restore_dest = root / "restored"
            submodule_remote = child_remote.resolve().as_uri()
            parent_url = parent_remote.resolve().as_uri()
            git_config = {
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": f"url.{parent_url}.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/acme/app.git",
                "GIT_CONFIG_KEY_1": f"url.{submodule_remote}.insteadOf",
                "GIT_CONFIG_VALUE_1": "https://github.com/acme/child.git",
                "GIT_CONFIG_KEY_2": "protocol.file.allow",
                "GIT_CONFIG_VALUE_2": "always",
            }
            with mock.patch.dict(os.environ, git_config):
                plans, results = restore_from_manifest(manifests[0], restore_dest)

            restored = restore_dest / "app"
            restored_child = restored / "vendor" / "child"
            self.assertEqual(plans[0].strategy, "git")
            self.assertEqual(results[0].strategy, "git")
            self.assertTrue((restored / "pyproject.toml").is_file())
            self.assertEqual(
                (restored_child / "main.py").read_text(encoding="utf-8"),
                "print('hi')\n",
            )
            self.assertEqual(git(restored_child, "rev-parse", "HEAD"), child_commit)
            git(restored, "switch", "--track", "-c", "main", "origin/main")
            initialized_backups, initialized_records, initialized_failures = run_backup(
                [scan_project(restored)], root / "initialized-backups"
            )
            self.assertFalse(initialized_failures)
            self.assertEqual(initialized_records[0].strategy, "git", initialized_records[0].fallback_reason)
            self.assertIsNone(initialized_records[0].fallback_reason)

            failed_dest = root / "failed-restore"
            broken_config = git_config | {
                "GIT_CONFIG_VALUE_1": (root / "missing-child.git").as_uri()
            }
            with mock.patch.dict(os.environ, broken_config):
                with self.assertRaises(RestoreError):
                    restore_from_manifest(manifests[0], failed_dest)
            self.assertFalse((failed_dest / "app").exists())

    def test_verify_and_restore_check_remote_probes_submodules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = make_pushed_repo(root, "child")
            child_commit = git(child, "rev-parse", "HEAD")
            child_remote = root / "child-remote.git"

            project = root / "app"
            project.mkdir()
            (project / "pyproject.toml").write_text("", encoding="utf-8")
            (project / ".gitmodules").write_text(
                '[submodule "vendor/child"]\n'
                "\tpath = vendor/child\n"
                "\turl = https://github.com/acme/child.git\n",
                encoding="utf-8",
            )
            init_repo(project)
            git(project, "add", ".")
            git(project, "commit", "-m", "parent")
            git(
                project,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{child_commit},vendor/child",
            )
            git(project, "commit", "-m", "add submodule")
            (project / "vendor" / "child").mkdir(parents=True)

            parent_remote = root / "app-remote.git"
            subprocess.run(
                ["git", "init", "--bare", str(parent_remote)],
                check=True,
                capture_output=True,
            )
            git(project, "remote", "add", "origin", str(parent_remote))
            git(project, "push", "-u", "origin", "main")
            git(project, "remote", "set-url", "origin", "https://github.com/acme/app.git")

            backup_dir = root / "backups"
            manifests, records, failures = run_backup(
                [scan_project(project)], backup_dir
            )
            self.assertFalse(failures)
            self.assertEqual(records[0].strategy, "git", records[0].fallback_reason)
            self.assertEqual(
                records[0].submodules,
                [
                    {
                        "path": "vendor/child",
                        "url": "https://github.com/acme/child.git",
                        "commit": child_commit,
                    }
                ],
            )

            parent_url = parent_remote.resolve().as_uri()
            submodule_remote = child_remote.resolve().as_uri()
            reachable_config = {
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": f"url.{parent_url}.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/acme/app.git",
                "GIT_CONFIG_KEY_1": f"url.{submodule_remote}.insteadOf",
                "GIT_CONFIG_VALUE_1": "https://github.com/acme/child.git",
                "GIT_CONFIG_KEY_2": "protocol.file.allow",
                "GIT_CONFIG_VALUE_2": "always",
            }
            with mock.patch.dict(os.environ, reachable_config):
                verified = verify_manifest(manifests[0], check_remote=True)
            self.assertEqual(verified[0]["status"], "remote-verified")
            checked = verified[0]["submodules"]
            self.assertEqual(len(checked), 1)
            self.assertEqual(checked[0]["path"], "vendor/child")
            self.assertEqual(checked[0]["status"], "remote-verified")

            unreachable_config = reachable_config | {
                "GIT_CONFIG_VALUE_1": (root / "missing-child.git").as_uri()
            }
            with mock.patch.dict(os.environ, unreachable_config):
                unreachable = verify_manifest(manifests[0], check_remote=True)
            self.assertEqual(unreachable[0]["status"], "remote-unreachable")
            self.assertEqual(unreachable[0]["submodules"][0]["status"], "remote-unreachable")

            failed_dest = root / "failed-restore"
            with mock.patch.dict(os.environ, unreachable_config):
                with self.assertRaises(RestoreError) as context:
                    restore_from_manifest(manifests[0], failed_dest, check_remote=True)
            self.assertIn("submodule vendor/child", str(context.exception))
            self.assertFalse((failed_dest / "app").exists())

    def test_restore_git_clones_and_checks_out_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            remote = root / "app-remote.git"
            head = git(project, "rev-parse", "HEAD")
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "app",
                        "source": str(project),
                        "archive": None,
                        "archive_bytes": 0,
                        "source_bytes": 1,
                        "sha256": None,
                        "strategy": "git",
                        "remote": "origin",
                        "remote_url": str(remote),
                        "branch": "main",
                        "commit": head,
                        "upstream": "origin/main",
                        "verified": "local-ref",
                        "fallback_reason": None,
                    }
                ],
            )
            restored = root / "restored"

            _, results = restore_from_manifest(manifest, restored)

            target = restored / "app"
            self.assertEqual(results[0].strategy, "git")
            self.assertEqual(
                (target / "main.py").read_text(encoding="utf-8"),
                "print('hi')\n",
            )
            self.assertEqual(git(target, "rev-parse", "HEAD"), head)
            self.assertEqual(
                git(target, "rev-parse", "--abbrev-ref", "HEAD"),
                "HEAD",
            )

    def test_restore_git_cleans_up_failed_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            remote = root / "app-remote.git"
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "app",
                        "strategy": "git",
                        "remote_url": str(remote),
                        "commit": "0" * 40,
                    }
                ],
            )
            restored = root / "restored"

            with self.assertRaisesRegex(RestoreError, "git -C"):
                restore_from_manifest(manifest, restored)

            self.assertTrue(restored.is_dir())
            self.assertEqual(list(restored.iterdir()), [])
            self.assertFalse((restored / "app").exists())

    def test_restore_git_to_original_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            original = project.resolve()
            remote = root / "app-remote.git"
            head = git(project, "rev-parse", "HEAD")
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "app",
                        "source": str(project),
                        "archive": None,
                        "archive_bytes": 0,
                        "source_bytes": 1,
                        "sha256": None,
                        "strategy": "git",
                        "remote": "origin",
                        "remote_url": str(remote),
                        "branch": "main",
                        "commit": head,
                        "upstream": "origin/main",
                        "verified": "local-ref",
                        "fallback_reason": None,
                    }
                ],
            )
            shutil.rmtree(project, onerror=remove_readonly)

            plans, results = restore_from_manifest(manifest, None)

            self.assertTrue(plans[0].in_place)
            self.assertEqual(results[0].target, original)
            self.assertEqual(
                (original / "main.py").read_text(encoding="utf-8"),
                "print('hi')\n",
            )
            self.assertEqual(git(original, "rev-parse", "HEAD"), head)
            self.assertEqual(
                git(original, "rev-parse", "--abbrev-ref", "HEAD"),
                "HEAD",
            )


@unittest.skipUnless(GIT_AVAILABLE, "git is not available")
class RemoteCheckTests(unittest.TestCase):
    def git_record(
        self, project: Path, remote: Path | str, commit: str, **overrides: object
    ) -> dict:
        record = {
            "project": project.name,
            "source": str(project),
            "archive": None,
            "archive_bytes": 0,
            "source_bytes": 1,
            "sha256": None,
            "strategy": "git",
            "remote": "origin",
            "remote_url": str(remote),
            "branch": "main",
            "commit": commit,
            "upstream": "origin/main",
            "verified": "local-ref",
            "fallback_reason": None,
        }
        record.update(overrides)
        return record

    def test_check_remote_lists_refs_at_recorded_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            remote = root / "app-remote.git"
            head = git(project, "rev-parse", "HEAD")
            git(project, "tag", "-a", "-m", "release", "v1.0")
            git(project, "push", "-q", str(remote), "refs/tags/v1.0")
            manifest = write_manifest(
                root / "manifest.json",
                [self.git_record(project, remote, head)],
            )

            result = verify_manifest(manifest, check_remote=True)[0]

            self.assertEqual(result["status"], "remote-verified")
            self.assertEqual(
                result["remote"]["refs"],
                ["refs/heads/main", "refs/tags/v1.0"],
            )
            self.assertEqual(result["remote"]["branch"], "main")
            self.assertIsNone(result["remote"]["reason"])
            self.assertEqual(result["remote"]["url"], str(remote))

    def test_check_remote_reports_moved_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            remote = root / "app-remote.git"
            head = git(project, "rev-parse", "HEAD")
            (project / "later.py").write_text("print('later')\n", encoding="utf-8")
            git(project, "add", ".")
            git(project, "commit", "-q", "-m", "later")
            git(project, "push", "-q", str(remote), "main")
            manifest = write_manifest(
                root / "manifest.json",
                [self.git_record(project, remote, head)],
            )

            result = verify_manifest(manifest, check_remote=True)[0]

            self.assertEqual(result["status"], "remote-moved")
            self.assertEqual(
                result["remote"]["reason"], "no-ref-matches-commit"
            )
            self.assertEqual(result["remote"]["refs"], [])

    def test_check_remote_reports_unreachable_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            head = git(project, "rev-parse", "HEAD")
            manifest = write_manifest(
                root / "manifest.json",
                [
                    self.git_record(
                        project, root / "removed-remote.git", head
                    )
                ],
            )

            result = verify_manifest(manifest, check_remote=True)[0]

            self.assertEqual(result["status"], "remote-unreachable")
            self.assertEqual(result["remote"]["reason"], "ls-remote-failed")

    def test_check_remote_reports_probe_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            head = git(project, "rev-parse", "HEAD")
            manifest = write_manifest(
                root / "manifest.json",
                [self.git_record(project, root / "app-remote.git", head)],
            )
            with mock.patch.object(
                restore_module,
                "_run_git",
                side_effect=restore_module.GitTimeoutError("timed out"),
            ):
                result = verify_manifest(manifest, check_remote=True)[0]

            self.assertEqual(result["status"], "remote-unreachable")
            self.assertEqual(result["remote"]["reason"], "ls-remote-timed-out")

    def test_check_remote_never_passes_credentials_to_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            head = git(project, "rev-parse", "HEAD")
            manifest = write_manifest(
                root / "manifest.json",
                [
                    self.git_record(
                        project,
                        "https://user:token@github.com/acme/app.git",
                        head,
                    )
                ],
            )
            with mock.patch.object(
                restore_module,
                "_run_git",
                side_effect=AssertionError("remote must not be contacted"),
            ):
                result = verify_manifest(manifest, check_remote=True)[0]

            self.assertEqual(result["status"], "remote-skipped")
            self.assertEqual(
                result["remote"]["reason"], "credentials-in-remote-url"
            )
            self.assertEqual(
                result["remote"]["url"], "https://github.com/acme/app.git"
            )

    def test_verify_without_check_remote_makes_no_git_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            remote = root / "app-remote.git"
            head = git(project, "rev-parse", "HEAD")
            manifest = write_manifest(
                root / "manifest.json",
                [self.git_record(project, remote, head)],
            )
            with mock.patch.object(
                restore_module,
                "_run_git",
                side_effect=AssertionError("remote must not be contacted"),
            ):
                result = verify_manifest(manifest)[0]

            self.assertEqual(result["status"], "metadata-only")
            self.assertNotIn("remote", result)

    def test_check_remote_leaves_archive_records_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest, _ = archive_backup(root)

            with mock.patch.object(
                restore_module,
                "_run_git",
                side_effect=AssertionError("remote must not be contacted"),
            ):
                result = verify_manifest(manifest, check_remote=True)[0]

            self.assertEqual(result["status"], "verified")
            self.assertNotIn("remote", result)


class RestoreRemoteCheckTests(unittest.TestCase):
    """execute_restore / restore --check-remote precheck."""

    def test_execute_restore_check_remote_blocks_unreachable_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            head = git(project, "rev-parse", "HEAD")
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "app",
                        "source": str(project),
                        "archive": None,
                        "archive_bytes": 0,
                        "source_bytes": 1,
                        "sha256": None,
                        "strategy": "git",
                        "remote": "origin",
                        "remote_url": str(root / "removed-remote.git"),
                        "branch": "main",
                        "commit": head,
                        "upstream": "origin/main",
                        "verified": "local-ref",
                    }
                ],
            )
            restored = root / "restored"
            plans = plan_restore(manifest, restored)

            with self.assertRaisesRegex(
                RestoreError,
                r"cannot restore app: remote .*removed-remote\.git is unreachable",
            ):
                execute_restore(plans, check_remote=True)

            self.assertFalse((restored / "app").exists())

    def test_execute_restore_check_remote_proceeds_when_remote_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            head = git(project, "rev-parse", "HEAD")
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "app",
                        "source": str(project),
                        "archive": None,
                        "archive_bytes": 0,
                        "source_bytes": 1,
                        "sha256": None,
                        "strategy": "git",
                        "remote": "origin",
                        "remote_url": str(root / "app-remote.git"),
                        "branch": "main",
                        "commit": head,
                        "upstream": "origin/main",
                        "verified": "local-ref",
                    }
                ],
            )
            restored = root / "restored"
            plans = plan_restore(manifest, restored)

            results = execute_restore(plans, check_remote=True)

            self.assertEqual(results[0].project, "app")
            target = restored / "app"
            self.assertEqual(git(target, "rev-parse", "HEAD"), head)

    def test_execute_restore_check_remote_incomplete_git_plan(self) -> None:
        from proj_janitor.models import RestorePlan

        plans = [
            RestorePlan(
                project="app",
                strategy="git",
                target=Path("/nowhere/app"),
                remote_url=None,
                commit=None,
            )
        ]
        with self.assertRaisesRegex(RestoreError, "incomplete git restore plan"):
            execute_restore(plans, check_remote=True)

    def test_execute_restore_check_remote_reports_probe_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            head = git(project, "rev-parse", "HEAD")
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "app",
                        "source": str(project),
                        "archive": None,
                        "archive_bytes": 0,
                        "source_bytes": 1,
                        "sha256": None,
                        "strategy": "git",
                        "remote": "origin",
                        "remote_url": str(root / "app-remote.git"),
                        "branch": "main",
                        "commit": head,
                        "upstream": "origin/main",
                        "verified": "local-ref",
                    }
                ],
            )
            restored = root / "restored"
            plans = plan_restore(manifest, restored)
            with mock.patch.object(
                restore_module,
                "_run_git",
                side_effect=restore_module.GitTimeoutError("timed out"),
            ):
                with self.assertRaisesRegex(RestoreError, "is unreachable"):
                    execute_restore(plans, check_remote=True)

            self.assertFalse((restored / "app").exists())

    def test_execute_restore_check_remote_ignores_archive_plans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest, record = archive_backup(root)
            restored = root / "restored"
            plans = plan_restore(manifest, restored)

            with mock.patch.object(
                restore_module,
                "_probe_remote",
                side_effect=AssertionError("remote must not be contacted"),
            ):
                results = execute_restore(plans, check_remote=True)

            self.assertEqual(results[0].project, "app")
            self.assertTrue((restored / "app" / "src" / "main.py").exists())
            self.assertEqual(record["strategy"], "archive")

    def test_restore_from_manifest_check_remote_blocks_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            head = git(project, "rev-parse", "HEAD")
            manifest = write_manifest(
                root / "manifest.json",
                [
                    {
                        "project": "app",
                        "source": str(project),
                        "archive": None,
                        "archive_bytes": 0,
                        "source_bytes": 1,
                        "sha256": None,
                        "strategy": "git",
                        "remote": "origin",
                        "remote_url": str(root / "removed-remote.git"),
                        "branch": "main",
                        "commit": head,
                        "upstream": "origin/main",
                        "verified": "local-ref",
                    }
                ],
            )
            restored = root / "restored"
            with self.assertRaisesRegex(RestoreError, "is unreachable"):
                restore_from_manifest(manifest, restored, check_remote=True)

            self.assertFalse((restored / "app").exists())


if __name__ == "__main__":
    unittest.main()
