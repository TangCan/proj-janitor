use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::atomic::{AtomicU64, Ordering},
};

static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);

struct TestDir(PathBuf);

impl TestDir {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "projjan-cli-test-{}-{}",
            std::process::id(),
            NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&path).unwrap();
        Self(path)
    }

    fn project(&self) -> PathBuf {
        let path = self.0.join("demo");
        fs::create_dir_all(&path).unwrap();
        fs::write(path.join("pyproject.toml"), "[project]\n").unwrap();
        path
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn dry_run(project: &Path, env_dest: &Path, cli_dest: Option<&Path>) -> String {
    let mut command = Command::new(env!("CARGO_BIN_EXE_projjan"));
    command
        .arg("backup")
        .arg("--dry-run")
        .arg(project)
        .env("BACKUP_DEST", env_dest);
    if let Some(dest) = cli_dest {
        command.arg("--backup-dest").arg(dest);
    }
    let output = command.output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

fn destination_marker(dest: &Path) -> String {
    format!("{}/demo", dest.file_name().unwrap().to_string_lossy())
}

fn normalized_output(output: &str) -> String {
    output.replace('\\', "/")
}

fn git(path: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .arg("-C")
        .arg(path)
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).trim().to_owned()
}

fn git_file_url(path: &Path) -> String {
    url::Url::from_file_path(path).unwrap().to_string()
}

struct SubmoduleFixture {
    project: PathBuf,
    child_remote: PathBuf,
    parent_remote: PathBuf,
    child_commit: String,
}

fn submodule_fixture(temp: &TestDir) -> SubmoduleFixture {
    let child = temp.0.join("child");
    fs::create_dir_all(&child).unwrap();
    fs::write(child.join("main.py"), "print('child')\n").unwrap();
    git(&child, &["init", "-q", "-b", "main"]);
    git(&child, &["config", "user.email", "test@example.com"]);
    git(&child, &["config", "user.name", "Test User"]);
    git(&child, &["add", "."]);
    git(&child, &["commit", "-q", "-m", "child"]);
    let child_commit = git(&child, &["rev-parse", "HEAD"]);
    let child_remote = temp.0.join("child-remote.git");
    let output = Command::new("git")
        .args(["clone", "-q", "--bare"])
        .arg(&child)
        .arg(&child_remote)
        .output()
        .unwrap();
    assert!(output.status.success());

    let project = temp.0.join("app");
    fs::create_dir_all(&project).unwrap();
    fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
    fs::write(
        project.join(".gitmodules"),
        "[submodule \"vendor/child\"]\n\tpath = vendor/child\n\turl = https://github.com/acme/child.git\n",
    )
    .unwrap();
    git(&project, &["init", "-q", "-b", "main"]);
    git(&project, &["config", "user.email", "test@example.com"]);
    git(&project, &["config", "user.name", "Test User"]);
    git(&project, &["add", "."]);
    git(&project, &["commit", "-q", "-m", "parent"]);
    git(
        &project,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("160000,{child_commit},vendor/child"),
        ],
    );
    git(&project, &["commit", "-q", "-m", "add submodule"]);
    fs::create_dir_all(project.join("vendor/child")).unwrap();
    let parent_remote = temp.0.join("app-remote.git");
    let output = Command::new("git")
        .args(["init", "-q", "--bare"])
        .arg(&parent_remote)
        .output()
        .unwrap();
    assert!(output.status.success());
    git(
        &project,
        &["remote", "add", "origin", parent_remote.to_str().unwrap()],
    );
    git(&project, &["push", "-q", "-u", "origin", "main"]);
    git(
        &project,
        &[
            "remote",
            "set-url",
            "origin",
            "https://github.com/acme/app.git",
        ],
    );
    SubmoduleFixture {
        project,
        child_remote,
        parent_remote,
        child_commit,
    }
}

#[test]
fn verify_and_restore_check_remote_probes_submodules() {
    let temp = TestDir::new();
    let fixture = submodule_fixture(&temp);

    let backup_dest = temp.0.join("backups");
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup"])
        .arg(&fixture.project)
        .arg("--backup-dest")
        .arg(&backup_dest)
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    let manifest = fs::read_dir(backup_dest.join("app"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .unwrap();
    let manifest_json: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    assert_eq!(manifest_json["records"][0]["strategy"], "git");
    let submodules = manifest_json["records"][0]["submodules"]
        .as_array()
        .unwrap();
    assert_eq!(submodules.len(), 1);
    assert_eq!(submodules[0]["path"], "vendor/child");
    assert_eq!(submodules[0]["url"], "https://github.com/acme/child.git");
    assert_eq!(submodules[0]["commit"], fixture.child_commit);

    let parent_url = git_file_url(&fixture.parent_remote);
    let child_url = git_file_url(&fixture.child_remote);

    // Both the top-level and submodule remotes are reachable (rewritten to the
    // local bare remotes via insteadOf), so verify reports both as verified.
    let verified = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["verify", "--check-remote", "--json"])
        .arg(&manifest)
        .env("GIT_CONFIG_COUNT", "3")
        .env("GIT_CONFIG_KEY_0", format!("url.{parent_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_0", "https://github.com/acme/app.git")
        .env("GIT_CONFIG_KEY_1", format!("url.{child_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_1", "https://github.com/acme/child.git")
        .env("GIT_CONFIG_KEY_2", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_2", "always")
        .output()
        .unwrap();
    assert!(
        verified.status.success(),
        "{}",
        String::from_utf8_lossy(&verified.stderr)
    );
    let verified_json: serde_json::Value = serde_json::from_slice(&verified.stdout).unwrap();
    assert_eq!(verified_json["projects"][0]["status"], "remote-verified");
    let checked_submodules = verified_json["projects"][0]["submodules"]
        .as_array()
        .unwrap();
    assert_eq!(checked_submodules.len(), 1);
    assert_eq!(checked_submodules[0]["path"], "vendor/child");
    assert_eq!(checked_submodules[0]["status"], "remote-verified");

    // The submodule remote is rewritten to a missing path, so verify reports
    // the submodule as remote-unreachable and fails the whole check.
    let unreachable = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["verify", "--check-remote", "--json"])
        .arg(&manifest)
        .env("GIT_CONFIG_COUNT", "3")
        .env("GIT_CONFIG_KEY_0", format!("url.{parent_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_0", "https://github.com/acme/app.git")
        .env(
            "GIT_CONFIG_KEY_1",
            "url.file:///missing-child.git.insteadOf",
        )
        .env("GIT_CONFIG_VALUE_1", "https://github.com/acme/child.git")
        .env("GIT_CONFIG_KEY_2", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_2", "always")
        .output()
        .unwrap();
    assert!(!unreachable.status.success());
    let unreachable_json: serde_json::Value = serde_json::from_slice(&unreachable.stdout).unwrap();
    let unreachable_submodules = unreachable_json["projects"][0]["submodules"]
        .as_array()
        .unwrap();
    assert_eq!(unreachable_submodules[0]["status"], "remote-unreachable");

    // restore --check-remote blocks before creating any target when a
    // submodule remote is unreachable.
    let failed_dest = temp.0.join("failed-restore");
    let failed = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--check-remote"])
        .arg(&manifest)
        .arg("--dest")
        .arg(&failed_dest)
        .env("GIT_CONFIG_COUNT", "3")
        .env("GIT_CONFIG_KEY_0", format!("url.{parent_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_0", "https://github.com/acme/app.git")
        .env(
            "GIT_CONFIG_KEY_1",
            "url.file:///missing-child.git.insteadOf",
        )
        .env("GIT_CONFIG_VALUE_1", "https://github.com/acme/child.git")
        .env("GIT_CONFIG_KEY_2", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_2", "always")
        .output()
        .unwrap();
    assert!(!failed.status.success());
    let stderr = String::from_utf8_lossy(&failed.stderr);
    assert!(
        stderr.contains("submodule vendor/child"),
        "stderr should mention the submodule: {stderr}"
    );
    assert!(!failed_dest.join("app").exists());

    let progress_dest = temp.0.join("progress-restore");
    let progress = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--progress", "--yes"])
        .arg(&manifest)
        .arg("--dest")
        .arg(&progress_dest)
        .env("GIT_CONFIG_COUNT", "3")
        .env("GIT_CONFIG_KEY_0", format!("url.{parent_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_0", "https://github.com/acme/app.git")
        .env("GIT_CONFIG_KEY_1", format!("url.{child_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_1", "https://github.com/acme/child.git")
        .env("GIT_CONFIG_KEY_2", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_2", "always")
        .output()
        .unwrap();
    assert!(
        progress.status.success(),
        "{}",
        String::from_utf8_lossy(&progress.stderr)
    );
    let progress_err = String::from_utf8_lossy(&progress.stderr);
    assert!(
        progress_err.contains("progress: app: updating 1 submodule(s)"),
        "{progress_err}"
    );
    assert!(
        progress_err.contains("progress: app: submodules done ("),
        "{progress_err}"
    );
}

#[test]
fn cli_help_and_version_are_available_without_subcommands() {
    for (argument, expected) in [
        ("--help", "Usage: projjan".to_owned()),
        (
            "--version",
            format!("projjan {}", env!("CARGO_PKG_VERSION")),
        ),
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
            .arg(argument)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(String::from_utf8_lossy(&output.stdout).contains(&expected));
        assert!(output.stderr.is_empty());
    }
}

#[test]
fn backup_help_hides_backup_dest_environment_value() {
    let temp = TestDir::new();
    let backup_dest = temp.0.join("private-backup-location");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--help"])
        .env("BACKUP_DEST", &backup_dest)
        .output()
        .unwrap();

    assert!(output.status.success());
    let help = String::from_utf8_lossy(&output.stdout);
    assert!(help.contains("BACKUP_DEST"));
    assert!(!help.contains(backup_dest.to_str().unwrap()));
}

#[test]
fn backup_help_describes_options() {
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--help"])
        .output()
        .unwrap();
    assert!(output.status.success());
    let help = String::from_utf8_lossy(&output.stdout);
    assert!(
        help.contains("Backup root directory"),
        "missing --backup-dest help: {help}"
    );
    assert!(
        help.contains("Print per-project and packing progress"),
        "missing --progress help: {help}"
    );
    assert!(
        help.contains("Encrypt archive/bundle"),
        "missing --encrypt-passphrase-file help: {help}"
    );
}

#[test]
fn top_level_help_describes_subcommands() {
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("--help")
        .output()
        .unwrap();
    assert!(output.status.success());
    let help = String::from_utf8_lossy(&output.stdout);
    assert!(help.contains("Record Git remotes or archive projects"));
    assert!(help.contains("Restore projects from a backup manifest"));
    assert!(help.contains("Write a starter .proj-janitor.toml"));
}

#[test]
fn backup_and_restore_git_lfs_objects_with_git_strategy() {
    if cfg!(windows) {
        eprintln!("skipping local-file Git LFS integration on Windows");
        return;
    }
    let version = Command::new("git")
        .args(["lfs", "version"])
        .output()
        .unwrap();
    if !version.status.success() {
        eprintln!("skipping Git LFS integration: git-lfs is unavailable");
        return;
    }
    let temp = TestDir::new();
    let project = temp.0.join("lfs-app");
    fs::create_dir_all(&project).unwrap();
    fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
    fs::write(project.join(".gitignore"), ".husky/_/\n").unwrap();
    fs::create_dir_all(project.join(".husky/_")).unwrap();
    fs::write(project.join(".husky/pre-commit"), "npm test\n").unwrap();
    fs::write(
        project.join(".husky/_/husky.sh"),
        "# generated hook helper\n",
    )
    .unwrap();
    git(&project, &["init", "-q", "-b", "main"]);
    git(&project, &["config", "user.email", "test@example.com"]);
    git(&project, &["config", "user.name", "Test User"]);
    git(&project, &["lfs", "install", "--local"]);
    git(&project, &["lfs", "track", "*.bin"]);
    let payload = b"a tracked Git LFS payload\n";
    fs::write(project.join("payload.bin"), payload).unwrap();
    git(&project, &["add", "."]);
    git(&project, &["commit", "-q", "-m", "LFS project"]);

    let remote = temp.0.join("lfs-app-remote.git");
    let output = Command::new("git")
        .args(["init", "-q", "--bare"])
        .arg(&remote)
        .output()
        .unwrap();
    assert!(output.status.success());
    git(
        &project,
        &["remote", "add", "origin", remote.to_str().unwrap()],
    );
    git(&project, &["push", "-q", "-u", "origin", "main"]);
    git(
        &project,
        &[
            "remote",
            "set-url",
            "origin",
            "https://github.com/acme/lfs-app.git",
        ],
    );
    let backup_dest = temp.0.join("backups");
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup"])
        .arg(&project)
        .arg("--backup-dest")
        .arg(&backup_dest)
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    let manifest = fs::read_dir(backup_dest.join("lfs-app"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .unwrap();
    let manifest_json: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    assert_eq!(manifest_json["records"][0]["strategy"], "git");
    assert!(manifest_json["records"][0]["archive"].is_null());
    // The manifest records the LFS pointer OID so long-term recovery can
    // verify object availability independently of the Git remote.
    let object_hash = format!("{:x}", Sha256::digest(payload));
    let lfs_objects = manifest_json["records"][0]["lfs_objects"]
        .as_array()
        .unwrap();
    assert!(
        lfs_objects.iter().any(|value| {
            value == object_hash.as_str()
                || (value["oid"] == object_hash.as_str() && value["size"] == payload.len() as u64)
        }),
        "lfs_objects should contain the tracked payload OID+size: {lfs_objects:?}"
    );

    let restored_dest = temp.0.join("restored");
    let remote_url = git_file_url(&remote);
    let verify = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["verify", "--check-remote", "--json"])
        .arg(&manifest)
        .env("GIT_CONFIG_COUNT", "2")
        .env("GIT_CONFIG_KEY_0", format!("url.{remote_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_0", "https://github.com/acme/lfs-app.git")
        .env("GIT_CONFIG_KEY_1", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_1", "always")
        .output()
        .unwrap();
    assert!(
        verify.status.success(),
        "{}",
        String::from_utf8_lossy(&verify.stderr)
    );
    let verified_json: serde_json::Value = serde_json::from_slice(&verify.stdout).unwrap();
    assert_eq!(verified_json["projects"][0]["status"], "remote-verified");
    assert_eq!(
        verified_json["projects"][0]["lfs"][0]["status"],
        "lfs-verified"
    );

    let restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--check-remote"])
        .arg(&manifest)
        .arg("--dest")
        .arg(&restored_dest)
        .env("GIT_CONFIG_COUNT", "2")
        .env("GIT_CONFIG_KEY_0", format!("url.{remote_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_0", "https://github.com/acme/lfs-app.git")
        .env("GIT_CONFIG_KEY_1", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_1", "always")
        .output()
        .unwrap();
    assert!(
        restore.status.success(),
        "{}",
        String::from_utf8_lossy(&restore.stderr)
    );
    assert_eq!(
        fs::read(restored_dest.join("lfs-app/payload.bin")).unwrap(),
        payload
    );

    let progress_dest = temp.0.join("restored-progress");
    let progress_restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--progress", "--yes"])
        .arg(&manifest)
        .arg("--dest")
        .arg(&progress_dest)
        .env("GIT_CONFIG_COUNT", "2")
        .env("GIT_CONFIG_KEY_0", format!("url.{remote_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_0", "https://github.com/acme/lfs-app.git")
        .env("GIT_CONFIG_KEY_1", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_1", "always")
        .output()
        .unwrap();
    assert!(
        progress_restore.status.success(),
        "{}",
        String::from_utf8_lossy(&progress_restore.stderr)
    );
    let progress_err = String::from_utf8_lossy(&progress_restore.stderr);
    assert!(
        progress_err.contains("progress: lfs-app: pulling LFS objects"),
        "{progress_err}"
    );
    assert!(
        progress_err.contains("progress: lfs-app: LFS pull done ("),
        "{progress_err}"
    );

    let lfs_object = remote
        .join("lfs/objects")
        .join(&object_hash[..2])
        .join(&object_hash[2..4])
        .join(&object_hash);
    fs::remove_file(lfs_object).unwrap();
    let missing_verify = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["verify", "--check-remote", "--json"])
        .arg(&manifest)
        .env("GIT_CONFIG_COUNT", "2")
        .env("GIT_CONFIG_KEY_0", format!("url.{remote_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_0", "https://github.com/acme/lfs-app.git")
        .env("GIT_CONFIG_KEY_1", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_1", "always")
        .output()
        .unwrap();
    assert!(!missing_verify.status.success());
    let missing_json: serde_json::Value = serde_json::from_slice(&missing_verify.stdout).unwrap();
    assert_eq!(missing_json["projects"][0]["status"], "lfs-unavailable");
    assert_eq!(
        missing_json["projects"][0]["lfs"][0]["status"],
        "lfs-missing"
    );

    let failed_dest = temp.0.join("missing-lfs-restore");
    let blocked = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--check-remote"])
        .arg(&manifest)
        .arg("--dest")
        .arg(&failed_dest)
        .env("GIT_CONFIG_COUNT", "2")
        .env("GIT_CONFIG_KEY_0", format!("url.{remote_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_0", "https://github.com/acme/lfs-app.git")
        .env("GIT_CONFIG_KEY_1", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_1", "always")
        .output()
        .unwrap();
    assert!(!blocked.status.success());
    let blocked_err = String::from_utf8_lossy(&blocked.stderr);
    assert!(blocked_err.contains("LFS object"), "{blocked_err}");
    assert!(!failed_dest.join("lfs-app").exists());

    let failed_restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore"])
        .arg(&manifest)
        .arg("--dest")
        .arg(&failed_dest)
        .env("GIT_CONFIG_COUNT", "2")
        .env("GIT_CONFIG_KEY_0", format!("url.{remote_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_0", "https://github.com/acme/lfs-app.git")
        .env("GIT_CONFIG_KEY_1", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_1", "always")
        .output()
        .unwrap();
    assert!(!failed_restore.status.success());
    assert!(!failed_dest.join("lfs-app").exists());
}

#[test]
fn backup_refuses_to_archive_credentialed_submodule_urls() {
    let temp = TestDir::new();
    let project = temp.0.join("app");
    fs::create_dir_all(&project).unwrap();
    fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
    fs::write(
        project.join(".gitmodules"),
        "[submodule \"private\"]\n\tpath = vendor/private\n\turl = ssh://git:ssh-secret@github.com/acme/private.git\n",
    )
    .unwrap();
    git(&project, &["init", "-q", "-b", "main"]);
    git(&project, &["config", "user.email", "test@example.com"]);
    git(&project, &["config", "user.name", "Test User"]);
    git(&project, &["add", "."]);
    git(
        &project,
        &["commit", "-q", "-m", "credentialed submodule URL"],
    );

    let remote = temp.0.join("app-remote.git");
    let output = Command::new("git")
        .args(["init", "-q", "--bare"])
        .arg(&remote)
        .output()
        .unwrap();
    assert!(output.status.success());
    git(
        &project,
        &["remote", "add", "origin", remote.to_str().unwrap()],
    );
    git(&project, &["push", "-q", "-u", "origin", "main"]);
    git(
        &project,
        &[
            "remote",
            "set-url",
            "origin",
            "https://github.com/acme/app.git",
        ],
    );

    let backup_dest = temp.0.join("backups");
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup"])
        .arg(&project)
        .arg("--backup-dest")
        .arg(&backup_dest)
        .output()
        .unwrap();
    assert!(
        !backup.status.success(),
        "stdout: {}\nstderr: {}",
        String::from_utf8_lossy(&backup.stdout),
        String::from_utf8_lossy(&backup.stderr)
    );
    let stderr = String::from_utf8_lossy(&backup.stderr);
    assert!(
        stderr.contains("submodule URL contains credentials or query data"),
        "{stderr}"
    );
    assert!(!stderr.contains("ssh-secret"));
    assert!(!backup_dest.join("app").exists());

    let dry_dest = temp.0.join("dry-backups");
    let dry_run = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--dry-run"])
        .arg(&project)
        .arg("--backup-dest")
        .arg(&dry_dest)
        .output()
        .unwrap();
    assert!(!dry_run.status.success());
    let stdout = String::from_utf8_lossy(&dry_run.stdout);
    assert!(stdout.contains("[error] app: submodule URL contains credentials or query data"));
    assert!(!stdout.contains("ssh-secret"));
    assert!(!stdout.contains(".tar.gz"));
    assert!(!dry_dest.exists());
}

#[test]
fn backup_and_restore_uninitialized_submodule_with_git_strategy() {
    let temp = TestDir::new();
    let child = temp.0.join("child");
    fs::create_dir_all(&child).unwrap();
    fs::write(child.join("main.py"), "print('child')\n").unwrap();
    git(&child, &["init", "-q", "-b", "main"]);
    git(&child, &["config", "user.email", "test@example.com"]);
    git(&child, &["config", "user.name", "Test User"]);
    git(&child, &["add", "."]);
    git(&child, &["commit", "-q", "-m", "child"]);
    let child_commit = git(&child, &["rev-parse", "HEAD"]);
    let child_remote = temp.0.join("child-remote.git");
    let output = Command::new("git")
        .args(["clone", "-q", "--bare"])
        .arg(&child)
        .arg(&child_remote)
        .output()
        .unwrap();
    assert!(output.status.success());

    let project = temp.0.join("app");
    fs::create_dir_all(&project).unwrap();
    fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
    fs::write(
        project.join(".gitmodules"),
        "[submodule \"vendor/child\"]\n\tpath = vendor/child\n\turl = https://github.com/acme/child.git\n",
    )
    .unwrap();
    git(&project, &["init", "-q", "-b", "main"]);
    git(&project, &["config", "user.email", "test@example.com"]);
    git(&project, &["config", "user.name", "Test User"]);
    git(&project, &["add", "."]);
    git(&project, &["commit", "-q", "-m", "parent"]);
    git(
        &project,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("160000,{child_commit},vendor/child"),
        ],
    );
    git(&project, &["commit", "-q", "-m", "add submodule"]);
    fs::create_dir_all(project.join("vendor/child")).unwrap();
    let local_submodule_data = project.join("vendor/child/local-data.txt");
    fs::write(&local_submodule_data, "keep me\n").unwrap();
    let parent_remote = temp.0.join("app-remote.git");
    let output = Command::new("git")
        .args(["init", "-q", "--bare"])
        .arg(&parent_remote)
        .output()
        .unwrap();
    assert!(output.status.success());
    git(
        &project,
        &["remote", "add", "origin", parent_remote.to_str().unwrap()],
    );
    git(&project, &["push", "-q", "-u", "origin", "main"]);
    git(
        &project,
        &[
            "remote",
            "set-url",
            "origin",
            "https://github.com/acme/app.git",
        ],
    );

    let stray_backup_dest = temp.0.join("stray-backups");
    let stray_backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup"])
        .arg(&project)
        .arg("--backup-dest")
        .arg(&stray_backup_dest)
        .output()
        .unwrap();
    assert!(stray_backup.status.success());
    let stray_manifest = fs::read_dir(stray_backup_dest.join("app"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .unwrap();
    let stray_json: serde_json::Value =
        serde_json::from_slice(&fs::read(stray_manifest).unwrap()).unwrap();
    assert_eq!(stray_json["records"][0]["strategy"], "archive");
    assert_eq!(
        stray_json["records"][0]["fallback_reason"],
        "uninitialized submodule path is not empty: vendor/child"
    );
    fs::remove_file(local_submodule_data).unwrap();

    let backup_dest = temp.0.join("backups");
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup"])
        .arg(&project)
        .arg("--backup-dest")
        .arg(&backup_dest)
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    let manifest = fs::read_dir(backup_dest.join("app"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .unwrap();
    let manifest_json: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    assert_eq!(manifest_json["records"][0]["strategy"], "git");
    assert!(manifest_json["records"][0]["archive"].is_null());

    let restored_dest = temp.0.join("restored");
    let parent_url = git_file_url(&parent_remote);
    let child_url = git_file_url(&child_remote);
    let restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore"])
        .arg(&manifest)
        .arg("--dest")
        .arg(&restored_dest)
        .env("GIT_CONFIG_COUNT", "3")
        .env("GIT_CONFIG_KEY_0", format!("url.{parent_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_0", "https://github.com/acme/app.git")
        .env("GIT_CONFIG_KEY_1", format!("url.{child_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_1", "https://github.com/acme/child.git")
        .env("GIT_CONFIG_KEY_2", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_2", "always")
        .output()
        .unwrap();
    assert!(
        restore.status.success(),
        "{}",
        String::from_utf8_lossy(&restore.stderr)
    );
    let restored_child = restored_dest.join("app/vendor/child");
    assert_eq!(
        fs::read_to_string(restored_child.join("main.py"))
            .unwrap()
            .replace("\r\n", "\n"),
        "print('child')\n"
    );
    assert_eq!(git(&restored_child, &["rev-parse", "HEAD"]), child_commit);
    git(
        &restored_dest.join("app"),
        &["switch", "--track", "-c", "main", "origin/main"],
    );

    let initialized_backup_dest = temp.0.join("initialized-backups");
    let initialized_backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup"])
        .arg(restored_dest.join("app"))
        .arg("--backup-dest")
        .arg(&initialized_backup_dest)
        .output()
        .unwrap();
    assert!(
        initialized_backup.status.success(),
        "{}",
        String::from_utf8_lossy(&initialized_backup.stderr)
    );
    let initialized_manifest = fs::read_dir(initialized_backup_dest.join("app"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .unwrap();
    let initialized_json: serde_json::Value =
        serde_json::from_slice(&fs::read(initialized_manifest).unwrap()).unwrap();
    assert_eq!(initialized_json["records"][0]["strategy"], "git");

    let failed_dest = temp.0.join("failed-restore");
    let failed_restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore"])
        .arg(&manifest)
        .arg("--dest")
        .arg(&failed_dest)
        .env("GIT_CONFIG_COUNT", "3")
        .env("GIT_CONFIG_KEY_0", format!("url.{parent_url}.insteadOf"))
        .env("GIT_CONFIG_VALUE_0", "https://github.com/acme/app.git")
        .env(
            "GIT_CONFIG_KEY_1",
            "url.file:///missing-child.git.insteadOf",
        )
        .env("GIT_CONFIG_VALUE_1", "https://github.com/acme/child.git")
        .env("GIT_CONFIG_KEY_2", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_2", "always")
        .output()
        .unwrap();
    assert!(!failed_restore.status.success());
    assert!(!failed_dest.join("app").exists());
}

#[test]
fn clean_progress_reports_planning_and_deletion() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::create_dir_all(project.join("node_modules/pkg")).unwrap();
    fs::write(project.join("pyproject.toml"), "").unwrap();
    fs::write(project.join("node_modules/pkg/index.js"), "x").unwrap();
    fs::write(project.join("main.py"), "print(1)\n").unwrap();
    let dry = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["clean", "--progress"])
        .arg(&temp.0)
        .output()
        .unwrap();
    assert!(
        dry.status.success(),
        "{}",
        String::from_utf8_lossy(&dry.stderr)
    );
    let dry_err = String::from_utf8_lossy(&dry.stderr);
    assert!(
        dry_err.contains("progress: cleaning 1 project(s)"),
        "{dry_err}"
    );
    assert!(dry_err.contains("progress: planning 1/1:"), "{dry_err}");
    assert!(dry_err.contains("progress: done, planned "), "{dry_err}");
    assert!(project.join("node_modules").exists());

    let applied = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["clean", "--apply", "--yes", "--progress"])
        .arg(&temp.0)
        .output()
        .unwrap();
    assert!(
        applied.status.success(),
        "{}",
        String::from_utf8_lossy(&applied.stderr)
    );
    let applied_err = String::from_utf8_lossy(&applied.stderr);
    assert!(
        applied_err.contains("progress: deleting 1/"),
        "{applied_err}"
    );
    assert!(
        applied_err.contains("progress: done, deleted "),
        "{applied_err}"
    );
    assert!(!project.join("node_modules").exists());
}

#[test]
fn clean_can_list_rules_without_roots() {
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["clean", "--list-rules"])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8_lossy(&output.stdout).contains("node:"));
    assert!(output.stderr.is_empty());
}

#[test]
fn clean_filters_plans_by_selected_rules() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::create_dir_all(project.join("node_modules/pkg")).unwrap();
    fs::write(project.join("node_modules/pkg/index.js"), "x").unwrap();
    fs::create_dir_all(project.join("target/debug")).unwrap();
    fs::write(project.join("target/debug/cache"), "x").unwrap();

    let selected = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("clean")
        .arg(&project)
        .arg("--rules")
        .arg("node,build")
        .arg("--json")
        .output()
        .unwrap();
    assert!(
        selected.status.success(),
        "{}",
        String::from_utf8_lossy(&selected.stderr)
    );
    let actions: serde_json::Value = serde_json::from_slice(&selected.stdout).unwrap();
    let rules: BTreeSet<_> = actions
        .as_array()
        .unwrap()
        .iter()
        .map(|action| action["rule"].as_str().unwrap())
        .collect();
    assert_eq!(rules, BTreeSet::from(["build", "node"]));
    assert!(project.join("node_modules").is_dir());
    assert!(project.join("target").is_dir());

    let rejected = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("clean")
        .arg(&project)
        .arg("--rules")
        .arg("unknown")
        .arg("--json")
        .output()
        .unwrap();
    assert!(!rejected.status.success());
    assert!(String::from_utf8_lossy(&rejected.stderr).contains("unknown rule: unknown"));
    assert!(project.join("node_modules").is_dir());
    assert!(project.join("target").is_dir());

    let applied = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("clean")
        .arg(&project)
        .arg("--rules")
        .arg("node")
        .arg("--apply")
        .arg("--yes")
        .output()
        .unwrap();
    assert!(
        applied.status.success(),
        "{}",
        String::from_utf8_lossy(&applied.stderr)
    );
    assert!(!project.join("node_modules").exists());
    assert!(project.join("target").is_dir());
}

#[test]
fn backup_cli_creates_an_archive_and_manifest() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::create_dir(project.join(".git")).unwrap();
    fs::write(project.join(".git/config"), "keep git history\n").unwrap();
    let no_git_path = temp.0.join("no-git-path");
    fs::create_dir(&no_git_path).unwrap();
    let backup_dest = temp.0.join("backups");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg(&project)
        .arg("--backup-dest")
        .arg(&backup_dest)
        .env("PATH", &no_git_path)
        .output()
        .unwrap();
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        panic!(
            "backup failed ({:?}); stderr length {}; prefix: {:?}",
            output.status.code(),
            stderr.len(),
            stderr.chars().take(256).collect::<String>()
        );
    }
    assert!(String::from_utf8_lossy(&output.stdout).contains("(detached HEAD)"));

    let project_backup = backup_dest.join(project.file_name().unwrap());
    let entries: Vec<_> = fs::read_dir(&project_backup).unwrap().collect();
    assert_eq!(entries.len(), 2, "expected one archive and one manifest");
    let manifest = entries
        .into_iter()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .unwrap();
    let manifest_json: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    assert_eq!(manifest_json["version"], env!("CARGO_PKG_VERSION"));
    assert_eq!(manifest_json["records"][0]["strategy"], "archive");
    assert_eq!(
        manifest_json["records"][0]["fallback_reason"],
        "detached HEAD"
    );
    let restored_dest = temp.0.join("default-restore");
    let restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("restore")
        .arg(&manifest)
        .arg("--dest")
        .arg(&restored_dest)
        .output()
        .unwrap();
    assert!(
        restore.status.success(),
        "{}",
        String::from_utf8_lossy(&restore.stderr)
    );
    assert!(restored_dest.join("demo/.git/config").is_file());
}

fn backup_run(project: &Path, dest: &Path, extra: &[&str]) {
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg(project)
        .arg("--backup-dest")
        .arg(dest)
        .args(extra)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "backup failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn manifest_count(dir: &Path) -> usize {
    fs::read_dir(dir)
        .unwrap()
        .filter_map(|entry| entry.ok())
        .filter(|entry| {
            entry
                .path()
                .extension()
                .is_some_and(|extension| extension == "json")
        })
        .count()
}

#[test]
fn backup_with_keep_yes_retains_only_latest_manifests() {
    let temp = TestDir::new();
    let project = temp.project();
    let backup_dest = temp.0.join("backups");
    for _ in 0..3 {
        backup_run(&project, &backup_dest, &[]);
    }
    let project_backup = backup_dest.join(project.file_name().unwrap());
    assert_eq!(manifest_count(&project_backup), 3);
    backup_run(&project, &backup_dest, &["--keep", "2", "--yes"]);
    assert_eq!(
        manifest_count(&project_backup),
        2,
        "backup --keep 2 --yes should retain only the latest 2 manifests"
    );
}

#[test]
fn backup_with_keep_without_yes_skips_retention_noninteractively() {
    let temp = TestDir::new();
    let project = temp.project();
    let backup_dest = temp.0.join("backups");
    for _ in 0..3 {
        backup_run(&project, &backup_dest, &[]);
    }
    let project_backup = backup_dest.join(project.file_name().unwrap());
    assert_eq!(manifest_count(&project_backup), 3);
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg(&project)
        .arg("--backup-dest")
        .arg(&backup_dest)
        .arg("--keep")
        .arg("2")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        String::from_utf8_lossy(&output.stdout).contains("Non-interactive; skipping retention"),
        "expected a non-interactive skip message, got: {}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert_eq!(
        manifest_count(&project_backup),
        4,
        "backup --keep 2 without --yes should not delete in a non-interactive session"
    );
}

#[test]
fn backup_incremental_creates_delta_and_restore_reconstructs_tree() {
    let temp = TestDir::new();
    let project = temp.project();
    let backup_dest = temp.0.join("backups");
    fs::write(project.join("a.txt"), "v1").unwrap();
    fs::write(project.join("b.txt"), "keep").unwrap();
    backup_run(&project, &backup_dest, &[]); // full snapshot
    fs::write(project.join("a.txt"), "v2").unwrap();
    fs::write(project.join("c.txt"), "new").unwrap();
    fs::remove_file(project.join("b.txt")).unwrap();
    backup_run(&project, &backup_dest, &["--incremental"]); // delta
    let project_backup = backup_dest.join(project.file_name().unwrap());
    let manifests: Vec<PathBuf> = fs::read_dir(&project_backup)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .collect();
    assert_eq!(manifests.len(), 2);
    let mut delta = None;
    for manifest in &manifests {
        let value: serde_json::Value =
            serde_json::from_slice(&fs::read(manifest).unwrap()).unwrap();
        if value["records"][0]["incremental"].is_object() {
            delta = Some(manifest.clone());
        }
    }
    let delta = delta.expect("expected an incremental delta manifest");
    let delta_json: serde_json::Value = serde_json::from_slice(&fs::read(&delta).unwrap()).unwrap();
    assert_eq!(
        delta_json["records"][0]["incremental"]["deleted"],
        serde_json::json!(["b.txt"])
    );
    let base = delta_json["records"][0]["incremental"]["base"]
        .as_str()
        .unwrap()
        .to_string();
    assert!(
        project_backup.join(&base).exists(),
        "base manifest {base} must be kept in the same directory"
    );
    let restored = temp.0.join("restored");
    let restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore"])
        .arg(&delta)
        .arg("--dest")
        .arg(&restored)
        .output()
        .unwrap();
    assert!(
        restore.status.success(),
        "{}",
        String::from_utf8_lossy(&restore.stderr)
    );
    let restored_project = restored.join(project.file_name().unwrap());
    assert_eq!(
        fs::read_to_string(restored_project.join("a.txt")).unwrap(),
        "v2"
    );
    assert_eq!(
        fs::read_to_string(restored_project.join("c.txt")).unwrap(),
        "new"
    );
    assert!(
        !restored_project.join("b.txt").exists(),
        "deleted b.txt should not be restored"
    );
    assert_eq!(
        fs::read_to_string(restored_project.join("pyproject.toml")).unwrap(),
        "[project]\n"
    );
}

#[test]
fn retention_keeps_whole_incremental_chain_together() {
    let temp = TestDir::new();
    let project = temp.project();
    let backup_dest = temp.0.join("backups");
    backup_run(&project, &backup_dest, &[]); // m1 full
    for _ in 0..3 {
        backup_run(&project, &backup_dest, &["--incremental"]); // m2..m4 deltas
    }
    let project_backup = backup_dest.join(project.file_name().unwrap());
    assert_eq!(manifest_count(&project_backup), 4);
    backup_run(
        &project,
        &backup_dest,
        &["--incremental", "--keep", "1", "--yes"],
    );
    assert_eq!(
        manifest_count(&project_backup),
        5,
        "retention must keep the whole incremental chain (keep 1 protects m1..m4)"
    );
}

#[test]
fn backup_and_restore_git_bundle_when_branch_matches_init_default() {
    // Regression: modern Git refuses fetch into the checked-out default branch
    // (often `master`). Restore must move HEAD aside before fetching refs.
    // Also: `git bundle verify` needs a repo context; run restore with cwd outside
    // any git work tree (e.g. $HOME) so we catch "need a repository to verify".
    let temp = TestDir::new();
    let project = temp.project();
    git(&project, &["init", "-q", "-b", "master"]);
    git(&project, &["config", "user.email", "test@example.com"]);
    git(&project, &["config", "user.name", "Test User"]);
    fs::write(project.join("main.py"), "print('master')\n").unwrap();
    git(&project, &["add", "."]);
    git(&project, &["commit", "-q", "-m", "initial"]);
    let head = git(&project, &["rev-parse", "HEAD"]);
    let backup_dest = temp.0.join("backups");
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg(&project)
        .arg("--backup-dest")
        .arg(&backup_dest)
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    assert!(
        String::from_utf8_lossy(&backup.stdout).contains("[git-bundle]"),
        "stdout: {}",
        String::from_utf8_lossy(&backup.stdout)
    );
    let project_backup = backup_dest.join("demo");
    let manifest = fs::read_dir(&project_backup)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .unwrap();
    let manifest_json: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    assert_eq!(manifest_json["records"][0]["strategy"], "git_bundle");
    assert_eq!(manifest_json["records"][0]["branch"], "master");
    assert_eq!(manifest_json["records"][0]["commit"], head);

    let restored_dest = temp.0.join("restored");
    let outside_git = temp.0.join("outside-git-cwd");
    fs::create_dir_all(&outside_git).unwrap();
    let restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .current_dir(&outside_git)
        .arg("restore")
        .arg(&manifest)
        .arg("--dest")
        .arg(&restored_dest)
        .output()
        .unwrap();
    assert!(
        restore.status.success(),
        "{}",
        String::from_utf8_lossy(&restore.stderr)
    );
    let restored = restored_dest.join("demo");
    assert_eq!(git(&restored, &["rev-parse", "HEAD"]), head);
    assert_eq!(git(&restored, &["branch", "--show-current"]), "master");
    assert_eq!(
        fs::read_to_string(restored.join("main.py"))
            .unwrap()
            .replace("\r\n", "\n"),
        "print('master')\n"
    );
}

#[test]
fn restore_git_bundle_surfaces_git_stderr_on_invalid_bundle() {
    let temp = TestDir::new();
    let backup_dir = temp.0.join("backups/demo");
    fs::create_dir_all(&backup_dir).unwrap();
    let bundle = backup_dir.join("demo-20261107-120000.bundle");
    fs::write(&bundle, b"not a git bundle\n").unwrap();
    let manifest = backup_dir.join("manifest-20261107-120000.json");
    let manifest_json = serde_json::json!({
        "schema_version": 2,
        "created_at": "2026-11-07T00:00:00+00:00",
        "tool": "proj-janitor",
        "version": "0.7.1",
        "include_artifacts": false,
        "always_archive": false,
        "exclude_git_metadata": false,
        "records": [{
            "project": "demo",
            "source": "/tmp/demo",
            "archive": bundle,
            "archive_bytes": 16,
            "source_bytes": 1,
            "sha256": "00",
            "strategy": "git_bundle",
            "remote": null,
            "remote_url": null,
            "branch": "master",
            "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "upstream": null,
            "verified": "local-bundle",
            "fallback_reason": "branch has no upstream remote"
        }]
    });
    fs::write(
        &manifest,
        serde_json::to_vec_pretty(&manifest_json).unwrap(),
    )
    .unwrap();

    let restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .current_dir(&temp.0)
        .arg("restore")
        .arg(&manifest)
        .arg("--dest")
        .arg(temp.0.join("restored"))
        .arg("--no-verify")
        .output()
        .unwrap();
    assert!(
        !restore.status.success(),
        "restore should fail for an invalid bundle"
    );
    let stderr = String::from_utf8_lossy(&restore.stderr);
    assert!(
        stderr.contains("invalid Git bundle for demo"),
        "stderr should keep the high-level error: {stderr}"
    );
    assert!(
        stderr.contains("fatal:") || stderr.contains("error:") || stderr.contains("bundle"),
        "stderr should include git's failure detail: {stderr}"
    );
}

#[test]
fn backup_progress_reports_git_bundle() {
    let temp = TestDir::new();
    let project = temp.project();
    git(&project, &["init", "-q", "-b", "main"]);
    git(&project, &["config", "user.email", "test@example.com"]);
    git(&project, &["config", "user.name", "Test User"]);
    fs::write(project.join("main.py"), "main\n").unwrap();
    git(&project, &["add", "."]);
    git(&project, &["commit", "-q", "-m", "main"]);
    git(&project, &["switch", "-q", "-c", "feature/local"]);
    fs::write(project.join("feature.py"), "local history\n").unwrap();
    git(&project, &["add", "."]);
    git(&project, &["commit", "-q", "-m", "local-only"]);
    let backup_dest = temp.0.join("backups");
    let name = project.file_name().unwrap().to_string_lossy();
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--progress", "--backup-dest"])
        .arg(&backup_dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    let stdout = String::from_utf8_lossy(&backup.stdout);
    let stderr = String::from_utf8_lossy(&backup.stderr);
    assert!(stdout.contains("[git-bundle]"), "stdout: {stdout}");
    assert!(
        stderr.contains(&format!("progress: {name}: creating git bundle")),
        "{stderr}"
    );
    assert!(
        stderr.contains(&format!("progress: {name}: git bundle done (")),
        "{stderr}"
    );
}

#[test]
fn backup_and_restore_unpushed_git_history_with_bundle() {
    let temp = TestDir::new();
    let project = temp.project();
    git(&project, &["init", "-q", "-b", "main"]);
    git(&project, &["config", "user.email", "test@example.com"]);
    git(&project, &["config", "user.name", "Test User"]);
    fs::write(project.join("main.py"), "main\n").unwrap();
    git(&project, &["add", "."]);
    git(&project, &["commit", "-q", "-m", "main"]);
    git(&project, &["switch", "-q", "-c", "feature/local"]);
    fs::write(project.join("feature.py"), "local history\n").unwrap();
    git(&project, &["add", "."]);
    git(&project, &["commit", "-q", "-m", "local-only"]);
    let head = git(&project, &["rev-parse", "HEAD"]);
    let backup_dest = temp.0.join("backups");
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg(&project)
        .arg("--backup-dest")
        .arg(&backup_dest)
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    assert!(
        String::from_utf8_lossy(&backup.stdout).contains("[git-bundle]"),
        "stdout: {}",
        String::from_utf8_lossy(&backup.stdout)
    );
    let project_backup = backup_dest.join("demo");
    let entries: Vec<_> = fs::read_dir(&project_backup)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    let manifest = entries
        .iter()
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .unwrap();
    let bundle = entries
        .iter()
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "bundle")
        })
        .unwrap();
    let manifest_json: serde_json::Value =
        serde_json::from_slice(&fs::read(manifest).unwrap()).unwrap();
    assert_eq!(manifest_json["records"][0]["strategy"], "git_bundle");
    assert_eq!(manifest_json["records"][0]["commit"], head);
    assert_eq!(
        manifest_json["records"][0]["sha256"],
        format!("{:x}", Sha256::digest(fs::read(bundle).unwrap()))
    );
    let verified = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("verify")
        .arg(manifest)
        .output()
        .unwrap();
    assert!(
        verified.status.success(),
        "{}",
        String::from_utf8_lossy(&verified.stderr)
    );

    let restored_dest = temp.0.join("restored");
    let restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("restore")
        .arg(manifest)
        .arg("--dest")
        .arg(&restored_dest)
        .output()
        .unwrap();
    assert!(
        restore.status.success(),
        "{}",
        String::from_utf8_lossy(&restore.stderr)
    );
    let restored = restored_dest.join("demo");
    assert_eq!(git(&restored, &["rev-parse", "HEAD"]), head);
    assert_eq!(
        git(&restored, &["branch", "--show-current"]),
        "feature/local"
    );
    assert!(git(&restored, &["branch", "--list"]).contains("main"));
    assert!(git(&restored, &["remote"]).is_empty());
    assert_eq!(
        fs::read_to_string(restored.join("feature.py"))
            .unwrap()
            .replace("\r\n", "\n"),
        "local history\n"
    );

    let excluded_backups = temp.0.join("excluded-backups");
    let excluded = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg(&project)
        .arg("--backup-dest")
        .arg(&excluded_backups)
        .arg("--exclude-git-metadata")
        .output()
        .unwrap();
    assert!(
        excluded.status.success(),
        "{}",
        String::from_utf8_lossy(&excluded.stderr)
    );
    let excluded_manifest = fs::read_dir(excluded_backups.join("demo"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .unwrap();
    let excluded_json: serde_json::Value =
        serde_json::from_slice(&fs::read(&excluded_manifest).unwrap()).unwrap();
    assert_eq!(excluded_json["exclude_git_metadata"], true);
    assert_eq!(excluded_json["records"][0]["strategy"], "archive");
    let excluded_restore = temp.0.join("excluded-restore");
    let restored_archive = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("restore")
        .arg(excluded_manifest)
        .arg("--dest")
        .arg(&excluded_restore)
        .output()
        .unwrap();
    assert!(
        restored_archive.status.success(),
        "{}",
        String::from_utf8_lossy(&restored_archive.stderr)
    );
    assert!(!excluded_restore.join("demo/.git").exists());

    use std::io::Write;
    fs::OpenOptions::new()
        .append(true)
        .open(bundle)
        .unwrap()
        .write_all(b"corruption")
        .unwrap();
    let rejected = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("verify")
        .arg(manifest)
        .output()
        .unwrap();
    assert!(!rejected.status.success());
    assert!(String::from_utf8_lossy(&rejected.stderr).contains("sha256 mismatch"));
    let rejected_dest = temp.0.join("tampered-restore");
    let rejected_restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("restore")
        .arg(manifest)
        .arg("--dest")
        .arg(&rejected_dest)
        .output()
        .unwrap();
    assert!(!rejected_restore.status.success());
    assert!(!rejected_dest.join("demo").exists());
}

#[test]
fn backup_can_archive_working_tree_without_git_metadata() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::create_dir_all(project.join("src")).unwrap();
    fs::write(project.join("src/main.py"), "print('hi')\n").unwrap();
    let git_dir = project.join(".git");
    fs::create_dir(&git_dir).unwrap();
    fs::write(git_dir.join("config"), "private git metadata\n").unwrap();
    let backup_dest = temp.0.join("backups");
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg(&project)
        .arg("--backup-dest")
        .arg(&backup_dest)
        .arg("--always-archive")
        .arg("--exclude-git-metadata")
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );

    let project_backup = backup_dest.join("demo");
    let manifest = fs::read_dir(&project_backup)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .unwrap();
    let manifest_json: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    assert_eq!(manifest_json["exclude_git_metadata"], true);

    let restored_dest = temp.0.join("restored");
    let restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("restore")
        .arg(&manifest)
        .arg("--dest")
        .arg(&restored_dest)
        .output()
        .unwrap();
    assert!(
        restore.status.success(),
        "{}",
        String::from_utf8_lossy(&restore.stderr)
    );
    assert_eq!(
        fs::read_to_string(restored_dest.join("demo/src/main.py")).unwrap(),
        "print('hi')\n"
    );
    assert!(!restored_dest.join("demo/.git").exists());
    assert!(git_dir.join("config").is_file());
}

#[test]
fn prune_progress_reports_deletion() {
    let temp = TestDir::new();
    let backup_dest = temp.0.join("backups");
    let project_backup = backup_dest.join("demo");
    fs::create_dir_all(&project_backup).unwrap();
    for stamp in ["20261101-100000", "20261102-100000"] {
        let archive = project_backup.join(format!("demo-{stamp}.tar.gz"));
        fs::write(&archive, b"backup").unwrap();
        fs::write(
            project_backup.join(format!("manifest-{stamp}.json")),
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 2,
                "records": [{
                    "project": "demo",
                    "strategy": "archive",
                    "archive": archive,
                }],
            }))
            .unwrap(),
        )
        .unwrap();
    }
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["prune", "--keep", "1", "--apply", "--yes", "--progress"])
        .arg(&backup_dest)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("progress: pruning "), "{stderr}");
    assert!(stderr.contains("progress: deleting 1/"), "{stderr}");
    assert!(stderr.contains("progress: done, deleted "), "{stderr}");
}

#[test]
fn prune_previews_and_removes_only_old_manifest_referenced_files() {
    let temp = TestDir::new();
    let backup_dest = temp.0.join("backups");
    let project_backup = backup_dest.join("demo");
    fs::create_dir_all(&project_backup).unwrap();
    let old_archive = project_backup.join("demo-20261101-100000.tar.gz");
    fs::write(&old_archive, b"old backup").unwrap();
    let old_manifest = project_backup.join("manifest-20261101-100000.json");
    fs::write(
        &old_manifest,
        serde_json::to_vec(&serde_json::json!({
            "schema_version": 2,
            "records": [{
                "project": "demo",
                "strategy": "archive",
                "archive": old_archive,
            }],
        }))
        .unwrap(),
    )
    .unwrap();
    let newest_manifest = project_backup.join("manifest-20261102-100000.json");
    let newest_bundle = project_backup.join("demo-20261102-100000.bundle");
    fs::write(&newest_bundle, b"newest bundle").unwrap();
    fs::write(
        &newest_manifest,
        serde_json::to_vec(&serde_json::json!({
            "schema_version": 2,
            "records": [{
                "project": "demo",
                "strategy": "git_bundle",
                "archive": newest_bundle,
            }],
        }))
        .unwrap(),
    )
    .unwrap();
    let orphan = project_backup.join("orphan.bundle");
    fs::write(&orphan, b"unreferenced artifact").unwrap();

    let invalid_keep = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("prune")
        .arg(&backup_dest)
        .args(["--keep", "0"])
        .output()
        .unwrap();
    assert!(!invalid_keep.status.success());
    assert!(old_manifest.exists());

    let preview = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("prune")
        .arg(&backup_dest)
        .args(["--keep", "1", "--json"])
        .output()
        .unwrap();
    assert!(
        preview.status.success(),
        "{}",
        String::from_utf8_lossy(&preview.stderr)
    );
    let preview_json: serde_json::Value = serde_json::from_slice(&preview.stdout).unwrap();
    assert_eq!(preview_json["dry_run"], true);
    assert_eq!(preview_json["files"].as_array().unwrap().len(), 2);
    assert!(old_manifest.exists());
    assert!(old_archive.exists());

    let refused = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("prune")
        .arg(&backup_dest)
        .args(["--keep", "1", "--apply"])
        .output()
        .unwrap();
    assert!(!refused.status.success());
    assert!(old_manifest.exists());
    assert!(old_archive.exists());

    let applied = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("prune")
        .arg(&backup_dest)
        .args(["--keep", "1", "--apply", "--yes", "--json"])
        .output()
        .unwrap();
    assert!(
        applied.status.success(),
        "{}",
        String::from_utf8_lossy(&applied.stderr)
    );
    let applied_json: serde_json::Value = serde_json::from_slice(&applied.stdout).unwrap();
    assert_eq!(applied_json["deleted"].as_array().unwrap().len(), 2);
    assert!(
        !old_manifest.exists(),
        "manifest still exists; prune output: {}",
        String::from_utf8_lossy(&applied.stdout)
    );
    assert!(
        !old_archive.exists(),
        "archive still exists; prune output: {}",
        String::from_utf8_lossy(&applied.stdout)
    );
    assert!(newest_manifest.exists());
    assert!(newest_bundle.exists());
    assert!(orphan.exists());
}

#[test]
fn backup_records_git_exclusion_option_when_project_has_no_git_metadata() {
    let temp = TestDir::new();
    let project = temp.project();
    let backup_dest = temp.0.join("backups");
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg(&project)
        .arg("--backup-dest")
        .arg(&backup_dest)
        .arg("--always-archive")
        .arg("--exclude-git-metadata")
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    assert!(
        String::from_utf8_lossy(&backup.stdout).contains("Git metadata exclusion enabled"),
        "stdout: {}",
        String::from_utf8_lossy(&backup.stdout)
    );

    let manifest = fs::read_dir(backup_dest.join("demo"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .unwrap();
    let manifest_json: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    assert_eq!(manifest_json["exclude_git_metadata"], true);
    assert!(!project.join(".git").exists());
}

#[test]
fn verify_checks_archive_hash_without_restoring() {
    let temp = TestDir::new();
    let project = temp.project();
    let backup_dir = temp.0.join("backups/demo");
    fs::create_dir_all(&backup_dir).unwrap();
    let archive = backup_dir.join("demo.tar.gz");
    let archive_bytes = b"archive fixture";
    fs::write(&archive, archive_bytes).unwrap();
    let manifest = backup_dir.join("manifest.json");
    let payload = serde_json::json!({
        "schema_version": 2,
        "created_at": "2026-10-27T00:00:00Z",
        "tool": "proj-janitor",
        "version": "0.2.4",
        "include_artifacts": false,
        "always_archive": true,
        "records": [{
            "project": "demo",
            "source": project.display().to_string(),
            "archive": archive.display().to_string(),
            "archive_bytes": archive_bytes.len(),
            "source_bytes": 0,
            "sha256": format!("{:x}", Sha256::digest(archive_bytes)),
            "strategy": "archive"
        }]
    });
    fs::write(&manifest, serde_json::to_vec(&payload).unwrap()).unwrap();

    let verified = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("verify")
        .arg(&manifest)
        .arg("--json")
        .output()
        .unwrap();
    assert!(
        verified.status.success(),
        "{}",
        String::from_utf8_lossy(&verified.stderr)
    );
    let result: serde_json::Value = serde_json::from_slice(&verified.stdout).unwrap();
    assert_eq!(result["projects"][0]["status"], "verified");
    assert!(project.exists());

    fs::write(&archive, b"corruption").unwrap();
    let rejected = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("verify")
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(!rejected.status.success());
    assert!(String::from_utf8_lossy(&rejected.stderr).contains("sha256 mismatch"));
}

#[test]
fn cli_deduplicates_overlapping_roots_for_backup_and_clean() {
    use std::time::{Duration, UNIX_EPOCH};

    let temp = TestDir::new();
    let project = temp.project();
    fs::create_dir_all(project.join("target")).unwrap();
    fs::write(project.join("target/cache"), "remove me\n").unwrap();
    let backup_dest = temp.0.join("backups");
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg(&temp.0)
        .arg(&project)
        .arg("--backup-dest")
        .arg(&backup_dest)
        .arg("--always-archive")
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&backup.stdout)
            .matches("[archive] demo:")
            .count(),
        1
    );
    assert_eq!(
        fs::read_dir(backup_dest.join("demo")).unwrap().count(),
        2,
        "one archive and one manifest should be written"
    );

    let clean = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("clean")
        .arg(&temp.0)
        .arg(&project)
        .arg("--json")
        .output()
        .unwrap();
    assert!(
        clean.status.success(),
        "{}",
        String::from_utf8_lossy(&clean.stderr)
    );
    let plan: serde_json::Value = serde_json::from_slice(&clean.stdout).unwrap();
    assert_eq!(plan.as_array().unwrap().len(), 1);
    assert!(project.join("target/cache").is_file());

    let hidden = temp.0.join(".hidden-project");
    fs::create_dir(&hidden).unwrap();
    fs::write(hidden.join("Cargo.toml"), "[package]\n").unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(&project, temp.0.join("linked-demo")).unwrap();
    let markers = [
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
    let latest_modified = UNIX_EPOCH + Duration::from_secs(1_700_000_000);
    fs::OpenOptions::new()
        .write(true)
        .open(project.join("pyproject.toml"))
        .unwrap()
        .set_modified(UNIX_EPOCH + Duration::from_secs(1_600_000_000))
        .unwrap();
    fs::OpenOptions::new()
        .write(true)
        .open(project.join("target/cache"))
        .unwrap()
        .set_modified(latest_modified)
        .unwrap();
    for (index, marker) in markers.iter().enumerate() {
        let marked = temp.0.join(format!("marker-{index}"));
        fs::create_dir(&marked).unwrap();
        if *marker == ".git" {
            fs::create_dir(marked.join(marker)).unwrap();
        } else {
            fs::write(marked.join(marker), "").unwrap();
        }
    }
    let scan = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("scan")
        .arg(&temp.0)
        .arg(&project)
        .arg("--json")
        .output()
        .unwrap();
    assert!(
        scan.status.success(),
        "{}",
        String::from_utf8_lossy(&scan.stderr)
    );
    let projects: serde_json::Value = serde_json::from_slice(&scan.stdout).unwrap();
    let names: BTreeSet<String> = projects
        .as_array()
        .unwrap()
        .iter()
        .map(|project| project["name"].as_str().unwrap().to_owned())
        .collect();
    let mut expected: BTreeSet<String> = (0..markers.len())
        .map(|index| format!("marker-{index}"))
        .collect();
    expected.insert("demo".into());
    assert_eq!(names, expected);
    let projects = projects.as_array().unwrap();
    let empty_git_project = projects
        .iter()
        .find(|project| project["name"] == "marker-0")
        .unwrap();
    let empty_git_fields = empty_git_project["git"].as_object().unwrap();
    assert!(empty_git_fields.contains_key("branch"));
    assert!(empty_git_fields.contains_key("dirty"));
    assert!(empty_git_fields.contains_key("detail"));
    assert!(empty_git_project["git"]["branch"].is_null());
    assert!(empty_git_project["git"]["dirty"].is_null());
    let detail = empty_git_project["git"]["detail"].as_str().unwrap();
    assert!(detail.contains("git "), "{detail}");
    let scan_err = String::from_utf8_lossy(&scan.stderr);
    assert!(
        scan_err.contains("warning: marker-0: git state unknown"),
        "{scan_err}"
    );
    assert_eq!(empty_git_project["size_bytes"], 0);
    assert!(empty_git_project["last_modified"].is_null());
    let demo = projects
        .iter()
        .find(|project| project["name"] == "demo")
        .unwrap();
    for field in [
        "name",
        "path",
        "size_bytes",
        "last_modified",
        "markers",
        "git",
    ] {
        assert!(demo.get(field).is_some(), "missing scan JSON field {field}");
    }
    assert_eq!(demo.as_object().unwrap().len(), 6);
    assert_eq!(
        demo["path"],
        fs::canonicalize(&project)
            .unwrap()
            .to_string_lossy()
            .as_ref()
    );
    assert_eq!(demo["markers"], serde_json::json!(["pyproject.toml"]));
    assert!(demo["git"].is_null());
    let actual_modified =
        chrono::DateTime::parse_from_rfc3339(demo["last_modified"].as_str().unwrap())
            .unwrap()
            .timestamp();
    assert_eq!(actual_modified, 1_700_000_000);

    let text_scan = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("scan")
        .arg(&temp.0)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        text_scan.status.success(),
        "{}",
        String::from_utf8_lossy(&text_scan.stderr)
    );
    let text = String::from_utf8_lossy(&text_scan.stdout);
    for column in ["PROJECT", "SIZE", "BRANCH", "STATE", "MARKERS", "MODIFIED"] {
        assert!(text.contains(column));
    }
    assert!(text.contains("demo"));

    let refused = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("clean")
        .arg(&temp.0)
        .arg("--json")
        .arg("--apply")
        .output()
        .unwrap();
    assert!(!refused.status.success());
    let actions: serde_json::Value = serde_json::from_slice(&refused.stdout).unwrap();
    assert_eq!(actions.as_array().unwrap().len(), 1);
    assert!(String::from_utf8_lossy(&refused.stderr).contains("--yes"));
    assert!(project.join("target/cache").is_file());

    let applied = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("clean")
        .arg(&temp.0)
        .arg("--json")
        .arg("--apply")
        .arg("--yes")
        .output()
        .unwrap();
    assert!(
        applied.status.success(),
        "{}",
        String::from_utf8_lossy(&applied.stderr)
    );
    let actions: serde_json::Value = serde_json::from_slice(&applied.stdout).unwrap();
    assert_eq!(actions.as_array().unwrap().len(), 1);
    assert!(applied.stderr.is_empty());
    assert!(!project.join("target/cache").exists());
}

#[test]
fn backup_reads_backup_dest_from_environment() {
    let temp = TestDir::new();
    let project = temp.project();
    let env_dest = temp.0.join("from-env");
    let output = dry_run(&project, &env_dest, None);
    assert!(
        normalized_output(&output).contains(&destination_marker(&env_dest)),
        "unexpected dry-run output: {output}"
    );
    assert!(output.contains("(detached HEAD)"));
}

#[test]
fn backup_rejects_empty_backup_dest_environment() {
    let temp = TestDir::new();
    let project = temp.project();
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--dry-run"])
        .arg(&project)
        .env("BACKUP_DEST", "")
        .output()
        .unwrap();

    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("a value is required for '--backup-dest")
    );
}

#[test]
fn command_line_backup_dest_overrides_environment() {
    let temp = TestDir::new();
    let project = temp.project();
    let env_dest = temp.0.join("from-env");
    let cli_dest = temp.0.join("from-cli");
    let output = dry_run(&project, &env_dest, Some(&cli_dest));
    let output = normalized_output(&output);
    assert!(
        output.contains(&destination_marker(&cli_dest)),
        "unexpected dry-run output: {output}"
    );
    assert!(!output.contains(&destination_marker(&env_dest)));
}

#[test]
fn backup_rejects_destination_inside_project() {
    let temp = TestDir::new();
    let project = temp.project();
    let backup_dest = project.join("backups");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg("--dry-run")
        .arg(&project)
        .arg("--backup-dest")
        .arg(&backup_dest)
        .output()
        .unwrap();

    assert!(
        !output.status.success(),
        "stdout: {}\nstderr: {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("backup destination is inside project")
    );
    assert!(!backup_dest.exists());
}

#[test]
fn verify_check_remote_reports_unreachable_and_keeps_credentials_hidden() {
    let temp = TestDir::new();
    let project = temp.project();
    let manifest = temp.0.join("manifest.json");
    let payload = serde_json::json!({
        "schema_version": 2,
        "created_at": "2026-11-04T00:00:00Z",
        "tool": "proj-janitor",
        "version": "0.4.0",
        "include_artifacts": false,
        "always_archive": false,
        "records": [
            {
                "project": "demo",
                "source": project.display().to_string(),
                "archive": null,
                "archive_bytes": 0,
                "source_bytes": 0,
                "sha256": null,
                "strategy": "git",
                "remote": "origin",
                "remote_url": temp.0.join("missing.git").display().to_string(),
                "branch": "main",
                "commit": "a".repeat(40),
                "upstream": "origin/main",
                "verified": "local-ref"
            },
            {
                "project": "second",
                "source": project.display().to_string(),
                "archive": null,
                "archive_bytes": 0,
                "source_bytes": 0,
                "sha256": null,
                "strategy": "git",
                "remote": "origin",
                "remote_url": "https://user:token@github.com/team/demo.git",
                "branch": "main",
                "commit": "b".repeat(40),
                "upstream": "origin/main",
                "verified": "local-ref"
            }
        ]
    });
    fs::write(&manifest, serde_json::to_vec(&payload).unwrap()).unwrap();

    let checked = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("verify")
        .arg(&manifest)
        .arg("--check-remote")
        .arg("--json")
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&checked.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&checked.stderr).into_owned();
    assert!(
        !checked.status.success(),
        "unreachable remote should exit non-zero"
    );
    assert!(
        stderr.contains("one or more remotes or LFS objects could not be verified"),
        "{stderr}"
    );
    let result: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(result["projects"][0]["status"], "remote-unreachable");
    assert_eq!(
        result["projects"][0]["remote"]["reason"],
        "ls-remote-failed"
    );
    assert_eq!(result["projects"][1]["status"], "remote-skipped");
    assert_eq!(
        result["projects"][1]["remote"]["url"],
        "https://github.com/team/demo.git"
    );
    assert!(!stdout.contains("token"), "credential leaked into JSON");
    assert!(!stderr.contains("token"), "credential leaked into stderr");

    let text = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("verify")
        .arg(&manifest)
        .arg("--check-remote")
        .output()
        .unwrap();
    let text_stdout = String::from_utf8_lossy(&text.stdout).into_owned();
    assert!(!text.status.success());
    assert!(
        text_stdout.contains(&format!(
            "remote ls-remote-failed for {}",
            temp.0.join("missing.git").display()
        )),
        "{text_stdout}"
    );
    assert!(text_stdout.contains("remote credentials-in-remote-url; not contacted"));

    let plain = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("verify")
        .arg(&manifest)
        .arg("--json")
        .output()
        .unwrap();
    assert!(plain.status.success());
    let plain_result: serde_json::Value = serde_json::from_slice(&plain.stdout).unwrap();
    assert_eq!(plain_result["projects"][0]["status"], "metadata-only");
    assert!(plain_result["projects"][0]["remote"].is_null());
}

#[test]
fn restore_check_remote_blocks_unreachable_and_keeps_credentials_hidden() {
    let temp = TestDir::new();
    let project = temp.project();
    let manifest = temp.0.join("manifest.json");
    let payload = serde_json::json!({
        "schema_version": 2,
        "created_at": "2026-11-05T00:00:00Z",
        "tool": "proj-janitor",
        "version": "0.4.0",
        "include_artifacts": false,
        "always_archive": false,
        "records": [
            {
                "project": "demo",
                "source": project.display().to_string(),
                "archive": null,
                "archive_bytes": 0,
                "source_bytes": 0,
                "sha256": null,
                "strategy": "git",
                "remote": "origin",
                "remote_url": temp.0.join("missing.git").display().to_string(),
                "branch": "main",
                "commit": "a".repeat(40),
                "upstream": "origin/main",
                "verified": "local-ref"
            },
            {
                "project": "second",
                "source": project.display().to_string(),
                "archive": null,
                "archive_bytes": 0,
                "source_bytes": 0,
                "sha256": null,
                "strategy": "git",
                "remote": "origin",
                "remote_url": "https://user:token@github.com/team/demo.git",
                "branch": "main",
                "commit": "b".repeat(40),
                "upstream": "origin/main",
                "verified": "local-ref"
            }
        ]
    });
    fs::write(&manifest, serde_json::to_vec(&payload).unwrap()).unwrap();
    let restored = temp.0.join("restored");

    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("restore")
        .arg(&manifest)
        .arg("--dest")
        .arg(&restored)
        .arg("--check-remote")
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
    assert!(
        !output.status.success(),
        "unreachable remote should block restore"
    );
    assert!(
        stderr.contains(&format!(
            "cannot restore demo: remote {} is unreachable",
            temp.0.join("missing.git").display()
        )),
        "{stderr}"
    );
    assert!(!restored.exists(), "no target should be created");
    assert!(!stdout.contains("token"), "credential leaked into stdout");
    assert!(!stderr.contains("token"), "credential leaked into stderr");
}

#[test]
fn backup_uses_dest_from_config_file() {
    let temp = TestDir::new();
    let project = temp.project();
    let dest = temp.0.join("config-backups");
    let config = temp.0.join(".proj-janitor.toml");
    fs::write(
        &config,
        format!("[backup]\ndest = {:?}\n", dest.to_string_lossy()),
    )
    .unwrap();

    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg("--dry-run")
        .arg("--config")
        .arg(&config)
        .arg(&project)
        .env_remove("BACKUP_DEST")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        stdout.contains("config-backups"),
        "config dest not used in output: {stdout}"
    );
}

#[test]
fn archive_backup_warns_about_possible_secrets() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join(".env"), "TOKEN=secret\n").unwrap();
    let dest = temp.0.join("backups");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--always-archive", "--backup-dest"])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("archive may include secrets"), "{stderr}");
    assert!(stderr.contains(".env"), "{stderr}");
    assert!(stderr.contains("not encrypted"), "{stderr}");
}

#[cfg(unix)]
#[test]
fn backup_fail_on_insecure_dest_refuses_open_directory() {
    use std::os::unix::fs::PermissionsExt;

    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join("ok.txt"), "hello\n").unwrap();
    let dest = temp.0.join("backups");
    fs::create_dir_all(&dest).unwrap();
    let mut perms = fs::metadata(&dest).unwrap().permissions();
    perms.set_mode(0o755);
    fs::set_permissions(&dest, perms).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args([
            "backup",
            "--always-archive",
            "--dry-run",
            "--fail-on-insecure-dest",
            "--backup-dest",
        ])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        !output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("insecure permissions"), "{stderr}");
    assert!(stderr.contains("0755"), "{stderr}");
}

#[test]
fn backup_check_space_dry_run_succeeds_when_space_available() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join("ok.txt"), "hello\n").unwrap();
    let dest = temp.0.join("backups");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args([
            "backup",
            "--always-archive",
            "--dry-run",
            "--check-space",
            "--backup-dest",
        ])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!dest.exists());
}

#[test]
fn backup_progress_dry_run_estimates_pack_size() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join("ok.txt"), "hello\n").unwrap();
    let dest = temp.0.join("backups");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args([
            "backup",
            "--always-archive",
            "--dry-run",
            "--progress",
            "--backup-dest",
        ])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("progress: would backup 1 project(s)"),
        "{stderr}"
    );
    assert!(stderr.contains("would pack"), "{stderr}");
    assert!(stderr.contains("files"), "{stderr}");
    assert!(stderr.contains("after excludes"), "{stderr}");
    assert!(!dest.exists());
}

#[test]
fn archive_backup_exclude_secrets_omits_secret_paths() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join(".env"), "TOKEN=secret\n").unwrap();
    fs::write(project.join("ok.txt"), "ok\n").unwrap();
    let dest = temp.0.join("backups");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args([
            "backup",
            "--always-archive",
            "--exclude-secrets",
            "--backup-dest",
        ])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("excluding secret paths"), "{stderr}");
    assert!(stderr.contains(".env"), "{stderr}");
    let archive = fs::read_dir(dest.join("demo"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| p.extension().and_then(|e| e.to_str()) == Some("gz"))
        .expect("archive");
    let file = fs::File::open(&archive).unwrap();
    let decoder = flate2::read::GzDecoder::new(file);
    let mut archive = tar::Archive::new(decoder);
    let names: Vec<String> = archive
        .entries()
        .unwrap()
        .map(|e| e.unwrap().path().unwrap().to_string_lossy().into_owned())
        .collect();
    assert!(names.iter().any(|n| n.ends_with("ok.txt")), "{names:?}");
    assert!(names.iter().all(|n| !n.ends_with(".env")), "{names:?}");
}

#[test]
fn archive_backup_fail_on_secrets_refuses_and_writes_nothing() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join(".env"), "TOKEN=secret\n").unwrap();
    let dest = temp.0.join("backups");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args([
            "backup",
            "--always-archive",
            "--fail-on-secrets",
            "--backup-dest",
        ])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        !output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("refusing backup"), "{stderr}");
    assert!(stderr.contains(".env"), "{stderr}");
    assert!(!dest.join("demo").exists());
}

#[cfg(unix)]
#[test]
fn archive_backup_preflight_lists_unsafe_colon_paths() {
    let temp = TestDir::new();
    let project = temp.project();
    let nested = project.join("src/core/prelink");
    fs::create_dir_all(&nested).unwrap();
    fs::write(nested.join("link.rs:25:5"), "").unwrap();
    fs::write(nested.join("mod.rs:1:1"), "").unwrap();
    let dest = temp.0.join("backups");

    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--always-archive", "--backup-dest"])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        !output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("unsafe paths"), "{stderr}");
    assert!(stderr.contains("link.rs:25:5"), "{stderr}");
    assert!(stderr.contains("mod.rs:1:1"), "{stderr}");
    assert!(stderr.contains("remove or rename"), "{stderr}");
    assert!(!dest.join("demo").exists());

    let dry = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--always-archive", "--dry-run", "--backup-dest"])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(!dry.status.success());
    let dry_err = String::from_utf8_lossy(&dry.stderr);
    assert!(dry_err.contains("unsafe paths"), "{dry_err}");
    assert!(dry_err.contains("link.rs:25:5"), "{dry_err}");
}

#[test]
fn archive_backup_fail_on_secrets_defaults_from_config_and_cli_overrides() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join(".env"), "TOKEN=secret\n").unwrap();
    let dest = temp.0.join("backups");
    let config = temp.0.join("cfg.toml");
    fs::write(&config, "[backup]\nfail_on_secrets = true\n").unwrap();

    let refused = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--always-archive", "--config"])
        .arg(&config)
        .args(["--backup-dest"])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        !refused.status.success(),
        "{}",
        String::from_utf8_lossy(&refused.stderr)
    );
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(stderr.contains("refusing backup"), "{stderr}");
    assert!(!dest.join("demo").exists());

    let warned = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args([
            "backup",
            "--always-archive",
            "--no-fail-on-secrets",
            "--config",
        ])
        .arg(&config)
        .args(["--backup-dest"])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        warned.status.success(),
        "{}",
        String::from_utf8_lossy(&warned.stderr)
    );
    let stderr = String::from_utf8_lossy(&warned.stderr);
    assert!(stderr.contains("archive may include secrets"), "{stderr}");
    assert!(dest.join("demo").exists());
}

#[test]
fn verify_check_remote_defaults_from_config_and_cli_overrides() {
    let temp = TestDir::new();
    let project = temp.project();
    git(&project, &["init", "-q", "-b", "main"]);
    git(&project, &["config", "user.email", "test@example.com"]);
    git(&project, &["config", "user.name", "Test User"]);
    git(&project, &["add", "."]);
    git(&project, &["commit", "-q", "-m", "init"]);
    let remote = temp.0.join("demo-remote.git");
    let init = Command::new("git")
        .args(["init", "-q", "--bare"])
        .arg(&remote)
        .output()
        .unwrap();
    assert!(init.status.success());
    git(
        &project,
        &["remote", "add", "origin", remote.to_str().unwrap()],
    );
    git(&project, &["push", "-q", "-u", "origin", "main"]);
    let head = git(&project, &["rev-parse", "HEAD"]);
    let manifest = temp.0.join("manifest.json");
    fs::write(
        &manifest,
        serde_json::json!({
            "schema_version": 2,
            "created_at": "2026-11-07T00:00:00Z",
            "tool": "proj-janitor",
            "version": "0.7.3",
            "include_artifacts": false,
            "always_archive": false,
            "records": [{
                "project": "demo",
                "source": project.display().to_string(),
                "archive": null,
                "archive_bytes": 0,
                "source_bytes": 0,
                "sha256": null,
                "strategy": "git",
                "remote": "origin",
                "remote_url": remote.display().to_string(),
                "branch": "main",
                "commit": head,
                "upstream": "origin/main",
                "verified": "local-ref"
            }]
        })
        .to_string(),
    )
    .unwrap();
    let config = temp.0.join("cfg.toml");
    fs::write(&config, "[verify]\ncheck_remote = true\n").unwrap();

    let enabled = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["verify", "--json", "--config"])
        .arg(&config)
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(
        enabled.status.success(),
        "{}",
        String::from_utf8_lossy(&enabled.stderr)
    );
    let enabled_json: serde_json::Value = serde_json::from_slice(&enabled.stdout).unwrap();
    assert_eq!(enabled_json["projects"][0]["status"], "remote-verified");

    let disabled = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["verify", "--json", "--no-check-remote", "--config"])
        .arg(&config)
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(
        disabled.status.success(),
        "{}",
        String::from_utf8_lossy(&disabled.stderr)
    );
    let disabled_json: serde_json::Value = serde_json::from_slice(&disabled.stdout).unwrap();
    assert_eq!(disabled_json["projects"][0]["status"], "metadata-only");
}

#[test]
fn backup_excludes_patterns_from_config_file() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join("keep.txt"), "keep me\n").unwrap();
    fs::create_dir_all(project.join("node_modules")).unwrap();
    fs::write(project.join("node_modules/dep.js"), "ignored\n").unwrap();
    let dest = temp.0.join("exclude-backups");
    let config = temp.0.join(".proj-janitor.toml");
    fs::write(
        &config,
        format!(
            "[backup]\ndest = {:?}\nexclude = [\"node_modules\"]\n",
            dest.to_string_lossy()
        ),
    )
    .unwrap();

    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg("--config")
        .arg(&config)
        .arg(&project)
        .env_remove("BACKUP_DEST")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );

    let project_dir = dest.join("demo");
    let archive = fs::read_dir(&project_dir)
        .unwrap()
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.path())
        .find(|path| path.to_string_lossy().ends_with(".tar.gz"))
        .expect("archive produced");
    let listing = Command::new("tar")
        .arg("-tzf")
        .arg(&archive)
        .output()
        .unwrap();
    assert!(
        listing.status.success(),
        "{}",
        String::from_utf8_lossy(&listing.stderr)
    );
    let listing = String::from_utf8_lossy(&listing.stdout);
    assert!(
        listing.contains("keep.txt"),
        "keep.txt missing from archive: {listing}"
    );
    assert!(
        !listing.contains("node_modules"),
        "node_modules not excluded from archive: {listing}"
    );
}

#[test]
fn restore_require_dest_blocks_original_location() {
    let temp = TestDir::new();
    let project = temp.project();
    let backup_dest = temp.0.join("backups");
    backup_run(&project, &backup_dest, &["--always-archive"]);
    let manifest = fs::read_dir(backup_dest.join("demo"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("manifest-"))
        })
        .expect("manifest");
    fs::remove_dir_all(&project).unwrap();
    let config = temp.0.join("cfg.toml");
    fs::write(&config, "[restore]\nrequire_dest = true\n").unwrap();

    let refused = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--yes", "--config"])
        .arg(&config)
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(
        !refused.status.success(),
        "{}",
        String::from_utf8_lossy(&refused.stderr)
    );
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(stderr.contains("--dest is required"), "{stderr}");
    assert!(!project.exists());

    let dest = temp.0.join("restored");
    let with_dest = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--config"])
        .arg(&config)
        .args(["--dest"])
        .arg(&dest)
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(
        with_dest.status.success(),
        "{}",
        String::from_utf8_lossy(&with_dest.stderr)
    );
    assert!(dest.join("demo").join("pyproject.toml").exists());

    let override_flag = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--yes", "--no-require-dest", "--config"])
        .arg(&config)
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(
        override_flag.status.success(),
        "{}",
        String::from_utf8_lossy(&override_flag.stderr)
    );
    assert!(project.join("pyproject.toml").exists());
}

#[test]
fn backup_without_dest_source_fails_when_not_configured() {
    let temp = TestDir::new();
    let project = temp.project();

    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg("--dry-run")
        .arg(&project)
        .env_remove("BACKUP_DEST")
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("configure [backup].dest"),
        "expected config hint in stderr"
    );
}

#[test]
fn prune_uses_keep_from_config_and_cli_overrides() {
    let temp = TestDir::new();
    let backup_dest = temp.0.join("backups");
    let project_backup = backup_dest.join("demo");
    fs::create_dir_all(&project_backup).unwrap();
    let manifest = project_backup.join("manifest-20261101-100000.json");
    let archive = project_backup.join("demo-20261101-100000.tar.gz");
    fs::write(&archive, b"old backup").unwrap();
    fs::write(
        &manifest,
        serde_json::to_vec(&serde_json::json!({
            "schema_version": 2,
            "records": [{
                "project": "demo",
                "strategy": "archive",
                "archive": archive,
            }],
        }))
        .unwrap(),
    )
    .unwrap();
    let config = temp.0.join(".proj-janitor.toml");
    fs::write(&config, "[prune]\nkeep = 1\n").unwrap();

    let no_source = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("prune")
        .arg(&backup_dest)
        .args(["--json"])
        .output()
        .unwrap();
    assert!(!no_source.status.success());

    let from_config = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("prune")
        .arg(&backup_dest)
        .args(["--config", config.to_str().unwrap(), "--json"])
        .output()
        .unwrap();
    assert!(
        from_config.status.success(),
        "{}",
        String::from_utf8_lossy(&from_config.stderr)
    );
    let from_config_json: serde_json::Value = serde_json::from_slice(&from_config.stdout).unwrap();
    assert_eq!(from_config_json["keep"], 1);

    let cli_override = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("prune")
        .arg(&backup_dest)
        .args([
            "--config",
            config.to_str().unwrap(),
            "--keep",
            "5",
            "--json",
        ])
        .output()
        .unwrap();
    assert!(
        cli_override.status.success(),
        "{}",
        String::from_utf8_lossy(&cli_override.stderr)
    );
    let override_json: serde_json::Value = serde_json::from_slice(&cli_override.stdout).unwrap();
    assert_eq!(override_json["keep"], 5);
}

#[test]
fn backup_encrypt_and_restore_round_trip() {
    if !Command::new("openssl")
        .arg("version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
    {
        eprintln!("skipping openssl encryption test: openssl unavailable");
        return;
    }
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join("secret.txt"), "payload\n").unwrap();
    let dest = temp.0.join("backups");
    let passphrase = temp.0.join("pass.txt");
    fs::write(&passphrase, "test-passphrase\n").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--always-archive", "--encrypt-passphrase-file"])
        .arg(&passphrase)
        .arg("--backup-dest")
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let mut found_enc = false;
    let mut found_plain = false;
    let mut manifest = None;
    fn visit(
        dir: &std::path::Path,
        found_enc: &mut bool,
        found_plain: &mut bool,
        manifest: &mut Option<std::path::PathBuf>,
    ) {
        for entry in fs::read_dir(dir).unwrap() {
            let entry = entry.unwrap();
            let path = entry.path();
            if path.is_dir() {
                visit(&path, found_enc, found_plain, manifest);
            } else if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                if name.ends_with(".tar.gz.enc") {
                    *found_enc = true;
                } else if name.ends_with(".tar.gz") {
                    *found_plain = true;
                } else if name.starts_with("manifest-") && name.ends_with(".json") {
                    *manifest = Some(path);
                }
            }
        }
    }
    visit(&dest, &mut found_enc, &mut found_plain, &mut manifest);
    assert!(found_enc, "expected encrypted archive");
    assert!(!found_plain, "plaintext archive should be removed");
    let manifest = manifest.expect("manifest");
    let text = fs::read_to_string(&manifest).unwrap();
    assert!(text.contains("openssl-aes-256-cbc-pbkdf2"), "{text}");
    let out = temp.0.join("restored");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--dest"])
        .arg(&out)
        .arg("--yes")
        .arg("--decrypt-passphrase-file")
        .arg(&passphrase)
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let restored =
        fs::read_to_string(out.join(project.file_name().unwrap()).join("secret.txt")).unwrap();
    assert_eq!(restored, "payload\n");
}

#[test]
fn encrypt_decrypt_progress_reports_openssl() {
    if !Command::new("openssl")
        .arg("version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
    {
        eprintln!("skipping openssl progress test: openssl unavailable");
        return;
    }
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join("secret.txt"), "payload\n").unwrap();
    let dest = temp.0.join("backups");
    let passphrase = temp.0.join("pass.txt");
    fs::write(&passphrase, "test-passphrase\n").unwrap();
    let name = project.file_name().unwrap().to_string_lossy();
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args([
            "backup",
            "--always-archive",
            "--progress",
            "--encrypt-passphrase-file",
        ])
        .arg(&passphrase)
        .arg("--backup-dest")
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    let backup_err = String::from_utf8_lossy(&backup.stderr);
    assert!(
        backup_err.contains(&format!("progress: {name}: encrypting with openssl")),
        "{backup_err}"
    );
    assert!(
        backup_err.contains(&format!("progress: {name}: openssl encrypt done (")),
        "{backup_err}"
    );
    let mut manifest = None;
    for entry in fs::read_dir(dest.join(project.file_name().unwrap())).unwrap() {
        let path = entry.unwrap().path();
        if path
            .file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|n| n.starts_with("manifest-") && n.ends_with(".json"))
        {
            manifest = Some(path);
        }
    }
    let manifest = manifest.expect("manifest");
    let out = temp.0.join("restored");
    let restore = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--progress", "--dest"])
        .arg(&out)
        .arg("--yes")
        .arg("--decrypt-passphrase-file")
        .arg(&passphrase)
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(
        restore.status.success(),
        "{}",
        String::from_utf8_lossy(&restore.stderr)
    );
    let restore_err = String::from_utf8_lossy(&restore.stderr);
    assert!(
        restore_err.contains(&format!("progress: {name}: decrypting with openssl")),
        "{restore_err}"
    );
    assert!(
        restore_err.contains(&format!("progress: {name}: openssl decrypt done (")),
        "{restore_err}"
    );
}

fn stress_scale() -> usize {
    std::env::var("PROJ_JANITOR_STRESS_SCALE")
        .ok()
        .and_then(|raw| raw.trim().parse::<usize>().ok())
        .unwrap_or(1)
        .max(1)
}

fn make_crowded_project(root: &Path, buckets: usize, files_per_bucket: usize) -> (PathBuf, u64) {
    let project = root.join("crowded");
    fs::create_dir_all(&project).unwrap();
    fs::write(
        project.join("pyproject.toml"),
        "[project]\nname='crowded'\n",
    )
    .unwrap();
    let junk = project.join("node_modules").join("pkg");
    fs::create_dir_all(&junk).unwrap();
    fs::write(junk.join("index.js"), vec![b'a'; 8192]).unwrap();
    let payload = [b'x'; 64];
    let mut expected_files = 1u64;
    let data = project.join("data");
    for bucket in 0..buckets {
        let bucket_dir = data.join(format!("b{bucket:04}"));
        fs::create_dir_all(&bucket_dir).unwrap();
        for index in 0..files_per_bucket {
            fs::write(bucket_dir.join(format!("f{index:04}.bin")), payload).unwrap();
            expected_files += 1;
        }
    }
    (project, expected_files)
}

#[test]
fn backup_progress_dry_run_estimates_crowded_tree() {
    let temp = TestDir::new();
    let scale = stress_scale();
    let (project, expected_files) = make_crowded_project(&temp.0, 20 * scale, 25);
    let dest = temp.0.join("backups");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args([
            "backup",
            "--always-archive",
            "--dry-run",
            "--progress",
            "--check-space",
            "--backup-dest",
        ])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    let needle = format!("would pack {expected_files} files,");
    assert!(stderr.contains(&needle), "{stderr}");
    assert!(stderr.contains("after excludes"), "{stderr}");
    assert!(!dest.exists());
}

#[test]
fn backup_and_verify_crowded_tree_with_progress() {
    let temp = TestDir::new();
    let scale = stress_scale();
    let (project, expected_files) = make_crowded_project(&temp.0, 15 * scale, 20);
    let dest = temp.0.join("backups");
    let started = std::time::Instant::now();
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args([
            "backup",
            "--always-archive",
            "--progress",
            "--check-space",
            "--backup-dest",
        ])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        started.elapsed().as_secs() < 90,
        "backup took {:?}",
        started.elapsed()
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    let done = format!("done, {expected_files} files,");
    assert!(stderr.contains(&done), "{stderr}");

    let project_dir = dest.join("crowded");
    let manifest = fs::read_dir(&project_dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("manifest-") && n.ends_with(".json"))
        })
        .expect("manifest");
    let verify = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["verify"])
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(
        verify.status.success(),
        "{}",
        String::from_utf8_lossy(&verify.stderr)
    );
}

#[test]
fn scan_many_sibling_projects() {
    let temp = TestDir::new();
    let scale = stress_scale();
    let count = 40 * scale;
    let workspace = temp.0.join("workspace");
    fs::create_dir_all(&workspace).unwrap();
    for index in 0..count {
        let project = workspace.join(format!("proj{index:04}"));
        fs::create_dir_all(&project).unwrap();
        fs::write(
            project.join("pyproject.toml"),
            format!("[project]\nname='proj{index:04}'\n"),
        )
        .unwrap();
        fs::write(project.join("main.py"), "print(1)\n").unwrap();
    }
    let started = std::time::Instant::now();
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["scan", "--json"])
        .arg(&workspace)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        started.elapsed().as_secs() < 30,
        "scan took {:?}",
        started.elapsed()
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    let projects: Vec<serde_json::Value> = serde_json::from_str(&stdout).unwrap();
    assert_eq!(projects.len(), count);
}

#[test]
fn scan_progress_reports_per_project() {
    let temp = TestDir::new();
    let workspace = temp.0.join("workspace");
    fs::create_dir_all(&workspace).unwrap();
    for name in ["alpha", "beta"] {
        let project = workspace.join(name);
        fs::create_dir_all(&project).unwrap();
        fs::write(project.join("pyproject.toml"), "").unwrap();
        fs::write(project.join("main.py"), "print(1)\n").unwrap();
    }
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["scan", "--progress"])
        .arg(&workspace)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("progress: scanning 2 project(s)"),
        "{stderr}"
    );
    assert!(stderr.contains("progress: scanning 1/2:"), "{stderr}");
    assert!(stderr.contains("progress: scanning 2/2:"), "{stderr}");
    assert!(stderr.contains("progress: done, scanned 2 ("), "{stderr}");

    let cfg = temp.0.join("scan.toml");
    fs::write(&cfg, "[scan]\nprogress = true\n").unwrap();
    let via_config = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["scan", "--config"])
        .arg(&cfg)
        .arg(&workspace)
        .output()
        .unwrap();
    assert!(
        via_config.status.success(),
        "{}",
        String::from_utf8_lossy(&via_config.stderr)
    );
    let via_config_err = String::from_utf8_lossy(&via_config.stderr);
    assert!(
        via_config_err.contains("progress: scanning 2 project(s)"),
        "{via_config_err}"
    );

    let disabled = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["scan", "--config"])
        .arg(&cfg)
        .arg("--no-progress")
        .arg(&workspace)
        .output()
        .unwrap();
    assert!(
        disabled.status.success(),
        "{}",
        String::from_utf8_lossy(&disabled.stderr)
    );
    let disabled_err = String::from_utf8_lossy(&disabled.stderr);
    assert!(!disabled_err.contains("progress:"), "{disabled_err}");
}

#[test]
fn verify_progress_reports_per_project() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join("ok.txt"), "data\n").unwrap();
    let dest = temp.0.join("backups");
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--always-archive", "--backup-dest"])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    let project_dir = dest.join(project.file_name().unwrap());
    let manifest = fs::read_dir(&project_dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("manifest-") && n.ends_with(".json"))
        })
        .expect("manifest");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["verify", "--progress"])
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    let name = project.file_name().unwrap().to_string_lossy();
    assert!(stderr.contains("progress: verifying 1/1:"), "{stderr}");
    assert!(
        stderr.contains(&format!("progress: {name}: hashing sha256")),
        "{stderr}"
    );
    assert!(
        stderr.contains(&format!("progress: {name}: sha256 done (")),
        "{stderr}"
    );
    assert!(stderr.contains("progress: done, verified 1 ("), "{stderr}");
}

#[test]
fn restore_progress_reports_per_project() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join("ok.txt"), "data\n").unwrap();
    let dest = temp.0.join("backups");
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--always-archive", "--backup-dest"])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    let project_dir = dest.join(project.file_name().unwrap());
    let manifest = fs::read_dir(&project_dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("manifest-") && n.ends_with(".json"))
        })
        .expect("manifest");
    let out = temp.0.join("restored");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--dest"])
        .arg(&out)
        .arg("--progress")
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    let project_name = project.file_name().unwrap().to_string_lossy();
    assert!(stderr.contains("progress: restoring 1/1:"), "{stderr}");
    assert!(
        stderr.contains(&format!("progress: {project_name}: hashing sha256")),
        "{stderr}"
    );
    assert!(
        stderr.contains(&format!("progress: {project_name}: sha256 done (")),
        "{stderr}"
    );
    assert!(
        stderr.contains(&format!("progress: {project_name}: extracted 1 members (")),
        "{stderr}"
    );
    assert!(
        stderr.contains(&format!("progress: {project_name}: extracted "))
            && stderr.contains("members (done,"),
        "{stderr}"
    );
    assert!(
        stderr.contains("progress: done, restored 1, skipped 0, failed 0 ("),
        "{stderr}"
    );

    let dry = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--dest"])
        .arg(temp.0.join("dry"))
        .args(["--dry-run", "--progress"])
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(dry.status.success());
    let dry_err = String::from_utf8_lossy(&dry.stderr);
    assert!(
        dry_err.contains("progress: would restore 1 project(s)"),
        "{dry_err}"
    );
}

#[test]
fn restore_progress_reports_git_fetch() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join("main.py"), "print('hi')\n").unwrap();
    git(&project, &["init", "-q", "-b", "main"]);
    git(&project, &["config", "user.email", "test@example.com"]);
    git(&project, &["config", "user.name", "Test User"]);
    git(&project, &["add", "."]);
    git(&project, &["commit", "-q", "-m", "initial"]);
    let remote = temp.0.join("app-remote.git");
    let init = Command::new("git")
        .args(["init", "-q", "--bare"])
        .arg(&remote)
        .output()
        .unwrap();
    assert!(init.status.success());
    git(
        &project,
        &["remote", "add", "origin", remote.to_str().unwrap()],
    );
    git(&project, &["push", "-q", "-u", "origin", "main"]);
    let head = git(&project, &["rev-parse", "HEAD"]);
    let remote_url = git_file_url(&remote);
    let manifest = temp.0.join("manifest.json");
    let body = serde_json::json!({
        "schema_version": 2,
        "created_at": "2026-11-09T00:00:00+00:00",
        "tool": "proj-janitor",
        "version": "0.7.25",
        "include_artifacts": false,
        "always_archive": false,
        "records": [{
            "project": project.file_name().unwrap().to_string_lossy(),
            "source": project,
            "archive": null,
            "archive_bytes": 0,
            "source_bytes": 1,
            "sha256": null,
            "strategy": "git",
            "remote": "origin",
            "remote_url": remote_url,
            "branch": "main",
            "commit": head,
            "upstream": "origin/main",
            "verified": "local-ref",
            "fallback_reason": null
        }]
    });
    fs::write(&manifest, serde_json::to_vec_pretty(&body).unwrap()).unwrap();
    let out = temp.0.join("restored");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--dest"])
        .arg(&out)
        .args(["--progress", "--yes"])
        .arg(&manifest)
        .env("GIT_CONFIG_COUNT", "1")
        .env("GIT_CONFIG_KEY_0", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_0", "always")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    let project_name = project.file_name().unwrap().to_string_lossy();
    assert!(stderr.contains("progress: restoring 1/1:"), "{stderr}");
    assert!(
        stderr.contains(&format!("progress: {project_name}: fetching git objects")),
        "{stderr}"
    );
    assert!(
        stderr.contains(&format!("progress: {project_name}: fetch done (")),
        "{stderr}"
    );
    assert!(
        stderr.contains("progress: done, restored 1, skipped 0, failed 0 ("),
        "{stderr}"
    );
}

#[test]
fn backup_progress_reports_per_project() {
    let temp = TestDir::new();
    let first = temp.project();
    fs::write(first.join("ok.txt"), "a\n").unwrap();
    let second = temp.0.join("other");
    fs::create_dir_all(&second).unwrap();
    fs::write(second.join("Cargo.toml"), "").unwrap();
    fs::write(second.join("ok.txt"), "b\n").unwrap();
    let dest = temp.0.join("backups");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--always-archive", "--progress", "--backup-dest"])
        .arg(&dest)
        .arg(&first)
        .arg(&second)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("progress: backing up 1/2:"), "{stderr}");
    assert!(stderr.contains("progress: backing up 2/2:"), "{stderr}");
    assert!(
        stderr.contains("progress: done, backed up 2, failed 0 ("),
        "{stderr}"
    );
}

#[test]
fn init_writes_starter_config() {
    let temp = TestDir::new();
    let dir = temp.0.join("cfg-home");
    fs::create_dir_all(&dir).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["init", "--dir"])
        .arg(&dir)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let path = dir.join(".proj-janitor.toml");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        stdout.contains(&format!("Wrote {}", path.display())),
        "{stdout}"
    );
    let text = fs::read_to_string(&path).unwrap();
    assert!(text.contains("[backup]"), "{text}");
    assert!(text.contains("[clean]"), "{text}");
    assert!(text.contains("[scan]"), "{text}");
    assert!(text.contains("[restore]"), "{text}");
    assert!(text.contains("Generated by `projjan init`"), "{text}");

    let again = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["init", "--dir"])
        .arg(&dir)
        .output()
        .unwrap();
    assert!(!again.status.success());
    let again_err = String::from_utf8_lossy(&again.stderr);
    assert!(again_err.contains("config already exists"), "{again_err}");
    assert!(again_err.contains("--force"), "{again_err}");

    let forced = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["init", "--dir"])
        .arg(&dir)
        .arg("--force")
        .output()
        .unwrap();
    assert!(
        forced.status.success(),
        "{}",
        String::from_utf8_lossy(&forced.stderr)
    );
}

#[test]
fn restore_skip_existing_leaves_target_and_restores_others() {
    let temp = TestDir::new();
    let mut records = Vec::new();
    for name in ["first", "second"] {
        let project = temp.0.join(name);
        fs::create_dir_all(&project).unwrap();
        fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
        fs::write(project.join("ok.txt"), format!("{name}\n")).unwrap();
        let dest = temp.0.join(format!("{name}-backups"));
        let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
            .args(["backup", "--always-archive", "--backup-dest"])
            .arg(&dest)
            .arg(&project)
            .output()
            .unwrap();
        assert!(
            backup.status.success(),
            "{}",
            String::from_utf8_lossy(&backup.stderr)
        );
        let project_dir = dest.join(name);
        let manifest = fs::read_dir(&project_dir)
            .unwrap()
            .map(|e| e.unwrap().path())
            .find(|p| {
                p.file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(|n| n.starts_with("manifest-") && n.ends_with(".json"))
            })
            .expect("manifest");
        let value: serde_json::Value =
            serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
        records.push(value["records"][0].clone());
    }
    let combined = temp.0.join("manifest.json");
    fs::write(
        &combined,
        serde_json::to_vec_pretty(&serde_json::json!({
            "schema_version": 2,
            "created_at": "2026-11-07T00:00:00+00:00",
            "tool": "proj-janitor",
            "version": "0.1.0",
            "include_artifacts": false,
            "always_archive": false,
            "exclude_git_metadata": false,
            "records": records,
        }))
        .unwrap(),
    )
    .unwrap();
    let out = temp.0.join("restored");
    let existing = out.join("first");
    fs::create_dir_all(&existing).unwrap();
    fs::write(existing.join("keep.txt"), "keep\n").unwrap();

    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--dest"])
        .arg(&out)
        .args(["--skip-existing", "--json"])
        .arg(&combined)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let payload: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    let projects = payload["projects"].as_array().unwrap();
    let first = projects.iter().find(|p| p["project"] == "first").unwrap();
    let second = projects.iter().find(|p| p["project"] == "second").unwrap();
    assert_eq!(first["skipped"], true);
    assert_eq!(second["skipped"], false);
    assert_eq!(
        fs::read_to_string(existing.join("keep.txt")).unwrap(),
        "keep\n"
    );
    assert!(out.join("second/ok.txt").is_file());

    let refused = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--dest"])
        .arg(&out)
        .args(["--force", "--skip-existing"])
        .arg(&combined)
        .output()
        .unwrap();
    assert!(!refused.status.success());
    let err = String::from_utf8_lossy(&refused.stderr);
    assert!(err.contains("cannot be combined"), "{err}");
}

#[test]
fn restore_continue_on_error_restores_remaining_projects() {
    let temp = TestDir::new();
    let mut records = Vec::new();
    for name in ["first", "second", "third"] {
        let project = temp.0.join(name);
        fs::create_dir_all(&project).unwrap();
        fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();
        fs::write(project.join("ok.txt"), format!("{name}\n")).unwrap();
        let dest = temp.0.join(format!("{name}-backups"));
        let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
            .args(["backup", "--always-archive", "--backup-dest"])
            .arg(&dest)
            .arg(&project)
            .output()
            .unwrap();
        assert!(
            backup.status.success(),
            "{}",
            String::from_utf8_lossy(&backup.stderr)
        );
        let project_dir = dest.join(name);
        let manifest = fs::read_dir(&project_dir)
            .unwrap()
            .map(|e| e.unwrap().path())
            .find(|p| {
                p.file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(|n| n.starts_with("manifest-") && n.ends_with(".json"))
            })
            .expect("manifest");
        let value: serde_json::Value =
            serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
        records.push(value["records"][0].clone());
    }
    let archive = PathBuf::from(records[1]["archive"].as_str().unwrap());
    fs::write(&archive, b"not a gzip archive").unwrap();
    let digest = {
        let mut hasher = Sha256::new();
        hasher.update(fs::read(&archive).unwrap());
        format!("{:x}", hasher.finalize())
    };
    records[1]["sha256"] = serde_json::Value::String(digest);
    let combined = temp.0.join("manifest.json");
    fs::write(
        &combined,
        serde_json::to_vec_pretty(&serde_json::json!({
            "schema_version": 2,
            "created_at": "2026-11-07T00:00:00+00:00",
            "tool": "proj-janitor",
            "version": "0.1.0",
            "include_artifacts": false,
            "always_archive": false,
            "exclude_git_metadata": false,
            "records": records,
        }))
        .unwrap(),
    )
    .unwrap();
    let out = temp.0.join("restored");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--dest"])
        .arg(&out)
        .args(["--continue-on-error", "--json"])
        .arg(&combined)
        .output()
        .unwrap();
    assert!(!output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(!stdout.trim().is_empty(), "stdout empty; stderr={stderr}");
    let payload: serde_json::Value = serde_json::from_str(&stdout).unwrap_or_else(|e| {
        panic!("json parse {e}; stdout={stdout}; stderr={stderr}");
    });
    assert_eq!(
        payload["projects"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p["project"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["first", "third"]
    );
    assert_eq!(
        payload["failures"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| f["project"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["second"]
    );
    assert!(stderr.contains("second"), "{stderr}");
    assert!(out.join("first/ok.txt").is_file());
    assert!(!out.join("second").exists());
    assert!(out.join("third/ok.txt").is_file());
}

#[test]
fn restore_force_replaces_existing_target_with_yes() {
    let temp = TestDir::new();
    let project = temp.project();
    fs::write(project.join("ok.txt"), "fresh\n").unwrap();
    let dest = temp.0.join("backups");
    let backup = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["backup", "--always-archive", "--backup-dest"])
        .arg(&dest)
        .arg(&project)
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    let project_dir = dest.join(project.file_name().unwrap());
    let manifest = fs::read_dir(&project_dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("manifest-") && n.ends_with(".json"))
        })
        .expect("manifest");
    let out = temp.0.join("restored");
    let target = out.join(project.file_name().unwrap());
    fs::create_dir_all(&target).unwrap();
    fs::write(target.join("stale.txt"), "old\n").unwrap();

    let refused = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--dest"])
        .arg(&out)
        .arg("--force")
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(!refused.status.success());
    let refused_err = String::from_utf8_lossy(&refused.stderr);
    assert!(refused_err.contains("--yes"), "{refused_err}");
    assert!(target.join("stale.txt").is_file());

    let dry = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--dest"])
        .arg(&out)
        .args(["--force", "--yes", "--dry-run", "--json"])
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(
        dry.status.success(),
        "{}",
        String::from_utf8_lossy(&dry.stderr)
    );
    let dry_json: serde_json::Value = serde_json::from_slice(&dry.stdout).unwrap();
    assert_eq!(dry_json["projects"][0]["overwrite"], true);

    let forced = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .args(["restore", "--dest"])
        .arg(&out)
        .args(["--force", "--yes"])
        .arg(&manifest)
        .output()
        .unwrap();
    assert!(
        forced.status.success(),
        "{}",
        String::from_utf8_lossy(&forced.stderr)
    );
    assert!(!target.join("stale.txt").exists());
    assert_eq!(
        fs::read_to_string(target.join("ok.txt")).unwrap(),
        "fresh\n"
    );
}
