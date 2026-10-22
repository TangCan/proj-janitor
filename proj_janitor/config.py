from __future__ import annotations

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


def artifact_patterns(include_artifacts: bool = False) -> tuple[str, ...]:
    if include_artifacts:
        return ()
    patterns: list[str] = []
    for names in ARTIFACT_RULES.values():
        patterns.extend(names)
    return tuple(dict.fromkeys(patterns))
