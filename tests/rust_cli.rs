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
    assert_eq!(empty_git_project["size_bytes"], 0);
    assert!(empty_git_project["last_modified"].is_null());
    let demo = projects
        .iter()
        .find(|project| project["name"] == "demo")
        .unwrap();
    let actual_modified =
        chrono::DateTime::parse_from_rfc3339(demo["last_modified"].as_str().unwrap())
            .unwrap()
            .timestamp();
    assert_eq!(actual_modified, 1_700_000_000);

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
