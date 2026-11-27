from __future__ import annotations

import io
import hashlib
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import collections
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError, URLError

from proj_janitor.backup import run_backup, sha256_file
import proj_janitor.restore as restore_module
import proj_janitor.scanner as scanner_module
from proj_janitor.restore import (
    PartialRestoreError,
    _git_args_have_sensitive_remote,
    _probe_lfs_batch,
    _extract_incremental_delta,
    _check_restore_space,
    _publish_restored_tree,
    _remove_deleted_paths,
    _restore_git_bundle,
    _restore_incremental,
    _rewrite_remote_url,
    _validate_remote_url,
    load_manifest,
    RestoreError,
    execute_restore,
    format_decrypt_restore_warning,
    format_fail_on_remote_moved_warning,
    format_fail_on_remote_skipped_warning,
    format_allowed_roots_warning,
    format_git_checkout_branch_warning,
    format_require_dest_warning,
    format_keep_replaced_restore_warning,
    format_no_verify_restore_warning,
    format_rollback_on_error_warning,
    format_skip_existing_restore_warning,
    plan_restore,
    restore_from_manifest,
    verify_manifest,
)
from proj_janitor.scanner import scan_project
from tests.support import GIT_AVAILABLE, commit_all, git, init_repo, make_pushed_repo

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
    def test_format_skip_existing_restore_warning(self) -> None:
        self.assertEqual(
            format_skip_existing_restore_warning(1),
            "warning: 1 existing target will be left unchanged "
            "(--skip-existing); omit that flag or remove the target to restore",
        )
        self.assertEqual(
            format_skip_existing_restore_warning(2),
            "warning: 2 existing targets will be left unchanged "
            "(--skip-existing); omit that flag or remove the target to restore",
        )

    def test_format_no_verify_restore_warning(self) -> None:
        self.assertEqual(
            format_no_verify_restore_warning(),
            "warning: --no-verify skips archive/bundle SHA-256 checks; "
            "only use with trusted manifests and archives",
        )

    def test_format_decrypt_restore_warning(self) -> None:
        self.assertEqual(
            format_decrypt_restore_warning(),
            "warning: decrypting archives requires the matching backup passphrase; "
            "keep the passphrase file private",
        )

    def test_format_fail_on_remote_moved_warning(self) -> None:
        self.assertEqual(
            format_fail_on_remote_moved_warning(),
            "warning: --fail-on-remote-moved hard-fails when --check-remote finds "
            "no remote ref for the recorded commit",
        )

    def test_format_fail_on_remote_skipped_warning(self) -> None:
        self.assertEqual(
            format_fail_on_remote_skipped_warning(),
            "warning: --fail-on-remote-skipped hard-fails when --check-remote skips "
            "a remote due to credentials/query/fragment in the URL",
        )

    def test_format_require_dest_warning(self) -> None:
        self.assertEqual(
            format_require_dest_warning(),
            "warning: --require-dest refuses restore without an explicit --dest "
            "(no in-place restore to manifest source)",
        )

    def test_format_allowed_roots_warning(self) -> None:
        self.assertEqual(
            format_allowed_roots_warning(),
            "warning: --allowed-root restricts restore targets to the given root(s); "
            "paths outside them are refused",
        )

    def test_format_git_checkout_branch_warning(self) -> None:
        self.assertEqual(
            format_git_checkout_branch_warning(1),
            "warning: 1 git-only record will restore via checkout -B "
            "(creates/resets the local branch); omit --checkout-branch for detached HEAD",
        )
        self.assertEqual(
            format_git_checkout_branch_warning(2),
            "warning: 2 git-only records will restore via checkout -B "
            "(creates/resets the local branch); omit --checkout-branch for detached HEAD",
        )

    def test_format_keep_replaced_restore_warning(self) -> None:
        self.assertEqual(
            format_keep_replaced_restore_warning(1),
            "warning: --keep-replaced will rename 1 existing target aside; "
            "reclaim disk space later if the .replaced-* copies are unneeded",
        )
        self.assertIn("2 existing targets", format_keep_replaced_restore_warning(2))

    def test_format_rollback_on_error_warning(self) -> None:
        self.assertEqual(
            format_rollback_on_error_warning(),
            "warning: --rollback-on-error will undo successful restores "
            "if a later project fails; omit it to keep partial successes",
        )

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

    @unittest.skipUnless(GIT_AVAILABLE, "git is required")
    def test_git_lock_contention_fails_fast_on_checkout(self) -> None:
        """V-06: stale index.lock must fail checkout quickly with a clear error."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("data\n", encoding="utf-8")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-q", "-m", "c1")
            lock = repo / ".git" / "index.lock"
            lock.write_text("", encoding="utf-8")

            started = time.monotonic()
            completed = restore_module._run_git(
                ["-C", str(repo), "checkout", "-f", "HEAD"],
                check=False,
                timeout=15,
            )
            elapsed = time.monotonic() - started

            self.assertNotEqual(completed.returncode, 0, completed.stderr)
            self.assertLess(elapsed, 10.0, f"lock contention hung for {elapsed:.1f}s")
            combined = f"{completed.stdout}\n{completed.stderr}".lower()
            self.assertTrue(
                "index.lock" in combined or "another git process" in combined,
                completed.stderr,
            )

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

    def test_posix_detached_descendant_survives_process_group_kill(self) -> None:
        """V-06: setsid() grandchildren escape killpg (documented limitation)."""
        if os.name != "posix":
            self.skipTest("POSIX process groups are required")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            marker = root / "detached-output"
            pid_file = root / "detached.pid"
            detached_code = (
                "import os,pathlib,time;"
                "os.setsid();"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));"
                f"p=pathlib.Path({str(marker)!r});"
                "exec(\"while True:\\n with p.open('a') as f: f.write('y')\\n "
                "time.sleep(0.02)\")"
            )
            fake_git = bin_dir / "git"
            fake_git.write_text(
                "#!/bin/sh\n"
                f"{shlex.quote(sys.executable)} -c {shlex.quote(detached_code)} &\n"
                "wait\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            real_popen = restore_module.subprocess.Popen
            processes: list[subprocess.Popen[str]] = []

            def capture_process(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                processes.append(process)
                return process

            detached_pid: int | None = None
            try:
                with (
                    mock.patch.object(
                        restore_module.subprocess, "Popen", side_effect=capture_process
                    ),
                    mock.patch.object(restore_module, "_GIT_TIMEOUT_SECONDS", 0.5),
                    self.assertRaisesRegex(RestoreError, "timed out after"),
                ):
                    restore_module._run_git(
                        ["status"],
                        extra_env={
                            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
                        },
                    )

                self.assertIsNotNone(processes[0].poll())
                deadline = time.monotonic() + 2
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.is_file(), "detached child did not start")
                detached_pid = int(pid_file.read_text(encoding="utf-8"))
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.is_file())
                size = marker.stat().st_size
                time.sleep(0.15)
                # Still growing: killpg did not reach the setsid() grandchild.
                self.assertGreater(marker.stat().st_size, size)
            finally:
                if processes and processes[0].poll() is None:
                    restore_module._stop_git_process(processes[0])
                if detached_pid is not None:
                    try:
                        os.kill(detached_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        os.waitpid(detached_pid, 0)
                    except ChildProcessError:
                        pass

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
                marker_deadline = time.monotonic() + 5
                while (
                    not marker.exists() or marker.stat().st_size == 0
                ) and time.monotonic() < marker_deadline:
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

            shutil.rmtree(restored)
            _, _, third_record = archive_backup(root / "third", "third")
            write_manifest(
                root / "manifest.json",
                [first_record, second_record, third_record],
            )
            with self.assertRaises(PartialRestoreError) as raised:
                restore_from_manifest(
                    manifest, restored, continue_on_error=True, verify=False
                )
            self.assertEqual(
                [result.project for result in raised.exception.results],
                ["first", "third"],
            )
            self.assertEqual(
                [project for project, _ in raised.exception.failures],
                ["second"],
            )
            self.assertTrue((restored / "first" / "src" / "main.py").is_file())
            self.assertFalse((restored / "second").exists())
            self.assertTrue((restored / "third" / "src" / "main.py").is_file())

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

    def test_restore_archive_force_replaces_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, _ = archive_backup(root)
            restored = root / "restored"
            target = restored / project.name
            target.mkdir(parents=True)
            (target / "stale.txt").write_text("stale", encoding="utf-8")

            plans, results = restore_from_manifest(manifest, restored, force=True)
            self.assertTrue(plans[0].overwrite)
            self.assertEqual(len(results), 1)
            self.assertFalse((target / "stale.txt").exists())
            self.assertTrue((target / "src" / "main.py").is_file())

    def test_extractable_file_member_accepts_gnu_sparse_and_contiguous(self) -> None:
        from proj_janitor.restore import _is_extractable_file_member

        regular = tarfile.TarInfo("app/file.bin")
        regular.type = tarfile.REGTYPE
        self.assertTrue(_is_extractable_file_member(regular))

        sparse = tarfile.TarInfo("app/kernel.elf")
        sparse.type = tarfile.GNUTYPE_SPARSE
        self.assertTrue(_is_extractable_file_member(sparse))

        if getattr(tarfile, "CONTTYPE", None) is not None:
            contiguous = tarfile.TarInfo("app/contig.bin")
            contiguous.type = tarfile.CONTTYPE
            self.assertTrue(_is_extractable_file_member(contiguous))

        fifo = tarfile.TarInfo("app/pipe")
        fifo.type = tarfile.FIFOTYPE
        self.assertFalse(_is_extractable_file_member(fifo))

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

            with self.assertRaisesRegex(RestoreError, "unsafe archive path"):
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

            with self.assertRaisesRegex(RestoreError, "unsafe archive path"):
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

    def test_restore_archive_rejects_fifo_and_device_nodes(self) -> None:
        """V-03: special files must not be materialised on restore."""
        for entry_type, label in (
            (tarfile.FIFOTYPE, "fifo"),
            (tarfile.CHRTYPE, "char"),
            (tarfile.BLKTYPE, "block"),
        ):
            with self.subTest(entry_type=label):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    archive = root / f"{label}.tar.gz"
                    with tarfile.open(archive, "w:gz") as tar:
                        special = tarfile.TarInfo(f"app/{label}")
                        special.type = entry_type
                        tar.addfile(special)
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
                        RestoreError, "unsupported entry type"
                    ):
                        restore_from_manifest(manifest, restored)
                    self.assertFalse((restored / "app").exists())
                    self.assertFalse((restored / "app" / label).exists())

    def test_restore_archive_rejects_absolute_member_and_symlink(self) -> None:
        """V-03: absolute paths and absolute symlinks must be refused."""
        cases = (
            ("absolute-member.tar.gz", "absolute", None, "absolute path"),
            (
                "absolute-symlink.tar.gz",
                "app/link",
                "/tmp/evil",
                "symlink escapes project",
            ),
        )
        for archive_name, member_name, link_target, pattern in cases:
            with self.subTest(archive=archive_name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    archive = root / archive_name
                    with tarfile.open(archive, "w:gz") as tar:
                        if link_target is None:
                            payload = b"x"
                            info = tarfile.TarInfo(member_name)
                            info.size = len(payload)
                            # Force an absolute-looking member name past TarInfo helpers.
                            info.name = "/etc/passwd"
                            tar.addfile(info, io.BytesIO(payload))
                        else:
                            info = tarfile.TarInfo(member_name)
                            info.type = tarfile.SYMTYPE
                            info.linkname = link_target
                            tar.addfile(info)
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
                    with self.assertRaisesRegex(RestoreError, pattern):
                        restore_from_manifest(manifest, restored)
                    self.assertFalse((restored / "app").exists())
                    self.assertFalse((root / "evil").exists())

    def test_restore_archive_rejects_relative_symlink_escape_and_reserved_names(
        self,
    ) -> None:
        """V-03: relative symlink escape, reserved names, trailing dot/space must be refused."""
        cases = (
            (
                "relative-symlink-escape.tar.gz",
                "app/nested/link",
                "../../evil",
                "symlink escapes project",
                "evil",
            ),
            (
                "reserved-con.tar.gz",
                "app/CON.txt",
                None,
                "unsafe archive path",
                "CON.txt",
            ),
            (
                "trailing-dot.tar.gz",
                "app/file.",
                None,
                "unsafe archive path",
                "file.",
            ),
            (
                "trailing-space.tar.gz",
                "app/file ",
                None,
                "unsafe archive path",
                "file ",
            ),
            (
                "reserved-com1.tar.gz",
                "app/COM1.txt",
                None,
                "unsafe archive path",
                "COM1.txt",
            ),
            (
                "reserved-lpt1.tar.gz",
                "app/LPT1.txt",
                None,
                "unsafe archive path",
                "LPT1.txt",
            ),
            (
                "reserved-aux.tar.gz",
                "app/AUX.txt",
                None,
                "unsafe archive path",
                "AUX.txt",
            ),
            (
                "reserved-prn.tar.gz",
                "app/PRN.txt",
                None,
                "unsafe archive path",
                "PRN.txt",
            ),
            (
                "reserved-nul.tar.gz",
                "app/NUL.txt",
                None,
                "unsafe archive path",
                "NUL.txt",
            ),
            (
                "reserved-com2.tar.gz",
                "app/COM2.txt",
                None,
                "unsafe archive path",
                "COM2.txt",
            ),
            (
                "reserved-lpt2.tar.gz",
                "app/LPT2.txt",
                None,
                "unsafe archive path",
                "LPT2.txt",
            ),
            (
                "reserved-conin.tar.gz",
                "app/CONIN$.txt",
                None,
                "unsafe archive path",
                "CONIN$.txt",
            ),
            (
                "reserved-conout.tar.gz",
                "app/CONOUT$.txt",
                None,
                "unsafe archive path",
                "CONOUT$.txt",
            ),
            (
                "reserved-com3.tar.gz",
                "app/COM3.txt",
                None,
                "unsafe archive path",
                "COM3.txt",
            ),
            (
                "reserved-lpt3.tar.gz",
                "app/LPT3.txt",
                None,
                "unsafe archive path",
                "LPT3.txt",
            ),
            (
                "reserved-com4.tar.gz",
                "app/COM4.txt",
                None,
                "unsafe archive path",
                "COM4.txt",
            ),
            (
                "reserved-lpt4.tar.gz",
                "app/LPT4.txt",
                None,
                "unsafe archive path",
                "LPT4.txt",
            ),
            *(
                case
                for n in range(5, 10)
                for case in (
                    (
                        f"reserved-com{n}.tar.gz",
                        f"app/COM{n}.txt",
                        None,
                        "unsafe archive path",
                        f"COM{n}.txt",
                    ),
                    (
                        f"reserved-lpt{n}.tar.gz",
                        f"app/LPT{n}.txt",
                        None,
                        "unsafe archive path",
                        f"LPT{n}.txt",
                    ),
                )
            ),
            *(
                case
                for digit, mark in (
                    ("1", "¹"),
                    ("2", "²"),
                    ("3", "³"),
                    ("4", "⁴"),
                    ("5", "⁵"),
                    ("6", "⁶"),
                    ("7", "⁷"),
                    ("8", "⁸"),
                    ("9", "⁹"),
                )
                for case in (
                    (
                        f"reserved-com{digit}-sup.tar.gz",
                        f"app/COM{mark}.txt",
                        None,
                        "unsafe archive path",
                        f"COM{mark}.txt",
                    ),
                    (
                        f"reserved-lpt{digit}-sup.tar.gz",
                        f"app/LPT{mark}.txt",
                        None,
                        "unsafe archive path",
                        f"LPT{mark}.txt",
                    ),
                )
            ),
            *(
                case
                for digit, mark in (
                    ("1", "１"),
                    ("2", "２"),
                    ("3", "３"),
                    ("4", "４"),
                    ("5", "５"),
                    ("6", "６"),
                    ("7", "７"),
                    ("8", "８"),
                    ("9", "９"),
                )
                for case in (
                    (
                        f"reserved-com{digit}-fw.tar.gz",
                        f"app/COM{mark}.txt",
                        None,
                        "unsafe archive path",
                        f"COM{mark}.txt",
                    ),
                    (
                        f"reserved-lpt{digit}-fw.tar.gz",
                        f"app/LPT{mark}.txt",
                        None,
                        "unsafe archive path",
                        f"LPT{mark}.txt",
                    ),
                )
            ),
            *(
                case
                for digit, mark in (
                    ("1", "①"),
                    ("2", "②"),
                    ("3", "③"),
                    ("4", "④"),
                    ("5", "⑤"),
                    ("6", "⑥"),
                    ("7", "⑦"),
                    ("8", "⑧"),
                    ("9", "⑨"),
                )
                for case in (
                    (
                        f"reserved-com{digit}-circ.tar.gz",
                        f"app/COM{mark}.txt",
                        None,
                        "unsafe archive path",
                        f"COM{mark}.txt",
                    ),
                    (
                        f"reserved-lpt{digit}-circ.tar.gz",
                        f"app/LPT{mark}.txt",
                        None,
                        "unsafe archive path",
                        f"LPT{mark}.txt",
                    ),
                )
            ),
            *(
                case
                for digit, mark in (
                    ("1", "١"),
                    ("2", "٢"),
                    ("3", "٣"),
                    ("4", "٤"),
                    ("5", "٥"),
                    ("6", "٦"),
                    ("7", "٧"),
                    ("8", "٨"),
                    ("9", "٩"),
                )
                for case in (
                    (
                        f"reserved-com{digit}-ar.tar.gz",
                        f"app/COM{mark}.txt",
                        None,
                        "unsafe archive path",
                        f"COM{mark}.txt",
                    ),
                    (
                        f"reserved-lpt{digit}-ar.tar.gz",
                        f"app/LPT{mark}.txt",
                        None,
                        "unsafe archive path",
                        f"LPT{mark}.txt",
                    ),
                )
            ),
            *(
                case
                for digit, mark in (
                    ("1", "۱"),
                    ("2", "۲"),
                    ("3", "۳"),
                    ("4", "۴"),
                    ("5", "۵"),
                    ("6", "۶"),
                    ("7", "۷"),
                    ("8", "۸"),
                    ("9", "۹"),
                )
                for case in (
                    (
                        f"reserved-com{digit}-fa.tar.gz",
                        f"app/COM{mark}.txt",
                        None,
                        "unsafe archive path",
                        f"COM{mark}.txt",
                    ),
                    (
                        f"reserved-lpt{digit}-fa.tar.gz",
                        f"app/LPT{mark}.txt",
                        None,
                        "unsafe archive path",
                        f"LPT{mark}.txt",
                    ),
                )
            ),
        )
        for archive_name, member_name, link_target, pattern, leaf in cases:
            with self.subTest(archive=archive_name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    archive = root / archive_name
                    with tarfile.open(archive, "w:gz") as tar:
                        if link_target is None:
                            payload = b"x"
                            info = tarfile.TarInfo(member_name)
                            info.size = len(payload)
                            tar.addfile(info, io.BytesIO(payload))
                        else:
                            info = tarfile.TarInfo(member_name)
                            info.type = tarfile.SYMTYPE
                            info.linkname = link_target
                            tar.addfile(info)
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
                    with self.assertRaisesRegex(RestoreError, pattern):
                        restore_from_manifest(manifest, restored)
                    self.assertFalse((restored / "app").exists())
                    self.assertFalse((root / leaf).exists())
                    self.assertFalse((restored / leaf).exists())

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
            lfs_objects = records[0].lfs_objects or []
            self.assertTrue(
                any(
                    (
                        item == object_hash
                        or (
                            isinstance(item, dict)
                            and item.get("oid") == object_hash
                            and item.get("size") == len(payload)
                        )
                    )
                    for item in lfs_objects
                ),
                lfs_objects,
            )

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
                verified = verify_manifest(manifests[0], check_remote=True)
                self.assertEqual(verified[0]["status"], "remote-verified")
                self.assertEqual(verified[0]["lfs"][0]["status"], "lfs-verified")
                _, results = restore_from_manifest(
                    manifests[0], restored_dest, check_remote=True
                )

            self.assertEqual(results[0].strategy, "git")
            self.assertEqual(
                (restored_dest / "lfs-app" / "payload.bin").read_bytes(), payload
            )

            progress_dest = root / "restored-progress"
            stderr = io.StringIO()
            with (
                mock.patch.dict(os.environ, git_config),
                redirect_stderr(stderr),
            ):
                _, progress_results = restore_from_manifest(
                    manifests[0], progress_dest, progress=True
                )
            self.assertEqual(progress_results[0].strategy, "git")
            err = stderr.getvalue()
            self.assertIn("progress: lfs-app: pulling LFS objects", err)
            self.assertRegex(err, r"progress: lfs-app: LFS pull done \(\d+s\)")
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
                unavailable = verify_manifest(manifests[0], check_remote=True)
                self.assertEqual(unavailable[0]["status"], "lfs-unavailable")
                self.assertEqual(unavailable[0]["lfs"][0]["status"], "lfs-missing")
                with self.assertRaises(RestoreError) as raised:
                    restore_from_manifest(
                        manifests[0], failed_dest, check_remote=True
                    )
                self.assertIn("LFS object", str(raised.exception))
                self.assertFalse((failed_dest / "lfs-app").exists())
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

    def test_restore_git_checkout_branch_creates_named_branch(self) -> None:
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
                        "strategy": "git",
                        "remote_url": str(remote),
                        "branch": "main",
                        "commit": head,
                    }
                ],
            )
            restored = root / "restored"
            _, _ = restore_from_manifest(manifest, restored, checkout_branch=True)
            target = restored / "app"
            self.assertEqual(git(target, "rev-parse", "HEAD"), head)
            self.assertEqual(
                git(target, "rev-parse", "--abbrev-ref", "HEAD"),
                "main",
            )

    def test_restore_git_checkout_branch_warns_without_branch(self) -> None:
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
                        "strategy": "git",
                        "remote_url": str(remote),
                        "commit": head,
                    }
                ],
            )
            restored = root / "restored"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                _, _ = restore_from_manifest(
                    manifest, restored, checkout_branch=True
                )
            err = stderr.getvalue()
            self.assertIn("--checkout-branch requested", err)
            self.assertIn("no branch", err)
            target = restored / "app"
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


class RestoreHelperTests(unittest.TestCase):
    def test_validate_remote_url_rejects_unsafe_forms(self) -> None:
        cases = (
            ("https://exa mple.com/repo", "contains whitespace"),
            ("-oProxyCommand=calc", "must not start with '-'"),
            ("https:///repo", "has no host"),
            ("ssh:///repo", "has no host"),
            ("file://user:pass@localhost/repo", "must not contain credentials"),
            ("file:///repo?query=1", "must not contain credentials"),
            ("https://[::1", "invalid remote URL"),
            ("git://example.com/repo", "unsupported remote URL"),
        )
        for remote, message in cases:
            with self.subTest(remote=remote), self.assertRaisesRegex(
                RestoreError, message
            ):
                _validate_remote_url(remote)

    def test_validate_remote_url_accepts_supported_forms(self) -> None:
        for remote in (
            "https://github.com/acme/app",
            "ssh://git@github.com/acme/app",
            "file:///tmp/repo",
            "git@github.com:acme/app.git",
            str(Path(tempfile.gettempdir()) / "repo"),
        ):
            with self.subTest(remote=remote):
                _validate_remote_url(remote)

    def test_git_args_detect_sensitive_remote(self) -> None:
        self.assertFalse(
            _git_args_have_sensitive_remote(["https://github.com/acme/app"])
        )
        self.assertFalse(_git_args_have_sensitive_remote(["--depth", "1"]))
        for value in (
            "https://token@github.com/acme/app",
            "https://github.com/acme/app?ref=main",
            "git@github.com:acme/app.git",
            "https://[::1",
        ):
            with self.subTest(value=value):
                self.assertTrue(_git_args_have_sensitive_remote([value]))
        self.assertTrue(
            _git_args_have_sensitive_remote(
                [], "https://token@github.com/acme/app"
            )
        )

    def test_load_manifest_rejects_invalid_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(RestoreError, "does not exist"):
                load_manifest(root / "missing.json")
            with self.assertRaisesRegex(RestoreError, "is not a file"):
                load_manifest(root)

            invalid = root / "invalid.json"
            invalid.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(RestoreError, "cannot read manifest"):
                load_manifest(invalid)

            array = root / "array.json"
            array.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(RestoreError, "must be a JSON object"):
                load_manifest(array)

            schema = root / "schema.json"
            schema.write_text(
                json.dumps({"schema_version": 1, "records": [{}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RestoreError, "schema_version"):
                load_manifest(schema)

            empty = root / "empty.json"
            empty.write_text(
                json.dumps({"schema_version": 2, "records": []}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RestoreError, "non-empty array"):
                load_manifest(empty)

            bad_record = root / "bad-record.json"
            bad_record.write_text(
                json.dumps({"schema_version": 2, "records": ["x"]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RestoreError, "must be JSON objects"):
                load_manifest(bad_record)

    def test_rewrite_remote_url_uses_longest_prefix(self) -> None:
        result = mock.Mock(
            returncode=0,
            stdout=(
                "url.https://mirror.example/.insteadof https://github.com/\n"
                "url.https://fallback.example/.insteadof https://\n"
                "garbage-line\n"
                "url.other.insteadof https://github.com/\n"
            ),
        )
        with mock.patch.object(restore_module, "_run_git", return_value=result):
            self.assertEqual(
                _rewrite_remote_url("https://github.com/acme/app.git"),
                "https://mirror.example/acme/app.git",
            )

    def test_rewrite_remote_url_falls_back(self) -> None:
        url = "https://github.com/acme/app.git"
        with mock.patch.object(
            restore_module,
            "_run_git",
            side_effect=RestoreError("git unavailable"),
        ):
            self.assertEqual(_rewrite_remote_url(url), url)
        with mock.patch.object(
            restore_module,
            "_run_git",
            return_value=mock.Mock(returncode=1, stdout=""),
        ):
            self.assertEqual(_rewrite_remote_url(url), url)


class RestorePlanValidationTests(unittest.TestCase):
    commit = "a" * 40

    def _manifest(self, root: Path, records: list[dict]) -> Path:
        path = root / "manifest.json"
        path.write_text(
            json.dumps({"schema_version": 2, "records": records}),
            encoding="utf-8",
        )
        return path

    def _git(self, project: str = "app", commit: str | None = None) -> dict:
        return {
            "project": project,
            "strategy": "git",
            "remote_url": "https://github.com/acme/app.git",
            "commit": commit or self.commit,
            "branch": "main",
        }

    def test_plan_restore_rejects_bad_destination_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "dest"
            dest.write_text("x", encoding="utf-8")
            manifest = self._manifest(root, [self._git()])
            with self.assertRaisesRegex(RestoreError, "not a directory"):
                plan_restore(manifest, dest)

            dest.unlink()
            dest.mkdir()
            duplicate = self._manifest(
                root, [self._git(), self._git(commit="b" * 40)]
            )
            with self.assertRaisesRegex(RestoreError, "duplicate project"):
                plan_restore(duplicate, dest)

            colliding = self._manifest(
                root,
                [self._git("App"), self._git("app", "b" * 40)],
            )
            with self.assertRaisesRegex(
                RestoreError,
                "duplicate restore target|collide on case-insensitive",
            ):
                plan_restore(colliding, dest)

    def test_plan_restore_rejects_invalid_git_and_bundle_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "dest"
            dest.mkdir()
            manifest = self._manifest(
                root, [self._git(commit="short")]
            )
            with self.assertRaisesRegex(RestoreError, "invalid commit"):
                plan_restore(manifest, dest)

            bundle = root / "app.bundle"
            bundle.write_bytes(b"bundle")
            missing_sha = self._manifest(
                root,
                [
                    {
                        "project": "app",
                        "strategy": "git_bundle",
                        "archive": str(bundle),
                        "commit": self.commit,
                        "branch": "main",
                    }
                ],
            )
            with self.assertRaisesRegex(RestoreError, "no sha256"):
                plan_restore(missing_sha, dest)

            bad_commit = self._manifest(
                root,
                [
                    {
                        "project": "app",
                        "strategy": "git_bundle",
                        "archive": str(bundle),
                        "commit": "abc",
                        "branch": "main",
                        "sha256": "0" * 64,
                    }
                ],
            )
            with self.assertRaisesRegex(RestoreError, "invalid commit"):
                plan_restore(bad_commit, dest)

    def test_plan_restore_rejects_bad_archives_and_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "dest"
            dest.mkdir()
            archive = root / "app.tar.gz"
            archive.write_bytes(b"archive")

            bad_sha = self._manifest(
                root,
                [
                    {
                        "project": "app",
                        "strategy": "archive",
                        "archive": str(archive),
                        "sha256": 1,
                    }
                ],
            )
            with self.assertRaisesRegex(RestoreError, "invalid sha256"):
                plan_restore(bad_sha, dest)

            missing_sha = self._manifest(
                root,
                [
                    {
                        "project": "app",
                        "strategy": "archive",
                        "archive": str(archive),
                    }
                ],
            )
            with self.assertRaisesRegex(RestoreError, "--no-verify"):
                plan_restore(missing_sha, dest)

            unsupported = self._manifest(
                root, [{"project": "app", "strategy": "rsync"}]
            )
            with self.assertRaisesRegex(
                RestoreError, "unsupported backup strategy"
            ):
                plan_restore(unsupported, dest)

    def test_verify_manifest_rejects_bad_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            duplicate = self._manifest(
                root, [self._git(), self._git(commit="b" * 40)]
            )
            with self.assertRaisesRegex(RestoreError, "duplicate project"):
                verify_manifest(duplicate)

            archive = root / "app.tar.gz"
            archive.write_bytes(b"archive")
            missing_sha = self._manifest(
                root,
                [
                    {
                        "project": "app",
                        "strategy": "archive",
                        "archive": str(archive),
                    }
                ],
            )
            with self.assertRaisesRegex(RestoreError, "no sha256"):
                verify_manifest(missing_sha)

            invalid = self._manifest(
                root, [self._git(commit="short")]
            )
            with self.assertRaisesRegex(RestoreError, "invalid commit"):
                verify_manifest(invalid)

    def test_git_bundle_restore_guard_clauses(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incomplete = RestorePlan(
                project="app",
                strategy="git_bundle",
                target=root / "app",
            )
            with self.assertRaisesRegex(RestoreError, "incomplete"):
                _restore_git_bundle(incomplete, verify=True)

            target = root / "app"
            target.mkdir()
            bundle = root / "app.bundle"
            bundle.write_bytes(b"bundle")
            existing = RestorePlan(
                project="app",
                strategy="git_bundle",
                target=target,
                archive=bundle,
                commit=self.commit,
                branch="main",
            )
            with self.assertRaisesRegex(RestoreError, "already exists"):
                _restore_git_bundle(existing, verify=True)

            encrypted = RestorePlan(
                project="app",
                strategy="git_bundle",
                target=root / "other",
                archive=bundle,
                commit=self.commit,
                branch="main",
                encryption="openssl-aes-256-cbc-pbkdf2",
            )
            with self.assertRaisesRegex(RestoreError, "decrypt-passphrase-file"):
                _restore_git_bundle(encrypted, verify=True)


class IncrementalDeltaTests(unittest.TestCase):
    def _archive(self, root: Path, entries, name: str = "delta.tar.gz") -> Path:
        path = root / name
        with tarfile.open(path, "w:gz") as tar:
            for entry in entries:
                info = tarfile.TarInfo(entry["name"])
                if entry.get("type") is not None:
                    info.type = entry["type"]
                if entry.get("linkname") is not None:
                    info.linkname = entry["linkname"]
                if entry.get("type") in (
                    tarfile.DIRTYPE,
                    tarfile.SYMTYPE,
                    tarfile.LNKTYPE,
                    tarfile.FIFOTYPE,
                ):
                    tar.addfile(info)
                    continue
                payload = entry.get("data", b"x")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        return path

    def test_extract_delta_rejects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = self._archive(root, [{"name": "app/a.txt", "data": b"a"}])
            with self.assertRaisesRegex(RestoreError, "sha256 mismatch"):
                _extract_incremental_delta(
                    archive, root / "out", "app", "0" * 64
                )

    def test_extract_delta_rejects_outside_project_and_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = self._archive(
                root, [{"name": "other/a.txt", "data": b"a"}], "outside.tar.gz"
            )
            with self.assertRaisesRegex(RestoreError, "outside project"):
                _extract_incremental_delta(outside, root / "out", "app", None)

            unsafe = self._archive(
                root,
                [{"name": "app/../escape.txt", "data": b"a"}],
                "unsafe.tar.gz",
            )
            with self.assertRaises(RestoreError):
                _extract_incremental_delta(unsafe, root / "out", "app", None)

    def test_extract_delta_rejects_duplicates_and_case_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            duplicate = self._archive(
                root,
                [
                    {"name": "app/a.txt", "data": b"a"},
                    {"name": "app/a.txt", "data": b"b"},
                ],
                "duplicate.tar.gz",
            )
            with self.assertRaisesRegex(RestoreError, "duplicate path"):
                _extract_incremental_delta(duplicate, root / "out", "app", None)

            collision = self._archive(
                root,
                [
                    {"name": "app/File.txt", "data": b"a"},
                    {"name": "app/file.txt", "data": b"b"},
                ],
                "collision.tar.gz",
            )
            with self.assertRaisesRegex(RestoreError, "collide on case-insensitive"):
                _extract_incremental_delta(collision, root / "out", "app", None)

    def test_extract_delta_rejects_links_and_special_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hardlink = self._archive(
                root,
                [
                    {
                        "name": "app/link.txt",
                        "type": tarfile.LNKTYPE,
                        "linkname": "app/a.txt",
                    }
                ],
                "hardlink.tar.gz",
            )
            with self.assertRaisesRegex(RestoreError, "hard link"):
                _extract_incremental_delta(hardlink, root / "out", "app", None)

            symlink = self._archive(
                root,
                [
                    {
                        "name": "app/link",
                        "type": tarfile.SYMTYPE,
                        "linkname": "../../escape",
                    }
                ],
                "symlink.tar.gz",
            )
            with self.assertRaisesRegex(RestoreError, "symlink escapes"):
                _extract_incremental_delta(symlink, root / "out", "app", None)

            fifo = self._archive(
                root,
                [{"name": "app/pipe", "type": tarfile.FIFOTYPE}],
                "fifo.tar.gz",
            )
            with self.assertRaisesRegex(RestoreError, "unsupported entry type"):
                _extract_incremental_delta(fifo, root / "out", "app", None)

    def test_extract_delta_writes_files_and_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = self._archive(
                root,
                [
                    {"name": "app/sub", "type": tarfile.DIRTYPE},
                    {"name": "app/sub/file.txt", "data": b"payload"},
                ],
            )
            target = root / "out"
            _extract_incremental_delta(archive, target, "app", None)
            self.assertEqual(
                (target / "sub" / "file.txt").read_bytes(), b"payload"
            )

    def test_incremental_restore_guard_clauses(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incomplete = RestorePlan(
                project="app",
                strategy="archive",
                target=root / "out",
                archive=None,
            )
            with self.assertRaisesRegex(RestoreError, "incomplete incremental"):
                _restore_incremental(incomplete, verify=False)

            archive = root / "delta.tar.gz"
            archive.write_bytes(b"x")
            no_base = RestorePlan(
                project="app",
                strategy="archive",
                target=root / "out",
                archive=archive,
                incremental={"base": ""},
            )
            with self.assertRaisesRegex(RestoreError, "no base manifest"):
                _restore_incremental(no_base, verify=False)

            missing = RestorePlan(
                project="app",
                strategy="archive",
                target=root / "out",
                archive=archive,
                incremental={"base": "missing.json"},
            )
            with self.assertRaisesRegex(RestoreError, "base manifest not found"):
                _restore_incremental(missing, verify=False)


class ExecuteRestoreGuardTests(unittest.TestCase):
    commit = "a" * 40
    sub_commit = "b" * 40

    def _plan(self, root: Path) -> "RestorePlan":
        from proj_janitor.models import RestorePlan

        return RestorePlan(
            project="app",
            strategy="git",
            target=root / "app",
            remote_url="https://github.com/acme/app.git",
            commit=self.commit,
            submodules=[
                {
                    "path": "sub",
                    "url": "https://github.com/acme/sub.git",
                    "commit": self.sub_commit,
                }
            ],
        )

    def _probe(self, sub_status: str, sub_reason: str):
        def probe(url: str, commit: str):
            if "sub" in url:
                return sub_status, sub_reason, []
            return "remote-verified", None, ["refs/heads/main"]

        return probe

    def test_incomplete_archive_plan_is_rejected(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            plan = RestorePlan(
                project="app",
                strategy="archive",
                target=Path(tmp) / "app",
                archive=None,
            )
            with self.assertRaisesRegex(RestoreError, "incomplete archive"):
                execute_restore([plan], verify=True)

    def test_submodule_remote_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self._plan(root)
            with mock.patch.object(
                restore_module,
                "_probe_remote",
                side_effect=self._probe("remote-moved", "no-ref"),
            ):
                with self.assertRaisesRegex(RestoreError, "no ref matching"):
                    execute_restore(
                        [plan],
                        verify=False,
                        check_remote=True,
                        fail_on_remote_moved=True,
                    )

            with mock.patch.object(
                restore_module,
                "_probe_remote",
                side_effect=self._probe("remote-skipped", "credentials"),
            ):
                with self.assertRaisesRegex(RestoreError, "was not checked"):
                    execute_restore(
                        [plan],
                        verify=False,
                        check_remote=True,
                        fail_on_remote_skipped=True,
                    )

            with mock.patch.object(
                restore_module,
                "_probe_remote",
                side_effect=self._probe("remote-unreachable", "timeout"),
            ):
                with self.assertRaisesRegex(RestoreError, "is unreachable"):
                    execute_restore(
                        [plan],
                        verify=False,
                        check_remote=True,
                    )

    def test_missing_lfs_object_blocks_restore(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = RestorePlan(
                project="app",
                strategy="git",
                target=root / "app",
                remote_url="https://github.com/acme/app.git",
                commit=self.commit,
                lfs_objects=[{"oid": "c" * 40, "size": 10}],
            )
            with (
                mock.patch.object(
                    restore_module,
                    "_probe_remote",
                    return_value=(
                        "remote-verified",
                        None,
                        ["refs/heads/main"],
                    ),
                ),
                mock.patch.object(
                    restore_module,
                    "_probe_lfs_objects",
                    return_value=[
                        {
                            "oid": "c" * 40,
                            "status": "lfs-missing",
                            "reason": "object-not-found",
                        }
                    ],
                ),
                self.assertRaisesRegex(RestoreError, "LFS object"),
            ):
                execute_restore([plan], verify=False, check_remote=True)

    def test_uncreatable_target_parent_is_rejected(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocked = root / "blocked"
            blocked.write_text("x", encoding="utf-8")
            plan = RestorePlan(
                project="app",
                strategy="git",
                target=blocked / "app",
                remote_url="https://github.com/acme/app.git",
                commit=self.commit,
            )
            with self.assertRaisesRegex(RestoreError, "cannot restore app"):
                execute_restore([plan], verify=False)


class PublishRestoredTreeTests(unittest.TestCase):
    def test_existing_file_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / "staged"
            staged.mkdir()
            (staged / "file.txt").write_text("x", encoding="utf-8")
            target = root / "target"
            target.write_text("file", encoding="utf-8")
            with self.assertRaisesRegex(
                RestoreError, "not a directory"
            ):
                _publish_restored_tree(staged, target, force=True)

    def test_existing_directory_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / "staged"
            staged.mkdir()
            target = root / "target"
            target.mkdir()
            with self.assertRaisesRegex(
                RestoreError, "already exists"
            ):
                _publish_restored_tree(staged, target, force=False)


class GitBundleExecutionTests(unittest.TestCase):
    def _bundle(self, root: Path):
        from proj_janitor.models import RestorePlan

        repo = root / "repo"
        repo.mkdir()
        init_repo(repo)
        (repo / "file.txt").write_text("bundle payload", encoding="utf-8")
        commit_all(repo)
        bundle = root / "app.bundle"
        git(repo, "bundle", "create", str(bundle), "--all")
        commit = git(repo, "rev-parse", "HEAD")
        plan = RestorePlan(
            project="app",
            strategy="git_bundle",
            target=root / "out" / "app",
            archive=bundle,
            commit=commit,
            branch="main",
            sha256=sha256_file(bundle),
        )
        return plan

    def test_restores_bundle_and_rejects_invalid_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self._bundle(root)
            _restore_git_bundle(plan, verify=True)
            self.assertEqual(
                (plan.target / "file.txt").read_text(encoding="utf-8"),
                "bundle payload",
            )

            invalid = replace(
                plan,
                target=root / "bad" / "app",
                branch="bad..name",
            )
            with self.assertRaisesRegex(RestoreError, "invalid branch"):
                _restore_git_bundle(invalid, verify=True)

    def test_commit_mismatch_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self._bundle(root)
            original = restore_module._run_git

            def fake_run(args, **kwargs):
                result = original(args, **kwargs)
                if (
                    "rev-parse" in args
                    and args[-1] == "HEAD"
                    and result.returncode == 0
                ):
                    result.stdout = "f" * 40 + "\n"
                return result

            with mock.patch.object(
                restore_module, "_run_git", side_effect=fake_run
            ):
                with self.assertRaisesRegex(RestoreError, "commit mismatch"):
                    _restore_git_bundle(plan, verify=True)

    def test_rollback_after_partial_restore(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "a.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("a/file.txt")
                payload = b"restored"
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))

            target_a = root / "out" / "a"
            target_b = root / "out" / "b"
            plans = [
                RestorePlan(
                    project="a",
                    strategy="archive",
                    target=target_a,
                    archive=archive,
                ),
                RestorePlan(
                    project="b",
                    strategy="archive",
                    target=target_b,
                    archive=root / "missing.tar.gz",
                ),
            ]
            with self.assertRaisesRegex(RestoreError, "rolled back"):
                execute_restore(
                    plans,
                    verify=False,
                    rollback_on_error=True,
                )
            self.assertFalse(target_a.exists())


class IncrementalDeletionTests(unittest.TestCase):
    def test_removes_files_directories_and_ignores_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            (target / "directory").mkdir()
            (target / "directory" / "nested.txt").write_text("x", encoding="utf-8")
            (target / "file.txt").write_text("x", encoding="utf-8")

            _remove_deleted_paths(
                target,
                ["directory", "file.txt", "missing.txt"],
            )

            self.assertFalse((target / "directory").exists())
            self.assertFalse((target / "file.txt").exists())

    def test_rejects_unsafe_deleted_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            for raw in ("../escape", "/absolute", "a\x00b", ".", ""):
                with self.subTest(raw=raw), self.assertRaisesRegex(
                    RestoreError, "unsafe deleted path"
                ):
                    _remove_deleted_paths(target, [raw])

    @unittest.skipIf(os.name == "nt", "symlink creation needs extra privileges")
    def test_symlink_is_unlinked_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep", encoding="utf-8")
            (target / "link").symlink_to(outside, target_is_directory=True)

            _remove_deleted_paths(target, ["link"])

            self.assertFalse((target / "link").exists())
            self.assertEqual(
                (outside / "keep.txt").read_text(encoding="utf-8"),
                "keep",
            )


class RestoreWriteFailureTests(unittest.TestCase):
    def test_archive_extraction_disk_full_leaves_no_target(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "app.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("app/file.txt")
                payload = b"payload"
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            target = root / "out" / "app"
            plan = RestorePlan(
                project="app",
                strategy="archive",
                target=target,
                archive=archive,
            )
            with mock.patch.object(
                restore_module.tarfile.TarFile,
                "extractall",
                side_effect=OSError("No space left on device"),
            ):
                with self.assertRaisesRegex(
                    RestoreError, "cannot restore app"
                ):
                    execute_restore([plan], verify=False)
            self.assertFalse(target.exists())
            self.assertFalse(list(target.parent.glob(".app-restore-*")))

    def test_bundle_copy_disk_full_leaves_no_target(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_repo(repo)
            (repo / "file.txt").write_text("payload", encoding="utf-8")
            commit_all(repo)
            bundle = root / "app.bundle"
            git(repo, "bundle", "create", str(bundle), "--all")
            commit = git(repo, "rev-parse", "HEAD")
            target = root / "out" / "app"
            plan = RestorePlan(
                project="app",
                strategy="git_bundle",
                target=target,
                archive=bundle,
                commit=commit,
                branch="main",
                sha256=sha256_file(bundle),
            )
            with mock.patch.object(
                restore_module.shutil,
                "copyfileobj",
                side_effect=OSError("No space left on device"),
            ):
                with self.assertRaisesRegex(
                    RestoreError, "cannot restore app"
                ):
                    execute_restore([plan], verify=False)
            self.assertFalse(target.exists())
            self.assertFalse(list(target.parent.glob(".app-restore-*")))


class RestoreSpaceCheckTests(unittest.TestCase):
    def test_check_space_refuses_when_archive_exceeds_free_space(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "app.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("app/file.txt")
                payload = b"0123456789"
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            target = root / "out" / "app"
            plan = RestorePlan(
                project="app",
                strategy="archive",
                target=target,
                archive=archive,
            )
            usage = collections.namedtuple("usage", "total used free")
            with mock.patch.object(
                restore_module.shutil,
                "disk_usage",
                return_value=usage(0, 0, 5),
            ):
                with self.assertRaisesRegex(
                    RestoreError, "not enough free space"
                ):
                    execute_restore([plan], verify=False, check_space=True)
            self.assertFalse(target.exists())

            with mock.patch.object(
                restore_module.shutil,
                "disk_usage",
                return_value=usage(0, 0, 1024 * 1024),
            ):
                execute_restore([plan], verify=False, check_space=True)
            self.assertEqual(
                (target / "file.txt").read_text(encoding="utf-8"),
                "0123456789",
            )

    def test_check_space_is_optional(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "app.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("app/file.txt")
                payload = b"payload"
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            plan = RestorePlan(
                project="app",
                strategy="archive",
                target=root / "out" / "app",
                archive=archive,
            )
            usage = collections.namedtuple("usage", "total used free")
            with mock.patch.object(
                restore_module.shutil,
                "disk_usage",
                return_value=usage(0, 0, 1),
            ):
                execute_restore([plan], verify=False)
            self.assertTrue(plan.target.exists())

    def test_inventory_covers_encrypted_archive(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = RestorePlan(
                project="app",
                strategy="archive",
                target=root / "out" / "app",
                archive=root / "app.enc",
                encryption="openssl-aes-256-cbc-pbkdf2",
                inventory=[{"path": "app/file", "sha256": "x", "size": 100}],
            )
            usage = collections.namedtuple("usage", "total used free")
            with mock.patch.object(
                restore_module.shutil,
                "disk_usage",
                return_value=usage(0, 0, 50),
            ):
                with self.assertRaisesRegex(
                    RestoreError, "not enough free space"
                ):
                    _check_restore_space([plan])

    def test_skips_encrypted_without_inventory_and_incremental(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            encrypted = RestorePlan(
                project="enc",
                strategy="archive",
                target=root / "out" / "enc",
                archive=root / "enc.bin",
                encryption="openssl-aes-256-cbc-pbkdf2",
            )
            incremental = RestorePlan(
                project="inc",
                strategy="archive",
                target=root / "out" / "inc",
                archive=root / "inc.tar.gz",
                incremental={"base": "previous.json", "deleted": []},
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                _check_restore_space([encrypted, incremental])
            output = stderr.getvalue()
            self.assertIn("skipped encrypted archive", output)
            self.assertIn("skipped incremental archive", output)

    def test_source_bytes_covers_incremental_and_encrypted(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans = [
                RestorePlan(
                    project="inc",
                    strategy="archive",
                    target=root / "out" / "inc",
                    archive=root / "inc.tar.gz",
                    source_bytes=100,
                    incremental={"base": "previous.json", "deleted": []},
                ),
                RestorePlan(
                    project="enc",
                    strategy="archive",
                    target=root / "out" / "enc",
                    archive=root / "enc.bin",
                    source_bytes=100,
                    encryption="openssl-aes-256-cbc-pbkdf2",
                ),
            ]
            usage = collections.namedtuple("usage", "total used free")
            with mock.patch.object(
                restore_module.shutil,
                "disk_usage",
                return_value=usage(0, 0, 50),
            ):
                for plan in plans:
                    with self.subTest(project=plan.project):
                        with self.assertRaisesRegex(
                            RestoreError, "not enough free space"
                        ):
                            _check_restore_space([plan])

    def test_combines_archives_sharing_a_parent(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans = [
                RestorePlan(
                    project=name,
                    strategy="archive",
                    target=root / "out" / name,
                    archive=root / f"{name}.tar.gz",
                    inventory=[{"path": f"app/{name}", "sha256": "x", "size": 60}],
                )
                for name in ("a", "b")
            ]
            usage = collections.namedtuple("usage", "total used free")
            with mock.patch.object(
                restore_module.shutil,
                "disk_usage",
                return_value=usage(0, 0, 100),
            ):
                with self.assertRaisesRegex(
                    RestoreError, "not enough free space"
                ):
                    _check_restore_space(plans)

    def test_incremental_inventory_wins_over_source_bytes(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = RestorePlan(
                project="inc",
                strategy="archive",
                target=root / "out" / "inc",
                archive=root / "inc.tar.gz",
                source_bytes=1000,
                incremental={"base": "previous.json", "deleted": []},
                inventory=[{"path": "app/a", "sha256": "x", "size": 60}],
            )
            usage = collections.namedtuple("usage", "total used free")
            with mock.patch.object(
                restore_module.shutil,
                "disk_usage",
                return_value=usage(0, 0, 100),
            ):
                _check_restore_space([plan])
            with mock.patch.object(
                restore_module.shutil,
                "disk_usage",
                return_value=usage(0, 0, 50),
            ):
                with self.assertRaisesRegex(
                    RestoreError, "not enough free space"
                ):
                    _check_restore_space([plan])

    def test_warns_when_git_plans_are_skipped(self) -> None:
        from proj_janitor.models import RestorePlan

        with tempfile.TemporaryDirectory() as tmp:
            plan = RestorePlan(
                project="git-only",
                strategy="git",
                target=Path(tmp) / "out" / "git-only",
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                _check_restore_space([plan])
            self.assertIn("only estimates archive restores", stderr.getvalue())
            self.assertIn("skipped 1 git/git_bundle plan", stderr.getvalue())


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class EncryptedVerifyTests(unittest.TestCase):
    def _encrypted_backup(self, root: Path):
        project = root / "app"
        project.mkdir()
        (project / "pyproject.toml").write_text("", encoding="utf-8")
        (project / "main.py").write_text("print('hi')\n", encoding="utf-8")
        passphrase = root / "pass.txt"
        passphrase.write_text("secret\n", encoding="utf-8")
        os.chmod(passphrase, 0o600)
        manifests, records, failures = run_backup(
            [scan_project(project)],
            root / "backups",
            always_archive=True,
            encrypt_passphrase_file=passphrase,
        )
        self.assertFalse(failures)
        self.assertEqual(len(manifests), 1)
        return manifests[0], passphrase

    def test_verify_encrypted_warns_without_passphrase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _ = self._encrypted_backup(root)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                results = verify_manifest(manifest)
            self.assertIn(
                "encrypted archive verified by ciphertext only",
                stderr.getvalue(),
            )
            self.assertEqual(results[0]["status"], "verified")

    def test_verify_encrypted_checks_inner_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, passphrase = self._encrypted_backup(root)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                results = verify_manifest(
                    manifest, decrypt_passphrase_file=passphrase
                )
            self.assertNotIn(
                "encrypted archive verified by ciphertext only",
                stderr.getvalue(),
            )
            self.assertEqual(results[0]["status"], "verified")

    def test_verify_encrypted_wrong_passphrase_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _ = self._encrypted_backup(root)
            wrong = root / "wrong.txt"
            wrong.write_text("not-the-passphrase\n", encoding="utf-8")
            os.chmod(wrong, 0o600)
            with self.assertRaises(RestoreError):
                verify_manifest(manifest, decrypt_passphrase_file=wrong)


if __name__ == "__main__":
    unittest.main()


class LfsBatchProbeTests(unittest.TestCase):
    class _Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def read(self) -> bytes:
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def _probe(self, payload: bytes, objects):
        with mock.patch.object(
            restore_module, "urlopen", return_value=self._Response(payload)
        ) as urlopen:
            checks = _probe_lfs_batch("https://example.com/acme/app", objects)
        return checks, urlopen

    def test_oid_only_is_skipped_without_network(self) -> None:
        with mock.patch.object(restore_module, "urlopen") as urlopen:
            checks = _probe_lfs_batch(
                "https://example.com/acme/app", [("a" * 64, None)]
            )
        urlopen.assert_not_called()
        self.assertEqual(
            checks,
            [
                {
                    "oid": "a" * 64,
                    "size": None,
                    "status": "lfs-skipped",
                    "reason": "oid-only-no-size",
                }
            ],
        )

    def test_batch_results_precede_skipped_entries(self) -> None:
        payload = json.dumps(
            {
                "objects": [
                    {"oid": "o1", "size": 10, "actions": {"download": {"href": "x"}}},
                    {"oid": "o2", "size": 20, "error": {"code": 404}},
                    {"oid": "o3", "size": 30},
                ]
            }
        ).encode("utf-8")
        checks, urlopen = self._probe(
            payload,
            [("o1", 10), ("o2", 20), ("o3", 30), ("o4", 40), ("o5", None)],
        )
        urlopen.assert_called_once()
        self.assertEqual(
            [(check["oid"], check["status"], check["reason"]) for check in checks],
            [
                ("o1", "lfs-verified", None),
                ("o2", "lfs-missing", "object-not-found"),
                ("o3", "lfs-missing", "no-download-action"),
                ("o4", "lfs-unreachable", "lfs-batch-incomplete"),
                ("o5", "lfs-skipped", "oid-only-no-size"),
            ],
        )

    def test_http_errors_map_to_lfs_statuses(self) -> None:
        objects = [("o1", 10), ("o2", 20), ("o3", None)]
        cases = (
            (404, "lfs-missing", "lfs-batch-not-found"),
            (401, "lfs-unreachable", "lfs-auth-required"),
            (403, "lfs-unreachable", "lfs-auth-required"),
            (500, "lfs-unreachable", "lfs-batch-failed"),
        )
        for code, status, reason in cases:
            with self.subTest(code=code):
                error = HTTPError(
                    "https://example.com/acme/app/info/lfs/objects/batch",
                    code,
                    "failed",
                    None,
                    None,
                )
                with mock.patch.object(restore_module, "urlopen", side_effect=error):
                    checks = _probe_lfs_batch(
                        "https://example.com/acme/app", objects
                    )
                self.assertEqual(
                    [(check["oid"], check["status"], check["reason"]) for check in checks],
                    [
                        ("o1", status, reason),
                        ("o2", status, reason),
                        ("o3", "lfs-skipped", "oid-only-no-size"),
                    ],
                )

    def test_network_and_invalid_json_map_to_failed_or_invalid(self) -> None:
        with mock.patch.object(
            restore_module, "urlopen", side_effect=URLError("offline")
        ):
            checks = _probe_lfs_batch(
                "https://example.com/acme/app", [("o1", 10)]
            )
        self.assertEqual(checks[0]["reason"], "lfs-batch-failed")

        checks, _ = self._probe(b"{", [("o1", 10)])
        self.assertEqual(checks[0]["reason"], "lfs-batch-invalid-json")

        with mock.patch.object(
            restore_module,
            "urlopen",
            return_value=self._Response(b"\xff"),
        ):
            checks = _probe_lfs_batch(
                "https://example.com/acme/app", [("o1", 10)]
            )
        self.assertEqual(checks[0]["reason"], "lfs-batch-invalid-json")


if __name__ == "__main__":
    unittest.main()
