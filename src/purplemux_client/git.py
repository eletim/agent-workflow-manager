from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from purplemux_client.client import WorkerFailure
from purplemux_client.operations import (
    AuthoritativeMutationRejection,
    MutationResolution,
    PossibleDispatchFailure,
    PreDispatchFailure,
    Reconciliation,
    execute_mutation,
)

_OBJECT_ID_RE = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")


class GitCommandRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class WorktreeState:
    root: Path
    current_branch: str | None
    dirty: bool
    status: tuple[str, ...]


@dataclass(frozen=True)
class BranchState:
    name: str
    local_sha: str | None
    remote_sha: str | None
    current: bool


@dataclass(frozen=True)
class FeaturePreparationState:
    branch: BranchState
    base: BranchState
    expected_base_sha: str | None
    base_is_ancestor: bool | None
    action: str


def github_origin_slug(origin: str) -> str:
    path: str
    if origin.startswith("git@github.com:"):
        path = origin.removeprefix("git@github.com:")
    else:
        try:
            parsed = urlsplit(origin)
        except ValueError as exc:
            raise WorkerFailure(f"invalid origin URL: {origin!r}") from exc
        valid = False
        if parsed.scheme == "https":
            valid = (
                parsed.hostname == "github.com"
                and parsed.username is None
                and parsed.password is None
                and parsed.port is None
            )
        elif parsed.scheme == "ssh":
            valid = (
                parsed.hostname == "github.com"
                and parsed.username == "git"
                and parsed.password is None
                and parsed.port is None
            )
        if not valid or parsed.query or parsed.fragment:
            raise WorkerFailure(f"unsupported or non-GitHub origin URL: {origin!r}")
        path = parsed.path.removeprefix("/")
    path = path.removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or any(not _valid_slug_part(part) for part in parts):
        raise WorkerFailure(f"origin has an invalid GitHub repository path: {origin!r}")
    return "/".join(parts)


class GitRepository:
    """Validated Git worktree handle exposing topology-safe operations only."""

    def __init__(
        self,
        *,
        root: Path,
        remote: str,
        expected_github_slug: str,
        command_timeout_seconds: float,
        runner: GitCommandRunner,
    ) -> None:
        self.root = root
        self.remote = remote
        self.expected_github_slug = expected_github_slug
        self.command_timeout_seconds = command_timeout_seconds
        self._runner = runner
        self._owns_process_group = runner is subprocess.run

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        remote: str = "origin",
        expected_github_slug: str,
        command_timeout_seconds: float = 30.0,
        runner: GitCommandRunner = subprocess.run,
    ) -> GitRepository:
        if not remote or "\0" in remote or remote.startswith("-"):
            raise ValueError("remote must be a non-empty name")
        _require_slug(expected_github_slug)
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise WorkerFailure(f"repository directory does not exist: {root}")
        repository = cls(
            root=root,
            remote=remote,
            expected_github_slug=expected_github_slug,
            command_timeout_seconds=command_timeout_seconds,
            runner=runner,
        )
        repository._validate_identity()
        return repository

    def inspect_worktree(self) -> WorktreeState:
        self._validate_identity()
        output = self._read(["status", "--porcelain=v1", "--untracked-files=all"])
        branch = self._current_branch()
        lines = tuple(line for line in output.splitlines() if line)
        return WorktreeState(self.root, branch, bool(lines), lines)

    def require_clean(self) -> None:
        state = self.inspect_worktree()
        if state.dirty:
            raise WorkerFailure(
                f"worktree must be clean: {len(state.status)} change(s)"
            )

    def inspect_branch(self, branch: str) -> BranchState:
        self._validate_identity()
        self._validate_branch(branch)
        return self._inspect_branch(branch)

    def inspect_feature_preparation(
        self,
        branch: str,
        *,
        base: str,
        expected_base_sha: str | None = None,
    ) -> FeaturePreparationState:
        self._validate_identity()
        self._validate_distinct_branches(branch, base)
        if expected_base_sha is not None:
            self._validate_sha(expected_base_sha)
        feature = self._inspect_branch(branch)
        base_state = self._inspect_branch(base)
        if base_state.remote_sha is None:
            raise WorkerFailure(f"remote base branch {base!r} does not exist")
        if expected_base_sha is not None and base_state.remote_sha != expected_base_sha:
            raise WorkerFailure(
                f"remote base {base!r} changed: expected {expected_base_sha}, "
                f"found {base_state.remote_sha}"
            )
        ancestor: bool | None = None
        if feature.local_sha is not None and self._has_commit(base_state.remote_sha):
            ancestor = self._is_ancestor(base_state.remote_sha, feature.local_sha)
        if feature.local_sha is None and feature.remote_sha is None:
            action = "create"
        elif feature.local_sha is None:
            action = "track"
        elif feature.remote_sha is None or feature.local_sha == feature.remote_sha:
            action = "switch"
        elif not self._has_commit(feature.remote_sha):
            action = "refresh_required"
        elif self._is_ancestor(feature.local_sha, feature.remote_sha):
            action = "fast_forward"
        elif self._is_ancestor(feature.remote_sha, feature.local_sha):
            action = "switch_local_ahead"
        else:
            action = "reject_diverged"
        return FeaturePreparationState(
            feature, base_state, expected_base_sha, ancestor, action
        )

    def require_current_branch(self, branch: str) -> BranchState:
        state = self.inspect_branch(branch)
        if not state.current:
            raise WorkerFailure(
                f"current branch is {self._current_branch()!r}, expected {branch!r}"
            )
        return state

    def require_pushed(self, branch: str) -> BranchState:
        self.require_clean()
        state = self.require_current_branch(branch)
        if state.local_sha is None or state.remote_sha is None:
            raise WorkerFailure(
                f"branch {branch!r} is not present locally and remotely"
            )
        if state.local_sha != state.remote_sha:
            raise WorkerFailure(
                f"branch {branch!r} is not fully pushed: local {state.local_sha}, "
                f"remote {state.remote_sha}"
            )
        return state

    def require_contains(self, branch: str, commit_sha: str) -> None:
        self._validate_sha(commit_sha)
        state = self.inspect_branch(branch)
        if state.local_sha is None:
            raise WorkerFailure(f"local branch {branch!r} does not exist")
        if not self._is_ancestor(commit_sha, state.local_sha):
            raise WorkerFailure(f"branch {branch!r} does not contain {commit_sha}")

    def synchronize_branch(self, branch: str) -> BranchState:
        self._validate_identity()
        self._validate_branch(branch)
        self.require_clean()
        authoritative_sha = self._remote_sha(branch)
        if authoritative_sha is None:
            raise WorkerFailure(f"remote branch {branch!r} does not exist")
        self._fetch_branch(branch, authoritative_sha)
        state = self._inspect_branch_from_tracking(branch, authoritative_sha)
        checkout_branch = self._checkout_branch(branch)
        if state.local_sha is None:
            self._git_mutation(
                [
                    "switch",
                    "--track",
                    "-c",
                    checkout_branch,
                    self._tracking_ref(branch),
                ],
                operation="create tracking branch",
                target=branch,
                pre_state=state,
                observe=lambda: self._inspect_branch_from_tracking(
                    branch, authoritative_sha
                ),
                desired=lambda: self._branch_matches(branch, authoritative_sha, True),
            )
        else:
            if state.local_sha != authoritative_sha:
                if self._is_ancestor(state.local_sha, authoritative_sha):
                    pass
                elif self._is_ancestor(authoritative_sha, state.local_sha):
                    raise WorkerFailure(
                        f"local {branch!r} is ahead of its remote; refusing repair"
                    )
                else:
                    raise WorkerFailure(
                        f"local and remote {branch!r} have diverged; refusing repair"
                    )
            if not state.current:
                self._git_mutation(
                    ["switch", checkout_branch],
                    operation="switch branch",
                    target=branch,
                    pre_state=state,
                    observe=lambda: self._inspect_branch_from_tracking(
                        branch, authoritative_sha
                    ),
                    desired=lambda: self._branch_is_current(branch),
                )
            if state.local_sha != authoritative_sha:
                before = self._inspect_branch_from_tracking(branch, authoritative_sha)
                self._git_mutation(
                    ["merge", "--ff-only", self._tracking_ref(branch)],
                    operation="fast-forward branch",
                    target=branch,
                    pre_state=before,
                    observe=lambda: self._inspect_branch_from_tracking(
                        branch, authoritative_sha
                    ),
                    desired=lambda: self._branch_matches(
                        branch, authoritative_sha, True
                    ),
                )
        result = self.inspect_branch(branch)
        if not result.current or result.local_sha != authoritative_sha:
            raise WorkerFailure(
                f"branch {branch!r} synchronization postcondition failed"
            )
        self.require_clean()
        return result

    def prepare_feature_branch(
        self,
        branch: str,
        *,
        base: str,
        expected_base_sha: str,
    ) -> BranchState:
        self._validate_identity()
        self._validate_distinct_branches(branch, base)
        self._validate_sha(expected_base_sha)
        self.require_clean()
        base_state = self._inspect_branch(base)
        if base_state.remote_sha != expected_base_sha:
            raise WorkerFailure(
                f"remote base {base!r} changed: expected {expected_base_sha}, "
                f"found {base_state.remote_sha}"
            )
        detached_at_base = (
            self._current_branch() is None
            and self._read(["rev-parse", "HEAD"]) == expected_base_sha
        )
        if (
            not (base_state.local_sha == expected_base_sha and base_state.current)
            and not detached_at_base
        ):
            raise WorkerFailure(
                f"base {base!r} must be synchronized at {expected_base_sha}, or "
                "the worktree must be detached at that exact commit"
            )
        feature_remote = self._remote_sha(branch)

        def recheck_authoritative_refs() -> None:
            self._require_feature_preparation_refs(
                branch,
                base=base,
                expected_base_sha=expected_base_sha,
                expected_feature_sha=feature_remote,
            )

        if feature_remote is not None:
            self._fetch_branch(
                branch,
                feature_remote,
                pre_dispatch=recheck_authoritative_refs,
            )
        state = self._inspect_branch_from_tracking(branch, feature_remote)
        checkout_branch = self._checkout_branch(branch)
        if state.local_sha is None and feature_remote is None:
            self._git_mutation(
                ["switch", "-c", checkout_branch, expected_base_sha],
                operation="create feature branch",
                target=branch,
                pre_state=state,
                observe=lambda: self._inspect_branch_from_tracking(branch, None),
                desired=lambda: self._branch_matches(branch, expected_base_sha, True),
                pre_dispatch=recheck_authoritative_refs,
            )
        elif state.local_sha is None:
            assert feature_remote is not None
            if not self._is_ancestor(expected_base_sha, feature_remote):
                raise WorkerFailure(
                    f"feature branch {branch!r} does not contain base "
                    f"{expected_base_sha}"
                )
            self._git_mutation(
                [
                    "switch",
                    "--track",
                    "-c",
                    checkout_branch,
                    self._tracking_ref(branch),
                ],
                operation="create tracking feature branch",
                target=branch,
                pre_state=state,
                observe=lambda: self._inspect_branch_from_tracking(
                    branch, feature_remote
                ),
                desired=lambda: self._branch_matches(branch, feature_remote, True),
                pre_dispatch=recheck_authoritative_refs,
            )
        else:
            if feature_remote is not None and state.local_sha != feature_remote:
                if self._is_ancestor(state.local_sha, feature_remote):
                    needs_fast_forward = True
                elif self._is_ancestor(feature_remote, state.local_sha):
                    needs_fast_forward = False
                else:
                    raise WorkerFailure(
                        f"local and remote {branch!r} have diverged; refusing repair"
                    )
            else:
                needs_fast_forward = False
            prepared_sha = feature_remote if needs_fast_forward else state.local_sha
            assert prepared_sha is not None
            if not self._is_ancestor(expected_base_sha, prepared_sha):
                raise WorkerFailure(
                    f"feature branch {branch!r} does not contain base "
                    f"{expected_base_sha}"
                )
            if not state.current:
                self._git_mutation(
                    ["switch", checkout_branch],
                    operation="switch feature branch",
                    target=branch,
                    pre_state=state,
                    observe=lambda: self._inspect_branch_from_tracking(
                        branch, feature_remote
                    ),
                    desired=lambda: self._branch_is_current(branch),
                    pre_dispatch=recheck_authoritative_refs,
                )
            if needs_fast_forward:
                assert feature_remote is not None
                before = self._inspect_branch_from_tracking(branch, feature_remote)
                self._git_mutation(
                    ["merge", "--ff-only", self._tracking_ref(branch)],
                    operation="fast-forward feature branch",
                    target=branch,
                    pre_state=before,
                    observe=lambda: self._inspect_branch_from_tracking(
                        branch, feature_remote
                    ),
                    desired=lambda: self._branch_matches(branch, feature_remote, True),
                    pre_dispatch=recheck_authoritative_refs,
                )
        result = self.inspect_branch(branch)
        if result.local_sha is None or not result.current:
            raise WorkerFailure(f"feature branch {branch!r} preparation failed")
        if result.remote_sha != feature_remote:
            raise WorkerFailure(
                f"remote feature {branch!r} changed during preparation: expected "
                f"{feature_remote}, found {result.remote_sha}"
            )
        if self._remote_sha(base) != expected_base_sha:
            raise WorkerFailure(f"remote base {base!r} changed during preparation")
        if not self._is_ancestor(expected_base_sha, result.local_sha):
            raise WorkerFailure(
                f"feature branch {branch!r} does not contain base {expected_base_sha}"
            )
        self.require_clean()
        return result

    def advance_after_merge(
        self,
        branch: str,
        *,
        previous_sha: str,
        merge_commit_sha: str,
        required_commit_sha: str,
    ) -> BranchState:
        self._validate_identity()
        self._validate_branch(branch)
        self._validate_sha(previous_sha)
        self._validate_sha(merge_commit_sha)
        self._validate_sha(required_commit_sha)
        self.require_clean()
        before = self.require_current_branch(branch)
        if before.local_sha != previous_sha:
            raise WorkerFailure(
                f"local {branch!r} changed: expected {previous_sha}, "
                f"found {before.local_sha}"
            )
        authoritative_sha = self._remote_sha(branch)
        if authoritative_sha != merge_commit_sha:
            raise WorkerFailure(
                f"remote {branch!r} is {authoritative_sha}, expected merge "
                f"commit {merge_commit_sha}"
            )
        self._fetch_branch(branch, merge_commit_sha)
        self._git_mutation(
            ["merge", "--ff-only", self._tracking_ref(branch)],
            operation="advance integration after merge",
            target=branch,
            pre_state=before,
            observe=lambda: self._inspect_branch_from_tracking(
                branch, merge_commit_sha
            ),
            desired=lambda: self._branch_matches(branch, merge_commit_sha, True),
        )
        result = self.inspect_branch(branch)
        if (
            result.local_sha != merge_commit_sha
            or result.remote_sha != merge_commit_sha
        ):
            raise WorkerFailure("integration advancement postcondition failed")
        if previous_sha == merge_commit_sha:
            raise WorkerFailure("integration branch did not advance")
        if not self._is_ancestor(previous_sha, merge_commit_sha):
            raise WorkerFailure("merge commit does not descend from the prior base")
        if not self._is_ancestor(required_commit_sha, merge_commit_sha):
            raise WorkerFailure(
                "advanced integration does not contain the approved head"
            )
        self.require_clean()
        return result

    def _validate_identity(self) -> None:
        root = self._read(["rev-parse", "--show-toplevel"])
        if Path(root).resolve() != self.root:
            raise WorkerFailure(
                f"repository identity changed: expected root {self.root}, found {root}"
            )
        origin = self._read(["remote", "get-url", self.remote])
        actual = github_origin_slug(origin)
        if actual.lower() != self.expected_github_slug.lower():
            raise WorkerFailure(
                f"remote {self.remote!r} resolves to {actual!r}, expected "
                f"{self.expected_github_slug!r}"
            )

    def _validate_branch(self, branch: str) -> None:
        if not branch or "\0" in branch or branch.startswith("-"):
            raise ValueError(f"invalid branch name: {branch!r}")
        completed = self._command(["check-ref-format", "--branch", branch], {0, 1})
        if completed.returncode != 0:
            raise ValueError(f"invalid branch name: {branch!r}")

    def _validate_distinct_branches(self, branch: str, base: str) -> None:
        self._validate_branch(branch)
        self._validate_branch(base)
        if branch == base:
            raise ValueError("feature branch and base branch must differ")

    @staticmethod
    def _validate_sha(sha: str) -> None:
        if not _OBJECT_ID_RE.fullmatch(sha):
            raise ValueError(f"expected a full Git object ID, got {sha!r}")

    def _inspect_branch(self, branch: str) -> BranchState:
        return BranchState(
            name=branch,
            local_sha=self._checkout_sha(branch),
            remote_sha=self._remote_sha(branch),
            current=self._branch_is_current(branch),
        )

    def _inspect_branch_from_tracking(
        self, branch: str, authoritative_sha: str | None
    ) -> BranchState:
        tracking_sha = self._tracking_sha(branch)
        if tracking_sha != authoritative_sha:
            raise WorkerFailure(
                f"tracking ref for {branch!r} is {tracking_sha}, expected "
                f"{authoritative_sha}"
            )
        return BranchState(
            branch,
            self._checkout_sha(branch),
            authoritative_sha,
            self._branch_is_current(branch),
        )

    def _checkout_branch(self, branch: str) -> str:
        private = self._private_branch(branch)
        current = self._current_branch()
        if current == private:
            return private
        owner = self._branch_worktree(branch)
        if owner is not None and owner != self.root:
            return private
        return branch

    def _checkout_sha(self, branch: str) -> str | None:
        return self._local_sha(self._checkout_branch(branch))

    def _branch_is_current(self, branch: str) -> bool:
        return self._current_branch() == self._checkout_branch(branch)

    def _private_branch(self, branch: str) -> str:
        run_identity = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        return f"awm-run/{run_identity}/{branch}"

    def _branch_worktree(self, branch: str) -> Path | None:
        output = self._read(
            [
                "for-each-ref",
                "--format=%(worktreepath)%00",
                f"refs/heads/{branch}",
            ]
        )
        path = output.split("\0", 1)[0]
        return Path(path).resolve() if path else None

    def _local_sha(self, branch: str) -> str | None:
        return self._optional_ref_sha(f"refs/heads/{branch}")

    def _tracking_sha(self, branch: str) -> str | None:
        return self._optional_ref_sha(f"refs/remotes/{self.remote}/{branch}")

    def _optional_ref_sha(self, ref: str) -> str | None:
        completed = self._command(["rev-parse", "--verify", ref], {0, 128})
        return completed.stdout.strip() if completed.returncode == 0 else None

    def _remote_sha(self, branch: str) -> str | None:
        completed = self._command(
            [
                "ls-remote",
                "--exit-code",
                "--refs",
                self.remote,
                f"refs/heads/{branch}",
            ],
            {0, 2},
        )
        if completed.returncode == 2:
            return None
        fields = completed.stdout.strip().split()
        if len(fields) != 2 or fields[1] != f"refs/heads/{branch}":
            raise WorkerFailure(f"unexpected ls-remote result for branch {branch!r}")
        return fields[0]

    def _current_branch(self) -> str | None:
        completed = self._command(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], {0, 1}
        )
        return completed.stdout.strip() if completed.returncode == 0 else None

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        completed = self._command(
            ["merge-base", "--is-ancestor", ancestor, descendant], {0, 1}
        )
        return completed.returncode == 0

    def _has_commit(self, sha: str) -> bool:
        completed = self._command(["cat-file", "-e", f"{sha}^{{commit}}"], {0, 128})
        return completed.returncode == 0

    def _tracking_ref(self, branch: str) -> str:
        return f"refs/remotes/{self.remote}/{branch}"

    def _fetch_branch(
        self,
        branch: str,
        authoritative_sha: str,
        *,
        pre_dispatch: Callable[[], None] | None = None,
    ) -> None:
        before = self._tracking_sha(branch)
        self._git_mutation(
            [
                "fetch",
                "--no-tags",
                self.remote,
                f"refs/heads/{branch}:{self._tracking_ref(branch)}",
            ],
            operation="fetch branch",
            target=f"{self.remote}/{branch}",
            pre_state=before,
            observe=lambda: self._tracking_sha(branch),
            desired=lambda: self._tracking_sha(branch) == authoritative_sha,
            pre_dispatch=pre_dispatch,
        )

    def _require_feature_preparation_refs(
        self,
        branch: str,
        *,
        base: str,
        expected_base_sha: str,
        expected_feature_sha: str | None,
    ) -> None:
        base_sha = self._remote_sha(base)
        if base_sha != expected_base_sha:
            raise WorkerFailure(
                f"remote base {base!r} changed before feature preparation: "
                f"expected {expected_base_sha}, found {base_sha}"
            )
        feature_sha = self._remote_sha(branch)
        if feature_sha != expected_feature_sha:
            raise WorkerFailure(
                f"remote feature {branch!r} changed before feature preparation: "
                f"expected {expected_feature_sha}, found {feature_sha}"
            )

    def _branch_matches(self, branch: str, sha: str, current: bool) -> bool:
        return self._checkout_sha(branch) == sha and (
            not current or self._branch_is_current(branch)
        )

    def _git_mutation(
        self,
        args: Sequence[str],
        *,
        operation: str,
        target: str,
        pre_state: object,
        observe: Callable[[], object],
        desired: Callable[[], bool],
        pre_dispatch: Callable[[], None] | None = None,
    ) -> None:
        def dispatch() -> None:
            if pre_dispatch is not None:
                pre_dispatch()
            try:
                if self._owns_process_group:
                    completed = self._run_mutation_process_group(args)
                else:
                    completed = self._runner(
                        ["git", *args],
                        cwd=self.root,
                        capture_output=True,
                        text=True,
                        timeout=self.command_timeout_seconds,
                        check=False,
                    )
            except _QuiescentMutationTimeout as exc:
                raise AuthoritativeMutationRejection(
                    f"local Git {operation} timed out after process-group quiescence"
                ) from exc
            except _QuiescentMutationInterruption as exc:
                raise AuthoritativeMutationRejection(
                    f"local Git {operation} was interrupted after process-group "
                    "quiescence"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise PossibleDispatchFailure(
                    f"local Git {operation} timed out without quiescence proof"
                ) from exc
            except InterruptedError as exc:
                raise PossibleDispatchFailure(
                    f"local Git {operation} communication was interrupted without "
                    "quiescence proof"
                ) from exc
            except OSError as exc:
                raise PreDispatchFailure(
                    f"could not execute Git {operation}: {exc}"
                ) from exc
            except KeyboardInterrupt as exc:
                raise PossibleDispatchFailure(
                    f"local Git {operation} was interrupted without quiescence proof"
                ) from exc
            if completed.returncode != 0:
                detail = (
                    completed.stderr.strip() or completed.stdout.strip() or "no output"
                )
                raise AuthoritativeMutationRejection(
                    f"local Git {operation} exited {completed.returncode}: {detail}"
                )
            if not desired():
                raise AuthoritativeMutationRejection(
                    f"local Git {operation} returned without its postcondition"
                )

        def reconcile(quiescent: bool) -> Reconciliation[None]:
            if desired():
                return Reconciliation(
                    MutationResolution.DESIRED, None, "postcondition holds"
                )
            if quiescent:
                observed = observe()
                if observed != pre_state:
                    return Reconciliation(
                        MutationResolution.CONFLICT,
                        None,
                        f"process is quiescent but state changed to {observed!r}",
                    )
                return Reconciliation(
                    MutationResolution.REJECTED,
                    None,
                    "process is quiescent and the requested postcondition is absent",
                )
            return Reconciliation(MutationResolution.UNKNOWN)

        execute_mutation(
            operation=operation,
            target=target,
            pre_state=pre_state,
            dispatch=dispatch,
            reconcile=reconcile,
            plan={"argv": ["git", *args]},
        )

    def _run_mutation_process_group(
        self, args: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        return _run_git_mutation_process_group(
            args, cwd=self.root, timeout=self.command_timeout_seconds
        )

    @staticmethod
    def _mutation_signals() -> set[signal.Signals]:
        return {signal.SIGINT, signal.SIGHUP, signal.SIGTERM}

    @classmethod
    def _block_mutation_signals(cls) -> set[signal.Signals] | None:
        if not hasattr(signal, "pthread_sigmask"):
            return None
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, cls._mutation_signals())
        return {signal.Signals(signum) for signum in previous}

    @staticmethod
    def _restore_signal_mask(mask: set[signal.Signals] | None) -> None:
        if mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)

    @staticmethod
    def _install_mutation_signal_handlers() -> dict[signal.Signals, Any]:
        handlers: dict[signal.Signals, Any] = {}
        for signum in GitRepository._mutation_signals():
            try:
                previous = signal.getsignal(signum)
                signal.signal(signum, _raise_mutation_signal)
            except ValueError:
                # Python restricts handler changes to the main thread. Direct
                # BaseException interruptions are still cleaned up below.
                continue
            handlers[signum] = previous
        return handlers

    @staticmethod
    def _restore_mutation_signal_handlers(
        handlers: dict[signal.Signals, Any],
    ) -> None:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)

    @staticmethod
    def _quiesce_process_group(
        process: subprocess.Popen[str], interruption: BaseException
    ) -> None:
        if hasattr(signal, "pthread_sigmask"):
            signal.pthread_sigmask(signal.SIG_BLOCK, GitRepository._mutation_signals())
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=1.0)
        except subprocess.TimeoutExpired as exc:
            raise PossibleDispatchFailure(
                "local Git process group could not be proven quiescent"
            ) from exc
        if process.poll() is None:
            raise PossibleDispatchFailure(
                "local Git process group could not be proven quiescent"
            ) from interruption

    def _read(self, args: Sequence[str]) -> str:
        return self._command(args, {0}).stdout.strip()

    def _command(
        self, args: Sequence[str], allowed_returncodes: set[int]
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._runner(
                ["git", *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=self.command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkerFailure(f"read-only Git {' '.join(args)} timed out") from exc
        except OSError as exc:
            raise WorkerFailure(f"could not execute Git {args[0]}: {exc}") from exc
        if completed.returncode not in allowed_returncodes:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
            raise WorkerFailure(f"Git {' '.join(args)} failed: {detail}")
        return completed


def _valid_slug_part(value: str) -> bool:
    return (
        bool(value)
        and value.isascii()
        and all(character.isalnum() or character in "-_." for character in value)
    )


def _require_slug(slug: str) -> None:
    parts = slug.split("/")
    if len(parts) != 2 or any(not _valid_slug_part(part) for part in parts):
        raise ValueError(f"invalid GitHub repository slug: {slug!r}")


class _QuiescentMutationTimeout(subprocess.TimeoutExpired):
    pass


class _QuiescentMutationInterruption(BaseException):
    def __init__(self, command: Sequence[str], interruption: BaseException) -> None:
        self.command = tuple(command)
        self.interruption = interruption
        super().__init__(f"interrupted by {type(interruption).__name__}")


class _MutationSignal(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"received signal {signum}")


def _raise_mutation_signal(signum: int, _frame: object) -> None:
    raise _MutationSignal(signum)


def _run_git_mutation_process_group(
    args: Sequence[str], *, cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a mutating Git command and quiesce its whole process group on failure."""
    command = ["git", *args]
    previous_mask = GitRepository._block_mutation_signals()
    previous_handlers = GitRepository._install_mutation_signal_handlers()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            GitRepository._restore_signal_mask(previous_mask)
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            GitRepository._quiesce_process_group(process, exc)
            raise _QuiescentMutationTimeout(command, timeout) from exc
        except BaseException as exc:
            GitRepository._quiesce_process_group(process, exc)
            raise _QuiescentMutationInterruption(command, exc) from exc
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        GitRepository._restore_mutation_signal_handlers(previous_handlers)
        GitRepository._restore_signal_mask(previous_mask)
