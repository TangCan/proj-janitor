use chrono::{Local, Utc};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    fs,
    path::PathBuf,
};

use crate::{
    Manifest, Record,
    archive::{create_archive, create_private_dir, portable_path_key, sha256, write_new_file},
    expand, human,
    project::{discover, inspect_recovery},
    resolve_for_creation,
};

const VERSION: &str = env!("CARGO_PKG_VERSION");

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
                    "[archive] {}: {} -> {}{}",
                    p.name,
                    human(p.size_bytes),
                    archive_path.display(),
                    if exclude_git_metadata {
                        " [Git metadata exclusion enabled; restored archive has no Git metadata]"
                    } else {
                        ""
                    }
                );
            }
            continue;
        }
        let mut archive_created = false;
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
            create_archive(
                &p.path,
                &archive_path,
                include_artifacts,
                exclude_git_metadata,
            )?;
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
                    exclude_git_metadata: exclude_git_metadata && record.strategy == "archive",
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
                        "[archive] {}: {} sha256:{}{}",
                        record.project,
                        human(record.archive_bytes),
                        &record.sha256.as_deref().unwrap_or_default()[..12],
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
