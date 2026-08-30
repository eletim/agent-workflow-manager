from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

import pytest

from purplemux_client import (
    CreateSessionRequest,
    MutationOutcomeUnknown,
    PurpleMuxCLIClient,
    ResultNotReady,
    SessionReadyTimeout,
    WorkerFailure,
    WorkerInterrupted,
    WorkerNeedsInput,
)


def completed(data: object, *, returncode: int = 0, stderr: str = ""):
    stdout = data if isinstance(data, str) else json.dumps(data)
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeRunner:
    def __init__(
        self,
        outcomes: Sequence[
            subprocess.CompletedProcess[str] | subprocess.TimeoutExpired | OSError
        ],
    ) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output is True
        assert text is True
        assert check is False
        self.calls.append(list(args))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def client(runner: FakeRunner, **kwargs: object) -> PurpleMuxCLIClient:
    return PurpleMuxCLIClient(
        "ws-test",
        poll_interval_seconds=0,
        runner=runner,
        sleep=lambda _: None,
        **kwargs,
    )


def request(worker: str = "codex", command: str | None = None) -> CreateSessionRequest:
    return CreateSessionRequest(
        worker=worker,
        cwd="/workspace/project",
        command=command or worker,
    )


def baseline(
    *,
    state: str = "idle",
    event_seq: int = 1,
    result_status: str = "not-ready",
    text: str | None = None,
    completion_timestamp: int | None = None,
) -> list[subprocess.CompletedProcess[str]]:
    return [
        completed({"cliState": state, "alive": True, "eventSeq": event_seq}),
        completed(
            {
                "status": result_status,
                "text": text,
                "completionTimestamp": completion_timestamp,
            }
        ),
    ]


def test_create_response_parsing_and_codex_panel_type() -> None:
    runner = FakeRunner([completed({"tabId": "tab-123"})])

    assert client(runner).create_session(request()) == "tab-123"
    assert runner.calls == [
        ["purplemux", "tab", "create", "-w", "ws-test", "-t", "codex-cli"]
    ]


@pytest.mark.parametrize(
    ("worker", "command"),
    [("claude-code", "claude"), ("unknown", "claude")],
)
def test_create_selects_claude_panel(worker: str, command: str) -> None:
    runner = FakeRunner([completed({"tabId": "tab-claude"})])

    client(runner).create_session(request(worker, command))

    assert runner.calls[0][-1] == "claude-code"


def test_create_rejects_unknown_provider() -> None:
    runner = FakeRunner([])

    with pytest.raises(WorkerFailure, match="unsupported PurpleMux worker"):
        client(runner).create_session(request("unknown"))

    assert runner.calls == []


def test_create_requires_tab_id() -> None:
    runner = FakeRunner([completed({"workspaceId": "ws-test"})])

    with pytest.raises(WorkerFailure, match="did not return a tab ID"):
        client(runner).create_session(request())


def test_read_status_returns_structured_status() -> None:
    status = {
        "tabId": "tab-1",
        "cliState": "idle",
        "alive": True,
        "eventSeq": 3,
    }
    runner = FakeRunner([completed(status)])

    assert client(runner).read_status("tab-1") == status
    assert runner.calls[0][1:3] == ["tab", "status"]


def test_wait_until_ready_polls_until_idle() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "inactive", "alive": True}),
            completed({"cliState": "idle", "alive": True}),
        ]
    )

    client(runner).wait_until_ready("tab-1", 1)

    assert len(runner.calls) == 2


def test_wait_until_ready_accepts_ready_for_review() -> None:
    runner = FakeRunner([completed({"cliState": "ready-for-review", "alive": True})])

    client(runner).wait_until_ready("tab-1", 1)


def test_ready_timeout_reports_last_state() -> None:
    runner = FakeRunner([completed({"cliState": "inactive", "alive": True})])

    with pytest.raises(SessionReadyTimeout, match="last cliState=inactive"):
        client(runner).wait_until_ready("tab-1", 0)


def test_send_records_baseline_then_uses_public_cli() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "do work")

    assert [call[2] for call in runner.calls] == ["status", "result", "send"]
    assert runner.calls[-1][-1] == "do work"


def test_send_rejects_empty_input_without_running_cli() -> None:
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="must not be empty"):
        client(runner).send_input("tab-1", "")


def test_wait_for_completion_correlates_busy_ready_and_result() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": 200,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "done",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "done"


def test_stale_ready_state_is_rejected_until_fresh_event_and_result() -> None:
    runner = FakeRunner(
        [
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 10,
                    "readyForReviewAt": 100,
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 1,
                }
            ),
            completed({"status": "sent"}),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 10,
                    "readyForReviewAt": 100,
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 1,
                }
            ),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 12,
                    "readyForReviewAt": 200,
                    "lastEvent": {"name": "stop", "seq": 12},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "new",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "new"


def test_fresh_result_handles_short_turn_when_busy_event_is_missed() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "ready-for-review", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "fast",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "fast"


def test_idle_accepts_fresh_completion_after_ready_state_is_dismissed() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": None,
                    "dismissedAt": 300,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "dismissed",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "dismissed"


def test_idle_accepts_fresh_completion_when_busy_poll_is_missed() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": None,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "fast dismissed",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "fast dismissed"


def test_send_rejects_unavailable_baseline_event_sequence() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True}),
        ]
    )
    cli = client(runner)

    with pytest.raises(WorkerFailure, match="no event sequence"):
        cli.send_input("tab-1", "work")

    assert [call[2] for call in runner.calls] == ["status"]


def test_idle_rejects_stale_stop_event_and_result() -> None:
    runner = FakeRunner(
        [
            *baseline(
                event_seq=3,
                result_status="completed",
                text="old",
                completion_timestamp=10,
            ),
            completed({"status": "sent"}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": None,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerFailure, match="did not complete"):
        cli.wait_for_turn_completion("tab-1", 0)

    assert [call[2] for call in runner.calls] == ["status", "result", "send", "status"]


def test_idle_uses_baseline_last_event_sequence_to_reject_stale_stop() -> None:
    runner = FakeRunner(
        [
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 10,
                }
            ),
            completed({"status": "sent"}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "readyForReviewAt": None,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerFailure, match="did not complete"):
        cli.wait_for_turn_completion("tab-1", 0)

    assert [call[2] for call in runner.calls] == ["status", "result", "send", "status"]


def test_idle_waits_for_completed_result_after_fresh_stop_event() -> None:
    stopped = {
        "cliState": "idle",
        "alive": True,
        "eventSeq": 3,
        "readyForReviewAt": None,
        "lastEvent": {"name": "stop", "seq": 3},
    }
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed(stopped),
            completed({"status": "not-ready", "completionTimestamp": None}),
            completed(stopped),
            completed(
                {
                    "status": "completed",
                    "text": "published",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "published"


def test_idle_fresh_stop_with_interrupted_result_is_explicit() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": None,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
            completed(
                {
                    "status": "interrupted",
                    "reason": "turn-interrupted",
                    "interrupted": True,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerInterrupted, match="interrupted"):
        cli.wait_for_turn_completion("tab-1", 1)


def test_read_result_rejects_stale_result_for_pending_turn() -> None:
    runner = FakeRunner(
        [
            *baseline(
                state="ready-for-review",
                result_status="completed",
                text="old",
                completion_timestamp=10,
            ),
            completed({"status": "sent"}),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 10,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(ResultNotReady, match="stale"):
        cli.read_result("tab-1")


def test_older_completion_timestamp_is_not_fresh() -> None:
    runner = FakeRunner(
        [
            *baseline(
                state="ready-for-review",
                result_status="completed",
                text="baseline",
                completion_timestamp=2,
            ),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True}),
            completed({"cliState": "ready-for-review", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "older",
                    "completionTimestamp": 1,
                }
            ),
            completed({"cliState": "ready-for-review", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "fresh",
                    "completionTimestamp": 3,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "fresh"


def test_wait_requires_pending_input() -> None:
    runner = FakeRunner([])

    with pytest.raises(WorkerFailure, match="no pending input"):
        client(runner).wait_for_turn_completion("tab-1", 1)


def test_wait_raises_for_needs_input() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "needs-input", "alive": True}),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerNeedsInput, match="needs input"):
        cli.wait_for_turn_completion("tab-1", 1)


def test_wait_raises_when_agent_becomes_inactive() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True}),
            completed({"cliState": "inactive", "alive": True}),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerFailure, match="became inactive"):
        cli.wait_for_turn_completion("tab-1", 1)


def test_fresh_interrupt_event_is_explicit() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "eventSeq": 3,
                    "lastEvent": {"name": "interrupt", "seq": 3},
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerInterrupted, match="interrupted"):
        cli.wait_for_turn_completion("tab-1", 1)


def test_stale_interrupt_result_does_not_poison_reused_session() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 3}),
            completed(
                {
                    "status": "interrupted",
                    "reason": "turn-interrupted",
                    "interrupted": True,
                }
            ),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 4}),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 5,
                    "readyForReviewAt": 200,
                    "lastEvent": {"name": "stop", "seq": 5},
                }
            ),
            completed(
                {
                    "status": "interrupted",
                    "reason": "turn-interrupted",
                    "interrupted": True,
                }
            ),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 5,
                    "readyForReviewAt": 200,
                    "lastEvent": {"name": "stop", "seq": 5},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "reused",
                    "completionTimestamp": 20,
                    "interrupted": False,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "second turn")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "reused"


def test_read_result_rejects_stale_interrupt_for_reused_session() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 3}),
            completed(
                {
                    "status": "interrupted",
                    "reason": "turn-interrupted",
                    "interrupted": True,
                }
            ),
            completed({"status": "sent"}),
            completed(
                {
                    "status": "interrupted",
                    "reason": "turn-interrupted",
                    "interrupted": True,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "second turn")
    with pytest.raises(ResultNotReady, match="stale"):
        cli.read_result("tab-1")


def test_interrupted_result_is_explicit() -> None:
    runner = FakeRunner(
        [completed({"status": "interrupted", "reason": "turn-interrupted"})]
    )

    with pytest.raises(WorkerInterrupted, match="turn-interrupted"):
        client(runner).read_result("tab-1")


def test_read_result_returns_structured_text() -> None:
    runner = FakeRunner(
        [
            completed(
                {
                    "status": "completed",
                    "text": "structured output",
                    "completionTimestamp": 10,
                }
            )
        ]
    )

    assert client(runner).read_result("tab-1") == "structured output"


def test_not_ready_result_is_explicit() -> None:
    runner = FakeRunner(
        [completed({"status": "not-ready", "reason": "jsonl-unavailable"})]
    )

    with pytest.raises(ResultNotReady, match="jsonl-unavailable"):
        client(runner).read_result("tab-1")


@pytest.mark.parametrize("status", ["not-applicable", "unavailable"])
def test_unavailable_result_statuses_fail(status: str) -> None:
    runner = FakeRunner([completed({"status": status, "reason": "reason"})])

    with pytest.raises(WorkerFailure, match=status):
        client(runner).read_result("tab-1")


@pytest.mark.parametrize(
    "state", ["cancelled", "dead", "error", "failed", "stopped", "exited"]
)
def test_failed_runtime_states_are_explicit(state: str) -> None:
    runner = FakeRunner([completed({"cliState": state, "alive": True})])

    with pytest.raises(WorkerFailure, match=f"entered {state}"):
        client(runner).wait_until_ready("tab-1", 1)


def test_dead_runtime_is_not_ready() -> None:
    runner = FakeRunner([completed({"cliState": "idle", "alive": False})])

    with pytest.raises(WorkerFailure, match="entered idle"):
        client(runner).wait_until_ready("tab-1", 1)


def test_interrupt_uses_public_cli() -> None:
    runner = FakeRunner([completed({"status": "interrupted"})])

    client(runner).interrupt("tab-1")

    assert runner.calls[0][1:] == [
        "tab",
        "interrupt",
        "-w",
        "ws-test",
        "tab-1",
    ]


def test_close_uses_public_cli_and_accepts_plain_ok() -> None:
    runner = FakeRunner([completed("ok\n")])

    client(runner).close_session("tab-1")

    assert runner.calls[0][1:] == ["tab", "close", "-w", "ws-test", "tab-1"]


def test_capture_screen_is_diagnostic_and_separate_from_result() -> None:
    runner = FakeRunner([completed({"content": "diagnostic pane"})])

    assert client(runner).capture_screen("tab-1") == "diagnostic pane"
    assert runner.calls[0][2] == "capture"


def test_cli_non_zero_exit_includes_stderr() -> None:
    runner = FakeRunner([completed({}, returncode=2, stderr="server unavailable")])

    with pytest.raises(WorkerFailure, match="server unavailable"):
        client(runner).read_result("tab-1")


@pytest.mark.parametrize("output", ["not-json", "[]"])
def test_malformed_or_non_object_json(output: str) -> None:
    runner = FakeRunner([completed(output)])

    with pytest.raises(WorkerFailure, match="malformed JSON|non-object JSON"):
        client(runner).read_result("tab-1")


def test_os_error_is_wrapped() -> None:
    runner = FakeRunner([OSError("purplemux missing")])

    with pytest.raises(WorkerFailure, match="could not execute"):
        client(runner).read_status("tab-1")


@pytest.mark.parametrize("operation", ["status", "result", "capture"])
def test_read_timeout_is_retried(operation: str) -> None:
    timeout = subprocess.TimeoutExpired(["purplemux"], 2)
    response = {
        "status": {"cliState": "idle", "alive": True},
        "result": {"status": "completed", "text": "done"},
        "capture": {"content": "diagnostic"},
    }[operation]
    runner = FakeRunner([timeout, completed(response)])
    cli = client(runner)

    if operation == "status":
        assert cli.read_status("tab-1")["cliState"] == "idle"
    elif operation == "result":
        assert cli.read_result("tab-1") == "done"
    else:
        assert cli.capture_screen("tab-1") == "diagnostic"

    assert len(runner.calls) == 2


def test_read_timeout_fails_after_configured_retries() -> None:
    timeout = subprocess.TimeoutExpired(["purplemux"], 2)
    runner = FakeRunner([timeout, timeout])

    with pytest.raises(WorkerFailure, match="status timed out"):
        client(runner).read_status("tab-1")

    assert len(runner.calls) == 2


@pytest.mark.parametrize("operation", ["create", "send", "interrupt", "close"])
def test_mutation_timeout_is_not_retried(operation: str) -> None:
    timeout = subprocess.TimeoutExpired(["purplemux"], 2)
    outcomes: list[
        subprocess.CompletedProcess[str] | subprocess.TimeoutExpired | OSError
    ]
    if operation == "send":
        outcomes = [*baseline(), timeout]
    else:
        outcomes = [timeout]
    runner = FakeRunner(outcomes)
    cli = client(runner)

    with pytest.raises(MutationOutcomeUnknown, match="outcome is unknown"):
        if operation == "create":
            cli.create_session(request())
        elif operation == "send":
            cli.send_input("tab-1", "work")
        elif operation == "interrupt":
            cli.interrupt("tab-1")
        else:
            cli.close_session("tab-1")

    mutation_calls = [call for call in runner.calls if call[2] == operation]
    assert len(mutation_calls) == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"workspace_id": ""}, "workspace_id"),
        ({"poll_interval_seconds": -1}, "poll_interval_seconds"),
        ({"command_timeout_seconds": 0}, "command_timeout_seconds"),
        ({"read_timeout_retries": -1}, "read_timeout_retries"),
    ],
)
def test_configuration_validation(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {"workspace_id": "ws-test"}
    base.update(kwargs)

    with pytest.raises(ValueError, match=message):
        PurpleMuxCLIClient(**base)
