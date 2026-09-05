from __future__ import annotations

import http.client
import json
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from purplemux_client.client import (
    WORKFLOW_HOST_WORKSPACE_ENV,
    CreateWorkspaceRequest,
    PurpleMuxRuntime,
    ShellCommandRequest,
    ShellResult,
    TabState,
    WorkspaceState,
)
from purplemux_client.errors import MutationOutcomeUnknown, WorkerFailure
from purplemux_client.progress import (
    EVENT_TOKEN_ENV,
    EVENT_URL_ENV,
    acknowledge_run_resource,
    emit_step,
)
from purplemux_client.runner import PythonRunner, RunnerSnapshot
from purplemux_client.web import RunnerHTTPServer


class _ManagedClient:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.exit_code = 0
        self.request: ShellCommandRequest | None = None
        self.interrupted = False
        self.start_error: BaseException | None = None
        self.wait_error: BaseException | None = None
        self.close_error: BaseException | None = None

    def start_shell(self, request, *, on_created=None):  # type: ignore[no-untyped-def]
        self.request = request
        result_dir = tempfile.mkdtemp(prefix="awm-shell-")
        if on_created is not None:
            on_created("tab-workflow", str(Path(result_dir) / "result.json"))
        if self.start_error is not None:
            raise self.start_error
        return "tab-workflow"

    def list_sessions(self) -> tuple[TabState, ...]:
        assert self.request is not None
        return (
            TabState(
                "tab-workflow",
                "ws-workflow",
                self.request.name,
                "terminal",
                None,
            ),
        )

    def wait_for_shell_completion(
        self, session_id: str, timeout_seconds: float
    ) -> None:
        assert session_id == "tab-workflow"
        if self.wait_error is not None:
            raise self.wait_error
        assert self.release.wait(timeout=3)

    def read_shell_result(self, session_id: str) -> ShellResult:
        assert session_id == "tab-workflow"
        return ShellResult(self.exit_code)

    def interrupt(self, session_id: str) -> None:
        assert session_id == "tab-workflow"
        self.interrupted = True
        self.exit_code = 130
        self.release.set()

    def close_session(self, session_id: str) -> None:
        assert session_id == "tab-workflow"
        if self.close_error is not None:
            raise self.close_error
        self.release.set()


class _ManagedRuntime:
    def __init__(self, client: _ManagedClient) -> None:
        self.client = client
        self.request: CreateWorkspaceRequest | None = None

    def create_workspace(self, request: CreateWorkspaceRequest) -> WorkspaceState:
        self.request = request
        return WorkspaceState("ws-workflow", request.name, (request.cwd,))

    def workspace(self, workspace_id: str) -> _ManagedClient:
        assert workspace_id == "ws-workflow"
        return self.client


def _wait_for_state(runner: PythonRunner, state: str) -> RunnerSnapshot:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snapshot = runner.snapshot()
        if snapshot.state == state:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"runner did not reach {state}: {runner.snapshot()}")


def test_workflow_reuses_its_host_workspace_for_the_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = WorkspaceState("ws-host", "Workflow host", (str(tmp_path),))
    runtime = PurpleMuxRuntime()
    monkeypatch.setenv(WORKFLOW_HOST_WORKSPACE_ENV, workspace.id)
    monkeypatch.setattr(runtime, "list_workspaces", lambda: (workspace,))

    selected = runtime.create_workspace(
        CreateWorkspaceRequest(str(tmp_path), "child work")
    )

    assert selected is workspace


def test_http_workflow_uses_visible_managed_shell_and_authenticated_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ManagedClient()
    runtime = _ManagedRuntime(client)
    runner = PythonRunner(
        workflow_cwd=tmp_path,
        managed_workflows=True,
        runtime_factory=lambda: runtime,  # type: ignore[arg-type]
    )
    server = RunnerHTTPServer(("127.0.0.1", 0), runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        run_id = runner.start("print('visible only in PurpleMux')")
        run = runner._runs[run_id]
        assert runtime.request is not None
        assert runtime.request.cwd == str(tmp_path)
        assert client.request is not None
        assert client.request.name == f"Workflow {run_id}: Python"
        assert run.event_token is not None
        assert run.event_token not in client.request.command
        assert str(run.credential_path) in client.request.command
        with pytest.raises(PermissionError, match="credential"):
            runner.accept_event(
                run_id, "wrong-run-token", '{"name":"forged","status":"started"}'
            )

        monkeypatch.setenv(
            EVENT_URL_ENV,
            f"http://127.0.0.1:{server.server_address[1]}/api/runs/{run_id}/events",
        )
        monkeypatch.setenv(EVENT_TOKEN_ENV, run.event_token)
        emit_step("managed", "started")
        worktree = str(tmp_path / "worktree")
        acknowledge_run_resource(
            "pending",
            "git_worktree",
            worktree,
            {"registration_state": "pending", "repository": str(tmp_path)},
        )
        acknowledge_run_resource(
            "verified",
            "git_worktree",
            worktree,
            {"registration_state": "verified", "repository": str(tmp_path)},
        )

        snapshot = runner.snapshot(run_id)
        assert snapshot.progress[0].name == "managed"
        assert snapshot.progress[0].observed_at is not None
        assert (
            datetime.fromisoformat(snapshot.progress[0].observed_at).tzinfo is not None
        )
        progress_json = snapshot.as_json()["progress"]
        assert isinstance(progress_json, list)
        assert progress_json[0]["observedAt"] == snapshot.progress[0].observed_at
        assert [resource.kind for resource in snapshot.resources[:3]] == [
            "purplemux_workspace",
            "purplemux_tab",
            "managed_shell_result",
        ]
        assert snapshot.resources[-1].metadata["registration_state"] == "verified"

        client.release.set()
        finished = _wait_for_state(runner, "success")
        assert finished.exit_code == 0
        assert finished.stdout == ""
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_stop_interrupts_managed_shell_and_uses_its_exit_result(tmp_path: Path) -> None:
    client = _ManagedClient()
    runtime = _ManagedRuntime(client)
    runner = PythonRunner(
        workflow_cwd=tmp_path,
        runtime_factory=lambda: runtime,  # type: ignore[arg-type]
    )
    runner.configure_event_endpoint("http://127.0.0.1:1")
    try:
        run_id = runner.start("import time; time.sleep(60)")
        assert runner.stop(run_id) is True
        stopped = _wait_for_state(runner, "stopped")
        assert client.interrupted is True
        assert stopped.exit_code == 130
    finally:
        runner.close()


def test_uncertain_stop_stays_running_and_http_reports_error(tmp_path: Path) -> None:
    client = _ManagedClient()
    client.wait_error = WorkerFailure("result unavailable")
    client.close_error = MutationOutcomeUnknown("close outcome unknown")
    runtime = _ManagedRuntime(client)
    runner = PythonRunner(
        workflow_cwd=tmp_path,
        runtime_factory=lambda: runtime,  # type: ignore[arg-type]
    )
    server = RunnerHTTPServer(("127.0.0.1", 0), runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        run_id = runner.start("print('possibly still running')")
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        connection.request(
            "POST",
            f"/api/runs/{run_id}/stop",
            body=b"",
            headers={"X-Python-Runner-Token": server.request_token},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        assert response.status == 502
        assert payload["stopped"] is False
        assert payload["state"] == "running"
        assert payload["cleanupAvailable"] is False
        assert "termination is uncertain" in payload["error"]
        snapshot = runner.snapshot(run_id)
        assert snapshot.state == "running"
        assert snapshot.exit_code is None
        assert snapshot.attempts == ()
        run = runner._runs[run_id]
        assert run.credential_path is not None and run.credential_path.exists()

        client.wait_error = None
        client.close_error = None
        client.exit_code = 130
        client.release.set()
        assert runner.stop(run_id) is True
        assert _wait_for_state(runner, "stopped").exit_code == 130
        assert not run.credential_path.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_authoritative_start_failure_tracks_created_tab_and_result(
    tmp_path: Path,
) -> None:
    client = _ManagedClient()
    client.start_error = WorkerFailure("command was rejected")
    runtime = _ManagedRuntime(client)
    runner = PythonRunner(
        workflow_cwd=tmp_path,
        runtime_factory=lambda: runtime,  # type: ignore[arg-type]
    )
    runner.configure_event_endpoint("http://127.0.0.1:1")
    try:
        run_id = runner.start("print('not started')")
        failed = runner.snapshot(run_id)

        assert failed.state == "failed"
        assert [resource.kind for resource in failed.resources] == [
            "purplemux_workspace",
            "purplemux_tab",
            "managed_shell_result",
        ]
        assert failed.as_json()["cleanupAvailable"] is True
    finally:
        snapshot = runner.snapshot()
        if len(snapshot.resources) >= 3:
            shutil.rmtree(snapshot.resources[2].identity, ignore_errors=True)
        runner.close()
