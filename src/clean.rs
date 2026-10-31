use cap_std::{ambient_authority, fs::Dir};
use globset::{Glob, GlobSetBuilder};
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::{self, IsTerminal, Write},
    path::{Path, PathBuf},
};
use walkdir::WalkDir;

use crate::{RULES, human, project::discover};

pub(crate) fn clean_cmd(
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
    let mut seen = HashSet::new();
    let mut project_dirs = HashMap::new();
    for root in roots {
        match discover(root, false) {
            Ok(ps) => {
                for p in ps {
                    if !seen.insert(p.path.clone()) {
                        continue;
                    }
                    if apply {
                        let identity = same_file::Handle::from_path(&p.path)?;
                        let dir = open_cleanup_project(&p.path, &identity)?;
                        project_dirs.insert(p.path.clone(), dir);
                    }
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
                            let identity = match same_file::Handle::from_path(path) {
                                Ok(identity) => identity,
                                Err(error) => {
                                    eprintln!("error: {error}");
                                    failed = true;
                                    continue;
                                }
                            };
                            actions.push((
                                p.path.clone(),
                                path.to_owned(),
                                size,
                                rule,
                                kind,
                                identity,
                            ));
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
    actions.retain(|(_, path, _, _, _, _)| {
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
            .map(|(p, path, size, rule, kind, _)| serde_json::json!({"project":p,"path":path,"rule":rule,"kind":kind,"size_bytes":size}))
            .collect();
        println!("{}", serde_json::to_string_pretty(&vals)?);
    } else if actions.is_empty() {
        println!("Nothing to clean.")
    } else {
        for (_, p, s, _, _, _) in &actions {
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
    for (project, path, _, _, _, identity) in actions {
        let dir = project_dirs
            .get(&project)
            .ok_or_else(|| anyhow::anyhow!("missing cleanup project handle"))?;
        remove_cleanup_target(dir, &project, &path, identity)?;
    }
    if failed {
        anyhow::bail!("scan failures")
    }
    Ok(())
}

pub(crate) fn open_cleanup_project(
    project: &Path,
    planned_identity: &same_file::Handle,
) -> anyhow::Result<Dir> {
    let dir = Dir::open_ambient_dir(project, ambient_authority())?;
    let opened_identity = same_file::Handle::from_file(dir.try_clone()?.into_std_file())?;
    anyhow::ensure!(
        &opened_identity == planned_identity,
        "project root changed during cleanup planning: {}",
        project.display()
    );
    anyhow::ensure!(
        fs::canonicalize(project)? == project,
        "project root changed during cleanup planning: {}",
        project.display()
    );
    Ok(dir)
}

pub(crate) fn remove_cleanup_target(
    dir: &Dir,
    project: &Path,
    path: &Path,
    planned_identity: same_file::Handle,
) -> anyhow::Result<()> {
    let relative = path
        .strip_prefix(project)
        .map_err(|_| anyhow::anyhow!("target escapes project root: {}", path.display()))?;
    anyhow::ensure!(
        !relative.as_os_str().is_empty()
            && !relative.is_absolute()
            && !relative
                .components()
                .any(|part| matches!(part, std::path::Component::ParentDir)),
        "unsafe cleanup target: {}",
        path.display()
    );
    let metadata = dir.symlink_metadata(relative)?;
    anyhow::ensure!(
        !metadata.file_type().is_symlink(),
        "refusing to follow symlink: {}",
        path.display()
    );
    let current_identity = if metadata.is_dir() {
        same_file::Handle::from_file(dir.open_dir(relative)?.into_std_file())?
    } else {
        same_file::Handle::from_file(dir.open(relative)?.into_std())?
    };
    anyhow::ensure!(
        current_identity == planned_identity,
        "cleanup target changed since cleanup plan: {}",
        path.display()
    );
    drop(current_identity);
    drop(planned_identity);
    if metadata.is_dir() {
        dir.remove_dir_all(relative)?;
    } else {
        dir.remove_file(relative)?;
    }
    Ok(())
}

pub(crate) fn tree_size(path: &Path) -> anyhow::Result<u64> {
    let mut total = 0;
    for entry in WalkDir::new(path).follow_links(false) {
        let entry = entry?;
        if entry.file_type().is_file() {
            total += entry.metadata()?.len();
        }
    }
    Ok(total)
}
