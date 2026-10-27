use sha2::{Digest, Sha256};
use std::{
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
    let backup_dest = temp.0.join("backups");
    let output = Command::new(env!("CARGO_BIN_EXE_projjan"))
        .arg("backup")
        .arg(&project)
        .arg("--backup-dest")
        .arg(&backup_dest)
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
    assert_eq!(
        fs::read_dir(&project_backup).unwrap().count(),
        2,
        "expected one archive and one manifest"
    );
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
