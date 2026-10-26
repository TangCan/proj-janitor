use flate2::{Compression, read::GzDecoder, write::GzEncoder};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    path::{Component, Path},
};
use tar::{Archive, Builder, EntryType, Header};
use walkdir::WalkDir;

use crate::is_artifact;

pub(crate) fn write_new_file(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    if let Err(error) = file.write_all(bytes) {
        drop(file);
        let _ = fs::remove_file(path);
        return Err(error);
    }
    Ok(())
}

pub(crate) fn create_archive(project: &Path, archive: &Path, include: bool) -> anyhow::Result<()> {
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
pub(crate) fn extract_archive(archive: &Path, target: &Path, project: &str) -> anyhow::Result<()> {
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
            } else if ty.is_hard_link() {
                anyhow::bail!("archive contains unsupported hard link: {}", name.display());
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

pub(crate) fn safe_archive_path(p: &Path, project: &str) -> bool {
    !p.is_absolute()
        && p.components().all(|c| matches!(c, Component::Normal(_)))
        && p.components()
            .next()
            .is_some_and(|c| c.as_os_str() == project)
}

pub(crate) fn safe_archive_link(name: &Path, link: &Path, project: &str) -> bool {
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
pub(crate) fn sha256(path: &Path) -> anyhow::Result<String> {
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
