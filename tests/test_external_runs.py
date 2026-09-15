from __future__ import annotations

import json
import threading
from contextlib import ExitStack
from urllib import error, request

import pytest

from purplemux_client import (
    ExternalRunClient,
    ExternalRunError,
    ExternalRunLaunchUnknown,
)
from purplemux_client.external_targets import ExternalTargetSettings
from purplemux_client.runner import PythonRunner
from purplemux_client.web import RunnerHTTPServer


@pytest.mark.parametrize("outcome", ["success", "failed", "stopped"])
def test_two_awms(outcome, tmp_path):
    with ExitStack() as stack:
        servers = []
        for name in ("source", "destination"):
            runner = PythonRunner(
                managed_workflows=False,
                run_history_file=tmp_path / f"{name}-history.json",
                stop_timeout=0.2,
            )
            stack.callback(runner.close)
            settings = ExternalTargetSettings(
                tmp_path / f"{name}-targets.json", environment={}
            )
            server = RunnerHTTPServer(
                ("127.0.0.1", 0), runner, external_target_settings=settings
            )
            stack.callback(server.server_close)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)
            servers.append(server)
        source, destination = servers
        url = f"http://127.0.0.1:{destination.server_address[1]}"
        source.external_target_settings._environment = {
            "REMOTE_TOKEN": destination.request_token
        }
        source.external_target_settings.update(
            {
                "targets": [
                    {"id": "remote", "destination": url, "tokenEnv": "REMOTE_TOKEN"}
                ]
            }
        )
        # Execute the supported client in an ordinary Workflow on the source AWM.
        code = 'print("remote output", flush=True)\n'
        if outcome == "failed":
            code += 'raise RuntimeError("remote failure")\n'
        elif outcome == "stopped":
            code += "import time; time.sleep(60)\n"
        client = ExternalRunClient(source.external_target_settings)
        if outcome == "stopped":
            remote_id = client.start_run("remote", code)
            assert client.get_run_result("remote", remote_id) is None
            with pytest.raises(TimeoutError):
                client.wait_run("remote", remote_id, timeout=0.01)
            destination.runner.stop(remote_id)
            result = client.wait_run("remote", remote_id, timeout=10)
        else:
            workflow = f"""
from purplemux_client import ExternalRunClient
from purplemux_client.external_targets import ExternalTargetSettings
import json
from pathlib import Path
client = ExternalRunClient(ExternalTargetSettings(Path({str(source.external_target_settings.path)!r}), environment={{"REMOTE_TOKEN": {destination.request_token!r}}}))
run_id = client.start_run("remote", {code!r})
result = client.wait_run("remote", run_id, timeout=10)
print(json.dumps(result.__dict__))
"""
            local_id = source.runner.start(workflow)
            import time

            deadline = time.monotonic() + 15
            while source.runner.snapshot(local_id).state == "running":
                assert time.monotonic() < deadline
                time.sleep(0.01)
            local = source.runner.snapshot(local_id)
            assert local.state == "success", local.stderr
            value = json.loads(local.stdout)
            result = client.get_run_result("remote", value["run_id"])
        assert result.state == outcome
        if outcome != "stopped":
            assert "remote output" in result.stdout
        if outcome == "failed":
            assert result.exit_code != 0
            assert "remote failure" in result.stderr
        with pytest.raises(ExternalRunError):
            client.get_run_result("remote", 99999)
        with pytest.raises(error.HTTPError) as rejected:
            request.urlopen(url + f"/api/runs/{result.run_id}/result")
        assert rejected.value.code == 403
        bad_origin = request.Request(
            url + f"/api/runs/{result.run_id}/result",
            headers={
                "X-Python-Runner-Token": destination.request_token,
                "Origin": "http://evil.example",
            },
        )
        with pytest.raises(error.HTTPError):
            request.urlopen(bad_origin)


@pytest.mark.parametrize(
    "value",
    [
        {"runId": 1, "state": []},
        {},
        {"runId": True, "state": "running"},
        {"runId": 1, "state": "unknown"},
    ],
)
def test_malformed_launch_is_unknown(value, monkeypatch):
    client = ExternalRunClient()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: value)
    with pytest.raises(ExternalRunLaunchUnknown):
        client.start_run("remote", "print(1)")


@pytest.mark.parametrize(
    "value",
    [
        {"runId": 1, "state": [], "result": None},
        {},
        {"runId": 1, "state": "running"},
        {"runId": 2, "state": "running", "result": None},
        {"runId": 1, "state": "unknown", "result": None},
        {
            "runId": 1,
            "state": "success",
            "result": {"exitCode": None, "stdout": "", "stderr": ""},
        },
        {
            "runId": 1,
            "state": "success",
            "result": {"exitCode": 1, "stdout": "", "stderr": ""},
        },
    ],
)
def test_malformed_result_fails(value, monkeypatch):
    client = ExternalRunClient()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: value)
    with pytest.raises(ExternalRunError):
        client.get_run_result("remote", 1)


@pytest.mark.parametrize("launching", [True, False])
@pytest.mark.parametrize(
    "response_kind",
    ["timeout", "disconnect", "malformed", "null", "redirect", "server_error"],
)
def test_transport_failures_never_succeed_or_retry(launching, response_kind, tmp_path):
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            calls.append(self.path)
            if response_kind == "timeout":
                import time

                time.sleep(0.1)
                return
            if response_kind == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            status = (
                302
                if response_kind == "redirect"
                else 500
                if response_kind == "server_error"
                else 202
                if launching
                else 200
            )
            body = b"null" if response_kind == "null" else b"broken json"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Location", "/credential-leak")
            self.end_headers()
            self.wfile.write(body)

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = ExternalTargetSettings(
            tmp_path / "targets.json", environment={"TOKEN": "secret"}
        )
        settings.update(
            {
                "targets": [
                    {
                        "id": "remote",
                        "destination": f"http://127.0.0.1:{server.server_port}",
                        "tokenEnv": "TOKEN",
                    }
                ]
            }
        )
        client = ExternalRunClient(settings, request_timeout=0.02)
        with pytest.raises(ExternalRunLaunchUnknown if launching else ExternalRunError):
            if launching:
                client.start_run("remote", "print(1)")
            else:
                client.wait_run("remote", 1, timeout=1)
        assert calls == ["/api/run" if launching else "/api/runs/1/result"]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_lost_launch_response_does_not_launch_twice(tmp_path, monkeypatch):
    from purplemux_client.web import RunnerRequestHandler

    original = RunnerRequestHandler._send_json

    def lose_accepted_response(self, status, payload):
        if self.path == "/api/run" and status == 202:
            self.close_connection = True
            return
        original(self, status, payload)

    monkeypatch.setattr(RunnerRequestHandler, "_send_json", lose_accepted_response)
    runner = PythonRunner(
        managed_workflows=False, run_history_file=tmp_path / "history.json"
    )
    server = RunnerHTTPServer(("127.0.0.1", 0), runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = ExternalTargetSettings(
            tmp_path / "targets.json", environment={"TOKEN": server.request_token}
        )
        settings.update(
            {
                "targets": [
                    {
                        "id": "remote",
                        "destination": f"http://127.0.0.1:{server.server_port}",
                        "tokenEnv": "TOKEN",
                    }
                ]
            }
        )
        client = ExternalRunClient(settings)
        with pytest.raises(ExternalRunLaunchUnknown):
            client.start_run("remote", 'print("launched once")')
        runs = runner.snapshots()
        assert len(runs) == 1
        result = client.wait_run("remote", runs[0].run_id, timeout=5)
        assert result.state == "success"
        assert result.stdout.strip() == "launched once"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        runner.close()
