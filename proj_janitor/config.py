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
    __slots__ = (
        "dest",
        "keep",
        "exclude",
        "incremental",
        "fail_on_secrets",
        "exclude_secrets",
        "progress",
    )

    def __init__(
        self,
        dest: Path | None = None,
        keep: int | None = None,
        exclude: tuple[str, ...] = (),
        incremental: bool = False,
        fail_on_secrets: bool = False,
        exclude_secrets: bool = False,
        progress: bool = False,
    ) -> None:
        self.dest = dest
        self.keep = keep
        self.exclude = exclude
        self.incremental = incremental
        self.fail_on_secrets = fail_on_secrets
        self.exclude_secrets = exclude_secrets
        self.progress = progress


class PruneConfig:
    __slots__ = ("keep",)

    def __init__(self, keep: int | None = None) -> None:
        self.keep = keep


class VerifyConfig:
    __slots__ = ("check_remote",)

    def __init__(self, check_remote: bool = False) -> None:
        self.check_remote = check_remote


class RestoreConfig:
    __slots__ = ("check_remote", "require_dest")

    def __init__(
        self, check_remote: bool = False, require_dest: bool = False
    ) -> None:
        self.check_remote = check_remote
        self.require_dest = require_dest


class Config:
    __slots__ = ("backup", "prune", "verify", "restore")

    def __init__(
        self,
        backup: BackupConfig,
        prune: PruneConfig,
        verify: VerifyConfig | None = None,
        restore: RestoreConfig | None = None,
    ) -> None:
        self.backup = backup
        self.prune = prune
        self.verify = verify if verify is not None else VerifyConfig()
        self.restore = restore if restore is not None else RestoreConfig()


def resolve_tri_state(flag: bool | None, config_default: bool) -> bool:
    """Resolve an optional CLI `--flag` / `--no-flag` pair against a config default.

    ``None`` means neither flag was passed; otherwise the explicit flag wins.
    """
    if flag is None:
        return config_default
    return bool(flag)


def resolve_check_remote(
    check_remote: bool | None, config_default: bool
) -> bool:
    """Resolve CLI `--check-remote` / `--no-check-remote` against a config default.

    ``None`` means neither flag was passed; otherwise the explicit flag wins.
    """
    return resolve_tri_state(check_remote, config_default)


def _parse_path(value: object, where: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"config {where} must be a non-empty string path")
    return Path(value)


def _parse_keep(value: object, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"config {where} must be a positive integer")
    return value


def _parse_bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"config {where} must be a boolean")
    return value


def _parse_exclude(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"config {where} must be a list of strings")
    normalized: list[str] = []
    for item in value:
        pattern = item.strip().lstrip("./").rstrip("/")
        if not pattern:
            raise ConfigError(f"config {where} entries must not be empty")
        if "{" in pattern or "}" in pattern:
            raise ConfigError(
                f"config {where} brace expansion is not supported: {item}"
            )
        normalized.append(pattern)
    return tuple(normalized)


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
    allowed_top = {"backup", "prune", "verify", "restore"}
    if set(data) - allowed_top:
        raise ConfigError(
            f"invalid config {path}: unknown top-level key(s) "
            f"{sorted(set(data) - allowed_top)}"
        )

    backup_data = data.get("backup", {})
    if not isinstance(backup_data, dict):
        raise ConfigError(f"invalid config {path}: [backup] must be a table")
    allowed_backup = {
        "dest",
        "keep",
        "exclude",
        "incremental",
        "fail_on_secrets",
        "exclude_secrets",
        "progress",
    }
    if set(backup_data) - allowed_backup:
        raise ConfigError(
            f"invalid config {path}: unknown [backup] key(s) "
            f"{sorted(set(backup_data) - allowed_backup)}"
        )
    backup = BackupConfig(
        dest=_parse_path(backup_data["dest"], "[backup].dest")
        if "dest" in backup_data
        else None,
        keep=_parse_keep(backup_data["keep"], "[backup].keep")
        if "keep" in backup_data
        else None,
        exclude=_parse_exclude(backup_data["exclude"], "[backup].exclude")
        if "exclude" in backup_data
        else (),
        incremental=_parse_bool(backup_data["incremental"], "[backup].incremental")
        if "incremental" in backup_data
        else False,
        fail_on_secrets=_parse_bool(
            backup_data["fail_on_secrets"], "[backup].fail_on_secrets"
        )
        if "fail_on_secrets" in backup_data
        else False,
        exclude_secrets=_parse_bool(
            backup_data["exclude_secrets"], "[backup].exclude_secrets"
        )
        if "exclude_secrets" in backup_data
        else False,
        progress=_parse_bool(backup_data["progress"], "[backup].progress")
        if "progress" in backup_data
        else False,
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

    verify_data = data.get("verify", {})
    if not isinstance(verify_data, dict):
        raise ConfigError(f"invalid config {path}: [verify] must be a table")
    if set(verify_data) - {"check_remote"}:
        raise ConfigError(
            f"invalid config {path}: unknown [verify] key(s) "
            f"{sorted(set(verify_data) - {'check_remote'})}"
        )
    verify = VerifyConfig(
        check_remote=_parse_bool(verify_data["check_remote"], "[verify].check_remote")
        if "check_remote" in verify_data
        else False,
    )

    restore_data = data.get("restore", {})
    if not isinstance(restore_data, dict):
        raise ConfigError(f"invalid config {path}: [restore] must be a table")
    allowed_restore = {"check_remote", "require_dest"}
    if set(restore_data) - allowed_restore:
        raise ConfigError(
            f"invalid config {path}: unknown [restore] key(s) "
            f"{sorted(set(restore_data) - allowed_restore)}"
        )
    restore = RestoreConfig(
        check_remote=_parse_bool(
            restore_data["check_remote"], "[restore].check_remote"
        )
        if "check_remote" in restore_data
        else False,
        require_dest=_parse_bool(
            restore_data["require_dest"], "[restore].require_dest"
        )
        if "require_dest" in restore_data
        else False,
    )

    return Config(backup=backup, prune=prune, verify=verify, restore=restore)
