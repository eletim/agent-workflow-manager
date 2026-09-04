from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from purplemux_client import GitRepository, MutationOutcomeUnknown, WorkerFailure
from purplemux_client.git import (
    _QuiescentMutationTimeout,
    _run_git_mutation_process_group,
)


class RecordingGitRunner:
    def __init__(self, origin_slug: str = "acme/project") -> None:
        self.origin_slug = origin_slug
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        command = list(args)
        self.calls.append(command)
        if command[1:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(
                command, 0, f"https://github.com/{self.origin_slug}.git\n", ""
            )
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
        )


class RefRaceGitRunner(RecordingGitRunner):
    def __init__(self, branch: str, race: Callable[[], None]) -> None:
        super().__init__()
        self.branch = branch
        self.race = race
        self.triggered = False

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        completed = super().__call__(
            args,
            cwd=cwd,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
        )
        if not self.triggered and list(args[1:]) == [
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{self.branch}",
        ]:
            self.triggered = True
            self.race()
        return completed


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test")
    (seed / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(seed, "add", "tracked.txt")
    git(seed, "commit", "-m", "base")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")
    git(tmp_path, "clone", "-b", "main", str(remote), str(work))
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    return remote, seed, work


def open_repo(work: Path, runner: RecordingGitRunner) -> GitRepository:
    return GitRepository.open(work, expected_github_slug="acme/project", runner=runner)


def test_safe_synchronize_prepare_and_read_only_require_pushed(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    runner = RecordingGitRunner()
    repo = open_repo(work, runner)

    integration = repo.synchronize_branch("main")
    feature = repo.prepare_feature_branch(
        "feature/65", base="main", expected_base_sha=integration.remote_sha or ""
    )

    assert feature.current
    assert feature.local_sha == integration.remote_sha
    before = len(runner.calls)
    git(work, "push", "-u", "origin", "feature/65")
    pushed = repo.require_pushed("feature/65")
    assert pushed.local_sha == pushed.remote_sha
    assert all(call[1] != "fetch" for call in runner.calls[before:])
    assert not any(
        forbidden in call
        for call in runner.calls
        for forbidden in ("reset", "rebase", "checkout", "--force", "-f")
    )


def test_prepare_feature_from_detached_exact_base_without_switching_source(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _remote, _seed, work = repositories
    base_sha = git(work, "rev-parse", "HEAD")
    isolated = tmp_path / "awm-run-isolated"
    git(work, "worktree", "add", "--detach", str(isolated), base_sha)
    repo = open_repo(isolated, RecordingGitRunner())

    feature = repo.prepare_feature_branch(
        "feature/isolated", base="main", expected_base_sha=base_sha
    )

    assert feature.current
    assert feature.local_sha == base_sha
    assert git(isolated, "branch", "--show-current") == "feature/isolated"
    assert git(work, "branch", "--show-current") == "main"


def test_synchronize_fast_forwards_but_rejects_ahead_and_dirty(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, seed, work = repositories
    runner = RecordingGitRunner()
    repo = open_repo(work, runner)
    (seed / "remote.txt").write_text("remote\n", encoding="utf-8")
    git(seed, "add", "remote.txt")
    git(seed, "commit", "-m", "remote")
    git(seed, "push", "origin", "main")

    synchronized = repo.synchronize_branch("main")
    assert synchronized.local_sha == git(seed, "rev-parse", "HEAD")

    (work / "local.txt").write_text("local\n", encoding="utf-8")
    git(work, "add", "local.txt")
    git(work, "commit", "-m", "local")
    with pytest.raises(WorkerFailure, match="ahead"):
        repo.synchronize_branch("main")


def test_synchronize_tracks_absent_branch_and_rejects_divergence(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    git(seed, "switch", "-c", "dev/v0.1.4")
    git(seed, "push", "-u", "origin", "dev/v0.1.4")

    tracked = repo.synchronize_branch("dev/v0.1.4")
    assert tracked.current
    assert tracked.local_sha == tracked.remote_sha

    (work / "local-divergence.txt").write_text("local\n", encoding="utf-8")
    git(work, "add", "local-divergence.txt")
    git(work, "commit", "-m", "local divergence")
    (seed / "remote-divergence.txt").write_text("remote\n", encoding="utf-8")
    git(seed, "add", "remote-divergence.txt")
    git(seed, "commit", "-m", "remote divergence")
    git(seed, "push", "origin", "dev/v0.1.4")

    with pytest.raises(WorkerFailure, match="diverged"):
        repo.synchronize_branch("dev/v0.1.4")

    (work / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(WorkerFailure, match="clean"):
        repo.synchronize_branch("main")


def test_prepare_rejects_stale_base_divergence_and_identity_change(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, seed, work = repositories
    runner = RecordingGitRunner()
    repo = open_repo(work, runner)
    integration = repo.synchronize_branch("main")
    old_sha = integration.local_sha or ""
    (seed / "next.txt").write_text("next\n", encoding="utf-8")
    git(seed, "add", "next.txt")
    git(seed, "commit", "-m", "next")
    git(seed, "push", "origin", "main")

    with pytest.raises(WorkerFailure, match="remote base.*changed"):
        repo.prepare_feature_branch(
            "feature/stale", base="main", expected_base_sha=old_sha
        )

    runner.origin_slug = "acme/other"
    with pytest.raises(WorkerFailure, match="resolves to"):
        repo.inspect_worktree()


def test_stale_existing_feature_is_rejected_before_switch(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    old_base = repo.synchronize_branch("main").local_sha or ""
    git(work, "branch", "feature/stale", old_base)
    (seed / "advanced.txt").write_text("advanced\n", encoding="utf-8")
    git(seed, "add", "advanced.txt")
    git(seed, "commit", "-m", "advance base")
    git(seed, "push", "origin", "main")
    new_base = repo.synchronize_branch("main").local_sha or ""

    with pytest.raises(WorkerFailure, match="does not contain base"):
        repo.prepare_feature_branch(
            "feature/stale", base="main", expected_base_sha=new_base
        )
    assert git(work, "branch", "--show-current") == "main"


def test_prepare_rechecks_base_before_creating_feature_branch(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, seed, work = repositories
    branch = "feature/base-race"
    initial_sha = git(work, "rev-parse", "HEAD")

    def advance_base() -> None:
        (seed / "racing-base.txt").write_text("advanced\n", encoding="utf-8")
        git(seed, "add", "racing-base.txt")
        git(seed, "commit", "-m", "advance base during preparation")
        git(seed, "push", "origin", "main")

    runner = RefRaceGitRunner(branch, advance_base)
    repo = open_repo(work, runner)

    with pytest.raises(WorkerFailure, match="remote base.*changed before"):
        repo.prepare_feature_branch(branch, base="main", expected_base_sha=initial_sha)

    assert runner.triggered
    assert git(work, "branch", "--show-current") == "main"
    assert git(work, "branch", "--list", branch) == ""
    assert git(work, "rev-parse", "HEAD") == initial_sha


def test_prepare_rechecks_absent_remote_feature_before_creating_local_branch(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, seed, work = repositories
    branch = "feature/appearance-race"
    initial_sha = git(work, "rev-parse", "HEAD")

    def publish_feature() -> None:
        git(seed, "branch", branch)
        git(seed, "push", "origin", branch)

    runner = RefRaceGitRunner(branch, publish_feature)
    repo = open_repo(work, runner)

    with pytest.raises(WorkerFailure, match="remote feature.*changed before"):
        repo.prepare_feature_branch(branch, base="main", expected_base_sha=initial_sha)

    assert runner.triggered
    assert git(work, "branch", "--show-current") == "main"
    assert git(work, "branch", "--list", branch) == ""
    assert git(work, "rev-parse", "HEAD") == initial_sha


def test_advance_after_merge_requires_exact_remote_and_containment(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    before = repo.synchronize_branch("main").local_sha or ""
    (seed / "merged.txt").write_text("merged\n", encoding="utf-8")
    git(seed, "add", "merged.txt")
    git(seed, "commit", "-m", "merge result")
    merged = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "origin", "main")

    result = repo.advance_after_merge(
        "main",
        previous_sha=before,
        merge_commit_sha=merged,
        required_commit_sha=merged,
    )
    assert result.local_sha == merged

    with pytest.raises(WorkerFailure, match="expected merge commit"):
        repo.advance_after_merge(
            "main",
            previous_sha=merged,
            merge_commit_sha="0" * 40,
            required_commit_sha=merged,
        )


def test_local_mutation_timeout_kills_process_group_before_confirming_rejection(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    git(work, "config", "alias.block", "!sleep 60")
    repo = GitRepository(
        root=work,
        remote="origin",
        expected_github_slug="acme/project",
        command_timeout_seconds=0.05,
        runner=subprocess.run,
    )
    started = time.monotonic()

    with pytest.raises(WorkerFailure, match="confirmed_rejected") as raised:
        repo._git_mutation(
            ["block"],
            operation="blocking test mutation",
            target="test",
            pre_state="unchanged",
            observe=lambda: "unchanged",
            desired=lambda: False,
        )

    assert not isinstance(raised.value, MutationOutcomeUnknown)
    assert time.monotonic() - started < 2


def test_shared_git_mutation_timeout_kills_surviving_descendant(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _remote, _seed, work = repositories
    marker = tmp_path / "descendant-survived"
    git(
        work,
        "config",
        "alias.descendant",
        f"!sh -c '(sleep 0.4; touch {marker}) & wait'",
    )

    with pytest.raises(_QuiescentMutationTimeout):
        _run_git_mutation_process_group(["descendant"], cwd=work, timeout=0.05)

    time.sleep(0.5)
    assert not marker.exists()


def test_unproven_local_timeout_with_unchanged_state_is_unknown(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories

    def timeout_runner(*_args: object, **_kwargs: object):
        raise subprocess.TimeoutExpired(["git"], 0.05)

    repo = GitRepository(
        root=work,
        remote="origin",
        expected_github_slug="acme/project",
        command_timeout_seconds=0.05,
        runner=timeout_runner,  # type: ignore[arg-type]
    )
    with pytest.raises(MutationOutcomeUnknown, match="unknown"):
        repo._git_mutation(
            ["switch", "main"],
            operation="unproven timeout",
            target="main",
            pre_state="unchanged",
            observe=lambda: "unchanged",
            desired=lambda: False,
        )


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_local_mutation_external_interruption_quiesces_before_reconciliation(
    repositories: tuple[Path, Path, Path], signum: signal.Signals
) -> None:
    _remote, _seed, work = repositories
    git(work, "config", "alias.block", "!sleep 60")
    repo = GitRepository(
        root=work,
        remote="origin",
        expected_github_slug="acme/project",
        command_timeout_seconds=5,
        runner=subprocess.run,
    )
    interrupter = threading.Timer(0.1, os.kill, args=(os.getpid(), signum))
    interrupter.start()
    started = time.monotonic()
    try:
        with pytest.raises(WorkerFailure, match="confirmed_rejected") as raised:
            repo._git_mutation(
                ["block"],
                operation="interrupted test mutation",
                target="test",
                pre_state="unchanged",
                observe=lambda: "unchanged",
                desired=lambda: False,
            )
    finally:
        interrupter.cancel()
        interrupter.join()

    assert not isinstance(raised.value, MutationOutcomeUnknown)
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), InterruptedError()])
def test_custom_runner_interruption_after_apply_reconciles_desired_state(
    repositories: tuple[Path, Path, Path], interruption: BaseException
) -> None:
    _remote, _seed, work = repositories
    state = {"value": "before"}
    calls = 0

    def interrupted_after_apply(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        state["value"] = "after"
        raise interruption

    repo = GitRepository(
        root=work,
        remote="origin",
        expected_github_slug="acme/project",
        command_timeout_seconds=1,
        runner=interrupted_after_apply,  # type: ignore[arg-type]
    )

    repo._git_mutation(
        ["switch", "main"],
        operation="custom runner interruption",
        target="main",
        pre_state="before",
        observe=lambda: state["value"],
        desired=lambda: state["value"] == "after",
    )
    assert calls == 1


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), InterruptedError()])
def test_custom_runner_interruption_with_unchanged_state_is_unknown(
    repositories: tuple[Path, Path, Path], interruption: BaseException
) -> None:
    _remote, _seed, work = repositories
    calls = 0

    def interrupted_without_apply(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        raise interruption

    repo = GitRepository(
        root=work,
        remote="origin",
        expected_github_slug="acme/project",
        command_timeout_seconds=1,
        runner=interrupted_without_apply,  # type: ignore[arg-type]
    )

    with pytest.raises(MutationOutcomeUnknown, match="unknown"):
        repo._git_mutation(
            ["switch", "main"],
            operation="custom runner interruption",
            target="main",
            pre_state="before",
            observe=lambda: "before",
            desired=lambda: False,
        )
    assert calls == 1
