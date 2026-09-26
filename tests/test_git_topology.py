from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from purplemux_client import (
    GitRepository,
    LocalRefState,
    MutationOutcomeUnknown,
    WorkerFailure,
    agent_commit_coauthor,
)
from purplemux_client.git import (
    _QuiescentMutationTimeout,
    _run_git_mutation_process_group,
    inspect_github_repository,
)


def test_inspect_github_repository_resolves_nested_directory(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    nested = repository / "packages" / "client"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "remote",
            "add",
            "origin",
            "git@github.com:acme/widgets.git",
        ],
        check=True,
    )

    identity = inspect_github_repository(nested)

    assert identity.slug == "acme/widgets"
    assert identity.url == "https://github.com/acme/widgets"


def test_agent_commit_coauthor_is_the_normalized_identity_source() -> None:
    assert agent_commit_coauthor("codex") == "Codex <noreply@openai.com>"
    assert agent_commit_coauthor("claude") == "Claude <noreply@anthropic.com>"
    with pytest.raises(ValueError, match="codex or claude"):
        agent_commit_coauthor("other")


@pytest.mark.parametrize(
    "origin",
    ["https://gitlab.com/acme/widgets.git", "https://example.com/acme/widgets.git"],
)
def test_inspect_github_repository_rejects_non_github_remote(
    tmp_path: Path, origin: str
) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", origin],
        check=True,
    )

    with pytest.raises(WorkerFailure, match="non-GitHub"):
        inspect_github_repository(tmp_path)


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
        env: Mapping[str, str] | None = None,
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
            env=env,
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
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = super().__call__(
            args,
            cwd=cwd,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
            env=env,
        )
        if not self.triggered and list(args[1:]) == [
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{self.branch}",
        ]:
            self.triggered = True
            self.race()
        return completed


class UpdateRefRaceGitRunner(RecordingGitRunner):
    def __init__(
        self,
        branch: str,
        race: Callable[[], None],
        *,
        reject_restore: bool = False,
    ) -> None:
        super().__init__()
        self.branch = branch
        self.race = race
        self.reject_restore = reject_restore
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
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = list(args)
        ref = f"refs/heads/{self.branch}"
        is_branch_update = command[1:3] == ["update-ref", ref]
        if is_branch_update and self.triggered and self.reject_restore:
            return subprocess.CompletedProcess(command, 1, "", "restore rejected")
        completed = super().__call__(
            args,
            cwd=cwd,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
            env=env,
        )
        if is_branch_update and not self.triggered and completed.returncode == 0:
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


def test_open_can_pin_identity_from_the_validated_github_origin(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    runner = RecordingGitRunner("acme/inferred")

    repository = GitRepository.open(work, runner=runner)

    assert repository.expected_github_slug == "acme/inferred"
    assert runner.calls.count(["git", "remote", "get-url", "origin"]) >= 2


def test_require_ancestor_checks_commit_topology(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    ancestor = git(work, "rev-parse", "HEAD")
    (work / "descendant.txt").write_text("descendant\n", encoding="utf-8")
    git(work, "add", "descendant.txt")
    git(work, "commit", "-m", "descendant")
    descendant = git(work, "rev-parse", "HEAD")
    repo = open_repo(work, RecordingGitRunner())

    repo.require_ancestor(ancestor, descendant)

    with pytest.raises(WorkerFailure, match="is not an ancestor"):
        repo.require_ancestor(descendant, ancestor)


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


def test_remote_branch_batch_ignores_stale_tracking_refs(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    stale_sha = git(work, "rev-parse", "refs/remotes/origin/main")
    git(seed, "commit", "--allow-empty", "-m", "advance remote only")
    git(seed, "push", "origin", "main")
    current_sha = git(seed, "rev-parse", "HEAD")

    result = repo.inspect_remote_branches(("main", "feature/missing"))

    assert stale_sha != current_sha
    assert result == {"main": current_sha, "feature/missing": None}
    assert git(work, "rev-parse", "refs/remotes/origin/main") == stale_sha


def test_remote_branch_enumeration_uses_authoritative_remote_heads(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    main_sha = git(seed, "rev-parse", "HEAD")
    git(seed, "branch", "dev/v1.2.3")
    git(seed, "push", "origin", "dev/v1.2.3")
    git(work, "branch", "local-only")

    result = repo.inspect_remote_branch_heads()

    assert result == {"dev/v1.2.3": main_sha, "main": main_sha}
    assert "local-only" not in result


def test_remote_ref_enumeration_includes_non_branch_refs(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, seed, work = repositories
    head = git(seed, "rev-parse", "HEAD")
    git(seed, "tag", "release-test")
    git(seed, "push", "origin", "refs/tags/release-test")

    refs = open_repo(work, RecordingGitRunner()).inspect_remote_refs()

    assert refs["refs/heads/main"] == head
    assert refs["refs/tags/release-test"] == head


def test_local_branch_head_inspection_includes_linked_worktrees(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _remote, _seed, work = repositories
    base = git(work, "rev-parse", "HEAD")
    branch = "feature/linked-head"
    linked = tmp_path / "linked"
    git(work, "worktree", "add", "-b", branch, str(linked), base)
    git(linked, "commit", "--allow-empty", "-m", "linked work")

    heads = open_repo(work, RecordingGitRunner()).inspect_local_branch_heads()

    assert heads["main"] == base
    assert heads[branch] == git(linked, "rev-parse", "HEAD")


def test_local_ref_enumeration_includes_stash_and_non_head_refs(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    head = git(work, "rev-parse", "HEAD")
    git(work, "tag", "local-test")
    (work / "tracked.txt").write_text("stashed\n", encoding="utf-8")
    git(work, "stash", "push", "-m", "recovery guard test")

    refs = open_repo(work, RecordingGitRunner()).inspect_local_refs()

    assert refs["refs/heads/main"] == LocalRefState(head, None)
    assert refs["refs/remotes/origin/main"] == LocalRefState(head, None)
    assert refs["refs/tags/local-test"] == LocalRefState(head, None)
    assert "refs/stash" in refs


def test_local_ref_enumeration_distinguishes_same_oid_symbolic_ref(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    head = git(work, "rev-parse", "HEAD")
    ref = "refs/remotes/origin/recovery-alias"
    git(work, "update-ref", ref, head)
    repo = open_repo(work, RecordingGitRunner())
    direct = repo.inspect_local_refs()[ref]

    git(work, "symbolic-ref", ref, "refs/heads/main")
    symbolic = repo.inspect_local_refs()[ref]

    assert direct == LocalRefState(head, None)
    assert symbolic == LocalRefState(head, "refs/heads/main")
    assert direct.object_sha == symbolic.object_sha
    assert direct != symbolic


def test_remote_notes_persist_recovery_state_without_moving_branches(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    remote, _seed, work = repositories
    other = tmp_path / "other"
    git(tmp_path, "clone", "-b", "main", str(remote), str(other))
    git(other, "config", "user.email", "test@example.com")
    git(other, "config", "user.name", "Test")
    first = open_repo(work, RecordingGitRunner())
    second = open_repo(other, RecordingGitRunner())
    anchor = git(work, "rev-parse", "refs/heads/main")
    branch_before = first.inspect_branch("main").remote_sha
    ref = "refs/notes/agent-workflow-manager/work-item-plan-test"

    assert first.inspect_remote_note(ref, anchor) is None
    assert (
        first.update_remote_note(ref, anchor, "position=0", expected_body=None)
        == "position=0"
    )
    assert second.inspect_remote_note(ref, anchor) == "position=0"
    assert (
        second.update_remote_note(ref, anchor, "position=1", expected_body="position=0")
        == "position=1"
    )
    assert first.inspect_remote_note(ref, anchor) == "position=1"
    assert first.inspect_branch("main").remote_sha == branch_before

    with pytest.raises(WorkerFailure, match="recovery note changed"):
        first.update_remote_note(
            ref, anchor, "stale update", expected_body="position=0"
        )


def test_committed_result_and_delivery_push_absent_or_behind_remote(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    repo.prepare_feature_branch("feature/delivery", base="main", expected_base_sha=base)

    git(work, "commit", "--allow-empty", "-m", "first result")
    first = git(work, "rev-parse", "HEAD")
    committed = repo.require_committed_result("feature/delivery", previous_sha=base)
    assert committed.local_sha == first
    assert committed.remote_sha is None
    assert (
        repo.ensure_pushed("feature/delivery", expected_local_sha=first).remote_sha
        == first
    )

    git(work, "commit", "--allow-empty", "-m", "fix result")
    second = git(work, "rev-parse", "HEAD")
    repo.require_committed_result("feature/delivery", previous_sha=first)
    delivered = repo.ensure_pushed("feature/delivery", expected_local_sha=second)

    assert delivered.local_sha == second
    assert delivered.remote_sha == second
    assert (
        git(work, "ls-remote", "origin", "refs/heads/feature/delivery").split()[0]
        == second
    )


def test_committed_result_requires_new_commit_and_clean_worktree(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    repo.prepare_feature_branch(
        "feature/postcondition", base="main", expected_base_sha=base
    )

    with pytest.raises(WorkerFailure, match="no new commit"):
        repo.require_committed_result("feature/postcondition", previous_sha=base)
    unchanged = repo.require_committed_result(
        "feature/postcondition", previous_sha=base, allow_unchanged=True
    )
    assert unchanged.local_sha == base

    (work / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(WorkerFailure, match="clean"):
        repo.require_committed_result(
            "feature/postcondition", previous_sha=base, allow_unchanged=True
        )


@pytest.mark.parametrize(
    ("agent", "coauthor"),
    [
        ("codex", "Codex <noreply@openai.com>"),
        ("claude", "Claude <noreply@anthropic.com>"),
    ],
)
def test_committed_result_requires_uniform_agent_provenance(
    repositories: tuple[Path, Path, Path], agent: str, coauthor: str
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = f"feature/{agent}-provenance"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(
        work,
        "commit",
        "--allow-empty",
        "-m",
        "agent result",
        "-m",
        f"Co-authored-by: {coauthor}\nAWM-Agent: {agent}\nAWM-Process: implementation",
    )

    result = repo.require_committed_result(
        branch,
        previous_sha=base,
        expected_agent=agent,
        expected_process="implementation",
    )

    assert result.local_sha == git(work, "rev-parse", "HEAD")


def test_committed_result_rejects_missing_agent_provenance(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    repo.prepare_feature_branch(
        "feature/missing-provenance", base="main", expected_base_sha=base
    )
    git(work, "commit", "--allow-empty", "-m", "unattributed result")

    with pytest.raises(WorkerFailure, match="AWM-Agent trailer"):
        repo.require_committed_result(
            "feature/missing-provenance",
            previous_sha=base,
            expected_agent="codex",
            expected_process="implementation",
        )


def test_committed_result_requires_the_expected_process(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/wrong-process"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(
        work,
        "commit",
        "--allow-empty",
        "-m",
        "mislabeled implementation",
        "-m",
        "Co-authored-by: Codex <noreply@openai.com>\n"
        "AWM-Agent: codex\n"
        "AWM-Process: cleanup",
    )

    with pytest.raises(WorkerFailure, match="AWM-Process.*implementation"):
        repo.require_committed_result(
            branch,
            previous_sha=base,
            expected_agent="codex",
            expected_process="implementation",
        )


def test_agent_provenance_verifies_exact_turn_ranges(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/process-boundaries"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(
        work,
        "commit",
        "--allow-empty",
        "-m",
        "implementation",
        "-m",
        "Co-authored-by: Codex <noreply@openai.com>\n"
        "AWM-Agent: codex\n"
        "AWM-Process: implementation",
    )
    implementation = git(work, "rev-parse", "HEAD")
    git(
        work,
        "commit",
        "--allow-empty",
        "-m",
        "cleanup",
        "-m",
        "Co-authored-by: Codex <noreply@openai.com>\n"
        "AWM-Agent: codex\n"
        "AWM-Process: cleanup",
    )
    cleanup = git(work, "rev-parse", "HEAD")

    repo.require_agent_commit_provenance(
        base,
        implementation,
        expected_agent="codex",
        expected_process="implementation",
    )
    repo.require_agent_commit_provenance(
        implementation,
        cleanup,
        expected_agent="codex",
        expected_process="cleanup",
    )
    repo.require_agent_commit_declared_provenance(
        base,
        cleanup,
        expected_agent="codex",
        allowed_processes=("implementation", "reviewer-fix", "cleanup"),
    )

    with pytest.raises(WorkerFailure, match="allowed AWM-Process"):
        repo.require_agent_commit_declared_provenance(
            base,
            cleanup,
            expected_agent="codex",
            allowed_processes=("implementation", "reviewer-fix"),
        )


@pytest.mark.parametrize(
    "trailers",
    [
        (
            "Co-authored-by: Codex <noreply@openai.com>\n\n"
            "AWM-Agent: codex\n\nAWM-Process: implementation"
        ),
        (
            "Co-authored-by: Codex <noreply@openai.com>\n"
            "AWM-Agent: codex\nAWM-Agent: codex\n"
            "AWM-Process: implementation\nAWM-Process: implementation"
        ),
        "Co-authored-by: Codex <noreply@openai.com>",
        "Co-authored-by: Codex\n <noreply@openai.com>",
    ],
    ids=("malformed-spacing", "duplicates", "missing-awm", "folded-coauthor"),
)
def test_normalize_unpublished_agent_provenance(
    repositories: tuple[Path, Path, Path], trailers: str
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/normalize-provenance"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(
        work,
        "commit",
        "--allow-empty",
        "-m",
        "agent result",
        "-m",
        f"Reviewed-by: Reviewer <reviewer@example.com>\n{trailers}",
    )
    original = git(work, "rev-parse", "HEAD")

    normalized = repo.normalize_agent_commit_provenance(
        branch,
        base,
        original,
        expected_agent="codex",
        expected_process="implementation",
    )

    assert normalized.local_sha is not None
    assert normalized.local_sha != original
    expected = {
        "Reviewed-by": ["Reviewer <reviewer@example.com>"],
        "Co-authored-by": ["Codex <noreply@openai.com>"],
        "AWM-Agent": ["codex"],
        "AWM-Process": ["implementation"],
    }
    for key, values in expected.items():
        assert (
            git(
                work,
                "show",
                "-s",
                f"--format=%(trailers:key={key},valueonly)",
                normalized.local_sha,
            ).splitlines()
            == values
        )
    repo.require_agent_commit_provenance(
        base,
        normalized.local_sha,
        expected_agent="codex",
        expected_process="implementation",
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "agent result\n   \nReviewed-by: Reviewer <reviewer@example.com>",
            {"Reviewed-by": ["Reviewer <reviewer@example.com>"]},
        ),
        (
            "agent result\n\nrelease note 1\nrelease note 2\nrelease note 3\n"
            "release note 4\nrelease note 5\nrelease note 6\n"
            "Reviewed-by: Reviewer <reviewer@example.com>\n"
            "Signed-off-by: Developer <developer@example.com>",
            {
                "Reviewed-by": ["Reviewer <reviewer@example.com>"],
                "Signed-off-by": ["Developer <developer@example.com>"],
            },
        ),
        (
            "agent result\n\nReviewed-by: Reviewer <reviewer@example.com>\n"
            "---\ndiff --git a/file b/file",
            {"Reviewed-by": ["Reviewer <reviewer@example.com>"]},
        ),
        (
            "agent result\n\n"
            "Reviewed-by: First Reviewer <first@example.com>\n"
            "Reviewed-by: Second Reviewer <second@example.com>\n"
            "Co-authored-by: First Human <first-human@example.com>\n"
            "Co-authored-by: Second Human <second-human@example.com>",
            {
                "Reviewed-by": [
                    "First Reviewer <first@example.com>",
                    "Second Reviewer <second@example.com>",
                ],
                "Co-authored-by": [
                    "First Human <first-human@example.com>",
                    "Second Human <second-human@example.com>",
                    "Codex <noreply@openai.com>",
                ],
            },
        ),
    ],
    ids=(
        "whitespace-separator",
        "mixed-block",
        "patch-divider",
        "repeated-unrelated-keys",
    ),
)
def test_normalize_preserves_git_trailer_blocks(
    repositories: tuple[Path, Path, Path],
    message: str,
    expected: dict[str, list[str]],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/git-trailer-block"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(work, "commit", "--allow-empty", "-m", message)
    original = git(work, "rev-parse", "HEAD")

    normalized = repo.normalize_agent_commit_provenance(
        branch,
        base,
        original,
        expected_agent="codex",
        expected_process="implementation",
    )

    assert normalized.local_sha is not None
    for key, values in expected.items():
        assert (
            git(
                work,
                "show",
                "-s",
                f"--format=%(trailers:key={key},valueonly)",
                normalized.local_sha,
            ).splitlines()
            == values
        )
    if "---\n" in message:
        assert "---\ndiff --git a/file b/file" in git(
            work, "show", "-s", "--format=%B", normalized.local_sha
        )


def test_normalize_provenance_preserves_unrelated_coauthors_and_precedes_push(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/normalize-before-push"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(
        work,
        "commit",
        "--allow-empty",
        "-m",
        "agent result",
        "-m",
        "Co-authored-by: Human <human@example.com>",
    )
    original = git(work, "rev-parse", "HEAD")

    normalized = repo.normalize_agent_commit_provenance(
        branch,
        base,
        original,
        expected_agent="codex",
        expected_process="implementation",
    )
    assert normalized.local_sha is not None
    pushed = repo.ensure_pushed(branch, expected_local_sha=normalized.local_sha)

    assert pushed.remote_sha == normalized.local_sha
    assert git(
        work,
        "show",
        "-s",
        "--format=%(trailers:key=Co-authored-by,valueonly)",
        normalized.local_sha,
    ).splitlines() == [
        "Human <human@example.com>",
        "Codex <noreply@openai.com>",
    ]


def test_normalize_provenance_refuses_ambiguous_values_without_moving_head(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/ambiguous-provenance"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(
        work,
        "commit",
        "--allow-empty",
        "-m",
        "agent result",
        "-m",
        "AWM-Agent: codex\nAWM-Agent: claude\nAWM-Process: implementation",
    )
    original = git(work, "rev-parse", "HEAD")

    with pytest.raises(WorkerFailure, match="ambiguous awm-agent provenance"):
        repo.normalize_agent_commit_provenance(
            branch,
            base,
            original,
            expected_agent="codex",
            expected_process="implementation",
        )

    assert git(work, "rev-parse", "HEAD") == original
    assert repo.inspect_branch(branch).remote_sha is None


def test_normalize_provenance_refuses_remote_visible_commit(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/pushed-malformed-provenance"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(work, "commit", "--allow-empty", "-m", "agent result")
    pushed_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", branch)

    with pytest.raises(WorkerFailure, match="already reachable from remote"):
        repo.normalize_agent_commit_provenance(
            branch,
            base,
            pushed_sha,
            expected_agent="codex",
            expected_process="implementation",
        )

    assert git(work, "rev-parse", "HEAD") == pushed_sha
    assert repo.inspect_branch(branch).remote_sha == pushed_sha


def test_normalize_provenance_refuses_commit_visible_from_remote_tag(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/tagged-malformed-provenance"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(work, "commit", "--allow-empty", "-m", "agent result")
    tagged_sha = git(work, "rev-parse", "HEAD")
    git(work, "tag", "published-agent-result", tagged_sha)
    git(work, "push", "origin", "refs/tags/published-agent-result")
    git(work, "tag", "-d", "published-agent-result")

    with pytest.raises(WorkerFailure, match="refs/tags/published-agent-result"):
        repo.normalize_agent_commit_provenance(
            branch,
            base,
            tagged_sha,
            expected_agent="codex",
            expected_process="implementation",
        )

    assert git(work, "rev-parse", "HEAD") == tagged_sha
    assert repo.inspect_branch(branch).remote_sha is None


def test_rejected_recovery_amend_restores_local_and_preserves_remote_refs(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    original = repo.synchronize_branch("main").local_sha or ""
    remote_before = repo.inspect_remote_refs()
    git(work, "commit", "--amend", "--allow-empty", "-m", "rewritten recovery")
    rejected = git(work, "rev-parse", "HEAD")
    assert rejected != original

    restored = repo.restore_rejected_recovery_branch(
        "main", original_sha=original, rejected_sha=rejected
    )

    assert restored.local_sha == original
    assert git(work, "rev-parse", "HEAD") == original
    assert repo.inspect_remote_refs() == remote_before
    assert repo.inspect_worktree().dirty is False


def test_rejected_recovery_restore_refuses_dirty_staged_and_unstaged_state(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    (work / "secondary.txt").write_text("base\n", encoding="utf-8")
    git(work, "add", "secondary.txt")
    git(work, "commit", "-m", "add secondary file")
    original = git(work, "rev-parse", "HEAD")
    git(work, "commit", "--amend", "--allow-empty", "-m", "rewritten recovery")
    rejected = git(work, "rev-parse", "HEAD")
    (work / "tracked.txt").write_text("staged recovery work\n", encoding="utf-8")
    git(work, "add", "tracked.txt")
    (work / "secondary.txt").write_text(
        "unstaged recovery work\n", encoding="utf-8"
    )
    status_before = git(work, "status", "--short")
    staged_before = git(work, "diff", "--cached")
    unstaged_before = git(work, "diff")

    with pytest.raises(WorkerFailure, match="worktree must be clean"):
        repo.restore_rejected_recovery_branch(
            "main", original_sha=original, rejected_sha=rejected
        )

    assert git(work, "rev-parse", "HEAD") == rejected
    assert git(work, "status", "--short") == status_before
    assert git(work, "diff", "--cached") == staged_before
    assert git(work, "diff") == unstaged_before


def test_rejected_recovery_restores_non_active_refs_before_amended_branch(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    original_sha = repo.synchronize_branch("main").local_sha or ""
    original_refs = repo.inspect_local_refs()
    remote_before = repo.inspect_remote_refs()
    git(work, "commit", "--amend", "--allow-empty", "-m", "rewritten recovery")
    rejected_sha = git(work, "rev-parse", "HEAD")
    git(work, "tag", "recovery-created", rejected_sha)
    recovered_refs = repo.inspect_local_refs()

    repo.restore_rejected_recovery_refs(
        original_refs,
        recovered_refs,
        active_ref="refs/heads/main",
    )
    repo.restore_rejected_recovery_branch(
        "main", original_sha=original_sha, rejected_sha=rejected_sha
    )

    assert repo.inspect_local_refs() == original_refs
    assert repo.inspect_remote_refs() == remote_before
    assert git(work, "rev-parse", "HEAD") == original_sha
    assert repo.inspect_worktree().dirty is False


def test_normalize_refuses_commit_fast_forwarded_from_remote_side_branch(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/remote-side-provenance"
    side_branch = "feature/published-agent-result"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(work, "switch", "-c", side_branch, base)
    git(work, "commit", "--allow-empty", "-m", "published malformed result")
    published_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", side_branch)
    git(work, "switch", branch)
    git(work, "merge", "--ff-only", side_branch)
    git(seed, "fetch", "origin", f"{side_branch}:{side_branch}")
    git(seed, "switch", side_branch)
    git(seed, "commit", "--allow-empty", "-m", "advance published side branch")
    remote_side_sha = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "origin", side_branch)

    with pytest.raises(WorkerFailure, match=f"refs/heads/{side_branch}"):
        repo.normalize_agent_commit_provenance(
            branch,
            base,
            published_sha,
            expected_agent="codex",
            expected_process="implementation",
        )

    assert git(work, "rev-parse", "HEAD") == published_sha
    assert repo.inspect_branch(branch).remote_sha is None
    assert repo.inspect_branch(side_branch).remote_sha == remote_side_sha


@pytest.mark.parametrize(
    ("trailers", "error"),
    [
        (
            "AWM-Agent: codex\n claude\nAWM-Process: implementation",
            "ambiguous awm-agent provenance",
        ),
        (
            "AWM-Agent: codex\nAWM-Process: implementation\n cleanup",
            "ambiguous awm-process provenance",
        ),
        (
            "Co-authored-by: Codex <noreply@openai.com>\n malicious\n"
            "AWM-Agent: codex\nAWM-Process: implementation",
            "ambiguous co-authored-by provenance",
        ),
    ],
    ids=("folded-agent", "folded-process", "folded-configured-coauthor"),
)
def test_normalize_refuses_conflicting_folded_provenance(
    repositories: tuple[Path, Path, Path], trailers: str, error: str
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/folded-provenance"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(work, "commit", "--allow-empty", "-m", "agent result", "-m", trailers)
    original = git(work, "rev-parse", "HEAD")

    with pytest.raises(WorkerFailure, match=error):
        repo.normalize_agent_commit_provenance(
            branch,
            base,
            original,
            expected_agent="codex",
            expected_process="implementation",
        )

    assert git(work, "rev-parse", "HEAD") == original
    assert repo.inspect_branch(branch).remote_sha is None


def test_normalize_valid_remote_visible_provenance_is_a_noop(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/pushed-valid-provenance"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(
        work,
        "commit",
        "--allow-empty",
        "-m",
        "agent result",
        "-m",
        "Co-authored-by: Codex <noreply@openai.com>\n"
        "AWM-Agent: codex\nAWM-Process: implementation",
    )
    pushed_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", branch)

    result = repo.normalize_agent_commit_provenance(
        branch,
        base,
        pushed_sha,
        expected_agent="codex",
        expected_process="implementation",
    )

    assert result.local_sha == pushed_sha
    assert result.remote_sha == pushed_sha


def test_normalize_rewrites_each_commit_in_an_unpublished_range(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/multiple-provenance-commits"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(work, "commit", "--allow-empty", "-m", "first result")
    git(work, "commit", "--allow-empty", "-m", "second result")
    original = git(work, "rev-parse", "HEAD")

    result = repo.normalize_agent_commit_provenance(
        branch,
        base,
        original,
        expected_agent="codex",
        expected_process="implementation",
    )

    assert result.local_sha is not None
    assert result.local_sha != original
    assert len(git(work, "rev-list", f"{base}..{result.local_sha}").splitlines()) == 2
    repo.require_agent_commit_provenance(
        base,
        result.local_sha,
        expected_agent="codex",
        expected_process="implementation",
    )


def test_normalize_preserves_residual_dirty_files_for_focused_cleanup(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/dirty-provenance"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(work, "commit", "--allow-empty", "-m", "agent result")
    original = git(work, "rev-parse", "HEAD")
    generated = work / "generated.log"
    generated.write_text("residual output\n", encoding="utf-8")
    status_before = repo.inspect_worktree().status

    result = repo.normalize_agent_commit_provenance(
        branch,
        base,
        original,
        expected_agent="codex",
        expected_process="implementation",
    )

    assert result.local_sha is not None
    assert result.local_sha != original
    assert repo.inspect_worktree().status == status_before
    assert generated.read_text(encoding="utf-8") == "residual output\n"
    repo.require_agent_commit_provenance(
        base,
        result.local_sha,
        expected_agent="codex",
        expected_process="implementation",
    )


@pytest.mark.parametrize("publish_side", [False, True])
def test_normalize_refuses_merged_side_history(
    repositories: tuple[Path, Path, Path], publish_side: bool
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/merge-provenance"
    side_branch = "feature/provenance-side"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(work, "switch", "-c", side_branch, base)
    git(work, "commit", "--allow-empty", "-m", "side history")
    side_sha = git(work, "rev-parse", "HEAD")
    if publish_side:
        git(work, "push", "origin", side_branch)
    git(work, "switch", branch)
    git(work, "merge", "--no-ff", side_branch, "-m", "merge side history")
    merge_sha = git(work, "rev-parse", "HEAD")

    with pytest.raises(WorkerFailure, match="nonlinear or merge"):
        repo.normalize_agent_commit_provenance(
            branch,
            base,
            merge_sha,
            expected_agent="codex",
            expected_process="implementation",
        )

    assert git(work, "rev-parse", "HEAD") == merge_sha
    assert "AWM-Agent" not in git(work, "show", "-s", "--format=%B", side_sha)
    expected_remote = side_sha if publish_side else None
    assert repo.inspect_branch(side_branch).remote_sha == expected_remote


def test_normalize_ignores_hostile_trailer_configuration(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    branch = "feature/config-independent-provenance"
    marker = work / "hostile-trailer-command-ran"
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(work, "config", "trailer.Co-authored-by.ifexists", "replace")
    git(work, "config", "trailer.AWM-Agent.cmd", f"touch {marker}")
    git(
        work,
        "commit",
        "--allow-empty",
        "-m",
        "agent result",
        "-m",
        "Co-authored-by: Human <human@example.com>",
    )
    original = git(work, "rev-parse", "HEAD")

    result = repo.normalize_agent_commit_provenance(
        branch,
        base,
        original,
        expected_agent="codex",
        expected_process="implementation",
    )

    assert result.local_sha is not None
    message = git(work, "show", "-s", "--format=%B", result.local_sha)
    assert not marker.exists()
    assert message.count("Co-authored-by: Human <human@example.com>") == 1
    assert message.count("Co-authored-by: Codex <noreply@openai.com>") == 1
    assert message.count("AWM-Agent: codex") == 1
    assert message.count("AWM-Process: implementation") == 1
    repo.require_agent_commit_provenance(
        base,
        result.local_sha,
        expected_agent="codex",
        expected_process="implementation",
    )


@pytest.mark.parametrize("reject_restore", [False, True])
def test_normalize_reconciles_remote_race_after_local_ref_update(
    repositories: tuple[Path, Path, Path], reject_restore: bool
) -> None:
    _remote, _seed, work = repositories
    setup = open_repo(work, RecordingGitRunner())
    base = setup.synchronize_branch("main").local_sha or ""
    branch = "feature/provenance-remote-race"
    setup.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(work, "commit", "--allow-empty", "-m", "agent result")
    original = git(work, "rev-parse", "HEAD")

    def publish_original() -> None:
        git(work, "push", "origin", f"{original}:refs/heads/{branch}")

    runner = UpdateRefRaceGitRunner(
        branch, publish_original, reject_restore=reject_restore
    )
    repo = open_repo(work, runner)
    expected_error = MutationOutcomeUnknown if reject_restore else WorkerFailure
    expected_message = (
        "restoration.*could not be proven" if reject_restore else "restored"
    )

    with pytest.raises(expected_error, match=expected_message):
        repo.normalize_agent_commit_provenance(
            branch,
            base,
            original,
            expected_agent="codex",
            expected_process="implementation",
        )

    assert runner.triggered
    assert repo.inspect_branch(branch).remote_sha == original
    if reject_restore:
        assert git(work, "rev-parse", "HEAD") != original
    else:
        assert git(work, "rev-parse", "HEAD") == original


def test_committed_result_requires_agent_and_process_together(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, _seed, work = repositories
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""

    with pytest.raises(ValueError, match="provided together"):
        repo.require_committed_result("main", previous_sha=base, expected_agent="codex")


@pytest.mark.parametrize("remote_relationship", ["ahead", "diverged"])
def test_delivery_refuses_remote_ahead_or_diverged(
    repositories: tuple[Path, Path, Path], remote_relationship: str
) -> None:
    _remote, seed, work = repositories
    branch = "feature/unsafe-delivery"
    repo = open_repo(work, RecordingGitRunner())
    base = repo.synchronize_branch("main").local_sha or ""
    repo.prepare_feature_branch(branch, base="main", expected_base_sha=base)
    git(work, "push", "-u", "origin", branch)

    git(seed, "switch", "-c", branch)
    git(seed, "commit", "--allow-empty", "-m", "remote result")
    git(seed, "push", "origin", branch)
    if remote_relationship == "diverged":
        git(work, "commit", "--allow-empty", "-m", "local result")
    local_sha = git(work, "rev-parse", "HEAD")

    with pytest.raises(WorkerFailure, match=remote_relationship):
        repo.ensure_pushed(branch, expected_local_sha=local_sha)


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


def test_synchronize_uses_run_private_branch_when_base_is_occupied(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _remote, _seed, work = repositories
    base_sha = git(work, "rev-parse", "HEAD")
    isolated = tmp_path / "awm-run-isolated"
    git(work, "worktree", "add", "--detach", str(isolated), base_sha)
    repo = open_repo(isolated, RecordingGitRunner())

    synchronized = repo.synchronize_branch("main")

    assert synchronized.current
    assert synchronized.local_sha == base_sha
    assert git(isolated, "branch", "--show-current").startswith("awm-run/")
    assert git(work, "branch", "--show-current") == "main"
    assert git(work, "rev-parse", "HEAD") == base_sha


@pytest.mark.parametrize("feature_owner", ["source", "retained"])
def test_prepare_feature_uses_run_private_branch_when_logical_branch_is_occupied(
    repositories: tuple[Path, Path, Path],
    tmp_path: Path,
    feature_owner: str,
) -> None:
    _remote, _seed, work = repositories
    base_sha = git(work, "rev-parse", "HEAD")
    branch = "feature/occupied"
    holder = work
    if feature_owner == "source":
        git(work, "switch", "-c", branch)
    else:
        holder = tmp_path / "retained-run"
        git(work, "branch", branch)
        git(work, "worktree", "add", str(holder), branch)
    git(work, "push", "-u", "origin", branch)
    isolated = tmp_path / "new-run"
    git(work, "worktree", "add", "--detach", str(isolated), base_sha)
    repo = open_repo(isolated, RecordingGitRunner())

    feature = repo.prepare_feature_branch(
        branch, base="main", expected_base_sha=base_sha
    )
    private_branch = git(isolated, "branch", "--show-current")
    git(isolated, "commit", "--allow-empty", "-m", "isolated change")

    assert feature.current
    assert feature.local_sha == base_sha
    assert private_branch.startswith("awm-run/")
    assert private_branch.endswith(f"/{branch}")
    assert git(holder, "branch", "--show-current") == branch
    assert git(holder, "rev-parse", "HEAD") == base_sha
    assert git(work, "rev-parse", branch) == base_sha

    git(isolated, "push", "origin", f"HEAD:refs/heads/{branch}")
    pushed = repo.require_pushed(branch)
    assert pushed.current
    assert pushed.local_sha == pushed.remote_sha


def test_recover_feature_finds_local_commit_in_retained_run_worktree(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _remote, _seed, work = repositories
    base_sha = git(work, "rev-parse", "HEAD")
    branch = "feature/local-recovery"
    git(work, "switch", "-c", branch)
    retained = tmp_path / "retained-run"
    git(work, "worktree", "add", "--detach", str(retained), base_sha)
    retained_repo = open_repo(retained, RecordingGitRunner())
    retained_repo.prepare_feature_branch(
        branch, base="main", expected_base_sha=base_sha
    )
    assert git(retained, "branch", "--show-current").startswith("awm-run/")
    git(retained, "commit", "--allow-empty", "-m", "implementation")
    implementation_sha = git(retained, "rev-parse", "HEAD")

    new_run = tmp_path / "new-run"
    git(work, "worktree", "add", "--detach", str(new_run), base_sha)
    new_repo = open_repo(new_run, RecordingGitRunner())
    recovered = new_repo.recover_feature_branch(
        branch, base="main", expected_base_sha=base_sha
    )
    unchanged = new_repo.require_committed_result(
        branch,
        previous_sha=implementation_sha,
        allow_unchanged=recovered.reused_existing_work,
    )

    assert recovered.reused_existing_work
    assert recovered.branch.current
    assert recovered.branch.local_sha == implementation_sha
    assert recovered.branch.remote_sha is None
    assert unchanged.local_sha == implementation_sha
    assert git(retained, "rev-parse", "HEAD") == implementation_sha


def test_recover_feature_reuses_pushed_commit_without_pull_request(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _remote, _seed, work = repositories
    base_sha = git(work, "rev-parse", "HEAD")
    branch = "feature/pushed-recovery"
    git(work, "switch", "-c", branch)
    git(work, "commit", "--allow-empty", "-m", "implementation")
    implementation_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", branch)

    new_run = tmp_path / "new-run"
    git(work, "worktree", "add", "--detach", str(new_run), base_sha)
    new_repo = open_repo(new_run, RecordingGitRunner())
    recovered = new_repo.recover_feature_branch(
        branch, base="main", expected_base_sha=base_sha
    )
    unchanged = new_repo.require_committed_result(
        branch,
        previous_sha=implementation_sha,
        allow_unchanged=recovered.reused_existing_work,
    )

    assert recovered.reused_existing_work
    assert recovered.branch.current
    assert recovered.branch.local_sha == implementation_sha
    assert recovered.branch.remote_sha == implementation_sha
    assert unchanged.local_sha == implementation_sha


def test_recover_feature_rejects_unreconciled_prior_run_commit(
    repositories: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _remote, seed, work = repositories
    old_base = git(work, "rev-parse", "HEAD")
    branch = "feature/stale-recovery"
    git(work, "switch", "-c", branch)
    git(work, "commit", "--allow-empty", "-m", "old implementation")
    git(seed, "commit", "--allow-empty", "-m", "advanced base")
    git(seed, "push", "origin", "main")
    new_base = git(seed, "rev-parse", "HEAD")
    assert old_base != new_base
    git(work, "fetch", "origin", "main")

    new_run = tmp_path / "new-run"
    git(work, "worktree", "add", "--detach", str(new_run), new_base)
    with pytest.raises(WorkerFailure, match="do not contain authoritative base"):
        open_repo(new_run, RecordingGitRunner()).recover_feature_branch(
            branch, base="main", expected_base_sha=new_base
        )


def test_synchronize_adopts_expected_remote_head_by_fast_forward_only(
    repositories: tuple[Path, Path, Path],
) -> None:
    _remote, seed, work = repositories
    branch = "feature/remote-ahead"
    git(work, "switch", "-c", branch)
    git(work, "push", "-u", "origin", branch)
    stale_sha = git(work, "rev-parse", "HEAD")
    git(seed, "fetch", "origin", branch)
    git(seed, "switch", "-c", branch, "--track", f"origin/{branch}")
    git(seed, "commit", "--allow-empty", "-m", "authoritative implementation")
    authoritative_sha = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "origin", branch)
    runner = RecordingGitRunner()
    repo = open_repo(work, runner)

    with pytest.raises(WorkerFailure, match="remote branch.*changed"):
        repo.synchronize_branch(branch, expected_remote_sha="f" * 40)

    assert git(work, "rev-parse", "HEAD") == stale_sha
    synchronized = repo.synchronize_branch(
        branch, expected_remote_sha=authoritative_sha
    )

    assert synchronized.current
    assert synchronized.local_sha == authoritative_sha
    assert synchronized.remote_sha == authoritative_sha
    assert [
        "git",
        "merge",
        "--ff-only",
        f"refs/remotes/origin/{branch}",
    ] in runner.calls
    assert not any(
        forbidden in call
        for call in runner.calls
        for forbidden in ("reset", "rebase", "push", "--force", "-f")
    )


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
