from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    prepare_run_repository,
)

RUN_LIVE = os.environ.get("AGENT_WORKFLOW_MANAGER_RUN_LIVE_CLAUDE_LINKED_COMMIT") == "1"


def _git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _cleanup_failure(errors: list[str], action: str, exc: BaseException) -> None:
    errors.append(f"{action}: {exc}")


def _finish_cleanup(errors: list[str], original_failure: bool) -> None:
    if not errors:
        return
    message = "live-test cleanup failed: " + "; ".join(errors)
    if original_failure:
        try:
            print(message, file=sys.stderr)
        except BaseException:
            pass
        return
    raise AssertionError(message)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="set AGENT_WORKFLOW_MANAGER_RUN_LIVE_CLAUDE_LINKED_COMMIT=1",
)
def test_real_claude_commits_cleanly_in_linked_run_worktree(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    _git("init", "--bare", str(remote))
    _git("init", "-b", "main", str(source))
    _git("config", "user.name", "AWM Live Test", cwd=source)
    _git("config", "user.email", "awm-live@example.com", cwd=source)
    (source / "proof.txt").write_text("base\n", encoding="utf-8")
    _git("add", "proof.txt", cwd=source)
    _git("commit", "-m", "base", cwd=source)
    _git("remote", "add", "origin", str(remote), cwd=source)
    _git("push", "origin", "main", cwd=source)

    context = prepare_run_repository(
        repo=source,
        base_branch="main",
        worktree_root=tmp_path / "worktrees",
    )
    worktree = context.execution_root
    workspace = None
    client: PurpleMuxCLIClient | None = None
    session_id = None
    turn_started = False
    turn_completed = False
    try:
        branch = "feature/live-claude-linked-commit"
        _git("switch", "-c", branch, context.base_sha, cwd=worktree)
        remote_refs_before = _git("ls-remote", "--refs", "origin", cwd=worktree)
        runtime = PurpleMuxRuntime()
        workspace = runtime.create_workspace(
            CreateWorkspaceRequest(
                cwd=str(worktree),
                name="Claude linked commit live integration",
                correlation_id=f"linked-commit-live-{uuid.uuid4().hex[:12]}",
            )
        )
        client = runtime.workspace(workspace.id)
        for initial_tab in client.list_sessions():
            client.close_session(initial_tab.id, expected_state=initial_tab)
        session_id = client.create_session(
            CreateSessionRequest(
                worker="claude",
                cwd=str(worktree),
                command="claude",
                restriction="publication-disabled",
            )
        )
        client.wait_until_ready(session_id, timeout_seconds=90)
        client.send_input(
            session_id,
            (
                "Append exactly one line containing 'claude' to proof.txt, then run "
                "git add proof.txt and git commit -m 'Verify Claude linked commit'. "
                "Do not change any other file, do not push, and finish with a clean "
                "worktree."
            ),
        )
        turn_started = True
        client.wait_for_turn_completion(session_id, timeout_seconds=300)
        client.read_result(session_id)
        turn_completed = True

        head = _git("rev-parse", "HEAD", cwd=worktree)
        assert head != context.base_sha
        assert (
            _git("merge-base", "--is-ancestor", context.base_sha, head, cwd=worktree)
            == ""
        )
        assert (
            _git("rev-list", "--count", f"{context.base_sha}..{head}", cwd=worktree)
            == "1"
        )
        assert _git("diff", "--name-only", context.base_sha, head, cwd=worktree) == (
            "proof.txt"
        )
        assert _git("branch", "--show-current", cwd=worktree) == branch
        assert _git("log", "-1", "--format=%s", cwd=worktree) == (
            "Verify Claude linked commit"
        )
        assert (worktree / "proof.txt").read_text(encoding="utf-8") == "base\nclaude\n"
        assert _git("status", "--porcelain", cwd=worktree) == ""
        assert _git("ls-remote", "--refs", "origin", cwd=worktree) == (
            remote_refs_before
        )
    finally:
        original_failure = sys.exc_info()[0] is not None
        cleanup_errors: list[str] = []
        if (
            client is not None
            and session_id is not None
            and turn_started
            and not turn_completed
        ):
            try:
                client.interrupt(session_id)
                client.wait_for_turn_completion(session_id, timeout_seconds=30)
            except BaseException as exc:
                _cleanup_failure(cleanup_errors, "stop Claude process group", exc)
        if client is not None and session_id is not None:
            try:
                client.close_session(session_id)
            except BaseException as exc:
                _cleanup_failure(cleanup_errors, "close Claude session", exc)
        if workspace is not None:
            try:
                PurpleMuxRuntime().delete_workspace(
                    workspace.id, expected_state=workspace
                )
            except BaseException as exc:
                _cleanup_failure(cleanup_errors, "delete PurpleMux workspace", exc)
        try:
            _git("worktree", "remove", "--force", str(worktree), cwd=source)
        except BaseException as exc:
            _cleanup_failure(cleanup_errors, "remove linked worktree", exc)
        _finish_cleanup(cleanup_errors, original_failure)
