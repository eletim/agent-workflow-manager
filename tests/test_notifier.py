from __future__ import annotations

import logging
import subprocess
import time

import pytest

from purplemux_client.notifier import NotificationResult, NotifyCLI
from purplemux_client.runner import PythonRunner, RunnerSnapshot


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


def test_stopped_notification_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/notify")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0)
        ),
    )
    notifier = NotifyCLI(enabled=True)

    result = notifier.notify_terminal(run_id=4, state="stopped", exit_code=-15)

    assert result.attempted is False
    assert calls == []


def test_stopped_notification_can_be_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/notify")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0)
        ),
    )
    notifier = NotifyCLI(enabled=True, notify_stopped=True)

    result = notifier.notify_terminal(run_id=4, state="stopped", exit_code=-15)

    assert result.delivered is True
    assert len(calls) == 1
    assert calls[0][0:2] == ["/usr/bin/notify", "send"]
    assert "Workflow stopped" in calls[0]


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
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "tk_super_secret_value"
    monkeypatch.setenv("NOTIFY_TOKEN", secret)
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/notify")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 2, stdout="", stderr=f"bad token: {secret}"
        ),
    )
    runner = PythonRunner(notifier=NotifyCLI(enabled=True))

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
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0)
        ),
    )

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
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0)
        ),
    )

    NotifyCLI(enabled=True).notify_terminal(run_id=13, state="failed", exit_code=9)

    assert "Workflow failed" in calls[0]
    assert "Run 13 finished with state: failed and exit code 9" in calls[0]
