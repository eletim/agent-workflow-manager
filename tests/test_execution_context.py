from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from purplemux_client import inspect_run_repository, prepare_run_repository
from purplemux_client.preflight import WorkflowValidator
from purplemux_client.runner import PythonRunner, RunnerSnapshot


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def repository_with_remote(tmp_path: Path) -> tuple[Path, str]:
    remote = tmp_path / "remote.git"
    repository = tmp_path / "source"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repository)], check=True)
    git(repository, "config", "user.email", "test@example.com")
    git(repository, "config", "user.name", "Test")
    (repository / "tracked").write_text("base\n", encoding="utf-8")
    git(repository, "add", "tracked")
    git(repository, "commit", "-qm", "base")
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "-qu", "origin", "main")
    return repository, git(repository, "rev-parse", "HEAD")


def wait_until_finished(runner: PythonRunner) -> RunnerSnapshot:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = runner.snapshot()
        if result.state != "running":
            return result
        time.sleep(0.02)
    raise AssertionError("workflow did not finish")


def test_inspection_resolves_declared_remote_base_without_mutation(
    tmp_path: Path,
) -> None:
    repository, sha = repository_with_remote(tmp_path)

    result = inspect_run_repository(repo=repository, base_branch="main")

    assert result.source_repository == repository.resolve()
    assert result.base_ref == "origin/main"
    assert result.base_sha == sha


def test_prepare_creates_fresh_detached_worktree_and_returns_identity(
    tmp_path: Path,
) -> None:
    repository, sha = repository_with_remote(tmp_path)
    worktree_root = tmp_path / "managed-worktrees"

    result = prepare_run_repository(
        repo=repository,
        base_branch="main",
        worktree_root=worktree_root,
    )

    assert result.source_repository == repository.resolve()
    assert result.base_sha == sha
    assert result.execution_root.parent == worktree_root
    assert result.execution_root.name.startswith("awm-run-source-")
    assert git(result.execution_root, "rev-parse", "HEAD") == sha
    assert (
        git(result.execution_root, "rev-parse", "--symbolic-full-name", "HEAD")
        == "HEAD"
    )
    assert git(repository, "branch", "--show-current") == "main"


def test_prepare_ignores_ambient_checkout_branch_and_dirty_state(
    tmp_path: Path,
) -> None:
    repository, sha = repository_with_remote(tmp_path)
    git(repository, "switch", "-qc", "ambient-work")
    dirty_file = repository / "unrelated-untracked"
    dirty_file.write_text("preserve me\n", encoding="utf-8")

    first = prepare_run_repository(
        repo=repository,
        base_branch="main",
        worktree_root=tmp_path / "managed-worktrees",
    )
    second = prepare_run_repository(
        repo=repository,
        base_branch="main",
        worktree_root=tmp_path / "managed-worktrees",
    )

    assert first.execution_root != second.execution_root
    assert git(first.execution_root, "rev-parse", "HEAD") == sha
    assert git(second.execution_root, "rev-parse", "HEAD") == sha
    assert git(repository, "branch", "--show-current") == "ambient-work"
    assert dirty_file.read_text(encoding="utf-8") == "preserve me\n"


def test_prepare_defaults_to_persistent_awm_owned_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _sha = repository_with_remote(tmp_path)
    managed_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(managed_home))

    result = prepare_run_repository(repo=repository, base_branch="main")

    assert result.execution_root.parent == (
        managed_home / ".local/share/agent-workflow-manager/worktrees"
    )
    assert result.execution_root.name.startswith("awm-run-source-")


def test_prepare_fetches_the_exact_current_remote_base(tmp_path: Path) -> None:
    repository, old_sha = repository_with_remote(tmp_path)
    publisher = tmp_path / "publisher"
    subprocess.run(
        ["git", "clone", "-q", str(tmp_path / "remote.git"), str(publisher)],
        check=True,
    )
    git(publisher, "config", "user.email", "test@example.com")
    git(publisher, "config", "user.name", "Test")
    git(publisher, "switch", "-q", "main")
    (publisher / "tracked").write_text("new base\n", encoding="utf-8")
    git(publisher, "commit", "-qam", "advance base")
    git(publisher, "push", "-q", "origin", "main")
    current_sha = git(publisher, "rev-parse", "HEAD")
    assert current_sha != old_sha

    result = prepare_run_repository(
        repo=repository,
        base_branch="main",
        worktree_root=tmp_path / "managed-worktrees",
    )

    assert result.base_sha == current_sha
    assert git(result.execution_root, "rev-parse", "HEAD") == current_sha
    assert git(repository, "branch", "--show-current") == "main"


def test_static_validation_checks_literal_repository_declaration(
    tmp_path: Path,
) -> None:
    repository, _sha = repository_with_remote(tmp_path)
    source = f"""
from purplemux_client import prepare_run_repository
WORKFLOW_DRY_RUN = 1
context = prepare_run_repository(repo={str(repository)!r}, base_branch="main")
"""

    valid = WorkflowValidator().validate(source)
    missing = WorkflowValidator().validate(
        source.replace(str(repository), str(tmp_path / "missing"))
    )

    assert valid.valid
    assert not valid.dry_run_issues
    assert not missing.valid
    assert missing.issues[0].kind == "execution_context"


def test_static_validation_resolves_relative_repository_from_workflow_cwd(
    tmp_path: Path,
) -> None:
    workflow_cwd = tmp_path / "workflow"
    workflow_cwd.mkdir()
    repository, _sha = repository_with_remote(workflow_cwd)
    source = """
from purplemux_client import prepare_run_repository
WORKFLOW_DRY_RUN = 1
prepare_run_repository(repo="source", base_branch="main")
"""

    result = WorkflowValidator().validate(source, cwd=workflow_cwd)

    assert result.valid
    assert repository == workflow_cwd / "source"


def test_static_validation_resolves_remote_base_without_cached_tracking_ref(
    tmp_path: Path,
) -> None:
    repository, sha = repository_with_remote(tmp_path)
    git(repository, "update-ref", "-d", "refs/remotes/origin/main")
    source = f"""
from purplemux_client import prepare_run_repository
WORKFLOW_DRY_RUN = 1
prepare_run_repository(repo={str(repository)!r}, base_branch="main")
"""

    result = WorkflowValidator().validate(source)

    assert result.valid
    assert git(repository, "ls-remote", "origin", "refs/heads/main").startswith(sha)
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "show-ref",
                "--verify",
                "--quiet",
                "refs/remotes/origin/main",
            ],
            check=False,
        ).returncode
        == 1
    )


def test_dry_run_reports_worktree_plan_without_creating_it(tmp_path: Path) -> None:
    repository, sha = repository_with_remote(tmp_path)
    worktree_root = tmp_path / "managed-worktrees"
    code = f"""
from purplemux_client import prepare_run_repository
WORKFLOW_DRY_RUN = 1
prepare_run_repository(
    repo={str(repository)!r},
    base_branch="main",
    worktree_root={str(worktree_root)!r},
)
"""
    runner = PythonRunner()
    try:
        result = runner.dry_run(code)
    finally:
        runner.close()

    assert result.status == "frontier"
    assert result.next_mutation is not None
    assert result.next_mutation["operation"] == "create isolated Git worktree"
    pre_state = result.next_mutation["preState"]
    assert isinstance(pre_state, dict)
    assert pre_state["baseSha"] == sha
    assert not worktree_root.exists()


def test_runner_registers_and_exposes_prepared_execution_context(
    tmp_path: Path,
) -> None:
    repository, sha = repository_with_remote(tmp_path)
    worktree_root = tmp_path / "managed-worktrees"
    code = f"""
from purplemux_client import prepare_run_repository
context = prepare_run_repository(
    repo={str(repository)!r},
    base_branch="main",
    worktree_root={str(worktree_root)!r},
)
print(context.execution_root)
"""
    runner = PythonRunner()
    try:
        runner.start(code)
        result = wait_until_finished(runner)
        payload = result.as_json()
        context = payload["executionContext"]
        assert isinstance(context, dict)
        assert context["sourceRepository"] == str(repository.resolve())
        assert context["baseRef"] == "origin/main"
        assert context["baseSha"] == sha
        assert context["executionRoot"] == result.resources[0].identity
        assert result.resources[0].metadata["registration_state"] == "verified"
        assert Path(result.resources[0].identity).is_dir()

        cleaned = runner.cleanup(result.run_id or 0)
        assert cleaned.resources[0].cleanup_state == "cleaned"
        assert not Path(result.resources[0].identity).exists()
    finally:
        runner.close()


def test_cleanup_refuses_dirty_prepared_worktree(tmp_path: Path) -> None:
    repository, _sha = repository_with_remote(tmp_path)
    worktree_root = tmp_path / "managed-worktrees"
    code = f"""
from pathlib import Path
from purplemux_client import prepare_run_repository
context = prepare_run_repository(
    repo={str(repository)!r},
    base_branch="main",
    worktree_root={str(worktree_root)!r},
)
Path(context.execution_root, "uncommitted").write_text("retain for inspection")
"""
    runner = PythonRunner()
    try:
        run_id = runner.start(code)
        completed = wait_until_finished(runner)
        worktree = Path(completed.resources[0].identity)

        cleanup = runner.cleanup(run_id)

        assert cleanup.resources[0].cleanup_state == "cleanup_retryable"
        assert "uncommitted changes" in (cleanup.resources[0].cleanup_error or "")
        assert worktree.is_dir()
    finally:
        runner.close()


def test_interrupted_post_creation_metadata_keeps_cleanup_owned(
    tmp_path: Path,
) -> None:
    repository, _sha = repository_with_remote(tmp_path)
    worktree_root = tmp_path / "managed-worktrees"
    code = f"""
import time
import purplemux_client.execution_context as execution_context
from purplemux_client import prepare_run_repository
execution_context._path_identity = lambda path: time.sleep(60)
prepare_run_repository(
    repo={str(repository)!r},
    base_branch="main",
    worktree_root={str(worktree_root)!r},
)
"""
    runner = PythonRunner()
    try:
        run_id = runner.start(code)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = runner.snapshot(run_id)
            if snapshot.resources and Path(snapshot.resources[0].identity).exists():
                break
            time.sleep(0.02)
        else:
            raise AssertionError("worktree was not created before interruption")

        assert snapshot.resources[0].metadata["registration_state"] == "pending"
        assert runner.stop(run_id)
        stopped = wait_until_finished(runner)
        assert stopped.state == "stopped"
        cleaned = runner.cleanup(run_id)
        assert cleaned.resources[0].cleanup_state == "cleaned"
        assert not Path(snapshot.resources[0].identity).exists()
    finally:
        runner.close()


def test_resume_adopts_matching_pending_worktree_without_repeating_mutation(
    tmp_path: Path,
) -> None:
    repository, original_sha = repository_with_remote(tmp_path)
    worktree_root = tmp_path / "managed-worktrees"
    code = f"""
import purplemux_client.execution_context as execution_context
from purplemux_client import prepare_run_repository, resume_checkpoint, save_checkpoint
checkpoint = resume_checkpoint()
if checkpoint is None:
    save_checkpoint("before repository preparation")
    def fail_identity(path):
        raise RuntimeError("pre-verification failure")
    execution_context._path_identity = fail_identity
context = prepare_run_repository(
    repo={str(repository)!r},
    base_branch="main",
    worktree_root={str(worktree_root)!r},
)
print(context.execution_root)
"""
    runner = PythonRunner()
    try:
        run_id = runner.start(code)
        failed = wait_until_finished(runner)
        assert failed.state == "failed"
        assert failed.as_json()["resumable"] is True
        assert failed.as_json()["executionContext"] is None
        assert failed.resources[0].metadata["registration_state"] == "pending"
        pending_root = failed.resources[0].identity

        publisher = tmp_path / "publisher"
        subprocess.run(
            ["git", "clone", "-q", str(tmp_path / "remote.git"), str(publisher)],
            check=True,
        )
        git(publisher, "config", "user.email", "test@example.com")
        git(publisher, "config", "user.name", "Test")
        git(publisher, "switch", "-q", "main")
        (publisher / "tracked").write_text("advanced\n", encoding="utf-8")
        git(publisher, "commit", "-qam", "advance while pending")
        git(publisher, "push", "-q", "origin", "main")
        assert git(publisher, "rev-parse", "HEAD") != original_sha

        runner.resume(run_id)
        resumed = wait_until_finished(runner)

        assert resumed.state == "success"
        assert len(resumed.resources) == 1
        assert resumed.resources[0].identity == pending_root
        assert resumed.resources[0].metadata["registration_state"] == "verified"
        assert resumed.resources[0].metadata["base_sha"] == original_sha
        worktrees = git(repository, "worktree", "list", "--porcelain").splitlines()
        assert sum(line.startswith("worktree ") for line in worktrees) == 2
        assert resumed.as_json()["executionContext"] == {
            "sourceRepository": str(repository.resolve()),
            "remote": "origin",
            "baseBranch": "main",
            "baseRef": "origin/main",
            "baseSha": original_sha,
            "executionRoot": pending_root,
        }

        cleaned = runner.cleanup(run_id)
        assert all(
            resource.cleanup_state == "cleaned" for resource in cleaned.resources
        )
        assert all(
            not Path(resource.identity).exists() for resource in cleaned.resources
        )
    finally:
        runner.close()


def test_resume_retries_absent_pending_worktree_at_reserved_identity(
    tmp_path: Path,
) -> None:
    repository, original_sha = repository_with_remote(tmp_path)
    worktree_root = tmp_path / "managed-worktrees"
    code = f"""
import purplemux_client.execution_context as execution_context
from purplemux_client import prepare_run_repository, resume_checkpoint, save_checkpoint
checkpoint = resume_checkpoint()
if checkpoint is None:
    save_checkpoint("before repository preparation")
    run_git = execution_context._run_git_mutation_process_group
    def reject_first_add(args, *, cwd, timeout):
        if args[0] == "worktree":
            raise execution_context._QuiescentMutationTimeout(["git", *args], timeout)
        return run_git(args, cwd=cwd, timeout=timeout)
    execution_context._run_git_mutation_process_group = reject_first_add
context = prepare_run_repository(
    repo={str(repository)!r},
    base_branch="main",
    worktree_root={str(worktree_root)!r},
)
print(context.execution_root)
"""
    runner = PythonRunner()
    try:
        run_id = runner.start(code)
        failed = wait_until_finished(runner)
        assert failed.state == "failed"
        assert len(failed.resources) == 1
        reserved_root = failed.resources[0].identity
        assert not Path(reserved_root).exists()

        publisher = tmp_path / "publisher"
        subprocess.run(
            ["git", "clone", "-q", str(tmp_path / "remote.git"), str(publisher)],
            check=True,
        )
        git(publisher, "config", "user.email", "test@example.com")
        git(publisher, "config", "user.name", "Test")
        git(publisher, "switch", "-q", "main")
        (publisher / "tracked").write_text("advanced\n", encoding="utf-8")
        git(publisher, "commit", "-qam", "advance before retry")
        git(publisher, "push", "-q", "origin", "main")
        assert git(publisher, "rev-parse", "HEAD") != original_sha

        runner.resume(run_id)
        resumed = wait_until_finished(runner)

        assert resumed.state == "success"
        assert len(resumed.resources) == 1
        assert resumed.resources[0].identity == reserved_root
        assert resumed.resources[0].metadata["registration_state"] == "verified"
        assert resumed.resources[0].metadata["base_sha"] == original_sha
        runner.cleanup(run_id)
    finally:
        runner.close()


def test_cleanup_blocks_for_unregistered_partial_worktree_path(
    tmp_path: Path,
) -> None:
    repository, _sha = repository_with_remote(tmp_path)
    worktree_root = tmp_path / "managed-worktrees"
    code = f"""
from pathlib import Path
import purplemux_client.execution_context as execution_context
from purplemux_client import prepare_run_repository, save_checkpoint
save_checkpoint("before repository preparation")
run_git = execution_context._run_git_mutation_process_group
def interrupt_worktree_add(args, *, cwd, timeout):
    if args[0] == "worktree":
        Path(args[3]).mkdir()
        raise execution_context._QuiescentMutationTimeout(["git", *args], timeout)
    return run_git(args, cwd=cwd, timeout=timeout)
execution_context._run_git_mutation_process_group = interrupt_worktree_add
prepare_run_repository(
    repo={str(repository)!r},
    base_branch="main",
    worktree_root={str(worktree_root)!r},
)
"""
    runner = PythonRunner()
    partial_path: Path | None = None
    try:
        run_id = runner.start(code)
        failed = wait_until_finished(runner)
        assert failed.state == "failed"
        assert len(failed.resources) == 1
        partial_path = Path(failed.resources[0].identity)
        assert partial_path.is_dir()
        assert (
            f"worktree {partial_path}"
            not in git(repository, "worktree", "list", "--porcelain").splitlines()
        )

        runner.resume(run_id)
        conflicted = wait_until_finished(runner)
        assert conflicted.state == "failed"
        assert "pending worktree reservation conflicts" in conflicted.stderr
        assert len(conflicted.resources) == 1

        cleanup = runner.cleanup(run_id)

        assert cleanup.resources[0].cleanup_state == "cleanup_retryable"
        assert "exists but is not registered" in (
            cleanup.resources[0].cleanup_error or ""
        )
        assert partial_path.is_dir()
    finally:
        runner.close()
        if partial_path is not None and partial_path.is_dir():
            partial_path.rmdir()


def test_oversized_ownership_event_fails_before_worktree_mutation(
    tmp_path: Path,
) -> None:
    repository, _sha = repository_with_remote(tmp_path)
    oversized_root = str(tmp_path / ("x" * 4100))
    code = f"""
from purplemux_client import prepare_run_repository
prepare_run_repository(
    repo={str(repository)!r},
    base_branch="main",
    worktree_root={oversized_root!r},
)
"""
    runner = PythonRunner()
    try:
        runner.start(code)
        result = wait_until_finished(runner)
    finally:
        runner.close()

    assert result.state == "failed"
    assert "ownership event exceeds" in result.stderr
    assert not result.resources
    worktrees = git(repository, "worktree", "list", "--porcelain").splitlines()
    assert sum(line.startswith("worktree ") for line in worktrees) == 1


def test_resume_reuses_the_run_owned_worktree(tmp_path: Path) -> None:
    repository, _sha = repository_with_remote(tmp_path)
    worktree_root = tmp_path / "managed-worktrees"
    repaired = tmp_path / "repaired"
    code = f"""
from pathlib import Path
from purplemux_client import prepare_run_repository, save_checkpoint
context = prepare_run_repository(
    repo={str(repository)!r},
    base_branch="main",
    worktree_root={str(worktree_root)!r},
)
save_checkpoint("repository prepared", {{"root": str(context.execution_root)}})
if not Path({str(repaired)!r}).exists():
    raise RuntimeError("repair required")
"""
    runner = PythonRunner()
    try:
        run_id = runner.start(code)
        first = wait_until_finished(runner)
        assert first.state == "failed"
        assert len(first.resources) == 1
        first_root = first.resources[0].identity

        repaired.touch()
        runner.resume(run_id)
        resumed = wait_until_finished(runner)

        assert resumed.state == "success"
        assert len(resumed.resources) == 1
        assert resumed.resources[0].identity == first_root
        runner.cleanup(run_id)
    finally:
        runner.close()
