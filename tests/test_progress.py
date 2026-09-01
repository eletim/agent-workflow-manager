from __future__ import annotations

import json
import os

import pytest

from purplemux_client import emit_step, resume_checkpoint, save_checkpoint, suspend_run
from purplemux_client.progress import (
    MAX_PROGRESS_EVENT_BYTES,
    PROGRESS_FD_ENV,
    RESUME_CHECKPOINT_ENV,
    SUSPENDED_EXIT_CODE,
)


def test_emit_step_writes_one_json_event(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(PROGRESS_FD_ENV, str(write_fd))
    try:
        emit_step(
            "review",
            "failed",
            iteration=2,
            attempt=1,
            message="checking",
            error="tests failed",
            workspace="ws-1",
            tab="tab-1",
        )
    finally:
        os.close(write_fd)

    with os.fdopen(read_fd, encoding="utf-8") as stream:
        event = json.loads(stream.read())

    assert event == {
        "name": "review",
        "status": "failed",
        "iteration": 2,
        "attempt": 1,
        "message": "checking",
        "error": "tests failed",
        "workspace": "ws-1",
        "tab": "tab-1",
    }


def test_emit_step_is_noop_outside_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROGRESS_FD_ENV, raising=False)

    emit_step("ordinary script", "completed")


def test_emit_step_drops_oversized_event(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(PROGRESS_FD_ENV, str(write_fd))
    try:
        emit_step("step", "failed", error="x" * MAX_PROGRESS_EVENT_BYTES)
    finally:
        os.close(write_fd)

    with os.fdopen(read_fd, "rb") as stream:
        assert stream.read() == b""


@pytest.mark.parametrize("status", ["pending", "retrying", "done"])
def test_emit_step_rejects_extra_statuses(status: str) -> None:
    with pytest.raises(ValueError, match="started, completed, or failed"):
        emit_step("step", status)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["iteration", "attempt"])
def test_emit_step_requires_positive_counters(field: str) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        emit_step("step", "started", **{field: 0})  # type: ignore[arg-type]


def test_checkpoint_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(PROGRESS_FD_ENV, str(write_fd))
    try:
        save_checkpoint("sessions ready", {"workspace": "ws-1", "tab": "tab-1"})
    finally:
        os.close(write_fd)

    with os.fdopen(read_fd, encoding="utf-8") as stream:
        event = json.loads(stream.read())
    assert event == {
        "type": "checkpoint",
        "name": "sessions ready",
        "data": {"workspace": "ws-1", "tab": "tab-1"},
    }

    monkeypatch.setenv(
        RESUME_CHECKPOINT_ENV,
        json.dumps({"name": event["name"], "data": event["data"]}),
    )
    assert resume_checkpoint() is not None
    assert resume_checkpoint().name == "sessions ready"  # type: ignore[union-attr]
    assert resume_checkpoint().data["workspace"] == "ws-1"  # type: ignore[union-attr]


def test_suspend_run_publishes_reason_and_uses_distinct_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(PROGRESS_FD_ENV, str(write_fd))
    try:
        with pytest.raises(SystemExit) as raised:
            suspend_run("answer the agent question")
    finally:
        os.close(write_fd)

    assert raised.value.code == SUSPENDED_EXIT_CODE
    with os.fdopen(read_fd, encoding="utf-8") as stream:
        assert json.loads(stream.read()) == {
            "type": "suspended",
            "reason": "answer the agent question",
        }
