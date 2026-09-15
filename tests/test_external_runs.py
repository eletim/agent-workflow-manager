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
    [
        "timeout",
        "disconnect",
        "malformed",
        "null",
        "redirect",
        "server_error",
        "truncated",
    ],
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
            if response_kind == "truncated":
                body = json.dumps(
                    {
                        "runId": 1,
                        "state": "success",
                        "result": {"exitCode": 0, "stdout": "", "stderr": ""},
                    }
                ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header(
                "Content-Length", str(len(body) + (response_kind == "truncated"))
            )
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


@pytest.fixture
def external_awms(tmp_path):
    with ExitStack() as stack:
        servers = []
        for name in ("a", "b"):
            runner = PythonRunner(
                managed_workflows=False, run_history_file=tmp_path / f"{name}.json"
            )
            stack.callback(runner.close)
            server = RunnerHTTPServer(("127.0.0.1", 0), runner)
            stack.callback(server.server_close)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)
            servers.append(server)
        environment = {"TOKEN": servers[0].request_token}
        settings = ExternalTargetSettings(
            tmp_path / "targets.json", environment=environment
        )

        def register(server):
            environment["TOKEN"] = server.request_token
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

        register(servers[0])
        yield servers, settings, register


def test_registration_change_cannot_observe_colliding_run(external_awms):
    servers, settings, register = external_awms
    client = ExternalRunClient(settings)
    run_id = client.start_run("remote", 'raise RuntimeError("AWM A failed")')
    assert client.wait_run("remote", run_id, timeout=5).state == "failed"
    register(servers[1])
    other = ExternalRunClient(settings)
    other_id = other.start_run("remote", 'print("unrelated AWM B success")')
    assert other_id == run_id
    assert other.wait_run("remote", other_id, timeout=5).state == "success"
    with pytest.raises(ExternalRunError, match="destination changed"):
        client.get_run_result("remote", run_id)
    with pytest.raises(ExternalRunError, match="destination changed"):
        client.wait_run("remote", run_id, timeout=5)
    with pytest.raises(ExternalRunError, match="destination changed"):
        client.start_run("remote", 'print("must not launch")')
    assert len(servers[1].runner.snapshots()) == 1


@pytest.mark.parametrize("trickle_part", ["headers", "body"])
def test_trickling_response_expires_within_polling_deadline(tmp_path, trickle_part):
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = json.dumps(
        {
            "runId": 1,
            "state": "success",
            "result": {"exitCode": 0, "stdout": "", "stderr": ""},
        }
    ).encode()
    finished = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if trickle_part == "headers":
                    message = (
                        f"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                        + body
                    )
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    message = body
                for byte in message:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.005)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                finished.set()

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
        client = ExternalRunClient(settings)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            client.wait_run("remote", 1, timeout=0.05)
        assert time.monotonic() - started < 0.15
        assert finished.wait(1)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_wait_checks_deadline_before_returning_result(monkeypatch):
    import time

    from purplemux_client import ExternalRunResult

    client = ExternalRunClient()

    def late_result(*args, **kwargs):
        time.sleep(0.02)
        return ExternalRunResult(1, "success", 0, "", "")

    monkeypatch.setattr(client, "get_run_result", late_result)
    with pytest.raises(TimeoutError):
        client.wait_run("remote", 1, timeout=0.01)


@pytest.mark.parametrize("character", ["a", "日", "😀"])
def test_maximum_retained_output_is_retrievable(character, external_awms):
    servers, settings, _ = external_awms
    client = ExternalRunClient(settings)
    run_id = client.start_run(
        "remote",
        f"import sys, time\ntime.sleep(0.02)\nsys.stdout.write({character!r} * 1_000_000)\nsys.stderr.write({character!r} * 1_000_000)",
    )
    result = client.wait_run("remote", run_id, timeout=15)
    assert result.state == "success"
    assert result.stdout == result.stderr == character * 1_000_000
    assert servers[0].runner.snapshot(run_id).stdout == result.stdout


def test_response_limit_remains_bounded(external_awms, monkeypatch):
    servers, settings, _ = external_awms
    run_id = servers[0].runner.start('print("x" * 256)')
    client = ExternalRunClient(settings)
    assert client.wait_run("remote", run_id, timeout=5).state == "success"
    monkeypatch.setattr("purplemux_client.external_runs._MAX_RESPONSE_BYTES", 128)
    with pytest.raises(ExternalRunError, match="malformed"):
        client.get_run_result("remote", run_id)


def test_synchronous_client_works_with_running_event_loop(external_awms):
    import asyncio

    _, settings, _ = external_awms
    client = ExternalRunClient(settings)

    async def workflow():
        run_id = client.start_run("remote", 'print("async caller")')
        return client.wait_run("remote", run_id, timeout=5)

    result = asyncio.run(workflow())
    assert result.state == "success"
    assert result.stdout.strip() == "async caller"


@pytest.mark.parametrize(
    "trusted,hostname", [(False, "localhost"), (True, "localhost"), (True, "127.0.0.1")]
)
def test_tls_verifies_certificate_and_hostname(
    trusted, hostname, tmp_path, monkeypatch
):
    import ssl
    import subprocess

    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    if trusted:
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    runner = PythonRunner(
        managed_workflows=False, run_history_file=tmp_path / "history.json"
    )
    server = RunnerHTTPServer(("127.0.0.1", 0), runner)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
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
                        "destination": f"https://{hostname}:{server.server_port}",
                        "tokenEnv": "TOKEN",
                    }
                ]
            }
        )
        client = ExternalRunClient(settings)
        if trusted and hostname == "localhost":
            run_id = client.start_run("remote", 'print("verified TLS")')
            result = client.wait_run("remote", run_id, timeout=5)
            assert result.state == "success"
            assert result.stdout.strip() == "verified TLS"
        else:
            with pytest.raises(ExternalRunLaunchUnknown):
                client.start_run("remote", 'print("must not execute")')
            assert not runner.snapshots()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        runner.close()


@pytest.mark.parametrize("launching", [True, False])
@pytest.mark.parametrize("resolver_delay", [0.5, 3600])
def test_dns_deadline_kills_and_reaps_resolver(
    launching, resolver_delay, external_awms, monkeypatch
):
    import asyncio
    import socket
    import time

    from purplemux_client import external_run_dns

    servers, settings, _ = external_awms
    settings.update(
        {
            "targets": [
                {
                    "id": "remote",
                    "destination": f"http://localhost:{servers[0].server_port}",
                    "tokenEnv": "TOKEN",
                }
            ]
        }
    )
    script = f"""
import socket, time
real_getaddrinfo = socket.getaddrinfo
def delayed_getaddrinfo(*args, **kwargs):
    time.sleep({resolver_delay})
    return real_getaddrinfo(*args, **kwargs)
socket.getaddrinfo = delayed_getaddrinfo
"""
    monkeypatch.setattr(
        external_run_dns, "_RESOLVER_SCRIPT", script + external_run_dns._RESOLVER_SCRIPT
    )
    processes = []
    original_spawn = asyncio.create_subprocess_exec

    async def track_process(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track_process)

    # Any accidental executor-backed lookup would delay worker shutdown too.
    # The helper process uses its own system resolver, unaffected by this patch.
    def forbidden_parent_dns(*args, **kwargs):
        raise AssertionError("DNS must not run in an unmanaged parent thread")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_parent_dns)
    client = ExternalRunClient(settings, request_timeout=0.1 if launching else 5)
    started = time.monotonic()
    with pytest.raises(ExternalRunLaunchUnknown if launching else TimeoutError):
        if launching:
            client.start_run("remote", 'print("must not launch")')
        else:
            client.wait_run("remote", 1, timeout=0.1)
    assert time.monotonic() - started < 0.3
    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert processes[0].returncode < 0
    assert not servers[0].runner.snapshots()
    # A subsequent ordinary lookup succeeds, demonstrating that cancelled DNS
    # neither abandons a resolver nor breaks the next request's owned loop.
    monkeypatch.undo()
    client.request_timeout = 5
    run_id = client.start_run("remote", 'print("DNS recovered")')
    result = client.wait_run("remote", run_id, timeout=5)
    assert result.state == "success"
    assert result.stdout.strip() == "DNS recovered"


@pytest.mark.parametrize("observed", [None, "a" * 32 + "-1", "b" * 32 + "-2"])
def test_navigation_rejects_missing_or_different_run_identity(
    tmp_path, monkeypatch, observed
):
    settings = ExternalTargetSettings(
        tmp_path / "targets.json", environment={"TOKEN": "secret"}
    )
    settings.update(
        {
            "targets": [
                {
                    "id": "remote",
                    "destination": "https://remote.example",
                    "tokenEnv": "TOKEN",
                }
            ]
        }
    )
    client = ExternalRunClient(settings)
    monkeypatch.setattr(
        client, "_request", lambda *args, **kwargs: {"runId": 1, "identity": observed}
    )
    assert client.navigation_url("b" * 32 + "-1") is None
