from __future__ import annotations

import io
import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from proj_janitor.backup import run_backup, sha256_file
from proj_janitor.restore import RestoreError, restore_from_manifest
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
            self.assertEqual(results[0].target, target)
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
            _, manifest, record = archive_backup(root)
            archive = Path(record["archive"])
            with archive.open("ab") as handle:
                handle.write(b"corruption")
            restored = root / "restored"

            with self.assertRaisesRegex(RestoreError, "sha256 mismatch"):
                restore_from_manifest(manifest, restored)

            self.assertFalse((restored / "app").exists())

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
                        "source": f"/original/{name}",
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
                    RestoreError, "case-insensitive filesystems"
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

    def test_restore_archive_to_original_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, _ = archive_backup(root)
            original = project.resolve()
            shutil.rmtree(project)

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
            shutil.rmtree(project)

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
            shutil.rmtree(project)

            with self.assertRaisesRegex(RestoreError, "has no source"):
                restore_from_manifest(manifest, None)

    def test_restore_original_rejects_relative_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, _ = archive_backup(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["records"][0]["source"] = "app"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            shutil.rmtree(project)

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
            shutil.rmtree(project)

            with self.assertRaisesRegex(RestoreError, "unsafe source path"):
                restore_from_manifest(manifest, None)

    def test_restore_original_rejects_source_name_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, manifest, _ = archive_backup(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["records"][0]["source"] = str(root / "other")
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            shutil.rmtree(project)

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
            shutil.rmtree(project)
            original.symlink_to(root / "elsewhere", target_is_directory=True)

            with self.assertRaisesRegex(RestoreError, "symlink"):
                restore_from_manifest(manifest, None)


@unittest.skipUnless(GIT_AVAILABLE, "git is not available")
class GitRestoreTests(unittest.TestCase):
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
            shutil.rmtree(project)

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


if __name__ == "__main__":
    unittest.main()
