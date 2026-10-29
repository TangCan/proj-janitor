from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from proj_janitor.config import PROJECT_MARKERS
from proj_janitor.scanner import (
    _remote_tag_refs,
    _submodule_url_has_sensitive_data,
    discover_projects,
    inspect_git_recoverability,
    is_github_remote_url,
    measure_tree,
    sanitize_remote_url,
    scan_project,
)
from tests.support import (
    GIT_AVAILABLE,
    commit_all,
    git,
    init_repo,
    make_pushed_repo,
)


class ScannerTests(unittest.TestCase):
    def test_measure_tree_propagates_walk_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                measure_tree(Path(tmp) / "missing")

    def test_measure_tree_ignores_file_disappearing_during_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "vanishing.txt"
            missing.write_text("will disappear", encoding="utf-8")
            real_stat = Path.stat

            def disappear(path, *args, **kwargs):
                if path == missing:
                    raise FileNotFoundError(path)
                return real_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", disappear):
                self.assertEqual(measure_tree(root)[0], 0)

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
            hidden = root / ".hidden"
            hidden.mkdir()
            (hidden / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
            expected_markers = {
                ".git",
                ".hg",
                ".svn",
                "pyproject.toml",
                "setup.py",
                "setup.cfg",
                "requirements.txt",
                "package.json",
                "Cargo.toml",
                "go.mod",
                "CMakeLists.txt",
                "Makefile",
                "meson.build",
                "build.gradle",
                "build.gradle.kts",
                "pom.xml",
            }
            self.assertEqual(set(PROJECT_MARKERS), expected_markers)
            marker_projects = set()
            for index, marker in enumerate(sorted(expected_markers)):
                marked = root / f"marker-{index}"
                marked.mkdir()
                if marker == ".git":
                    (marked / marker).mkdir()
                else:
                    (marked / marker).write_text("", encoding="utf-8")
                marker_projects.add(marked.name)

            projects = discover_projects(root)

            by_name = {project.name: project for project in projects}
            self.assertEqual(set(by_name), {"alpha", *marker_projects})
            self.assertEqual(by_name["alpha"].size_bytes, 10)
            self.assertIn("pyproject.toml", by_name["alpha"].markers)

    def test_discovery_skips_symlink_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "app"
            project.mkdir()
            (project / "pyproject.toml").write_text("", encoding="utf-8")
            try:
                (root / "linked-app").symlink_to(project, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"cannot create directory symlink: {exc}")

            projects = discover_projects(root)
            self.assertEqual([item.path for item in projects], [project.resolve()])

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

            self.assertEqual([project.path for project in projects], [root.resolve()])

    def test_missing_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                discover_projects(Path(tmp) / "missing")

    def test_scan_project_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            root.mkdir()
            marker = root / "package.json"
            marker.write_text("{}", encoding="utf-8")
            older_file = root / "older.py"
            older_file.write_text("old", encoding="utf-8")
            timestamp = 1_700_000_000
            os.utime(marker, (timestamp, timestamp))
            os.utime(older_file, (timestamp - 100, timestamp - 100))

            project = scan_project(root)

            self.assertIsNone(project.git)
            self.assertEqual(project.branch_label, "-")
            self.assertEqual(project.dirty_label, "-")
            self.assertAlmostEqual(project.last_modified.timestamp(), timestamp, delta=1)

            empty = Path(tmp) / "empty"
            (empty / ".git").mkdir(parents=True)
            empty_project = scan_project(empty)
            self.assertEqual(empty_project.size_bytes, 0)
            self.assertIsNone(empty_project.last_modified)

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


class GithubUrlTests(unittest.TestCase):
    def test_accepts_supported_github_urls(self) -> None:
        for url in (
            "https://github.com/acme/app",
            "https://github.com/acme/app.git",
            "https://token@github.com/acme/app.git",
            "https://user:token@github.com/acme/app.git",
            "https://x-access-token:ghp_example@github.com/acme/app.git",
            "ssh://git@github.com/acme/app.git",
            "git@github.com:acme/app.git",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_github_remote_url(url))

    def test_rejects_non_github_or_ambiguous_urls(self) -> None:
        for url in (
            "http://github.com/acme/app.git",
            "https://github.com.evil.com/acme/app.git",
            "https://gitlab.com/acme/app.git",
            "/tmp/acme/app.git",
            ".",
            "..",
            "https://github.com/acme/app/",
            "https://github.com/acme/app/extra",
            "https://github.com/acme/app?ref=main",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_github_remote_url(url))

    def test_sanitizes_https_credentials(self) -> None:
        self.assertEqual(
            sanitize_remote_url(
                "https://user:secret-token@github.com/acme/app.git",
            ),
            "https://github.com/acme/app.git",
        )
        self.assertEqual(
            sanitize_remote_url("https://secret-token@github.com/acme/app"),
            "https://github.com/acme/app",
        )
        self.assertEqual(
            sanitize_remote_url("git@github.com:acme/app.git"),
            "git@github.com:acme/app.git",
        )


@unittest.skipUnless(GIT_AVAILABLE, "git is not available")
class GitRecoveryTests(unittest.TestCase):
    def test_submodule_https_credentials_block_git_only_without_exposing_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_pushed_repo(Path(tmp))
            (project / ".gitmodules").write_text(
                '[submodule "private"]\n'
                "\tpath = vendor/private\n"
                "\turl = https://alice:submodule-secret@github.com/acme/private.git\n",
                encoding="utf-8",
            )
            git(project, "add", ".gitmodules")
            git(project, "commit", "-m", "configure submodule")
            git(project, "update-ref", "refs/remotes/origin/main", "HEAD")

            state = inspect_git_recoverability(scan_project(project))

            self.assertFalse(state.eligible)
            self.assertEqual(
                state.reason,
                "submodule URL contains credentials or query data",
            )
            self.assertNotIn("submodule-secret", state.reason)

    def test_submodule_query_data_blocks_git_only_without_exposing_token(self) -> None:
        self.assertFalse(
            _submodule_url_has_sensitive_data("ssh://git@github.com/acme/private.git")
        )
        urls = (
            "https://github.com/acme/private.git?token=query-secret",
            "ssh://git:ssh-secret@github.com/acme/private.git",
        )
        for url in urls:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as tmp:
                project = make_pushed_repo(Path(tmp))
                (project / ".gitmodules").write_text(
                    '[submodule "private"]\n'
                    "\tpath = vendor/private\n"
                    f"\turl = {url}\n",
                    encoding="utf-8",
                )
                git(project, "add", ".gitmodules")
                git(project, "commit", "-m", "configure submodule")
                git(project, "update-ref", "refs/remotes/origin/main", "HEAD")

                state = inspect_git_recoverability(scan_project(project))

                self.assertFalse(state.eligible)
                self.assertEqual(
                    state.reason,
                    "submodule URL contains credentials or query data",
                )
                self.assertNotIn("secret", state.reason)

    def test_pushed_repository_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_pushed_repo(Path(tmp))

            state = inspect_git_recoverability(scan_project(project))

            self.assertTrue(state.eligible)
            self.assertIsNone(state.reason)
            self.assertEqual(state.remote, "origin")
            self.assertEqual(
                state.remote_url,
                "https://github.com/acme/app.git",
            )
            self.assertEqual(state.branch, "main")
            self.assertEqual(state.commit, git(project, "rev-parse", "HEAD"))
            self.assertEqual(state.upstream, "origin/main")
            self.assertEqual(state.verified, "local-ref")

    def test_credentialed_https_remote_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_pushed_repo(Path(tmp))
            git(
                project,
                "remote",
                "set-url",
                "origin",
                "https://user:secret-token@github.com/acme/app.git",
            )

            state = inspect_git_recoverability(scan_project(project))

            self.assertTrue(state.eligible)
            self.assertEqual(
                state.remote_url,
                "https://github.com/acme/app.git",
            )
            self.assertNotIn("secret-token", state.remote_url or "")

    def test_unpushed_commit_is_not_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_pushed_repo(Path(tmp))
            (project / "main.py").write_text("print('new')\n", encoding="utf-8")
            commit_all(project, "unpushed")

            state = inspect_git_recoverability(scan_project(project))

            self.assertFalse(state.eligible)
            self.assertEqual(state.reason, "HEAD is not contained in upstream")

    def test_dirty_repository_is_not_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_pushed_repo(Path(tmp))
            (project / "main.py").write_text("print('dirty')\n", encoding="utf-8")

            state = inspect_git_recoverability(scan_project(project))

            self.assertFalse(state.eligible)
            self.assertEqual(state.reason, "working tree is not clean")

    def test_stash_blocks_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_pushed_repo(Path(tmp))
            (project / "main.py").write_text("print('stash')\n", encoding="utf-8")
            git(project, "stash", "push", "-q", "-m", "wip")

            state = inspect_git_recoverability(scan_project(project))

            self.assertFalse(state.eligible)
            self.assertEqual(state.reason, "stash entries are not included")

    def test_remote_tag_refs_returns_raw_tag_object_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            remote = root / "app-remote.git"
            git(project, "tag", "v1")
            git(project, "tag", "-a", "v2", "-m", "v2")
            git(project, "push", "-q", str(remote), "v1", "v2")

            refs = _remote_tag_refs(project, str(remote))

            self.assertEqual(
                refs,
                {
                    "v1": git(project, "rev-parse", "v1"),
                    "v2": git(project, "rev-parse", "v2"),
                },
            )

    def test_pushed_tag_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_pushed_repo(Path(tmp))
            git(project, "tag", "v1")
            tag_sha = git(project, "rev-parse", "v1")

            with patch(
                "proj_janitor.scanner._remote_tag_refs",
                return_value={"v1": tag_sha},
            ):
                state = inspect_git_recoverability(scan_project(project))

            self.assertTrue(state.eligible)
            self.assertIsNone(state.reason)

    def test_local_only_tag_blocks_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_pushed_repo(Path(tmp))
            git(project, "tag", "v1")

            with patch(
                "proj_janitor.scanner._remote_tag_refs",
                return_value={},
            ):
                state = inspect_git_recoverability(scan_project(project))

            self.assertFalse(state.eligible)
            self.assertEqual(state.reason, "local tag is not pushed: v1")

    def test_tag_with_different_remote_target_blocks_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_pushed_repo(Path(tmp))
            git(project, "tag", "v1")

            with patch(
                "proj_janitor.scanner._remote_tag_refs",
                return_value={"v1": "0" * 40},
            ):
                state = inspect_git_recoverability(scan_project(project))

            self.assertFalse(state.eligible)
            self.assertEqual(
                state.reason,
                "local tag differs from remote: v1",
            )

    def test_git_subprocess_failures_block_url_only_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_pushed_repo(Path(tmp))
            git(project, "tag", "v1")
            real_run = subprocess.run

            cases = (
                ("ls-remote", "timeout", "cannot verify local tags on remote"),
                ("ls-remote", "nonzero", "cannot verify local tags on remote"),
                ("ls-remote", "spawn", "cannot verify local tags on remote"),
                ("status", "nonzero", "git status failed"),
            )
            for target, failure, reason in cases:
                with self.subTest(command=target, failure=failure):
                    def fail_git(command, *args, **kwargs):
                        if target not in command:
                            return real_run(command, *args, **kwargs)
                        if failure == "timeout":
                            raise subprocess.TimeoutExpired(command, timeout=10)
                        if failure == "spawn":
                            raise PermissionError("git could not be started")
                        return subprocess.CompletedProcess(command, 1, "", "")

                    with patch(
                        "proj_janitor.scanner.subprocess.run",
                        side_effect=fail_git,
                    ):
                        state = inspect_git_recoverability(scan_project(project))

                    self.assertFalse(state.eligible)
                    self.assertEqual(state.reason, reason)

    def test_non_github_remote_is_not_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            git(
                project,
                "remote",
                "set-url",
                "origin",
                str(root / "app-remote.git"),
            )

            state = inspect_git_recoverability(scan_project(project))

            self.assertFalse(state.eligible)
            self.assertEqual(
                state.reason,
                "remote is not a supported GitHub URL",
            )

    def _pushed_repo_with_ignored_dir(self, root: Path) -> Path:
        project = make_pushed_repo(root)
        (project / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        commit_all(project, "ignore artifacts")
        git(project, "remote", "set-url", "origin", str(root / "app-remote.git"))
        git(project, "push", "-q", "origin", "main")
        git(
            project,
            "remote",
            "set-url",
            "origin",
            "https://github.com/acme/app.git",
        )
        (project / "node_modules" / "pkg").mkdir(parents=True)
        (project / "node_modules" / "pkg" / "index.js").write_text(
            "x" * 8,
            encoding="utf-8",
        )
        return project

    def _pushed_repo_with_ignored_coverage(
        self,
        root: Path,
        *,
        marker: bool,
        nested: bool = False,
    ) -> Path:
        project = make_pushed_repo(root)
        (project / ".gitignore").write_text("coverage/\n", encoding="utf-8")
        coverage = project / "src" / "coverage" if nested else project / "coverage"
        coverage.mkdir(parents=True)
        if marker:
            (coverage / "lcov.info").write_text("", encoding="utf-8")
        (coverage / "notes.txt").write_text("", encoding="utf-8")
        commit_all(project, "ignore coverage")
        git(project, "remote", "set-url", "origin", str(root / "app-remote.git"))
        git(project, "push", "-q", "origin", "main")
        git(
            project,
            "remote",
            "set-url",
            "origin",
            "https://github.com/acme/app.git",
        )
        return project

    def test_root_coverage_artifact_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._pushed_repo_with_ignored_coverage(
                Path(tmp),
                marker=True,
            )

            state = inspect_git_recoverability(
                scan_project(project),
                patterns=("coverage",),
            )

            self.assertTrue(state.eligible)
            self.assertIsNone(state.reason)

    def test_nested_coverage_artifact_blocks_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._pushed_repo_with_ignored_coverage(
                Path(tmp),
                marker=True,
                nested=True,
            )

            state = inspect_git_recoverability(
                scan_project(project),
                patterns=("coverage",),
            )

            self.assertFalse(state.eligible)
            self.assertTrue(
                state.reason.startswith("ignored path is not an artifact"),
            )

    def test_root_coverage_without_marker_blocks_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._pushed_repo_with_ignored_coverage(
                Path(tmp),
                marker=False,
            )

            state = inspect_git_recoverability(
                scan_project(project),
                patterns=("coverage",),
            )

            self.assertFalse(state.eligible)
            self.assertTrue(
                state.reason.startswith("ignored path is not an artifact"),
            )

    def test_ignored_artifact_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._pushed_repo_with_ignored_dir(Path(tmp))

            state = inspect_git_recoverability(
                scan_project(project),
                patterns=("node_modules",),
            )

            self.assertTrue(state.eligible)
            self.assertIsNone(state.reason)

    def test_ignored_non_artifact_blocks_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._pushed_repo_with_ignored_dir(Path(tmp))

            state = inspect_git_recoverability(scan_project(project))

            self.assertFalse(state.eligible)
            self.assertTrue(
                state.reason.startswith("ignored path is not an artifact"),
            )


if __name__ == "__main__":
    unittest.main()
