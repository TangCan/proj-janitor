use chrono::{DateTime, Local, Utc};
use clap::{Parser, Subcommand};
use flate2::{Compression, read::GzDecoder, write::GzEncoder};
use globset::{Glob, GlobSetBuilder};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    env,
    fs::{self, File},
    io::{self, IsTerminal, Read, Write},
    path::{Component, Path, PathBuf},
    process::{Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};
use tar::{Archive, Builder, EntryType, Header};
use url::Url;
use walkdir::WalkDir;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const MARKERS: &[&str] = &[
    ".git",
    ".hg",
    ".svn",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "CMakeLists.txt",
    "Makefile",
    "meson.build",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
];
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

#[derive(Serialize)]
struct Project {
    name: String,
    path: PathBuf,
    size_bytes: u64,
    last_modified: Option<DateTime<Local>>,
    markers: Vec<String>,
    git: Option<GitState>,
}
#[derive(Serialize)]
struct GitState {
    branch: Option<String>,
    dirty: Option<bool>,
}
struct Recovery {
    remote: String,
    remote_url: String,
    branch: String,
    commit: String,
    upstream: String,
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

fn is_project(path: &Path) -> bool {
    MARKERS.iter().any(|m| path.join(m).exists())
}
fn discover(root: &Path, all: bool) -> anyhow::Result<Vec<Project>> {
    let root = expand(root);
    anyhow::ensure!(root.exists(), "root does not exist: {}", root.display());
    anyhow::ensure!(root.is_dir(), "root is not a directory: {}", root.display());
    let candidates = if is_project(&root) {
        vec![root]
    } else {
        let mut v = Vec::new();
        for e in fs::read_dir(&root)? {
            let e = e?;
            let p = e.path();
            let n = e.file_name();
            if p.is_dir()
                && !p.is_symlink()
                && !n.to_string_lossy().starts_with('.')
                && (all || is_project(&p))
                && fs::read_dir(&p)?.next().is_some()
            {
                v.push(p);
            }
        }
        v.sort();
        v
    };
    candidates.into_iter().map(|p| inspect(&p)).collect()
}
fn inspect(path: &Path) -> anyhow::Result<Project> {
    let path = fs::canonicalize(path)?;
    let markers = MARKERS
        .iter()
        .filter(|m| path.join(m).exists())
        .map(|m| m.to_string())
        .collect();
    let mut size = 0u64;
    let mut latest = None;
    for e in WalkDir::new(&path)
        .follow_links(false)
        .into_iter()
        .filter_map(Result::ok)
    {
        if e.file_type().is_file()
            && let Ok(m) = e.metadata()
        {
            size += m.len();
            let t = m.modified().ok();
            if t > latest {
                latest = t;
            }
        }
    }
    let git = if path.join(".git").exists() {
        let branch = git_output(&path, &["rev-parse", "--abbrev-ref", "HEAD"]);
        let status = git_output(&path, &["status", "--porcelain"]);
        Some(GitState {
            branch,
            dirty: status.map(|s| !s.trim().is_empty()),
        })
    } else {
        None
    };
    Ok(Project {
        name: path
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned(),
        path,
        size_bytes: size,
        last_modified: latest.map(|t| DateTime::<Utc>::from(t).with_timezone(&Local)),
        markers,
        git,
    })
}
fn git_output(path: &Path, args: &[&str]) -> Option<String> {
    git_run(path, args)
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string())
}
fn git_run(path: &Path, args: &[&str]) -> Option<std::process::Output> {
    let mut child = Command::new("git")
        .env("GIT_TERMINAL_PROMPT", "0")
        .arg("-C")
        .arg(path)
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .ok()?;
    let mut stdout = child.stdout.take()?;
    let mut stderr = child.stderr.take()?;
    let stdout_reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout.read_to_end(&mut bytes).map(|_| bytes)
    });
    let stderr_reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr.read_to_end(&mut bytes).map(|_| bytes)
    });
    let deadline = Instant::now() + Duration::from_secs(10);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(50)),
            Ok(None) | Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    Some(Output {
        status,
        stdout: stdout_reader.join().ok()?.ok()?,
        stderr: stderr_reader.join().ok()?.ok()?,
    })
}

fn github_url(remote: &str) -> Option<String> {
    if remote.trim() != remote || remote.chars().any(char::is_whitespace) {
        return None;
    }
    if let Some(path) = remote.strip_prefix("git@github.com:") {
        return valid_github_path(path).then(|| remote.to_owned());
    }
    if let Some(path) = remote.strip_prefix("ssh://") {
        let url = Url::parse(&format!("ssh://{path}")).ok()?;
        if url.scheme() == "ssh"
            && url.host_str()? == "github.com"
            && url.username() == "git"
            && url.password().is_none()
            && url.port().is_none_or(|port| port == 22)
            && url.query().is_none()
            && url.fragment().is_none()
            && valid_github_path(url.path().trim_start_matches('/'))
        {
            return Some(remote.to_owned());
        }
        return None;
    }
    if let Some(rest) = remote.strip_prefix("https://") {
        let url = Url::parse(&format!("https://{rest}")).ok()?;
        if url.scheme() != "https"
            || url.host_str()? != "github.com"
            || url.port().is_some_and(|port| port != 443)
            || url.query().is_some()
            || url.fragment().is_some()
            || !valid_github_path(url.path().trim_start_matches('/'))
        {
            return None;
        }
        return Some(format!("https://github.com{}", url.path()));
    }
    None
}

fn valid_github_path(path: &str) -> bool {
    let path = path.strip_suffix(".git").unwrap_or(path);
    let mut parts = path.split('/');
    let valid_part = |s: &str| {
        !s.is_empty()
            && s != "."
            && s != ".."
            && s.chars()
                .all(|c| c.is_ascii_alphanumeric() || "._-".contains(c))
    };
    parts.next().is_some_and(valid_part)
        && parts.next().is_some_and(valid_part)
        && parts.next().is_none()
}

fn inspect_recovery(project: &Project) -> Result<Recovery, String> {
    let path = &project.path;
    let branch = git_output(path, &["symbolic-ref", "--quiet", "--short", "HEAD"])
        .filter(|s| !s.is_empty())
        .ok_or("detached HEAD")?;
    let status = git_output(path, &["status", "--porcelain", "--untracked-files=all"])
        .ok_or("git status failed")?;
    if !status.is_empty() {
        return Err("working tree is not clean".into());
    }
    let stashes = git_output(path, &["stash", "list"]).ok_or("cannot inspect stashes")?;
    if !stashes.is_empty() {
        return Err("stash entries are not included".into());
    }
    let remote = git_output(
        path,
        &["config", "--get", &format!("branch.{branch}.remote")],
    )
    .filter(|s| !s.is_empty() && s != ".")
    .ok_or("branch has no upstream remote")?;
    let upstream = git_output(
        path,
        &[
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ],
    )
    .filter(|s| !s.is_empty())
    .ok_or("branch has no upstream")?;
    let raw_url = git_output(path, &["remote", "get-url", &remote])
        .filter(|s| !s.is_empty())
        .ok_or("remote URL is unavailable")?;
    let remote_url = github_url(&raw_url).ok_or("remote is not a supported GitHub URL")?;
    let commit = git_output(path, &["rev-parse", "HEAD"]).ok_or("HEAD is unavailable")?;
    let ancestor = git_run(
        path,
        &["merge-base", "--is-ancestor", "HEAD", "@{upstream}"],
    )
    .is_some_and(|o| o.status.success());
    if !ancestor {
        return Err("HEAD is not contained in upstream".into());
    }
    let ahead = git_output(path, &["rev-list", "--count", "@{upstream}..HEAD"])
        .and_then(|s| s.parse::<u64>().ok())
        .ok_or("cannot compare HEAD with upstream")?;
    if ahead != 0 {
        return Err("HEAD is ahead of upstream".into());
    }
    if path.join(".gitmodules").exists() {
        return Err("submodules are not supported".into());
    }
    let submodules =
        git_output(path, &["submodule", "status"]).ok_or("cannot inspect submodules")?;
    if !submodules.is_empty() {
        return Err("submodules are not supported".into());
    }
    if path.join(".lfsconfig").exists() {
        return Err("Git LFS is not supported".into());
    }
    let lfs = git_run(path, &["grep", "-l", "filter=lfs", "--", "*.gitattributes"])
        .ok_or("cannot inspect Git LFS attributes")?;
    if !lfs.status.success() && lfs.status.code() != Some(1) {
        return Err("cannot inspect Git LFS attributes".into());
    }
    if !lfs.stdout.is_empty() {
        return Err("Git LFS is not supported".into());
    }
    let ignored = git_run(
        path,
        &[
            "-c",
            "core.quotePath=false",
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
        ],
    )
    .filter(|o| o.status.success())
    .ok_or("cannot inspect ignored files")?;
    for raw in ignored
        .stdout
        .split(|byte| *byte == 0)
        .filter(|s| !s.is_empty())
    {
        let ignored_path = String::from_utf8_lossy(raw)
            .trim_end_matches('/')
            .to_string();
        if !ignored_is_artifact(path, Path::new(&ignored_path)) {
            return Err(format!("ignored path is not an artifact: {ignored_path}"));
        }
    }
    let branches = git_output(
        path,
        &["for-each-ref", "--format=%(refname:short)", "refs/heads"],
    )
    .ok_or("cannot list local branches")?;
    let remote_refs = git_output(
        path,
        &[
            "for-each-ref",
            "--format=%(refname)",
            &format!("refs/remotes/{remote}/"),
        ],
    )
    .ok_or("cannot list remote-tracking refs")?;
    let refs: Vec<_> = remote_refs.lines().filter(|s| !s.is_empty()).collect();
    if refs.is_empty() {
        return Err("no remote-tracking refs".into());
    }
    for local_branch in branches.lines().filter(|s| !s.is_empty()) {
        let mut args = vec!["rev-list", "--count", local_branch, "--not"];
        args.extend(refs.iter().copied());
        let count = git_output(path, &args)
            .and_then(|s| s.parse::<u64>().ok())
            .ok_or_else(|| format!("cannot inspect local branch: {local_branch}"))?;
        if count != 0 {
            return Err(format!("local branch is not fully pushed: {local_branch}"));
        }
    }
    verify_local_tags(path, &remote)?;
    Ok(Recovery {
        remote,
        remote_url,
        branch,
        commit,
        upstream,
    })
}

fn ignored_is_artifact(project: &Path, path: &Path) -> bool {
    for (index, component) in path.components().enumerate() {
        let name = component.as_os_str().to_string_lossy();
        if name == "coverage" {
            return index == 0 && coverage_artifact(&project.join("coverage"));
        }
        if is_artifact(&name) {
            return true;
        }
    }
    false
}

fn coverage_artifact(path: &Path) -> bool {
    !path.is_symlink()
        && path.is_dir()
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
        .any(|marker| path.join(marker).exists())
}

fn verify_local_tags(path: &Path, remote: &str) -> Result<(), String> {
    let tags = git_output(
        path,
        &[
            "for-each-ref",
            "--format=%(refname:short) %(objectname)",
            "refs/tags",
        ],
    )
    .ok_or("cannot list tags")?;
    if tags.is_empty() {
        return Ok(());
    }
    let output = git_run(path, &["ls-remote", "--tags", "--refs", "--", remote])
        .filter(|o| o.status.success())
        .ok_or("cannot verify local tags on remote")?;
    let output_text = String::from_utf8_lossy(&output.stdout);
    let mut remote_tags = std::collections::HashMap::new();
    for line in output_text.lines() {
        let Some((sha, reference)) = line.split_once('\t') else {
            return Err("cannot parse remote tags".into());
        };
        if let Some(name) = reference.strip_prefix("refs/tags/") {
            remote_tags.insert(name.to_owned(), sha.to_owned());
        }
    }
    for line in tags.lines() {
        let Some((name, sha)) = line.split_once(' ') else {
            return Err("cannot list tags".into());
        };
        match remote_tags.get(name) {
            None => return Err(format!("local tag is not pushed: {name}")),
            Some(remote_sha) if *remote_sha != sha => {
                return Err(format!("local tag differs from remote: {name}"));
            }
            _ => {}
        }
    }
    Ok(())
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
fn expand(p: &Path) -> PathBuf {
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
        fs::create_dir_all(&dir)?;
        let result = (|| -> anyhow::Result<Record> {
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
                        fs::write(&manifest_path, bytes).map_err(anyhow::Error::from)
                    });
                if let Err(e) = write_result {
                    let _ = fs::remove_file(&archive_path);
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
                let _ = fs::remove_file(&archive_path);
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
fn create_archive(project: &Path, archive: &Path, include: bool) -> anyhow::Result<()> {
    let f = File::create(archive)?;
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
}
fn is_artifact(s: &str) -> bool {
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
                    for e in WalkDir::new(&p.path)
                        .follow_links(false)
                        .into_iter()
                        .filter_entry(|entry| entry.path() == p.path || entry.file_name() != ".git")
                        .filter_map(Result::ok)
                    {
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
                                e.metadata().map(|m| m.len()).unwrap_or(0)
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
fn tree_size(path: &Path) -> u64 {
    WalkDir::new(path)
        .follow_links(false)
        .into_iter()
        .filter_map(Result::ok)
        .filter_map(|e| e.metadata().ok())
        .filter(|m| m.is_file())
        .map(|m| m.len())
        .sum()
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
    if link.is_absolute() {
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
    for component in link.components() {
        match component {
            Component::Normal(part) => parts.push(part.to_string_lossy().into_owned()),
            Component::CurDir => {}
            Component::ParentDir => {
                if parts.len() <= 1 {
                    return false;
                }
                parts.pop();
            }
            Component::RootDir | Component::Prefix(_) => return false,
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
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);

    struct TestDir(PathBuf);
    impl TestDir {
        fn new() -> Self {
            let path = env::temp_dir().join(format!(
                "projjan-test-{}-{}",
                std::process::id(),
                NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for TestDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn local_git(path: &Path, args: &[&str]) -> bool {
        Command::new("git")
            .arg("-C")
            .arg(path)
            .args(args)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_TERMINAL_PROMPT", "0")
            .output()
            .is_ok_and(|output| output.status.success())
    }

    fn pushed_github_fixture(path: &Path) -> bool {
        if !Command::new("git")
            .arg("--version")
            .output()
            .is_ok_and(|o| o.status.success())
        {
            return false;
        }
        fs::create_dir_all(path).unwrap();
        assert!(
            Command::new("git")
                .args(["init", "-b", "main"])
                .arg(path)
                .output()
                .is_ok_and(|s| s.status.success())
        );
        let configured = local_git(path, &["config", "user.name", "Test User"])
            && local_git(path, &["config", "user.email", "test@example.invalid"]);
        assert!(configured, "could not configure local Git fixture");
        fs::write(path.join("pyproject.toml"), "[project]\nname='demo'\n").unwrap();
        fs::write(path.join("tracked.txt"), "clean\n").unwrap();
        assert!(
            local_git(path, &["add", "."]),
            "could not stage Git fixture"
        );
        assert!(
            local_git(path, &["commit", "-m", "fixture"]),
            "could not commit Git fixture"
        );
        let ready = local_git(
            path,
            &[
                "config",
                "remote.origin.url",
                "https://user:token@github.com/team/demo.git",
            ],
        ) && local_git(path, &["config", "branch.main.remote", "origin"])
            && local_git(
                path,
                &[
                    "config",
                    "remote.origin.fetch",
                    "+refs/heads/*:refs/remotes/origin/*",
                ],
            )
            && local_git(path, &["config", "branch.main.merge", "refs/heads/main"])
            && local_git(path, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
        assert!(ready, "could not configure pushed Git fixture");
        true
    }

    fn git_ok(path: &Path, args: &[&str]) -> bool {
        Command::new("git")
            .args(args)
            .arg(path)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .output()
            .is_ok_and(|output| output.status.success())
    }

    fn raw_test_archive(
        temp: &TestDir,
        archive_name: &str,
        member_name: &str,
        link_target: Option<&str>,
    ) -> PathBuf {
        let archive_path = temp.0.join(archive_name);
        let mut header = Header::new_gnu();
        header.set_entry_type(if link_target.is_some() {
            EntryType::Symlink
        } else {
            EntryType::Regular
        });
        header.set_mode(0o644);
        header.set_size(if link_target.is_some() { 0 } else { 4 });
        header.set_path("demo/safe-entry").unwrap();
        if let Some(target) = link_target {
            header.set_link_name_literal(target.as_bytes()).unwrap();
        }
        let name = member_name.as_bytes();
        assert!(name.len() < 100);
        let raw = header.as_mut_bytes();
        raw[..100].fill(0);
        raw[..name.len()].copy_from_slice(name);
        header.set_cksum();

        let encoder = GzEncoder::new(File::create(&archive_path).unwrap(), Compression::default());
        let mut tar = Builder::new(encoder);
        let data: &[u8] = if link_target.is_some() { b"" } else { b"evil" };
        tar.append(&header, data).unwrap();
        tar.into_inner().unwrap().finish().unwrap();
        archive_path
    }

    #[test]
    fn github_remote_validation_accepts_supported_forms_and_redacts_https_tokens() {
        assert_eq!(
            github_url("https://alice:secret@github.com/team/project.git").as_deref(),
            Some("https://github.com/team/project.git")
        );
        assert_eq!(
            github_url("git@github.com:team/project.git").as_deref(),
            Some("git@github.com:team/project.git")
        );
        assert_eq!(
            github_url("ssh://git@github.com/team/project").as_deref(),
            Some("ssh://git@github.com/team/project")
        );
        assert!(github_url("https://github.com.attacker.invalid/team/project").is_none());
        assert!(github_url("https://github.com/team/project/extra").is_none());
        assert_eq!(
            redact_remote("https://alice:secret@github.com/team/project"),
            "https://github.com/team/project"
        );
    }

    #[test]
    fn restore_rejects_git_options_and_malformed_commit_ids() {
        assert!(
            validate_git_restore("https://github.com/team/project.git", &"a".repeat(40)).is_ok()
        );
        assert!(validate_git_restore("--upload-pack=evil", &"a".repeat(40)).is_err());
        assert!(validate_git_restore("https://github.com/team/project.git", "--help").is_err());
        assert!(validate_git_restore("https://github.com/team/project.git", "abc").is_err());
    }

    #[test]
    fn archive_paths_and_links_cannot_escape_project_root() {
        assert!(safe_archive_path(
            Path::new("project/src/main.rs"),
            "project"
        ));
        assert!(!safe_archive_path(
            Path::new("project/../../outside"),
            "project"
        ));
        assert!(!safe_archive_path(Path::new("/project/file"), "project"));
        assert!(safe_archive_link(
            Path::new("project/src/link"),
            Path::new("../main.rs"),
            "project"
        ));
        assert!(!safe_archive_link(
            Path::new("project/link"),
            Path::new("../../outside"),
            "project"
        ));
        assert!(!safe_archive_link(
            Path::new("project/link"),
            Path::new("/etc/passwd"),
            "project"
        ));
    }

    #[test]
    fn manifest_v2_accepts_python_style_null_record_fields() {
        let json = r#"{
            "schema_version":2,
            "created_at":"2026-10-24T00:00:00+00:00",
            "tool":"proj-janitor",
            "version":"0.1.0",
            "include_artifacts":false,
            "always_archive":false,
            "records":[{
                "project":"demo","source":"/tmp/demo","archive":null,
                "archive_bytes":0,"source_bytes":1,"sha256":null,"strategy":"git",
                "remote":"origin","remote_url":"https://github.com/team/demo",
                "branch":"main","commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "upstream":"origin/main","verified":"local-ref","fallback_reason":null
            }]
        }"#;
        let manifest: Manifest = serde_json::from_str(json).unwrap();
        assert_eq!(manifest.records[0].strategy, "git");
        assert!(manifest.records[0].archive.is_none());
    }

    #[test]
    fn archive_round_trip_keeps_files_executable_and_internal_symlinks() {
        let temp = TestDir::new();
        let project = temp.0.join("demo");
        fs::create_dir_all(project.join("src/subdir")).unwrap();
        fs::create_dir_all(project.join("target/cache")).unwrap();
        fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
        fs::write(project.join("src/main.sh"), "#!/bin/sh\necho ok\n").unwrap();
        fs::write(project.join("src/subdir/data.txt"), "directory target\n").unwrap();
        fs::write(project.join("target/cache/blob"), "regenerable").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::{PermissionsExt, symlink};
            fs::set_permissions(
                project.join("src/main.sh"),
                fs::Permissions::from_mode(0o755),
            )
            .unwrap();
            symlink("main.sh", project.join("src/current")).unwrap();
            symlink("subdir", project.join("src/current-dir")).unwrap();
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::{symlink_dir, symlink_file};
            // shortcut: skip Windows link round-trip when the test runner lacks symlink privilege.
            if let Err(error) = symlink_file("main.sh", project.join("src/current")) {
                eprintln!(
                    "Windows symlink privilege unavailable; skipping link round-trip: {error}"
                );
                return;
            }
            if let Err(error) = symlink_dir("subdir", project.join("src/current-dir")) {
                eprintln!(
                    "Windows symlink privilege unavailable; skipping link round-trip: {error}"
                );
                return;
            }
        }

        let archive = temp.0.join("demo.tar.gz");
        create_archive(&project, &archive, false).unwrap();
        let restored = temp.0.join("restored/demo");
        extract_archive(&archive, &restored, "demo").unwrap();

        assert_eq!(
            fs::read_to_string(restored.join("pyproject.toml")).unwrap(),
            "[project]\n"
        );
        assert!(!restored.join("target").exists());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(restored.join("src/main.sh"))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o755
            );
            assert_eq!(
                fs::read_link(restored.join("src/current")).unwrap(),
                PathBuf::from("main.sh")
            );
            assert_eq!(
                fs::read_link(restored.join("src/current-dir")).unwrap(),
                PathBuf::from("subdir")
            );
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::FileTypeExt;
            assert!(
                fs::symlink_metadata(restored.join("src/current"))
                    .unwrap()
                    .file_type()
                    .is_symlink_file()
            );
            assert!(
                fs::symlink_metadata(restored.join("src/current-dir"))
                    .unwrap()
                    .file_type()
                    .is_symlink_dir()
            );
            assert_eq!(
                fs::read_link(restored.join("src/current")).unwrap(),
                PathBuf::from("main.sh")
            );
            assert_eq!(
                fs::read_link(restored.join("src/current-dir")).unwrap(),
                PathBuf::from("subdir")
            );
            println!("Windows file and directory symlink round-trip verified");
        }
    }

    #[test]
    fn backup_uses_url_only_when_safe_and_archives_dirty_git_projects() {
        let temp = TestDir::new();
        let project = temp.0.join("demo");
        if !pushed_github_fixture(&project) {
            eprintln!("git unavailable; skipping Git recovery fixture");
            return;
        }
        let project_info = inspect(&project).unwrap();
        assert!(
            inspect_recovery(&project_info).is_ok(),
            "{:?}",
            inspect_recovery(&project_info).err()
        );
        let backup_dir = temp.0.join("backups");
        backup_cmd(
            std::slice::from_ref(&project),
            Some(backup_dir.clone()),
            false,
            false,
            false,
        )
        .unwrap();

        let project_backup = backup_dir.join("demo");
        let manifest_path = fs::read_dir(project_backup)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .find(|path| {
                path.extension()
                    .is_some_and(|extension| extension == "json")
            })
            .unwrap();
        let manifest: Manifest =
            serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
        let record = &manifest.records[0];
        assert_eq!(record.strategy, "git");
        assert!(record.archive.is_none());
        assert_eq!(
            record.remote_url.as_deref(),
            Some("https://github.com/team/demo.git")
        );
        assert!(!serde_json::to_string(&manifest).unwrap().contains("token"));

        fs::write(project.join("tracked.txt"), "modified\n").unwrap();
        backup_cmd(&[project], Some(backup_dir.clone()), false, false, false).unwrap();
        let archive_manifest_path = fs::read_dir(backup_dir.join("demo"))
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .find(|path| {
                path.extension()
                    .is_some_and(|extension| extension == "json")
                    && path != &manifest_path
            })
            .unwrap();
        let archive_manifest: Manifest =
            serde_json::from_slice(&fs::read(archive_manifest_path).unwrap()).unwrap();
        let archive_record = &archive_manifest.records[0];
        assert_eq!(archive_record.strategy, "archive");
        assert_eq!(
            archive_record.fallback_reason.as_deref(),
            Some("working tree is not clean")
        );
        assert!(Path::new(archive_record.archive.as_deref().unwrap()).is_file());
    }

    #[test]
    fn restore_reads_a_schema_v2_archive_manifest_and_checks_its_hash() {
        let temp = TestDir::new();
        let source = temp.0.join("demo");
        fs::create_dir_all(&source).unwrap();
        fs::write(source.join("pyproject.toml"), "[project]\n").unwrap();
        fs::write(source.join("data.txt"), "restore me\n").unwrap();
        let backup_dir = temp.0.join("backups/demo");
        fs::create_dir_all(&backup_dir).unwrap();
        let archive = backup_dir.join("demo.tar.gz");
        create_archive(&source, &archive, true).unwrap();
        let record = Record {
            project: "demo".into(),
            source: source.to_string_lossy().into_owned(),
            archive: Some(archive.to_string_lossy().into_owned()),
            archive_bytes: fs::metadata(&archive).unwrap().len(),
            source_bytes: tree_size(&source),
            sha256: Some(sha256(&archive).unwrap()),
            strategy: "archive".into(),
            remote: None,
            remote_url: None,
            branch: None,
            commit: None,
            upstream: None,
            verified: None,
            fallback_reason: Some("fixture".into()),
        };
        let manifest = Manifest {
            schema_version: 2,
            created_at: "2026-10-24T00:00:00+00:00".into(),
            tool: "proj-janitor".into(),
            version: "0.1.0".into(),
            include_artifacts: false,
            always_archive: false,
            records: vec![record],
        };
        let manifest_path = backup_dir.join("manifest.json");
        fs::write(
            &manifest_path,
            serde_json::to_vec_pretty(&manifest).unwrap(),
        )
        .unwrap();
        let restore_root = temp.0.join("restored");
        restore_cmd(
            &manifest_path,
            Some(restore_root.clone()),
            &[],
            false,
            false,
            false,
            false,
        )
        .unwrap();
        assert_eq!(
            fs::read_to_string(restore_root.join("demo/data.txt")).unwrap(),
            "restore me\n"
        );
    }

    #[test]
    fn restore_rejects_parent_traversal_and_escaping_symlink_archives() {
        let temp = TestDir::new();
        let traversal = raw_test_archive(&temp, "traversal.tar.gz", "demo/../../escape", None);
        let traversal_target = temp.0.join("restored-traversal/demo");
        assert!(extract_archive(&traversal, &traversal_target, "demo").is_err());
        assert!(!traversal_target.exists());
        assert!(!temp.0.join("restored-traversal/escape").exists());

        let escaping_link = raw_test_archive(
            &temp,
            "escaping-link.tar.gz",
            "demo/link",
            Some("../../escape"),
        );
        let link_target = temp.0.join("restored-link/demo");
        assert!(extract_archive(&escaping_link, &link_target, "demo").is_err());
        assert!(!link_target.exists());
        assert!(!temp.0.join("restored-link/escape").exists());
    }

    #[test]
    fn clean_apply_removes_only_artifacts_and_preserves_project_files() {
        let temp = TestDir::new();
        let project = temp.0.join("demo");
        fs::create_dir_all(project.join("target/debug")).unwrap();
        fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
        fs::write(project.join("src.txt"), "keep\n").unwrap();
        fs::write(project.join("target/debug/cache"), "remove\n").unwrap();

        clean_cmd(std::slice::from_ref(&project), &[], true, true, true, false).unwrap();

        assert!(!project.join("target").exists());
        assert_eq!(
            fs::read_to_string(project.join("src.txt")).unwrap(),
            "keep\n"
        );
        assert!(project.join("pyproject.toml").is_file());
    }

    #[test]
    fn remote_tag_check_uses_remote_refs_and_rejects_a_mismatched_tag() {
        let temp = TestDir::new();
        let project = temp.0.join("demo");
        if !pushed_github_fixture(&project) {
            eprintln!("git unavailable; skipping Git recovery fixture");
            return;
        }
        let remote = temp.0.join("remote.git");
        assert!(
            git_ok(&remote, &["init", "--bare"]),
            "could not create bare remote"
        );
        assert!(local_git(
            &project,
            &["tag", "-a", "v1", "-m", "fixture tag"]
        ));
        let remote_arg = remote.to_string_lossy().into_owned();
        assert!(local_git(
            &project,
            &["push", &remote_arg, "main", "--tags"]
        ));
        assert!(verify_local_tags(&project, &remote_arg).is_ok());

        fs::write(project.join("tracked.txt"), "second commit\n").unwrap();
        assert!(local_git(&project, &["add", "tracked.txt"]));
        assert!(local_git(
            &project,
            &["commit", "-m", "second fixture commit"]
        ));
        assert!(local_git(&project, &["push", &remote_arg, "main"]));
        assert!(local_git(&project, &["tag", "-f", "v1", "HEAD"]));

        match verify_local_tags(&project, &remote_arg) {
            Err(reason) => assert_eq!(reason, "local tag differs from remote: v1"),
            Ok(_) => panic!("mismatched remote tag was accepted"),
        }
    }
}
