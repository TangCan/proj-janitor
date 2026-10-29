use chrono::{DateTime, Local, Utc};
use serde::Serialize;
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::Read,
    path::{Component, Path, PathBuf},
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

pub(crate) struct Submodule {
    pub(crate) path: PathBuf,
    pub(crate) url: String,
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

pub(crate) fn git_output(path: &Path, args: &[&str]) -> Option<String> {
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

pub(crate) fn submodule_config(path: &Path) -> Result<Vec<Submodule>, String> {
    let gitmodules = path.join(".gitmodules");
    match fs::symlink_metadata(&gitmodules) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(_) => return Err("cannot inspect .gitmodules".into()),
        Ok(metadata) if !metadata.is_file() || metadata.file_type().is_symlink() => {
            return Err("invalid .gitmodules file".into());
        }
        Ok(_) => {}
    }
    let output = git_run(
        path,
        &[
            "config",
            "--file",
            ".gitmodules",
            "--get-regexp",
            r"^submodule\..*\.(path|url)$",
        ],
    )
    .ok_or("cannot inspect .gitmodules")?;
    if !(output.status.success() || output.status.code() == Some(1) && output.stdout.is_empty()) {
        return Err("cannot inspect .gitmodules".into());
    }
    let mut entries: HashMap<String, (Option<PathBuf>, Option<String>)> = HashMap::new();
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        let Some((key, value)) = line.split_once(' ') else {
            return Err("invalid .gitmodules entry".into());
        };
        let (name, is_path) = if let Some(name) = key
            .strip_prefix("submodule.")
            .and_then(|key| key.strip_suffix(".path"))
        {
            (name, true)
        } else if let Some(name) = key
            .strip_prefix("submodule.")
            .and_then(|key| key.strip_suffix(".url"))
        {
            (name, false)
        } else {
            return Err("invalid .gitmodules entry".into());
        };
        if name.is_empty() || value.is_empty() || value.contains('\0') {
            return Err("invalid .gitmodules entry".into());
        }
        let entry = entries.entry(name.to_owned()).or_default();
        if is_path {
            let submodule_path = PathBuf::from(value);
            if value.contains('\\')
                || submodule_path.is_absolute()
                || submodule_path.components().any(|component| {
                    matches!(
                        component,
                        Component::ParentDir
                            | Component::CurDir
                            | Component::RootDir
                            | Component::Prefix(_)
                    )
                })
                || submodule_path.as_os_str().is_empty()
            {
                return Err("unsafe submodule path".into());
            }
            if entry.0.replace(submodule_path).is_some() {
                return Err("duplicate submodule path".into());
            }
        } else if entry.1.replace(value.to_owned()).is_some() {
            return Err("duplicate submodule URL".into());
        }
    }
    let mut paths = HashSet::new();
    entries
        .into_values()
        .map(|(path, url)| match (path, url) {
            (Some(path), Some(url)) if paths.insert(path.clone()) => Ok(Submodule { path, url }),
            (Some(_), Some(_)) => Err("duplicate submodule path".into()),
            _ => Err("incomplete .gitmodules entry".into()),
        })
        .collect()
}

pub(crate) fn submodule_url_supported(url: &str) -> bool {
    if url.starts_with("./") || url.starts_with("../") {
        !url.chars()
            .any(|character| character.is_whitespace() || "\\?#:".contains(character))
    } else if url.starts_with("https://") {
        Url::parse(url).is_ok_and(|url| {
            url.username().is_empty() && url.password().is_none() && !url.as_str().contains('@')
        }) && github_url(url).is_some()
    } else {
        github_url(url).is_some()
    }
}

pub(crate) fn url_has_sensitive_data(url: &str) -> bool {
    if url.contains(['?', '#']) {
        return true;
    }
    let Some((scheme, remainder)) = url.split_once("://") else {
        return false;
    };
    let authority = remainder.split('/').next().unwrap_or(remainder);
    if !authority.contains('@') {
        return false;
    }
    let Ok(parsed) = Url::parse(url) else {
        return true;
    };
    !(scheme == "ssh"
        && parsed.username() == "git"
        && parsed.password().is_none()
        && parsed.host_str() == Some("github.com")
        && parsed.port().is_none_or(|port| port == 22))
}

fn git_lfs_recovery_reason(path: &Path) -> Option<String> {
    let lfsconfig = path.join(".lfsconfig");
    if lfsconfig.exists() || lfsconfig.is_symlink() {
        return Some("Git LFS is not supported".into());
    }
    let Some(attributes) = git_run(path, &["grep", "-l", "filter=lfs", "--", "*.gitattributes"])
    else {
        return Some("cannot inspect Git LFS attributes".into());
    };
    if !attributes.status.success() {
        if attributes.status.code() == Some(1) {
            return None;
        }
        return Some("cannot inspect Git LFS attributes".into());
    }
    let Some(endpoint) = git_run(
        path,
        &[
            "config",
            "--get-regexp",
            r"^(lfs\..*\.url|remote\..*\.lfsurl)$",
        ],
    ) else {
        return Some("cannot inspect Git LFS configuration".into());
    };
    if endpoint.status.success() && !endpoint.stdout.is_empty() {
        return Some("custom Git LFS endpoints are not supported".into());
    }
    if !endpoint.status.success() && endpoint.status.code() != Some(1) {
        return Some("cannot inspect Git LFS configuration".into());
    }
    if !git_run(path, &["lfs", "version"]).is_some_and(|output| output.status.success()) {
        return Some("Git LFS is not installed".into());
    }
    None
}

pub(crate) fn inspect_submodules(path: &Path) -> Result<(), String> {
    let mut pending = vec![(path.to_path_buf(), submodule_config(path)?, String::new())];
    while let Some((current, configured, prefix)) = pending.pop() {
        if let Some(reason) = git_lfs_recovery_reason(&current) {
            return Err(if prefix.is_empty() {
                reason
            } else {
                format!("{reason}: {}", prefix.trim_end_matches('/'))
            });
        }
        if configured
            .iter()
            .any(|module| url_has_sensitive_data(&module.url))
        {
            return Err("submodule URL contains credentials or query data".into());
        }
        if configured
            .iter()
            .any(|module| !submodule_url_supported(&module.url))
        {
            return Err("submodule URL is not a supported GitHub URL".into());
        }
        let output = git_run(
            &current,
            &["-c", "core.quotePath=false", "submodule", "status"],
        )
        .filter(|output| output.status.success())
        .ok_or("cannot inspect submodules")?;
        let mut status_paths = HashSet::new();
        for line in String::from_utf8_lossy(&output.stdout).lines() {
            let Some(marker) = line.chars().next() else {
                return Err("cannot parse submodule status".into());
            };
            let Some((_, display_path)) = line[marker.len_utf8()..].trim_start().split_once(' ')
            else {
                return Err("cannot parse submodule status".into());
            };
            let module = configured
                .iter()
                .filter(|module| {
                    let configured_path = module.path.to_string_lossy();
                    display_path == configured_path
                        || (display_path.starts_with(&format!("{configured_path} ("))
                            && display_path.ends_with(')'))
                })
                .max_by_key(|module| module.path.as_os_str().len())
                .ok_or("cannot match configured submodules to Git links")?;
            let relative = &module.path;
            let raw_path = format!("{prefix}{}", relative.display());
            status_paths.insert(relative.clone());
            match marker {
                '-' => {
                    let uninitialized = current.join(relative);
                    match fs::symlink_metadata(&uninitialized) {
                        Ok(metadata) => {
                            if !metadata.is_dir()
                                || metadata.file_type().is_symlink()
                                || fs::read_dir(&uninitialized)
                                    .map_err(|_| "cannot inspect uninitialized submodule")?
                                    .next()
                                    .is_some()
                            {
                                return Err(format!(
                                    "uninitialized submodule path is not empty: {raw_path}"
                                ));
                            }
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                        Err(_) => return Err("cannot inspect uninitialized submodule".into()),
                    }
                    continue;
                }
                '+' => {
                    return Err(format!(
                        "submodule is not at its recorded commit: {raw_path}"
                    ));
                }
                'U' => return Err(format!("submodule has merge conflicts: {raw_path}")),
                ' ' => {}
                _ => return Err("cannot parse submodule status".into()),
            }
            let submodule_path = fs::canonicalize(current.join(relative))
                .map_err(|_| format!("cannot inspect submodule working tree: {raw_path}"))?;
            if !submodule_path.starts_with(&current) {
                return Err(format!("submodule path escapes project: {raw_path}"));
            }
            let status = git_output(
                &submodule_path,
                &[
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--ignore-submodules=none",
                ],
            )
            .ok_or_else(|| format!("cannot inspect submodule working tree: {raw_path}"))?;
            if !status.is_empty() {
                return Err(format!("submodule working tree is not clean: {raw_path}"));
            }
            let nested = submodule_config(&submodule_path)
                .map_err(|_| format!("cannot inspect nested submodules: {raw_path}"))?;
            pending.push((submodule_path.clone(), nested, raw_path.clone() + "/"));
            let ignored = git_run(
                &submodule_path,
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
            .filter(|output| output.status.success())
            .ok_or_else(|| format!("cannot inspect ignored submodule files: {raw_path}"))?;
            for raw in ignored
                .stdout
                .split(|byte| *byte == 0)
                .filter(|s| !s.is_empty())
            {
                let ignored_path = String::from_utf8_lossy(raw)
                    .trim_end_matches('/')
                    .to_owned();
                if !ignored_is_artifact(&submodule_path, Path::new(&ignored_path)) {
                    return Err(format!(
                        "ignored submodule path is not an artifact: {raw_path}/{ignored_path}"
                    ));
                }
            }
        }
        let configured_paths: HashSet<_> = configured.into_iter().map(|item| item.path).collect();
        if !configured_paths.is_subset(&status_paths) {
            return Err("cannot match configured submodules to Git links".into());
        }
    }
    Ok(())
}

pub(crate) fn inspect_recovery(project: &Project) -> Result<Recovery, String> {
    let recovery = inspect_recovery_inner(project);
    if recovery.is_ok() {
        return recovery;
    }
    if let Some(remotes) = git_output(&project.path, &["remote"]) {
        for remote in remotes.lines().filter(|remote| !remote.is_empty()) {
            if git_output(&project.path, &["remote", "get-url", remote])
                .is_some_and(|url| url_has_sensitive_data(&url))
            {
                return Err("remote URL contains credentials or query data".into());
            }
        }
    }
    recovery
}

fn inspect_recovery_inner(project: &Project) -> Result<Recovery, String> {
    let path = &project.path;
    let branch = git_output(path, &["symbolic-ref", "--quiet", "--short", "HEAD"])
        .filter(|s| !s.is_empty())
        .ok_or("detached HEAD")?;
    let status = git_output(
        path,
        &[
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
    )
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
    if url_has_sensitive_data(&raw_url) && github_url(&raw_url).is_none() {
        return Err("remote URL contains credentials or query data".into());
    }
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
    inspect_submodules(path)?;
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
    // These paths are generated by project setup/build commands.
    if [
        Path::new(".husky/_"),
        Path::new("src/generated"),
        Path::new("src/shared/proto"),
        Path::new("webview-ui/src/services/grpc-client.ts"),
    ]
    .iter()
    .any(|generated| path == *generated || path.starts_with(generated))
    {
        return true;
    }
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
