from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from purplemux_client.errors import WorkerFailure
from purplemux_client.operations import (
    AuthoritativeMutationRejection,
    MutationResolution,
    PreDispatchFailure,
    Reconciliation,
    execute_mutation,
)
from purplemux_client.progress import emit_finding, register_run_resource

DEFAULT_WORKTREE_ROOT = Path("~/.local/share/agent-workflow-manager/worktrees")
REPOSITORY_CONTEXT_ENV = "PURPLEMUX_RUNNER_REPOSITORY_CONTEXT"
_OBJECT_ID_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


@dataclass(frozen=True)
class RepositoryExecutionContext:
    """Structured identity for one isolated repository workflow execution."""

    source_repository: Path
    remote: str
    base_branch: str
    base_ref: str
    base_sha: str
    execution_root: Path


@dataclass(frozen=True)
class RepositoryPreparation:
    """Read-only result shared by validation and run-time preparation."""

    source_repository: Path
    remote: str
    base_branch: str
    base_ref: str
    base_sha: str


def inspect_run_repository(
    *,
    repo: str | os.PathLike[str],
    base_branch: str,
    remote: str = "origin",
    command_timeout_seconds: float = 30.0,
) -> RepositoryPreparation:
    """Resolve and validate a declared source repository and remote base."""

    return _inspect_run_repository(
        repo=repo,
        base_branch=base_branch,
        remote=remote,
        command_timeout_seconds=command_timeout_seconds,
        live_remote=True,
    )


def _inspect_repository_declaration(
    *,
    repo: str | os.PathLike[str],
    base_branch: str,
    remote: str = "origin",
    command_timeout_seconds: float = 30.0,
) -> RepositoryPreparation:
    """Validate static declarations without contacting an arbitrary remote."""

    return _inspect_run_repository(
        repo=repo,
        base_branch=base_branch,
        remote=remote,
        command_timeout_seconds=command_timeout_seconds,
        live_remote=False,
    )


def _inspect_run_repository(
    *,
    repo: str | os.PathLike[str],
    base_branch: str,
    remote: str,
    command_timeout_seconds: float,
    live_remote: bool,
) -> RepositoryPreparation:
    if not isinstance(base_branch, str) or not base_branch or "\0" in base_branch:
        raise ValueError("base_branch must be a non-empty string without nulls")
    if (
        not isinstance(remote, str)
        or not remote
        or "\0" in remote
        or remote.startswith("-")
    ):
        raise ValueError("remote must be a non-empty name")
    if command_timeout_seconds <= 0:
        raise ValueError("command_timeout_seconds must be positive")
    try:
        requested = Path(repo).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise WorkerFailure(f"repository path could not be resolved: {exc}") from exc
    if not requested.is_dir():
        raise WorkerFailure(f"repository directory does not exist: {requested}")

    source = Path(
        _git_read(
            requested,
            ["rev-parse", "--show-toplevel"],
            command_timeout_seconds,
        )
    ).resolve()
    if not source.is_dir():
        raise WorkerFailure(f"Git reported an invalid repository root: {source}")
    remotes = _git_read(source, ["remote"], command_timeout_seconds).splitlines()
    if remote not in remotes:
        raise WorkerFailure(f"Git remote {remote!r} does not exist in {source}")
    _git_read(source, ["remote", "get-url", remote], command_timeout_seconds)
    checked_branch = _git_read(
        source,
        ["check-ref-format", "--branch", base_branch],
        command_timeout_seconds,
    )
    if checked_branch != base_branch:
        raise WorkerFailure(f"invalid base branch {base_branch!r}")
    if live_remote:
        remote_ref = f"refs/heads/{base_branch}"
        remote_lines = _git_read(
            source,
            ["ls-remote", "--exit-code", remote, remote_ref],
            command_timeout_seconds,
        ).splitlines()
        matches = [
            line.split("\t", 1)[0].lower()
            for line in remote_lines
            if line.split("\t", 1)[-1] == remote_ref
        ]
        if len(matches) != 1:
            raise WorkerFailure(
                f"remote base {remote}/{base_branch} did not resolve exactly once"
            )
        base_sha = matches[0]
    else:
        base_sha = _git_read(
            source,
            [
                "rev-parse",
                "--verify",
                f"refs/remotes/{remote}/{base_branch}^{{commit}}",
            ],
            command_timeout_seconds,
        ).lower()
    if not _OBJECT_ID_RE.fullmatch(base_sha):
        raise WorkerFailure(
            f"remote base {remote}/{base_branch} did not resolve to a commit"
        )
    return RepositoryPreparation(
        source_repository=source,
        remote=remote,
        base_branch=base_branch,
        base_ref=f"{remote}/{base_branch}",
        base_sha=base_sha,
    )


def prepare_run_repository(
    *,
    repo: str | os.PathLike[str],
    base_branch: str,
    remote: str = "origin",
    worktree_root: str | os.PathLike[str] | None = None,
    command_timeout_seconds: float = 30.0,
) -> RepositoryExecutionContext:
    """Create, verify, and register a fresh detached run worktree.

    The source checkout is inspected but never switched, reset, stashed, or cleaned.
    Dry Run stops at the worktree-add mutation through the shared mutation protocol.
    """

    resumed = _resume_context(
        repo=repo,
        base_branch=base_branch,
        remote=remote,
        command_timeout_seconds=command_timeout_seconds,
    )
    if resumed is not None:
        return resumed

    preparation = inspect_run_repository(
        repo=repo,
        base_branch=base_branch,
        remote=remote,
        command_timeout_seconds=command_timeout_seconds,
    )
    if worktree_root is not None and not isinstance(worktree_root, (str, os.PathLike)):
        raise TypeError("worktree_root must be a path or None")
    root_value = DEFAULT_WORKTREE_ROOT if worktree_root is None else worktree_root
    root = Path(root_value).expanduser().resolve()
    repository_name = _safe_name(preparation.source_repository.name)
    execution_root = root / (f"awm-run-{repository_name}-{uuid.uuid4().hex[:12]}")
    before = _inspect_candidate(
        preparation.source_repository,
        execution_root,
        preparation.remote,
        preparation.base_branch,
        command_timeout_seconds,
    )
    if before["registered"] or before["path_exists"]:
        raise WorkerFailure(
            f"fresh worktree path unexpectedly exists: {execution_root}"
        )

    def dispatch() -> None:
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fetched = subprocess.run(
                [
                    "git",
                    "-C",
                    str(preparation.source_repository),
                    "fetch",
                    "--no-tags",
                    preparation.remote,
                    f"+refs/heads/{preparation.base_branch}:"
                    f"refs/remotes/{preparation.remote}/{preparation.base_branch}",
                ],
                capture_output=True,
                text=True,
                timeout=command_timeout_seconds,
                check=False,
            )
            if fetched.returncode != 0:
                detail = fetched.stderr.strip() or fetched.stdout.strip() or "no output"
                raise AuthoritativeMutationRejection(
                    f"Git remote-base fetch exited {fetched.returncode}: {detail}"
                )
            try:
                fetched_sha = _git_read(
                    preparation.source_repository,
                    [
                        "rev-parse",
                        "--verify",
                        f"refs/remotes/{preparation.remote}/{preparation.base_branch}^{{commit}}",
                    ],
                    command_timeout_seconds,
                ).lower()
            except WorkerFailure as exc:
                raise AuthoritativeMutationRejection(
                    "fetched remote base could not be verified after Git quiesced"
                ) from exc
            if fetched_sha != preparation.base_sha:
                raise AuthoritativeMutationRejection(
                    f"remote base changed during preparation: expected "
                    f"{preparation.base_sha}, fetched {fetched_sha}"
                )
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(preparation.source_repository),
                    "worktree",
                    "add",
                    "--detach",
                    str(execution_root),
                    preparation.base_sha,
                ],
                capture_output=True,
                text=True,
                timeout=command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AuthoritativeMutationRejection(
                "Git worktree creation timed out after the Git process was reaped"
            ) from exc
        except OSError as exc:
            raise PreDispatchFailure(
                f"could not execute Git worktree creation: {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
            raise AuthoritativeMutationRejection(
                f"Git worktree creation exited {completed.returncode}: {detail}"
            )
        try:
            created = observe()
        except WorkerFailure as exc:
            raise AuthoritativeMutationRejection(
                "created worktree could not be verified after Git quiesced"
            ) from exc
        if not desired(created):
            raise AuthoritativeMutationRejection(
                f"Git worktree creation returned without its postcondition: {created!r}"
            )

    def observe() -> dict[str, object]:
        return _inspect_candidate(
            preparation.source_repository,
            execution_root,
            preparation.remote,
            preparation.base_branch,
            command_timeout_seconds,
        )

    def desired(state: dict[str, object]) -> bool:
        return (
            state["registered"] is True
            and state["path_exists"] is True
            and state["head"] == preparation.base_sha
            and state["branch"] == "HEAD"
        )

    def reconcile(quiescent: bool) -> Reconciliation[None]:
        state = observe()
        if desired(state):
            return Reconciliation(MutationResolution.DESIRED)
        if quiescent:
            if state == before:
                return Reconciliation(MutationResolution.REJECTED)
            return Reconciliation(
                MutationResolution.CONFLICT,
                detail=f"worktree preparation changed to {state!r}",
            )
        return Reconciliation(MutationResolution.UNKNOWN)

    execute_mutation(
        operation="create isolated Git worktree",
        target=str(execution_root),
        pre_state=before,
        dispatch=dispatch,
        reconcile=reconcile,
        plan={
            "kind": "git_worktree_add",
            "repository": str(preparation.source_repository),
            "remoteBase": preparation.base_ref,
            "baseSha": preparation.base_sha,
            "path": str(execution_root),
            "detached": True,
        },
    )
    verified = observe()
    if not desired(verified):
        raise WorkerFailure(
            f"created worktree failed postcondition verification: {verified!r}"
        )

    git_file = execution_root / ".git"
    metadata = {
        "repository": str(preparation.source_repository),
        "source_repository": str(preparation.source_repository),
        "remote": preparation.remote,
        "base_branch": preparation.base_branch,
        "base_ref": preparation.base_ref,
        "base_sha": preparation.base_sha,
        "path_identity": _path_identity(execution_root),
        "git_file_identity": _administrative_identity(git_file),
        "git_dir": _git_read(
            execution_root,
            ["rev-parse", "--absolute-git-dir"],
            command_timeout_seconds,
        ),
        "head": preparation.base_sha,
        "branch": "HEAD",
    }
    register_run_resource("git_worktree", str(execution_root), metadata)
    emit_finding(
        "git",
        f"prepared {preparation.base_ref} at {preparation.base_sha} in {execution_root}",
    )
    return RepositoryExecutionContext(
        source_repository=preparation.source_repository,
        remote=preparation.remote,
        base_branch=preparation.base_branch,
        base_ref=preparation.base_ref,
        base_sha=preparation.base_sha,
        execution_root=execution_root,
    )


def _resume_context(
    *,
    repo: str | os.PathLike[str],
    base_branch: str,
    remote: str,
    command_timeout_seconds: float,
) -> RepositoryExecutionContext | None:
    encoded = os.environ.get(REPOSITORY_CONTEXT_ENV)
    if encoded is None:
        return None
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise WorkerFailure("Runner supplied an invalid repository context") from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise WorkerFailure("Runner supplied an invalid repository context")
    required = {
        "repository",
        "remote",
        "base_branch",
        "base_ref",
        "base_sha",
        "execution_root",
        "path_identity",
        "git_file_identity",
        "git_dir",
    }
    if not required.issubset(value):
        raise WorkerFailure("Runner supplied an incomplete repository context")
    requested = Path(repo).expanduser().resolve()
    source = Path(value["repository"])
    execution_root = Path(value["execution_root"])
    requested_source = Path(
        _git_read(
            requested,
            ["rev-parse", "--show-toplevel"],
            command_timeout_seconds,
        )
    ).resolve()
    if (
        requested_source != source
        or remote != value["remote"]
        or base_branch != value["base_branch"]
    ):
        raise WorkerFailure(
            "repository preparation conflicts with the retained run context"
        )
    if _path_identity(execution_root) != value["path_identity"]:
        raise WorkerFailure("retained worktree path identity changed")
    if _administrative_identity(execution_root / ".git") != value["git_file_identity"]:
        raise WorkerFailure("retained worktree administrative identity changed")
    listed = _git_read(
        source, ["worktree", "list", "--porcelain"], command_timeout_seconds
    )
    if not any(line == f"worktree {execution_root}" for line in listed.splitlines()):
        raise WorkerFailure("retained worktree is no longer registered")
    git_dir = _git_read(
        execution_root,
        ["rev-parse", "--absolute-git-dir"],
        command_timeout_seconds,
    )
    if git_dir != value["git_dir"]:
        raise WorkerFailure("retained worktree Git identity changed")
    emit_finding(
        "git",
        f"reused retained execution worktree {execution_root} from {value['base_sha']}",
    )
    return RepositoryExecutionContext(
        source_repository=source,
        remote=value["remote"],
        base_branch=value["base_branch"],
        base_ref=value["base_ref"],
        base_sha=value["base_sha"],
        execution_root=execution_root,
    )


def _inspect_candidate(
    repository: Path,
    worktree: Path,
    remote: str,
    base_branch: str,
    timeout: float,
) -> dict[str, object]:
    listed = _git_read(repository, ["worktree", "list", "--porcelain"], timeout)
    registered = any(line == f"worktree {worktree}" for line in listed.splitlines())
    path_exists = os.path.lexists(worktree)
    head: str | None = None
    branch: str | None = None
    if registered and worktree.is_dir():
        head = _git_read(worktree, ["rev-parse", "HEAD"], timeout).lower()
        branch = _git_read(
            worktree, ["rev-parse", "--symbolic-full-name", "HEAD"], timeout
        )
    return {
        "registered": registered,
        "path_exists": path_exists,
        "head": head,
        "branch": branch,
        "tracking_sha": _git_optional_read(
            repository,
            [
                "rev-parse",
                "--verify",
                f"refs/remotes/{remote}/{base_branch}^{{commit}}",
            ],
            timeout,
        ),
    }


def _git_read(repository: Path, args: list[str], timeout: float) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkerFailure(f"read-only Git {args[0]} timed out") from exc
    except OSError as exc:
        raise WorkerFailure(f"could not execute Git {args[0]}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise WorkerFailure(f"Git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _git_optional_read(repository: Path, args: list[str], timeout: float) -> str | None:
    try:
        return _git_read(repository, args, timeout).lower()
    except WorkerFailure:
        return None


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return safe[:48] or "repository"


def _path_identity(path: Path) -> str:
    state = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(state.st_mode):
        raise WorkerFailure(f"worktree path is not a directory: {path}")
    return f"{state.st_dev}:{state.st_ino}"


def _administrative_identity(path: Path) -> str:
    state = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(state.st_mode):
        raise WorkerFailure(f"worktree administrative file is invalid: {path}")
    return f"{state.st_dev}:{state.st_ino}:{state.st_ctime_ns}"
