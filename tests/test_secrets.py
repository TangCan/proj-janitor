from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from proj_janitor.secrets import (
    find_possible_secret_paths,
    format_secret_warning,
    path_looks_like_secret,
)


class SecretPathTests(unittest.TestCase):
    def test_classifies_common_secret_names(self) -> None:
        self.assertTrue(path_looks_like_secret(".env"))
        self.assertTrue(path_looks_like_secret("config/.env.production"))
        self.assertFalse(path_looks_like_secret(".env.example"))
        self.assertTrue(path_looks_like_secret("certs/server.pem"))
        self.assertTrue(path_looks_like_secret(".aws/credentials"))
        self.assertTrue(path_looks_like_secret("nested/.aws/credentials"))
        self.assertTrue(path_looks_like_secret("id_ed25519"))
        self.assertFalse(path_looks_like_secret("README.md"))
        self.assertFalse(path_looks_like_secret("src/main.rs"))

    def test_finds_secrets_while_honoring_excludes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
            (root / "ok.txt").write_text("ok\n", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "id_rsa").write_text("key\n", encoding="utf-8")
            (root / "skip").mkdir()
            (root / "skip" / ".env").write_text("x\n", encoding="utf-8")
            hits = find_possible_secret_paths(root, exclude_patterns=("skip",))
            self.assertEqual(hits, [".env", "nested/id_rsa"])

    def test_warning_message_mentions_encryption(self) -> None:
        message = format_secret_warning("demo", [".env", "id_rsa"])
        self.assertIn("demo", message)
        self.assertIn(".env", message)
        self.assertIn("not encrypted", message)


if __name__ == "__main__":
    unittest.main()
