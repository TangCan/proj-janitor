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

#[test]
fn backup_reads_backup_dest_from_environment() {
    let temp = TestDir::new();
    let project = temp.project();
    let env_dest = temp.0.join("from-env");
    let output = dry_run(&project, &env_dest, None);
    assert!(output.contains(&env_dest.join("demo").to_string_lossy().to_string()));
}

#[test]
fn command_line_backup_dest_overrides_environment() {
    let temp = TestDir::new();
    let project = temp.project();
    let env_dest = temp.0.join("from-env");
    let cli_dest = temp.0.join("from-cli");
    let output = dry_run(&project, &env_dest, Some(&cli_dest));
    assert!(output.contains(&cli_dest.join("demo").to_string_lossy().to_string()));
    assert!(!output.contains(&env_dest.to_string_lossy().to_string()));
}
