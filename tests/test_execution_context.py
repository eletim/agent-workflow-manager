from __future__ import annotations

import subprocess
import time
from pathlib import Path

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
        assert Path(result.resources[0].identity).is_dir()

        cleaned = runner.cleanup(result.run_id or 0)
        assert cleaned.resources[0].cleanup_state == "cleaned"
        assert not Path(result.resources[0].identity).exists()
    finally:
        runner.close()


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
