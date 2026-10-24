from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from proj_janitor.cli import main
from tests.support import GIT_AVAILABLE, git, make_pushed_repo


def make_project(root: Path) -> Path:
    project = root / "app"
    project.mkdir()
    (project / "pyproject.toml").write_text("", encoding="utf-8")
    (project / "node_modules" / "pkg").mkdir(parents=True)
    (project / "node_modules" / "pkg" / "index.js").write_text(
        "x" * 8,
        encoding="utf-8",
    )
    (project / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return project


def run_cli(*argv: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(list(argv))
    return code, stdout.getvalue(), stderr.getvalue()


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class CliTests(unittest.TestCase):
    def test_scan_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            make_project(Path(tmp))

            code, out, _ = run_cli("scan", tmp, "--json")

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["name"], "app")
            self.assertGreater(payload[0]["size_bytes"], 0)

    def test_backup_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            make_project(Path(tmp))
            dest = Path(tmp) / "backups"

            code, out, _ = run_cli(
                "backup",
                tmp,
                "--dest",
                str(dest),
                "--dry-run",
            )

            self.assertEqual(code, 0)
            self.assertIn("Dry run", out)
            self.assertFalse(dest.exists())

    def test_restore_archive_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backups = root / "backups"

            code, _, _ = run_cli(
                "backup",
                tmp,
                "--dest",
                str(backups),
            )
            self.assertEqual(code, 0)
            manifest = next(backups.glob("*/manifest-*.json"))
            shutil.rmtree(project)

            restored = root / "restored"
            code, out, _ = run_cli(
                "restore",
                str(manifest),
                "--dest",
                str(restored),
            )

            self.assertEqual(code, 0)
            self.assertIn("Restored 1 project(s)", out)
            self.assertEqual(
                (restored / "app" / "main.py").read_text(encoding="utf-8"),
                "print('hi')\n",
            )
            self.assertFalse((restored / "app" / "node_modules").exists())

    def test_restore_dry_run_json_is_pure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_project(root)
            backups = root / "backups"
            run_cli("backup", tmp, "--dest", str(backups))
            manifest = next(backups.glob("*/manifest-*.json"))
            restored = root / "restored"

            code, out, _ = run_cli(
                "restore",
                str(manifest),
                "--dest",
                str(restored),
                "--dry-run",
                "--json",
            )

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["projects"][0]["strategy"], "archive")
            self.assertFalse(restored.exists())

    def test_restore_original_requires_yes_when_not_tty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backups = root / "backups"
            run_cli("backup", tmp, "--dest", str(backups))
            manifest = next(backups.glob("*/manifest-*.json"))
            original = project.resolve()
            shutil.rmtree(project)

            code, _, err = run_cli("restore", str(manifest))

            self.assertEqual(code, 2)
            self.assertIn("--yes", err)
            self.assertFalse(original.exists())

    def test_restore_original_with_yes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backups = root / "backups"
            run_cli("backup", tmp, "--dest", str(backups))
            manifest = next(backups.glob("*/manifest-*.json"))
            original = project.resolve()
            shutil.rmtree(project)

            code, out, _ = run_cli("restore", str(manifest), "--yes")

            self.assertEqual(code, 0)
            self.assertIn("Restored 1 project(s)", out)
            self.assertEqual(
                (original / "main.py").read_text(encoding="utf-8"),
                "print('hi')\n",
            )
            self.assertFalse((original / "node_modules").exists())

    def test_restore_original_dry_run_shows_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backups = root / "backups"
            run_cli("backup", tmp, "--dest", str(backups))
            manifest = next(backups.glob("*/manifest-*.json"))
            original = project.resolve()
            shutil.rmtree(project)

            code, out, _ = run_cli("restore", str(manifest), "--dry-run")

            self.assertEqual(code, 0)
            self.assertIn("original backup locations", out)
            self.assertIn(str(original), out)
            self.assertFalse(original.exists())

    def test_restore_original_json_requires_yes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backups = root / "backups"
            run_cli("backup", tmp, "--dest", str(backups))
            manifest = next(backups.glob("*/manifest-*.json"))
            original = project.resolve()
            shutil.rmtree(project)

            code, out, err = run_cli("restore", str(manifest), "--json")

            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertIn("--yes", err)
            self.assertFalse(original.exists())

    def test_restore_original_json_with_yes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backups = root / "backups"
            run_cli("backup", tmp, "--dest", str(backups))
            manifest = next(backups.glob("*/manifest-*.json"))
            original = project.resolve()
            shutil.rmtree(project)

            code, out, _ = run_cli(
                "restore",
                str(manifest),
                "--yes",
                "--json",
            )

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertFalse(payload["dry_run"])
            self.assertEqual(payload["mode"], "original")
            self.assertEqual(payload["projects"][0]["target"], str(original))
            self.assertTrue((original / "main.py").is_file())

    def test_restore_original_confirmation_accepts_yes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backups = root / "backups"
            run_cli("backup", tmp, "--dest", str(backups))
            manifest = next(backups.glob("*/manifest-*.json"))
            original = project.resolve()
            shutil.rmtree(project)

            with mock.patch("sys.stdin", Tty()), mock.patch(
                "builtins.input",
                return_value="y",
            ):
                code, out, _ = run_cli("restore", str(manifest))

            self.assertEqual(code, 0)
            self.assertIn("About to restore to original locations", out)
            self.assertTrue((original / "main.py").is_file())

    def test_restore_original_confirmation_aborts_on_no(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backups = root / "backups"
            run_cli("backup", tmp, "--dest", str(backups))
            manifest = next(backups.glob("*/manifest-*.json"))
            original = project.resolve()
            shutil.rmtree(project)

            with mock.patch("sys.stdin", Tty()), mock.patch(
                "builtins.input",
                return_value="n",
            ):
                code, out, _ = run_cli("restore", str(manifest))

            self.assertEqual(code, 2)
            self.assertIn("Aborted.", out)
            self.assertFalse(original.exists())

    def test_restore_missing_manifest_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            code, _, err = run_cli(
                "restore",
                str(root / "missing.json"),
                "--dest",
                str(root / "restored"),
            )

            self.assertEqual(code, 1)
            self.assertIn("manifest does not exist", err)

    def test_clean_requires_confirmation_when_not_tty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))

            code, _, err = run_cli("clean", tmp, "--apply")

            self.assertEqual(code, 2)
            self.assertIn("--yes", err)
            self.assertTrue((project / "node_modules").exists())

    def test_clean_apply_with_yes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))

            code, out, _ = run_cli("clean", tmp, "--apply", "--yes")

            self.assertEqual(code, 0)
            self.assertIn("Removed 1 item", out)
            self.assertFalse((project / "node_modules").exists())
            self.assertTrue((project / "main.py").exists())

    def test_clean_defaults_to_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))

            code, out, _ = run_cli("clean", tmp)

            self.assertEqual(code, 0)
            self.assertIn("Dry run", out)
            self.assertTrue((project / "node_modules").exists())

    def test_list_rules(self) -> None:
        code, out, _ = run_cli("clean", ".", "--list-rules")

        self.assertEqual(code, 0)
        self.assertIn("node:", out)
        self.assertIn("build:", out)

    def test_missing_root_reports_error(self) -> None:
        code, _, err = run_cli("scan", "/path/that/does/not/exist")

        self.assertEqual(code, 1)
        self.assertIn("error:", err)


@unittest.skipUnless(GIT_AVAILABLE, "git is not available")
class GitCliTests(unittest.TestCase):
    def test_backup_dry_run_shows_git_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_pushed_repo(root)
            dest = root / "backups"

            code, out, _ = run_cli(
                "backup",
                str(root),
                "--dest",
                str(dest),
                "--dry-run",
            )

            self.assertEqual(code, 0)
            self.assertIn("Dry run", out)
            self.assertIn("[git] app:", out)
            self.assertIn("https://github.com/acme/app.git", out)
            self.assertFalse(dest.exists())

    def test_backup_dry_run_redacts_credentials(self) -> None:
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

            code, out, _ = run_cli(
                "backup",
                str(root),
                "--dest",
                str(dest),
                "--dry-run",
            )

            self.assertEqual(code, 0)
            self.assertIn("[git] app:", out)
            self.assertIn("https://github.com/acme/app.git", out)
            self.assertNotIn("secret-token", out)

    def test_backup_records_git_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            dest = root / "backups"
            head = git(project, "rev-parse", "HEAD")

            code, out, _ = run_cli(
                "backup",
                str(root),
                "--dest",
                str(dest),
            )

            self.assertEqual(code, 0)
            self.assertIn("[git] app:", out)
            self.assertNotIn("[archive]", out)

            manifest_path = next(dest.glob("*/manifest-*.json"))
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"),
            )
            entry = manifest["records"][0]
            self.assertEqual(entry["strategy"], "git")
            self.assertEqual(entry["commit"], head)
            self.assertEqual(
                entry["remote_url"],
                "https://github.com/acme/app.git",
            )

    def test_always_archive_flag_forces_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_pushed_repo(root)
            dest = root / "backups"

            code, out, _ = run_cli(
                "backup",
                str(root),
                "--dest",
                str(dest),
                "--always-archive",
            )

            self.assertEqual(code, 0)
            self.assertIn("[archive] app:", out)

            manifest_path = next(dest.glob("*/manifest-*.json"))
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertTrue(manifest["always_archive"])
            self.assertEqual(
                manifest["records"][0]["fallback_reason"],
                "always_archive",
            )


if __name__ == "__main__":
    unittest.main()
