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
    #[serde(default)]
    pub(crate) verify: VerifyConfig,
    #[serde(default)]
    pub(crate) restore: RestoreConfig,
}

#[derive(Deserialize, Default, Debug)]
#[serde(deny_unknown_fields)]
pub(crate) struct BackupConfig {
    /// Default destination for `backup --backup-dest`.
    #[serde(default)]
    pub(crate) dest: Option<PathBuf>,
    /// Default retention count for `backup --keep`.
    #[serde(default)]
    pub(crate) keep: Option<usize>,
    /// Exclusion glob patterns for the archive strategy (matched per path component).
    #[serde(default)]
    pub(crate) exclude: Vec<String>,
    /// Whether to create incremental (delta) backups for the archive strategy.
    #[serde(default)]
    pub(crate) incremental: bool,
    /// Refuse archive backups when common secret paths are detected.
    #[serde(default)]
    pub(crate) fail_on_secrets: bool,
    /// Omit common secret paths from archive backups.
    #[serde(default)]
    pub(crate) exclude_secrets: bool,
    /// Emit archive packing / dry-run size progress on stderr.
    #[serde(default)]
    pub(crate) progress: bool,
    /// Refuse archive backups when free space on dest is below the estimate.
    #[serde(default)]
    pub(crate) check_space: bool,
    /// Refuse backups when the destination directory is group/world-accessible.
    #[serde(default)]
    pub(crate) fail_on_insecure_dest: bool,
}

#[derive(Deserialize, Default, Debug)]
#[serde(deny_unknown_fields)]
pub(crate) struct PruneConfig {
    /// Default retention count for `prune --keep`.
    #[serde(default)]
    pub(crate) keep: Option<usize>,
}

#[derive(Deserialize, Default, Debug)]
#[serde(deny_unknown_fields)]
pub(crate) struct VerifyConfig {
    /// Default for `verify --check-remote` when neither flag is passed.
    #[serde(default)]
    pub(crate) check_remote: bool,
}

#[derive(Deserialize, Default, Debug)]
#[serde(deny_unknown_fields)]
pub(crate) struct RestoreConfig {
    /// Default for `restore --check-remote` when neither flag is passed.
    #[serde(default)]
    pub(crate) check_remote: bool,
    /// Require `--dest` so restore never writes to manifest `source` paths.
    #[serde(default)]
    pub(crate) require_dest: bool,
}

/// Resolve an optional CLI `--flag` / `--no-flag` pair against a config default.
/// Explicit flags always win; otherwise use `config_default`.
pub(crate) fn resolve_tri_state(enable: bool, disable: bool, config_default: bool) -> bool {
    if enable {
        true
    } else if disable {
        false
    } else {
        config_default
    }
}

/// Resolve an optional CLI `--check-remote` / `--no-check-remote` pair against
/// a config default. Explicit flags always win; otherwise use `config_default`.
pub(crate) fn resolve_check_remote(
    check_remote: bool,
    no_check_remote: bool,
    config_default: bool,
) -> bool {
    resolve_tri_state(check_remote, no_check_remote, config_default)
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
    anyhow::ensure!(
        config.backup.keep.is_none_or(|keep| keep >= 1),
        "invalid config {}: [backup].keep must be a positive integer",
        path.display()
    );
    anyhow::ensure!(
        config.prune.keep.is_none_or(|keep| keep >= 1),
        "invalid config {}: [prune].keep must be a positive integer",
        path.display()
    );
    for pattern in &config.backup.exclude {
        anyhow::ensure!(
            !pattern.is_empty(),
            "invalid config {}: [backup].exclude entries must not be empty",
            path.display()
        );
        anyhow::ensure!(
            !pattern.contains('{') && !pattern.contains('}'),
            "invalid config {}: [backup].exclude brace expansion is not supported: {}",
            path.display(),
            pattern
        );
    }
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
    fn explicit_path_loads_backup_keep() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\nkeep = 5\n");
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert_eq!(config.backup.keep, Some(5));
    }

    #[test]
    fn explicit_path_loads_backup_exclude() {
        let temp = TempDir::new();
        let path = write_config(
            temp.0.as_path(),
            "[backup]\nexclude = [\"node_modules\", \"*.log\"]\n",
        );
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert_eq!(config.backup.exclude, vec!["node_modules", "*.log"]);
        assert_eq!(config.backup.dest, None);
        assert_eq!(config.backup.keep, None);
    }

    #[test]
    fn empty_exclude_entry_is_rejected() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\nexclude = [\"\"]\n");
        let err = discover_config(Some(&path)).unwrap_err().to_string();
        assert!(
            err.contains("[backup].exclude entries must not be empty"),
            "{err}"
        );
    }

    #[test]
    fn brace_expansion_is_rejected_for_exclude() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\nexclude = [\"{a,b}\"]\n");
        let err = discover_config(Some(&path)).unwrap_err().to_string();
        assert!(err.contains("brace expansion is not supported"), "{err}");
    }

    #[test]
    fn explicit_path_loads_backup_incremental() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\nincremental = true\n");
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert!(config.backup.incremental);
        assert_eq!(config.backup.dest, None);
        assert_eq!(config.backup.keep, None);
        assert_eq!(config.backup.exclude, Vec::<String>::new());
    }

    #[test]
    fn explicit_path_loads_verify_and_restore_check_remote() {
        let temp = TempDir::new();
        let path = write_config(
            temp.0.as_path(),
            "[verify]\ncheck_remote = true\n[restore]\ncheck_remote = true\n",
        );
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert!(config.verify.check_remote);
        assert!(config.restore.check_remote);
        assert!(!config.restore.require_dest);
        assert!(!resolve_check_remote(false, true, true));
        assert!(resolve_check_remote(true, false, false));
        assert!(resolve_check_remote(false, false, true));
        assert!(!resolve_check_remote(false, false, false));
    }

    #[test]
    fn explicit_path_loads_restore_require_dest() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[restore]\nrequire_dest = true\n");
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert!(config.restore.require_dest);
        assert!(!config.restore.check_remote);
    }

    #[test]
    fn incremental_defaults_to_false() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\ndest = \"/tmp/x\"\n");
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert!(!config.backup.incremental);
    }

    #[test]
    fn explicit_path_loads_backup_fail_on_secrets() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\nfail_on_secrets = true\n");
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert!(config.backup.fail_on_secrets);
        assert!(!resolve_tri_state(false, true, true));
        assert!(resolve_tri_state(true, false, false));
        assert!(resolve_tri_state(false, false, true));
        assert!(!resolve_tri_state(false, false, false));
    }

    #[test]
    fn fail_on_secrets_defaults_to_false() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\ndest = \"/tmp/x\"\n");
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert!(!config.backup.fail_on_secrets);
    }

    #[test]
    fn explicit_path_loads_backup_exclude_secrets() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\nexclude_secrets = true\n");
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert!(config.backup.exclude_secrets);
    }

    #[test]
    fn explicit_path_loads_backup_progress() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\nprogress = true\n");
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert!(config.backup.progress);
    }

    #[test]
    fn explicit_path_loads_backup_check_space() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\ncheck_space = true\n");
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert!(config.backup.check_space);
    }

    #[test]
    fn explicit_path_loads_backup_fail_on_insecure_dest() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\nfail_on_insecure_dest = true\n");
        let config = discover_config(Some(&path)).unwrap().unwrap();
        assert!(config.backup.fail_on_insecure_dest);
    }

    #[test]
    fn zero_keep_is_rejected_for_backup_and_prune() {
        let temp = TempDir::new();
        let path = write_config(temp.0.as_path(), "[backup]\nkeep = 0\n");
        let err = discover_config(Some(&path)).unwrap_err().to_string();
        assert!(
            err.contains("[backup].keep must be a positive integer"),
            "{err}"
        );
        let path = write_config(temp.0.as_path(), "[prune]\nkeep = 0\n");
        let err = discover_config(Some(&path)).unwrap_err().to_string();
        assert!(
            err.contains("[prune].keep must be a positive integer"),
            "{err}"
        );
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
