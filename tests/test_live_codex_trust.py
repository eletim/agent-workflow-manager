from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    PurpleMuxRuntime,
)

EXPECTED_RESULT = "AWM_CODEX_TRUST_LIVE_OK"
RUN_LIVE = os.environ.get("AGENT_WORKFLOW_MANAGER_RUN_LIVE_CODEX_TRUST") == "1"


def _git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="set AGENT_WORKFLOW_MANAGER_RUN_LIVE_CODEX_TRUST=1",
)
def test_fresh_and_already_trusted_worktree_launch_without_interaction(
    tmp_path: Path,
) -> None:
    source = Path(_git("rev-parse", "--show-toplevel"))
    worktree = tmp_path / "fresh-linked-worktree"
    runtime = PurpleMuxRuntime()
    workspace = None
    client = None
    sessions: list[str] = []
    _git("worktree", "add", "--detach", str(worktree), "HEAD", cwd=source)
    try:
        workspace = runtime.create_workspace(
            CreateWorkspaceRequest(
                cwd=str(worktree),
                name="Codex trust live integration",
                correlation_id=f"trust-live-{uuid.uuid4().hex[:12]}",
            )
        )
        client = runtime.workspace(workspace.id)
        for initial_tab in client.list_sessions():
            client.close_session(initial_tab.id, expected_state=initial_tab)

        for _ in range(2):
            session_id = client.create_session(
                CreateSessionRequest(worker="codex", cwd=str(worktree), command="codex")
            )
            sessions.append(session_id)
            client.wait_until_ready(session_id, timeout_seconds=90)
            client.send_input(
                session_id,
                f"Reply with exactly {EXPECTED_RESULT} and nothing else.",
            )
            client.wait_for_turn_completion(session_id, timeout_seconds=300)
            assert client.read_result(session_id).strip() == EXPECTED_RESULT
            client.close_session(session_id)
            sessions.remove(session_id)
    finally:
        if client is not None:
            for session_id in sessions:
                client.close_session(session_id)
        if workspace is not None:
            runtime.delete_workspace(workspace.id, expected_state=workspace)
        _git("worktree", "remove", str(worktree), cwd=source)
