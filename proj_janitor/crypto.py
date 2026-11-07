"""Optional OpenSSL-backed archive encryption helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

OPENSSL_AES_256_CBC_PBKDF2 = "openssl-aes-256-cbc-pbkdf2"


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


def archive_is_encrypted(path: Path, encryption: str | None) -> bool:
    name = path.name
    return encryption == OPENSSL_AES_256_CBC_PBKDF2 or name.endswith(
        (".tar.gz.enc", ".bundle.enc")
    )


def encrypted_sibling(path: Path) -> Path:
    return Path(str(path) + ".enc")


def validate_passphrase_file(path: Path) -> None:
    target = path.expanduser()
    if not target.is_file():
        raise OSError(f"passphrase path is not a file: {target}")
    if target.stat().st_size <= 0:
        raise OSError(f"passphrase file is empty: {target}")


def openssl_encrypt_file(
    input_path: Path, output_path: Path, passphrase_file: Path
) -> tuple[str, int]:
    from proj_janitor.backup import sha256_file

    if not openssl_available():
        raise OSError(
            "openssl is required for archive encryption (not found in PATH)"
        )
    validate_passphrase_file(passphrase_file)
    if output_path.exists():
        raise OSError(f"encrypted archive already exists: {output_path}")
    pass_arg = f"file:{passphrase_file.expanduser()}"
    completed = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-salt",
            "-pass",
            pass_arg,
            "-in",
            str(input_path),
            "-out",
            str(output_path),
        ],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        raise OSError(f"openssl encryption failed for {input_path}")
    return sha256_file(output_path), output_path.stat().st_size


def openssl_decrypt_to_temp(input_path: Path, passphrase_file: Path) -> Path:
    if not openssl_available():
        raise OSError(
            "openssl is required to decrypt encrypted archives (not found in PATH)"
        )
    validate_passphrase_file(passphrase_file)
    parent = input_path.parent if input_path.parent.exists() else Path.cwd()
    temp_dir = Path(
        tempfile.mkdtemp(prefix=".proj-janitor-decrypt-", dir=parent)
    )
    try:
        if os.name == "posix":
            os.chmod(temp_dir, 0o700)
    except OSError:
        pass
    output = temp_dir / "archive.tar.gz"
    pass_arg = f"file:{passphrase_file.expanduser()}"
    completed = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-pass",
            pass_arg,
            "-in",
            str(input_path),
            "-out",
            str(output),
        ],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        cleanup_decrypt_temp(output)
        raise OSError(f"openssl decryption failed for {input_path}")
    return output


def cleanup_decrypt_temp(decrypted: Path) -> None:
    parent = decrypted.parent
    try:
        if decrypted.exists():
            decrypted.unlink()
    except OSError:
        pass
    try:
        if parent.exists():
            parent.rmdir()
    except OSError:
        shutil.rmtree(parent, ignore_errors=True)
