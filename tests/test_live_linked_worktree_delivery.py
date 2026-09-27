from __future__ import annotations

import os
import shlex
import subprocess
import uuid
from pathlib import Path

import pytest

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    PurpleMuxRuntime,
)

RUN_CODEX_LIVE = os.environ.get("AGENT_WORKFLOW_MANAGER_RUN_LIVE_CODEX_DELIVERY") == "1"
RUN_CLAUDE_LIVE = (
    os.environ.get("AGENT_WORKFLOW_MANAGER_RUN_LIVE_CLAUDE_DELIVERY") == "1"
)


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _exercise_linked_worktree_delivery(tmp_path: Path, worker: str) -> None:
    repository = tmp_path / "repository-parent"
    checkout = tmp_path / "linked-checkout"
    remote = tmp_path / "test-remote.git"
    _git("init", "-b", "main", str(repository))
    _git("config", "user.name", "Test", cwd=repository)
    _git("config", "user.email", "test@example.com", cwd=repository)
    (repository / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=repository)
    _git("commit", "-m", "base", cwd=repository)
    base = _git("rev-parse", "HEAD", cwd=repository)
    _git("init", "--bare", str(remote))
    _git(
        "worktree",
        "add",
        "-b",
        f"feature/live-{worker}-delivery",
        str(checkout),
        cwd=repository,
    )
    _git("remote", "add", "test-remote", str(remote), cwd=checkout)

    runtime = PurpleMuxRuntime()
    workspace = None
    client = None
    sessions: list[str] = []
    try:
        workspace = runtime.create_workspace(
            CreateWorkspaceRequest(
                cwd=str(checkout),
                name=f"{worker.title()} linked delivery live integration",
                correlation_id=f"delivery-live-{uuid.uuid4().hex[:12]}",
            )
        )
        client = runtime.workspace(workspace.id)
        for initial_tab in client.list_sessions():
            client.close_session(initial_tab.id, expected_state=initial_tab)

        session_id = client.create_session(
            CreateSessionRequest(
                worker=worker,
                cwd=str(checkout),
                command=worker,
                restriction="publication-disabled",
            )
        )
        sessions.append(session_id)
        client.wait_until_ready(session_id, timeout_seconds=90)
        client.send_input(
            session_id,
            "Perform this exact local delivery check without changing any other file: "
            "replace tracked.txt with the single line 'after', run "
            "'git add tracked.txt', commit it with message 'linked-delivery-live', "
            f"attempt 'git push {shlex.quote(str(remote))} "
            "HEAD:refs/heads/forbidden' and require "
            "that push to fail, then require 'git status --porcelain' to be empty. "
            "Reply with exactly AWM_LINKED_DELIVERY_LIVE_OK only after every step "
            "has the required result.",
        )
        client.wait_for_turn_completion(session_id, timeout_seconds=600)
        assert client.read_result(session_id).strip() == "AWM_LINKED_DELIVERY_LIVE_OK"
        client.close_session(session_id)
        sessions.remove(session_id)

        head = _git("rev-parse", "HEAD", cwd=checkout)
        assert head != base
        assert _git("rev-list", "--count", f"{base}..{head}", cwd=checkout) == "1"
        assert _git("show", "-s", "--format=%s", "HEAD", cwd=checkout) == (
            "linked-delivery-live"
        )
        assert _git("show", "HEAD:tracked.txt", cwd=checkout) == "after"
        assert _git("status", "--porcelain", cwd=checkout) == ""
        assert (
            subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(remote),
                    "show-ref",
                    "--verify",
                    "refs/heads/forbidden",
                ],
                check=False,
                capture_output=True,
            ).returncode
            != 0
        )
    finally:
        if client is not None:
            for session_id in sessions:
                client.close_session(session_id)
        if workspace is not None:
            runtime.delete_workspace(workspace.id, expected_state=workspace)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_CODEX_LIVE,
    reason="set AGENT_WORKFLOW_MANAGER_RUN_LIVE_CODEX_DELIVERY=1",
)
def test_real_codex_delivers_from_linked_worktree(tmp_path: Path) -> None:
    _exercise_linked_worktree_delivery(tmp_path, "codex")


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_CLAUDE_LIVE,
    reason="set AGENT_WORKFLOW_MANAGER_RUN_LIVE_CLAUDE_DELIVERY=1",
)
def test_real_claude_delivers_from_linked_worktree(tmp_path: Path) -> None:
    _exercise_linked_worktree_delivery(tmp_path, "claude")
