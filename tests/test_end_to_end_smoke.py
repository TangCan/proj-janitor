"""End-to-end backup/verify/restore smoke test on a lifelike project (CR-212)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from proj_janitor.backup import run_backup
from proj_janitor.restore import (
    RestoreError,
    restore_from_manifest,
    verify_manifest,
)
from proj_janitor.scanner import scan_project
from tests.support import commit_all, git, init_repo


def make_sample_project(root: Path) -> Path:
    project = root / "sample" / "app"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text(
        "print('hello')\n", encoding="utf-8"
    )
    (project / "src" / "util.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    assets = project / "assets"
    assets.mkdir()
    (assets / "binary.bin").write_bytes(bytes(range(256)) * 8)
    unicode_dir = project / "数据"
    unicode_dir.mkdir()
    (unicode_dir / "说明.txt").write_text("你好，世界\n", encoding="utf-8")
    junk = project / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "index.js").write_text("x" * 4096, encoding="utf-8")

    init_repo(project)
    commit_all(project, "initial")
    git(project, "tag", "v1")
    git(project, "checkout", "-q", "-b", "feature")
    (project / "feature.txt").write_text("feature\n", encoding="utf-8")
    commit_all(project, "feature")
    git(project, "checkout", "-q", "main")
    (project / "second.txt").write_text("second\n", encoding="utf-8")
    commit_all(project, "second")
    return project


def tracked_files(project: Path) -> set[Path]:
    return {
        path.relative_to(project)
        for path in project.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "node_modules" not in path.parts
    }


class EndToEndSmokeTests(unittest.TestCase):
    def test_backup_verify_restore_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_sample_project(root)
            dest = root / "backups"

            manifests, records, failures = run_backup(
                [scan_project(project)],
                dest,
                always_archive=True,
            )
            self.assertFalse(failures)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].strategy, "archive")
            self.assertIsNotNone(records[0].archive)

            verified = verify_manifest(manifests[0])
            self.assertEqual(verified[0]["status"], "verified")

            restored = root / "restored"
            restore_from_manifest(manifests[0], restored)
            self.assertEqual(
                (restored / "app" / "src" / "main.py").read_text(encoding="utf-8"),
                "print('hello')\n",
            )
            for relative in tracked_files(project):
                self.assertEqual(
                    (restored / "app" / relative).read_bytes(),
                    (project / relative).read_bytes(),
                    relative,
                )
            self.assertFalse((restored / "app" / "node_modules").exists())

    def test_tampered_archive_and_corrupt_manifest_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_sample_project(root)
            dest = root / "backups"
            manifests, records, failures = run_backup(
                [scan_project(project)],
                dest,
                always_archive=True,
            )
            self.assertFalse(failures)

            tampered = root / "tampered"
            shutil.copytree(dest, tampered)
            tampered_manifest = next(tampered.rglob("manifest-*.json"))
            tampered_archive = next(tampered.rglob("*.tar.gz"))
            tampered_archive.write_bytes(tampered_archive.read_bytes() + b"tamper")
            document = json.loads(tampered_manifest.read_text(encoding="utf-8"))
            document["records"][0]["archive"] = str(tampered_archive)
            tampered_manifest.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(RestoreError, "sha256 mismatch"):
                verify_manifest(tampered_manifest)
            target = root / "should-not-exist"
            with self.assertRaises(RestoreError):
                restore_from_manifest(tampered_manifest, target)
            self.assertFalse(target.exists())

            corrupt = root / "corrupt.json"
            corrupt.write_text(
                json.dumps({"schema_version": 2, "records": []}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RestoreError, "non-empty array"):
                restore_from_manifest(corrupt, root / "also-missing")


if __name__ == "__main__":
    unittest.main()
