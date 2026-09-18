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
from purplemux_client.workflow import CONTROL_TOKEN_ENV, CONTROL_URL_ENV


class _ManagedClient:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.exit_code = 0
        self.stdout = ""
        self.stderr = ""
        self.request: ShellCommandRequest | None = None
        self.interrupted = False
        self.start_error: BaseException | None = None
        self.wait_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.read_errors: list[BaseException] = []
        self.read_calls = 0

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
        self.read_calls += 1
        if self.read_errors:
            raise self.read_errors.pop(0)
        return ShellResult(self.exit_code, stdout=self.stdout, stderr=self.stderr)

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
    def __init__(
        self, client: _ManagedClient, *, initial_tab_discovery_pending: bool = False
    ) -> None:
        self.client = client
        self.request: CreateWorkspaceRequest | None = None
        self.initial_tab_discovery_pending = initial_tab_discovery_pending

    def create_workspace(self, request: CreateWorkspaceRequest) -> WorkspaceState:
        self.request = request
        return WorkspaceState(
            "ws-workflow",
            request.name,
            (request.cwd,),
            None
            if self.initial_tab_discovery_pending
            else TabState("tab-initial", "ws-workflow", "", None, None),
            self.initial_tab_discovery_pending,
        )

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
    server = RunnerHTTPServer(
        ("127.0.0.1", 0), runner, host_aliases=("runner.example",)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        run_id = runner.start("print('visible only in PurpleMux')")
        run = runner._runs[run_id]
        assert runtime.request is not None
        assert runtime.request.cwd == str(tmp_path)
        assert client.request is not None
        assert client.request.name == f"Workflow {run_id}: Python"
        assert [
            (resource.kind, resource.identity) for resource in run.resources[:2]
        ] == [
            ("purplemux_workspace", "ws-workflow"),
            ("purplemux_tab", "tab-initial"),
        ]
        assert run.resources[1].metadata["origin"] == "workspace_initial"
        assert run.event_token is not None
        assert run.event_token not in client.request.command
        assert run.control_token is not None
        assert run.control_token not in client.request.command
        environment = run.credential_path.read_text(encoding="utf-8")
        assert f"export {CONTROL_TOKEN_ENV}=" in environment
        assert f"export {CONTROL_URL_ENV}=http://127.0.0.1:" in environment
        assert str(run.credential_path) in client.request.command
        assert (
            f"http://127.0.0.1:{server.server_address[1]}"
            f"/api/runs/{run_id}/events"
            in run.credential_path.read_text(encoding="utf-8")
        )
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
        assert [resource.kind for resource in snapshot.resources[:4]] == [
            "purplemux_workspace",
            "purplemux_tab",
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
    client.stdout = "before stop\n"
    client.stderr = "stop warning\n"
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
        assert stopped.stdout == "before stop\n"
        assert stopped.stderr == "stop warning\n"
    finally:
        runner.close()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_managed_result_output_is_bounded_and_reloaded(
    tmp_path: Path, exit_code: int
) -> None:
    client = _ManagedClient()
    client.exit_code = exit_code
    client.stdout = "prefix-stdout-tail\n"
    client.stderr = "prefix-stderr-tail\n"
    history = tmp_path / "history.json"
    output_limit = 12 if exit_code == 0 else 1000
    runner = PythonRunner(
        workflow_cwd=tmp_path,
        run_history_file=history,
        max_output_chars=output_limit,
        runtime_factory=lambda: _ManagedRuntime(client),  # type: ignore[arg-type]
    )
    runner.configure_event_endpoint("http://127.0.0.1:1")
    try:
        run_id = runner.start("print('managed')")
        assert client.request is not None
        assert client.request.max_output_chars == output_limit
        client.release.set()
        finished = _wait_for_state(runner, "success" if exit_code == 0 else "failed")
        assert finished.exit_code == exit_code
        if exit_code == 0:
            assert finished.stdout == "[output truncated; showing tail]\nstdout-tail\n"
        else:
            assert finished.stdout == client.stdout
        assert "stderr-tail\n" in finished.stderr
        assert finished.as_json()["stdout"] == finished.stdout
        assert finished.as_json()["stderr"] == finished.stderr
        if exit_code:
            assert "Workflow failed (exit code 7)" in finished.stderr
    finally:
        runner.close()

    restored = PythonRunner(run_history_file=history, max_output_chars=output_limit)
    try:
        after = restored.snapshot(run_id)
        assert (after.state, after.stdout, after.stderr) == (
            finished.state,
            finished.stdout,
            finished.stderr,
        )
        assert after.stdout_entries == finished.stdout_entries
        assert after.stderr_entries == finished.stderr_entries
    finally:
        restored.close()


def test_managed_launch_retains_failed_initial_tab_discovery(tmp_path: Path) -> None:
    client = _ManagedClient()
    runtime = _ManagedRuntime(client, initial_tab_discovery_pending=True)
    runner = PythonRunner(
        workflow_cwd=tmp_path,
        runtime_factory=lambda: runtime,  # type: ignore[arg-type]
    )
    runner.configure_event_endpoint("http://127.0.0.1:1")
    try:
        run_id = runner.start("print('managed')")
        resources = runner.snapshot(run_id).resources

        assert [(resource.kind, resource.identity) for resource in resources[:2]] == [
            ("purplemux_workspace", "ws-workflow"),
            ("purplemux_initial_tab", "ws-workflow"),
        ]
        assert resources[0].metadata["initial_tab_discovery"] == "pending"
        client.release.set()
        _wait_for_state(runner, "success")
    finally:
        for resource in runner.snapshot().resources:
            if resource.kind == "managed_shell_result":
                shutil.rmtree(resource.identity, ignore_errors=True)
        runner.close()


def test_transient_result_error_is_retried_without_stop(tmp_path: Path) -> None:
    client = _ManagedClient()
    client.read_errors.append(WorkerFailure("transient result read failure"))
    client.release.set()
    runtime = _ManagedRuntime(client)
    runner = PythonRunner(
        workflow_cwd=tmp_path,
        runtime_factory=lambda: runtime,  # type: ignore[arg-type]
    )
    runner.configure_event_endpoint("http://127.0.0.1:1")
    try:
        run_id = runner.start("print('completed')")

        finished = _wait_for_state(runner, "success")

        assert finished.run_id == run_id
        assert finished.exit_code == 0
        assert finished.attempts[0].state == "success"
        assert client.read_calls == 2
        assert "result observation is uncertain" in finished.stderr
        assert client.interrupted is False
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
            "purplemux_tab",
            "managed_shell_result",
        ]
        assert failed.as_json()["cleanupAvailable"] is True
    finally:
        for resource in runner.snapshot().resources:
            if resource.kind == "managed_shell_result":
                shutil.rmtree(resource.identity, ignore_errors=True)
        runner.close()


class _ExecutingManagedClient(_ManagedClient):
    """Execute the generated shell command while retaining structured completion."""

    def __init__(self, result_root: Path) -> None:
        super().__init__()
        self.result_root = result_root

    def start_shell(self, request, *, on_created=None):
        import subprocess

        self.request = request
        tab_id = "tab-workflow"
        result_dir = tempfile.mkdtemp(prefix="awm-shell-", dir=self.result_root)
        if on_created is not None:
            on_created(tab_id, str(Path(result_dir) / "result.json"))
        self.process = subprocess.Popen(
            ["bash", "-c", request.command],
            cwd=request.cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return tab_id

    def wait_for_shell_completion(self, session_id, timeout_seconds):
        self.process.wait(timeout=timeout_seconds)

    def read_shell_result(self, session_id):
        assert self.process.returncode is not None
        return ShellResult(self.process.returncode)

    def interrupt(self, session_id):
        import signal

        self.interrupted = True
        self.process.send_signal(signal.SIGINT)

    def close_session(self, session_id):
        self.process.kill()
        self.process.wait(timeout=3)


@pytest.mark.parametrize("target_id", [None, "remote"])
@pytest.mark.parametrize("outcome", ["success", "failed", "stopped"])
def test_managed_workflow_executes_child_helpers_and_reloads_family(
    tmp_path: Path, target_id: str | None, outcome: str
) -> None:
    from contextlib import ExitStack

    from purplemux_client.external_targets import ExternalTargetSettings

    def runtime():
        return _ManagedRuntime(_ExecutingManagedClient(tmp_path))

    with ExitStack() as stack:
        runners = {}
        servers = {}
        for name in ("remote", "local"):
            runner = PythonRunner(
                workflow_cwd=tmp_path,
                run_history_file=tmp_path / f"{name}.json",
                runtime_factory=runtime,  # type: ignore[arg-type]
            )
            assert runner.managed_workflows is True
            stack.callback(runner.close)
            settings = None
            if name == "local":
                settings = ExternalTargetSettings(
                    tmp_path / "targets.json",
                    environment={"REMOTE_TOKEN": servers["remote"].request_token},
                )
                settings.update(
                    {
                        "targets": [
                            {
                                "id": "remote",
                                "destination": f"http://127.0.0.1:{servers['remote'].server_port}",
                                "tokenEnv": "REMOTE_TOKEN",
                            }
                        ]
                    }
                )
            server = RunnerHTTPServer(
                ("127.0.0.1", 0), runner, external_target_settings=settings
            )
            stack.callback(server.server_close)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)
            runners[name] = runner
            servers[name] = server

        child_code = 'from purplemux_client import emit_step\nemit_step("child", "completed")\nprint("shell output")\n'
        if outcome == "failed":
            child_code += "raise SystemExit(7)\n"
        elif outcome == "stopped":
            child_code += "import time; time.sleep(60)\n"
        result_path = tmp_path / "observed-result.json"
        parent_code = f"""
from purplemux_client import start_child_run, get_child_run_result, wait_child_run, emit_step
from dataclasses import asdict
from pathlib import Path
import json
child_id = start_child_run({child_code!r}, target_id={target_id!r})
result = wait_child_run(child_id, target_id={target_id!r}, timeout=10)
assert result == get_child_run_result(child_id, target_id={target_id!r})
Path({str(result_path)!r}).write_text(json.dumps(asdict(result)))
emit_step("parent", "completed")
"""
        local = runners["local"]
        destination = runners["remote"] if target_id else local
        parent_id = local.start(parent_code)
        deadline = time.monotonic() + 10
        while not local.snapshot(parent_id).child_runs:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        parent_identity = local._run_identity(parent_id)
        child_identity = local.snapshot(parent_id).child_runs[0]
        child_id = int(child_identity.rsplit("-", 1)[1])
        if outcome == "stopped":
            while not destination.snapshot(child_id).progress:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert local.snapshot(parent_id).state == "running"
            assert destination.stop(child_id) is True
            assert destination._runs[child_id].managed_client.interrupted is True
        while local.snapshot(parent_id).state == "running":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        parent = local.snapshot(parent_id)
        child = destination.snapshot(child_id)
        assert parent.state == "success", parent.stderr
        assert child.state == outcome
        assert child.exit_code == {"success": 0, "failed": 7, "stopped": -2}[outcome]
        assert json.loads(result_path.read_text()) == {
            "run_id": child_id,
            "state": child.state,
            "exit_code": child.exit_code,
            "stdout": child.stdout,
            "stderr": child.stderr,
        }
        assert child.stdout == ""
        assert child.progress[0].name == "child"
        assert parent.progress[0].name == "parent"
        assert child.parent_run == parent_identity
        assert parent.child_runs == (destination._run_identity(child_id),)
        for owner in {local, destination}:
            records = json.loads(owner._run_history_file.read_text())
            assert records["runFamilyLinks"][parent_identity] == [child_identity]
        for runner in runners.values():
            runner.close()
        for name, run_id, before in (
            ("local", parent_id, parent),
            ("remote" if target_id else "local", child_id, child),
        ):
            restored = PythonRunner(run_history_file=tmp_path / f"{name}.json")
            try:
                after = restored.snapshot(run_id)
                assert (after.state, after.exit_code, after.stdout, after.stderr) == (
                    before.state,
                    before.exit_code,
                    before.stdout,
                    before.stderr,
                )
                assert after.parent_run == before.parent_run
                assert after.child_runs == before.child_runs
                assert after.progress == before.progress
            finally:
                restored.close()
