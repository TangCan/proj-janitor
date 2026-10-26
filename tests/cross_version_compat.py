from __future__ import annotations

import json
import subprocess
import sys
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
    rust_destination = root / "rust-restored"
    run([str(RUST_CLI), "restore", str(python_manifest), "--dest", str(rust_destination)])
    assert_restored(rust_destination / "demo")

    rust_backups = root / "rust-backups"
    run([
        str(RUST_CLI), "backup", str(project), "--backup-dest", str(rust_backups),
        "--always-archive",
    ])
    rust_manifest = next((rust_backups / "demo").glob("manifest-*.json"))
    python_destination = root / "python-restored"
    run([
        sys.executable, "-m", "proj_janitor", "restore", str(rust_manifest),
        "--dest", str(python_destination),
    ])
    assert_restored(python_destination / "demo")

print("Python and Rust schema v2 archive/restore compatibility passed.")
