use chrono::{DateTime, Local, Utc};
use serde::Serialize;
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};
use url::Url;
use walkdir::WalkDir;

use crate::is_artifact;

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
#[derive(Serialize)]
pub(crate) struct Project {
    pub(crate) name: String,
    pub(crate) path: PathBuf,
    pub(crate) size_bytes: u64,
    pub(crate) last_modified: Option<DateTime<Local>>,
    pub(crate) markers: Vec<String>,
    pub(crate) git: Option<GitState>,
}
#[derive(Serialize)]
pub(crate) struct GitState {
    pub(crate) branch: Option<String>,
    pub(crate) dirty: Option<bool>,
}
pub(crate) struct Recovery {
    pub(crate) remote: String,
    pub(crate) remote_url: String,
    pub(crate) branch: String,
    pub(crate) commit: String,
    pub(crate) upstream: String,
}
fn is_project(path: &Path) -> bool {
    MARKERS.iter().any(|m| path.join(m).exists())
}
pub(crate) fn discover(root: &Path, all: bool) -> anyhow::Result<Vec<Project>> {
    let root = crate::expand(root);
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
pub(crate) fn inspect(path: &Path) -> anyhow::Result<Project> {
    let path = fs::canonicalize(path)?;
    let markers = MARKERS
        .iter()
        .filter(|m| path.join(m).exists())
        .map(|m| m.to_string())
        .collect();
    let mut size = 0u64;
    let mut latest = None;
    for entry in WalkDir::new(&path).follow_links(false) {
        let e = entry?;
        if e.file_type().is_file() {
            let Some(m) = metadata_if_present(e.path())? else {
                continue;
            };
            if !m.file_type().is_file() {
                continue;
            }
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

pub(crate) fn metadata_if_present(path: &Path) -> std::io::Result<Option<fs::Metadata>> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => Ok(Some(metadata)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
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

pub(crate) fn github_url(remote: &str) -> Option<String> {
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

pub(crate) fn inspect_recovery(project: &Project) -> Result<Recovery, String> {
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

pub(crate) fn verify_local_tags(path: &Path, remote: &str) -> Result<(), String> {
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
