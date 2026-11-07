"""Heuristic detection of common secret filenames that may land in archives.

These checks are path/name matchers only. They do not inspect file contents.
By default they surface warnings so operators can exclude, redact, or encrypt
before long-term storage (R-05). With ``--fail-on-secrets`` /
``[backup].fail_on_secrets`` they refuse the archive backup; with
``--exclude-secrets`` / ``[backup].exclude_secrets`` matching paths are
omitted from the archive.
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from proj_janitor.config import artifact_patterns

_MAX_REPORTED = 12

_EXACT_NAMES = {
    ".env",
    ".netrc",
    ".htpasswd",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_ecdsa_sk",
    "id_ed25519_sk",
}
_EXACT_LOWER = {
    "credentials.json",
    "credentials.csv",
    "service-account.json",
    "serviceaccount.json",
}
_ENV_EXAMPLE_SUFFIXES = {"example", "sample", "template", "dist", "default"}
_EXTENSIONS = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".tfstate",
)


def path_looks_like_secret(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    if rel == ".aws/credentials" or rel.endswith("/.aws/credentials"):
        return True
    name = rel.rsplit("/", 1)[-1]
    lower = name.lower()
    if name in _EXACT_NAMES or lower in _EXACT_LOWER:
        return True
    if name.startswith(".env."):
        rest = name[5:].lower()
        if rest not in _ENV_EXAMPLE_SUFFIXES:
            return True
    if any(lower.endswith(ext) for ext in _EXTENSIONS):
        return True
    return lower.endswith(".tfstate.backup")


def find_possible_secret_paths(
    project: Path,
    *,
    include_artifacts: bool = False,
    exclude_git_metadata: bool = False,
    exclude_patterns: tuple[str, ...] = (),
    exclude_secrets: bool = False,
) -> list[str]:
    """Return sorted project-relative paths that look like common secrets.

    When ``exclude_secrets`` is true, matching paths are treated as already
    omitted and are not returned (same filter used when packing archives).
    """
    root = project.expanduser()
    if root.exists():
        root = root.resolve()
    patterns = artifact_patterns(include_artifacts)
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        parts = (
            ()
            if rel_dir == "."
            else tuple(PurePosixPath(rel_dir.replace(os.sep, "/")).parts)
        )
        kept: list[str] = []
        for dirname in dirnames:
            child = parts + (dirname,)
            if _component_excluded(
                child, patterns, exclude_git_metadata, exclude_patterns, exclude_secrets
            ):
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for filename in filenames:
            child = parts + (filename,)
            if _component_excluded(
                child, patterns, exclude_git_metadata, exclude_patterns, exclude_secrets
            ):
                continue
            rel = "/".join(child)
            if path_looks_like_secret(rel):
                hits.append(rel)
    return sorted(set(hits))


def _format_secret_list(secrets: list[str]) -> str:
    shown = secrets[:_MAX_REPORTED]
    listing = ", ".join(shown)
    if len(secrets) > _MAX_REPORTED:
        listing += f" (+{len(secrets) - _MAX_REPORTED} more)"
    return listing


def format_secret_warning(project: str, secrets: list[str]) -> str:
    return (
        f"warning: {project}: archive may include secrets "
        f"({_format_secret_list(secrets)}); archives are not encrypted"
    )


def format_secret_refusal_detail(secrets: list[str]) -> str:
    """Body for error paths that already prefix ``error: {project}:``."""
    return (
        f"archive would include secrets ({_format_secret_list(secrets)}); "
        "refusing backup (--fail-on-secrets)"
    )


def format_secret_refusal(project: str, secrets: list[str]) -> str:
    return f"error: {project}: {format_secret_refusal_detail(secrets)}"


def format_secret_exclusion(project: str, secrets: list[str]) -> str:
    return (
        f"note: {project}: excluding secret paths from archive "
        f"({_format_secret_list(secrets)})"
    )


class SecretPathsError(RuntimeError):
    """Raised when archive backup is refused because secret paths were found."""


def _component_excluded(
    parts: tuple[str, ...],
    patterns: tuple[str, ...],
    exclude_git_metadata: bool,
    exclude_patterns: tuple[str, ...],
    exclude_secrets: bool = False,
) -> bool:
    if exclude_git_metadata and ".git" in parts:
        return True
    if exclude_secrets and path_looks_like_secret("/".join(parts)):
        return True
    for part in parts:
        for pattern in exclude_patterns:
            if fnmatch(part, pattern):
                return True
        for pattern in patterns:
            if fnmatch(part, pattern):
                return True
    return False
