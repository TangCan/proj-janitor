from __future__ import annotations

import json
import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from proj_janitor.config import PROJECT_MARKERS

RUST_CLI = Path(sys.argv[1]).resolve()


def run(command: list[str]) -> str:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"{command!r} failed:\n{result.stderr}")
    return result.stdout


def run_rejected(command: list[str], reason: str) -> None:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode == 0 or reason not in result.stderr:
        raise SystemExit(
            f"{command!r} should fail with {reason!r}:\n{result.stderr}"
        )


def assert_restored(path: Path) -> None:
    assert (path / "src/main.py").read_text(encoding="utf-8") == "print('interop')\n"
    assert (path / "src/current.py").is_symlink()
    assert (path / "src/current.py").read_text(encoding="utf-8") == "print('interop')\n"
    config_link = path / "src/project-config"
    assert config_link.is_symlink()
    assert config_link.readlink() == Path("../pyproject.toml")
    assert not (path / "node_modules").exists()
    assert not (path / "coverage").exists()
    assert (path / "src/coverage/keep.txt").is_file()


with tempfile.TemporaryDirectory(prefix="projjan-interop-") as temporary:
    root = Path(temporary)
    python_rules = run([sys.executable, "-m", "proj_janitor", "clean", "--list-rules"])
    rust_rules = run([str(RUST_CLI), "clean", "--list-rules"])
    assert python_rules == rust_rules, "Python and Rust cleanup rules differ"

    project = root / "demo"
    (project / "src").mkdir(parents=True)
    (project / ".git").mkdir()
    for marker in PROJECT_MARKERS:
        if marker != ".git":
            (project / marker).write_text("", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (project / "src/main.py").write_text("print('interop')\n", encoding="utf-8")
    (project / "src/current.py").symlink_to("main.py")
    (project / "src/project-config").symlink_to("../pyproject.toml")
    (project / "node_modules/pkg").mkdir(parents=True)
    (project / "node_modules/pkg/cache.js").write_text("artifact\n", encoding="utf-8")
    (project / "coverage").mkdir()
    (project / "coverage/lcov.info").write_text("coverage artifact\n", encoding="utf-8")
    (project / "src/coverage").mkdir()
    (project / "src/coverage/keep.txt").write_text("keep\n", encoding="utf-8")

    python_scan = json.loads(
        run([sys.executable, "-m", "proj_janitor", "scan", str(project), "--json"])
    )[0]
    rust_scan = json.loads(run([str(RUST_CLI), "scan", str(project), "--json"]))[0]
    expected_markers = list(PROJECT_MARKERS)
    assert python_scan["markers"] == expected_markers, "Python project markers differ"
    assert rust_scan["markers"] == expected_markers, "Rust project markers differ"
    assert python_scan["size_bytes"] == rust_scan["size_bytes"], "project sizes differ"

    python_backups = root / "python-backups"
    run([
        sys.executable, "-m", "proj_janitor", "backup", str(project),
        "--backup-dest", str(python_backups), "--always-archive",
    ])
    python_manifest = next((python_backups / "demo").glob("manifest-*.json"))
    python_verified = json.loads(
        run([sys.executable, "-m", "proj_janitor", "verify", str(python_manifest), "--json"])
    )
    rust_verified = json.loads(
        run([str(RUST_CLI), "verify", str(python_manifest), "--json"])
    )
    assert python_verified == rust_verified, "Python and Rust verify results differ"
    rust_destination = root / "rust-restored"
    run([str(RUST_CLI), "restore", str(python_manifest), "--dest", str(rust_destination)])
    assert_restored(rust_destination / "demo")

    rust_backups = root / "rust-backups"
    run([
        str(RUST_CLI), "backup", str(project), "--backup-dest", str(rust_backups),
        "--always-archive",
    ])
    rust_manifest = next((rust_backups / "demo").glob("manifest-*.json"))
    python_verified = json.loads(
        run([sys.executable, "-m", "proj_janitor", "verify", str(rust_manifest), "--json"])
    )
    rust_verified = json.loads(
        run([str(RUST_CLI), "verify", str(rust_manifest), "--json"])
    )
    assert python_verified == rust_verified, "Python and Rust verify results differ"
    python_destination = root / "python-restored"
    run([
        sys.executable, "-m", "proj_janitor", "restore", str(rust_manifest),
        "--dest", str(python_destination),
    ])
    assert_restored(python_destination / "demo")

    directory_only_project = root / "directory-only"
    (directory_only_project / ".git").mkdir(parents=True)
    for producer, consumer, suffix in (
        ([sys.executable, "-m", "proj_janitor"], [str(RUST_CLI)], "python-to-rust"),
        ([str(RUST_CLI)], [sys.executable, "-m", "proj_janitor"], "rust-to-python"),
    ):
        backup_dest = root / f"{suffix}-backups"
        run([
            *producer, "backup", str(directory_only_project),
            "--backup-dest", str(backup_dest), "--always-archive",
        ])
        manifest = next((backup_dest / "directory-only").glob("manifest-*.json"))
        destination = root / f"{suffix}-restored"
        run([*consumer, "restore", str(manifest), "--dest", str(destination)])
        restored_project = destination / "directory-only"
        assert (restored_project / ".git").is_dir()
        assert not any(path.is_file() for path in restored_project.rglob("*"))

    malicious_archive = root / "windows-traversal.tar.gz"
    with tarfile.open(malicious_archive, "w:gz") as archive:
        member = tarfile.TarInfo(r"demo/src\..\..\escape.txt")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    digest = hashlib.sha256(malicious_archive.read_bytes()).hexdigest()
    malicious_manifest = root / "malicious.json"
    malicious_manifest.write_text(
        json.dumps({
            "schema_version": 2,
            "created_at": "2026-10-26T00:00:00+00:00",
            "tool": "proj-janitor",
            "version": "0.2.2",
            "include_artifacts": False,
            "always_archive": True,
            "records": [{
                "project": "demo",
                "source": str(project),
                "archive": str(malicious_archive),
                "archive_bytes": malicious_archive.stat().st_size,
                "source_bytes": 0,
                "sha256": digest,
                "strategy": "archive",
            }],
        }),
        encoding="utf-8",
    )
    for tool, command in (
        ("Python", [sys.executable, "-m", "proj_janitor"]),
        ("Rust", [str(RUST_CLI)]),
    ):
        destination = root / f"{tool.lower()}-malicious-restore"
        run_rejected(
            [*command, "restore", str(malicious_manifest), "--dest", str(destination)],
            "path",
        )
        assert not (destination / "demo").exists()
    assert not (root / "escape.txt").exists()

print("Python and Rust schema v2 archive/restore compatibility passed.")
