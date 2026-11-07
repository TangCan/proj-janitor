//! Heuristic detection of common secret filenames that may land in archives.
//!
//! These checks are intentionally conservative path/name matchers. They do not
//! inspect file contents. By default they surface warnings; `--fail-on-secrets`
//! refuses the archive backup, and `--exclude-secrets` skips matching paths
//! when packing (R-05).

use crate::archive::{archive_excluded, build_exclude_globset};
use anyhow::Context;
use std::path::Path;
use walkdir::WalkDir;

const MAX_REPORTED: usize = 12;

/// Return project-relative paths (posix separators) that look like common secrets.
pub(crate) fn find_possible_secret_paths(
    project: &Path,
    include_artifacts: bool,
    exclude_git_metadata: bool,
    exclude: &[String],
    exclude_secrets: bool,
) -> anyhow::Result<Vec<String>> {
    let exclude_set = build_exclude_globset(exclude)?;
    let mut hits = Vec::new();
    for entry in WalkDir::new(project)
        .follow_links(false)
        .into_iter()
        .filter_entry(|entry| {
            !archive_excluded(
                entry.path(),
                project,
                include_artifacts,
                exclude_git_metadata,
                &exclude_set,
                exclude_secrets,
            )
        })
    {
        let entry = entry.with_context(|| format!("cannot walk {}", project.display()))?;
        if !entry.file_type().is_file() && !entry.file_type().is_symlink() {
            continue;
        }
        let Ok(relative) = entry.path().strip_prefix(project) else {
            continue;
        };
        let rel = relative
            .components()
            .map(|component| component.as_os_str().to_string_lossy())
            .collect::<Vec<_>>()
            .join("/");
        if path_looks_like_secret(&rel) {
            hits.push(rel);
        }
    }
    hits.sort();
    hits.dedup();
    Ok(hits)
}

pub(crate) fn format_secret_warning(project: &str, secrets: &[String]) -> String {
    let shown = &secrets[..secrets.len().min(MAX_REPORTED)];
    let mut list = shown.join(", ");
    if secrets.len() > MAX_REPORTED {
        list.push_str(&format!(" (+{} more)", secrets.len() - MAX_REPORTED));
    }
    format!("warning: {project}: archive may include secrets ({list}); archives are not encrypted")
}

fn format_secret_list(secrets: &[String]) -> String {
    let shown = &secrets[..secrets.len().min(MAX_REPORTED)];
    let mut list = shown.join(", ");
    if secrets.len() > MAX_REPORTED {
        list.push_str(&format!(" (+{} more)", secrets.len() - MAX_REPORTED));
    }
    list
}

/// Body for `anyhow` / CLI error paths that already prefix `error: {project}:`.
pub(crate) fn format_secret_refusal_detail(secrets: &[String]) -> String {
    format!(
        "archive would include secrets ({}); refusing backup (--fail-on-secrets)",
        format_secret_list(secrets)
    )
}

pub(crate) fn format_secret_refusal(project: &str, secrets: &[String]) -> String {
    format!(
        "error: {project}: {}",
        format_secret_refusal_detail(secrets)
    )
}

pub(crate) fn format_secret_exclusion(project: &str, secrets: &[String]) -> String {
    format!(
        "note: {project}: excluding secret paths from archive ({})",
        format_secret_list(secrets)
    )
}

pub(crate) fn path_looks_like_secret(rel: &str) -> bool {
    let rel = rel.replace('\\', "/");
    if rel == ".aws/credentials" || rel.ends_with("/.aws/credentials") {
        return true;
    }
    let name = rel.rsplit('/').next().unwrap_or(rel.as_str());
    let lower = name.to_ascii_lowercase();
    match name {
        ".env" | ".netrc" | ".htpasswd" | "id_rsa" | "id_dsa" | "id_ecdsa" | "id_ed25519"
        | "id_ecdsa_sk" | "id_ed25519_sk" => return true,
        _ => {}
    }
    match lower.as_str() {
        "credentials.json" | "credentials.csv" | "service-account.json" | "serviceaccount.json" => {
            return true;
        }
        _ => {}
    }
    if let Some(rest) = name.strip_prefix(".env.") {
        let rest = rest.to_ascii_lowercase();
        if !matches!(
            rest.as_str(),
            "example" | "sample" | "template" | "dist" | "default"
        ) {
            return true;
        }
    }
    for extension in [
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".jks",
        ".keystore",
        ".tfstate",
    ] {
        if lower.ends_with(extension) {
            return true;
        }
    }
    lower.ends_with(".tfstate.backup")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU32, Ordering};

    static NEXT: AtomicU32 = AtomicU32::new(0);

    struct TempDir(PathBuf);
    impl TempDir {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "projjan-secrets-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn classifies_common_secret_names() {
        assert!(path_looks_like_secret(".env"));
        assert!(path_looks_like_secret("config/.env.production"));
        assert!(!path_looks_like_secret(".env.example"));
        assert!(path_looks_like_secret("certs/server.pem"));
        assert!(path_looks_like_secret(".aws/credentials"));
        assert!(path_looks_like_secret("nested/.aws/credentials"));
        assert!(path_looks_like_secret("id_ed25519"));
        assert!(!path_looks_like_secret("README.md"));
        assert!(!path_looks_like_secret("src/main.rs"));
    }

    #[test]
    fn finds_secrets_while_honoring_excludes() {
        let temp = TempDir::new();
        fs::write(temp.0.join(".env"), "SECRET=1\n").unwrap();
        fs::write(temp.0.join("ok.txt"), "ok\n").unwrap();
        fs::create_dir_all(temp.0.join("nested")).unwrap();
        fs::write(temp.0.join("nested/id_rsa"), "key\n").unwrap();
        fs::create_dir_all(temp.0.join("skip")).unwrap();
        fs::write(temp.0.join("skip/.env"), "x\n").unwrap();

        let hits =
            find_possible_secret_paths(&temp.0, false, false, &["skip".into()], false).unwrap();
        assert_eq!(hits, vec![".env".to_owned(), "nested/id_rsa".to_owned()]);
    }
}
