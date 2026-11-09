use clap::{Parser, Subcommand};
use globset::{GlobBuilder, GlobMatcher};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    env, fs,
    path::{Component, Path, PathBuf},
    sync::OnceLock,
};
mod archive;
mod backup;
mod clean;
mod config;
mod crypto;
mod init;
mod project;
mod restore;
mod retention;
mod secrets;
use backup::backup_cmd;
use clean::clean_cmd;
use config::{discover_config, resolve_check_remote, resolve_tri_state};
use restore::restore_cmd;

pub(crate) const RULES: &[(&str, &[&str])] = &[
    (
        "python-cache",
        &[
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "*.pyc",
            ".coverage",
            "htmlcov",
        ],
    ),
    (
        "python-env",
        &[".venv", "venv", ".tox", ".nox", "*.egg-info"],
    ),
    (
        "node",
        &[
            "node_modules",
            ".next",
            ".nuxt",
            ".svelte-kit",
            ".turbo",
            ".parcel-cache",
            "coverage",
        ],
    ),
    (
        "build",
        &[
            "build",
            "dist",
            "dist-standalone",
            "out",
            "target",
            "CMakeFiles",
            ".gradle",
        ],
    ),
    ("editor", &[".cache", ".DS_Store", "Thumbs.db"]),
];

static ARTIFACT_GLOBS: OnceLock<Vec<GlobMatcher>> = OnceLock::new();

#[derive(Parser)]
#[command(
    name = "projjan",
    version,
    about = "Scan, back up, restore, prune, and clean old projects safely"
)]
struct Cli {
    #[command(subcommand)]
    command: Action,
}

#[derive(Subcommand)]
enum Action {
    /// Discover projects and report size and git state
    Scan {
        /// Project roots or parent directories to scan
        #[arg(required = true)]
        roots: Vec<PathBuf>,
        /// Include directories without a known project marker
        #[arg(long)]
        all: bool,
        /// Print per-project scan progress with elapsed time
        #[arg(long, conflicts_with = "no_progress")]
        progress: bool,
        /// Disable scan progress even when [scan].progress is true
        #[arg(long = "no-progress", conflicts_with = "progress")]
        no_progress: bool,
        /// Path to a .proj-janitor.toml config file
        #[arg(long)]
        config: Option<PathBuf>,
        /// Emit JSON
        #[arg(long)]
        json: bool,
    },
    /// Record Git remotes or archive projects into a destination
    Backup {
        /// Project roots or parent directories to back up
        #[arg(required = true)]
        roots: Vec<PathBuf>,
        /// Backup root directory (or [backup].dest)
        #[arg(long, env = "BACKUP_DEST", hide_env_values = true)]
        backup_dest: Option<PathBuf>,
        /// Path to a .proj-janitor.toml config file
        #[arg(long)]
        config: Option<PathBuf>,
        /// After success, retain only N newest manifests per project [backup].keep
        #[arg(long, value_parser = parse_positive_usize, value_name = "N")]
        keep: Option<usize>,
        /// Skip confirmation when deleting old backups via --keep
        #[arg(long)]
        yes: bool,
        /// Show the plan without writing archives
        #[arg(long)]
        dry_run: bool,
        /// Archive build artifacts and dependency caches too
        #[arg(long)]
        include_artifacts: bool,
        /// Archive every project even when it can be recovered from GitHub
        #[arg(long)]
        always_archive: bool,
        /// Omit .git from archives; restored trees have no Git history
        #[arg(long)]
        exclude_git_metadata: bool,
        /// Record a delta snapshot (changed/new files) instead of a full archive
        #[arg(long)]
        incremental: bool,
        /// Refuse archive backups when common secret paths are detected
        #[arg(long, conflicts_with = "no_fail_on_secrets")]
        fail_on_secrets: bool,
        /// Warn about secret paths but still archive (overrides config)
        #[arg(long = "no-fail-on-secrets", conflicts_with = "fail_on_secrets")]
        no_fail_on_secrets: bool,
        /// Omit common secret paths from archive backups
        #[arg(long, conflicts_with = "no_exclude_secrets")]
        exclude_secrets: bool,
        /// Include secret paths in archives and only warn (overrides config)
        #[arg(long = "no-exclude-secrets", conflicts_with = "exclude_secrets")]
        no_exclude_secrets: bool,
        /// Print per-project and packing progress with elapsed time; with --dry-run, estimate size after excludes
        #[arg(long, conflicts_with = "no_progress")]
        progress: bool,
        /// Disable progress output even when [backup].progress is true
        #[arg(long = "no-progress", conflicts_with = "progress")]
        no_progress: bool,
        /// Refuse archive backups when free space is below the size estimate
        #[arg(long, conflicts_with = "no_check_space")]
        check_space: bool,
        /// Skip free-space preflight even when [backup].check_space is true
        #[arg(long = "no-check-space", conflicts_with = "check_space")]
        no_check_space: bool,
        /// Refuse backup when the destination is group/world-accessible (Unix)
        #[arg(long, conflicts_with = "no_fail_on_insecure_dest")]
        fail_on_insecure_dest: bool,
        /// Allow group/world-accessible backup destinations (overrides config)
        #[arg(
            long = "no-fail-on-insecure-dest",
            conflicts_with = "fail_on_insecure_dest"
        )]
        no_fail_on_insecure_dest: bool,
        /// Encrypt archive/bundle payloads with openssl using this passphrase file
        #[arg(long, value_name = "FILE")]
        encrypt_passphrase_file: Option<PathBuf>,
        /// Disable encryption even when [backup].encrypt_passphrase_file is set
        #[arg(long, conflicts_with = "encrypt_passphrase_file")]
        no_encrypt: bool,
    },
    /// Find and remove known build artifacts (dry-run by default)
    Clean {
        /// Project roots to clean (optional with --list-rules)
        roots: Vec<PathBuf>,
        /// Comma-separated cleanup rules to apply (default: all)
        #[arg(long, value_delimiter = ',')]
        rules: Vec<String>,
        /// Actually delete planned items
        #[arg(long)]
        apply: bool,
        /// Skip the interactive confirmation
        #[arg(long)]
        yes: bool,
        /// Print per-project clean planning / deletion progress with elapsed time
        #[arg(long, conflicts_with = "no_progress")]
        progress: bool,
        /// Disable clean progress even when [clean].progress is true
        #[arg(long = "no-progress", conflicts_with = "progress")]
        no_progress: bool,
        /// Path to a .proj-janitor.toml config file
        #[arg(long)]
        config: Option<PathBuf>,
        /// Emit JSON
        #[arg(long)]
        json: bool,
        /// Print available cleanup rules and exit
        #[arg(long)]
        list_rules: bool,
    },
    /// Preview or remove old backups while retaining the newest snapshots
    Prune {
        /// Backup destination root that contains per-project snapshots
        backup_root: PathBuf,
        /// Keep this many newest manifests per project
        #[arg(long, value_parser = parse_positive_usize, value_name = "N")]
        keep: Option<usize>,
        /// Path to a .proj-janitor.toml config file
        #[arg(long)]
        config: Option<PathBuf>,
        /// Delete the planned backups
        #[arg(long)]
        apply: bool,
        /// Skip interactive confirmation
        #[arg(long)]
        yes: bool,
        /// Print prune deletion progress with elapsed time
        #[arg(long, conflicts_with = "no_progress")]
        progress: bool,
        /// Disable prune progress even when [prune].progress is true
        #[arg(long = "no-progress", conflicts_with = "progress")]
        no_progress: bool,
        /// Emit JSON
        #[arg(long)]
        json: bool,
    },
    /// Restore projects from a backup manifest
    Restore {
        /// Manifest JSON produced by backup
        manifest: PathBuf,
        /// Restore parent directory (default: original location in manifest)
        #[arg(long)]
        dest: Option<PathBuf>,
        /// Restore only this project (repeatable)
        #[arg(long = "project", value_name = "NAME")]
        projects: Vec<String>,
        /// Show the restore plan without writing files
        #[arg(long)]
        dry_run: bool,
        /// Skip archive SHA-256 verification
        #[arg(long)]
        no_verify: bool,
        /// Skip confirmation for original-location restore or --force overwrite
        #[arg(long)]
        yes: bool,
        /// Fail before restore when a Git remote/submodule/LFS object is unreachable
        #[arg(long, conflicts_with = "no_check_remote")]
        check_remote: bool,
        /// Disable remote probing even when [restore].check_remote is true
        #[arg(long = "no-check-remote", conflicts_with = "check_remote")]
        no_check_remote: bool,
        /// Require --dest; refuse original-location restore
        #[arg(long, conflicts_with = "no_require_dest")]
        require_dest: bool,
        /// Allow original-location restore even when [restore].require_dest is true
        #[arg(long = "no-require-dest", conflicts_with = "require_dest")]
        no_require_dest: bool,
        /// Replace an existing restore target directory after staging succeeds
        #[arg(long, conflicts_with = "no_force")]
        force: bool,
        /// Refuse existing restore targets even when [restore].force is true
        #[arg(long = "no-force", conflicts_with = "force")]
        no_force: bool,
        /// When --force replaces a target, rename the old directory aside instead of deleting it
        #[arg(long, conflicts_with = "no_keep_replaced")]
        keep_replaced: bool,
        /// Delete replaced targets even when [restore].keep_replaced is true
        #[arg(long = "no-keep-replaced", conflicts_with = "keep_replaced")]
        no_keep_replaced: bool,
        /// Keep restoring remaining projects after a per-project failure
        #[arg(long, conflicts_with = "no_continue_on_error")]
        continue_on_error: bool,
        /// Stop at the first per-project failure (overrides config)
        #[arg(long = "no-continue-on-error", conflicts_with = "continue_on_error")]
        no_continue_on_error: bool,
        /// Skip projects whose restore target directory already exists
        #[arg(long, conflicts_with = "no_skip_existing")]
        skip_existing: bool,
        /// Refuse existing restore targets even when [restore].skip_existing is true
        #[arg(long = "no-skip-existing", conflicts_with = "skip_existing")]
        no_skip_existing: bool,
        /// Print per-project restore progress with elapsed time (hashing, probes, archive, git, LFS, submodules)
        #[arg(long, conflicts_with = "no_progress")]
        progress: bool,
        /// Disable restore progress even when [restore].progress is true
        #[arg(long = "no-progress", conflicts_with = "progress")]
        no_progress: bool,
        /// Passphrase file for openssl-encrypted archives
        #[arg(long, value_name = "FILE")]
        decrypt_passphrase_file: Option<PathBuf>,
        /// Path to a .proj-janitor.toml config file
        #[arg(long)]
        config: Option<PathBuf>,
        /// Emit JSON
        #[arg(long)]
        json: bool,
    },
    /// Verify backup archives against a manifest
    Verify {
        /// Manifest JSON produced by backup
        manifest: PathBuf,
        /// Verify only this project (repeatable)
        #[arg(long = "project", value_name = "NAME")]
        projects: Vec<String>,
        /// Contact recorded remotes, submodules, and LFS endpoints
        #[arg(long, conflicts_with = "no_check_remote")]
        check_remote: bool,
        /// Disable remote probing even when [verify].check_remote is true
        #[arg(long = "no-check-remote", conflicts_with = "check_remote")]
        no_check_remote: bool,
        /// Print per-project verify progress with elapsed time (SHA-256 hashing and remote probes)
        #[arg(long, conflicts_with = "no_progress")]
        progress: bool,
        /// Disable verify progress even when [verify].progress is true
        #[arg(long = "no-progress", conflicts_with = "progress")]
        no_progress: bool,
        /// Path to a .proj-janitor.toml config file
        #[arg(long)]
        config: Option<PathBuf>,
        /// Emit JSON
        #[arg(long)]
        json: bool,
    },
    /// Write a starter .proj-janitor.toml in the current (or given) directory
    Init {
        /// Directory that should receive .proj-janitor.toml (default: .)
        #[arg(long, default_value = ".")]
        dir: PathBuf,
        /// Overwrite an existing .proj-janitor.toml
        #[arg(long)]
        force: bool,
    },
}

fn parse_positive_usize(value: &str) -> Result<usize, String> {
    let keep = value
        .parse::<usize>()
        .map_err(|_| "must be a positive integer".to_owned())?;
    if keep == 0 {
        return Err("must be at least 1".into());
    }
    Ok(keep)
}

#[derive(Serialize, Deserialize)]
pub(crate) struct Manifest {
    pub(crate) schema_version: u32,
    pub(crate) created_at: String,
    pub(crate) tool: String,
    pub(crate) version: String,
    pub(crate) include_artifacts: bool,
    pub(crate) always_archive: bool,
    #[serde(default)]
    pub(crate) exclude_git_metadata: bool,
    pub(crate) records: Vec<Record>,
}
#[derive(Serialize, Deserialize, Clone)]
pub(crate) struct Record {
    pub(crate) project: String,
    pub(crate) source: String,
    pub(crate) archive: Option<String>,
    pub(crate) archive_bytes: u64,
    pub(crate) source_bytes: u64,
    pub(crate) sha256: Option<String>,
    pub(crate) strategy: String,
    pub(crate) remote: Option<String>,
    pub(crate) remote_url: Option<String>,
    pub(crate) branch: Option<String>,
    pub(crate) commit: Option<String>,
    pub(crate) upstream: Option<String>,
    pub(crate) verified: Option<String>,
    pub(crate) fallback_reason: Option<String>,
    /// Full file inventory (path -> sha256/size) for archive snapshots.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) inventory: Option<Vec<FileEntry>>,
    /// Present only for incremental (delta) archive snapshots.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) incremental: Option<IncrementalInfo>,
    /// Submodule URL -> pinned commit map for git-strategy records.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) submodules: Option<Vec<SubmoduleInfo>>,
    /// Git LFS pointer objects for git-strategy records (`oid` + `size`).
    /// Legacy manifests may store bare OID strings; those remain readable.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) lfs_objects: Option<Vec<LfsObjectRef>>,
    /// Optional archive encryption scheme (e.g. openssl-aes-256-cbc-pbkdf2).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) encryption: Option<String>,
}

/// One LFS object recorded at backup time. New backups always write
/// `{oid, size}`; older manifests may still contain a bare OID string.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
#[serde(untagged)]
pub(crate) enum LfsObjectRef {
    /// Legacy CR-094 form: OID only (availability probe needs a re-backup).
    Oid(String),
    /// Current form: OID plus exact byte size from the LFS pointer blob.
    Object { oid: String, size: u64 },
}

impl LfsObjectRef {
    pub(crate) fn oid(&self) -> &str {
        match self {
            Self::Oid(oid) | Self::Object { oid, .. } => oid,
        }
    }

    pub(crate) fn size(&self) -> Option<u64> {
        match self {
            Self::Oid(_) => None,
            Self::Object { size, .. } => Some(*size),
        }
    }
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub(crate) struct SubmoduleInfo {
    /// Submodule path relative to the project root, using `/` separators.
    pub(crate) path: String,
    pub(crate) url: String,
    /// Pinned gitlink commit recorded in the parent tree.
    pub(crate) commit: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub(crate) struct FileEntry {
    /// Path relative to the project root, using `/` separators.
    pub(crate) path: String,
    pub(crate) sha256: String,
    pub(crate) size: u64,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub(crate) struct IncrementalInfo {
    /// Filename of the previous snapshot manifest this delta is based on.
    pub(crate) base: String,
    /// Paths (relative to the project root) removed since the base snapshot.
    pub(crate) deleted: Vec<String>,
}

fn main() {
    #[cfg(windows)]
    let result = match std::thread::Builder::new()
        .stack_size(8 * 1024 * 1024)
        .spawn(run)
    {
        Ok(worker) => worker
            .join()
            .unwrap_or_else(|_| Err(anyhow::anyhow!("command thread panicked"))),
        Err(error) => Err(error.into()),
    };
    #[cfg(not(windows))]
    let result = run();
    if let Err(e) = result {
        if let Some(interrupted) = e.downcast_ref::<restore::RestoreInterrupted>() {
            std::process::exit(128 + interrupted.0);
        }
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}
fn run() -> anyhow::Result<()> {
    match Cli::parse().command {
        Action::Scan {
            roots,
            all,
            progress,
            no_progress,
            config,
            json,
        } => {
            let config = discover_config(config.as_deref())?;
            let progress = resolve_tri_state(
                progress,
                no_progress,
                config
                    .as_ref()
                    .map(|cfg| cfg.scan.progress)
                    .unwrap_or(false),
            );
            scan_cmd(&roots, all, progress, json)
        }
        Action::Backup {
            roots,
            backup_dest,
            config,
            keep,
            yes,
            dry_run,
            include_artifacts,
            always_archive,
            exclude_git_metadata,
            incremental,
            fail_on_secrets,
            no_fail_on_secrets,
            exclude_secrets,
            no_exclude_secrets,
            progress,
            no_progress,
            check_space,
            no_check_space,
            fail_on_insecure_dest,
            no_fail_on_insecure_dest,
            encrypt_passphrase_file,
            no_encrypt,
        } => {
            let config = discover_config(config.as_deref())?;
            let backup_dest = backup_dest
                .filter(|path| !path.as_os_str().is_empty())
                .or_else(|| config.as_ref().and_then(|cfg| cfg.backup.dest.clone()));
            let keep = keep.or_else(|| config.as_ref().and_then(|cfg| cfg.backup.keep));
            let exclude = config
                .as_ref()
                .map(|cfg| cfg.backup.exclude.clone())
                .unwrap_or_default();
            let incremental = incremental
                || config
                    .as_ref()
                    .map(|cfg| cfg.backup.incremental)
                    .unwrap_or(false);
            let fail_on_secrets = resolve_tri_state(
                fail_on_secrets,
                no_fail_on_secrets,
                config
                    .as_ref()
                    .map(|cfg| cfg.backup.fail_on_secrets)
                    .unwrap_or(false),
            );
            let exclude_secrets = resolve_tri_state(
                exclude_secrets,
                no_exclude_secrets,
                config
                    .as_ref()
                    .map(|cfg| cfg.backup.exclude_secrets)
                    .unwrap_or(false),
            );
            let progress = resolve_tri_state(
                progress,
                no_progress,
                config
                    .as_ref()
                    .map(|cfg| cfg.backup.progress)
                    .unwrap_or(false),
            );
            let check_space = resolve_tri_state(
                check_space,
                no_check_space,
                config
                    .as_ref()
                    .map(|cfg| cfg.backup.check_space)
                    .unwrap_or(false),
            );
            let fail_on_insecure_dest = resolve_tri_state(
                fail_on_insecure_dest,
                no_fail_on_insecure_dest,
                config
                    .as_ref()
                    .map(|cfg| cfg.backup.fail_on_insecure_dest)
                    .unwrap_or(false),
            );
            let encrypt_passphrase_file = if no_encrypt {
                None
            } else {
                encrypt_passphrase_file.or_else(|| {
                    config
                        .as_ref()
                        .and_then(|cfg| cfg.backup.encrypt_passphrase_file.clone())
                })
            };
            backup_cmd(
                &roots,
                backup_dest,
                keep,
                yes,
                dry_run,
                include_artifacts,
                always_archive,
                exclude_git_metadata,
                &exclude,
                incremental,
                fail_on_secrets,
                exclude_secrets,
                progress,
                check_space,
                fail_on_insecure_dest,
                encrypt_passphrase_file,
            )
        }
        Action::Clean {
            roots,
            rules,
            apply,
            yes,
            progress,
            no_progress,
            config,
            json,
            list_rules,
        } => {
            let config = discover_config(config.as_deref())?;
            let progress = resolve_tri_state(
                progress,
                no_progress,
                config
                    .as_ref()
                    .map(|cfg| cfg.clean.progress)
                    .unwrap_or(false),
            );
            clean_cmd(&roots, &rules, apply, yes, progress, json, list_rules)
        }
        Action::Prune {
            backup_root,
            keep,
            config,
            apply,
            yes,
            progress,
            no_progress,
            json,
        } => {
            let config = discover_config(config.as_deref())?;
            let keep = keep.or_else(|| config.as_ref().and_then(|cfg| cfg.prune.keep));
            let progress = resolve_tri_state(
                progress,
                no_progress,
                config
                    .as_ref()
                    .map(|cfg| cfg.prune.progress)
                    .unwrap_or(false),
            );
            retention::prune_cmd(
                &backup_root,
                keep.ok_or_else(|| anyhow::anyhow!("set --keep or configure [prune].keep"))?,
                apply,
                yes,
                progress,
                json,
            )
        }
        Action::Restore {
            manifest,
            dest,
            projects,
            dry_run,
            no_verify,
            yes,
            check_remote,
            no_check_remote,
            require_dest,
            no_require_dest,
            force,
            no_force,
            keep_replaced,
            no_keep_replaced,
            continue_on_error,
            no_continue_on_error,
            skip_existing,
            no_skip_existing,
            progress,
            no_progress,
            decrypt_passphrase_file,
            config,
            json,
        } => {
            let config = discover_config(config.as_deref())?;
            let check_remote = resolve_check_remote(
                check_remote,
                no_check_remote,
                config
                    .as_ref()
                    .map(|cfg| cfg.restore.check_remote)
                    .unwrap_or(false),
            );
            let require_dest = resolve_tri_state(
                require_dest,
                no_require_dest,
                config
                    .as_ref()
                    .map(|cfg| cfg.restore.require_dest)
                    .unwrap_or(false),
            );
            let force = resolve_tri_state(
                force,
                no_force,
                config
                    .as_ref()
                    .map(|cfg| cfg.restore.force)
                    .unwrap_or(false),
            );
            let keep_replaced = resolve_tri_state(
                keep_replaced,
                no_keep_replaced,
                config
                    .as_ref()
                    .map(|cfg| cfg.restore.keep_replaced)
                    .unwrap_or(false),
            );
            let continue_on_error = resolve_tri_state(
                continue_on_error,
                no_continue_on_error,
                config
                    .as_ref()
                    .map(|cfg| cfg.restore.continue_on_error)
                    .unwrap_or(false),
            );
            let skip_existing = resolve_tri_state(
                skip_existing,
                no_skip_existing,
                config
                    .as_ref()
                    .map(|cfg| cfg.restore.skip_existing)
                    .unwrap_or(false),
            );
            let progress = resolve_tri_state(
                progress,
                no_progress,
                config
                    .as_ref()
                    .map(|cfg| cfg.restore.progress)
                    .unwrap_or(false),
            );
            anyhow::ensure!(
                !(force && skip_existing),
                "--force and --skip-existing cannot be combined"
            );
            let decrypt_passphrase_file = decrypt_passphrase_file.or_else(|| {
                config
                    .as_ref()
                    .and_then(|cfg| cfg.restore.decrypt_passphrase_file.clone())
            });
            restore_cmd(
                &manifest,
                dest,
                &projects,
                dry_run,
                no_verify,
                yes,
                check_remote,
                require_dest,
                force,
                keep_replaced,
                continue_on_error,
                skip_existing,
                progress,
                decrypt_passphrase_file,
                json,
            )
        }
        Action::Verify {
            manifest,
            projects,
            check_remote,
            no_check_remote,
            progress,
            no_progress,
            config,
            json,
        } => {
            let config = discover_config(config.as_deref())?;
            let check_remote = resolve_check_remote(
                check_remote,
                no_check_remote,
                config
                    .as_ref()
                    .map(|cfg| cfg.verify.check_remote)
                    .unwrap_or(false),
            );
            let progress = resolve_tri_state(
                progress,
                no_progress,
                config
                    .as_ref()
                    .map(|cfg| cfg.verify.progress)
                    .unwrap_or(false),
            );
            restore::verify_cmd(&manifest, &projects, check_remote, progress, json)
        }
        Action::Init { dir, force } => {
            let path = init::init_config(&dir, force)?;
            println!("Wrote {}", path.display());
            Ok(())
        }
    }
}

fn scan_cmd(roots: &[PathBuf], all: bool, progress: bool, json: bool) -> anyhow::Result<()> {
    use project::{inspect, list_candidates};
    let mut candidates = Vec::new();
    let mut seen_paths = HashSet::new();
    let mut failed = false;
    for root in roots {
        match list_candidates(root, all) {
            Ok(paths) => {
                for path in paths {
                    if seen_paths.insert(path.clone()) {
                        candidates.push(path);
                    }
                }
            }
            Err(e) => {
                eprintln!("error: {e}");
                failed = true
            }
        }
    }
    let total = candidates.len();
    let progress_started = progress.then(std::time::Instant::now);
    if progress {
        eprintln!("progress: scanning {total} project(s)");
    }
    let mut projects = Vec::new();
    for (index, path) in candidates.into_iter().enumerate() {
        if progress {
            let name = path.file_name().unwrap_or_default().to_string_lossy();
            eprintln!("progress: scanning {}/{total}: {name}", index + 1);
        }
        match inspect(&path) {
            Ok(project) => projects.push(project),
            Err(e) => {
                eprintln!("error: {e}");
                failed = true
            }
        }
    }
    if progress {
        let elapsed = progress_started
            .map(|started| format_elapsed(started.elapsed()))
            .unwrap_or_else(|| "0s".into());
        eprintln!("progress: done, scanned {} ({elapsed})", projects.len());
    }
    if json {
        println!("{}", serde_json::to_string_pretty(&projects)?);
    } else if projects.is_empty() {
        eprintln!("No projects found.")
    } else {
        println!(
            "{:<24} {:>10}  {:<20} {:<8} {:<42} MODIFIED",
            "PROJECT", "SIZE", "BRANCH", "STATE", "MARKERS"
        );
        for p in &projects {
            let (branch, state): (String, String) = match &p.git {
                None => ("-".into(), "-".into()),
                Some(g) => (
                    g.branch.clone().unwrap_or_else(|| "-".into()),
                    g.dirty
                        .map(|d| if d { "dirty" } else { "clean" }.into())
                        .unwrap_or_else(|| "unknown".into()),
                ),
            };
            println!(
                "{:<24} {:>10}  {:<20} {:<8} {:<42} {}",
                p.name,
                human(p.size_bytes),
                branch,
                state,
                p.markers.join(", "),
                p.last_modified
                    .map(|d| d.format("%Y-%m-%d %H:%M").to_string())
                    .unwrap_or_else(|| "-".into())
            );
        }
    }
    for p in &projects {
        let Some(git) = &p.git else {
            continue;
        };
        if git.dirty.is_some() {
            continue;
        }
        let detail = git.detail.as_deref().unwrap_or("git status unavailable");
        eprintln!("warning: {}: git state unknown ({detail})", p.name);
    }
    if failed {
        anyhow::bail!("one or more roots could not be scanned")
    }
    Ok(())
}
pub(crate) fn human(n: u64) -> String {
    let mut v = n as f64;
    let mut unit = "B";
    for u in ["KiB", "MiB", "GiB", "TiB"] {
        if v < 1024.0 {
            break;
        }
        v /= 1024.0;
        unit = u;
    }
    if unit == "B" {
        format!("{n} B")
    } else {
        format!("{v:.1} {unit}")
    }
}

/// Compact elapsed time for `--progress` lines (R-08 observability).
pub(crate) fn format_elapsed(duration: std::time::Duration) -> String {
    let secs = duration.as_secs();
    if secs < 60 {
        format!("{secs}s")
    } else if secs < 3600 {
        format!("{}m{:02}s", secs / 60, secs % 60)
    } else {
        format!("{}h{:02}m{:02}s", secs / 3600, (secs / 60) % 60, secs % 60)
    }
}
pub(crate) fn expand(p: &Path) -> PathBuf {
    let s = p.to_string_lossy();
    let home = || {
        #[cfg(windows)]
        {
            env::var_os("USERPROFILE").or_else(|| env::var_os("HOME"))
        }
        #[cfg(not(windows))]
        {
            env::var_os("HOME")
        }
    };
    if s == "~" {
        home().map(PathBuf::from).unwrap_or_else(|| p.to_owned())
    } else {
        let rest = if cfg!(windows) {
            s.strip_prefix("~/").or_else(|| s.strip_prefix("~\\"))
        } else {
            s.strip_prefix("~/")
        };
        rest.map(|rest| home().map(PathBuf::from).unwrap_or_default().join(rest))
            .unwrap_or_else(|| p.to_owned())
    }
}
fn resolve_for_creation(path: &Path) -> anyhow::Result<PathBuf> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        env::current_dir()?.join(path)
    };
    let mut resolved = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::Prefix(_) | Component::RootDir => resolved.push(component.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                match fs::metadata(&resolved) {
                    Ok(metadata) => anyhow::ensure!(
                        metadata.is_dir(),
                        "path component is not a directory: {}",
                        resolved.display()
                    ),
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error.into()),
                }
                resolved.pop();
            }
            Component::Normal(name) => {
                resolved.push(name);
                match fs::symlink_metadata(&resolved) {
                    Ok(_) => resolved = fs::canonicalize(&resolved)?,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error.into()),
                }
            }
        }
    }
    Ok(resolved)
}

pub(crate) fn is_artifact(s: &str) -> bool {
    ARTIFACT_GLOBS
        .get_or_init(|| {
            RULES
                .iter()
                .flat_map(|(_, patterns)| *patterns)
                .filter_map(|pattern| {
                    GlobBuilder::new(pattern)
                        .case_insensitive(cfg!(windows))
                        .build()
                        .ok()
                })
                .map(|glob| glob.compile_matcher())
                .collect()
        })
        .iter()
        .any(|glob| glob.is_match(s))
}
#[cfg(test)]
#[path = "tests.rs"]
mod tests;
