use same_file::Handle;
use serde_json::Value;
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::{self, IsTerminal, Write},
    path::{Path, PathBuf},
};

use crate::{
    archive::{archive_path_matches, safe_windows_component},
    human,
};

pub(crate) struct PruneFile {
    pub(crate) project: String,
    pub(crate) kind: String,
    pub(crate) path: PathBuf,
    pub(crate) size_bytes: u64,
    identity: Handle,
}

pub(crate) struct PruneArtifact {
    pub(crate) file: PruneFile,
    manifests: Vec<PathBuf>,
}

pub(crate) struct PrunePlan {
    pub(crate) backup_root: PathBuf,
    pub(crate) keep: usize,
    pub(crate) kept_manifests: usize,
    pub(crate) manifests: Vec<PruneFile>,
    pub(crate) artifacts: Vec<PruneArtifact>,
}

pub(crate) struct DeletedFile {
    pub(crate) project: String,
    pub(crate) kind: String,
    pub(crate) path: PathBuf,
    pub(crate) size_bytes: u64,
}

pub(crate) struct PruneResult {
    pub(crate) deleted: Vec<DeletedFile>,
    pub(crate) errors: Vec<String>,
}

pub(crate) fn prune_cmd(
    backup_root: &Path,
    keep: usize,
    apply: bool,
    yes: bool,
    json: bool,
) -> anyhow::Result<()> {
    let plan = plan_retention(backup_root, keep)?;
    let files: Vec<_> = plan
        .manifests
        .iter()
        .chain(plan.artifacts.iter().map(|artifact| &artifact.file))
        .collect();
    if !apply {
        if json {
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "dry_run": true,
                    "backup_root": plan.backup_root,
                    "keep": plan.keep,
                    "kept_manifests": plan.kept_manifests,
                    "files": files.iter().map(|file| serde_json::json!({
                        "project": file.project,
                        "kind": file.kind,
                        "path": file.path,
                        "size_bytes": file.size_bytes,
                    })).collect::<Vec<_>>(),
                }))?
            );
        } else if files.is_empty() {
            println!("Nothing to prune.");
        } else {
            println!(
                "Prune plan for {} (keep {} per project):",
                plan.backup_root.display(),
                plan.keep
            );
            for file in &files {
                println!("  [{}] {}", file.kind, file.path.display());
            }
            let bytes: u64 = files.iter().map(|file| file.size_bytes).sum();
            println!(
                "{} manifest(s), {} artifact(s), {} reclaimable.",
                plan.manifests.len(),
                plan.artifacts.len(),
                human(bytes)
            );
            println!("Dry run. Re-run with --apply to delete these files.");
        }
        return Ok(());
    }
    if files.is_empty() {
        if json {
            println!(
                "{}",
                serde_json::json!({
                    "dry_run": false,
                    "backup_root": plan.backup_root,
                    "keep": plan.keep,
                    "kept_manifests": plan.kept_manifests,
                    "files": [],
                    "deleted": [],
                    "errors": [],
                })
            );
        } else {
            println!("Nothing to prune.");
        }
        return Ok(());
    }
    if json {
        anyhow::ensure!(yes, "--yes is required for prune --apply with --json");
    } else {
        eprintln!("Files to delete:");
        for file in &files {
            eprintln!("  [{}] {}", file.kind, file.path.display());
        }
        if !yes {
            anyhow::ensure!(
                io::stdin().is_terminal(),
                "refusing to prune without --yes in a non-interactive session"
            );
            eprint!("Delete {} file(s)? [y/N] ", files.len());
            io::stderr().flush()?;
            let mut answer = String::new();
            io::stdin().read_line(&mut answer)?;
            anyhow::ensure!(
                matches!(answer.trim().to_lowercase().as_str(), "y" | "yes"),
                "Aborted"
            );
        }
    }

    let backup_root = plan.backup_root.clone();
    let keep = plan.keep;
    let kept_manifests = plan.kept_manifests;
    let files_json = files
        .iter()
        .map(|file| {
            serde_json::json!({
                "project": file.project,
                "kind": file.kind,
                "path": file.path,
                "size_bytes": file.size_bytes,
            })
        })
        .collect::<Vec<_>>();
    let result = apply_retention_plan(plan);
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "dry_run": false,
                "backup_root": backup_root,
                "keep": keep,
                "kept_manifests": kept_manifests,
                "files": files_json,
                "deleted": result.deleted.iter().map(|file| serde_json::json!({
                    "project": file.project,
                    "kind": file.kind,
                    "path": file.path,
                })).collect::<Vec<_>>(),
                "errors": result.errors,
            }))?
        );
    } else {
        let bytes: u64 = result.deleted.iter().map(|file| file.size_bytes).sum();
        println!(
            "Deleted {} file(s), freed {}.",
            result.deleted.len(),
            human(bytes)
        );
        for error in &result.errors {
            eprintln!("error: {error}");
        }
    }
    anyhow::ensure!(
        result.errors.is_empty(),
        "one or more prune operations failed"
    );
    Ok(())
}

pub(crate) fn retain_after_backup(root: &Path, keep: usize, yes: bool) -> anyhow::Result<()> {
    let plan = plan_retention(root, keep)?;
    let files: Vec<_> = plan
        .manifests
        .iter()
        .chain(plan.artifacts.iter().map(|artifact| &artifact.file))
        .collect();
    if files.is_empty() {
        return Ok(());
    }
    println!("Retention (keep {keep}):");
    for file in &files {
        println!("  [{}] {}", file.kind, file.path.display());
    }
    let total_bytes: u64 = files.iter().map(|file| file.size_bytes).sum();
    println!(
        "{} file(s), {} would be removed.",
        files.len(),
        human(total_bytes)
    );
    if !yes {
        if !io::stdin().is_terminal() {
            println!("Non-interactive; skipping retention (use --yes to delete).");
            return Ok(());
        }
        eprint!("Delete {} file(s)? [y/N] ", files.len());
        io::stderr().flush()?;
        let mut answer = String::new();
        io::stdin().read_line(&mut answer)?;
        if !matches!(answer.trim().to_lowercase().as_str(), "y" | "yes") {
            println!("Retention skipped.");
            return Ok(());
        }
    }
    let result = apply_retention_plan(plan);
    println!("Deleted {} file(s).", result.deleted.len());
    for error in &result.errors {
        eprintln!("error: {error}");
    }
    anyhow::ensure!(
        result.errors.is_empty(),
        "one or more retention operations failed"
    );
    Ok(())
}

struct ManifestRow {
    path: PathBuf,
    project: String,
    artifact: Option<PathBuf>,
    identity: Handle,
    size_bytes: u64,
}

pub(crate) fn plan_retention(root: &Path, keep: usize) -> anyhow::Result<PrunePlan> {
    anyhow::ensure!(keep > 0, "--keep must be at least 1");
    let root = fs::canonicalize(root)?;
    anyhow::ensure!(
        root.is_dir(),
        "backup root is not a directory: {}",
        root.display()
    );
    let mut directories: Vec<_> = fs::read_dir(&root)?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<Result<_, _>>()?;
    directories.sort();

    let mut manifests_to_delete = Vec::new();
    let mut kept_artifacts = HashSet::new();
    let mut kept_manifests = 0;
    let mut artifact_refs: HashMap<PathBuf, (PruneFile, HashSet<PathBuf>)> = HashMap::new();
    for directory in directories {
        let metadata = fs::symlink_metadata(&directory)?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() || legacy_run_dir(&directory) {
            continue;
        }
        let mut rows = Vec::new();
        for entry in fs::read_dir(&directory)? {
            let path = entry?.path();
            let Some((stamp, suffix, stamp_name)) = manifest_stamp(&path) else {
                continue;
            };
            let metadata = match fs::symlink_metadata(&path) {
                Ok(metadata) if metadata.is_file() && !metadata.file_type().is_symlink() => {
                    metadata
                }
                Ok(_) => continue,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error.into()),
            };
            let manifest: Value = serde_json::from_slice(&fs::read(&path)?)?;
            let record = manifest_record(&manifest, &path)?;
            let artifact = artifact_path(&directory, &path, &stamp_name, record)?;
            let identity = Handle::from_path(&path)?;
            rows.push((
                stamp,
                suffix,
                ManifestRow {
                    path,
                    project: record["project"].as_str().unwrap().to_owned(),
                    artifact,
                    identity,
                    size_bytes: metadata.len(),
                },
            ));
        }
        if rows.is_empty() {
            continue;
        }
        let project_names: HashSet<_> = rows
            .iter()
            .map(|(_, _, row)| row.project.as_str())
            .collect();
        anyhow::ensure!(
            project_names.len() == 1,
            "multiple projects share a backup directory: {}",
            directory.display()
        );
        rows.sort_by(|left, right| (&left.0, left.1).cmp(&(&right.0, right.1)));
        let split = rows.len().saturating_sub(keep);
        let kept_rows = rows.split_off(split);
        // Scheme A: keep incremental chains whole. A retained snapshot's base
        // chain (via `incremental.base`) must not be pruned.
        let mut protected: HashSet<PathBuf> = HashSet::new();
        for (_, _, row) in &kept_rows {
            let mut current = row.path.clone();
            loop {
                if !protected.insert(current.clone()) {
                    break;
                }
                let text = match fs::read_to_string(&current) {
                    Ok(text) => text,
                    Err(_) => break,
                };
                let manifest: Value = match serde_json::from_str(&text) {
                    Ok(manifest) => manifest,
                    Err(_) => break,
                };
                let Some(base) = manifest["records"][0]["incremental"]["base"].as_str() else {
                    break;
                };
                let next = directory.join(base);
                if next == current {
                    break;
                }
                current = next;
            }
        }
        for (_, _, row) in kept_rows {
            if let Some(artifact) = row.artifact {
                kept_artifacts.insert(artifact);
            }
            kept_manifests += 1;
        }
        for (_, _, row) in rows.drain(..split) {
            if protected.contains(&row.path) {
                if let Some(artifact) = row.artifact {
                    kept_artifacts.insert(artifact);
                }
                kept_manifests += 1;
                continue;
            }
            if let Some(artifact) = &row.artifact {
                match fs::symlink_metadata(artifact) {
                    Ok(metadata) if metadata.is_file() && !metadata.file_type().is_symlink() => {
                        if !kept_artifacts.contains(artifact) {
                            let artifact_identity = Handle::from_path(artifact)?;
                            let kind = if artifact.extension().is_some_and(|ext| ext == "bundle") {
                                "bundle"
                            } else {
                                "archive"
                            };
                            let file = PruneFile {
                                project: row.project.clone(),
                                kind: kind.to_owned(),
                                path: artifact.clone(),
                                size_bytes: metadata.len(),
                                identity: artifact_identity,
                            };
                            match artifact_refs.get_mut(artifact) {
                                Some((_, manifests)) => {
                                    manifests.insert(row.path.clone());
                                }
                                None => {
                                    artifact_refs.insert(
                                        artifact.clone(),
                                        (file, HashSet::from([row.path.clone()])),
                                    );
                                }
                            }
                        }
                    }
                    Ok(_) => anyhow::bail!(
                        "refusing to prune non-regular artifact: {}",
                        artifact.display()
                    ),
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error.into()),
                }
            }
            manifests_to_delete.push(PruneFile {
                project: row.project,
                kind: "manifest".into(),
                path: row.path,
                size_bytes: row.size_bytes,
                identity: row.identity,
            });
        }
    }
    manifests_to_delete.sort_by(|left, right| left.path.cmp(&right.path));
    let mut artifacts = artifact_refs
        .into_values()
        .map(|(file, manifests)| {
            let mut manifests: Vec<_> = manifests.into_iter().collect();
            manifests.sort();
            PruneArtifact { file, manifests }
        })
        .collect::<Vec<_>>();
    artifacts.sort_by(|left, right| left.file.path.cmp(&right.file.path));
    Ok(PrunePlan {
        backup_root: root,
        keep,
        kept_manifests,
        manifests: manifests_to_delete,
        artifacts,
    })
}

pub(crate) fn apply_retention_plan(plan: PrunePlan) -> PruneResult {
    let mut deleted = Vec::new();
    let mut errors = Vec::new();
    let mut deleted_manifests = HashSet::new();
    for file in plan.manifests {
        let path = file.path.clone();
        match remove_planned_file(file) {
            Ok(Some(file)) => {
                deleted_manifests.insert(path);
                deleted.push(file);
            }
            Ok(None) => {}
            Err(error) => errors.push(error),
        }
    }
    for artifact in plan.artifacts {
        if !artifact
            .manifests
            .iter()
            .all(|path| deleted_manifests.contains(path))
        {
            continue;
        }
        match remove_planned_file(artifact.file) {
            Ok(Some(file)) => deleted.push(file),
            Ok(None) => {}
            Err(error) => errors.push(error),
        }
    }
    PruneResult { deleted, errors }
}

fn remove_planned_file(file: PruneFile) -> Result<Option<DeletedFile>, String> {
    let path = file.path.clone();
    let same = fs::symlink_metadata(&path).is_ok_and(|metadata| {
        metadata.is_file()
            && !metadata.file_type().is_symlink()
            && archive_path_matches(&path, &file.identity)
    });
    drop(file.identity);
    if !same {
        if path.exists() || path.is_symlink() {
            return Err(format!("{}: changed since prune plan", path.display()));
        }
        return Ok(None);
    }
    // shortcut: a same-user replacement can race this check and unlink; use directory capabilities if concurrent writers are in scope.
    match fs::remove_file(&path) {
        Ok(()) => Ok(Some(DeletedFile {
            project: file.project,
            kind: file.kind,
            path,
            size_bytes: file.size_bytes,
        })),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!("{}: {error}", path.display())),
    }
}

fn manifest_record<'a>(manifest: &'a Value, path: &Path) -> anyhow::Result<&'a Value> {
    anyhow::ensure!(
        manifest["schema_version"] == 2,
        "unsupported manifest: {}",
        path.display()
    );
    let records = manifest["records"]
        .as_array()
        .filter(|records| records.len() == 1)
        .ok_or_else(|| anyhow::anyhow!("invalid manifest records: {}", path.display()))?;
    let record = &records[0];
    record["project"]
        .as_str()
        .filter(|project| safe_project_name(project))
        .ok_or_else(|| anyhow::anyhow!("invalid manifest project: {}", path.display()))?;
    anyhow::ensure!(
        matches!(
            record["strategy"].as_str(),
            Some("git" | "archive" | "git_bundle")
        ),
        "unsupported manifest strategy: {}",
        path.display()
    );
    Ok(record)
}

fn artifact_path(
    project_dir: &Path,
    manifest_path: &Path,
    stamp: &str,
    record: &Value,
) -> anyhow::Result<Option<PathBuf>> {
    let strategy = record["strategy"].as_str().unwrap();
    if strategy == "git" {
        return Ok(None);
    }
    let project = record["project"].as_str().unwrap();
    let raw = record["archive"]
        .as_str()
        .filter(|raw| !raw.is_empty())
        .ok_or_else(|| anyhow::anyhow!("missing artifact path in {}", manifest_path.display()))?;
    let suffix = if strategy == "git_bundle" {
        ".bundle"
    } else {
        ".tar.gz"
    };
    let expected = format!("{project}-{stamp}{suffix}");
    anyhow::ensure!(
        Path::new(raw)
            .file_name()
            .is_some_and(|name| name == std::ffi::OsStr::new(&expected)),
        "unexpected artifact path in manifest: {}",
        manifest_path.display()
    );
    let raw_path = Path::new(raw);
    let raw_parent_is_project = raw_path
        .parent()
        .and_then(|parent| fs::canonicalize(parent).ok())
        .is_some_and(|parent| parent == project_dir);
    anyhow::ensure!(
        (raw_path.is_absolute() || raw_path.components().count() == 1)
            && !(raw_path.is_absolute() && raw_path.exists() && !raw_parent_is_project),
        "artifact is outside its project directory: {}",
        manifest_path.display()
    );
    Ok(Some(project_dir.join(expected)))
}

fn manifest_stamp(path: &Path) -> Option<(String, u64, String)> {
    let name = path.file_name()?.to_str()?;
    let stamp = name.strip_prefix("manifest-")?.strip_suffix(".json")?;
    let mut parts = stamp.split('-');
    let date = parts.next()?;
    let time = parts.next()?;
    let suffix = parts.next();
    if date.len() != 8
        || time.len() != 6
        || !date.bytes().all(|byte| byte.is_ascii_digit())
        || !time.bytes().all(|byte| byte.is_ascii_digit())
    {
        return None;
    }
    let number = match suffix {
        Some(number) if parts.next().is_none() => {
            let parsed = number.parse::<u64>().ok()?;
            if parsed < 2 || parsed.to_string() != number {
                return None;
            }
            parsed
        }
        None => 1,
        _ => return None,
    };
    Some((format!("{date}-{time}"), number, stamp.to_owned()))
}

fn legacy_run_dir(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    let Some(stamp) = name.strip_prefix("backup-") else {
        return false;
    };
    let mut parts = stamp.split('-');
    let Some(date) = parts.next() else {
        return false;
    };
    let Some(time) = parts.next() else {
        return false;
    };
    parts.next().is_none()
        && date.len() == 8
        && time.len() == 6
        && date.bytes().all(|byte| byte.is_ascii_digit())
        && time.bytes().all(|byte| byte.is_ascii_digit())
}

fn safe_project_name(project: &str) -> bool {
    !project.is_empty()
        && project != "."
        && project != ".."
        && !project
            .chars()
            .any(|character| matches!(character, '/' | '\\' | '\0'))
        && safe_windows_component(project)
}
