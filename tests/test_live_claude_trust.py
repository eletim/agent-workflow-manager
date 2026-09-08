from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    PurpleMuxRuntime,
)

EXPECTED_RESULT = "AWM_CLAUDE_TRUST_LIVE_OK"
RUN_LIVE = os.environ.get("AGENT_WORKFLOW_MANAGER_RUN_LIVE_CLAUDE_TRUST") == "1"


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
    reason="set AGENT_WORKFLOW_MANAGER_RUN_LIVE_CLAUDE_TRUST=1",
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
                name="Claude trust live integration",
                correlation_id=f"trust-live-{uuid.uuid4().hex[:12]}",
            )
        )
        client = runtime.workspace(workspace.id)
        for initial_tab in client.list_sessions():
            client.close_session(initial_tab.id, expected_state=initial_tab)

        for _ in range(2):
            session_id = client.create_session(
                CreateSessionRequest(
                    worker="claude", cwd=str(worktree), command="claude"
                )
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


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="set AGENT_WORKFLOW_MANAGER_RUN_LIVE_CLAUDE_TRUST=1",
)
def test_real_claude_writer_and_awm_share_the_state_lock(tmp_path: Path) -> None:
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("claude executable is unavailable")
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "claude-config"
    config.mkdir()
    state_path = config / ".config.json"
    state_path.write_text(
        '{"projects":{},"preservedSentinel":true}\n', encoding="utf-8"
    )
    lock_path = config / ".config.json.lock"
    lock_path.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["CLAUDE_CONFIG_DIR"] = str(config)
    helper_program = (
        "from purplemux_client.claude_trust import ensure_claude_project_trust; "
        "import sys; ensure_claude_project_trust(sys.argv[1])"
    )
    doctor = subprocess.Popen(
        [claude, "doctor"],
        cwd=project,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    helper = subprocess.Popen(
        [sys.executable, "-c", helper_program, str(project)],
        cwd=project,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.2)
        assert doctor.poll() is None
        assert helper.poll() is None
    finally:
        lock_path.rmdir()
    doctor_stdout, doctor_stderr = doctor.communicate(timeout=20)
    helper_stdout, helper_stderr = helper.communicate(timeout=20)

    assert (doctor.returncode, doctor_stderr) == (0, "")
    assert "Claude Code doctor" in doctor_stdout
    assert (helper.returncode, helper_stdout, helper_stderr) == (0, "", "")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["preservedSentinel"] is True
    assert state["projects"][str(project)]["hasTrustDialogAccepted"] is True
