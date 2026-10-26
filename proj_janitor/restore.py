from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from proj_janitor.backup import safe_archive_link, sha256_file
from proj_janitor.models import RestorePlan, RestoreResult

_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,64}\Z")


class RestoreError(RuntimeError):
    pass


def restore_from_manifest(
    manifest_path: Path,
    dest: Path | None,
    projects: Iterable[str] = (),
    dry_run: bool = False,
    verify: bool = True,
) -> tuple[list[RestorePlan], list[RestoreResult]]:
    plans = plan_restore(
        manifest_path,
        dest,
        projects=projects,
        verify=verify,
    )
    if dry_run:
        return plans, []

    results = execute_restore(plans)
    return plans, results


def execute_restore(plans: Iterable[RestorePlan]) -> list[RestoreResult]:
    plan_list = list(plans)
    try:
        for plan in plan_list:
            plan.target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RestoreError(
            f"cannot create restore target directory: {exc}"
        ) from exc

    results: list[RestoreResult] = []
    for plan in plan_list:
        try:
            results.append(_restore_one(plan))
        except OSError as exc:
            raise RestoreError(
                f"cannot restore {plan.project}: {exc}"
            ) from exc
    return results


def plan_restore(
    manifest_path: Path,
    dest: Path | None,
    projects: Iterable[str] = (),
    verify: bool = True,
) -> list[RestorePlan]:
    manifest_path, manifest = load_manifest(manifest_path)
    if dest is not None:
        dest = dest.expanduser().resolve()
        if dest.exists() and not dest.is_dir():
            raise RestoreError(f"destination is not a directory: {dest}")

    records = manifest["records"]
    requested = list(projects)
    if requested:
        wanted = set(requested)
        selected = []
        found = set()
        for record in records:
            name = _record_project(record)
            if name in wanted:
                selected.append(record)
                found.add(name)
        missing = wanted - found
        if missing:
            names = ", ".join(sorted(missing))
            raise RestoreError(f"project not found in manifest: {names}")
    else:
        selected = records

    plans: list[RestorePlan] = []
    seen: set[str] = set()
    seen_targets: set[Path] = set()
    seen_casefolded_targets: set[str] = set()
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
        casefolded_target = str(target).casefold()
        if casefolded_target in seen_casefolded_targets:
            raise RestoreError(
                f"restore targets collide on case-insensitive filesystems: {target}"
            )
        seen_casefolded_targets.add(casefolded_target)
        if target.exists() or target.is_symlink():
            raise RestoreError(f"restore target already exists: {target}")

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
                )
            )
            continue

        if strategy == "archive":
            archive = _resolve_archive(record, manifest_path.parent, project)
            sha256 = record.get("sha256")
            if sha256 is not None and not isinstance(sha256, str):
                raise RestoreError(f"invalid sha256 for {project}")
            hash_verified = False
            if verify:
                if not sha256:
                    raise RestoreError(
                        f"manifest has no sha256 for {project}; "
                        "use --no-verify to skip verification"
                    )
                try:
                    actual = sha256_file(archive)
                except OSError as exc:
                    raise RestoreError(
                        f"cannot read archive {archive}: {exc}"
                    ) from exc
                if actual.lower() != sha256.lower():
                    raise RestoreError(
                        f"sha256 mismatch for {archive}: "
                        f"expected {sha256}, got {actual}"
                    )
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
                )
            )
            continue

        raise RestoreError(
            f"unsupported backup strategy for {project}: {strategy!r}"
        )
    return plans


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
    if manifest.get("schema_version") != 2:
        raise RestoreError(
            "unsupported manifest schema_version: "
            f"{manifest.get('schema_version')!r}"
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
            raise RestoreError(f"invalid remote URL: {remote}") from exc
        if parsed.scheme in {"https", "ssh"} and not parsed.hostname:
            raise RestoreError(f"remote URL has no host: {remote}")
        return
    if re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:.+", remote):
        return
    if Path(remote).is_absolute():
        return
    raise RestoreError(f"unsupported remote URL: {remote}")


def redact_remote_url(remote: str) -> str:
    if remote.startswith(("http://", "https://", "ssh://")):
        try:
            parsed = urlsplit(remote)
            if parsed.username is not None or parsed.password is not None:
                host = parsed.hostname or ""
                if parsed.port is not None:
                    host = f"{host}:{parsed.port}"
                return urlunsplit(
                    (
                        parsed.scheme,
                        host,
                        parsed.path,
                        parsed.query,
                        parsed.fragment,
                    )
                )
        except ValueError:
            return "<invalid>"
    return remote


def _restore_one(plan: RestorePlan) -> RestoreResult:
    if plan.strategy == "git":
        _restore_git(plan)
    elif plan.strategy == "archive":
        _restore_archive(plan)
    else:
        raise RestoreError(f"unsupported strategy: {plan.strategy}")
    return RestoreResult(
        project=plan.project,
        strategy=plan.strategy,
        target=plan.target,
    )


def _restore_git(plan: RestorePlan) -> None:
    if plan.remote_url is None or plan.commit is None:
        raise RestoreError(f"incomplete git restore plan for {plan.project}")
    if plan.target.exists() or plan.target.is_symlink():
        raise RestoreError(f"restore target already exists: {plan.target}")

    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{plan.project}-restore-",
            dir=plan.target.parent,
        )
    )
    repo = temporary / "repo"
    try:
        _run_git(
            ["clone", "--no-checkout", "--", plan.remote_url, str(repo)]
        )
        _run_git(["-C", str(repo), "checkout", "--detach", plan.commit])
        head = _run_git(["-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
        if not head.lower().startswith(plan.commit.lower()):
            raise RestoreError(
                f"restored HEAD {head} does not match {plan.commit}"
            )
        if plan.target.exists() or plan.target.is_symlink():
            raise RestoreError(f"restore target already exists: {plan.target}")
        repo.rename(plan.target)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _restore_archive(plan: RestorePlan) -> None:
    if plan.archive is None:
        raise RestoreError(f"incomplete archive restore plan for {plan.project}")
    if plan.target.exists() or plan.target.is_symlink():
        raise RestoreError(f"restore target already exists: {plan.target}")

    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{plan.project}-restore-",
            dir=plan.target.parent,
        )
    )
    try:
        _safe_extract(plan.archive, temporary, plan.project)
        extracted = temporary / plan.project
        if extracted.is_symlink() or not extracted.is_dir():
            raise RestoreError(
                f"archive does not contain project directory: {plan.project}"
            )
        if plan.target.exists() or plan.target.is_symlink():
            raise RestoreError(f"restore target already exists: {plan.target}")
        extracted.rename(plan.target)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _safe_extract(archive: Path, dest: Path, project: str) -> None:
    try:
        with tarfile.open(archive, "r:*") as tar:
            members = tar.getmembers()
            if not members:
                raise RestoreError(f"archive is empty: {archive}")
            seen_casefolded_paths: dict[
                tuple[str, ...], tuple[str, ...]
            ] = {}
            for member in members:
                parts = _archive_parts(member.name)
                if parts[0] != project:
                    raise RestoreError(
                        f"archive entry is outside project {project}: "
                        f"{member.name}"
                    )
                _validate_archive_case_path(
                    parts, seen_casefolded_paths, member.name
                )
                if member.ischr() or member.isblk() or member.isfifo():
                    raise RestoreError(
                        f"archive contains special file: {member.name}"
                    )
                if member.islnk():
                    raise RestoreError(
                        f"archive contains unsupported hard link: {member.name}"
                    )
                if member.issym():
                    _validate_archive_link(member)
            try:
                tar.extractall(dest, filter="data")
            except TypeError:
                tar.extractall(dest)
    except (OSError, tarfile.TarError) as exc:
        raise RestoreError(f"cannot extract archive {archive}: {exc}") from exc


def _archive_parts(name: str) -> tuple[str, ...]:
    if not name or "\x00" in name:
        raise RestoreError("archive contains an empty or invalid path")
    path = PurePosixPath(name)
    if path.is_absolute():
        raise RestoreError(f"archive contains an absolute path: {name}")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise RestoreError(f"archive contains an unsafe path: {name}")
    return parts


def _validate_archive_case_path(
    parts: tuple[str, ...],
    seen: dict[tuple[str, ...], tuple[str, ...]],
    member_name: str,
) -> None:
    for length in range(1, len(parts) + 1):
        original = parts[:length]
        folded = tuple(part.lower() for part in original)
        previous = seen.get(folded)
        if previous is not None and previous != original:
            raise RestoreError(
                f"archive paths collide on case-insensitive filesystems: "
                f"{member_name}"
            )
        seen[folded] = original


def _validate_archive_link(
    member: tarfile.TarInfo,
) -> None:
    link = member.linkname
    if "\x00" in link:
        raise RestoreError("archive contains an invalid link target")
    if not safe_archive_link(member.name, link):
        raise RestoreError(f"archive symlink escapes project: {member.name}")


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", *args],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    except OSError as exc:
        raise RestoreError(f"cannot run git: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        if not detail:
            detail = f"exit code {result.returncode}"
        command = " ".join(args[:2])
        raise RestoreError(f"git {command} failed: {detail}")
    return result
