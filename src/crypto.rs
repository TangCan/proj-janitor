use anyhow::Context;
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::Instant,
};

use crate::archive::{create_private_temp_dir, sha256};

/// Manifest / CLI identifier for OpenSSL AES-256-CBC with PBKDF2.
pub(crate) const OPENSSL_AES_256_CBC_PBKDF2: &str = "openssl-aes-256-cbc-pbkdf2";

pub(crate) fn openssl_available() -> bool {
    Command::new("openssl")
        .arg("version")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

pub(crate) fn archive_is_encrypted(path: &Path, encryption: Option<&str>) -> bool {
    encryption == Some(OPENSSL_AES_256_CBC_PBKDF2)
        || path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.ends_with(".tar.gz.enc") || name.ends_with(".bundle.enc"))
}

pub(crate) fn encrypted_sibling(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(".enc");
    PathBuf::from(name)
}

pub(crate) fn validate_passphrase_file(path: &Path) -> anyhow::Result<()> {
    let meta = fs::metadata(path)
        .with_context(|| format!("cannot read passphrase file {}", path.display()))?;
    anyhow::ensure!(
        meta.is_file(),
        "passphrase path is not a file: {}",
        path.display()
    );
    anyhow::ensure!(
        meta.len() > 0,
        "passphrase file is empty: {}",
        path.display()
    );
    Ok(())
}

/// Encrypt `input` to `output` with openssl; returns ciphertext sha256 and size.
pub(crate) fn openssl_encrypt_file(
    input: &Path,
    output: &Path,
    passphrase_file: &Path,
    progress_label: Option<&str>,
) -> anyhow::Result<(String, u64)> {
    anyhow::ensure!(
        openssl_available(),
        "openssl is required for archive encryption (not found in PATH)"
    );
    validate_passphrase_file(passphrase_file)?;
    if output.exists() {
        anyhow::bail!("encrypted archive already exists: {}", output.display());
    }
    let progress_started = progress_label.map(|_| Instant::now());
    if let Some(name) = progress_label {
        eprintln!("progress: {name}: encrypting with openssl");
    }
    let pass_arg = format!("file:{}", passphrase_file.display());
    let status = Command::new("openssl")
        .args([
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-salt",
            "-pass",
            &pass_arg,
            "-in",
        ])
        .arg(input)
        .arg("-out")
        .arg(output)
        .status()
        .context("failed to run openssl enc")?;
    if !status.success() {
        let _ = fs::remove_file(output);
        anyhow::bail!("openssl encryption failed for {}", input.display());
    }
    if let Some(name) = progress_label {
        let elapsed = progress_started
            .map(|started| crate::format_elapsed(started.elapsed()))
            .unwrap_or_else(|| "0s".into());
        eprintln!("progress: {name}: openssl encrypt done ({elapsed})");
    }
    let meta = fs::metadata(output)?;
    let digest = sha256(output)?;
    Ok((digest, meta.len()))
}

/// Decrypt ciphertext to a new private temp file; caller must delete it.
pub(crate) fn openssl_decrypt_to_temp(
    input: &Path,
    passphrase_file: &Path,
    progress_label: Option<&str>,
) -> anyhow::Result<PathBuf> {
    anyhow::ensure!(
        openssl_available(),
        "openssl is required to decrypt encrypted archives (not found in PATH)"
    );
    validate_passphrase_file(passphrase_file)?;
    let parent = input
        .parent()
        .filter(|path| path.exists())
        .unwrap_or_else(|| Path::new("."));
    let temp_dir = create_private_temp_dir(parent)?;
    let output = temp_dir.join("archive.tar.gz");
    let progress_started = progress_label.map(|_| Instant::now());
    if let Some(name) = progress_label {
        eprintln!("progress: {name}: decrypting with openssl");
    }
    let pass_arg = format!("file:{}", passphrase_file.display());
    let status = Command::new("openssl")
        .args([
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-pass",
            &pass_arg,
            "-in",
        ])
        .arg(input)
        .arg("-out")
        .arg(&output)
        .status()
        .context("failed to run openssl enc -d")?;
    if !status.success() {
        let _ = fs::remove_file(&output);
        let _ = fs::remove_dir(&temp_dir);
        anyhow::bail!("openssl decryption failed for {}", input.display());
    }
    if let Some(name) = progress_label {
        let elapsed = progress_started
            .map(|started| crate::format_elapsed(started.elapsed()))
            .unwrap_or_else(|| "0s".into());
        eprintln!("progress: {name}: openssl decrypt done ({elapsed})");
    }
    Ok(output)
}

pub(crate) fn cleanup_decrypt_temp(decrypted: &Path) {
    let _ = fs::remove_file(decrypted);
    if let Some(parent) = decrypted.parent() {
        let _ = fs::remove_dir(parent);
    }
}
