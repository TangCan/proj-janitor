from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from proj_janitor.backup import run_backup, sha256_file
from proj_janitor.scanner import scan_project


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


if __name__ == "__main__":
    unittest.main()
