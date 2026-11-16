"""Unit tests for OpenSSL path helpers."""

from __future__ import annotations

import unittest
from pathlib import Path

from proj_janitor.crypto import openssl_compatible_path


class OpensslPathTests(unittest.TestCase):
    def test_strips_windows_verbatim_prefixes(self) -> None:
        self.assertEqual(
            openssl_compatible_path(Path(r"\\?\C:\temp\archive.tar.gz")),
            Path(r"C:\temp\archive.tar.gz"),
        )
        self.assertEqual(
            openssl_compatible_path(Path(r"\\?\UNC\server\share\pass.txt")),
            Path(r"\\server\share\pass.txt"),
        )

    def test_leaves_ordinary_paths_unchanged(self) -> None:
        path = Path("/tmp/archive.tar.gz")
        self.assertEqual(openssl_compatible_path(path), path)


if __name__ == "__main__":
    unittest.main()
