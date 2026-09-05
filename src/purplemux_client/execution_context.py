from __future__ import annotations

import os
import re
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from purplemux_client.errors import WorkerFailure
from purplemux_client.git import (
    _QuiescentMutationInterruption,
    _QuiescentMutationTimeout,
    _run_git_mutation_process_group,
)
from purplemux_client.operations import (
    AuthoritativeMutationRejection,
    MutationResolution,
    PreDispatchFailure,
    Reconciliation,
    execute_mutation,
)
from purplemux_client.progress import acknowledge_run_resource, emit_finding

DEFAULT_WORKTREE_ROOT = Path("~/.local/share/agent-workflow-manager/worktrees")
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
    cwd: Path | None = None,
) -> RepositoryPreparation:
    """Validate static declarations with an authoritative read-only remote lookup."""

    return _inspect_run_repository(
        repo=repo,
        base_branch=base_branch,
        remote=remote,
        command_timeout_seconds=command_timeout_seconds,
        live_remote=True,
        resolution_base=cwd,
    )


def _inspect_run_repository(
    *,
    repo: str | os.PathLike[str],
    base_branch: str,
    remote: str,
    command_timeout_seconds: float,
    live_remote: bool,
    resolution_base: Path | None = None,
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
        requested = Path(repo).expanduser()
        if not requested.is_absolute() and resolution_base is not None:
            requested = resolution_base / requested
        requested = requested.resolve()
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

    if worktree_root is not None and not isinstance(worktree_root, (str, os.PathLike)):
        raise TypeError("worktree_root must be a path or None")
    root_value = DEFAULT_WORKTREE_ROOT if worktree_root is None else worktree_root
    root = Path(root_value).expanduser().resolve()
    preparation = inspect_run_repository(
        repo=repo,
        base_branch=base_branch,
        remote=remote,
        command_timeout_seconds=command_timeout_seconds,
    )
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

    pending_metadata = {
        "registration_state": "pending",
        "repository": str(preparation.source_repository),
        "source_repository": str(preparation.source_repository),
        "remote": preparation.remote,
        "base_branch": preparation.base_branch,
        "base_ref": preparation.base_ref,
        "base_sha": preparation.base_sha,
    }
    acknowledge_run_resource(
        "pending", "git_worktree", str(execution_root), pending_metadata
    )

    def dispatch() -> None:
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fetched = _run_git_mutation_process_group(
                [
                    "fetch",
                    "--no-tags",
                    preparation.remote,
                    f"+refs/heads/{preparation.base_branch}:"
                    f"refs/remotes/{preparation.remote}/{preparation.base_branch}",
                ],
                cwd=preparation.source_repository,
                timeout=command_timeout_seconds,
            )
            if fetched.returncode != 0:
                detail = fetched.stderr.strip() or fetched.stdout.strip() or "no output"
                raise AuthoritativeMutationRejection(
                    f"Git remote-base fetch exited {fetched.returncode}: {detail}"
                )
            try:
                verification_ref = (
                    f"refs/remotes/{preparation.remote}/{preparation.base_branch}"
                )
                fetched_sha = _git_read(
                    preparation.source_repository,
                    ["rev-parse", "--verify", f"{verification_ref}^{{commit}}"],
                    command_timeout_seconds,
                ).lower()
            except WorkerFailure as exc:
                raise AuthoritativeMutationRejection(
                    "reserved base could not be verified after Git quiesced"
                ) from exc
            if fetched_sha != preparation.base_sha:
                raise AuthoritativeMutationRejection(
                    f"repository base changed during preparation: expected "
                    f"{preparation.base_sha}, fetched {fetched_sha}"
                )
            completed = _run_git_mutation_process_group(
                [
                    "worktree",
                    "add",
                    "--detach",
                    str(execution_root),
                    preparation.base_sha,
                ],
                cwd=preparation.source_repository,
                timeout=command_timeout_seconds,
            )
        except _QuiescentMutationTimeout as exc:
            raise AuthoritativeMutationRejection(
                "Git repository preparation timed out after process-group quiescence"
            ) from exc
        except _QuiescentMutationInterruption as exc:
            raise AuthoritativeMutationRejection(
                "Git repository preparation was interrupted after process-group "
                "quiescence"
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

    return _finalize_preparation(
        preparation,
        execution_root,
        command_timeout_seconds,
        finding="prepared",
    )


def _finalize_preparation(
    preparation: RepositoryPreparation,
    execution_root: Path,
    command_timeout_seconds: float,
    *,
    finding: str,
) -> RepositoryExecutionContext:
    git_file = execution_root / ".git"
    metadata = {
        "registration_state": "verified",
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
    acknowledge_run_resource("verified", "git_worktree", str(execution_root), metadata)
    emit_finding(
        "git",
        f"{finding} {preparation.base_ref} at {preparation.base_sha} "
        f"in {execution_root}",
    )
    return RepositoryExecutionContext(
        source_repository=preparation.source_repository,
        remote=preparation.remote,
        base_branch=preparation.base_branch,
        base_ref=preparation.base_ref,
        base_sha=preparation.base_sha,
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
