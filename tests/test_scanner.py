from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from proj_janitor.scanner import discover_projects, scan_project


class ScannerTests(unittest.TestCase):
    def test_discovers_marked_projects_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alpha = root / "alpha"
            alpha.mkdir()
            (alpha / "pyproject.toml").write_text("", encoding="utf-8")
            (alpha / "src.py").write_text("x" * 10, encoding="utf-8")

            beta = root / "beta"
            beta.mkdir()
            (beta / "notes.txt").write_text("x", encoding="utf-8")

            projects = discover_projects(root)

            self.assertEqual([project.name for project in projects], ["alpha"])
            self.assertEqual(projects[0].size_bytes, 10)
            self.assertIn("pyproject.toml", projects[0].markers)

    def test_include_unmarked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("alpha", "beta"):
                directory = root / name
                directory.mkdir()
                (directory / "file.txt").write_text("x", encoding="utf-8")
            (root / "alpha" / "pyproject.toml").write_text("", encoding="utf-8")

            projects = discover_projects(root, include_unmarked=True)

            self.assertEqual(
                {project.name for project in projects},
                {"alpha", "beta"},
            )

    def test_root_can_be_a_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "main.py").write_text("pass\n", encoding="utf-8")

            projects = discover_projects(root)

            self.assertEqual([project.path for project in projects], [root])

    def test_missing_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                discover_projects(Path(tmp) / "missing")

    def test_scan_project_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            root.mkdir()
            (root / "package.json").write_text("{}", encoding="utf-8")

            project = scan_project(root)

            self.assertIsNone(project.git)
            self.assertEqual(project.branch_label, "-")
            self.assertEqual(project.dirty_label, "-")

    @unittest.skipUnless(shutil.which("git"), "git is not available")
    def test_scan_project_reports_clean_and_dirty_git_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            root.mkdir()
            subprocess.run(
                ["git", "init", "-q", str(root)],
                check=True,
                capture_output=True,
                text=True,
            )

            clean = scan_project(root)
            self.assertIsNotNone(clean.git)
            self.assertFalse(clean.git.dirty)
            self.assertEqual(clean.dirty_label, "clean")

            (root / "untracked.txt").write_text("x", encoding="utf-8")

            dirty = scan_project(root)
            self.assertIsNotNone(dirty.git)
            self.assertTrue(dirty.git.dirty)
            self.assertEqual(dirty.dirty_label, "dirty")


if __name__ == "__main__":
    unittest.main()
