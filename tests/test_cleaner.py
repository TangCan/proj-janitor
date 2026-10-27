from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from proj_janitor.cleaner import (
    _action,
    apply_cleanup,
    build_cleanup_plan,
    resolve_rules,
)


def make_project(root: Path) -> Path:
    project = root / "app"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (project / "node_modules" / "pkg").mkdir(parents=True)
    (project / "node_modules" / "pkg" / "index.js").write_text(
        "x" * 32,
        encoding="utf-8",
    )
    (project / "src" / "__pycache__").mkdir()
    (project / "src" / "__pycache__" / "main.pyc").write_bytes(b"\x00" * 8)
    (project / "dist").mkdir()
    (project / "dist" / "bundle.js").write_text("x" * 16, encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return project


class CleanerTests(unittest.TestCase):
    def test_file_action_propagates_stat_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            path = project / "node_modules" / "pkg" / "index.js"
            with mock.patch.object(Path, "stat", side_effect=PermissionError("stat denied")):
                with self.assertRaisesRegex(PermissionError, "stat denied"):
                    _action(project, path, "node", "file")

    def test_plan_propagates_walk_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            with mock.patch(
                "proj_janitor.cleaner.os.walk",
                side_effect=PermissionError("walk denied"),
            ):
                with self.assertRaisesRegex(PermissionError, "walk denied"):
                    build_cleanup_plan(project)

    def test_plan_finds_artifacts_and_protects_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))

            plan = build_cleanup_plan(project)
            matched = {action.path.name for action in plan}

            self.assertEqual(matched, {"node_modules", "__pycache__", "dist"})
            self.assertNotIn(".git", matched)
            self.assertNotIn("main.py", matched)
            self.assertTrue(all(action.project == project.resolve() for action in plan))

    def test_apply_cleanup_removes_only_planned_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            plan = build_cleanup_plan(project)

            result = apply_cleanup(plan)

            self.assertEqual(result.removed, 3)
            self.assertGreater(result.freed_bytes, 0)
            self.assertEqual(result.errors, ())
            self.assertFalse((project / "node_modules").exists())
            self.assertFalse((project / "dist").exists())
            self.assertFalse((project / "src" / "__pycache__").exists())
            self.assertTrue((project / "src" / "main.py").exists())
            self.assertTrue((project / ".git" / "config").exists())

    def test_rule_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))

            plan = build_cleanup_plan(project, ["node"])

            self.assertEqual(
                [action.path.name for action in plan],
                ["node_modules"],
            )
            with self.assertRaises(ValueError):
                resolve_rules(["does-not-exist"])

    def test_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            plan = build_cleanup_plan(project)
            apply_cleanup(plan)

            second = apply_cleanup(plan)

            self.assertEqual(second.removed, 0)
            self.assertEqual(second.errors, ())

    def test_coverage_rule_matches_only_root_with_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "app"
            project.mkdir()
            (project / "pyproject.toml").write_text("", encoding="utf-8")
            root_coverage = project / "coverage"
            root_coverage.mkdir()
            (root_coverage / "lcov.info").write_text("", encoding="utf-8")
            nested_coverage = project / "src" / "coverage"
            nested_coverage.mkdir(parents=True)
            (nested_coverage / "lcov.info").write_text("", encoding="utf-8")
            empty_coverage = project / "docs" / "coverage"
            empty_coverage.mkdir(parents=True)
            (empty_coverage / "notes.txt").write_text("", encoding="utf-8")

            plan = build_cleanup_plan(project, ["node"])

            paths = {action.path for action in plan}
            self.assertIn(root_coverage.resolve(), paths)
            self.assertNotIn(nested_coverage.resolve(), paths)
            self.assertNotIn(empty_coverage.resolve(), paths)

    def test_coverage_without_marker_is_not_matched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "app"
            project.mkdir()
            (project / "pyproject.toml").write_text("", encoding="utf-8")
            coverage = project / "coverage"
            coverage.mkdir()
            (coverage / "notes.txt").write_text("", encoding="utf-8")

            plan = build_cleanup_plan(project, ["node"])

            self.assertEqual(plan, [])

    def test_coverage_file_is_not_matched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "app"
            project.mkdir()
            (project / "pyproject.toml").write_text("", encoding="utf-8")
            (project / "coverage").write_text("", encoding="utf-8")

            plan = build_cleanup_plan(project, ["node"])

            self.assertEqual(plan, [])

    def test_symlink_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "app"
            project.mkdir()
            (project / "pyproject.toml").write_text("", encoding="utf-8")
            outside = root / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep", encoding="utf-8")
            (project / "node_modules").symlink_to(outside, target_is_directory=True)

            plan = build_cleanup_plan(project)

            self.assertEqual(plan, [])
            self.assertTrue((outside / "keep.txt").exists())


if __name__ == "__main__":
    unittest.main()
