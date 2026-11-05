from __future__ import annotations

from pathlib import Path

PROJECT_MARKERS: tuple[str, ...] = (
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
)

ARTIFACT_RULES: dict[str, tuple[str, ...]] = {
    "python-cache": (
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "*.pyc",
        ".coverage",
        "htmlcov",
    ),
    "python-env": (
        ".venv",
        "venv",
        ".tox",
        ".nox",
        "*.egg-info",
    ),
    "node": (
        "node_modules",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".parcel-cache",
        "coverage",
    ),
    "build": (
        "build",
        "dist",
        "dist-standalone",
        "out",
        "target",
        "CMakeFiles",
        ".gradle",
    ),
    "editor": (
        ".cache",
        ".DS_Store",
        "Thumbs.db",
    ),
}

ALL_RULES: tuple[str, ...] = tuple(ARTIFACT_RULES)

COVERAGE_DIR = "coverage"
COVERAGE_MARKERS: tuple[str, ...] = (
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
)


def is_coverage_artifact(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    return any((path / marker).exists() for marker in COVERAGE_MARKERS)


def artifact_patterns(include_artifacts: bool = False) -> tuple[str, ...]:
    if include_artifacts:
        return ()
    patterns: list[str] = []
    for names in ARTIFACT_RULES.values():
        patterns.extend(names)
    return tuple(dict.fromkeys(patterns))


# --- User configuration file (.proj-janitor.toml) -----------------------------
#
# Provides defaults for command options. Command-line flags take precedence over
# environment variables, which take precedence over this file.

try:
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]

CONFIG_NAME = ".proj-janitor.toml"


class ConfigError(ValueError):
    """Raised when a configuration file is missing, malformed, or invalid."""


class BackupConfig:
    __slots__ = ("dest", "keep")

    def __init__(self, dest: Path | None = None, keep: int | None = None) -> None:
        self.dest = dest
        self.keep = keep


class PruneConfig:
    __slots__ = ("keep",)

    def __init__(self, keep: int | None = None) -> None:
        self.keep = keep


class Config:
    __slots__ = ("backup", "prune")

    def __init__(self, backup: BackupConfig, prune: PruneConfig) -> None:
        self.backup = backup
        self.prune = prune


def _parse_path(value: object, where: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"config {where} must be a non-empty string path")
    return Path(value)


def _parse_keep(value: object, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"config {where} must be a positive integer")
    return value


def _load_toml(path: Path) -> dict:
    if _toml is None:
        raise ConfigError(
            f"config file {path} requires Python 3.11+ (tomllib) or the 'tomli' "
            "package"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        data = _toml.loads(text)
    except ValueError as exc:
        raise ConfigError(f"invalid config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"invalid config {path}: expected a table")
    return data


def load_config(path: Path | None = None) -> Config | None:
    """Load a `.proj-janitor.toml` config from `path`, or discover one by
    walking up from the current directory. Returns `None` when no config file
    is present and no explicit path was given."""
    if path is None:
        current = Path.cwd()
        for directory in (current, *current.parents):
            candidate = directory / CONFIG_NAME
            if candidate.is_file():
                path = candidate
                break
        else:
            return None
    else:
        path = path.expanduser()
        if not path.is_file():
            raise ConfigError(f"cannot read config {path}: no such file")

    data = _load_toml(path)
    if set(data) - {"backup", "prune"}:
        raise ConfigError(
            f"invalid config {path}: unknown top-level key(s) "
            f"{sorted(set(data) - {'backup', 'prune'})}"
        )

    backup_data = data.get("backup", {})
    if not isinstance(backup_data, dict):
        raise ConfigError(f"invalid config {path}: [backup] must be a table")
    if set(backup_data) - {"dest", "keep"}:
        raise ConfigError(
            f"invalid config {path}: unknown [backup] key(s) "
            f"{sorted(set(backup_data) - {'dest', 'keep'})}"
        )
    backup = BackupConfig(
        dest=_parse_path(backup_data["dest"], "[backup].dest")
        if "dest" in backup_data
        else None,
        keep=_parse_keep(backup_data["keep"], "[backup].keep")
        if "keep" in backup_data
        else None,
    )

    prune_data = data.get("prune", {})
    if not isinstance(prune_data, dict):
        raise ConfigError(f"invalid config {path}: [prune] must be a table")
    if set(prune_data) - {"keep"}:
        raise ConfigError(
            f"invalid config {path}: unknown [prune] key(s) "
            f"{sorted(set(prune_data) - {'keep'})}"
        )
    prune = PruneConfig(
        keep=_parse_keep(prune_data["keep"], "[prune].keep")
        if "keep" in prune_data
        else None
    )

    return Config(backup=backup, prune=prune)
