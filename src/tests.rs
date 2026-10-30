use super::*;
use crate::archive::{
    create_archive, extract_archive, safe_archive_link, safe_archive_path, sha256, write_new_file,
};
use crate::backup::backup_cmd;
use crate::clean::tree_size;
use crate::project::{github_url, inspect, inspect_recovery, verify_local_tags};
use crate::restore::{redact_remote, restore_cmd, validate_git_restore};
use flate2::{Compression, write::GzEncoder};
use std::sync::atomic::{AtomicU64, Ordering};
use std::{fs::File, process::Command};
use tar::{Builder, EntryType, Header};

static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);

#[test]
fn artifact_matching_uses_cleanup_rules() {
    assert!(is_artifact("node_modules"));
    assert!(is_artifact("bundle.pyc"));
    assert!(is_artifact(".venv"));
    assert!(is_artifact("dist-standalone"));
    assert!(!is_artifact("src"));
    assert!(!is_artifact(".git"));
    assert_eq!(is_artifact("NODE_MODULES"), cfg!(windows));
}

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

fn raw_case_collision_archive(temp: &TestDir) -> PathBuf {
    let archive_path = temp.0.join("case-collision.tar.gz");
    let encoder = GzEncoder::new(File::create(&archive_path).unwrap(), Compression::default());
    let mut tar = Builder::new(encoder);
    for name in ["demo/Dir/one.txt", "demo/dir/two.txt"] {
        let mut file = Header::new_gnu();
        file.set_entry_type(EntryType::Regular);
        file.set_mode(0o644);
        file.set_size(1);
        file.set_path(name).unwrap();
        file.set_cksum();
        tar.append(&file, &b"x"[..]).unwrap();
    }
    tar.into_inner().unwrap().finish().unwrap();
    archive_path
}

fn raw_windows_traversal_archive(temp: &TestDir) -> PathBuf {
    raw_test_archive(
        temp,
        "windows-traversal.tar.gz",
        r"demo/src\..\..\escape.txt",
        None,
    )
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
    assert!(!safe_archive_path(
        Path::new(r"project/src\..\..\outside"),
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
    create_archive(&project, &archive, false, false).unwrap();
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
    let error = create_archive(&project, &archive, false, false).unwrap_err();

    assert!(
        error
            .to_string()
            .contains("archive symlink escapes project")
    );
}

#[cfg(unix)]
#[test]
fn backup_rejects_windows_traversal_member_names() {
    let temp = TestDir::new();
    let project = temp.0.join("demo");
    fs::create_dir_all(&project).unwrap();
    fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
    fs::write(project.join(r"src\..\..\escape.txt"), "bad\n").unwrap();
    let archive = temp.0.join("demo.tar.gz");

    let error = create_archive(&project, &archive, false, false).unwrap_err();

    assert!(error.to_string().contains("unsafe archive path"));
    assert!(!archive.exists());
}

#[test]
fn backup_output_creation_never_overwrites_existing_files() {
    let temp = TestDir::new();
    let project = temp.0.join("demo");
    fs::create_dir_all(&project).unwrap();
    fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
    let archive = temp.0.join("demo.tar.gz");
    fs::write(&archive, "keep existing archive").unwrap();
    assert!(create_archive(&project, &archive, false, false).is_err());
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

#[cfg(unix)]
#[test]
fn backup_outputs_are_private_to_the_current_user() {
    use std::os::unix::fs::PermissionsExt;

    let temp = TestDir::new();
    let project = temp.0.join("demo");
    fs::create_dir_all(&project).unwrap();
    fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
    let backup_dir = temp.0.join("backups");

    backup_cmd(
        std::slice::from_ref(&project),
        Some(backup_dir.clone()),
        false,
        false,
        true,
        false,
    )
    .unwrap();

    for path in [&backup_dir, &backup_dir.join("demo")] {
        let mode = fs::metadata(path).unwrap().permissions().mode();
        assert_eq!(mode & 0o077, 0);
        assert_eq!(mode & 0o700, 0o700);
    }
    for entry in fs::read_dir(backup_dir.join("demo")).unwrap() {
        let mode = entry.unwrap().metadata().unwrap().permissions().mode();
        assert_eq!(mode & 0o077, 0);
        assert_eq!(mode & 0o600, 0o600);
    }
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

    assert!(local_git(
        &project,
        &[
            "remote",
            "set-url",
            "origin",
            "https://github.com/team/demo.git"
        ]
    ));
    fs::write(project.join("tracked.txt"), "modified\n").unwrap();
    backup_cmd(
        &[project],
        Some(backup_dir.clone()),
        false,
        false,
        false,
        false,
    )
    .unwrap();
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
fn git_recovery_rejects_credentials_in_submodule_urls() {
    let temp = TestDir::new();
    let project = temp.0.join("demo");
    if !pushed_github_fixture(&project) {
        eprintln!("git unavailable; skipping Git submodule URL fixture");
        return;
    }
    assert!(local_git(
        &project,
        &[
            "remote",
            "set-url",
            "origin",
            "https://github.com/team/demo.git"
        ]
    ));
    fs::write(
        project.join(".gitmodules"),
        "[submodule \"private\"]\n\tpath = vendor/private\n\turl = https://alice:submodule-secret@github.com/team/private.git\n",
    )
    .unwrap();
    assert!(local_git(&project, &["add", ".gitmodules"]));
    assert!(local_git(
        &project,
        &["commit", "-m", "configure submodule"]
    ));
    assert!(local_git(
        &project,
        &["update-ref", "refs/remotes/origin/main", "HEAD"]
    ));

    let configured = crate::project::submodule_config(&project).unwrap();
    assert_eq!(
        configured[0].url,
        "https://alice:submodule-secret@github.com/team/private.git"
    );
    assert!(crate::project::url_has_sensitive_data(&configured[0].url));
    assert!(crate::project::url_has_sensitive_data(
        "https://github.com/team/private.git?token=secret"
    ));
    assert!(crate::project::url_has_sensitive_data(
        "ssh://git:secret@github.com/team/private.git"
    ));
    assert!(crate::project::url_has_sensitive_data(
        "http://token@github.com/team/private.git"
    ));
    assert!(crate::project::url_has_sensitive_data(
        "ssh://git@github.com:bad/team/private.git"
    ));
    assert!(!crate::project::url_has_sensitive_data(
        "ssh://git@github.com/team/private.git"
    ));
    let project_info = inspect(&project).unwrap();
    let reason = match inspect_recovery(&project_info) {
        Ok(_) => panic!("credentialed submodule URL was accepted"),
        Err(reason) => reason,
    };
    assert_eq!(reason, "submodule URL contains credentials or query data");
    assert!(!reason.contains("submodule-secret"));
}

#[test]
fn backup_rejects_sensitive_unsupported_remote_without_archiving() {
    let temp = TestDir::new();
    let project = temp.0.join("demo");
    if !pushed_github_fixture(&project) {
        eprintln!("git unavailable; skipping sensitive remote fixture");
        return;
    }
    assert!(local_git(
        &project,
        &[
            "remote",
            "set-url",
            "origin",
            "https://github.com/team/demo.git?token=remote-secret"
        ]
    ));
    let backup_dir = temp.0.join("backups");
    let error = crate::backup::backup_cmd(
        std::slice::from_ref(&project),
        Some(backup_dir.clone()),
        false,
        false,
        false,
        false,
    )
    .unwrap_err();
    assert!(!error.to_string().contains("remote-secret"));
    assert!(!backup_dir.join("demo").exists());
}

#[test]
fn backup_rejects_archiving_with_a_sensitive_secondary_remote_url() {
    let temp = TestDir::new();
    let project = temp.0.join("demo");
    if !pushed_github_fixture(&project) {
        eprintln!("git unavailable; skipping credentialed dirty remote fixture");
        return;
    }
    assert!(local_git(
        &project,
        &[
            "remote",
            "set-url",
            "origin",
            "https://github.com/team/demo.git"
        ]
    ));
    assert!(local_git(
        &project,
        &[
            "config",
            "--add",
            "remote.origin.url",
            "https://user:token@github.com/team/demo.git"
        ]
    ));
    fs::write(project.join("tracked.txt"), "dirty\n").unwrap();
    let backup_dir = temp.0.join("backups");
    let error = crate::backup::backup_cmd(
        std::slice::from_ref(&project),
        Some(backup_dir.clone()),
        false,
        false,
        false,
        false,
    )
    .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("one or more backup operations failed")
    );
    assert!(!error.to_string().contains("token"));
    assert!(!backup_dir.join("demo").exists());
}

#[test]
fn backup_rejects_archiving_with_a_credentialed_push_url() {
    let temp = TestDir::new();
    let project = temp.0.join("demo");
    if !pushed_github_fixture(&project) {
        eprintln!("git unavailable; skipping credentialed push URL fixture");
        return;
    }
    assert!(local_git(
        &project,
        &[
            "remote",
            "set-url",
            "origin",
            "https://github.com/team/demo.git"
        ]
    ));
    assert!(local_git(
        &project,
        &[
            "remote",
            "set-url",
            "--push",
            "origin",
            "https://token:push-secret@github.com/team/demo.git"
        ]
    ));
    fs::write(project.join("tracked.txt"), "dirty\n").unwrap();
    let backup_dir = temp.0.join("backups");
    let error = crate::backup::backup_cmd(
        std::slice::from_ref(&project),
        Some(backup_dir.clone()),
        false,
        false,
        false,
        false,
    )
    .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("one or more backup operations failed")
    );
    assert!(!error.to_string().contains("push-secret"));
    assert!(!backup_dir.join("demo").exists());
}

#[test]
fn backup_rejects_archiving_with_sensitive_git_auth_config() {
    let settings = [
        (
            "include.path",
            "credentials.inc",
            "Git config contains HTTP extraHeader",
            "extra-secret",
        ),
        (
            "credential.password",
            "config-secret",
            "Git config contains credential passwords",
            "config-secret",
        ),
        (
            "credential.helper",
            "!echo helper-secret",
            "Git config contains an inline credential helper",
            "helper-secret",
        ),
        (
            "credential.https://github.com.helper",
            "!echo url-helper-secret",
            "Git config contains an inline credential helper",
            "url-helper-secret",
        ),
        (
            "credential.helper",
            "manager --token=argument-secret",
            "Git config contains sensitive credential helper arguments",
            "argument-secret",
        ),
        (
            "credential.helper",
            "store --file=.git-credentials",
            "Git config points to a credential file inside the project",
            "store-secret",
        ),
        (
            "http.proxy",
            "http://proxy-user:proxy-secret@proxy.example:8080",
            "Git config contains credentials or query data in a service URL",
            "proxy-secret",
        ),
        (
            "remote.origin.proxy",
            "http://remote-proxy-user:remote-proxy-secret@proxy.example:8080",
            "Git config contains credentials or query data in a service URL",
            "remote-proxy-secret",
        ),
        (
            "lfs.url",
            "https://lfs-user:lfs-secret@lfs.example/objects",
            "Git config contains credentials or query data in a service URL",
            "lfs-secret",
        ),
        (
            "url.https://rewrite-secret@github.com/.insteadOf",
            "https://public.example/",
            "Git config contains credentials in a URL rewrite",
            "rewrite-secret",
        ),
        (
            "url.https://github.com/.insteadOf",
            "https://token:rewrite-value-secret@public.example/",
            "Git config contains credentials in a URL rewrite",
            "rewrite-value-secret",
        ),
        (
            ".lfsconfig",
            "https://lfs-user:file-secret@lfs.example/objects",
            "LFS config contains credentials or query data",
            "file-secret",
        ),
        (
            ".lfsconfig.proxy",
            "http://proxy-user:lfs-proxy-secret@proxy.example:8080",
            "LFS config contains credentials or query data",
            "lfs-proxy-secret",
        ),
    ];
    for (key, value, expected_reason, secret) in settings {
        let temp = TestDir::new();
        let project = temp.0.join("demo");
        if !pushed_github_fixture(&project) {
            eprintln!("git unavailable; skipping Git authentication config fixture");
            return;
        }
        assert!(local_git(
            &project,
            &[
                "remote",
                "set-url",
                "origin",
                "https://github.com/team/demo.git"
            ]
        ));
        if key == "include.path" {
            fs::write(
                project.join(".git/credentials.inc"),
                "[http]\n\textraHeader = Authorization: Basic extra-secret\n",
            )
            .unwrap();
        }
        if key == ".lfsconfig" {
            fs::write(project.join(key), format!("[lfs]\n\turl = {value}\n")).unwrap();
        } else if key == ".lfsconfig.proxy" {
            fs::write(
                project.join(".lfsconfig"),
                format!("[http]\n\tproxy = {value}\n"),
            )
            .unwrap();
        }
        if value.starts_with("store --file=") {
            fs::write(
                project.join(".git-credentials"),
                format!("https://user:{secret}@github.com\n"),
            )
            .unwrap();
        }
        if !key.starts_with(".lfsconfig") {
            assert!(local_git(&project, &["config", key, value]));
        }
        fs::write(project.join("tracked.txt"), "dirty\n").unwrap();
        let project_info = inspect(&project).unwrap();
        let reason = match crate::project::inspect_recovery(&project_info) {
            Ok(_) => panic!("sensitive Git authentication config was accepted"),
            Err(reason) => reason,
        };
        assert_eq!(reason, expected_reason);
        let backup_dir = temp.0.join("backups");
        let error = crate::backup::backup_cmd(
            std::slice::from_ref(&project),
            Some(backup_dir.clone()),
            false,
            false,
            false,
            false,
        )
        .unwrap_err();
        assert!(!error.to_string().contains(secret));
        assert!(!backup_dir.join("demo").exists());
    }
}

#[test]
fn backup_rejects_archiving_a_dirty_project_with_submodule_credentials() {
    let temp = TestDir::new();
    let project = temp.0.join("demo");
    if !pushed_github_fixture(&project) {
        eprintln!("git unavailable; skipping credentialed submodule fixture");
        return;
    }
    assert!(local_git(
        &project,
        &[
            "remote",
            "set-url",
            "origin",
            "https://github.com/team/demo.git"
        ]
    ));
    fs::write(
        project.join(".gitmodules"),
        "[submodule \"private\"]\n\tpath = vendor/private\n\turl = https://github.com/team/private.git\n\turl = https://token:module-secret@github.com/team/private.git\n",
    )
    .unwrap();
    let backup_dir = temp.0.join("backups");
    let error = crate::backup::backup_cmd(
        std::slice::from_ref(&project),
        Some(backup_dir.clone()),
        false,
        false,
        false,
        false,
    )
    .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("one or more backup operations failed")
    );
    assert!(!error.to_string().contains("module-secret"));
    assert!(!backup_dir.join("demo").exists());
}

#[test]
fn backup_rejects_non_git_projects_with_submodule_credentials() {
    let temp = TestDir::new();
    let project = temp.0.join("demo");
    fs::create_dir_all(&project).unwrap();
    fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
    fs::write(
        project.join(".gitmodules"),
        "[submodule \"private\"]\n\tpath = vendor/private\n\turl = https://github.com/team/private.git\n\turl = https://token:module-secret@github.com/team/private.git\n",
    )
    .unwrap();
    let backup_dir = temp.0.join("backups");
    let error = crate::backup::backup_cmd(
        std::slice::from_ref(&project),
        Some(backup_dir.clone()),
        false,
        false,
        false,
        false,
    )
    .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("one or more backup operations failed")
    );
    assert!(!error.to_string().contains("module-secret"));
    assert!(!backup_dir.join("demo").exists());
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

#[cfg(unix)]
#[test]
fn path_resolution_follows_symlinks_before_parent_components() {
    use std::os::unix::fs::symlink;

    let temp = TestDir::new();
    let real_directory = temp.0.join("project/nested");
    fs::create_dir_all(&real_directory).unwrap();
    symlink(&real_directory, temp.0.join("alias")).unwrap();
    let path = temp.0.join("alias/../backups/demo");
    let expected = fs::canonicalize(temp.0.join("project"))
        .unwrap()
        .join("backups/demo");

    assert_eq!(resolve_for_creation(&path).unwrap(), expected);
}

#[cfg(windows)]
#[test]
fn path_resolution_follows_windows_symlinks_before_parent_components() {
    use std::os::windows::fs::symlink_dir;

    let temp = TestDir::new();
    let real_directory = temp.0.join("project/nested");
    fs::create_dir_all(&real_directory).unwrap();
    let alias = temp.0.join("alias");
    // shortcut: skip this assertion when the runner lacks Windows symlink privilege.
    if let Err(error) = symlink_dir(&real_directory, &alias) {
        eprintln!("Windows symlink privilege unavailable; skipping path-resolution check: {error}");
        return;
    }
    let path = alias.join("../backups/demo");
    let expected = fs::canonicalize(temp.0.join("project"))
        .unwrap()
        .join("backups/demo");

    assert_eq!(resolve_for_creation(&path).unwrap(), expected);
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
    create_archive(&source, &archive, true, false).unwrap();
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
    let second_source = temp.0.join("second");
    fs::create_dir_all(&second_source).unwrap();
    fs::write(second_source.join("data.txt"), "second project\n").unwrap();
    let second_archive = backup_dir.join("second.tar.gz");
    create_archive(&second_source, &second_archive, true, false).unwrap();
    let mut second_record = record.clone();
    second_record.project = "second".into();
    second_record.source = second_source.to_string_lossy().into_owned();
    second_record.archive = Some(second_archive.to_string_lossy().into_owned());
    second_record.archive_bytes = fs::metadata(&second_archive).unwrap().len();
    second_record.source_bytes = tree_size(&second_source).unwrap();
    second_record.sha256 = Some(sha256(&second_archive).unwrap());
    let mut manifest = Manifest {
        schema_version: 2,
        created_at: "2026-10-24T00:00:00+00:00".into(),
        tool: "proj-janitor".into(),
        version: "0.1.0".into(),
        include_artifacts: false,
        always_archive: false,
        exclude_git_metadata: false,
        records: vec![record, second_record],
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

    fs::write(&second_archive, b"not a gzip archive").unwrap();
    let failed_restore_root = temp.0.join("failed-restore");
    let error = restore_cmd(
        &manifest_path,
        Some(failed_restore_root.clone()),
        &[],
        false,
        false,
        false,
        false,
    )
    .unwrap_err();
    assert!(error.to_string().contains("sha256 mismatch"));
    assert!(!failed_restore_root.exists());

    manifest.records[1].sha256 = Some(sha256(&second_archive).unwrap());
    fs::write(
        &manifest_path,
        serde_json::to_vec_pretty(&manifest).unwrap(),
    )
    .unwrap();
    let error = restore_cmd(
        &manifest_path,
        Some(failed_restore_root.clone()),
        &[],
        false,
        false,
        false,
        false,
    )
    .unwrap_err();
    assert!(error.to_string().contains("already restored: demo ->"));
    assert_eq!(
        fs::read_to_string(failed_restore_root.join("demo/data.txt")).unwrap(),
        "restore me\n"
    );
    assert!(!failed_restore_root.join("second").exists());
}

#[test]
fn restore_accepts_archive_containing_only_project_directory() {
    let temp = TestDir::new();
    let source = temp.0.join("demo");
    fs::create_dir_all(source.join(".git")).unwrap();
    fs::create_dir_all(source.join("target/cache")).unwrap();
    let archive = temp.0.join("demo.tar.gz");
    create_archive(&source, &archive, false, false).unwrap();

    let target = temp.0.join("restored/demo");
    extract_archive(&archive, &target, "demo").unwrap();

    assert!(target.is_dir());
    assert!(target.join(".git").is_dir());
    assert!(!target.join("target").exists());
}

#[test]
fn git_restore_fetches_commit_without_persisting_fetch_url() {
    let temp = TestDir::new();
    let project = temp.0.join("source");
    if !pushed_github_fixture(&project) {
        eprintln!("git unavailable; skipping Git restore fixture");
        return;
    }
    let remote = temp.0.join("remote.git");
    assert!(git_ok(&remote, &["init", "--bare"]));
    let remote_arg = remote.to_string_lossy().into_owned();
    assert!(local_git(&project, &["push", &remote_arg, "main"]));
    let head = Command::new("git")
        .arg("-C")
        .arg(&project)
        .args(["rev-parse", "HEAD"])
        .output()
        .unwrap();
    assert!(head.status.success());
    let commit = String::from_utf8(head.stdout).unwrap().trim().to_owned();
    let manifest = Manifest {
        schema_version: 2,
        created_at: "2026-10-27T00:00:00+00:00".into(),
        tool: "proj-janitor".into(),
        version: env!("CARGO_PKG_VERSION").into(),
        include_artifacts: false,
        always_archive: false,
        exclude_git_metadata: false,
        records: vec![Record {
            project: "demo".into(),
            source: project.to_string_lossy().into_owned(),
            archive: None,
            archive_bytes: 0,
            source_bytes: 0,
            sha256: None,
            strategy: "git".into(),
            remote: Some("origin".into()),
            remote_url: Some(remote_arg.clone()),
            branch: Some("main".into()),
            commit: Some(commit.clone()),
            upstream: Some("origin/main".into()),
            verified: Some("local-ref".into()),
            fallback_reason: None,
        }],
    };
    let manifest_path = temp.0.join("manifest.json");
    fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    let dest = temp.0.join("restored");

    restore_cmd(
        &manifest_path,
        Some(dest.clone()),
        &[],
        false,
        false,
        true,
        false,
    )
    .unwrap();

    let restored = dest.join("demo");
    assert_eq!(
        fs::read_to_string(restored.join("tracked.txt"))
            .unwrap()
            .replace("\r\n", "\n"),
        "clean\n"
    );
    assert!(!restored.join(".git/FETCH_HEAD").exists());
    let origin = Command::new("git")
        .arg("-C")
        .arg(&restored)
        .args(["config", "--get", "remote.origin.url"])
        .output()
        .unwrap();
    assert!(origin.status.success());
    assert_eq!(String::from_utf8(origin.stdout).unwrap().trim(), remote_arg);
    let actual_head = Command::new("git")
        .arg("-C")
        .arg(&restored)
        .args(["rev-parse", "HEAD"])
        .output()
        .unwrap();
    assert!(actual_head.status.success());
    assert_eq!(
        String::from_utf8(actual_head.stdout).unwrap().trim(),
        commit
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

    let case_collision = raw_case_collision_archive(&temp);
    let case_target = temp.0.join("restored-case/demo");
    let error = extract_archive(&case_collision, &case_target, "demo").unwrap_err();
    assert!(error.to_string().contains("case-insensitive filesystems"));
    assert!(!case_target.exists());

    let windows_traversal = raw_windows_traversal_archive(&temp);
    let windows_target = temp.0.join("restored-windows-traversal/demo");
    assert!(extract_archive(&windows_traversal, &windows_target, "demo").is_err());
    assert!(!windows_target.exists());

    let backslash_member =
        raw_test_archive(&temp, "backslash-member.tar.gz", r"demo/src\file.txt", None);
    let backslash_target = temp.0.join("restored-backslash/demo");
    assert!(extract_archive(&backslash_member, &backslash_target, "demo").is_err());
    assert!(!backslash_target.exists());
}

#[test]
fn restore_dest_rejects_project_names_that_differ_only_by_case() {
    let temp = TestDir::new();
    let manifest_path = temp.0.join("manifest.json");
    let original = temp.0.join("original");
    let record = |project: &str| Record {
        project: project.into(),
        source: original.join(project).to_string_lossy().into_owned(),
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
        exclude_git_metadata: false,
        records: vec![record("App"), record("app")],
    };
    fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    let dest = temp.0.join("restored");

    for destination in [Some(dest.clone()), None] {
        let error =
            restore_cmd(&manifest_path, destination, &[], true, false, true, false).unwrap_err();
        let error = error.to_string();
        assert!(
            error.contains("case-insensitive filesystems")
                || error.contains("duplicate restore target"),
            "unexpected restore error: {error}"
        );
    }
    assert!(!dest.exists());
}

#[test]
fn restore_git_rejects_windows_invalid_project_names() {
    let temp = TestDir::new();
    let original = temp.0.join("original");
    for (index, project) in ["CON", "CONIN$", "CONOUT$", "bad:name", "trailing."]
        .into_iter()
        .enumerate()
    {
        let manifest_path = temp.0.join(format!("manifest-{index}.json"));
        let manifest = Manifest {
            schema_version: 2,
            created_at: "2026-10-26T00:00:00+00:00".into(),
            tool: "proj-janitor".into(),
            version: "0.2.3".into(),
            include_artifacts: false,
            always_archive: false,
            exclude_git_metadata: false,
            records: vec![Record {
                project: project.into(),
                source: original.join(project).to_string_lossy().into_owned(),
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
            }],
        };
        fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        let dest = temp.0.join(format!("restored-{index}"));

        let error = restore_cmd(
            &manifest_path,
            Some(dest.clone()),
            &[],
            true,
            false,
            true,
            false,
        )
        .unwrap_err();

        assert!(error.to_string().contains("unsafe project name"));
        assert!(!dest.exists());
    }
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
fn metadata_if_present_ignores_disappeared_files() {
    let temp = TestDir::new();
    assert!(
        crate::project::metadata_if_present(&temp.0.join("missing"))
            .unwrap()
            .is_none()
    );
    let file = temp.0.join("present");
    fs::write(&file, "present").unwrap();
    assert!(
        crate::project::metadata_if_present(&file)
            .unwrap()
            .is_some()
    );
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
