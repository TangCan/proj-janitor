use clap::{Parser, Subcommand};
use globset::Glob;
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    env, fs,
    path::{Component, Path, PathBuf},
};
mod archive;
mod backup;
mod clean;
mod project;
mod restore;
use backup::backup_cmd;
use clean::clean_cmd;
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
        &["build", "dist", "out", "target", "CMakeFiles", ".gradle"],
    ),
    ("editor", &[".cache", ".DS_Store", "Thumbs.db"]),
];

#[derive(Parser)]
#[command(
    name = "projjan",
    version,
    about = "Scan, back up, restore, and clean old projects safely"
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
        #[arg(long, env = "BACKUP_DEST")]
        backup_dest: Option<PathBuf>,
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        include_artifacts: bool,
        #[arg(long)]
        always_archive: bool,
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
        #[arg(long)]
        json: bool,
    },
}

#[derive(Serialize, Deserialize)]
pub(crate) struct Manifest {
    pub(crate) schema_version: u32,
    pub(crate) created_at: String,
    pub(crate) tool: String,
    pub(crate) version: String,
    pub(crate) include_artifacts: bool,
    pub(crate) always_archive: bool,
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
}

fn main() {
    if let Err(e) = run() {
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
            dry_run,
            include_artifacts,
            always_archive,
        } => backup_cmd(
            &roots,
            backup_dest,
            dry_run,
            include_artifacts,
            always_archive,
        ),
        Action::Clean {
            roots,
            rules,
            apply,
            yes,
            json,
            list_rules,
        } => clean_cmd(&roots, &rules, apply, yes, json, list_rules),
        Action::Restore {
            manifest,
            dest,
            projects,
            dry_run,
            no_verify,
            yes,
            json,
        } => restore_cmd(&manifest, dest, &projects, dry_run, no_verify, yes, json),
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
    RULES.iter().flat_map(|(_, p)| *p).any(|pat| {
        Glob::new(pat)
            .ok()
            .is_some_and(|g| g.compile_matcher().is_match(s))
    })
}
#[cfg(test)]
#[path = "tests.rs"]
mod tests;
