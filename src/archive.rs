use flate2::{Compression, read::GzDecoder, write::GzEncoder};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    fs::{self, File, OpenOptions},
    io::{self, Read, Seek, SeekFrom, Write},
    path::{Component, Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};
use tar::{Archive, Builder, EntryType, Header};
use unicode_normalization::UnicodeNormalization;
use walkdir::WalkDir;

use crate::secrets::path_looks_like_secret;
use crate::{FileEntry, is_artifact};
use anyhow::Context;
use globset::{GlobBuilder, GlobSet, GlobSetBuilder};

static TEMP_DIR_COUNTER: AtomicU64 = AtomicU64::new(0);

pub(crate) type IncrementalArchiveResult =
    (String, u64, same_file::Handle, Vec<FileEntry>, Vec<String>);

pub(crate) fn portable_path_key(value: &str) -> String {
    value.to_lowercase().nfc().collect()
}

pub(crate) fn create_private_temp_dir(parent: &Path) -> io::Result<PathBuf> {
    loop {
        let path = parent.join(format!(
            ".projjan-tmp-{}-{}",
            std::process::id(),
            TEMP_DIR_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        let result = {
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;
                let mut builder = fs::DirBuilder::new();
                builder.mode(0o700).create(&path)
            }
            #[cfg(not(unix))]
            {
                fs::create_dir(&path)
            }
        };
        match result {
            Ok(()) => return Ok(path),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
}

pub(crate) fn write_new_file(path: &Path, bytes: &[u8]) -> io::Result<same_file::Handle> {
    let mut file = create_private_file(path)?;
    let identity = same_file::Handle::from_file(file.try_clone()?).map_err(io::Error::other)?;
    let result = file.write_all(bytes).and_then(|()| {
        let current = same_file::Handle::from_path(path).map_err(io::Error::other)?;
        if current != identity {
            return Err(io::Error::other(format!(
                "output path changed while it was being written: {}",
                path.display()
            )));
        }
        Ok(())
    });
    drop(file);
    if let Err(error) = result {
        remove_file_if_same(path, identity);
        return Err(error);
    }
    Ok(identity)
}

pub(crate) fn copy_new_file(
    source: &Path,
    destination: &Path,
) -> io::Result<(String, u64, same_file::Handle)> {
    let mut input = File::open(source)?;
    let mut output = create_private_file(destination)?;
    let identity = same_file::Handle::from_file(output.try_clone()?).map_err(io::Error::other)?;
    let result = (|| -> io::Result<(String, u64)> {
        io::copy(&mut input, &mut output)?;
        output.flush()?;
        let size = output.metadata()?.len();
        output.seek(SeekFrom::Start(0))?;
        let digest = sha256_reader(&mut output).map_err(io::Error::other)?;
        let current = same_file::Handle::from_path(destination).map_err(io::Error::other)?;
        if current != identity {
            return Err(io::Error::other(format!(
                "output path changed while it was being written: {}",
                destination.display()
            )));
        }
        Ok((digest, size))
    })();
    drop(output);
    match result {
        Ok((digest, size)) => Ok((digest, size, identity)),
        Err(error) => {
            remove_file_if_same(destination, identity);
            Err(error)
        }
    }
}

pub(crate) fn remove_file_if_same(path: &Path, identity: same_file::Handle) {
    let still_ours = archive_path_matches(path, &identity);
    drop(identity);
    if still_ours {
        let _ = fs::remove_file(path);
    }
}

#[allow(dead_code)]
pub(crate) fn create_archive(
    project: &Path,
    archive: &Path,
    include: bool,
    exclude_git_metadata: bool,
    exclude: &[String],
    exclude_secrets: bool,
) -> anyhow::Result<(String, u64, same_file::Handle)> {
    let f = create_private_file(archive)?;
    let identity = same_file::Handle::from_file(f.try_clone()?)?;
    let result = (|| -> anyhow::Result<(String, u64)> {
        let enc = GzEncoder::new(f, Compression::default());
        let mut tar = Builder::new(enc);
        let root = project.file_name().unwrap_or_default();
        let mut archive_paths = HashMap::new();
        register_archive_case_path(Path::new(root), &mut archive_paths)?;
        let exclude_set = build_exclude_globset(exclude)?;
        tar.append_dir(root, project)?;
        for e in WalkDir::new(project)
            .follow_links(false)
            .into_iter()
            .filter_entry(|entry| {
                !archive_excluded(
                    entry.path(),
                    project,
                    include,
                    exclude_git_metadata,
                    &exclude_set,
                    exclude_secrets,
                )
            })
        {
            let e = e?;
            let rel = e.path().strip_prefix(project)?;
            if rel.as_os_str().is_empty() {
                continue;
            }
            let name = Path::new(root).join(rel);
            anyhow::ensure!(
                safe_archive_path(&name, &root.to_string_lossy()),
                "unsafe archive path: {}",
                name.display()
            );
            register_archive_case_path(&name, &mut archive_paths)?;
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
        let mut file = tar.into_inner()?.finish()?;
        let size = file.metadata()?.len();
        file.seek(SeekFrom::Start(0))?;
        let digest = sha256_reader(&mut file)?;
        let current = same_file::Handle::from_path(archive)?;
        anyhow::ensure!(
            current == identity,
            "archive path changed while it was being written: {}",
            archive.display()
        );
        Ok((digest, size))
    })();
    match result {
        Ok((digest, size)) => Ok((digest, size, identity)),
        Err(error) => {
            remove_file_if_same(archive, identity);
            Err(error)
        }
    }
}

/// Count files/symlinks and total byte size that archive packing would see
/// after applying the same exclusion rules as backup.
pub(crate) fn estimate_archive_stats(
    project: &Path,
    include: bool,
    exclude_git_metadata: bool,
    exclude: &[String],
    exclude_secrets: bool,
) -> anyhow::Result<(u64, u64)> {
    let exclude_set = build_exclude_globset(exclude)?;
    let mut files = 0u64;
    let mut bytes = 0u64;
    for e in WalkDir::new(project)
        .follow_links(false)
        .into_iter()
        .filter_entry(|entry| {
            !archive_excluded(
                entry.path(),
                project,
                include,
                exclude_git_metadata,
                &exclude_set,
                exclude_secrets,
            )
        })
    {
        let e = e.with_context(|| format!("cannot walk {}", project.display()))?;
        if e.path() == project {
            continue;
        }
        if e.file_type().is_symlink() {
            files += 1;
        } else if e.file_type().is_file() {
            files += 1;
            bytes += e.metadata().map(|m| m.len()).unwrap_or(0);
        }
    }
    Ok((files, bytes))
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn create_incremental_archive(
    project: &Path,
    archive: &Path,
    include: bool,
    exclude_git_metadata: bool,
    exclude: &[String],
    previous: Option<&HashMap<String, String>>,
    delta: bool,
    exclude_secrets: bool,
    progress_label: Option<&str>,
) -> anyhow::Result<IncrementalArchiveResult> {
    let f = create_private_file(archive)?;
    let identity = same_file::Handle::from_file(f.try_clone()?)?;
    let result = (|| -> anyhow::Result<(String, u64, Vec<FileEntry>, Vec<String>)> {
        let enc = GzEncoder::new(f, Compression::default());
        let mut tar = Builder::new(enc);
        let root = project.file_name().unwrap_or_default();
        let mut archive_paths = HashMap::new();
        register_archive_case_path(Path::new(root), &mut archive_paths)?;
        let exclude_set = build_exclude_globset(exclude)?;
        tar.append_dir(root, project)?;
        let mut inventory: Vec<FileEntry> = Vec::new();
        let mut seen_files = 0u64;
        let mut seen_bytes = 0u64;
        for e in WalkDir::new(project)
            .follow_links(false)
            .into_iter()
            .filter_entry(|entry| {
                !archive_excluded(
                    entry.path(),
                    project,
                    include,
                    exclude_git_metadata,
                    &exclude_set,
                    exclude_secrets,
                )
            })
        {
            let e = e?;
            let rel = e.path().strip_prefix(project)?;
            if rel.as_os_str().is_empty() {
                continue;
            }
            let name = Path::new(root).join(rel);
            anyhow::ensure!(
                safe_archive_path(&name, &root.to_string_lossy()),
                "unsafe archive path: {}",
                name.display()
            );
            register_archive_case_path(&name, &mut archive_paths)?;
            let rel_str = rel
                .components()
                .map(|c| c.as_os_str().to_string_lossy())
                .collect::<Vec<_>>()
                .join("/");
            if e.file_type().is_symlink() {
                let target = fs::read_link(e.path())?;
                anyhow::ensure!(
                    safe_archive_link(&name, &target, &root.to_string_lossy()),
                    "archive symlink escapes project: {} -> {}",
                    name.display(),
                    target.display()
                );
                let digest = format!("{:x}", Sha256::digest(target.to_string_lossy().as_bytes()));
                if !delta || !previous.is_some_and(|prev| prev.get(&rel_str) == Some(&digest)) {
                    let mut h = Header::new_gnu();
                    h.set_entry_type(EntryType::Symlink);
                    h.set_size(0);
                    h.set_mode(0o777);
                    h.set_link_name(&target)?;
                    h.set_cksum();
                    tar.append_link(&mut h, name, target)?;
                }
                inventory.push(FileEntry {
                    path: rel_str,
                    sha256: digest,
                    size: 0,
                });
                seen_files += 1;
                report_archive_progress(progress_label, seen_files, seen_bytes);
            } else if e.file_type().is_dir() {
                tar.append_dir(name, e.path())?;
            } else if e.file_type().is_file() {
                let meta = fs::metadata(e.path())?;
                let mut file = File::open(e.path())?;
                let mut hasher = Sha256::new();
                io::copy(&mut file, &mut hasher)?;
                let digest = format!("{:x}", hasher.finalize());
                if !delta || !previous.is_some_and(|prev| prev.get(&rel_str) == Some(&digest)) {
                    tar.append_path_with_name(e.path(), name)?;
                }
                inventory.push(FileEntry {
                    path: rel_str,
                    sha256: digest,
                    size: meta.len(),
                });
                seen_files += 1;
                seen_bytes += meta.len();
                report_archive_progress(progress_label, seen_files, seen_bytes);
            }
        }
        if let Some(label) = progress_label {
            eprintln!(
                "progress: {label}: done, {seen_files} files, {} scanned",
                crate::human(seen_bytes)
            );
        }
        let mut deleted: Vec<String> = Vec::new();
        if delta && let Some(prev) = previous {
            let current: HashSet<&String> = inventory.iter().map(|entry| &entry.path).collect();
            for path in prev.keys() {
                if !current.contains(path) {
                    deleted.push(path.clone());
                }
            }
            deleted.sort();
        }
        let mut file = tar.into_inner()?.finish()?;
        let size = file.metadata()?.len();
        file.seek(SeekFrom::Start(0))?;
        let digest = sha256_reader(&mut file)?;
        let current = same_file::Handle::from_path(archive)?;
        anyhow::ensure!(
            current == identity,
            "archive path changed while it was being written: {}",
            archive.display()
        );
        Ok((digest, size, inventory, deleted))
    })();
    match result {
        Ok((digest, size, inventory, deleted)) => Ok((digest, size, identity, inventory, deleted)),
        Err(error) => {
            remove_file_if_same(archive, identity);
            Err(error)
        }
    }
}

pub(crate) fn archive_path_matches(path: &Path, identity: &same_file::Handle) -> bool {
    same_file::Handle::from_path(path).is_ok_and(|current| current == *identity)
}

fn create_private_file(path: &Path) -> io::Result<File> {
    let mut options = OpenOptions::new();
    options.read(true).write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path)
}

pub(crate) fn create_private_dir(path: &Path) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    #[cfg(unix)]
    let result = {
        use std::os::unix::fs::DirBuilderExt;
        let mut builder = fs::DirBuilder::new();
        builder.mode(0o700).create(path)
    };
    #[cfg(not(unix))]
    let result = fs::create_dir(path);
    match result {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists && path.is_dir() => Ok(()),
        Err(error) => Err(error),
    }
}

pub(crate) fn extract_archive(archive: &Path, target: &Path, project: &str) -> anyhow::Result<()> {
    extract_archive_with_hash(archive, target, project, None)
}

pub(crate) fn extract_archive_with_hash(
    archive: &Path,
    target: &Path,
    project: &str,
    expected_hash: Option<&str>,
) -> anyhow::Result<()> {
    anyhow::ensure!(
        !target.exists() && !target.is_symlink(),
        "restore target already exists: {}",
        target.display()
    );
    let mut f = File::open(archive)?;
    if let Some(expected) = expected_hash {
        let actual = sha256_reader(&mut f)?;
        anyhow::ensure!(
            actual.eq_ignore_ascii_case(expected),
            "sha256 mismatch for {}",
            archive.display()
        );
        f.seek(SeekFrom::Start(0))?;
    }
    let parent = target
        .parent()
        .ok_or_else(|| anyhow::anyhow!("target has no parent"))?;
    fs::create_dir_all(parent)?;
    let tmp = create_private_temp_dir(parent)?;
    let result = (|| -> anyhow::Result<()> {
        let dec = GzDecoder::new(f);
        let mut ar = Archive::new(dec);
        let entries = ar.entries()?;
        let mut member_count = 0;
        let mut directories = Vec::new();
        let mut symlinks = Vec::new();
        let mut casefolded_paths = HashMap::new();
        let mut archive_members = HashSet::new();
        for item in entries {
            let mut e = item?;
            member_count += 1;
            let raw_name = e.path_bytes();
            anyhow::ensure!(
                !raw_name.contains(&b'\\'),
                "archive member path contains a backslash: {}",
                String::from_utf8_lossy(&raw_name)
            );
            let name = e.path()?.into_owned();
            anyhow::ensure!(
                safe_archive_path(&name, project),
                "unsafe archive path: {}",
                name.display()
            );
            let member_path: Vec<_> = name
                .components()
                .map(|component| component.as_os_str().to_string_lossy().into_owned())
                .collect();
            anyhow::ensure!(
                archive_members.insert(member_path),
                "archive contains a duplicate path: {}",
                name.display()
            );
            register_archive_case_path(&name, &mut casefolded_paths)?;
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
            }
        }
        anyhow::ensure!(member_count > 0, "archive is empty");
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

fn remove_path(path: &Path) -> anyhow::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if meta.is_dir() && !meta.file_type().is_symlink() {
        fs::remove_dir_all(path)?;
    } else {
        fs::remove_file(path)?;
    }
    Ok(())
}

pub(crate) fn remove_deleted_paths(target: &Path, deleted: &[String]) -> anyhow::Result<()> {
    for path in deleted {
        anyhow::ensure!(
            !path.is_empty() && !path.contains('\\') && !path.starts_with('/'),
            "unsafe deleted path: {path}"
        );
        let rel = Path::new(path);
        anyhow::ensure!(
            rel.components().all(|c| matches!(c, Component::Normal(_))),
            "unsafe deleted path: {path}"
        );
        let dest = target.join(rel);
        if fs::symlink_metadata(&dest).is_ok() {
            remove_path(&dest)?;
        }
    }
    Ok(())
}

pub(crate) fn extract_incremental_delta(
    archive: &Path,
    target: &Path,
    project: &str,
    expected_hash: Option<&str>,
) -> anyhow::Result<()> {
    anyhow::ensure!(
        target.is_dir() && !target.is_symlink(),
        "incremental restore target is not a directory: {}",
        target.display()
    );
    let mut f = File::open(archive)?;
    if let Some(expected) = expected_hash {
        let actual = sha256_reader(&mut f)?;
        anyhow::ensure!(
            actual.eq_ignore_ascii_case(expected),
            "sha256 mismatch for {}",
            archive.display()
        );
        f.seek(SeekFrom::Start(0))?;
    }
    let dec = GzDecoder::new(f);
    let mut ar = Archive::new(dec);
    let entries = ar.entries()?;
    let mut member_count = 0;
    let mut casefolded_paths = HashMap::new();
    let mut seen: HashSet<Vec<String>> = HashSet::new();
    for item in entries {
        let mut e = item?;
        member_count += 1;
        let raw_name = e.path_bytes();
        anyhow::ensure!(
            !raw_name.contains(&b'\\'),
            "archive member path contains a backslash: {}",
            String::from_utf8_lossy(&raw_name)
        );
        let name = e.path()?.into_owned();
        anyhow::ensure!(
            safe_archive_path(&name, project),
            "unsafe archive path: {}",
            name.display()
        );
        let rel = name.strip_prefix(project)?;
        if rel.as_os_str().is_empty() {
            continue;
        }
        let dest = target.join(rel);
        let member_path: Vec<_> = rel
            .components()
            .map(|c| c.as_os_str().to_string_lossy().into_owned())
            .collect();
        anyhow::ensure!(
            seen.insert(member_path),
            "delta archive contains a duplicate path: {}",
            name.display()
        );
        register_archive_case_path(&name, &mut casefolded_paths)?;
        let ty = e.header().entry_type();
        let mode = e.header().mode()? & 0o777;
        if ty.is_dir() {
            if let Ok(meta) = fs::symlink_metadata(&dest)
                && (!meta.is_dir() || meta.file_type().is_symlink())
            {
                remove_path(&dest)?;
            }
            fs::create_dir_all(&dest)?;
            set_mode(&dest, mode)?;
        } else if ty.is_symlink() {
            let link = e
                .link_name()?
                .ok_or_else(|| anyhow::anyhow!("symlink has no target"))?;
            anyhow::ensure!(
                safe_archive_link(&name, &link, project),
                "archive symlink escapes project: {}",
                name.display()
            );
            if let Some(parent) = dest.parent() {
                fs::create_dir_all(parent)?;
            }
            if fs::symlink_metadata(&dest).is_ok() {
                remove_path(&dest)?;
            }
            let target_is_dir = dest
                .parent()
                .is_some_and(|parent| parent.join(&link).is_dir());
            create_symlink(&link, &dest, target_is_dir)?;
        } else {
            anyhow::ensure!(
                ty.is_file(),
                "archive contains special file: {}",
                name.display()
            );
            if let Some(parent) = dest.parent() {
                fs::create_dir_all(parent)?;
            }
            if fs::symlink_metadata(&dest).is_ok() {
                remove_path(&dest)?;
            }
            let mut out = File::create(&dest)?;
            io::copy(&mut e, &mut out)?;
            set_mode(&dest, mode)?;
        }
    }
    anyhow::ensure!(member_count > 0, "delta archive is empty");
    Ok(())
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
        && p.components().all(|component| {
            matches!(component, Component::Normal(name) if name.to_str().is_some_and(safe_windows_component))
        })
        && p.components()
            .next()
            .is_some_and(|component| component.as_os_str() == project)
}

fn register_archive_case_path(
    name: &Path,
    seen: &mut HashMap<Vec<String>, Vec<String>>,
) -> anyhow::Result<()> {
    let parts: Vec<_> = name
        .components()
        .map(|component| component.as_os_str().to_string_lossy().into_owned())
        .collect();
    for length in 1..=parts.len() {
        let original = parts[..length].to_vec();
        let folded: Vec<_> = original
            .iter()
            .map(|part| portable_path_key(part))
            .collect();
        if let Some(previous) = seen.get(&folded) {
            anyhow::ensure!(
                previous == &original,
                "archive paths collide on case-insensitive filesystems (including Unicode normalization): {}",
                name.display()
            );
        } else {
            seen.insert(folded, original);
        }
    }
    Ok(())
}

pub(crate) fn safe_archive_link(name: &Path, link: &Path, project: &str) -> bool {
    let Some(target) = link.to_str() else {
        return false;
    };
    if link.is_absolute()
        || target.contains(['\0', ':'])
        || target.starts_with(['/', '\\'])
        || (target.as_bytes().get(1) == Some(&b':') && target.as_bytes()[0].is_ascii_alphabetic())
    {
        return false;
    }
    if target
        .split(['/', '\\'])
        .any(|part| !matches!(part, "" | "." | "..") && !safe_windows_component(part))
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

pub(crate) fn safe_windows_component(name: &str) -> bool {
    if name.is_empty()
        || name.ends_with([' ', '.'])
        || name
            .chars()
            .any(|character| character.is_ascii_control() || "<>:\"|?*\\".contains(character))
    {
        return false;
    }
    let stem = name
        .split('.')
        .next()
        .unwrap_or("")
        .trim_end_matches([' ', '.'])
        .to_uppercase();
    let reserved = matches!(
        stem.as_str(),
        "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$"
    ) || ["COM", "LPT"].iter().any(|prefix| {
        stem.strip_prefix(prefix).is_some_and(|number| {
            matches!(
                number,
                "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
            )
        })
    });
    !reserved
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
pub(crate) fn build_exclude_globset(exclude: &[String]) -> anyhow::Result<GlobSet> {
    let mut builder = GlobSetBuilder::new();
    for raw in exclude {
        let pattern = raw.trim().trim_start_matches("./").trim_end_matches('/');
        if pattern.is_empty() {
            continue;
        }
        let glob = GlobBuilder::new(pattern)
            .literal_separator(true)
            .build()
            .with_context(|| format!("invalid exclude pattern: {raw}"))?;
        builder.add(glob);
    }
    builder.build().context("invalid exclude patterns")
}
fn report_archive_progress(label: Option<&str>, files: u64, bytes: u64) {
    let Some(name) = label else {
        return;
    };
    if files == 1 || files.is_multiple_of(100) {
        eprintln!(
            "progress: {name}: {files} files, {} scanned",
            crate::human(bytes)
        );
    }
}

pub(crate) fn archive_excluded(
    path: &Path,
    project: &Path,
    include: bool,
    exclude_git_metadata: bool,
    exclude: &GlobSet,
    exclude_secrets: bool,
) -> bool {
    if path == project {
        return false;
    }
    let Ok(relative) = path.strip_prefix(project) else {
        return true;
    };
    if exclude_git_metadata
        && relative
            .components()
            .any(|component| component.as_os_str() == ".git")
    {
        return true;
    }
    if relative.components().any(|component| {
        let name = component.as_os_str().to_string_lossy();
        exclude.is_match(name.as_ref())
    }) {
        return true;
    }
    if exclude_secrets {
        let rel = relative
            .components()
            .map(|component| component.as_os_str().to_string_lossy())
            .collect::<Vec<_>>()
            .join("/");
        if path_looks_like_secret(&rel) {
            return true;
        }
    }
    if include {
        return false;
    }
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
    sha256_reader(&mut f)
}

fn sha256_reader(reader: &mut impl Read) -> anyhow::Result<String> {
    let mut h = Sha256::new();
    let mut b = [0; 1024 * 1024];
    loop {
        let n = reader.read(&mut b)?;
        if n == 0 {
            break;
        }
        h.update(&b[..n]);
    }
    Ok(format!("{:x}", h.finalize()))
}
