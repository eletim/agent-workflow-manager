from __future__ import annotations

from pathlib import Path

import pytest

from purplemux_client.client import ShellCommandRequest, ShellResult
from purplemux_client.environment_setup_execution import (
    execute_environment_setup_commands,
)
from purplemux_client.errors import (
    MutationOutcomeUnknown,
    ResultNotReady,
    WorkerFailure,
)


class ManagedClient:
    workspace_id = "ws-setup"

    def __init__(self, outcomes: dict[str, list[int | None]]) -> None:
        self.outcomes = outcomes
        self.requests: list[ShellCommandRequest] = []
        self.results: dict[str, int | None] = {}
        self.launch_error: str | None = None
        self.launch_uncertain = False
        self.interrupted: list[str] = []

    def start_shell(self, request: ShellCommandRequest, *, on_created=None) -> str:
        assert request.deadline_check is not None
        tab = f"tab-{len(self.requests) + 1}"
        self.requests.append(request)
        self.results[tab] = self.outcomes[request.command].pop(0)
        if on_created is not None:
            on_created(tab, "/managed/result.json")
        if request.command == self.launch_error:
            if self.launch_uncertain:
                raise MutationOutcomeUnknown("send outcome unknown")
            raise WorkerFailure("send failed")
        return tab

    def wait_for_shell_completion(self, tab: str, _timeout: float) -> None:
        if self.results[tab] is None:
            raise WorkerFailure("shell did not finish")

    def read_shell_result(self, tab: str) -> ShellResult:
        result = self.results[tab]
        if result is None:
            raise ResultNotReady("still running")
        return ShellResult(result)

    def capture_screen(self, tab: str) -> str:
        return f"observed output from {tab}"

    def read_status(self, _tab: str) -> dict[str, object]:
        return {"alive": True}

    def interrupt(self, tab: str) -> None:
        self.interrupted.append(tab)


def execute(client: ManagedClient, tmp_path: Path, **overrides: object) -> dict:
    args: dict = {
        "client": client,
        "build": None,
        "start": None,
        "ready_check": None,
        "verification_command": None,
        "cwd": str(tmp_path),
        "remaining": lambda: 5,
    }
    args.update(overrides)
    return execute_environment_setup_commands(**args)


def test_runs_supplied_commands_in_managed_terminals_in_order(tmp_path: Path) -> None:
    client = ManagedClient({"build": [0], "start": [None], "ready": [0]})
    attempt = execute(
        client, tmp_path, build="build", start="start", ready_check="ready"
    )
    assert attempt["failure"] is None
    assert [request.command for request in client.requests] == [
        "build",
        "start",
        "ready",
    ]
    assert all(request.cwd == str(tmp_path) for request in client.requests)
    assert attempt["checks"]["start"]["tab_id"] == "tab-2"
    assert attempt["checks"]["start"]["running"] is True
    assert attempt["verification"]["tab_id"] == "tab-3"
    assert attempt["verification"]["output"] == "observed output from tab-3"
    assert attempt["service_tab"] == "tab-2"


def test_omitted_commands_still_require_usability_check(tmp_path: Path) -> None:
    client = ManagedClient({"verify": [0]})
    missing = execute(client, tmp_path)
    assert missing["failed_stage"] == "ready_check"
    assert not client.requests
    observed = execute(client, tmp_path, verification_command="verify")
    assert observed["failure"] is None
    assert observed["checks"] == {}
    assert observed["verification"]["exit_code"] == 0


def test_failed_command_is_observed_and_can_be_retried(tmp_path: Path) -> None:
    client = ManagedClient({"build": [3, 0], "start": [None], "ready": [0]})
    failed = execute(
        client, tmp_path, build="build", start="start", ready_check="ready"
    )
    assert failed["failed_stage"] == "build"
    assert failed["checks"]["build"]["exit_code"] == 3
    assert [request.command for request in client.requests] == ["build"]
    retried = execute(
        client,
        tmp_path,
        build="build",
        start="start",
        ready_check="ready",
        resume_at=failed["failed_stage"],
    )
    assert retried["failure"] is None
    assert [request.command for request in client.requests] == [
        "build",
        "build",
        "start",
        "ready",
    ]


def test_failed_ready_check_retains_service_for_recovery(tmp_path: Path) -> None:
    client = ManagedClient({"start": [None], "ready": [1, 0]})
    failed = execute(client, tmp_path, start="start", ready_check="ready")
    assert failed["failed_stage"] == "ready_check"
    assert failed["service_tab"] == "tab-1"
    retried = execute(
        client,
        tmp_path,
        start="start",
        ready_check="ready",
        resume_at="ready_check",
        service_tab=failed["service_tab"],
    )
    assert retried["failure"] is None
    assert [request.command for request in client.requests] == [
        "start",
        "ready",
        "ready",
    ]


def test_service_failure_is_observable(tmp_path: Path) -> None:
    client = ManagedClient({"start": [2], "ready": [0]})
    failed = execute(client, tmp_path, start="start", ready_check="ready")
    assert failed["failed_stage"] == "start"
    assert failed["checks"]["start"]["exit_code"] == 2
    assert failed["checks"]["start"]["output"] == "observed output from tab-1"
    assert [request.command for request in client.requests] == ["start"]


def test_managed_shell_launch_failure_keeps_terminal_for_diagnosis(
    tmp_path: Path,
) -> None:
    client = ManagedClient({"build": [None]})
    client.launch_error = "build"
    failed = execute(client, tmp_path, build="build", verification_command="verify")
    assert failed["failed_stage"] == "build"
    assert failed["checks"]["build"]["tab_id"] == "tab-1"
    assert "send failed" in failed["failure"]


@pytest.mark.parametrize("exit_code", [0, 3])
def test_uncertain_build_launch_reconciles_structured_result(
    tmp_path: Path, exit_code: int
) -> None:
    client = ManagedClient({"build": [exit_code], "verify": [0]})
    client.launch_error = "build"
    client.launch_uncertain = True
    attempt = execute(client, tmp_path, build="build", verification_command="verify")
    assert attempt["checks"]["build"]["exit_code"] == exit_code
    assert [request.command for request in client.requests] == (
        ["build", "verify"] if exit_code == 0 else ["build"]
    )
    assert (attempt["failure"] is None) is (exit_code == 0)


@pytest.mark.parametrize("stage", ["build", "start"])
def test_unresolved_launch_stops_without_replay(tmp_path: Path, stage: str) -> None:
    client = ManagedClient({stage: [None], "verify": [0]})
    client.launch_error = stage
    client.launch_uncertain = True
    with pytest.raises(MutationOutcomeUnknown, match="unresolved"):
        execute(client, tmp_path, **{stage: stage}, verification_command="verify")
    assert [request.command for request in client.requests] == [stage]


def test_uncertain_start_launch_with_completed_result_continues(tmp_path: Path) -> None:
    client = ManagedClient({"start": [0], "ready": [0]})
    client.launch_error = "start"
    client.launch_uncertain = True
    attempt = execute(client, tmp_path, start="start", ready_check="ready")
    assert attempt["failure"] is None
    assert [request.command for request in client.requests] == ["start", "ready"]


def test_command_deadline_interrupts_managed_terminal(tmp_path: Path) -> None:
    class TimedOutClient(ManagedClient):
        def wait_for_shell_completion(self, _tab: str, _timeout: float) -> None:
            raise WorkerFailure("shell did not finish")

    client = TimedOutClient({"build": [None]})
    calls = 0

    def remaining() -> float:
        nonlocal calls
        calls += 1
        if calls >= 4:
            raise TimeoutError("Environment Setup timed out")
        return 1

    with pytest.raises(TimeoutError, match="timed out"):
        execute(client, tmp_path, build="build", remaining=remaining)
    assert client.interrupted == ["tab-1"]


@pytest.mark.parametrize("stage", ["build", "ready_check"])
def test_deadline_after_launch_interrupts_command(tmp_path: Path, stage: str) -> None:
    command = "build" if stage == "build" else "ready"
    client = ManagedClient({command: [None]})
    calls = 0

    def remaining() -> float:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("Environment Setup timed out")
        return 1

    with pytest.raises(TimeoutError, match="timed out"):
        execute(client, tmp_path, **{stage: command}, remaining=remaining)
    assert client.interrupted == ["tab-1"]
