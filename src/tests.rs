use super::*;
use crate::archive::{
    create_archive, extract_archive, safe_archive_link, safe_archive_path, sha256, write_new_file,
};
use crate::backup::backup_cmd;
use crate::clean::tree_size;
use crate::project::{github_url, inspect, inspect_recovery, verify_local_tags};
use crate::restore::{redact_remote, validate_git_restore};
use flate2::{Compression, write::GzEncoder};
use std::sync::atomic::{AtomicU64, Ordering};
use std::{fs::File, process::Command};
use tar::{Builder, EntryType, Header};

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

fn raw_hardlink_archive(temp: &TestDir) -> PathBuf {
    let archive_path = temp.0.join("hardlink.tar.gz");
    let encoder = GzEncoder::new(File::create(&archive_path).unwrap(), Compression::default());
    let mut tar = Builder::new(encoder);
    let mut file = Header::new_gnu();
    file.set_entry_type(EntryType::Regular);
    file.set_mode(0o644);
    file.set_size(4);
    file.set_path("demo/file.txt").unwrap();
    file.set_cksum();
    tar.append(&file, &b"data"[..]).unwrap();

    let mut hardlink = Header::new_gnu();
    hardlink.set_entry_type(EntryType::Link);
    hardlink.set_mode(0o644);
    hardlink.set_size(0);
    hardlink.set_link_name("demo/file.txt").unwrap();
    hardlink.set_cksum();
    tar.append_link(&mut hardlink, "demo/hardlink.txt", "demo/file.txt")
        .unwrap();
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
    assert!(validate_git_restore("https://github.com/team/project.git", &"a".repeat(40)).is_ok());
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
        Path::new("../pyproject.toml"),
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
            eprintln!("Windows symlink privilege unavailable; skipping link round-trip: {error}");
            return;
        }
        if let Err(error) = symlink_dir("subdir", project.join("src/current-dir")) {
            eprintln!("Windows symlink privilege unavailable; skipping link round-trip: {error}");
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

#[cfg(unix)]
#[test]
fn backup_rejects_symlink_targets_outside_project() {
    use std::os::unix::fs::symlink;

    let temp = TestDir::new();
    let project = temp.0.join("demo");
    fs::create_dir_all(project.join("src")).unwrap();
    fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
    let outside = temp.0.join("outside.txt");
    fs::write(&outside, "outside\n").unwrap();
    symlink(&outside, project.join("src/outside-link")).unwrap();

    let archive = temp.0.join("demo.tar.gz");
    let error = create_archive(&project, &archive, false).unwrap_err();

    assert!(
        error
            .to_string()
            .contains("archive symlink escapes project")
    );
}

#[test]
fn backup_output_creation_never_overwrites_existing_files() {
    let temp = TestDir::new();
    let project = temp.0.join("demo");
    fs::create_dir_all(&project).unwrap();
    fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
    let archive = temp.0.join("demo.tar.gz");
    fs::write(&archive, "keep existing archive").unwrap();
    assert!(create_archive(&project, &archive, false).is_err());
    assert_eq!(
        fs::read_to_string(&archive).unwrap(),
        "keep existing archive"
    );

    let manifest = temp.0.join("manifest.json");
    fs::write(&manifest, "keep existing manifest").unwrap();
    assert!(write_new_file(&manifest, b"replacement").is_err());
    assert_eq!(
        fs::read_to_string(manifest).unwrap(),
        "keep existing manifest"
    );
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
    let manifest: Manifest = serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
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
fn backup_rejects_destination_inside_source_project_before_writing() {
    let temp = TestDir::new();
    let project = temp.0.join("demo");
    fs::create_dir_all(&project).unwrap();
    fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
    let backup_dest = project.join("backups");

    let error = backup_cmd(
        std::slice::from_ref(&project),
        Some(backup_dest.clone()),
        false,
        true,
        false,
    )
    .unwrap_err();

    assert!(
        error
            .to_string()
            .contains("backup destination is inside project")
    );
    assert!(!backup_dest.exists());
}

#[test]
fn backup_disambiguates_project_names_that_differ_only_by_case() {
    let temp = TestDir::new();
    let first = temp.0.join("one/App");
    let second = temp.0.join("two/app");
    fs::create_dir_all(&first).unwrap();
    fs::create_dir_all(&second).unwrap();
    fs::write(first.join("pyproject.toml"), "[project]\n").unwrap();
    fs::write(second.join("pyproject.toml"), "[project]\n").unwrap();
    let backup_dest = temp.0.join("backups");

    backup_cmd(
        &[first, second],
        Some(backup_dest.clone()),
        false,
        true,
        false,
    )
    .unwrap();

    let names: Vec<_> = fs::read_dir(backup_dest)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(names.len(), 2);
    assert!(names.iter().any(|name| name.starts_with("App-")));
    assert!(names.iter().any(|name| name.starts_with("app-")));
    assert!(names.iter().all(|name| {
        name.rsplit_once('-')
            .is_some_and(|(_, hash)| hash.len() == 8 && hash.bytes().all(|b| b.is_ascii_hexdigit()))
    }));
}

#[test]
fn backup_continues_after_one_project_directory_creation_fails() {
    let temp = TestDir::new();
    let first = temp.0.join("first");
    let second = temp.0.join("second");
    fs::create_dir_all(&first).unwrap();
    fs::create_dir_all(&second).unwrap();
    fs::write(first.join("pyproject.toml"), "[project]\n").unwrap();
    fs::write(second.join("pyproject.toml"), "[project]\n").unwrap();
    let backup_dest = temp.0.join("backups");
    fs::create_dir_all(&backup_dest).unwrap();
    fs::write(backup_dest.join("first"), "blocks directory\n").unwrap();

    assert!(
        backup_cmd(
            &[first, second],
            Some(backup_dest.clone()),
            false,
            true,
            false,
        )
        .is_err()
    );
    assert_eq!(
        fs::read_dir(backup_dest.join("second"))
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "json"))
            .count(),
        1
    );
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
        source_bytes: tree_size(&source).unwrap(),
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

    let hardlink = raw_hardlink_archive(&temp);
    let hardlink_target = temp.0.join("restored-hardlink/demo");
    let error = extract_archive(&hardlink, &hardlink_target, "demo").unwrap_err();
    assert!(
        error.to_string().contains("unsupported hard link"),
        "unexpected hard-link error: {error}"
    );
    assert!(!hardlink_target.exists());
}

#[test]
fn restore_dest_rejects_project_names_that_differ_only_by_case() {
    let temp = TestDir::new();
    let manifest_path = temp.0.join("manifest.json");
    let record = |project: &str| Record {
        project: project.into(),
        source: format!("/original/{project}"),
        archive: None,
        archive_bytes: 0,
        source_bytes: 0,
        sha256: None,
        strategy: "git".into(),
        remote: None,
        remote_url: Some("https://github.com/team/demo.git".into()),
        branch: None,
        commit: Some("a".repeat(40)),
        upstream: None,
        verified: None,
        fallback_reason: None,
    };
    let manifest = Manifest {
        schema_version: 2,
        created_at: "2026-10-26T00:00:00+00:00".into(),
        tool: "proj-janitor".into(),
        version: "0.2.2".into(),
        include_artifacts: false,
        always_archive: false,
        records: vec![record("App"), record("app")],
    };
    fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    let dest = temp.0.join("restored");

    for destination in [Some(dest.clone()), None] {
        let error =
            restore_cmd(&manifest_path, destination, &[], true, false, true, false).unwrap_err();
        assert!(error.to_string().contains("case-insensitive filesystems"));
    }
    assert!(!dest.exists());
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
fn tree_size_reports_traversal_errors() {
    let temp = TestDir::new();
    assert!(tree_size(&temp.0.join("missing")).is_err());
}

#[test]
fn clean_does_not_apply_partial_plan_when_a_root_fails() {
    let temp = TestDir::new();
    let project = temp.0.join("demo");
    fs::create_dir_all(project.join("target")).unwrap();
    fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
    fs::write(project.join("target/cache"), "remove me\n").unwrap();

    assert!(
        clean_cmd(
            &[project.clone(), temp.0.join("missing-root")],
            &[],
            true,
            true,
            true,
            false,
        )
        .is_err()
    );
    assert!(project.join("target/cache").is_file());
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
