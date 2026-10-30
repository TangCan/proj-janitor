use globset::{Glob, GlobSetBuilder};
use std::{
    collections::HashSet,
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
    for root in roots {
        match discover(root, false) {
            Ok(ps) => {
                for p in ps {
                    if !seen.insert(p.path.clone()) {
                        continue;
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
        let target = cleanup_target(&project, &path)?;
        if target.is_dir() {
            fs::remove_dir_all(target)?
        } else {
            fs::remove_file(target)?
        }
    }
    if failed {
        anyhow::bail!("scan failures")
    }
    Ok(())
}

pub(crate) fn cleanup_target(project: &Path, path: &Path) -> anyhow::Result<PathBuf> {
    // `project` is already canonical from discovery; keep that planned root fixed.
    // shortcut: path checks race concurrent renames; use dirfd deletion if hostile writers are in scope.
    let target = fs::canonicalize(path)?;
    anyhow::ensure!(
        target != project && target.starts_with(project),
        "target escapes project root: {}",
        path.display()
    );
    Ok(target)
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
