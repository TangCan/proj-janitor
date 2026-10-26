from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUST_CLI = Path(sys.argv[1]).resolve()


def run(command: list[str]) -> None:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"{command!r} failed:\n{result.stderr}")


def assert_restored(path: Path) -> None:
    assert (path / "src/main.py").read_text(encoding="utf-8") == "print('interop')\n"
    assert (path / "src/current.py").is_symlink()
    assert (path / "src/current.py").read_text(encoding="utf-8") == "print('interop')\n"
    config_link = path / "src/project-config"
    assert config_link.is_symlink()
    assert config_link.readlink() == Path("../pyproject.toml")


with tempfile.TemporaryDirectory(prefix="projjan-interop-") as temporary:
    root = Path(temporary)
    project = root / "demo"
    (project / "src").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (project / "src/main.py").write_text("print('interop')\n", encoding="utf-8")
    (project / "src/current.py").symlink_to("main.py")
    (project / "src/project-config").symlink_to("../pyproject.toml")

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
