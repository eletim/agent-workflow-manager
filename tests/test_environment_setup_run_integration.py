"""Exercise generated Environment Setup through AWM's ordinary Run endpoints."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from test_runner import request, wait_for

from purplemux_client.runner import PythonRunner
from purplemux_client.web import RunnerHTTPServer


def test_generated_setup_run_result_stop_and_history(
    tmp_path: Path, monkeypatch
) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / "README.md").write_text("test repository\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Initial",
        ],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    source = json.dumps(
        {
            "mode": "environment-setup",
            "repository": str(repository),
            "revision": revision,
            "environment_agent": "codex",
            "timeout": 120,
            "ready_check": "test -f README.md",
        }
    )
    history = tmp_path / "history.json"
    runner = PythonRunner(managed_workflows=False, run_history_file=history)
    server = RunnerHTTPServer(("127.0.0.1", 0), runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = (str(server.server_address[0]), int(server.server_address[1]))
    token = server.request_token
    ready_run_id = None
    stopped_run_id = None
    try:
        status, generated = request(
            address,
            "POST",
            "/api/environment-setup/generate",
            json.dumps({"json": source}),
            token=token,
        )
        assert status == 200
        assert generated["config"]["revision"] == revision
        assert generated["revisionValidation"] == "verified"
        code = generated["generatedCode"]
        assert "prepare_run_revision(" in code

        # The real generated workflow runs in a subprocess. Only its external
        # agent, worktree, and command boundary is replaced in that process.
        shim = tmp_path / "shim"
        shim.mkdir()
        (shim / "sitecustomize.py").write_text(
            """import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
import purplemux_client as awm
import purplemux_client.environment_setup_execution as execution

def prepare_run_revision(**_kwargs):
    if os.environ["AWM_SETUP_TEST_MODE"] == "stop":
        Path(os.environ["AWM_SETUP_TEST_ENTERED"]).write_text("entered")
        while True:
            time.sleep(1)
    return SimpleNamespace(
        execution_root=Path(os.environ["AWM_SETUP_TEST_REPOSITORY"]),
        base_sha=os.environ["AWM_SETUP_TEST_REVISION"],
    )

class Client:
    def __init__(self):
        self.reads = 0
    def create_session(self, _request):
        return "agent-tab"
    def wait_until_ready(self, _tab, _timeout):
        pass
    def send_input(self, _tab, _message):
        pass
    def wait_for_turn_completion(self, _tab, _timeout, **_kwargs):
        pass
    def read_result(self, _tab):
        self.reads += 1
        return json.dumps({"status": "READY", "summary": "environment ready",
                           "endpoint": "http://127.0.0.1:8000"})

class Runtime:
    def __init__(self, *, owned_by_run):
        assert owned_by_run
        self.client = Client()
    def create_workspace(self, _request):
        return SimpleNamespace(id="setup-workspace")
    def workspace(self, _workspace_id):
        return self.client

def execute_commands(**kwargs):
    check = {"command": kwargs["ready_check"], "exit_code": 0,
             "output": "ready"}
    kwargs["observation"].update({"checks": {"ready_check": check},
                                  "verification": check, "failure": None})
    return {"checks": {"ready_check": check}, "service_tab": None,
            "verification": check, "failure": None}

awm.prepare_run_revision = prepare_run_revision
awm.PurpleMuxRuntime = Runtime
execution.execute_environment_setup_commands = execute_commands
""",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "PYTHONPATH", str(shim) + os.pathsep + os.environ.get("PYTHONPATH", "")
        )
        monkeypatch.setenv("AWM_SETUP_TEST_REPOSITORY", str(repository))
        monkeypatch.setenv("AWM_SETUP_TEST_REVISION", revision)
        monkeypatch.setenv("AWM_SETUP_TEST_ENTERED", str(tmp_path / "entered"))

        monkeypatch.setenv("AWM_SETUP_TEST_MODE", "ready")
        status, started = request(
            address,
            "POST",
            "/api/run",
            json.dumps({"code": code, "args": [], "environmentSetupJson": source}),
            token=token,
        )
        assert status == 202
        ready_run_id = started["runId"]
        finished = wait_for(
            runner, lambda item: item.state != "running", run_id=ready_run_id
        )
        assert finished.state == "success"
        assert finished.as_json()["mode"] == "environment-setup"
        assert [event.status for event in finished.progress][-1] == "completed"
        status, result = request(
            address, "GET", f"/api/runs/{ready_run_id}/result", token=token
        )
        assert status == 200
        assert result["state"] == "success"
        readiness = json.loads(result["result"]["stdout"])
        assert readiness["status"] == "READY"
        assert readiness["resolved_revision"] == revision
        assert readiness["connection"] == {
            "workspace_id": "setup-workspace",
            "agent_tab_id": "agent-tab",
            "endpoint": "http://127.0.0.1:8000",
        }
        assert readiness["verification"]["exit_code"] == 0

        monkeypatch.setenv("AWM_SETUP_TEST_MODE", "stop")
        status, started = request(
            address,
            "POST",
            "/api/run",
            json.dumps({"code": code, "args": [], "environmentSetupJson": source}),
            token=token,
        )
        assert status == 202
        stopped_run_id = started["runId"]
        entered = tmp_path / "entered"
        deadline = time.monotonic() + 5
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists(), runner.snapshot(stopped_run_id).stderr
        status, stopped = request(
            address, "POST", f"/api/runs/{stopped_run_id}/stop", "{}", token=token
        )
        assert status == 202
        assert stopped["stopped"] is True
        assert (
            wait_for(
                runner, lambda item: item.state == "stopped", run_id=stopped_run_id
            ).state
            == "stopped"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    restored = PythonRunner(managed_workflows=False, run_history_file=history)
    try:
        assert ready_run_id is not None and stopped_run_id is not None
        ready = restored.snapshot(ready_run_id)
        stopped = restored.snapshot(stopped_run_id)
        assert ready.environment_setup_json == source
        assert ready.code == code
        assert json.loads(ready.stdout)["status"] == "READY"
        assert ready.as_summary_json()["mode"] == "environment-setup"
        assert stopped.environment_setup_json == source
        assert stopped.code == code
        assert stopped.state == "stopped"
        assert stopped.as_summary_json()["mode"] == "environment-setup"
    finally:
        restored.close()
