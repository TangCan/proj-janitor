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
mod project;
mod restore;
mod retention;
mod secrets;
use backup::backup_cmd;
use clean::clean_cmd;
use config::{discover_config, resolve_check_remote, resolve_tri_state};
use project::discover;
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
    Scan {
        #[arg(required = true)]
        roots: Vec<PathBuf>,
        #[arg(long)]
        all: bool,
        #[arg(long)]
        json: bool,
    },
    Backup {
        #[arg(required = true)]
        roots: Vec<PathBuf>,
        #[arg(long, env = "BACKUP_DEST", hide_env_values = true)]
        backup_dest: Option<PathBuf>,
        #[arg(long)]
        config: Option<PathBuf>,
        #[arg(long, value_parser = parse_positive_usize)]
        keep: Option<usize>,
        #[arg(long)]
        yes: bool,
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        include_artifacts: bool,
        #[arg(long)]
        always_archive: bool,
        #[arg(long)]
        exclude_git_metadata: bool,
        #[arg(long)]
        incremental: bool,
        #[arg(long, conflicts_with = "no_fail_on_secrets")]
        fail_on_secrets: bool,
        #[arg(long = "no-fail-on-secrets", conflicts_with = "fail_on_secrets")]
        no_fail_on_secrets: bool,
        #[arg(long, conflicts_with = "no_exclude_secrets")]
        exclude_secrets: bool,
        #[arg(long = "no-exclude-secrets", conflicts_with = "exclude_secrets")]
        no_exclude_secrets: bool,
        #[arg(long, conflicts_with = "no_progress")]
        progress: bool,
        #[arg(long = "no-progress", conflicts_with = "progress")]
        no_progress: bool,
        #[arg(long, conflicts_with = "no_check_space")]
        check_space: bool,
        #[arg(long = "no-check-space", conflicts_with = "check_space")]
        no_check_space: bool,
        #[arg(long, conflicts_with = "no_fail_on_insecure_dest")]
        fail_on_insecure_dest: bool,
        #[arg(
            long = "no-fail-on-insecure-dest",
            conflicts_with = "fail_on_insecure_dest"
        )]
        no_fail_on_insecure_dest: bool,
    },
    Clean {
        roots: Vec<PathBuf>,
        #[arg(long, value_delimiter = ',')]
        rules: Vec<String>,
        #[arg(long)]
        apply: bool,
        #[arg(long)]
        yes: bool,
        #[arg(long)]
        json: bool,
        #[arg(long)]
        list_rules: bool,
    },
    Prune {
        backup_root: PathBuf,
        #[arg(long, value_parser = parse_positive_usize)]
        keep: Option<usize>,
        #[arg(long)]
        config: Option<PathBuf>,
        #[arg(long)]
        apply: bool,
        #[arg(long)]
        yes: bool,
        #[arg(long)]
        json: bool,
    },
    Restore {
        manifest: PathBuf,
        #[arg(long)]
        dest: Option<PathBuf>,
        #[arg(long = "project")]
        projects: Vec<String>,
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        no_verify: bool,
        #[arg(long)]
        yes: bool,
        #[arg(long, conflicts_with = "no_check_remote")]
        check_remote: bool,
        #[arg(long = "no-check-remote", conflicts_with = "check_remote")]
        no_check_remote: bool,
        #[arg(long, conflicts_with = "no_require_dest")]
        require_dest: bool,
        #[arg(long = "no-require-dest", conflicts_with = "require_dest")]
        no_require_dest: bool,
        #[arg(long)]
        config: Option<PathBuf>,
        #[arg(long)]
        json: bool,
    },
    Verify {
        manifest: PathBuf,
        #[arg(long = "project")]
        projects: Vec<String>,
        #[arg(long, conflicts_with = "no_check_remote")]
        check_remote: bool,
        #[arg(long = "no-check-remote", conflicts_with = "check_remote")]
        no_check_remote: bool,
        #[arg(long)]
        config: Option<PathBuf>,
        #[arg(long)]
        json: bool,
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
        Action::Scan { roots, all, json } => scan_cmd(&roots, all, json),
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
            )
        }
        Action::Clean {
            roots,
            rules,
            apply,
            yes,
            json,
            list_rules,
        } => clean_cmd(&roots, &rules, apply, yes, json, list_rules),
        Action::Prune {
            backup_root,
            keep,
            config,
            apply,
            yes,
            json,
        } => {
            let config = discover_config(config.as_deref())?;
            let keep = keep.or_else(|| config.as_ref().and_then(|cfg| cfg.prune.keep));
            retention::prune_cmd(
                &backup_root,
                keep.ok_or_else(|| anyhow::anyhow!("set --keep or configure [prune].keep"))?,
                apply,
                yes,
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
            restore_cmd(
                &manifest,
                dest,
                &projects,
                dry_run,
                no_verify,
                yes,
                check_remote,
                require_dest,
                json,
            )
        }
        Action::Verify {
            manifest,
            projects,
            check_remote,
            no_check_remote,
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
            restore::verify_cmd(&manifest, &projects, check_remote, json)
        }
    }
}

fn scan_cmd(roots: &[PathBuf], all: bool, json: bool) -> anyhow::Result<()> {
    let mut projects = Vec::new();
    let mut seen = HashSet::new();
    let mut failed = false;
    for root in roots {
        match discover(root, all) {
            Ok(ps) => {
                for p in ps {
                    if seen.insert(p.path.clone()) {
                        projects.push(p)
                    }
                }
            }
            Err(e) => {
                eprintln!("error: {e}");
                failed = true
            }
        }
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
        for p in projects {
            let (branch, state): (String, String) = match p.git {
                None => ("-".into(), "-".into()),
                Some(g) => (
                    g.branch.unwrap_or_else(|| "-".into()),
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
