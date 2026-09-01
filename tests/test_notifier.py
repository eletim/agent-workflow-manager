from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

import pytest

from purplemux_client.notifier import NotificationResult, NotifyCLI
from purplemux_client.runner import PythonRunner, RunnerSnapshot


class FinishedProcess:
    def __init__(self, return_code: int = 0) -> None:
        self.pid = 12345
        self.return_code = return_code

    def wait(self, timeout: float | None = None) -> int:
        return self.return_code


def _record_process_start(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[list[str]],
    *,
    return_code: int = 0,
) -> None:
    def start(command: list[str], **kwargs: object) -> FinishedProcess:
        calls.append(command)
        assert kwargs["start_new_session"] is True
        return FinishedProcess(return_code)

    monkeypatch.setattr(subprocess, "Popen", start)


def _wait_until_finished(runner: PythonRunner) -> RunnerSnapshot:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = runner.snapshot()
        if snapshot.state != "running":
            return snapshot
        time.sleep(0.02)
    raise AssertionError("runner did not finish")


class RecordingNotifier:
    def __init__(self, result: NotificationResult | None = None) -> None:
        self.calls: list[tuple[int, str, int | None]] = []
        self.result = result or NotificationResult(True, True, "notification sent")

    def notify_terminal(
        self, *, run_id: int, state: str, exit_code: int | None
    ) -> NotificationResult:
        self.calls.append((run_id, state, exit_code))
        return self.result

    def close(self) -> None:
        return


@pytest.mark.parametrize(
    ("code", "expected_state", "expected_exit_code"),
    [('print("ok")', "success", 0), ("raise SystemExit(7)", "failed", 7)],
)
def test_terminal_result_attempts_exactly_one_notification(
    code: str, expected_state: str, expected_exit_code: int
) -> None:
    notifier = RecordingNotifier()
    runner = PythonRunner(notifier=notifier)
    try:
        run_id = runner.start(code)
        result = _wait_until_finished(runner)
        deadline_calls = notifier.calls
        for _ in range(100):
            if deadline_calls:
                break
            time.sleep(0.01)
    finally:
        runner.close()

    assert result.state == expected_state
    assert notifier.calls == [(run_id, expected_state, expected_exit_code)]


def test_suspended_result_attempts_exactly_one_attention_notification() -> None:
    notifier = RecordingNotifier()
    runner = PythonRunner(notifier=notifier)
    code = """\
from purplemux_client import save_checkpoint, suspend_run
save_checkpoint("waiting", {"tab": "tab-1"})
suspend_run("answer tab-1")
"""
    try:
        run_id = runner.start(code)
        result = _wait_until_finished(runner)
        for _ in range(100):
            if notifier.calls:
                break
            time.sleep(0.01)
    finally:
        runner.close()

    assert result.state == "suspended"
    assert notifier.calls == [(run_id, "suspended", 75)]


def test_stopped_notification_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/notify")
    _record_process_start(monkeypatch, calls)
    notifier = NotifyCLI(enabled=True)

    result = notifier.notify_terminal(run_id=4, state="stopped", exit_code=-15)

    assert result.attempted is False
    assert calls == []


def test_stopped_notification_can_be_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/notify")
    _record_process_start(monkeypatch, calls)
    notifier = NotifyCLI(enabled=True, notify_stopped=True)

    result = notifier.notify_terminal(run_id=4, state="stopped", exit_code=-15)

    assert result.delivered is True
    assert len(calls) == 1
    assert calls[0][0:2] == ["/usr/bin/notify", "send"]
    assert "Workflow stopped" in calls[0]


@pytest.mark.parametrize(
    ("state", "options", "diagnostic"),
    [
        ("success", {"notify_success": False}, "success notifications disabled"),
        ("failed", {"notify_failure": False}, "failure notifications disabled"),
        ("stopped", {"notify_stopped": False}, "stopped notifications disabled"),
    ],
)
def test_terminal_policy_can_disable_each_state(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    options: dict[str, bool],
    diagnostic: str,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/notify")
    _record_process_start(monkeypatch, calls)

    result = NotifyCLI(enabled=True, **options).notify_terminal(  # type: ignore[arg-type]
        run_id=4,
        state=state,
        exit_code=0,  # type: ignore[arg-type]
    )

    assert result == NotificationResult(False, False, diagnostic)
    assert calls == []


def test_notify_command_failure_does_not_change_runner_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    notifier = RecordingNotifier(
        NotificationResult(True, False, "notify exited with status 2")
    )
    runner = PythonRunner(notifier=notifier)
    with caplog.at_level(logging.WARNING):
        try:
            runner.start('print("workflow result")')
            result = _wait_until_finished(runner)
            for _ in range(100):
                if caplog.records:
                    break
                time.sleep(0.01)
        finally:
            runner.close()

    assert result.state == "success"
    assert result.exit_code == 0
    assert "notify exited with status 2" in caplog.text


def test_notify_unavailable_is_safe() -> None:
    notifier = NotifyCLI(enabled=True, executable="definitely-not-installed-notify")

    result = notifier.notify_terminal(run_id=1, state="success", exit_code=0)

    assert result == NotificationResult(True, False, "notify command unavailable")


def test_disabled_notify_is_safe() -> None:
    notifier = NotifyCLI(enabled=False)

    result = notifier.notify_terminal(run_id=1, state="success", exit_code=0)

    assert result == NotificationResult(False, False, "notifications disabled")


def test_notify_failure_diagnostic_does_not_surface_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    secret = "tk_super_secret_value"
    monkeypatch.setenv("NOTIFY_TOKEN", secret)
    executable = tmp_path / "notify"
    executable.write_text(
        f"#!/usr/bin/env bash\nprintf 'bad token: {secret}\\n' >&2\nexit 2\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    runner = PythonRunner(notifier=NotifyCLI(enabled=True, executable=str(executable)))

    with caplog.at_level(logging.WARNING):
        try:
            runner.start("")
            _wait_until_finished(runner)
            for _ in range(100):
                if caplog.records:
                    break
                time.sleep(0.01)
        finally:
            runner.close()

    assert secret not in caplog.text
    assert "notify exited with status 2" in caplog.text


def test_notify_command_contains_required_success_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/notify")
    _record_process_start(monkeypatch, calls)

    NotifyCLI(enabled=True).notify_terminal(run_id=12, state="success", exit_code=0)

    assert calls == [
        [
            "/usr/bin/notify",
            "send",
            "--title",
            "Workflow completed",
            "--message",
            "Run 12 finished with state: success",
        ]
    ]


def test_notify_command_contains_required_failure_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/notify")
    _record_process_start(monkeypatch, calls)

    NotifyCLI(enabled=True).notify_terminal(run_id=13, state="failed", exit_code=9)

    assert "Workflow failed" in calls[0]
    assert "Run 13 finished with state: failed and exit code 9" in calls[0]


def test_suspended_notification_is_attention_not_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/notify")
    _record_process_start(monkeypatch, calls)

    result = NotifyCLI(enabled=True, notify_failure=False).notify_terminal(
        run_id=14, state="suspended", exit_code=75
    )

    assert result.delivered is True
    assert calls == [
        [
            "/usr/bin/notify",
            "send",
            "--title",
            "Workflow needs attention",
            "--message",
            "Run 14 is suspended and waiting for manual input or recovery",
        ]
    ]


def test_test_notification_uses_public_cli_and_config_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def start(command: list[str], **kwargs: object) -> FinishedProcess:
        calls.append((command, kwargs))
        return FinishedProcess()

    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/notify")
    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setenv("NOTIFY_SERVER", "https://old.example")
    monkeypatch.setenv("NOTIFY_TOPIC", "old-topic")
    monkeypatch.setenv("NOTIFY_TOKEN", "tk_old_secret")

    notifier = NotifyCLI(config_path="/tmp/external-notify-config")
    notifier.configure_transport(
        server="https://new.example",
        topic="new-topic",
        replacement_token="tk_new_secret",
    )
    result = notifier.send_test()

    assert result.delivered is True
    command, options = calls[0]
    assert command == [
        "/usr/bin/notify",
        "send",
        "--title",
        "Agent Workflow Manager",
        "--message",
        "Test notification",
    ]
    environment = options["env"]
    assert isinstance(environment, dict)
    assert environment["NOTIFY_CONFIG"] == "/tmp/external-notify-config"
    assert environment["NOTIFY_SERVER"] == "https://new.example"
    assert environment["NOTIFY_TOPIC"] == "new-topic"
    assert environment["NOTIFY_TOKEN"] == "tk_new_secret"
    assert "tk_new_secret" not in command
    assert options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL


def test_test_notification_timeout_is_bounded(tmp_path: Path) -> None:
    executable = tmp_path / "notify"
    executable.write_text("#!/usr/bin/env bash\nsleep 60\n", encoding="utf-8")
    executable.chmod(0o755)

    started = time.monotonic()
    result = NotifyCLI(executable=str(executable), timeout=0.1).send_test()

    assert time.monotonic() - started < 2
    assert result == NotificationResult(True, False, "notify command timed out")


def test_notify_timeout_kills_child_process_group(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    executable = tmp_path / "notify"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "sleep 60 &\n"
        f"printf '%s' \"$!\" >{child_pid_file!s}\n"
        "wait\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    result = NotifyCLI(
        enabled=True, executable=str(executable), timeout=0.2
    ).notify_terminal(run_id=1, state="success", exit_code=0)

    assert result == NotificationResult(True, False, "notify command timed out")
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _process_is_live(child_pid):
        time.sleep(0.02)
    assert not _process_is_live(child_pid)


def test_runner_close_reaps_blocking_notification_tree(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "close-child.pid"
    executable = tmp_path / "notify"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "sleep 60 &\n"
        f"printf '%s' \"$!\" >{child_pid_file!s}\n"
        "wait\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    runner = PythonRunner(
        notifier=NotifyCLI(enabled=True, executable=str(executable), timeout=10)
    )
    runner.start("")
    _wait_until_finished(runner)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not child_pid_file.exists():
        time.sleep(0.01)
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))

    started_close = time.monotonic()
    runner.close()

    assert time.monotonic() - started_close < 2
    assert not _process_is_live(child_pid)


def _process_is_live(pid: int) -> bool:
    try:
        output = subprocess.check_output(
            ["ps", "-o", "stat=", "-p", str(pid)], text=True
        ).strip()
    except subprocess.CalledProcessError:
        return False
    return bool(output) and not output.startswith("Z")
