from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from purplemux_client.runner import (
    AlreadyRunningError,
    ProgressEvent,
    PythonRunner,
    RunnerSnapshot,
)
from purplemux_client.web import RunnerHTTPServer


@pytest.fixture
def runner() -> Iterator[PythonRunner]:
    instance = PythonRunner(stop_timeout=0.5)
    yield instance
    instance.close()


def wait_for(
    runner: PythonRunner,
    predicate: Callable[[RunnerSnapshot], bool],
    *,
    timeout: float = 5,
) -> RunnerSnapshot:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = runner.snapshot()
        if predicate(snapshot):
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"runner did not reach expected state: {runner.snapshot()}")


def wait_until_finished(runner: PythonRunner) -> RunnerSnapshot:
    return wait_for(runner, lambda snapshot: snapshot.state != "running")


def test_simple_stdout(runner: PythonRunner) -> None:
    runner.start('print("HELLO_RUNNER")')

    result = wait_until_finished(runner)

    assert result.state == "success"
    assert result.stdout == "HELLO_RUNNER\n"
    assert result.stderr == ""
    assert result.exit_code == 0


def test_stderr(runner: PythonRunner) -> None:
    runner.start('import sys; print("BAD", file=sys.stderr)')

    result = wait_until_finished(runner)

    assert result.state == "success"
    assert result.stderr == "BAD\n"


def test_progress_events_preserve_emit_order_and_optional_fields(
    runner: PythonRunner,
) -> None:
    runner.start(
        "from purplemux_client import emit_step\n"
        'emit_step("implementation", "started", message="working", '
        'workspace="ws-1", tab="tab-1")\n'
        'emit_step("implementation", "completed")\n'
        'emit_step("review", "started", iteration=2, attempt=1)\n'
    )

    result = wait_until_finished(runner)

    assert result.state == "success"
    assert result.progress == (
        ProgressEvent(
            "implementation",
            "started",
            message="working",
            workspace="ws-1",
            tab="tab-1",
        ),
        ProgressEvent("implementation", "completed"),
        ProgressEvent("review", "started", iteration=2, attempt=1),
    )


def test_failed_progress_is_retained_with_failed_process(
    runner: PythonRunner,
) -> None:
    runner.start(
        "from purplemux_client import emit_step\n"
        'emit_step("review", "failed", iteration=1, error="tests failed")\n'
        "raise SystemExit(4)\n"
    )

    result = wait_until_finished(runner)

    assert result.state == "failed"
    assert result.exit_code == 4
    assert result.progress == (
        ProgressEvent("review", "failed", iteration=1, error="tests failed"),
    )


def test_started_progress_is_visible_while_runner_is_running(
    runner: PythonRunner,
) -> None:
    runner.start(
        "from purplemux_client import emit_step\n"
        "import time\n"
        'emit_step("implementation", "started")\n'
        "time.sleep(60)\n"
    )

    result = wait_for(runner, lambda snapshot: bool(snapshot.progress))

    assert result.state == "running"
    assert result.progress == (ProgressEvent("implementation", "started"),)


def test_progress_does_not_change_stdout_or_stderr(runner: PythonRunner) -> None:
    runner.start(
        "from purplemux_client import emit_step\n"
        "import sys\n"
        'emit_step("step", "started")\n'
        'print("OUT")\n'
        'print("ERR", file=sys.stderr)\n'
        'emit_step("step", "completed")\n'
    )

    result = wait_until_finished(runner)

    assert result.stdout == "OUT\n"
    assert result.stderr == "ERR\n"
    assert [event.status for event in result.progress] == ["started", "completed"]


def test_next_run_clears_previous_progress(runner: PythonRunner) -> None:
    runner.start(
        'from purplemux_client import emit_step; emit_step("first", "completed")'
    )
    assert wait_until_finished(runner).progress

    runner.start('print("next")')
    result = wait_until_finished(runner)

    assert result.state == "success"
    assert result.progress == ()


def test_progress_retains_only_configured_event_limit() -> None:
    runner = PythonRunner(max_progress_events=3)
    try:
        runner.start(
            "from purplemux_client import emit_step\n"
            "for iteration in range(1, 6):\n"
            '    emit_step("review", "completed", iteration=iteration)\n'
        )
        result = wait_until_finished(runner)
    finally:
        runner.close()

    assert [event.iteration for event in result.progress] == [3, 4, 5]


def test_runner_discards_oversized_progress_line_and_reads_next_event(
    runner: PythonRunner,
) -> None:
    runner.start(
        "import os\n"
        "from purplemux_client import emit_step\n"
        "from purplemux_client.progress import PROGRESS_FD_ENV\n"
        "fd = int(os.environ[PROGRESS_FD_ENV])\n"
        'os.write(fd, b"x" * 5000 + b"\\n")\n'
        'emit_step("next", "completed")\n'
    )

    result = wait_until_finished(runner)

    assert result.state == "success"
    assert result.progress == (ProgressEvent("next", "completed"),)


@pytest.mark.parametrize("max_progress_events", [0, -1])
def test_progress_event_limit_must_be_positive(max_progress_events: int) -> None:
    with pytest.raises(ValueError, match="max_progress_events must be positive"):
        PythonRunner(max_progress_events=max_progress_events)


def test_nonzero_exit(runner: PythonRunner) -> None:
    runner.start("raise SystemExit(3)")

    result = wait_until_finished(runner)

    assert result.state == "failed"
    assert result.exit_code == 3


@pytest.mark.parametrize("code", ["def broken(", "raise RuntimeError('boom')"])
def test_python_error_is_reported(runner: PythonRunner, code: str) -> None:
    runner.start(code)

    result = wait_until_finished(runner)

    assert result.state == "failed"
    assert result.exit_code != 0
    assert result.stderr


def test_empty_code(runner: PythonRunner) -> None:
    runner.start("")

    result = wait_until_finished(runner)

    assert result.state == "success"
    assert result.exit_code == 0


def test_stop_long_running_process(runner: PythonRunner) -> None:
    runner.start('import time; print("START", flush=True); time.sleep(60)')
    wait_for(runner, lambda snapshot: snapshot.stdout == "START\n")

    assert runner.stop() is True
    result = wait_until_finished(runner)

    assert result.state == "stopped"
    assert result.exit_code is not None


def test_streams_flushed_output_without_newline(runner: PythonRunner) -> None:
    runner.start('import time; print("PARTIAL", end="", flush=True); time.sleep(60)')

    result = wait_for(runner, lambda snapshot: snapshot.stdout == "PARTIAL")

    assert result.state == "running"


def test_output_is_bounded_and_reports_truncation() -> None:
    runner = PythonRunner(max_output_chars=20)
    try:
        runner.start('print("x" * 50, end="")')
        result = wait_until_finished(runner)
    finally:
        runner.close()

    assert result.stdout == "[output truncated; showing tail]\n" + "x" * 20


def test_runner_rejects_non_posix_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")

    with pytest.raises(RuntimeError, match="requires a POSIX"):
        PythonRunner()


def test_second_run_is_rejected_while_running(runner: PythonRunner) -> None:
    runner.start("import time; time.sleep(60)")

    with pytest.raises(AlreadyRunningError, match="already running"):
        runner.start('print("second")')


def test_can_run_again_after_finish(runner: PythonRunner) -> None:
    first_id = runner.start('print("first")')
    wait_until_finished(runner)

    second_id = runner.start('print("second")')
    result = wait_until_finished(runner)

    assert second_id > first_id
    assert result.stdout == "second\n"
    assert result.state == "success"


def test_can_run_again_after_stop(runner: PythonRunner) -> None:
    runner.start("import time; time.sleep(60)")
    assert runner.stop() is True
    wait_until_finished(runner)

    runner.start('print("after stop")')
    result = wait_until_finished(runner)

    assert result.stdout == "after stop\n"
    assert result.state == "success"


def test_close_stops_process_group() -> None:
    runner = PythonRunner(stop_timeout=0.5)
    runner.start(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    result = wait_for(runner, lambda snapshot: bool(snapshot.stdout))
    child_pid = int(result.stdout.strip())

    runner.close()
    wait_until_finished(runner)

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _process_is_live(child_pid):
        time.sleep(0.05)
    assert not _process_is_live(child_pid)


def test_stop_kills_child_that_ignores_sigterm(runner: PythonRunner) -> None:
    runner.start(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        "'import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        'print(f"CHILD {os.getpid()}", flush=True); time.sleep(60)\'])\n'
        "time.sleep(60)\n"
    )
    result = wait_for(runner, lambda snapshot: "CHILD " in snapshot.stdout)
    child_pid = int(result.stdout.split()[1])

    assert runner.stop() is True
    wait_until_finished(runner)

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _process_is_live(child_pid):
        time.sleep(0.05)
    assert not _process_is_live(child_pid)


def _process_is_live(pid: int) -> bool:
    try:
        output = subprocess.check_output(
            ["ps", "-o", "stat=", "-p", str(pid)], text=True
        ).strip()
    except subprocess.CalledProcessError:
        return False
    return bool(output) and not output.startswith("Z")


def test_popen_uses_current_interpreter_without_shell(
    runner: PythonRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_popen = subprocess.Popen
    calls: list[tuple[object, dict[str, object]]] = []

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        calls.append((args[0], kwargs))
        return real_popen(*args, **kwargs)  # type: ignore[call-overload,return-value]

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    runner.start("")
    wait_until_finished(runner)

    command, options = calls[0]
    assert isinstance(command, list)
    assert command[0] == sys.executable
    assert options["shell"] is False
    assert options["start_new_session"] is True


@pytest.fixture
def web_server() -> Iterator[tuple[tuple[str, int], str]]:
    server = RunnerHTTPServer(("127.0.0.1", 0), PythonRunner(stop_timeout=0.5))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield (str(host), int(port)), server.request_token
    server.shutdown()
    server.server_close()
    thread.join()


def request(
    server_address: tuple[str, int],
    method: str,
    path: str,
    body: str | None = None,
    *,
    token: str | None = None,
    origin: str | None = None,
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection(*server_address, timeout=3)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token is not None:
        headers["X-Python-Runner-Token"] = token
    if origin is not None:
        headers["Origin"] = origin
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    return response.status, payload


def request_text(server_address: tuple[str, int], path: str) -> tuple[int, str, str]:
    connection = http.client.HTTPConnection(*server_address, timeout=3)
    connection.request("GET", path)
    response = connection.getresponse()
    content = response.read().decode()
    content_type = response.getheader("Content-Type", "")
    connection.close()
    return response.status, content_type, content


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("not json", "invalid JSON"),
        ("[]", "JSON object required"),
        ('{"code": 42}', "code must be a string"),
    ],
)
def test_malformed_run_request(
    web_server: tuple[tuple[str, int], str], body: str, error: str
) -> None:
    address, token = web_server
    status, payload = request(address, "POST", "/api/run", body, token=token)

    assert status == 400
    assert payload == {"error": error}


def test_runner_http_lifecycle(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    status, started = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": 'print("HTTP_OK")'}),
        token=token,
    )
    assert status == 202
    assert started["runId"] == 1

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, result = request(address, "GET", "/api/status")
        if result["state"] != "running":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("HTTP run did not finish")

    assert status == 200
    assert result == {
        "state": "success",
        "stdout": "HTTP_OK\n",
        "stderr": "",
        "exitCode": 0,
        "runId": 1,
        "progress": [],
    }


def test_workflow_guide_is_served_by_runner(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, _ = web_server

    status, content_type, guide = request_text(address, "/python-workflow-guide.md")

    assert status == 200
    assert content_type == "text/markdown; charset=utf-8"
    assert guide.startswith("# Python Workflow Guide")
    assert "MutationOutcomeUnknown" in guide
    assert 'emit_step("implementation", "started"' in guide


def test_runner_ui_has_workflow_guide_display_copy_and_raw_links(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, _ = web_server

    index_status, _, index = request_text(address, "/")
    script_status, _, script = request_text(address, "/app.js")

    assert index_status == 200
    assert script_status == 200
    assert 'id="guide-open">Workflow Guide' in index
    assert 'href="/python-workflow-guide.md"' in index
    assert 'id="guide-content"' in index
    assert 'id="guide-copy"' in index
    assert 'fetch("/python-workflow-guide.md")' in script
    assert "navigator.clipboard.writeText" in script


def test_http_status_includes_progress_events(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    request(
        address,
        "POST",
        "/api/run",
        json.dumps(
            {
                "code": (
                    "from purplemux_client import emit_step\n"
                    'emit_step("review", "completed", iteration=2)'
                )
            }
        ),
        token=token,
    )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, result = request(address, "GET", "/api/status")
        if result["state"] != "running":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("HTTP run did not finish")

    assert status == 200
    assert result["state"] == "success"
    assert result["progress"] == [
        {
            "name": "review",
            "status": "completed",
            "iteration": 2,
            "attempt": None,
            "message": None,
            "error": None,
            "workspace": None,
            "tab": None,
        }
    ]


@pytest.mark.parametrize(
    ("token", "origin"),
    [(None, None), ("wrong", None), ("valid", "https://attacker.example")],
)
def test_run_rejects_untrusted_request(
    web_server: tuple[tuple[str, int], str], token: str | None, origin: str | None
) -> None:
    address, actual_token = web_server
    status, payload = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": 'print("must not run")'}),
        token=actual_token if token == "valid" else token,
        origin=origin,
    )

    assert status == 403
    assert payload == {"error": "untrusted request"}


def test_token_rejects_untrusted_host(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, _ = web_server
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.request("GET", "/api/token", headers={"Host": "attacker.example"})
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()

    assert response.status == 403
    assert payload == {"error": "untrusted host"}


def test_server_allows_explicitly_requested_hostname() -> None:
    server = RunnerHTTPServer(("localhost", 0))
    try:
        _, port = server.server_address
        assert server.is_allowed_host(f"localhost:{port}")
    finally:
        server.server_close()


def test_web_server_close_stops_running_process() -> None:
    runner = PythonRunner(stop_timeout=0.5)
    server = RunnerHTTPServer(("127.0.0.1", 0), runner)
    runner.start("import time; time.sleep(60)")

    server.server_close()

    assert wait_until_finished(runner).state == "stopped"
