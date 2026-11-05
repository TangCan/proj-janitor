use anyhow::{Context, Result};
use serde::Deserialize;
use std::path::{Path, PathBuf};

const CONFIG_NAME: &str = ".proj-janitor.toml";

/// User configuration loaded from a `.proj-janitor.toml` file.
///
/// Provides defaults for command options. Command-line flags take precedence
/// over environment variables, which take precedence over this file.
#[derive(Deserialize, Default, Debug)]
#[serde(deny_unknown_fields)]
pub(crate) struct Config {
    #[serde(default)]
    pub(crate) backup: BackupConfig,
    #[serde(default)]
    pub(crate) prune: PruneConfig,
}

#[derive(Deserialize, Default, Debug)]
#[serde(deny_unknown_fields)]
pub(crate) struct BackupConfig {
    /// Default destination for `backup --backup-dest`.
    #[serde(default)]
    pub(crate) dest: Option<PathBuf>,
}

#[derive(Deserialize, Default, Debug)]
#[serde(deny_unknown_fields)]
pub(crate) struct PruneConfig {
    /// Default retention count for `prune --keep`.
    #[serde(default)]
    pub(crate) keep: Option<usize>,
}

/// Load configuration from an explicit path, or discover `.proj-janitor.toml`
/// by walking up from the current directory. Returns `Ok(None)` when no config
/// file is present and no explicit path was given.
pub(crate) fn discover_config(explicit: Option<&Path>) -> Result<Option<Config>> {
    let path = match explicit {
        Some(path) => Some(path.to_path_buf()),
        None => find_config(&std::env::current_dir()?),
    };
    let Some(path) = path else {
        return Ok(None);
    };
    let text = std::fs::read_to_string(&path)
        .with_context(|| format!("cannot read config {}", path.display()))?;
    let config: Config =
        toml::from_str(&text).with_context(|| format!("invalid config {}", path.display()))?;
    Ok(Some(config))
}

fn find_config(start: &Path) -> Option<PathBuf> {
    start.ancestors().find_map(|dir| {
        let candidate = dir.join(CONFIG_NAME);
        candidate.is_file().then_some(candidate)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicU32, Ordering};

    static NEXT_TEMP: AtomicU32 = AtomicU32::new(0);

    struct TempDir(PathBuf);
    impl TempDir {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "projjan-config-test-{}-{}",
                std::process::id(),
                NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
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

    fn write_config(dir: &Path, content: &str) -> PathBuf {
        let path = dir.join(CONFIG_NAME);
        fs::write(&path, content).unwrap();
        path
    }

    #[test]
    fn explicit_path_loads_backup_dest() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\ndest = \"/tmp/backups\"\n");
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert_eq!(config.backup.dest, Some(PathBuf::from("/tmp/backups")));
        assert_eq!(config.prune.keep, None);
    }

    #[test]
    fn explicit_path_loads_prune_keep() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[prune]\nkeep = 7\n");
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert_eq!(config.prune.keep, Some(7));
        assert_eq!(config.backup.dest, None);
    }

    #[test]
    fn explicit_path_missing_is_error() {
        let temp = TempDir::new();
        let missing = temp.0.join("nope.toml");
        assert!(discover_config(Some(&missing)).is_err());
    }

    #[test]
    fn invalid_toml_is_error() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "backup = [not toml\n");
        let err = discover_config(Some(&path)).unwrap_err().to_string();
        assert!(err.contains("invalid config"));
    }

    #[test]
    fn unknown_field_is_rejected() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\nunknown = 1\n");
        assert!(discover_config(Some(&path)).is_err());
    }

    #[test]
    fn no_config_anywhere_returns_none() {
        assert!(discover_config(None).unwrap().is_none());
    }

    #[test]
    fn find_config_walks_up_ancestors() {
        let temp = TempDir::new();
        let nested = temp.0.join("a/b/c");
        fs::create_dir_all(&nested).unwrap();
        write_config(temp.0.as_path(), "[prune]\nkeep = 3\n");
        let found = find_config(&nested).unwrap();
        assert_eq!(found, temp.0.join(CONFIG_NAME));
    }
}
