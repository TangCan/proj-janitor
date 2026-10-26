use chrono::{Local, Utc};
use clap::{Parser, Subcommand};
use flate2::{Compression, read::GzDecoder, write::GzEncoder};
use globset::{Glob, GlobSetBuilder};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    env,
    fs::{self, File, OpenOptions},
    io::{self, IsTerminal, Read, Write},
    path::{Component, Path, PathBuf},
    process::Command,
};
use tar::{Archive, Builder, EntryType, Header};
use url::Url;
mod project;
use project::{discover, inspect_recovery};
use walkdir::WalkDir;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const RULES: &[(&str, &[&str])] = &[
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
struct Manifest {
    schema_version: u32,
    created_at: String,
    tool: String,
    version: String,
    include_artifacts: bool,
    always_archive: bool,
    records: Vec<Record>,
}
#[derive(Serialize, Deserialize, Clone)]
struct Record {
    project: String,
    source: String,
    archive: Option<String>,
    archive_bytes: u64,
    source_bytes: u64,
    sha256: Option<String>,
    strategy: String,
    remote: Option<String>,
    remote_url: Option<String>,
    branch: Option<String>,
    commit: Option<String>,
    upstream: Option<String>,
    verified: Option<String>,
    fallback_reason: Option<String>,
}

fn main() {
    if let Err(e) = run() {
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
fn human(n: u64) -> String {
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
    if s == "~" {
        env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| p.to_owned())
    } else if let Some(rest) = s.strip_prefix("~/") {
        env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_default()
            .join(rest)
    } else {
        p.to_owned()
    }
}
fn resolve_for_creation(path: &Path) -> anyhow::Result<PathBuf> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        env::current_dir()?.join(path)
    };
    let mut normalized = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::Prefix(_) | Component::RootDir => normalized.push(component.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            Component::Normal(name) => normalized.push(name),
        }
    }
    let absolute = normalized;
    let mut ancestor = absolute.as_path();
    let mut missing = Vec::new();
    while !ancestor.exists() {
        let name = ancestor
            .file_name()
            .ok_or_else(|| anyhow::anyhow!("cannot resolve path: {}", path.display()))?;
        missing.push(name.to_owned());
        ancestor = ancestor
            .parent()
            .ok_or_else(|| anyhow::anyhow!("cannot resolve path: {}", path.display()))?;
    }
    let mut resolved = fs::canonicalize(ancestor)?;
    for component in missing.into_iter().rev() {
        resolved.push(component);
    }
    Ok(resolved)
}

fn validate_git_restore(remote: &str, commit: &str) -> anyhow::Result<()> {
    anyhow::ensure!(
        (7..=64).contains(&commit.len()) && commit.bytes().all(|b| b.is_ascii_hexdigit()),
        "invalid commit in manifest"
    );
    anyhow::ensure!(
        !remote.is_empty() && !remote.starts_with('-') && !remote.chars().any(char::is_whitespace),
        "invalid remote URL in manifest"
    );
    if remote.starts_with("https://")
        || remote.starts_with("ssh://")
        || remote.starts_with("file://")
    {
        let parsed =
            Url::parse(remote).map_err(|_| anyhow::anyhow!("invalid remote URL in manifest"))?;
        anyhow::ensure!(
            matches!(parsed.scheme(), "file") || parsed.host_str().is_some(),
            "remote URL has no host"
        );
        return Ok(());
    }
    if Path::new(remote).is_absolute() {
        return Ok(());
    }
    if let Some((identity, path)) = remote.split_once(':') {
        let Some((user, host)) = identity.rsplit_once('@') else {
            anyhow::bail!("unsupported remote URL in manifest")
        };
        anyhow::ensure!(
            !user.is_empty()
                && !host.is_empty()
                && !path.is_empty()
                && host
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b".-".contains(&b)),
            "unsupported remote URL in manifest"
        );
        return Ok(());
    }
    anyhow::bail!("unsupported remote URL in manifest")
}

fn redact_remote(remote: &str) -> String {
    if remote.starts_with("http://")
        || remote.starts_with("https://")
        || remote.starts_with("ssh://")
    {
        let Ok(mut parsed) = Url::parse(remote) else {
            return "<invalid>".into();
        };
        if parsed.username().is_empty() && parsed.password().is_none() {
            return remote.to_owned();
        }
        if parsed.set_username("").is_err() || parsed.set_password(None).is_err() {
            return "<invalid>".into();
        }
        return parsed.to_string();
    }
    remote.to_owned()
}

fn backup_cmd(
    roots: &[PathBuf],
    dest: Option<PathBuf>,
    dry: bool,
    include_artifacts: bool,
    always: bool,
) -> anyhow::Result<()> {
    let dest = dest.ok_or_else(|| anyhow::anyhow!("set BACKUP_DEST or pass --backup-dest"))?;
    let dest = resolve_for_creation(&expand(&dest))?;
    if dest.exists() && !dest.is_dir() {
        anyhow::bail!("destination is not a directory: {}", dest.display())
    }
    let mut projects = Vec::new();
    let mut seen = HashSet::new();
    let mut failed = false;
    for root in roots {
        match discover(root, false) {
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
    if projects.is_empty() {
        anyhow::bail!("No projects found")
    }
    for p in &projects {
        let dir_name = if projects.iter().filter(|other| other.name == p.name).count() > 1 {
            let path_hash = format!("{:x}", Sha256::digest(p.path.to_string_lossy().as_bytes()));
            format!("{}-{}", p.name, &path_hash[..8])
        } else {
            p.name.clone()
        };
        let output_dir = resolve_for_creation(&dest.join(dir_name))?;
        anyhow::ensure!(
            !output_dir.starts_with(&p.path),
            "backup destination is inside project: {} -> {}",
            output_dir.display(),
            p.path.display()
        );
    }
    let timestamp = Local::now().format("%Y%m%d-%H%M%S").to_string();
    for p in &projects {
        let dir_name = if projects.iter().filter(|other| other.name == p.name).count() > 1 {
            let path_hash = format!("{:x}", Sha256::digest(p.path.to_string_lossy().as_bytes()));
            format!("{}-{}", p.name, &path_hash[..8])
        } else {
            p.name.clone()
        };
        let dir = dest.join(dir_name);
        let mut run_stamp = timestamp.clone();
        let mut suffix = 2;
        while dir.join(format!("manifest-{run_stamp}.json")).exists()
            || dir.join(format!("{}-{run_stamp}.tar.gz", p.name)).exists()
        {
            run_stamp = format!("{timestamp}-{suffix}");
            suffix += 1;
        }
        let manifest_path = dir.join(format!("manifest-{run_stamp}.json"));
        let archive_path = dir.join(format!("{}-{run_stamp}.tar.gz", p.name));
        let (recovery, fallback_reason) = if include_artifacts {
            (None, Some("include_artifacts".to_owned()))
        } else if always {
            (None, Some("always_archive".to_owned()))
        } else {
            match inspect_recovery(p) {
                Ok(recovery) => (Some(recovery), None),
                Err(reason) => (None, Some(reason)),
            }
        };
        if dry {
            if let Some(git) = &recovery {
                println!(
                    "[git] {}: {} @ {}",
                    p.name,
                    git.remote_url,
                    &git.commit[..12.min(git.commit.len())]
                );
            } else {
                println!(
                    "[archive] {}: {} -> {}",
                    p.name,
                    human(p.size_bytes),
                    archive_path.display()
                );
            }
            continue;
        }
        let mut archive_created = false;
        let result = (|| -> anyhow::Result<Record> {
            fs::create_dir_all(&dir)?;
            if let Some(git) = &recovery {
                return Ok(Record {
                    project: p.name.clone(),
                    source: p.path.to_string_lossy().into(),
                    archive: None,
                    archive_bytes: 0,
                    source_bytes: p.size_bytes,
                    sha256: None,
                    strategy: "git".into(),
                    remote: Some(git.remote.clone()),
                    remote_url: Some(git.remote_url.clone()),
                    branch: Some(git.branch.clone()),
                    commit: Some(git.commit.clone()),
                    upstream: Some(git.upstream.clone()),
                    verified: Some("local-ref".into()),
                    fallback_reason: None,
                });
            }
            create_archive(&p.path, &archive_path, include_artifacts)?;
            archive_created = true;
            let hash = sha256(&archive_path)?;
            let bytes = fs::metadata(&archive_path)?.len();
            Ok(Record {
                project: p.name.clone(),
                source: p.path.to_string_lossy().into(),
                archive: Some(archive_path.to_string_lossy().into()),
                archive_bytes: bytes,
                source_bytes: p.size_bytes,
                sha256: Some(hash),
                strategy: "archive".into(),
                remote: None,
                remote_url: None,
                branch: None,
                commit: None,
                upstream: None,
                verified: None,
                fallback_reason: fallback_reason.clone(),
            })
        })();
        match result {
            Ok(record) => {
                let manifest = Manifest {
                    schema_version: 2,
                    created_at: Utc::now().to_rfc3339(),
                    tool: "proj-janitor".into(),
                    version: VERSION.into(),
                    include_artifacts,
                    always_archive: always,
                    records: vec![record.clone()],
                };
                let write_result = serde_json::to_vec_pretty(&manifest)
                    .map_err(anyhow::Error::from)
                    .and_then(|mut bytes| {
                        bytes.push(b'\n');
                        write_new_file(&manifest_path, &bytes).map_err(anyhow::Error::from)
                    });
                if let Err(e) = write_result {
                    if archive_created {
                        let _ = fs::remove_file(&archive_path);
                    }
                    eprintln!("error: {}: cannot write manifest: {e}", p.name);
                    failed = true;
                    continue;
                }
                if record.strategy == "git" {
                    println!(
                        "[git] {}: {} @ {}",
                        record.project,
                        record.remote_url.as_deref().unwrap_or("-"),
                        &record.commit.as_deref().unwrap_or_default()
                            [..12.min(record.commit.as_deref().unwrap_or_default().len())]
                    );
                } else {
                    println!(
                        "[archive] {}: {} sha256:{}",
                        record.project,
                        human(record.archive_bytes),
                        &record.sha256.as_deref().unwrap_or_default()[..12]
                    );
                }
                println!("Manifest: {}", manifest_path.display());
            }
            Err(e) => {
                if archive_created {
                    let _ = fs::remove_file(&archive_path);
                }
                eprintln!("error: {}: {e}", p.name);
                failed = true
            }
        }
    }
    if failed {
        anyhow::bail!("one or more backup operations failed")
    }
    Ok(())
}
fn write_new_file(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    if let Err(error) = file.write_all(bytes) {
        drop(file);
        let _ = fs::remove_file(path);
        return Err(error);
    }
    Ok(())
}

fn create_archive(project: &Path, archive: &Path, include: bool) -> anyhow::Result<()> {
    let f = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(archive)?;
    let result = (|| -> anyhow::Result<()> {
        let enc = GzEncoder::new(f, Compression::default());
        let mut tar = Builder::new(enc);
        let root = project.file_name().unwrap_or_default();
        tar.append_dir(root, project)?;
        for e in WalkDir::new(project)
            .follow_links(false)
            .into_iter()
            .filter_entry(|entry| !archive_excluded(entry.path(), project, include))
        {
            let e = e?;
            let rel = e.path().strip_prefix(project)?;
            if rel.as_os_str().is_empty() {
                continue;
            }
            let name = Path::new(root).join(rel);
            if e.file_type().is_symlink() {
                let target = fs::read_link(e.path())?;
                anyhow::ensure!(
                    safe_archive_link(&name, &target, &root.to_string_lossy()),
                    "archive symlink escapes project: {} -> {}",
                    name.display(),
                    target.display()
                );
                let mut h = Header::new_gnu();
                h.set_entry_type(EntryType::Symlink);
                h.set_size(0);
                h.set_mode(0o777);
                h.set_link_name(&target)?;
                h.set_cksum();
                tar.append_link(&mut h, name, target)?;
            } else if e.file_type().is_dir() {
                tar.append_dir(name, e.path())?;
            } else if e.file_type().is_file() {
                tar.append_path_with_name(e.path(), name)?;
            }
        }
        tar.into_inner()?.finish()?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(archive);
    }
    result
}
pub(crate) fn is_artifact(s: &str) -> bool {
    RULES.iter().flat_map(|(_, p)| *p).any(|pat| {
        Glob::new(pat)
            .ok()
            .is_some_and(|g| g.compile_matcher().is_match(s))
    })
}
fn archive_excluded(path: &Path, project: &Path, include: bool) -> bool {
    if include || path == project {
        return false;
    }
    let Ok(relative) = path.strip_prefix(project) else {
        return true;
    };
    for (index, component) in relative.components().enumerate() {
        let name = component.as_os_str().to_string_lossy();
        if name == "coverage" {
            return index == 0
                && [
                    "lcov.info",
                    "lcov-report",
                    "coverage-final.json",
                    "coverage-summary.json",
                    "coverage.xml",
                    "coverage.json",
                    "coverage.out",
                    "coverage.cobertura.xml",
                    "clover.xml",
                    "jacoco.xml",
                    "jacoco.csv",
                    "status.json",
                    ".coverage",
                ]
                .iter()
                .any(|marker| path.join(marker).exists());
        }
        if is_artifact(&name) {
            return true;
        }
    }
    false
}
fn sha256(path: &Path) -> anyhow::Result<String> {
    let mut f = File::open(path)?;
    let mut h = Sha256::new();
    let mut b = [0; 1024 * 1024];
    loop {
        let n = f.read(&mut b)?;
        if n == 0 {
            break;
        }
        h.update(&b[..n]);
    }
    Ok(format!("{:x}", h.finalize()))
}

fn clean_cmd(
    roots: &[PathBuf],
    selected: &[String],
    apply: bool,
    yes: bool,
    json: bool,
    list: bool,
) -> anyhow::Result<()> {
    if list {
        for (n, p) in RULES {
            println!("{n}: {}", p.join(", "));
        }
        return Ok(());
    }
    anyhow::ensure!(!roots.is_empty(), "at least one root is required");
    let chosen: Vec<_> = if selected.is_empty() {
        RULES.iter().collect()
    } else {
        let mut v = Vec::new();
        for s in selected {
            v.push(
                RULES
                    .iter()
                    .find(|(n, _)| n == s)
                    .ok_or_else(|| anyhow::anyhow!("unknown rule: {s}"))?,
            );
        }
        v
    };
    let mut rule_patterns = Vec::new();
    for (rule, ps) in &chosen {
        let mut builder = GlobSetBuilder::new();
        for p in *ps {
            builder.add(Glob::new(p)?);
        }
        rule_patterns.push(((*rule).to_owned(), builder.build()?));
    }
    let mut actions = Vec::new();
    let mut failed = false;
    for root in roots {
        match discover(root, false) {
            Ok(ps) => {
                for p in ps {
                    for entry in WalkDir::new(&p.path)
                        .follow_links(false)
                        .into_iter()
                        .filter_entry(|entry| entry.path() == p.path || entry.file_name() != ".git")
                    {
                        let e = match entry {
                            Ok(entry) => entry,
                            Err(error) => {
                                eprintln!("error: {error}");
                                failed = true;
                                continue;
                            }
                        };
                        let path = e.path();
                        if path == p.path || e.file_type().is_symlink() {
                            continue;
                        }
                        let name = path.file_name().unwrap_or_default().to_string_lossy();
                        if name == ".git" {
                            continue;
                        }
                        let cov = name == "coverage"
                            && path.parent() == Some(p.path.as_path())
                            && [
                                "lcov.info",
                                "coverage-final.json",
                                "coverage-summary.json",
                                "coverage.xml",
                                "coverage.json",
                                "coverage.out",
                                "clover.xml",
                                "jacoco.xml",
                                "jacoco.csv",
                                "status.json",
                                ".coverage",
                                "lcov-report",
                                "coverage.cobertura.xml",
                            ]
                            .iter()
                            .any(|m| path.join(m).exists());
                        let rule = if name == "coverage" && !cov {
                            None
                        } else {
                            rule_patterns
                                .iter()
                                .find(|(_, set)| set.is_match(name.as_ref()))
                                .map(|(rule, _)| rule.clone())
                        };
                        if let Some(rule) = rule {
                            let size = if e.file_type().is_dir() {
                                tree_size(path)
                            } else {
                                e.metadata().map(|m| m.len()).map_err(anyhow::Error::from)
                            };
                            let size = match size {
                                Ok(size) => size,
                                Err(error) => {
                                    eprintln!("error: {error}");
                                    failed = true;
                                    continue;
                                }
                            };
                            let kind = if e.file_type().is_dir() {
                                "dir"
                            } else {
                                "file"
                            };
                            actions.push((p.path.clone(), path.to_owned(), size, rule, kind));
                        }
                    }
                }
            }
            Err(e) => {
                eprintln!("error: {e}");
                failed = true
            }
        }
    }
    if failed {
        anyhow::bail!("scan failures; no cleanup was applied")
    }
    actions.sort_by_key(|action| action.1.components().count());
    let mut selected_paths: Vec<PathBuf> = Vec::new();
    actions.retain(|(_, path, _, _, _)| {
        if selected_paths.iter().any(|parent| path.starts_with(parent)) {
            false
        } else {
            selected_paths.push(path.clone());
            true
        }
    });
    actions.sort_by(|a, b| a.1.cmp(&b.1));
    if json {
        let vals: Vec<_> = actions
            .iter()
            .map(|(p, path, size, rule, kind)| serde_json::json!({"project":p,"path":path,"rule":rule,"kind":kind,"size_bytes":size}))
            .collect();
        println!("{}", serde_json::to_string_pretty(&vals)?);
    } else if actions.is_empty() {
        println!("Nothing to clean.")
    } else {
        for (_, p, s, _, _) in &actions {
            println!("{}  {}", human(*s), p.display())
        }
        println!(
            "{} item(s), {} reclaimable.",
            actions.len(),
            human(actions.iter().map(|a| a.2).sum())
        );
        if !apply {
            println!("Dry run. Re-run with --apply to delete these items.")
        }
    }
    if !apply || actions.is_empty() {
        return if failed {
            anyhow::bail!("scan failures")
        } else {
            Ok(())
        };
    }
    if !yes {
        if !io::stdin().is_terminal() {
            anyhow::bail!("refusing to delete without --yes in a non-interactive session")
        }
        eprint!("Delete {} item(s)? [y/N] ", actions.len());
        io::stderr().flush()?;
        let mut a = String::new();
        io::stdin().read_line(&mut a)?;
        if !matches!(a.trim().to_lowercase().as_str(), "y" | "yes") {
            anyhow::bail!("Aborted")
        }
    }
    for (project, path, _, _, _) in actions {
        let root = fs::canonicalize(project)?;
        let target = fs::canonicalize(&path)?;
        anyhow::ensure!(
            target != root && target.starts_with(&root),
            "target escapes project root: {}",
            path.display()
        );
        if path.is_dir() {
            fs::remove_dir_all(path)?
        } else {
            fs::remove_file(path)?
        }
    }
    if failed {
        anyhow::bail!("scan failures")
    }
    Ok(())
}
fn tree_size(path: &Path) -> anyhow::Result<u64> {
    let mut total = 0;
    for entry in WalkDir::new(path).follow_links(false) {
        let entry = entry?;
        if entry.file_type().is_file() {
            total += entry.metadata()?.len();
        }
    }
    Ok(total)
}

fn restore_cmd(
    manifest_path: &Path,
    dest: Option<PathBuf>,
    projects: &[String],
    dry: bool,
    no_verify: bool,
    yes: bool,
    json: bool,
) -> anyhow::Result<()> {
    let manifest_path = fs::canonicalize(expand(manifest_path))?;
    let manifest: Manifest = serde_json::from_slice(&fs::read(&manifest_path)?)?;
    anyhow::ensure!(
        manifest.schema_version == 2,
        "unsupported manifest schema_version: {}",
        manifest.schema_version
    );
    anyhow::ensure!(
        !manifest.records.is_empty(),
        "manifest records must be a non-empty array"
    );
    let dest = if let Some(dest) = dest {
        let resolved = resolve_for_creation(&expand(&dest))?;
        anyhow::ensure!(
            !resolved.exists() || resolved.is_dir(),
            "destination is not a directory: {}",
            resolved.display()
        );
        Some(resolved)
    } else {
        None
    };
    let mut plans = Vec::new();
    let mut names = HashSet::new();
    let wanted: HashSet<_> = projects.iter().collect();
    for r in &manifest.records {
        if !wanted.is_empty() && !wanted.contains(&r.project) {
            continue;
        }
        anyhow::ensure!(safe_name(&r.project), "unsafe project name: {}", r.project);
        anyhow::ensure!(
            names.insert(r.project.clone()),
            "duplicate project: {}",
            r.project
        );
        let target = if let Some(d) = &dest {
            d.join(&r.project)
        } else {
            let src = PathBuf::from(&r.source);
            anyhow::ensure!(
                src.is_absolute()
                    && !src.components().any(|c| matches!(c, Component::ParentDir))
                    && src
                        .file_name()
                        .is_some_and(|n| n.to_string_lossy() == r.project),
                "unsafe source path for {}",
                r.project
            );
            anyhow::ensure!(src.parent().is_some(), "restore source has no parent");
            resolve_for_creation(&src)?
        };
        anyhow::ensure!(
            !target.exists() && !target.is_symlink(),
            "restore target already exists: {}",
            target.display()
        );
        let archive = match r.strategy.as_str() {
            "archive" => Some(resolve_archive(
                r,
                manifest_path.parent().unwrap_or(Path::new(".")),
            )?),
            "git" => {
                let url = r
                    .remote_url
                    .as_deref()
                    .ok_or_else(|| anyhow::anyhow!("missing remote_url"))?;
                let commit = r
                    .commit
                    .as_deref()
                    .ok_or_else(|| anyhow::anyhow!("missing commit"))?;
                validate_git_restore(url, commit)?;
                None
            }
            other => anyhow::bail!("unsupported backup strategy: {other}"),
        };
        if let Some(a) = &archive
            && !no_verify
        {
            let expect = r
                .sha256
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("manifest has no sha256 for {}", r.project))?;
            let actual = sha256(a)?;
            anyhow::ensure!(
                actual.eq_ignore_ascii_case(expect),
                "sha256 mismatch for {}",
                a.display()
            );
        }
        plans.push((r.clone(), target, archive));
    }
    for p in projects {
        anyhow::ensure!(
            plans.iter().any(|(r, _, _)| &r.project == p),
            "project not found in manifest: {p}"
        )
    }
    if dry {
        if json {
            let vals: Vec<_> = plans
                .iter()
                .map(|(r, t, a)| {
                    serde_json::json!({
                        "project": r.project,
                        "strategy": r.strategy,
                        "target": t,
                        "archive": a,
                        "remote_url": r.remote_url.as_deref().map(redact_remote),
                        "commit": r.commit,
                        "sha256": r.sha256,
                        "sha256_verified": r.strategy == "archive" && !no_verify,
                        "in_place": dest.is_none(),
                    })
                })
                .collect();
            println!(
                "{}",
                serde_json::to_string_pretty(
                    &serde_json::json!({"dry_run":true,"mode":if dest.is_some(){"dest"}else{"original"},"projects":vals})
                )?
            )
        } else {
            for (r, t, a) in &plans {
                if r.strategy == "git" {
                    println!(
                        "[git] {}: {} @ {} -> {}",
                        r.project,
                        r.remote_url
                            .as_deref()
                            .map(redact_remote)
                            .unwrap_or_default(),
                        r.commit.as_deref().unwrap_or("-"),
                        t.display()
                    );
                } else {
                    println!(
                        "[archive] {}: {} -> {}",
                        r.project,
                        a.as_ref()
                            .map(|p| p.display().to_string())
                            .unwrap_or_default(),
                        t.display()
                    );
                }
            }
            println!("Dry run.")
        }
        return Ok(());
    }
    if dest.is_none() && !yes {
        anyhow::ensure!(
            io::stdin().is_terminal() && !json,
            "--yes is required for original-location restore in non-interactive or JSON mode"
        );
        eprintln!("About to restore to original locations:");
        for (record, target, _) in &plans {
            eprintln!("  {}: {}", record.project, target.display());
        }
        eprint!(
            "Restore {} project(s) to original locations? [y/N] ",
            plans.len()
        );
        io::stderr().flush()?;
        let mut a = String::new();
        io::stdin().read_line(&mut a)?;
        anyhow::ensure!(
            matches!(a.trim().to_lowercase().as_str(), "y" | "yes"),
            "Aborted"
        )
    }
    let mut done = Vec::new();
    for (r, target, archive) in &plans {
        fs::create_dir_all(
            target
                .parent()
                .ok_or_else(|| anyhow::anyhow!("target has no parent"))?,
        )?;
        match r.strategy.as_str() {
            "archive" => extract_archive(archive.as_ref().unwrap(), target, &r.project)?,
            "git" => {
                let url = r
                    .remote_url
                    .as_ref()
                    .ok_or_else(|| anyhow::anyhow!("missing remote_url"))?;
                let commit = r
                    .commit
                    .as_ref()
                    .ok_or_else(|| anyhow::anyhow!("missing commit"))?;
                restore_git(url, commit, target, &r.project)?;
            }
            x => anyhow::bail!("unsupported backup strategy: {x}"),
        };
        done.push((r.project.clone(), r.strategy.clone(), target.clone()));
    }
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(
                &serde_json::json!({"dry_run":false,"mode":if dest.is_some(){"dest"}else{"original"},"projects":done.iter().map(|(p,s,t)|serde_json::json!({"project":p,"strategy":s,"target":t})).collect::<Vec<_>>()})
            )?
        )
    } else {
        println!("Restored {} project(s).", done.len());
        for (p, s, t) in done {
            println!("[{s}] {p} -> {}", t.display())
        }
    }
    Ok(())
}

fn restore_git(url: &str, commit: &str, target: &Path, project: &str) -> anyhow::Result<()> {
    anyhow::ensure!(
        !target.exists() && !target.is_symlink(),
        "restore target already exists: {}",
        target.display()
    );
    let parent = target
        .parent()
        .ok_or_else(|| anyhow::anyhow!("target has no parent"))?;
    fs::create_dir_all(parent)?;
    let temporary = parent.join(format!(".{project}-restore-{}", std::process::id()));
    anyhow::ensure!(!temporary.exists(), "temporary restore path already exists");
    fs::create_dir(&temporary)?;
    let repo = temporary.join("repo");
    let result = (|| -> anyhow::Result<()> {
        let clone = Command::new("git")
            .env("GIT_TERMINAL_PROMPT", "0")
            .args(["clone", "--no-checkout", "--", url])
            .arg(&repo)
            .output()?;
        anyhow::ensure!(clone.status.success(), "git clone failed for {project}");
        let checkout = Command::new("git")
            .arg("-C")
            .arg(&repo)
            .args(["checkout", "--detach", commit])
            .output()?;
        anyhow::ensure!(
            checkout.status.success(),
            "git checkout failed for {project}"
        );
        let head = Command::new("git")
            .arg("-C")
            .arg(&repo)
            .args(["rev-parse", "HEAD"])
            .output()?;
        anyhow::ensure!(
            head.status.success(),
            "cannot verify restored commit for {project}"
        );
        let actual = String::from_utf8_lossy(&head.stdout).trim().to_owned();
        anyhow::ensure!(
            actual
                .to_ascii_lowercase()
                .starts_with(&commit.to_ascii_lowercase()),
            "restored commit does not match manifest for {project}"
        );
        anyhow::ensure!(
            !target.exists() && !target.is_symlink(),
            "restore target already exists: {}",
            target.display()
        );
        fs::rename(&repo, target)?;
        Ok(())
    })();
    let _ = fs::remove_dir_all(&temporary);
    result
}

fn safe_name(s: &str) -> bool {
    !s.is_empty()
        && s != "."
        && s != ".."
        && !s.contains('/')
        && !s.contains('\\')
        && !s.contains('\0')
        && Path::new(s).file_name().is_some_and(|n| n == s)
}
fn resolve_archive(r: &Record, dir: &Path) -> anyhow::Result<PathBuf> {
    let raw = PathBuf::from(
        r.archive
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("missing archive for {}", r.project))?,
    );
    let mut v = Vec::new();
    if raw.is_absolute() {
        v.push(raw.clone())
    } else {
        v.push(dir.join(&raw))
    }
    let name = raw
        .file_name()
        .ok_or_else(|| anyhow::anyhow!("invalid archive path"))?;
    v.push(dir.join(name));
    v.push(dir.join(&r.project).join(name));
    for p in v {
        if p.is_file() {
            return Ok(fs::canonicalize(p)?);
        }
    }
    anyhow::bail!("archive not found for {}", r.project)
}
fn extract_archive(archive: &Path, target: &Path, project: &str) -> anyhow::Result<()> {
    anyhow::ensure!(
        !target.exists() && !target.is_symlink(),
        "restore target already exists: {}",
        target.display()
    );
    let parent = target
        .parent()
        .ok_or_else(|| anyhow::anyhow!("target has no parent"))?;
    fs::create_dir_all(parent)?;
    let tmp = parent.join(format!(".{project}-restore-{}", std::process::id()));
    anyhow::ensure!(
        !tmp.exists(),
        "temporary path already exists: {}",
        tmp.display()
    );
    fs::create_dir(&tmp)?;
    let result = (|| -> anyhow::Result<()> {
        let f = File::open(archive)?;
        let dec = GzDecoder::new(f);
        let mut ar = Archive::new(dec);
        let entries = ar.entries()?;
        let mut count = 0;
        let mut directories = Vec::new();
        let mut symlinks = Vec::new();
        for item in entries {
            let mut e = item?;
            let name = e.path()?.into_owned();
            anyhow::ensure!(
                safe_archive_path(&name, project),
                "unsafe archive path: {}",
                name.display()
            );
            let ty = e.header().entry_type();
            let mode = e.header().mode()? & 0o777;
            let out = tmp.join(&name);
            if ty.is_dir() {
                fs::create_dir_all(&out)?;
                directories.push((out, mode));
            } else if ty.is_symlink() {
                let link = e
                    .link_name()?
                    .ok_or_else(|| anyhow::anyhow!("symlink has no target"))?;
                anyhow::ensure!(
                    safe_archive_link(&name, &link, project),
                    "archive symlink escapes project: {}",
                    name.display()
                );
                if let Some(parent) = out.parent() {
                    fs::create_dir_all(parent)?;
                }
                symlinks.push((link.into_owned(), out));
            } else {
                anyhow::ensure!(
                    ty.is_file(),
                    "archive contains special file: {}",
                    name.display()
                );
                if let Some(p) = out.parent() {
                    fs::create_dir_all(p)?
                }
                let mut f = File::create(&out)?;
                io::copy(&mut e, &mut f)?;
                set_mode(&out, mode)?;
                count += 1;
            }
        }
        anyhow::ensure!(count > 0, "archive is empty");
        let root = tmp.join(project);
        anyhow::ensure!(
            root.is_dir() && !root.is_symlink(),
            "archive does not contain project directory: {project}"
        );
        for (link_target, link_path) in symlinks {
            let target_is_dir = link_path
                .parent()
                .is_some_and(|parent| parent.join(&link_target).is_dir());
            create_symlink(&link_target, &link_path, target_is_dir)?;
        }
        for (directory, mode) in directories.into_iter().rev() {
            set_mode(&directory, mode)?;
        }
        fs::rename(root, target)?;
        Ok(())
    })();
    let _ = fs::remove_dir_all(&tmp);
    result
}

#[cfg(unix)]
fn set_mode(path: &Path, mode: u32) -> anyhow::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))?;
    Ok(())
}

#[cfg(not(unix))]
fn set_mode(_path: &Path, _mode: u32) -> anyhow::Result<()> {
    Ok(())
}

fn safe_archive_path(p: &Path, project: &str) -> bool {
    !p.is_absolute()
        && p.components().all(|c| matches!(c, Component::Normal(_)))
        && p.components()
            .next()
            .is_some_and(|c| c.as_os_str() == project)
}

fn safe_archive_link(name: &Path, link: &Path, project: &str) -> bool {
    let target = link.to_string_lossy();
    if link.is_absolute()
        || target.starts_with(['/', '\\'])
        || (target.as_bytes().get(1) == Some(&b':') && target.as_bytes()[0].is_ascii_alphabetic())
    {
        return false;
    }
    let mut parts: Vec<String> = name
        .parent()
        .unwrap_or(Path::new(""))
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect();
    if parts.first().is_none_or(|part| part != project) {
        return false;
    }
    for component in target.split(['/', '\\']) {
        match component {
            "" | "." => {}
            ".." => {
                if parts.len() <= 1 {
                    return false;
                }
                parts.pop();
            }
            part => parts.push(part.to_owned()),
        }
    }
    !parts.is_empty() && parts[0] == project
}

#[cfg(unix)]
fn create_symlink(target: &Path, link: &Path, _target_is_dir: bool) -> anyhow::Result<()> {
    std::os::unix::fs::symlink(target, link)?;
    Ok(())
}

#[cfg(windows)]
fn create_symlink(target: &Path, link: &Path, target_is_dir: bool) -> anyhow::Result<()> {
    if target_is_dir {
        std::os::windows::fs::symlink_dir(target, link)?;
    } else {
        // shortcut: dangling directory links have no type in tar headers; add link-type metadata if preserving them becomes required.
        std::os::windows::fs::symlink_file(target, link)?;
    }
    Ok(())
}

#[cfg(not(any(unix, windows)))]
fn create_symlink(_target: &Path, _link: &Path, _target_is_dir: bool) -> anyhow::Result<()> {
    anyhow::bail!("symbolic link restoration is unsupported on this platform")
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
