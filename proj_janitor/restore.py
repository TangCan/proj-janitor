from __future__ import annotations

import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from proj_janitor.backup import (
    _portable_path_key,
    format_unsafe_archive_path,
    register_archive_path,
    safe_archive_link,
    safe_archive_member,
    sha256_file,
    sha256_stream,
)
from proj_janitor.models import RestorePlan, RestoreResult, format_elapsed
from proj_janitor.scanner import is_supported_submodule_url, submodule_config

_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,64}\Z")
_GIT_TIMEOUT_SECONDS = 600
_REMOTE_PROBE_TIMEOUT_SECONDS = 30


class RestoreError(RuntimeError):
    pass


class GitTimeoutError(RestoreError):
    pass


class PartialRestoreError(RestoreError):
    """Raised when ``continue_on_error`` kept going after one or more project failures."""

    def __init__(
        self,
        results: list[RestoreResult],
        failures: list[tuple[str, str]],
    ) -> None:
        self.results = results
        self.failures = failures
        detail = "; ".join(f"{project}: {error}" for project, error in failures)
        super().__init__(f"one or more restore operations failed ({detail})")


def restore_from_manifest(
    manifest_path: Path,
    dest: Path | None,
    projects: Iterable[str] = (),
    dry_run: bool = False,
    verify: bool = True,
    check_remote: bool = False,
    force: bool = False,
    continue_on_error: bool = False,
    skip_existing: bool = False,
    progress: bool = False,
) -> tuple[list[RestorePlan], list[RestoreResult]]:
    plans = plan_restore(
        manifest_path,
        dest,
        projects=projects,
        verify=verify if dry_run else False,
        force=force,
        skip_existing=skip_existing,
    )
    if dry_run:
        return plans, []

    results = execute_restore(
        plans,
        verify=verify,
        check_remote=check_remote,
        force=force,
        continue_on_error=continue_on_error,
        progress=progress,
    )
    if verify:
        plans = [
            replace(plan, hash_verified=True)
            if plan.strategy in ("archive", "git_bundle")
            else plan
            for plan in plans
        ]
    return plans, results


def execute_restore(
    plans: Iterable[RestorePlan],
    *,
    verify: bool = True,
    check_remote: bool = False,
    decrypt_passphrase_file: Path | None = None,
    force: bool = False,
    continue_on_error: bool = False,
    progress: bool = False,
) -> list[RestoreResult]:
    plan_list = list(plans)
    if verify:
        for plan in plan_list:
            if plan.skip:
                continue
            if plan.strategy in ("archive", "git_bundle"):
                if plan.archive is None:
                    raise RestoreError(
                        f"incomplete archive restore plan for {plan.project}"
                    )
                _verify_archive_hash(
                    plan.archive,
                    plan.project,
                    plan.sha256,
                    progress_label=plan.project if progress else None,
                )
    if check_remote:
        git_plans = [
            plan for plan in plan_list if not plan.skip and plan.strategy == "git"
        ]
        probe_total = len(git_plans)
        for probe_index, plan in enumerate(git_plans, start=1):
            if progress:
                print(
                    f"progress: probing remote {probe_index}/{probe_total}: "
                    f"{plan.project}",
                    file=sys.stderr,
                )
            if plan.remote_url is None or plan.commit is None:
                raise RestoreError(f"incomplete git restore plan for {plan.project}")
            status, reason, _refs = _probe_remote(plan.remote_url, plan.commit)
            if status == "remote-unreachable":
                raise RestoreError(
                    f"cannot restore {plan.project}: remote "
                    f"{redact_remote_url(plan.remote_url)} is unreachable ({reason})"
                )
            raw_submodules = [
                submodule
                for submodule in (plan.submodules or [])
                if isinstance(submodule, dict)
                and isinstance(submodule.get("url"), str)
                and isinstance(submodule.get("commit"), str)
            ]
            if progress and raw_submodules:
                print(
                    f"progress: {plan.project}: probing {len(raw_submodules)} "
                    "submodule(s)",
                    file=sys.stderr,
                )
            for submodule in raw_submodules:
                sub_url = submodule["url"]
                sub_commit = submodule["commit"]
                sub_path = submodule.get("path", "")
                assert isinstance(sub_url, str) and isinstance(sub_commit, str)
                sub_status, sub_reason, _ = _probe_remote(sub_url, sub_commit)
                if sub_status == "remote-unreachable":
                    raise RestoreError(
                        f"cannot restore {plan.project}: submodule {sub_path} remote "
                        f"{redact_remote_url(sub_url)} is unreachable ({sub_reason})"
                    )
            lfs_objects = plan.lfs_objects or []
            if progress and lfs_objects:
                print(
                    f"progress: {plan.project}: probing {len(lfs_objects)} "
                    "LFS object(s)",
                    file=sys.stderr,
                )
            for lfs_check in _probe_lfs_objects(plan.remote_url, lfs_objects):
                if lfs_check["status"] in ("lfs-missing", "lfs-unreachable"):
                    oid = str(lfs_check["oid"])
                    raise RestoreError(
                        f"cannot restore {plan.project}: LFS object {oid[:12]} is "
                        f"{lfs_check['status']} ({lfs_check.get('reason') or 'unavailable'})"
                    )
    try:
        for plan in plan_list:
            plan.target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RestoreError(
            f"cannot create restore target directory: {exc}"
        ) from exc
    results: list[RestoreResult] = []
    failures: list[tuple[str, str]] = []
    total = len(plan_list)
    progress_started = time.monotonic() if progress else None
    for index, plan in enumerate(plan_list, start=1):
        if plan.skip:
            if progress:
                print(
                    f"progress: skipped {index}/{total}: {plan.project} "
                    f"({plan.strategy}, existing)",
                    file=sys.stderr,
                )
            results.append(
                RestoreResult(
                    project=plan.project,
                    strategy=plan.strategy,
                    target=plan.target,
                    skipped=True,
                )
            )
            continue
        if progress:
            print(
                f"progress: restoring {index}/{total}: {plan.project} "
                f"({plan.strategy})",
                file=sys.stderr,
            )
        try:
            results.append(
                _restore_one(
                    plan,
                    verify=verify,
                    decrypt_passphrase_file=decrypt_passphrase_file,
                    force=force,
                    progress_label=plan.project if progress else None,
                )
            )
        except (OSError, RestoreError) as exc:
            message = f"cannot restore {plan.project}: {exc}"
            if continue_on_error:
                # Emit immediately (CR-132), matching Rust restore --continue-on-error.
                print(f"error: {plan.project}: {exc}", file=sys.stderr)
                if progress:
                    print(
                        f"progress: failed {index}/{total}: {plan.project}",
                        file=sys.stderr,
                    )
                failures.append((plan.project, str(exc)))
                continue
            completed = ", ".join(
                f"{result.project} -> {result.target}" for result in results
            )
            suffix = f"; already restored: {completed}" if completed else ""
            raise RestoreError(f"{message}{suffix}") from exc
    if progress:
        restored = sum(1 for result in results if not result.skipped)
        skipped = sum(1 for result in results if result.skipped)
        elapsed = format_elapsed(
            0.0
            if progress_started is None
            else time.monotonic() - progress_started
        )
        print(
            f"progress: done, restored {restored}, skipped {skipped}, "
            f"failed {len(failures)} ({elapsed})",
            file=sys.stderr,
        )
    if failures:
        raise PartialRestoreError(results, failures)
    return results


def plan_restore(
    manifest_path: Path,
    dest: Path | None,
    projects: Iterable[str] = (),
    verify: bool = True,
    force: bool = False,
    skip_existing: bool = False,
) -> list[RestorePlan]:
    manifest_path, manifest = load_manifest(manifest_path)
    if dest is not None:
        dest = dest.expanduser().resolve()
        if dest.exists() and not dest.is_dir():
            raise RestoreError(f"destination is not a directory: {dest}")

    selected = _select_records(manifest["records"], projects)

    plans: list[RestorePlan] = []
    seen: set[str] = set()
    seen_targets: set[Path] = set()
    seen_portable_targets: set[str] = set()
    for record in selected:
        project = _record_project(record)
        if project in seen:
            raise RestoreError(f"duplicate project in manifest: {project}")
        seen.add(project)

        if dest is None:
            target = _record_source(record, project)
        else:
            target = dest / project
        if target in seen_targets:
            raise RestoreError(f"duplicate restore target in manifest: {target}")
        seen_targets.add(target)
        portable_target = _portable_path_key(str(target))
        if portable_target in seen_portable_targets:
            raise RestoreError(
                "restore targets collide on case-insensitive filesystems "
                f"(including Unicode normalization): {target}"
            )
        seen_portable_targets.add(portable_target)
        overwrite, skip = _plan_existing(
            target, force=force, skip_existing=skip_existing
        )

        strategy = record.get("strategy")
        if strategy == "git":
            remote_url = _record_string(record, "remote_url")
            _validate_remote_url(remote_url)
            commit = _record_string(record, "commit")
            if not _COMMIT_RE.fullmatch(commit):
                raise RestoreError(
                    f"invalid commit for {project}: {commit!r}"
                )
            plans.append(
                RestorePlan(
                    project=project,
                    strategy="git",
                    target=target,
                    remote_url=remote_url,
                    commit=commit,
                    in_place=dest is None,
                    submodules=(
                        record.get("submodules")
                        if isinstance(record.get("submodules"), list)
                        else None
                    ),
                    lfs_objects=(
                        record.get("lfs_objects")
                        if isinstance(record.get("lfs_objects"), list)
                        else None
                    ),
                    overwrite=overwrite,
                    skip=skip,
                )
            )
            continue

        if strategy == "git_bundle":
            bundle = _resolve_archive(record, manifest_path.parent, project)
            sha256 = record.get("sha256")
            if not isinstance(sha256, str) or not sha256:
                raise RestoreError(f"manifest has no sha256 for {project}")
            branch = _record_string(record, "branch")
            commit = _record_string(record, "commit")
            if len(commit) not in (40, 64) or not _COMMIT_RE.fullmatch(commit):
                raise RestoreError(f"invalid commit for {project}: {commit!r}")
            if verify and not skip:
                _verify_archive_hash(bundle, project, sha256)
            plans.append(
                RestorePlan(
                    project=project,
                    strategy="git_bundle",
                    target=target,
                    archive=bundle,
                    branch=branch,
                    commit=commit,
                    sha256=sha256,
                    hash_verified=verify and not skip,
                    in_place=dest is None,
                    encryption=(
                        record.get("encryption")
                        if isinstance(record.get("encryption"), str)
                        else None
                    ),
                    overwrite=overwrite,
                    skip=skip,
                )
            )
            continue

        if strategy == "archive":
            archive = _resolve_archive(record, manifest_path.parent, project)
            sha256 = record.get("sha256")
            if sha256 is not None and not isinstance(sha256, str):
                raise RestoreError(f"invalid sha256 for {project}")
            hash_verified = False
            if verify and not skip:
                if not sha256:
                    raise RestoreError(
                        f"manifest has no sha256 for {project}; "
                        "use --no-verify to skip verification"
                    )
                _verify_archive_hash(archive, project, sha256)
                hash_verified = True
            plans.append(
                RestorePlan(
                    project=project,
                    strategy="archive",
                    target=target,
                    archive=archive,
                    sha256=sha256,
                    hash_verified=hash_verified,
                    in_place=dest is None,
                    incremental=record.get("incremental"),
                    inventory=record.get("inventory"),
                    manifest_dir=manifest_path.parent,
                    encryption=(
                        record.get("encryption")
                        if isinstance(record.get("encryption"), str)
                        else None
                    ),
                    overwrite=overwrite,
                    skip=skip,
                )
            )
            continue

        raise RestoreError(
            f"unsupported backup strategy for {project}: {strategy!r}"
        )
    return plans


def _plan_existing(
    target: Path, *, force: bool, skip_existing: bool
) -> tuple[bool, bool]:
    """Validate an existing restore target.

    Returns ``(overwrite, skip)``. ``force`` and ``skip_existing`` must not both
    be true; callers enforce that at the CLI/config layer.
    """
    if target.is_symlink():
        raise RestoreError(f"restore target is a symlink: {target}")
    if not target.exists():
        return False, False
    if not target.is_dir():
        raise RestoreError(f"restore target exists and is not a directory: {target}")
    if skip_existing:
        return False, True
    if not force:
        raise RestoreError(f"restore target already exists: {target}")
    return True, False


def _publish_restored_tree(staged: Path, target: Path, *, force: bool) -> None:
    """Move a staged restore tree into place, optionally replacing an existing directory."""
    if target.is_symlink():
        raise RestoreError(f"restore target is a symlink: {target}")
    if target.exists():
        if not force:
            raise RestoreError(f"restore target already exists: {target}")
        if not target.is_dir():
            raise RestoreError(
                f"restore target exists and is not a directory: {target}"
            )
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise RestoreError(f"cannot replace restore target {target}: {exc}") from exc
    try:
        staged.rename(target)
    except OSError as exc:
        raise RestoreError(f"cannot publish restore target {target}: {exc}") from exc


def _verify_archive_hash(
    archive: Path,
    project: str,
    expected: str | None,
    *,
    progress_label: str | None = None,
) -> str:
    if not expected:
        raise RestoreError(f"manifest has no sha256 for {project}")
    try:
        actual = sha256_file(archive, progress_label=progress_label)
    except OSError as exc:
        raise RestoreError(f"cannot read archive {archive}: {exc}") from exc
    if actual.lower() != expected.lower():
        raise RestoreError(
            f"sha256 mismatch for {archive}: expected {expected}, got {actual}"
        )
    return actual


def _probe_remote(remote_url: str, commit: str) -> tuple[str, str | None, list[str]]:
    if redact_remote_url(remote_url) != remote_url:
        return "remote-skipped", "credentials-in-remote-url", []
    try:
        result = _run_git(
            ["ls-remote", remote_url],
            timeout=_REMOTE_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except GitTimeoutError:
        return "remote-unreachable", "ls-remote-timed-out", []
    except RestoreError:
        return "remote-unreachable", "ls-remote-failed", []
    if result.returncode != 0:
        return "remote-unreachable", "ls-remote-failed", []
    matched: set[str] = set()
    for line in result.stdout.splitlines():
        sha, separator, ref = line.partition("\t")
        if not separator or sha.strip().lower() != commit.lower():
            continue
        ref = ref.strip()
        if ref.endswith("^{}"):
            ref = ref[: -len("^{}")]
        if ref:
            matched.add(ref)
    if matched:
        return "remote-verified", None, sorted(matched)
    return "remote-moved", "no-ref-matches-commit", []


def _probe_lfs_objects(
    remote_url: str, objects: list
) -> list[dict[str, object]]:
    """Probe LFS object availability without downloading payloads."""
    if not objects:
        return []
    normalized: list[tuple[str, int | None]] = []
    for item in objects:
        if isinstance(item, str):
            normalized.append((item, None))
        elif isinstance(item, dict):
            oid = item.get("oid")
            size = item.get("size")
            if not isinstance(oid, str):
                continue
            normalized.append(
                (oid, size if isinstance(size, int) and size >= 0 else None)
            )
    if not normalized:
        return []
    if redact_remote_url(remote_url) != remote_url:
        return [
            {
                "oid": oid,
                "size": size,
                "status": "lfs-skipped",
                "reason": "credentials-in-remote-url",
            }
            for oid, size in normalized
        ]
    effective = _rewrite_remote_url(remote_url)
    store = _local_lfs_store_root(effective)
    if store is not None:
        checks: list[dict[str, object]] = []
        for oid, size in normalized:
            path = store / oid[:2] / oid[2:4] / oid
            if path.is_file():
                checks.append(
                    {
                        "oid": oid,
                        "size": size,
                        "status": "lfs-verified",
                        "reason": None,
                    }
                )
            else:
                checks.append(
                    {
                        "oid": oid,
                        "size": size,
                        "status": "lfs-missing",
                        "reason": "object-not-found",
                    }
                )
        return checks
    if effective.startswith("https://"):
        return _probe_lfs_batch(effective, normalized)
    return [
        {
            "oid": oid,
            "size": size,
            "status": "lfs-skipped",
            "reason": "unsupported-lfs-endpoint",
        }
        for oid, size in normalized
    ]


def _rewrite_remote_url(url: str) -> str:
    try:
        result = _run_git(
            ["config", "--get-regexp", r"^url\..+\.insteadof$"],
            timeout=_REMOTE_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (RestoreError, GitTimeoutError):
        return url
    if result.returncode != 0:
        return url
    best: tuple[int, str] | None = None
    for line in result.stdout.splitlines():
        key, sep, prefix = line.partition(" ")
        if not sep:
            continue
        prefix = prefix.strip()
        if not prefix or not url.startswith(prefix):
            continue
        if not key.startswith("url.") or not key.endswith(".insteadof"):
            continue
        base = key[len("url.") : -len(".insteadof")]
        rewritten = f"{base}{url[len(prefix) :]}"
        if best is None or len(prefix) > best[0]:
            best = (len(prefix), rewritten)
    return best[1] if best is not None else url


def _local_lfs_store_root(url: str) -> Path | None:
    if url.startswith("file://"):
        parsed = urlsplit(url)
        if parsed.scheme != "file":
            return None
        path = Path(parsed.path)
        # file:///C:/... on Windows keeps the drive in path; Unix is absolute.
        if os.name == "nt" and path.parts and path.parts[0] == "\\":
            path = Path(str(path)[1:]) if len(str(path)) > 1 else path
    elif url.startswith("/") or (os.name == "nt" and Path(url).is_absolute()):
        path = Path(url)
    else:
        return None
    store = path / "lfs" / "objects"
    if store.is_dir() or path.exists():
        return store
    return None


def _probe_lfs_batch(
    remote_url: str, objects: list[tuple[str, int | None]]
) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    batch: list[tuple[str, int]] = []
    for oid, size in objects:
        if size is None:
            checks.append(
                {
                    "oid": oid,
                    "size": None,
                    "status": "lfs-skipped",
                    "reason": "oid-only-no-size",
                }
            )
        else:
            batch.append((oid, size))
    if not batch:
        return checks
    endpoint = f"{remote_url.rstrip('/')}/info/lfs/objects/batch"
    body = json.dumps(
        {
            "operation": "download",
            "transfers": ["basic"],
            "objects": [{"oid": oid, "size": size} for oid, size in batch],
        }
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.git-lfs+json",
            "Content-Type": "application/vnd.git-lfs+json",
        },
    )
    try:
        with urlopen(request, timeout=_REMOTE_PROBE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        reason = {
            401: "lfs-auth-required",
            403: "lfs-auth-required",
            404: "lfs-batch-not-found",
        }.get(exc.code, "lfs-batch-failed")
        status = "lfs-missing" if exc.code == 404 else "lfs-unreachable"
        return [
            {
                "oid": oid,
                "size": size,
                "status": status,
                "reason": reason,
            }
            for oid, size in batch
        ] + checks
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [
            {
                "oid": oid,
                "size": size,
                "status": "lfs-unreachable",
                "reason": "lfs-batch-failed",
            }
            for oid, size in batch
        ] + checks
    reported = {
        entry["oid"]: entry
        for entry in payload.get("objects") or []
        if isinstance(entry, dict) and isinstance(entry.get("oid"), str)
    }
    for oid, size in batch:
        entry = reported.get(oid)
        if entry is None:
            checks.append(
                {
                    "oid": oid,
                    "size": size,
                    "status": "lfs-unreachable",
                    "reason": "lfs-batch-incomplete",
                }
            )
        elif entry.get("error") is not None:
            checks.append(
                {
                    "oid": oid,
                    "size": size,
                    "status": "lfs-missing",
                    "reason": "object-not-found",
                }
            )
        elif isinstance(entry.get("actions"), dict) and entry["actions"].get(
            "download"
        ):
            checks.append(
                {
                    "oid": oid,
                    "size": size,
                    "status": "lfs-verified",
                    "reason": None,
                }
            )
        else:
            checks.append(
                {
                    "oid": oid,
                    "size": size,
                    "status": "lfs-missing",
                    "reason": "no-download-action",
                }
            )
    return checks


def verify_manifest(
    manifest_path: Path,
    projects: Iterable[str] = (),
    check_remote: bool = False,
    progress: bool = False,
) -> list[dict[str, object]]:
    manifest_path, manifest = load_manifest(manifest_path)
    selected = _select_records(manifest["records"], projects)
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    total = len(selected)
    progress_started = time.monotonic() if progress else None
    for index, record in enumerate(selected, start=1):
        project = _record_project(record)
        if project in seen:
            raise RestoreError(f"duplicate project in manifest: {project}")
        seen.add(project)

        strategy = record.get("strategy")
        if progress:
            print(
                f"progress: verifying {index}/{total}: {project} ({strategy})",
                file=sys.stderr,
            )
        if strategy == "archive":
            archive = _resolve_archive(record, manifest_path.parent, project)
            expected = record.get("sha256")
            if not isinstance(expected, str) or not expected:
                raise RestoreError(f"manifest has no sha256 for {project}")
            try:
                actual = sha256_file(
                    archive,
                    progress_label=project if progress else None,
                )
            except OSError as exc:
                raise RestoreError(f"cannot read archive {archive}: {exc}") from exc
            if actual.lower() != expected.lower():
                raise RestoreError(
                    f"sha256 mismatch for {archive}: expected {expected}, got {actual}"
                )
            results.append(
                {
                    "project": project,
                    "strategy": "archive",
                    "status": "verified",
                    "archive": str(archive),
                    "sha256": actual,
                    "commit": None,
                }
            )
        elif strategy == "git":
            remote_url = _record_string(record, "remote_url")
            _validate_remote_url(remote_url)
            commit = _record_string(record, "commit")
            if not _COMMIT_RE.fullmatch(commit):
                raise RestoreError(f"invalid commit for {project}: {commit!r}")
            branch = record.get("branch")
            entry: dict[str, object] = {
                "project": project,
                "strategy": "git",
                "status": "metadata-only",
                "archive": None,
                "sha256": None,
                "commit": commit,
            }
            if check_remote:
                if progress:
                    print(
                        f"progress: {project}: probing remote",
                        file=sys.stderr,
                    )
                status, reason, refs = _probe_remote(remote_url, commit)
                entry["status"] = status
                entry["remote"] = {
                    "url": redact_remote_url(remote_url),
                    "branch": branch if isinstance(branch, str) and branch else None,
                    "reason": reason,
                    "refs": refs,
                }
                raw_submodules = [
                    submodule
                    for submodule in (record.get("submodules") or [])
                    if isinstance(submodule, dict)
                    and isinstance(submodule.get("url"), str)
                    and isinstance(submodule.get("commit"), str)
                ]
                if progress and raw_submodules:
                    print(
                        f"progress: {project}: probing {len(raw_submodules)} "
                        "submodule(s)",
                        file=sys.stderr,
                    )
                submodule_checks: list[dict[str, object]] = []
                for submodule in raw_submodules:
                    sub_url = submodule["url"]
                    sub_commit = submodule["commit"]
                    sub_path = submodule.get("path", "")
                    assert isinstance(sub_url, str) and isinstance(sub_commit, str)
                    sub_status, sub_reason, sub_refs = _probe_remote(
                        sub_url, sub_commit
                    )
                    submodule_checks.append(
                        {
                            "path": sub_path,
                            "url": redact_remote_url(sub_url),
                            "branch": None,
                            "reason": sub_reason,
                            "refs": sub_refs,
                            "status": sub_status,
                        }
                    )
                entry["submodules"] = submodule_checks
                if any(
                    check.get("status") == "remote-unreachable"
                    for check in submodule_checks
                ):
                    entry["status"] = "remote-unreachable"
                if not submodule_checks:
                    del entry["submodules"]
                lfs_objects = record.get("lfs_objects") or []
                if progress and lfs_objects:
                    print(
                        f"progress: {project}: probing {len(lfs_objects)} "
                        "LFS object(s)",
                        file=sys.stderr,
                    )
                lfs_checks = _probe_lfs_objects(remote_url, lfs_objects)
                if lfs_checks:
                    entry["lfs"] = lfs_checks
                    if entry["status"] != "remote-unreachable" and any(
                        check.get("status") in ("lfs-missing", "lfs-unreachable")
                        for check in lfs_checks
                    ):
                        entry["status"] = "lfs-unavailable"
            results.append(entry)
        elif strategy == "git_bundle":
            bundle = _resolve_archive(record, manifest_path.parent, project)
            expected = record.get("sha256")
            if not isinstance(expected, str) or not expected:
                raise RestoreError(f"manifest has no sha256 for {project}")
            actual = _verify_archive_hash(
                bundle,
                project,
                expected,
                progress_label=project if progress else None,
            )
            results.append(
                {
                    "project": project,
                    "strategy": "git_bundle",
                    "status": "verified",
                    "archive": str(bundle),
                    "sha256": actual,
                    "commit": _record_string(record, "commit"),
                }
            )
        else:
            raise RestoreError(
                f"unsupported backup strategy for {project}: {strategy!r}"
            )
    if progress:
        elapsed = (
            "0s"
            if progress_started is None
            else format_elapsed(time.monotonic() - progress_started)
        )
        print(
            f"progress: done, verified {len(results)} ({elapsed})",
            file=sys.stderr,
        )
    return results


def _select_records(records: list[dict], projects: Iterable[str]) -> list[dict]:
    requested = list(projects)
    if not requested:
        return records
    wanted = set(requested)
    selected = [record for record in records if _record_project(record) in wanted]
    found = {_record_project(record) for record in selected}
    missing = wanted - found
    if missing:
        names = ", ".join(sorted(missing))
        raise RestoreError(f"project not found in manifest: {names}")
    return selected


def load_manifest(path: Path) -> tuple[Path, dict]:
    manifest_path = path.expanduser()
    if not manifest_path.exists():
        raise RestoreError(f"manifest does not exist: {manifest_path}")
    if not manifest_path.is_file():
        raise RestoreError(f"manifest is not a file: {manifest_path}")
    manifest_path = manifest_path.resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreError(
            f"cannot read manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise RestoreError("manifest root must be a JSON object")
    schema_version = manifest.get("schema_version")
    if type(schema_version) is not int or schema_version != 2:
        raise RestoreError(
            "unsupported manifest schema_version: "
            f"{schema_version!r}"
        )
    records = manifest.get("records")
    if not isinstance(records, list) or not records:
        raise RestoreError("manifest records must be a non-empty array")
    if not all(isinstance(record, dict) for record in records):
        raise RestoreError("manifest records must be JSON objects")
    return manifest_path, manifest


def _record_project(record: dict) -> str:
    value = record.get("project")
    if not isinstance(value, str) or not value:
        raise RestoreError("manifest record has no project name")
    if (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or Path(value).is_absolute()
        or Path(value).name != value
        or not safe_archive_member(value, value)
    ):
        raise RestoreError(f"unsafe project name in manifest: {value!r}")
    return value


def _record_string(record: dict, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise RestoreError(f"manifest record has no {key}")
    if value != value.strip():
        raise RestoreError(f"manifest record has invalid {key}")
    return value


def _record_source(record: dict, project: str) -> Path:
    raw = _record_string(record, "source")
    if "\x00" in raw:
        raise RestoreError(f"invalid source for {project}: contains NUL")
    path = Path(raw)
    if not path.is_absolute():
        raise RestoreError(
            f"manifest source for {project} must be absolute: {raw!r}"
        )
    if any(part == ".." for part in path.parts):
        raise RestoreError(f"unsafe source path for {project}: {raw!r}")
    if path.name != project:
        raise RestoreError(
            f"source path for {project} does not match project name: {raw!r}"
        )
    if path == Path(path.anchor):
        raise RestoreError(
            f"refusing filesystem root as restore source: {raw!r}"
        )

    try:
        parent = path.parent.resolve()
    except OSError as exc:
        raise RestoreError(
            f"cannot resolve source parent for {project}: {exc}"
        ) from exc
    if parent.exists() and not parent.is_dir():
        raise RestoreError(
            f"restore source parent is not a directory: {parent}"
        )
    target = parent / path.name
    if target.is_symlink():
        raise RestoreError(f"restore target is a symlink: {target}")
    return target


def _resolve_archive(record: dict, manifest_dir: Path, project: str) -> Path:
    raw = _record_string(record, "archive")
    raw_path = Path(raw).expanduser()
    candidates = []
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.append(manifest_dir / raw_path)
    candidates.append(manifest_dir / raw_path.name)
    candidates.append(manifest_dir / project / raw_path.name)

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate.resolve()
    raise RestoreError(f"archive not found for {project}: {raw}")


def _validate_remote_url(remote: str) -> None:
    if any(character.isspace() for character in remote):
        raise RestoreError("remote URL contains whitespace")
    if remote.startswith("-"):
        raise RestoreError("remote URL must not start with '-'")
    if remote.startswith(("https://", "ssh://", "file://")):
        try:
            parsed = urlsplit(remote)
        except ValueError as exc:
            raise RestoreError("invalid remote URL") from exc
        if parsed.scheme in {"https", "ssh"} and not parsed.hostname:
            raise RestoreError("remote URL has no host")
        if parsed.scheme == "file" and (
            parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise RestoreError("file URL must not contain credentials or query data")
        return
    if re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:.+", remote):
        return
    if Path(remote).is_absolute():
        return
    raise RestoreError("unsupported remote URL")


def redact_remote_url(remote: str) -> str:
    if remote.startswith(("http://", "https://", "ssh://")):
        try:
            parsed = urlsplit(remote)
            if (
                parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                host = parsed.netloc.rsplit("@", 1)[-1]
                if parsed.scheme == "ssh" and parsed.username is not None:
                    host = f"{parsed.username}@{host}"
                return urlunsplit(
                    (
                        parsed.scheme,
                        host,
                        parsed.path,
                        "",
                        "",
                    )
                )
        except ValueError:
            return "<invalid>"
    return remote


def _restore_one(
    plan: RestorePlan,
    *,
    verify: bool,
    decrypt_passphrase_file: Path | None = None,
    force: bool = False,
    progress_label: str | None = None,
) -> RestoreResult:
    if plan.strategy == "git":
        _restore_git(plan, force=force, progress=progress_label is not None)
    elif plan.strategy == "git_bundle":
        _restore_git_bundle(
            plan,
            verify=verify,
            decrypt_passphrase_file=decrypt_passphrase_file,
            force=force,
        )
    elif plan.strategy == "archive":
        if plan.incremental:
            _restore_incremental(
                plan,
                verify=verify,
                decrypt_passphrase_file=decrypt_passphrase_file,
                force=force,
                progress_label=progress_label,
            )
        else:
            _restore_archive(
                plan,
                verify=verify,
                decrypt_passphrase_file=decrypt_passphrase_file,
                force=force,
                progress_label=progress_label,
            )
    else:
        raise RestoreError(f"unsupported strategy: {plan.strategy}")
    return RestoreResult(
        project=plan.project,
        strategy=plan.strategy,
        target=plan.target,
    )


def _restore_submodules(
    repo: Path, project: str, *, progress: bool = False
) -> None:
    pending = [(repo, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > 32:
            raise RestoreError(f"submodule nesting is too deep for {project}")
        _restore_lfs(current, project, progress=progress)
        modules = submodule_config(current)
        if modules is None:
            raise RestoreError(f"invalid submodule metadata for {project}")
        if not modules:
            continue
        if any(not is_supported_submodule_url(url) for _, url in modules):
            raise RestoreError(f"unsupported submodule URL for {project}")
        paths = [path.as_posix() for path, _ in modules]
        init_args = ["-C", str(current), "submodule"]
        if not progress:
            init_args.append("--quiet")
        init_args.extend(["init", "--", *paths])
        initialized = _run_git(init_args, check=False)
        if initialized.returncode:
            raise RestoreError(
                f"cannot initialize submodules for {project}: {_git_failure_detail(initialized)}"
            )
        update_args = [
            "-c",
            "submodule.recurse=false",
            "-C",
            str(current),
            "submodule",
        ]
        if not progress:
            update_args.append("--quiet")
        update_args.extend(["update", "--init", "--checkout", "--", *paths])
        update_started = time.monotonic() if progress else None
        if progress:
            print(
                f"progress: {project}: updating {len(paths)} submodule(s)",
                file=sys.stderr,
            )
        updated = _run_git(
            update_args,
            extra_env={"GIT_LFS_SKIP_SMUDGE": "1"},
            check=False,
            stream_stderr=progress,
        )
        if updated.returncode:
            raise RestoreError(
                f"cannot restore submodules for {project}: {_git_failure_detail(updated)}"
            )
        if progress:
            elapsed = format_elapsed(
                0.0 if update_started is None else time.monotonic() - update_started
            )
            print(
                f"progress: {project}: submodules done ({elapsed})",
                file=sys.stderr,
            )
        for relative, _ in modules:
            target = current.joinpath(*relative.parts)
            if target.is_symlink() or not target.is_dir():
                raise RestoreError(f"invalid restored submodule for {project}")
            target = target.resolve()
            if not target.is_relative_to(current.resolve()):
                raise RestoreError(f"submodule escapes restore target for {project}")
            result = _run_git(
                ["-C", str(target), "rev-parse", "HEAD"],
                check=False,
            )
            if result.returncode:
                raise RestoreError(f"cannot verify restored submodule for {project}")
            commit = result.stdout.strip().lower()
            if len(commit) < 7:
                raise RestoreError(f"cannot verify restored submodule for {project}")
            pending.append((target, depth + 1))


def _restore_lfs(repo: Path, project: str, *, progress: bool = False) -> None:
    lfsconfig = repo / ".lfsconfig"
    if lfsconfig.exists() or lfsconfig.is_symlink():
        raise RestoreError(f"unsupported Git LFS configuration for {project}")
    attributes = _run_git(
        ["-C", str(repo), "grep", "-l", "filter=lfs", "--", "*.gitattributes"],
        check=False,
    )
    if attributes.returncode == 1:
        return
    if attributes.returncode:
        raise RestoreError(f"cannot inspect Git LFS attributes for {project}")
    endpoint = _run_git(
        [
            "-C",
            str(repo),
            "config",
            "--get-regexp",
            r"^(lfs\..*\.url|remote\..*\.lfsurl)$",
        ],
        check=False,
    )
    if endpoint.returncode == 0 and endpoint.stdout.strip():
        raise RestoreError(f"custom Git LFS endpoints are not supported for {project}")
    if endpoint.returncode not in (0, 1):
        raise RestoreError(f"cannot inspect Git LFS configuration for {project}")
    version = _run_git(["-C", str(repo), "lfs", "version"], check=False)
    if version.returncode:
        raise RestoreError(
            f"Git LFS is required to restore {project}: {_git_failure_detail(version)}"
        )
    installed = _run_git(["-C", str(repo), "lfs", "install", "--local"], check=False)
    if installed.returncode:
        raise RestoreError(
            f"cannot configure Git LFS for {project}: {_git_failure_detail(installed)}"
        )
    pull_started = time.monotonic() if progress else None
    if progress:
        print(f"progress: {project}: pulling LFS objects", file=sys.stderr)
    pulled = _run_git(
        ["-C", str(repo), "lfs", "pull"],
        check=False,
        stream_stderr=progress,
    )
    if pulled.returncode:
        raise RestoreError(
            f"cannot restore Git LFS objects for {project}: {_git_failure_detail(pulled)}"
        )
    if progress:
        elapsed = format_elapsed(
            0.0 if pull_started is None else time.monotonic() - pull_started
        )
        print(
            f"progress: {project}: LFS pull done ({elapsed})",
            file=sys.stderr,
        )


def _restore_git(
    plan: RestorePlan, *, force: bool = False, progress: bool = False
) -> None:
    if plan.remote_url is None or plan.commit is None:
        raise RestoreError(f"incomplete git restore plan for {plan.project}")
    if not force and (plan.target.exists() or plan.target.is_symlink()):
        raise RestoreError(f"restore target already exists: {plan.target}")

    plan.target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{plan.project}-restore-",
            dir=plan.target.parent,
        )
    )
    repo = temporary / "repo"
    try:
        safe_remote = redact_remote_url(plan.remote_url)
        _run_git(["init", str(repo)])
        fetch_remote = f"projjan-restore-{temporary.name.rsplit('-', 1)[-1]}"
        refspecs = (
            "+refs/heads/*:refs/remotes/origin/*",
            "+refs/tags/*:refs/tags/*",
        )
        for refspec in refspecs:
            _run_git(
                [
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    "--add",
                    f"remote.{fetch_remote}.fetch",
                    refspec,
                ]
            )
        probe_url = "https://projjan.invalid/"
        probe_env = _git_config_env(f"remote.{fetch_remote}.url", probe_url)
        uses_config_env = False
        if probe_env is not None:
            # Probe with a harmless URL so secrets never enter argv or probe output.
            probe = _run_git(
                ["-C", str(repo), "config", "--get", f"remote.{fetch_remote}.url"],
                extra_env=probe_env,
                check=False,
            )
            uses_config_env = (
                probe.returncode == 0 and probe.stdout.strip() == probe_url
            )
        if not uses_config_env and safe_remote != plan.remote_url:
            raise RestoreError(
                "Git cannot safely pass URL credentials or query data; "
                "upgrade Git or use a credential helper or SSH"
            )
        fetch_args = ["-C", str(repo), "fetch", "--no-write-fetch-head", "--no-tags"]
        if progress:
            fetch_args.append("--progress")
        fetch_env = None
        if uses_config_env:
            fetch_args.append(fetch_remote)
            fetch_env = dict(probe_env)
            config_index = int(fetch_env["GIT_CONFIG_COUNT"]) - 1
            fetch_env[f"GIT_CONFIG_VALUE_{config_index}"] = plan.remote_url
        else:
            fetch_args.extend([plan.remote_url, *refspecs])
        fetch_started = time.monotonic() if progress else None
        if progress:
            print(
                f"progress: {plan.project}: fetching git objects",
                file=sys.stderr,
            )
        _run_git(
            fetch_args,
            extra_env=fetch_env,
            sensitive_remote=plan.remote_url,
            stream_stderr=progress,
        )
        if progress:
            elapsed = format_elapsed(
                0.0 if fetch_started is None else time.monotonic() - fetch_started
            )
            print(
                f"progress: {plan.project}: fetch done ({elapsed})",
                file=sys.stderr,
            )
        _run_git(
            ["-C", str(repo), "config", "--local", "--remove-section", f"remote.{fetch_remote}"]
        )
        _run_git(["-C", str(repo), "remote", "add", "origin", safe_remote])
        _run_git(
            ["-C", str(repo), "checkout", "--detach", plan.commit],
            extra_env={"GIT_LFS_SKIP_SMUDGE": "1"},
        )
        head = _run_git(["-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
        if not head.lower().startswith(plan.commit.lower()):
            raise RestoreError(
                f"restored HEAD {head} does not match {plan.commit}"
            )
        _restore_submodules(repo, plan.project, progress=progress)
        _publish_restored_tree(repo, plan.target, force=force)
    finally:
        _cleanup_restore_temp(temporary)


def _restore_git_bundle(
    plan: RestorePlan,
    *,
    verify: bool,
    decrypt_passphrase_file: Path | None = None,
    force: bool = False,
) -> None:
    from proj_janitor.crypto import (
        archive_is_encrypted,
        cleanup_decrypt_temp,
        openssl_decrypt_to_temp,
    )

    if plan.archive is None or plan.commit is None or plan.branch is None:
        raise RestoreError(f"incomplete git bundle restore plan for {plan.project}")
    if not force and (plan.target.exists() or plan.target.is_symlink()):
        raise RestoreError(f"restore target already exists: {plan.target}")
    plan.target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{plan.project}-restore-",
            dir=plan.target.parent,
        )
    )
    repo = temporary / "repo"
    decrypted: Path | None = None
    try:
        bundle_source = plan.archive
        if archive_is_encrypted(plan.archive, plan.encryption):
            if decrypt_passphrase_file is None:
                raise RestoreError(
                    "encrypted archive requires --decrypt-passphrase-file or "
                    "[restore].decrypt_passphrase_file"
                )
            if verify:
                _verify_archive_hash(plan.archive, plan.project, plan.sha256)
                verify = False
            decrypted = openssl_decrypt_to_temp(
                plan.archive, decrypt_passphrase_file
            )
            bundle_source = decrypted
        bundle_copy = temporary / "source.bundle"
        with bundle_source.open("rb") as source, bundle_copy.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        if verify and sha256_file(bundle_copy).lower() != (plan.sha256 or "").lower():
            raise RestoreError(f"sha256 mismatch for {plan.archive}")
        initialized = _run_git(["init", str(repo)], check=False)
        if initialized.returncode:
            raise RestoreError(
                f"cannot initialize Git bundle restore for {plan.project}: {_git_failure_detail(initialized)}"
            )
        # Point HEAD at an unborn placeholder so fetch can update every branch in
        # the bundle, including the machine default (often `master`/`main`).
        # Modern Git refuses: "refusing to fetch into branch '…' checked out at …".
        prepared = _run_git(
            [
                "-C",
                str(repo),
                "symbolic-ref",
                "HEAD",
                "refs/heads/projjan-restore-temp",
            ],
            check=False,
        )
        if prepared.returncode:
            raise RestoreError(
                f"cannot prepare Git bundle restore for {plan.project}: {_git_failure_detail(prepared)}"
            )
        # `git bundle verify` requires a repository context on modern Git; running it
        # with the caller's cwd (e.g. $HOME) fails with "need a repository to verify a
        # bundle". Always verify inside the fresh restore repo.
        verified = _run_git(
            ["-C", str(repo), "bundle", "verify", str(bundle_copy)],
            check=False,
        )
        if verified.returncode:
            raise RestoreError(
                f"invalid Git bundle for {plan.project}: {_git_failure_detail(verified)}"
            )
        fetched = _run_git(
            [
                "-C",
                str(repo),
                "fetch",
                "--no-tags",
                str(bundle_copy),
                "+refs/heads/*:refs/heads/*",
                "+refs/tags/*:refs/tags/*",
            ],
            check=False,
        )
        if fetched.returncode:
            raise RestoreError(
                f"cannot fetch Git bundle for {plan.project}: {_git_failure_detail(fetched)}"
            )
        checked = _run_git(
            ["-C", str(repo), "check-ref-format", "--branch", plan.branch],
            check=False,
        )
        if checked.returncode:
            raise RestoreError(
                f"invalid branch in Git bundle manifest for {plan.project}: {_git_failure_detail(checked)}"
            )
        checkout = _run_git(
            ["-C", str(repo), "checkout", "--force", "-B", plan.branch, plan.commit],
            check=False,
            extra_env={"GIT_LFS_SKIP_SMUDGE": "1"},
        )
        if checkout.returncode:
            raise RestoreError(
                f"cannot check out Git bundle commit for {plan.project}: {_git_failure_detail(checkout)}"
            )
        head = _run_git(["-C", str(repo), "rev-parse", "HEAD"], check=False)
        if head.returncode or head.stdout.strip().lower() != plan.commit.lower():
            raise RestoreError(f"Git bundle commit mismatch for {plan.project}")
        _publish_restored_tree(repo, plan.target, force=force)
    finally:
        if decrypted is not None:
            cleanup_decrypt_temp(decrypted)
        _cleanup_restore_temp(temporary)


def _restore_archive(
    plan: RestorePlan,
    *,
    verify: bool,
    decrypt_passphrase_file: Path | None = None,
    force: bool = False,
    progress_label: str | None = None,
) -> None:
    from proj_janitor.crypto import (
        archive_is_encrypted,
        cleanup_decrypt_temp,
        openssl_decrypt_to_temp,
    )

    if plan.archive is None:
        raise RestoreError(f"incomplete archive restore plan for {plan.project}")
    if not force and (plan.target.exists() or plan.target.is_symlink()):
        raise RestoreError(f"restore target already exists: {plan.target}")

    archive_path = plan.archive
    decrypted: Path | None = None
    try:
        if archive_is_encrypted(archive_path, plan.encryption):
            if decrypt_passphrase_file is None:
                raise RestoreError(
                    "encrypted archive requires --decrypt-passphrase-file or "
                    "[restore].decrypt_passphrase_file"
                )
            if verify:
                # Ciphertext was already hashed in execute_restore pre-check.
                _verify_archive_hash(archive_path, plan.project, plan.sha256)
            decrypted = openssl_decrypt_to_temp(
                archive_path,
                decrypt_passphrase_file,
                progress_label=progress_label,
            )
            archive_path = decrypted
            verify = False

        with archive_path.open("rb") as archive_source:
            if verify:
                if not plan.sha256:
                    raise RestoreError(f"manifest has no sha256 for {plan.project}")
                # Re-hash without progress; pre-check already reported hashing.
                actual = sha256_stream(archive_source)
                if actual.lower() != plan.sha256.lower():
                    raise RestoreError(
                        f"sha256 mismatch for {plan.archive}: "
                        f"expected {plan.sha256}, got {actual}"
                    )
                archive_source.seek(0)
            plan.target.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=f".{plan.project}-restore-",
                    dir=plan.target.parent,
                )
            )
            try:
                _safe_extract(
                    archive_path,
                    temporary,
                    plan.project,
                    fileobj=archive_source,
                    progress_label=progress_label,
                )
                extracted = temporary / plan.project
                if extracted.is_symlink() or not extracted.is_dir():
                    raise RestoreError(
                        f"archive does not contain project directory: {plan.project}"
                    )
                _publish_restored_tree(extracted, plan.target, force=force)
            finally:
                _cleanup_restore_temp(temporary)
    finally:
        if decrypted is not None:
            cleanup_decrypt_temp(decrypted)


def _restore_incremental(
    plan: RestorePlan,
    *,
    verify: bool,
    decrypt_passphrase_file: Path | None = None,
    force: bool = False,
    progress_label: str | None = None,
) -> None:
    if plan.archive is None:
        raise RestoreError(f"incomplete incremental restore plan for {plan.project}")
    manifest_dir = plan.manifest_dir
    if manifest_dir is None:
        manifest_dir = plan.archive.parent
    project = plan.project

    chain = [plan]
    current = plan
    seen: set[str] = set()
    while True:
        incremental = current.incremental
        if not incremental:
            break
        base_name = incremental.get("base")
        if not isinstance(base_name, str) or not base_name:
            raise RestoreError(
                f"incremental snapshot {current.archive} has no base manifest"
            )
        if base_name in seen:
            raise RestoreError(f"incremental chain cycle at {base_name}")
        seen.add(base_name)
        base_path = manifest_dir / base_name
        if not base_path.is_file():
            raise RestoreError(f"incremental base manifest not found: {base_name}")
        try:
            document = json.loads(base_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RestoreError(f"cannot read base manifest {base_path}: {exc}") from exc
        records = document.get("records")
        if not isinstance(records, list) or not records or not isinstance(records[0], dict):
            raise RestoreError(f"invalid base manifest records: {base_path}")
        base_record = records[0]
        if base_record.get("strategy") != "archive":
            raise RestoreError(
                f"incremental base crosses non-archive snapshot: {base_name}"
            )
        base_archive = _resolve_archive(base_record, manifest_dir, project)
        base_sha256 = base_record.get("sha256")
        if base_sha256 is not None and not isinstance(base_sha256, str):
            raise RestoreError(f"invalid sha256 in base manifest: {base_name}")
        base_plan = RestorePlan(
            project=project,
            strategy="archive",
            target=plan.target,
            archive=base_archive,
            sha256=base_sha256,
            incremental=base_record.get("incremental"),
            inventory=base_record.get("inventory"),
            manifest_dir=manifest_dir,
            in_place=plan.in_place,
            encryption=(
                base_record.get("encryption")
                if isinstance(base_record.get("encryption"), str)
                else None
            ),
        )
        chain.insert(0, base_plan)
        current = base_plan

    _restore_archive(
        chain[0],
        verify=verify,
        decrypt_passphrase_file=decrypt_passphrase_file,
        force=force,
        progress_label=progress_label,
    )
    for delta_plan in chain[1:]:
        deleted = (delta_plan.incremental or {}).get("deleted") or []
        if not isinstance(deleted, list):
            raise RestoreError(f"invalid deleted list in {delta_plan.incremental}")
        _remove_deleted_paths(
            plan.target, [str(item) for item in deleted if isinstance(item, str)]
        )
        _extract_incremental_delta(
            delta_plan.archive,
            plan.target,
            project,
            delta_plan.sha256 if verify else None,
        )


def _remove_deleted_paths(target: Path, paths: list[str]) -> None:
    for raw in paths:
        if "\x00" in raw or raw.startswith("/"):
            raise RestoreError(f"unsafe deleted path: {raw}")
        parts = tuple(part for part in PurePosixPath(raw).parts if part not in ("", "."))
        if not parts or any(part == ".." for part in parts):
            raise RestoreError(f"unsafe deleted path: {raw}")
        _remove_any(target.joinpath(*parts))


def _remove_any(dest: Path) -> None:
    try:
        if dest.is_symlink():
            dest.unlink()
        elif dest.is_dir():
            shutil.rmtree(dest)
        elif dest.exists():
            dest.unlink()
    except FileNotFoundError:
        pass


def _extract_incremental_delta(
    archive: Path,
    target: Path,
    project: str,
    expected_hash: str | None,
) -> None:
    if expected_hash:
        actual = sha256_file(archive)
        if actual.lower() != expected_hash.lower():
            raise RestoreError(
                f"sha256 mismatch for {archive}: expected {expected_hash}, got {actual}"
            )
    try:
        with tarfile.open(archive, "r:*") as tar:
            seen_members: set[tuple[str, ...]] = set()
            seen_archive_paths: dict[tuple[str, ...], tuple[str, ...]] = {}
            for member in tar:
                parts = _archive_parts(member.name)
                if parts[0] != project:
                    raise RestoreError(
                        f"archive entry is outside project {project}: {member.name}"
                    )
                if not safe_archive_member(member.name, project):
                    raise RestoreError(format_unsafe_archive_path(member.name))
                if parts in seen_members:
                    raise RestoreError(
                        f"archive contains a duplicate path: {member.name}"
                    )
                seen_members.add(parts)
                if not register_archive_path(parts, seen_archive_paths):
                    raise RestoreError(
                        "archive paths collide on case-insensitive filesystems "
                        f"(including Unicode normalization): {member.name}"
                    )
                if member.islnk():
                    raise RestoreError(
                        f"archive contains unsupported hard link: {member.name}"
                    )
                if member.issym():
                    _validate_archive_link(member)
                elif member.isdir():
                    pass
                elif not _is_extractable_file_member(member):
                    raise RestoreError(
                        f"archive contains unsupported entry type: {member.name}"
                    )
                relative = parts[1:]
                dest = target if not relative else target.joinpath(*relative)
                if member.isdir():
                    _ensure_dir(dest)
                else:
                    _remove_any(dest)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if member.issym():
                        os.symlink(member.linkname, dest)
                    else:
                        source = tar.extractfile(member)
                        if source is None:
                            raise RestoreError(
                                f"cannot read archive member: {member.name}"
                            )
                        with source, dest.open("wb") as output:
                            shutil.copyfileobj(source, output)
                        try:
                            os.chmod(dest, member.mode & 0o777)
                        except OSError:
                            pass
    except (OSError, tarfile.TarError, TypeError) as exc:
        raise RestoreError(f"cannot extract archive {archive}: {exc}") from exc


def _ensure_dir(dest: Path) -> None:
    if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
        _remove_any(dest)
    dest.mkdir(parents=True, exist_ok=True)


def _safe_extract(
    archive: Path,
    dest: Path,
    project: str,
    *,
    fileobj: BinaryIO | None = None,
    progress_label: str | None = None,
) -> None:
    try:
        tar_source = (
            tarfile.open(archive, "r:*")
            if fileobj is None
            else tarfile.open(fileobj=fileobj, mode="r:*")
        )
        with tar_source as tar:
            members = tar.getmembers()
            if not members:
                raise RestoreError(f"archive is empty: {archive}")
            seen_archive_paths: dict[tuple[str, ...], tuple[str, ...]] = {}
            seen_members: set[tuple[str, ...]] = set()
            for member in members:
                parts = _archive_parts(member.name)
                if parts[0] != project:
                    raise RestoreError(
                        f"archive entry is outside project {project}: "
                        f"{member.name}"
                    )
                if not safe_archive_member(member.name, project):
                    raise RestoreError(format_unsafe_archive_path(member.name))
                if parts in seen_members:
                    raise RestoreError(f"archive contains a duplicate path: {member.name}")
                seen_members.add(parts)
                if not register_archive_path(parts, seen_archive_paths):
                    raise RestoreError(
                        "archive paths collide on case-insensitive filesystems "
                        f"(including Unicode normalization): {member.name}"
                    )
                if member.islnk():
                    raise RestoreError(
                        f"archive contains unsupported hard link: {member.name}"
                    )
                if member.issym():
                    _validate_archive_link(member)
                elif member.isdir():
                    pass
                elif not _is_extractable_file_member(member):
                    raise RestoreError(
                        f"archive contains unsupported entry type: {member.name}"
                    )
            extract_kwargs: dict[str, object] = {}
            if hasattr(tarfile, "data_filter"):
                extract_kwargs["filter"] = "data"
            if progress_label is None:
                tar.extractall(dest, **extract_kwargs)  # type: ignore[arg-type]
            else:
                total = len(members)
                progress_started = time.monotonic()
                for index, member in enumerate(members, start=1):
                    tar.extract(member, dest, **extract_kwargs)  # type: ignore[arg-type]
                    _report_extract_progress(
                        progress_label, progress_started, index, done=False
                    )
                _report_extract_progress(
                    progress_label, progress_started, total, done=True
                )
    except (OSError, tarfile.TarError, TypeError) as exc:
        raise RestoreError(f"cannot extract archive {archive}: {exc}") from exc


def _is_extractable_file_member(member: tarfile.TarInfo) -> bool:
    """Accept regular and GNU sparse/contiguous file members.

    Rust ``tar`` historically emitted GNU sparse headers for holey files;
    Python ``TarInfo.isfile()`` already treats those as files, but our
    explicit type allow-list must include them too.
    """
    if member.isfile():
        return True
    allowed = {tarfile.REGTYPE, tarfile.AREGTYPE}
    cont = getattr(tarfile, "CONTTYPE", None)
    if cont is not None:
        allowed.add(cont)
    sparse = getattr(tarfile, "GNUTYPE_SPARSE", None)
    if sparse is not None:
        allowed.add(sparse)
    return member.type in allowed


def _report_extract_progress(
    label: str, started: float, members: int, *, done: bool
) -> None:
    elapsed = format_elapsed(time.monotonic() - started)
    if done:
        print(
            f"progress: {label}: extracted {members} members (done, {elapsed})",
            file=sys.stderr,
        )
        return
    if members == 1 or members % 100 == 0:
        print(
            f"progress: {label}: extracted {members} members ({elapsed})",
            file=sys.stderr,
        )


def _archive_parts(name: str) -> tuple[str, ...]:
    if not name or "\x00" in name:
        raise RestoreError("archive contains an empty or invalid path")
    path = PurePosixPath(name)
    if path.is_absolute():
        raise RestoreError(f"archive contains an absolute path: {name}")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise RestoreError(format_unsafe_archive_path(name))
    return parts


def _validate_archive_link(
    member: tarfile.TarInfo,
) -> None:
    link = member.linkname
    if "\x00" in link:
        raise RestoreError("archive contains an invalid link target")
    if not safe_archive_link(member.name, link):
        raise RestoreError(f"archive symlink escapes project: {member.name}")


def _run_git(
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    sensitive_remote: str | None = None,
    check: bool = True,
    timeout: int | None = None,
    stream_stderr: bool = False,
) -> subprocess.CompletedProcess[str]:
    if timeout is None:
        timeout = _GIT_TIMEOUT_SECONDS
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_PAGER"] = "cat"
    if extra_env is not None:
        environment.update(extra_env)
    command = " ".join(args[:2])
    try:
        options = {"start_new_session": True} if os.name == "posix" else {}
        process = subprocess.Popen(
            ["git", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            **options,
        )
    except OSError as exc:
        raise RestoreError(f"cannot run git: {exc}") from exc
    previous_sigterm = None
    if os.name == "posix" and threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.signal(signal.SIGTERM, _interrupt_git)
    try:
        if stream_stderr:
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []

            def _drain(stream: object, chunks: list[str], *, echo: bool) -> None:
                while True:
                    chunk = stream.read(4096)  # type: ignore[attr-defined]
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if echo:
                        sys.stderr.write(chunk)
                        sys.stderr.flush()

            readers = [
                threading.Thread(
                    target=_drain,
                    args=(process.stdout, stdout_chunks),
                    kwargs={"echo": False},
                    daemon=True,
                ),
                threading.Thread(
                    target=_drain,
                    args=(process.stderr, stderr_chunks),
                    kwargs={"echo": True},
                    daemon=True,
                ),
            ]
            for reader in readers:
                reader.start()
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    _stop_git_process(process)
                    for reader in readers:
                        reader.join(timeout=5)
                    raise GitTimeoutError(
                        f"git {command} timed out after {timeout} seconds"
                    )
                time.sleep(0.05)
            for reader in readers:
                reader.join(timeout=5)
            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)
        else:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _stop_git_process(process)
                raise GitTimeoutError(
                    f"git {command} timed out after {timeout} seconds"
                ) from exc
    except (KeyboardInterrupt, SystemExit):
        _stop_git_process(process)
        raise
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    result = subprocess.CompletedProcess(
        ["git", *args], process.returncode, stdout, stderr
    )
    if check and result.returncode != 0:
        detail = _git_failure_detail(result, args=args, sensitive_remote=sensitive_remote)
        raise RestoreError(f"git {command} failed: {detail}")
    return result


def _git_failure_detail(
    result: subprocess.CompletedProcess[str],
    *,
    args: list[str] | None = None,
    sensitive_remote: str | None = None,
) -> str:
    """Return a single-line git failure detail, redacting credentialed URLs."""
    if args is not None and _git_args_have_sensitive_remote(args, sensitive_remote):
        return "remote URL details redacted"
    detail = (result.stderr or "").strip() or (result.stdout or "").strip()
    if not detail:
        return f"exit code {result.returncode}"
    line = next(
        (part.strip() for part in reversed(detail.splitlines()) if part.strip()),
        detail,
    )
    return _redact_git_message(line)


def _redact_git_message(message: str) -> str:
    marker = "://"
    idx = message.find(marker)
    if idx >= 0:
        after = message[idx + len(marker) :]
        at = after.find("@")
        if at > 0 and ":" in after[:at]:
            return "remote URL details redacted"
    return message


def _git_args_have_sensitive_remote(
    args: list[str], remote_override: str | None = None
) -> bool:
    for value in ([remote_override] if remote_override else []) + args:
        if value.startswith(("http://", "https://", "ssh://", "file://")):
            try:
                remote = urlsplit(value)
            except ValueError:
                return True
            if remote.username is not None or remote.password is not None:
                return True
            if remote.query or remote.fragment:
                return True
        elif re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:.+", value):
            return True
    return False


def _git_config_env(key: str, value: str) -> dict[str, str] | None:
    raw_count = os.environ.get("GIT_CONFIG_COUNT", "0")
    try:
        count = int(raw_count) if raw_count else 0
    except ValueError:
        return None
    if count < 0:
        return None
    return {
        "GIT_CONFIG_COUNT": str(count + 1),
        f"GIT_CONFIG_KEY_{count}": key,
        f"GIT_CONFIG_VALUE_{count}": value,
    }


def _stop_git_process(process: subprocess.Popen[str]) -> None:
    # Detached descendants that call setsid() can escape killpg (CR-131);
    # use stronger OS supervision (cgroup/job) for strict containment.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            _kill_git_process(process)
    elif os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            if result.returncode != 0:
                _kill_git_process(process)
        except (OSError, subprocess.TimeoutExpired):
            _kill_git_process(process)
    else:
        _kill_git_process(process)
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        _kill_git_process(process)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        process.wait()


def _kill_git_process(process: subprocess.Popen[str]) -> None:
    try:
        process.kill()
    except OSError:
        pass


def _cleanup_restore_temp(path: Path) -> None:
    try:
        shutil.rmtree(path, onerror=_remove_readonly)
    except OSError:
        pass


def _remove_readonly(function, path: str, error_info) -> None:
    error = error_info[1]
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _interrupt_git(_signum: int, _frame: object) -> None:
    if _signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(128 + _signum)
