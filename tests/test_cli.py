from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from proj_janitor import __version__
from proj_janitor import backup as backup_module
from proj_janitor.cleaner import build_cleanup_plan
from proj_janitor.cli import build_parser, main
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


def write_git_manifest(
    root: Path, project: Path, remote: object, commit: str
) -> Path:
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
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "created_at": "2026-11-04T00:00:00+00:00",
                "tool": "proj-janitor",
                "version": "0.1.0",
                "include_artifacts": False,
                "always_archive": False,
                "records": [record],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class CliTests(unittest.TestCase):
    def test_help_and_version(self) -> None:
        for argv, expected in (
            (["--help"], "usage: projjan"),
            (["--version"], f"projjan {__version__}"),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                build_parser().parse_args(argv)

            self.assertEqual(raised.exception.code, 0)
            self.assertIn(expected, stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_package_version_matches_pyproject(self) -> None:
        import tomllib
        from pathlib import Path

        manifest = tomllib.loads(
            Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(__version__, manifest["project"]["version"])

    def test_verify_archive_manifest_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backup_dest = root / "backups"
            code, _, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(backup_dest),
                "--always-archive",
            )
            self.assertEqual(code, 0, err)
            manifest = next((backup_dest / "app").glob("manifest-*.json"))

            code, out, err = run_cli("verify", str(manifest), "--json")

            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["projects"][0]["status"], "verified")
            self.assertTrue(project.exists())

    def test_scan_json_and_text_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))

            code, out, _ = run_cli("scan", tmp, str(project), "--json")

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(len(payload), 1)
            record = payload[0]
            self.assertEqual(
                set(record),
                {"name", "path", "size_bytes", "last_modified", "markers", "git"},
            )
            self.assertEqual(record["name"], "app")
            self.assertEqual(record["path"], str(project.resolve()))
            self.assertGreater(record["size_bytes"], 0)
            self.assertIsInstance(record["last_modified"], str)
            self.assertEqual(record["markers"], ["pyproject.toml"])
            self.assertIsNone(record["git"])

            (project / ".git").mkdir()
            code, out, err = run_cli("scan", str(project), "--json")

            self.assertEqual(code, 0, err)
            git_record = json.loads(out)[0]
            self.assertEqual(git_record["markers"], [".git", "pyproject.toml"])
            self.assertIsNone(git_record["git"]["branch"])
            self.assertIsNone(git_record["git"]["dirty"])
            self.assertIn("detail", git_record["git"])
            self.assertIn("git ", git_record["git"]["detail"])
            self.assertIn("warning: app: git state unknown", err)

            code, out, err = run_cli("scan", tmp, str(project))

            self.assertEqual(code, 0, err)
            for column in ("PROJECT", "SIZE", "BRANCH", "STATE", "MARKERS", "MODIFIED"):
                self.assertIn(column, out)
            self.assertIn("app", out)
            self.assertIn("warning: app: git state unknown", err)
            self.assertIn("unknown", out)

    def test_backup_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            make_project(Path(tmp))
            dest = Path(tmp) / "backups"

            code, out, _ = run_cli(
                "backup",
                tmp,
                "--backup-dest",
                str(dest),
                "--dry-run",
            )

            self.assertEqual(code, 0)
            self.assertIn("Dry run", out)
            self.assertFalse(dest.exists())

    def test_backup_deduplicates_overlapping_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backup_dest = root / "backups"

            code, out, err = run_cli(
                "backup",
                str(root),
                str(project),
                "--backup-dest",
                str(backup_dest),
                "--always-archive",
            )

            self.assertEqual(code, 0, err)
            self.assertIn("Backed up 1 project(s).", out)
            self.assertEqual(len(list((backup_dest / "app").glob("manifest-*.json"))), 1)

    def test_backup_keep_yes_retains_only_latest_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backup_dest = root / "backups"

            for _ in range(3):
                code, out, err = run_cli(
                    "backup", str(project), "--backup-dest", str(backup_dest)
                )
                self.assertEqual(code, 0, err)
            self.assertEqual(
                len(list((backup_dest / "app").glob("manifest-*.json"))), 3
            )

            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(backup_dest),
                "--keep",
                "2",
                "--yes",
            )
            self.assertEqual(code, 0, err)
            self.assertIn("Deleted ", out)
            self.assertEqual(
                len(list((backup_dest / "app").glob("manifest-*.json"))), 2
            )

    def test_backup_keep_without_yes_skips_retention_noninteractively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backup_dest = root / "backups"

            for _ in range(3):
                code, out, err = run_cli(
                    "backup", str(project), "--backup-dest", str(backup_dest)
                )
                self.assertEqual(code, 0, err)

            with mock.patch("sys.stdin", io.StringIO()):
                code, out, err = run_cli(
                    "backup",
                    str(project),
                    "--backup-dest",
                    str(backup_dest),
                    "--keep",
                    "2",
                )
            self.assertEqual(code, 0, err)
            self.assertIn("Non-interactive; skipping retention", out)
            self.assertEqual(
                len(list((backup_dest / "app").glob("manifest-*.json"))), 4
            )

    def test_backup_rejects_destination_inside_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            backup_dest = project / "backups"

            code, out, err = run_cli(
                "backup", str(project), "--backup-dest", str(backup_dest)
            )

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("backup destination is inside project", err)
            self.assertNotIn("Traceback", err)
            self.assertFalse(backup_dest.exists())

    def test_backup_uses_backup_dest_environment_variable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backup_dest = root / "env-backups"

            with mock.patch.dict(
                "os.environ", {"BACKUP_DEST": str(backup_dest)}
            ):
                code, out, err = run_cli("backup", str(project))

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertTrue(list((backup_dest / "app").glob("manifest-*.json")))

    def test_backup_dest_option_overrides_environment_variable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            env_dest = root / "env-backups"
            cli_dest = root / "cli-backups"

            with mock.patch.dict(
                "os.environ", {"BACKUP_DEST": str(env_dest)}
            ):
                code, out, err = run_cli(
                    "backup", str(project), "--backup-dest", str(cli_dest)
                )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertTrue(list((cli_dest / "app").glob("manifest-*.json")))
            self.assertFalse(env_dest.exists())

    def test_backup_destination_is_required_without_environment_variable(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            code, out, err = run_cli("backup", ".")
        self.assertEqual(code, 1)
        self.assertIn("configure [backup].dest", err)

    def test_backup_treats_empty_backup_dest_environment_as_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            with mock.patch.dict("os.environ", {"BACKUP_DEST": ""}):
                code, out, err = run_cli("backup", str(project), "--dry-run")

            self.assertEqual(code, 1)
            self.assertIn("configure [backup].dest", err)

    def test_backup_dest_option_rejects_legacy_dest(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    build_parser().parse_args(
                        ["backup", ".", "--dest", "/tmp/backups"]
                    )

        self.assertEqual(exc.exception.code, 2)

    def test_backup_project_failure_reports_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "good"
            bad = root / "bad"
            for project in (good, bad):
                project.mkdir()
                (project / "pyproject.toml").write_text("", encoding="utf-8")
                (project / "main.py").write_text("x\n", encoding="utf-8")
            dest = root / "backups"
            real_backup_project = backup_module._backup_project_with_identity

            def fail_bad_project(
                project,
                archive_dir,
                patterns=(),
                archive_name=None,
                fallback_reason=None,
                exclude_git_metadata=False,
                exclude_patterns=(),
                incremental=False,
                base_name=None,
                previous=None,
                exclude_secrets=False,
                progress=False,
                encrypt_passphrase_file=None,
            ):
                if project.name == "bad":
                    raise PermissionError("access denied")
                return real_backup_project(
                    project,
                    archive_dir,
                    patterns=patterns,
                    archive_name=archive_name,
                    fallback_reason=fallback_reason,
                    exclude_git_metadata=exclude_git_metadata,
                    exclude_patterns=exclude_patterns,
                    incremental=incremental,
                    base_name=base_name,
                    previous=previous,
                    exclude_secrets=exclude_secrets,
                    progress=progress,
                    encrypt_passphrase_file=encrypt_passphrase_file,
                )

            with mock.patch(
                "proj_janitor.backup._backup_project_with_identity",
                side_effect=fail_bad_project,
            ):
                code, out, err = run_cli(
                    "backup", str(root), "--backup-dest", str(dest)
                )

            self.assertEqual(code, 1)
            self.assertIn("Backed up 1 project(s).", out)
            self.assertIn("error: bad: access denied", err)
            self.assertNotIn("Traceback", err)
            self.assertTrue(list((dest / "good").glob("manifest-*.json")))
            self.assertFalse(list((dest / "bad").glob("manifest-*.json")))
            self.assertFalse(list((dest / "bad").glob("*.tar.gz")))

    def test_restore_archive_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backups = root / "backups"

            code, _, _ = run_cli(
                "backup",
                tmp,
                "--backup-dest",
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
            run_cli("backup", tmp, "--backup-dest", str(backups))
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
            run_cli("backup", tmp, "--backup-dest", str(backups))
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
            run_cli("backup", tmp, "--backup-dest", str(backups))
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

    def test_restore_force_overwrites_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            dest = root / "backups"
            code, _out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
            )
            self.assertEqual(code, 0, err)
            manifests = sorted((dest / project.name).glob("manifest-*.json"))
            self.assertEqual(len(manifests), 1)

            out = root / "restored"
            first = out / project.name
            first.mkdir(parents=True)
            (first / "stale.txt").write_text("old\n", encoding="utf-8")

            code, _out, err = run_cli(
                "restore",
                str(manifests[0]),
                "--dest",
                str(out),
                "--force",
            )
            self.assertEqual(code, 2, err)
            self.assertIn("--yes", err)
            self.assertTrue((first / "stale.txt").is_file())

            code, out_text, err = run_cli(
                "restore",
                str(manifests[0]),
                "--dest",
                str(out),
                "--force",
                "--yes",
                "--dry-run",
                "--json",
            )
            self.assertEqual(code, 0, err)
            payload = json.loads(out_text)
            self.assertTrue(payload["projects"][0]["overwrite"])

            code, _out, err = run_cli(
                "restore",
                str(manifests[0]),
                "--dest",
                str(out),
                "--force",
                "--yes",
            )
            self.assertEqual(code, 0, err)
            self.assertFalse((first / "stale.txt").exists())
            self.assertTrue((first / "main.py").is_file() or (first / "src" / "main.py").is_file())

    def test_restore_require_dest_blocks_original_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backups = root / "backups"
            run_cli("backup", tmp, "--backup-dest", str(backups))
            manifest = next(backups.glob("*/manifest-*.json"))
            original = project.resolve()
            shutil.rmtree(project)
            config = root / "cfg.toml"
            config.write_text("[restore]\nrequire_dest = true\n", encoding="utf-8")

            code, out, err = run_cli(
                "restore",
                str(manifest),
                "--yes",
                "--config",
                str(config),
            )
            self.assertEqual(code, 1, out + err)
            self.assertIn("--dest is required", err)
            self.assertFalse(original.exists())

            dest = root / "restored"
            code, out, err = run_cli(
                "restore",
                str(manifest),
                "--dest",
                str(dest),
                "--config",
                str(config),
            )
            self.assertEqual(code, 0, err)
            self.assertTrue((dest / "app" / "main.py").exists())

            code, out, err = run_cli(
                "restore",
                str(manifest),
                "--yes",
                "--config",
                str(config),
                "--no-require-dest",
            )
            self.assertEqual(code, 0, err)
            self.assertTrue((original / "main.py").exists())

    def test_restore_original_dry_run_shows_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backups = root / "backups"
            run_cli("backup", tmp, "--backup-dest", str(backups))
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
            run_cli("backup", tmp, "--backup-dest", str(backups))
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
            run_cli("backup", tmp, "--backup-dest", str(backups))
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
            run_cli("backup", tmp, "--backup-dest", str(backups))
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
            run_cli("backup", tmp, "--backup-dest", str(backups))
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

    def test_clean_does_not_apply_partial_plan_when_root_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            missing = root / "missing"

            code, _, err = run_cli(
                "clean", str(project), str(missing), "--apply", "--yes"
            )

            self.assertEqual(code, 1)
            self.assertIn("root does not exist", err)
            self.assertTrue((project / "node_modules").exists())

    def test_clean_does_not_apply_partial_plan_when_project_walk_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "a-good"
            bad = root / "z-bad"
            for project in (good, bad):
                (project / "node_modules" / "pkg").mkdir(parents=True)
                (project / "pyproject.toml").write_text("", encoding="utf-8")
            real_plan = build_cleanup_plan

            def fail_for_bad_project(project, rule_names=None):
                if project.name == "z-bad":
                    raise PermissionError("walk denied")
                return real_plan(project, rule_names)

            with mock.patch(
                "proj_janitor.cli.build_cleanup_plan",
                side_effect=fail_for_bad_project,
            ):
                code, _, err = run_cli("clean", str(root), "--apply", "--yes")

            self.assertEqual(code, 1)
            self.assertIn("z-bad: walk denied", err)
            self.assertTrue((good / "node_modules").exists())

    def test_clean_defaults_to_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))

            code, out, _ = run_cli("clean", tmp)

            self.assertEqual(code, 0)
            self.assertIn("Dry run", out)
            self.assertTrue((project / "node_modules").exists())

    def test_clean_rules_selection_and_unknown_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            (project / "dist").mkdir()
            (project / "dist" / "bundle.js").write_text("x", encoding="utf-8")

            code, out, err = run_cli(
                "clean", str(project), "--rules", "node,build", "--json"
            )

            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual({item["rule"] for item in payload}, {"node", "build"})
            self.assertTrue((project / "node_modules").exists())
            self.assertTrue((project / "dist").exists())

            code, out, err = run_cli(
                "clean", str(project), "--rules", "unknown", "--json"
            )

            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out), [])
            self.assertIn("unknown rule", err)
            self.assertTrue((project / "node_modules").exists())
            self.assertTrue((project / "dist").exists())

            code, _, err = run_cli(
                "clean",
                str(project),
                "--rules",
                "node",
                "--apply",
                "--yes",
            )

            self.assertEqual(code, 0, err)
            self.assertFalse((project / "node_modules").exists())
            self.assertTrue((project / "dist").exists())

    def test_list_rules(self) -> None:
        code, out, _ = run_cli("clean", ".", "--list-rules")

        self.assertEqual(code, 0)
        self.assertIn("node:", out)
        self.assertIn("build:", out)

    def test_list_rules_does_not_require_root(self) -> None:
        code, out, err = run_cli("clean", "--list-rules")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("node:", out)

    def test_clean_without_root_reports_error(self) -> None:
        code, out, err = run_cli("clean")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("root", err)

    def test_clean_json_is_pure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))

            code, out, err = run_cli("clean", tmp, str(project), "--json")

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(len(payload), 1)
            self.assertNotIn("Dry run", out)
            self.assertEqual(err, "")

    def test_clean_json_apply_is_pure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))

            code, out, err = run_cli(
                "clean", tmp, "--json", "--apply", "--yes"
            )

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(len(payload), 1)
            self.assertIn("Removed 1 item", err)
            self.assertFalse((project / "node_modules").exists())

    def test_clean_json_apply_requires_yes_when_not_tty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))

            code, out, err = run_cli("clean", tmp, "--json", "--apply")

            self.assertEqual(code, 2)
            json.loads(out)
            self.assertIn("--yes", err)
            self.assertTrue((project / "node_modules").exists())

    def test_missing_root_reports_error(self) -> None:
        code, _, err = run_cli("scan", "/path/that/does/not/exist")

        self.assertEqual(code, 1)
        self.assertIn("error:", err)

    def test_backup_uses_dest_from_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            config = root / "cfg.toml"
            dest = root / "config-backups"
            config.write_text(
                f'[backup]\ndest = "{dest.as_posix()}"\n', encoding="utf-8"
            )

            with mock.patch.dict("os.environ", {}, clear=True):
                code, out, err = run_cli(
                    "backup", str(project), "--config", str(config)
                )

            self.assertEqual(code, 0, err)
            self.assertEqual(err, "")
            self.assertTrue(list((dest / "app").glob("manifest-*.json")))

    def test_archive_backup_warns_about_possible_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            dest = root / "backups"

            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
            )

            self.assertEqual(code, 0, err)
            self.assertIn("archive may include secrets", err)
            self.assertIn(".env", err)
            self.assertIn("not encrypted", err)
            self.assertTrue(list((dest / "app").glob("manifest-*.json")))

    @unittest.skipUnless(os.name == "posix", "Unix permission check")
    def test_backup_fail_on_insecure_dest_refuses_open_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            (project / "ok.txt").write_text("hello\n", encoding="utf-8")
            dest = root / "backups"
            dest.mkdir(mode=0o755)
            dest.chmod(0o755)

            code, _out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
                "--dry-run",
                "--fail-on-insecure-dest",
            )

            self.assertNotEqual(code, 0, err)
            self.assertIn("insecure permissions", err)
            self.assertIn("0755", err)

    
    @unittest.skipUnless(shutil.which("openssl"), "openssl is not available")
    def test_archive_backup_encrypt_and_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            (project / "secret.txt").write_text("payload\n", encoding="utf-8")
            dest = root / "backups"
            out = root / "restored"
            passphrase = root / "pass.txt"
            passphrase.write_text("test-passphrase\n", encoding="utf-8")

            code, _stdout, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
                "--encrypt-passphrase-file",
                str(passphrase),
            )
            self.assertEqual(code, 0, err)
            archives = list(dest.rglob("*.tar.gz.enc"))
            self.assertEqual(len(archives), 1, archives)
            self.assertFalse(list(dest.rglob("*.tar.gz")))

            manifests = list(dest.rglob("manifest-*.json"))
            self.assertEqual(len(manifests), 1)
            self.assertIn(
                "openssl-aes-256-cbc-pbkdf2",
                manifests[0].read_text(encoding="utf-8"),
            )

            code, _stdout, err = run_cli(
                "restore",
                str(manifests[0]),
                "--dest",
                str(out),
                "--yes",
                "--decrypt-passphrase-file",
                str(passphrase),
            )
            self.assertEqual(code, 0, err)
            restored = out / project.name / "secret.txt"
            self.assertTrue(restored.is_file())
            self.assertEqual(restored.read_text(encoding="utf-8"), "payload\n")

    def test_backup_check_space_dry_run_refuses_when_disk_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            (project / "ok.txt").write_text("hello\n", encoding="utf-8")
            dest = root / "backups"

            with mock.patch(
                "proj_janitor.cli.available_disk_bytes",
                return_value=0,
            ):
                code, _out, err = run_cli(
                    "backup",
                    str(project),
                    "--backup-dest",
                    str(dest),
                    "--always-archive",
                    "--dry-run",
                    "--check-space",
                )

            self.assertNotEqual(code, 0, err)
            self.assertIn("not enough free space", err)
            self.assertFalse(dest.exists())

    def test_backup_progress_dry_run_estimates_pack_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            (project / "ok.txt").write_text("hello\n", encoding="utf-8")
            dest = root / "backups"

            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
                "--dry-run",
                "--progress",
            )

            self.assertEqual(code, 0, err)
            self.assertIn("would pack", err)
            self.assertIn("files", err)
            self.assertIn("after excludes", err)
            self.assertFalse(dest.exists())

    def test_archive_backup_exclude_secrets_omits_secret_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            (project / "ok.txt").write_text("ok\n", encoding="utf-8")
            dest = root / "backups"

            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
                "--exclude-secrets",
            )

            self.assertEqual(code, 0, err)
            self.assertIn("excluding secret paths", err)
            self.assertIn(".env", err)
            manifests = list((dest / "app").glob("manifest-*.json"))
            self.assertTrue(manifests)

            archive = next((dest / "app").glob("*.tar.gz"))
            with tarfile.open(archive, "r:gz") as tf:
                names = tf.getnames()
            self.assertTrue(any(n.endswith("ok.txt") for n in names), names)
            self.assertFalse(any(n.endswith(".env") for n in names), names)

    def test_archive_backup_fail_on_secrets_refuses_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            dest = root / "backups"

            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
                "--fail-on-secrets",
            )

            self.assertEqual(code, 1, out + err)
            self.assertIn("refusing backup", err)
            self.assertIn(".env", err)
            self.assertFalse((dest / "app").exists())

    @unittest.skipUnless(os.name != "nt", "colon filenames are not creatable on Windows")
    def test_archive_backup_preflight_lists_unsafe_colon_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            nested = project / "src" / "core" / "prelink"
            nested.mkdir(parents=True)
            (nested / "link.rs:25:5").write_text("", encoding="utf-8")
            (nested / "mod.rs:1:1").write_text("", encoding="utf-8")
            dest = root / "backups"

            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
            )

            self.assertEqual(code, 1, out + err)
            self.assertIn("unsafe paths", err)
            self.assertIn("link.rs:25:5", err)
            self.assertIn("mod.rs:1:1", err)
            self.assertIn("remove or rename", err)
            self.assertFalse((dest / "app").exists())

            dry_code, dry_out, dry_err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
                "--dry-run",
            )
            self.assertEqual(dry_code, 1, dry_out + dry_err)
            self.assertIn("unsafe paths", dry_err)
            self.assertIn("link.rs:25:5", dry_err)

    def test_archive_backup_fail_on_secrets_defaults_from_config_and_cli_overrides(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            dest = root / "backups"
            config = root / "cfg.toml"
            config.write_text("[backup]\nfail_on_secrets = true\n", encoding="utf-8")

            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
                "--config",
                str(config),
            )
            self.assertEqual(code, 1, out + err)
            self.assertIn("refusing backup", err)
            self.assertFalse((dest / "app").exists())

            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dest),
                "--always-archive",
                "--config",
                str(config),
                "--no-fail-on-secrets",
            )
            self.assertEqual(code, 0, err)
            self.assertIn("archive may include secrets", err)
            self.assertTrue(list((dest / "app").glob("manifest-*.json")))

    def test_backup_dest_option_overrides_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            config = root / "cfg.toml"
            config_dest = root / "config-backups"
            cli_dest = root / "cli-backups"
            config.write_text(
                f'[backup]\ndest = "{config_dest.as_posix()}"\n', encoding="utf-8"
            )

            with mock.patch.dict("os.environ", {}, clear=True):
                code, out, err = run_cli(
                    "backup",
                    str(project),
                    "--config",
                    str(config),
                    "--backup-dest",
                    str(cli_dest),
                )

            self.assertEqual(code, 0, err)
            self.assertTrue(list((cli_dest / "app").glob("manifest-*.json")))
            self.assertFalse(config_dest.exists())

    def test_backup_excludes_patterns_from_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            config = root / "cfg.toml"
            dest = root / "config-backups"
            config.write_text(
                f'[backup]\ndest = "{dest.as_posix()}"\n'
                'exclude = ["node_modules"]\n',
                encoding="utf-8",
            )

            with mock.patch.dict("os.environ", {}, clear=True):
                code, out, err = run_cli(
                    "backup", str(project), "--config", str(config)
                )

            self.assertEqual(code, 0, err)
            archives = list((dest / "app").glob("app-*.tar.gz"))
            self.assertEqual(len(archives), 1)
            with tarfile.open(archives[0], "r:gz") as tf:
                names = tf.getnames()
            self.assertTrue(any(n.endswith("main.py") for n in names))
            self.assertTrue(any(n.endswith("pyproject.toml") for n in names))
            self.assertFalse(any("node_modules" in n for n in names))

    def test_backup_incremental_creates_delta_and_restore_reconstructs_tree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backup_dest = root / "backups"

            code, out, err = run_cli(
                "backup", str(project), "--backup-dest", str(backup_dest)
            )
            self.assertEqual(code, 0, err)
            manifests = list((backup_dest / "app").glob("manifest-*.json"))
            self.assertEqual(len(manifests), 1)

            (project / "main.py").write_text("print('bye')\n", encoding="utf-8")
            (project / "new.txt").write_text("new content", encoding="utf-8")
            (project / "node_modules" / "pkg" / "index.js").unlink()

            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(backup_dest),
                "--incremental",
            )
            self.assertEqual(code, 0, err)
            manifests = list((backup_dest / "app").glob("manifest-*.json"))
            self.assertEqual(len(manifests), 2)
            # Select the newest by modification time (the delta), not by name:
            # a same-second backup gets a "-N" counter suffix that sorts
            # lexicographically before the plain "manifest-{stamp}.json".
            delta_manifest = max(manifests, key=lambda p: p.stat().st_mtime)
            document = json.loads(delta_manifest.read_text(encoding="utf-8"))
            record = document["records"][0]
            self.assertIn("incremental", record)
            self.assertIsInstance(record["incremental"]["base"], str)
            self.assertIn("inventory", record)
            self.assertTrue(
                any(item["path"] == "new.txt" for item in record["inventory"])
            )

            restored = root / "restored"
            code, out, err = run_cli(
                "restore", str(delta_manifest), "--dest", str(restored)
            )
            self.assertEqual(code, 0, err)
            restored_project = restored / "app"
            self.assertEqual(
                (restored_project / "main.py").read_text(encoding="utf-8"),
                "print('bye')\n",
            )
            self.assertEqual(
                (restored_project / "new.txt").read_text(encoding="utf-8"),
                "new content",
            )
            self.assertFalse(
                (restored_project / "node_modules" / "pkg" / "index.js").exists()
            )
            self.assertTrue((restored_project / "pyproject.toml").exists())

    def test_retention_keeps_whole_incremental_chain_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_project(root)
            backup_dest = root / "backups"

            code, out, err = run_cli(
                "backup", str(project), "--backup-dest", str(backup_dest)
            )
            self.assertEqual(code, 0, err)
            for _ in range(3):
                code, out, err = run_cli(
                    "backup",
                    str(project),
                    "--backup-dest",
                    str(backup_dest),
                    "--incremental",
                )
                self.assertEqual(code, 0, err)
            self.assertEqual(
                len(list((backup_dest / "app").glob("manifest-*.json"))), 4
            )

            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(backup_dest),
                "--incremental",
                "--keep",
                "1",
                "--yes",
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(
                len(list((backup_dest / "app").glob("manifest-*.json"))), 5
            )

    def test_prune_uses_keep_from_config_file_and_cli_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dest = root / "backups"
            project_backup = backup_dest / "app"
            project_backup.mkdir(parents=True)
            manifest = project_backup / "manifest-20261101-100000.json"
            archive = project_backup / "app-20261101-100000.tar.gz"
            archive.write_bytes(b"old backup")
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "records": [
                            {
                                "project": "app",
                                "strategy": "archive",
                                "archive": str(archive),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = root / "cfg.toml"
            config.write_text("[prune]\nkeep = 1\n", encoding="utf-8")

            code, out, err = run_cli(
                "prune", str(backup_dest), "--config", str(config), "--json"
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["keep"], 1)

            code, out, err = run_cli(
                "prune",
                str(backup_dest),
                "--config",
                str(config),
                "--keep",
                "5",
                "--json",
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["keep"], 5)

    def test_prune_requires_keep_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dest = root / "backups"
            (backup_dest / "app").mkdir(parents=True)

            code, out, err = run_cli("prune", str(backup_dest))
            self.assertEqual(code, 1)
            self.assertIn("configure [prune].keep", err)


@unittest.skipUnless(GIT_AVAILABLE, "git is not available")
class GitCliTests(unittest.TestCase):
    def test_credentialed_submodule_url_blocks_automatic_archive(self) -> None:
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

            dry_dest = root / "dry-backups"
            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(dry_dest),
                "--dry-run",
            )
            self.assertEqual(code, 1)
            self.assertIn(
                "[error] app: submodule URL contains credentials or query data",
                out,
            )
            self.assertNotIn("submodule-secret", out + err)
            self.assertNotIn(".tar.gz", out)
            self.assertNotIn("Manifests would be", out)
            self.assertFalse(dry_dest.exists())

            backup_dest = root / "backups"
            code, out, err = run_cli(
                "backup",
                str(project),
                "--backup-dest",
                str(backup_dest),
            )
            self.assertEqual(code, 1)
            self.assertIn("submodule URL contains credentials or query data", err)
            self.assertNotIn("submodule-secret", out + err)
            self.assertFalse((backup_dest / "app").exists())

    def test_backup_dry_run_shows_git_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_pushed_repo(root)
            dest = root / "backups"

            code, out, _ = run_cli(
                "backup",
                str(root),
                "--backup-dest",
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
                "--backup-dest",
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
                "--backup-dest",
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
                "--backup-dest",
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

    def test_verify_check_remote_reports_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            remote = root / "app-remote.git"
            head = git(project, "rev-parse", "HEAD")
            manifest = write_git_manifest(root, project, remote, head)

            code, out, err = run_cli("verify", str(manifest), "--check-remote")

            self.assertEqual(code, 0, err)
            self.assertIn(
                f"commit {head[:12]} is at refs/heads/main on {remote}", out
            )

            code, out, err = run_cli(
                "verify", str(manifest), "--check-remote", "--json"
            )

            self.assertEqual(code, 0, err)
            result = json.loads(out)["projects"][0]
            self.assertEqual(result["status"], "remote-verified")
            self.assertEqual(result["remote"]["refs"], ["refs/heads/main"])

    def test_verify_check_remote_defaults_from_config_and_cli_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            remote = root / "app-remote.git"
            head = git(project, "rev-parse", "HEAD")
            manifest = write_git_manifest(root, project, remote, head)
            config = root / "proj.toml"
            config.write_text("[verify]\ncheck_remote = true\n", encoding="utf-8")

            code, out, err = run_cli(
                "verify", str(manifest), "--config", str(config), "--json"
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(
                json.loads(out)["projects"][0]["status"], "remote-verified"
            )

            code, out, err = run_cli(
                "verify",
                str(manifest),
                "--config",
                str(config),
                "--no-check-remote",
                "--json",
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(
                json.loads(out)["projects"][0]["status"], "metadata-only"
            )

    def test_verify_check_remote_exits_one_for_unreachable_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            head = git(project, "rev-parse", "HEAD")
            manifest = write_git_manifest(
                root, project, root / "gone.git", head
            )

            code, out, err = run_cli("verify", str(manifest), "--check-remote")

            self.assertEqual(code, 1)
            self.assertIn(f"remote ls-remote-failed for {root / 'gone.git'}", out)
            self.assertIn(
                "error: one or more remotes or LFS objects could not be verified",
                err,
            )

            code, out, err = run_cli(
                "verify", str(manifest), "--check-remote", "--json"
            )

            self.assertEqual(code, 1)
            self.assertEqual(
                json.loads(out)["projects"][0]["status"], "remote-unreachable"
            )
            self.assertIn(
                "one or more remotes or LFS objects could not be verified", err
            )

    def test_restore_check_remote_blocks_unreachable_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            head = git(project, "rev-parse", "HEAD")
            manifest = write_git_manifest(
                root, project, root / "gone.git", head
            )
            restored = root / "restored"

            code, out, err = run_cli(
                "restore",
                str(manifest),
                "--dest",
                str(restored),
                "--check-remote",
            )

            self.assertEqual(code, 1)
            self.assertIn(
                f"cannot restore {project.name}: remote {root / 'gone.git'} "
                "is unreachable",
                err,
            )
            self.assertFalse((restored / "app").exists())
            self.assertEqual(out, "")

    def test_restore_check_remote_proceeds_when_remote_reachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_pushed_repo(root)
            head = git(project, "rev-parse", "HEAD")
            manifest = write_git_manifest(
                root, project, root / "app-remote.git", head
            )
            restored = root / "restored"

            code, out, err = run_cli(
                "restore",
                str(manifest),
                "--dest",
                str(restored),
                "--check-remote",
            )

            self.assertEqual(code, 0)
            self.assertIn("Restored 1 project(s)", out)
            self.assertEqual(err, "")
            self.assertEqual(
                git(restored / "app", "rev-parse", "HEAD"), head
            )


if __name__ == "__main__":
    unittest.main()
