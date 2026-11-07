from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from proj_janitor.config import (
    CONFIG_NAME,
    ConfigError,
    load_config,
    resolve_check_remote,
)


class ConfigTests(unittest.TestCase):
    def test_loads_backup_dest_and_prune_keep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text(
                '[backup]\ndest = "/tmp/backups"\n[prune]\nkeep = 5\n',
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertIsNotNone(config)
            assert config is not None
            self.assertEqual(config.backup.dest, Path("/tmp/backups"))
            self.assertEqual(config.prune.keep, 5)

    def test_omitted_sections_default_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[backup]\ndest = \"/tmp/backups\"\n", encoding="utf-8")
            config = load_config(path)
            assert config is not None
            self.assertEqual(config.prune.keep, None)

    def test_missing_explicit_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.toml"
            with self.assertRaisesRegex(ConfigError, "no such file"):
                load_config(missing)

    def test_invalid_toml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("backup = [not toml\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "invalid config"):
                load_config(path)

    def test_unknown_top_level_key_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("bogus = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "unknown top-level"):
                load_config(path)

    def test_unknown_backup_key_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[backup]\nbad = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "unknown \\[backup\\] key"):
                load_config(path)

    def test_non_positive_keep_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[prune]\nkeep = 0\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "positive integer"):
                load_config(path)

    def test_loads_backup_keep_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[backup]\nkeep = 4\n", encoding="utf-8")
            config = load_config(path)
            assert config is not None
            self.assertEqual(config.backup.keep, 4)
            self.assertIsNone(config.backup.dest)

    def test_non_positive_backup_keep_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[backup]\nkeep = 0\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "positive integer"):
                load_config(path)

    def test_loads_backup_exclude_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text(
                '[backup]\nexclude = ["node_modules", "*.log"]\n', encoding="utf-8"
            )
            config = load_config(path)
            assert config is not None
            self.assertEqual(config.backup.exclude, ("node_modules", "*.log"))
            self.assertIsNone(config.backup.dest)
            self.assertIsNone(config.backup.keep)

    def test_exclude_non_list_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text('[backup]\nexclude = "node_modules"\n', encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "must be a list of strings"):
                load_config(path)

    def test_exclude_empty_entry_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text('[backup]\nexclude = [""]\n', encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "must not be empty"):
                load_config(path)

    def test_exclude_brace_expansion_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text('[backup]\nexclude = ["{a,b}"]\n', encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "brace expansion"):
                load_config(path)

    def test_loads_backup_incremental_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[backup]\nincremental = true\n", encoding="utf-8")
            config = load_config(path)
            assert config is not None
            self.assertTrue(config.backup.incremental)

    def test_backup_incremental_defaults_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[backup]\n", encoding="utf-8")
            config = load_config(path)
            assert config is not None
            self.assertFalse(config.backup.incremental)

    def test_loads_backup_fail_on_secrets_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[backup]\nfail_on_secrets = true\n", encoding="utf-8")
            config = load_config(path)
            assert config is not None
            self.assertTrue(config.backup.fail_on_secrets)

    def test_backup_fail_on_secrets_defaults_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[backup]\n", encoding="utf-8")
            config = load_config(path)
            assert config is not None
            self.assertFalse(config.backup.fail_on_secrets)

    def test_loads_backup_exclude_secrets_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[backup]\nexclude_secrets = true\n", encoding="utf-8")
            config = load_config(path)
            assert config is not None
            self.assertTrue(config.backup.exclude_secrets)

    def test_loads_backup_progress_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[backup]\nprogress = true\n", encoding="utf-8")
            config = load_config(path)
            assert config is not None
            self.assertTrue(config.backup.progress)

    def test_loads_backup_check_space_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[backup]\ncheck_space = true\n", encoding="utf-8")
            config = load_config(path)
            assert config is not None
            self.assertTrue(config.backup.check_space)

    def test_loads_backup_fail_on_insecure_dest_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text(
                "[backup]\nfail_on_insecure_dest = true\n", encoding="utf-8"
            )
            config = load_config(path)
            assert config is not None
            self.assertTrue(config.backup.fail_on_insecure_dest)

    def test_discovers_config_walking_up_from_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            (root / CONFIG_NAME).write_text("[prune]\nkeep = 3\n", encoding="utf-8")
            with mock.patch.object(Path, "cwd", return_value=nested):
                config = load_config()
            assert config is not None
            self.assertEqual(config.prune.keep, 3)

    def test_returns_none_when_no_config_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(Path, "cwd", return_value=root):
                config = load_config()
            self.assertIsNone(config)

    def test_loads_verify_and_restore_check_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text(
                "[verify]\ncheck_remote = true\n[restore]\ncheck_remote = true\n",
                encoding="utf-8",
            )
            config = load_config(path)
            assert config is not None
            self.assertTrue(config.verify.check_remote)
            self.assertTrue(config.restore.check_remote)
            self.assertFalse(config.restore.require_dest)
            self.assertTrue(resolve_check_remote(None, True))
            self.assertFalse(resolve_check_remote(False, True))
            self.assertTrue(resolve_check_remote(True, False))
            self.assertFalse(resolve_check_remote(None, False))

    def test_loads_restore_require_dest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[restore]\nrequire_dest = true\n", encoding="utf-8")
            config = load_config(path)
            assert config is not None
            self.assertTrue(config.restore.require_dest)

    def test_loads_restore_continue_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".proj-janitor.toml"
            path.write_text(
                "[restore]\ncontinue_on_error = true\n", encoding="utf-8"
            )
            config = load_config(path)
            assert config is not None
            self.assertTrue(config.restore.continue_on_error)

    def test_loads_restore_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[restore]\nforce = true\n", encoding="utf-8")
            config = load_config(path)
            assert config is not None
            self.assertTrue(config.restore.force)
            self.assertFalse(config.restore.check_remote)

    def test_unknown_verify_key_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text("[verify]\nbogus = true\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, r"unknown \[verify\] key"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
