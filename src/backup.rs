use chrono::{Local, Utc};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

use crate::{
    Manifest, Record,
    archive::{
        archive_path_matches, copy_new_file, create_archive, create_private_dir,
        create_private_temp_dir, portable_path_key, remove_file_if_same, safe_windows_component,
        write_new_file,
    },
    expand, human,
    project::{discover, inspect_bundle_recovery, inspect_recovery},
    resolve_for_creation,
};

const VERSION: &str = env!("CARGO_PKG_VERSION");

fn blocked_backup_reason(reason: &str) -> bool {
    [
        "remote URL contains credentials or query data",
        "submodule URL contains credentials or query data",
        "Git config contains HTTP extraHeader",
        "Git config contains credential passwords",
        "Git config contains an inline credential helper",
        "Git config contains sensitive credential helper arguments",
        "Git config contains a custom SSH command",
        "Git config points to a credential file inside the project",
        "Git config contains credentials or query data in a service URL",
        "Git config contains credentials in a URL rewrite",
        "LFS config contains credentials or query data",
        "cannot safely inspect LFS config for credentials",
        "cannot safely inspect .gitmodules for credentials",
        "cannot safely inspect Git config for credentials",
    ]
    .iter()
    .any(|prefix| reason.starts_with(prefix))
}

fn create_git_bundle(
    project: &Path,
    bundle: &Path,
) -> anyhow::Result<(String, u64, same_file::Handle)> {
    let temporary = create_private_temp_dir(
        bundle
            .parent()
            .ok_or_else(|| anyhow::anyhow!("bundle has no parent"))?,
    )?;
    let temporary_bundle = temporary.join("bundle");
    let result = (|| -> anyhow::Result<_> {
        let output = Command::new("git")
            .arg("-C")
            .arg(project)
            .args(["bundle", "create"])
            .arg(&temporary_bundle)
            .arg("--all")
            .stdin(Stdio::null())
            .output()?;
        anyhow::ensure!(
            output.status.success(),
            "git bundle creation failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
        Ok(copy_new_file(&temporary_bundle, bundle)?)
    })();
    let _ = fs::remove_dir_all(&temporary);
    result
}

pub(crate) fn backup_cmd(
    roots: &[PathBuf],
    dest: Option<PathBuf>,
    dry: bool,
    include_artifacts: bool,
    always: bool,
    exclude_git_metadata: bool,
) -> anyhow::Result<()> {
    let dest = dest
        .filter(|path| !path.as_os_str().is_empty())
        .ok_or_else(|| anyhow::anyhow!("set BACKUP_DEST or pass --backup-dest"))?;
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
    for project in &projects {
        anyhow::ensure!(
            safe_windows_component(&project.name),
            "unsafe project name: {}",
            project.name
        );
    }
    let mut name_counts = HashMap::new();
    for project in &projects {
        *name_counts
            .entry(portable_path_key(&project.name))
            .or_insert(0) += 1;
    }
    let project_dirs: Vec<_> = projects
        .iter()
        .map(|project| {
            if name_counts[&portable_path_key(&project.name)] > 1 {
                let path_hash = format!(
                    "{:x}",
                    Sha256::digest(project.path.to_string_lossy().as_bytes())
                );
                format!("{}-{}", project.name, &path_hash[..8])
            } else {
                project.name.clone()
            }
        })
        .collect();
    for (project, dir_name) in projects.iter().zip(&project_dirs) {
        let output_dir = resolve_for_creation(&dest.join(dir_name))?;
        anyhow::ensure!(
            !output_dir.starts_with(&project.path),
            "backup destination is inside project: {} -> {}",
            output_dir.display(),
            project.path.display()
        );
    }
    if !dry {
        create_private_dir(&dest)?;
    }
    let timestamp = Local::now().format("%Y%m%d-%H%M%S").to_string();
    for (p, dir_name) in projects.iter().zip(&project_dirs) {
        let dir = dest.join(dir_name);
        let mut run_stamp = timestamp.clone();
        let mut suffix = 2;
        while dir.join(format!("manifest-{run_stamp}.json")).exists()
            || dir.join(format!("{}-{run_stamp}.tar.gz", p.name)).exists()
            || dir.join(format!("{}-{run_stamp}.bundle", p.name)).exists()
        {
            run_stamp = format!("{timestamp}-{suffix}");
            suffix += 1;
        }
        let manifest_path = dir.join(format!("manifest-{run_stamp}.json"));
        let mut archive_path = dir.join(format!("{}-{run_stamp}.tar.gz", p.name));
        let bundle_path = dir.join(format!("{}-{run_stamp}.bundle", p.name));
        let (recovery, bundle_recovery, fallback_reason) = if include_artifacts {
            (None, None, Some("include_artifacts".to_owned()))
        } else if always {
            (None, None, Some("always_archive".to_owned()))
        } else {
            match inspect_recovery(p) {
                Ok(recovery) => (Some(recovery), None, None),
                Err(reason) => {
                    if exclude_git_metadata || blocked_backup_reason(&reason) {
                        (None, None, Some(reason))
                    } else {
                        match inspect_bundle_recovery(p) {
                            Ok(bundle) => (None, Some(bundle), Some(reason)),
                            Err(_) => (None, None, Some(reason)),
                        }
                    }
                }
            }
        };
        let blocked = !include_artifacts
            && !always
            && bundle_recovery.is_none()
            && fallback_reason
                .as_deref()
                .is_some_and(blocked_backup_reason);
        if blocked {
            let reason = fallback_reason.as_deref().unwrap_or_default();
            if dry {
                println!("[error] {}: {reason}", p.name);
            } else {
                eprintln!("error: {}: {reason}", p.name);
            }
            failed = true;
            continue;
        }
        if dry {
            if let Some(git) = &recovery {
                println!(
                    "[git] {}: {} @ {}",
                    p.name,
                    git.remote_url,
                    &git.commit[..12.min(git.commit.len())]
                );
            } else if let Some(bundle) = &bundle_recovery {
                let reason = fallback_reason
                    .as_deref()
                    .map(|reason| format!(" ({reason})"))
                    .unwrap_or_default();
                println!(
                    "[git-bundle] {}: {} -> {}{}",
                    p.name,
                    &bundle.commit[..12.min(bundle.commit.len())],
                    bundle_path.display(),
                    reason
                );
            } else {
                let reason = fallback_reason
                    .as_deref()
                    .map(|reason| format!(" ({reason})"))
                    .unwrap_or_default();
                println!(
                    "[archive] {}: {} -> {}{}{}",
                    p.name,
                    human(p.size_bytes),
                    archive_path.display(),
                    reason,
                    if exclude_git_metadata {
                        " [Git metadata exclusion enabled; restored archive has no Git metadata]"
                    } else {
                        ""
                    }
                );
            }
            continue;
        }
        let mut archive_identity = None;
        let mut manifest_identity = None;
        let result = (|| -> anyhow::Result<Record> {
            create_private_dir(&dir)?;
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
            if let Some(bundle) = &bundle_recovery {
                let (hash, bytes, identity) = create_git_bundle(&p.path, &bundle_path)?;
                archive_identity = Some(identity);
                archive_path.clone_from(&bundle_path);
                Ok(Record {
                    project: p.name.clone(),
                    source: p.path.to_string_lossy().into(),
                    archive: Some(bundle_path.to_string_lossy().into()),
                    archive_bytes: bytes,
                    source_bytes: p.size_bytes,
                    sha256: Some(hash),
                    strategy: "git_bundle".into(),
                    remote: None,
                    remote_url: None,
                    branch: Some(bundle.branch.clone()),
                    commit: Some(bundle.commit.clone()),
                    upstream: None,
                    verified: Some("local-bundle".into()),
                    fallback_reason: fallback_reason.clone(),
                })
            } else {
                let (hash, bytes, identity) = create_archive(
                    &p.path,
                    &archive_path,
                    include_artifacts,
                    exclude_git_metadata,
                )?;
                archive_identity = Some(identity);
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
            }
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
                    exclude_git_metadata: exclude_git_metadata && record.strategy == "archive",
                    records: vec![record.clone()],
                };
                let write_result = (|| -> anyhow::Result<()> {
                    if let Some(identity) = &archive_identity {
                        anyhow::ensure!(
                            archive_path_matches(&archive_path, identity),
                            "archive path changed before manifest write: {}",
                            archive_path.display()
                        );
                    }
                    let mut bytes = serde_json::to_vec_pretty(&manifest)?;
                    bytes.push(b'\n');
                    manifest_identity = Some(write_new_file(&manifest_path, &bytes)?);
                    if let Some(identity) = &archive_identity {
                        anyhow::ensure!(
                            archive_path_matches(&archive_path, identity),
                            "archive path changed while manifest was written: {}",
                            archive_path.display()
                        );
                    }
                    Ok(())
                })();
                if let Err(e) = write_result {
                    if let Some(identity) = manifest_identity.take() {
                        remove_file_if_same(&manifest_path, identity);
                    }
                    if let Some(identity) = archive_identity.take() {
                        remove_file_if_same(&archive_path, identity);
                    }
                    eprintln!("error: {}: cannot write manifest: {e}", p.name);
                    failed = true;
                    continue;
                }
                drop(manifest_identity.take());
                if record.strategy == "git" {
                    println!(
                        "[git] {}: {} @ {}",
                        record.project,
                        record.remote_url.as_deref().unwrap_or("-"),
                        &record.commit.as_deref().unwrap_or_default()
                            [..12.min(record.commit.as_deref().unwrap_or_default().len())]
                    );
                } else if record.strategy == "git_bundle" {
                    let reason = record
                        .fallback_reason
                        .as_deref()
                        .map(|reason| format!(" ({reason})"))
                        .unwrap_or_default();
                    println!(
                        "[git-bundle] {}: {} {} sha256:{} {}{}",
                        record.project,
                        &record.commit.as_deref().unwrap_or_default()
                            [..12.min(record.commit.as_deref().unwrap_or_default().len())],
                        human(record.archive_bytes),
                        &record.sha256.as_deref().unwrap_or_default()[..12],
                        record.archive.as_deref().unwrap_or_default(),
                        reason
                    );
                } else {
                    let reason = record
                        .fallback_reason
                        .as_deref()
                        .map(|reason| format!(" ({reason})"))
                        .unwrap_or_default();
                    println!(
                        "[archive] {}: {} sha256:{}{}{}",
                        record.project,
                        human(record.archive_bytes),
                        &record.sha256.as_deref().unwrap_or_default()[..12],
                        reason,
                        if exclude_git_metadata {
                            " [Git metadata exclusion enabled; restored archive has no Git metadata]"
                        } else {
                            ""
                        }
                    );
                }
                println!("Manifest: {}", manifest_path.display());
            }
            Err(e) => {
                if let Some(identity) = archive_identity.take() {
                    remove_file_if_same(&archive_path, identity);
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
