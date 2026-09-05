from __future__ import annotations

import json
import os

import pytest

from purplemux_client import (
    emit_step,
    register_run_resource,
)
from purplemux_client.progress import (
    MAX_PROGRESS_EVENT_BYTES,
    PROGRESS_FD_ENV,
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


def test_emit_step_truncates_oversized_error_after_json_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(PROGRESS_FD_ENV, str(write_fd))
    try:
        emit_step(
            "step",
            "failed",
            error="failure context\n" + ('"\\\0' * MAX_PROGRESS_EVENT_BYTES),
            workspace="ws-1",
            tab="tab-1",
        )
    finally:
        os.close(write_fd)

    with os.fdopen(read_fd, "rb") as stream:
        encoded = stream.read()

    assert len(encoded) <= MAX_PROGRESS_EVENT_BYTES
    event = json.loads(encoded)
    assert event["name"] == "step"
    assert event["status"] == "failed"
    assert event["workspace"] == "ws-1"
    assert event["tab"] == "tab-1"
    assert event["error"].startswith("failure context\n")
    assert event["error"].endswith("\n[error truncated]")


def test_emit_step_still_drops_oversized_event_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(PROGRESS_FD_ENV, str(write_fd))
    try:
        emit_step("step", "completed", message="x" * MAX_PROGRESS_EVENT_BYTES)
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


def test_register_run_resource_writes_structured_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(PROGRESS_FD_ENV, str(write_fd))
    try:
        register_run_resource(
            "purplemux_tab",
            "tab-1",
            {"workspace_id": "ws-1", "panel_type": "codex-cli"},
        )
    finally:
        os.close(write_fd)

    with os.fdopen(read_fd, encoding="utf-8") as stream:
        event = json.loads(stream.read())
    assert event == {
        "type": "resource",
        "kind": "purplemux_tab",
        "identity": "tab-1",
        "metadata": {"workspace_id": "ws-1", "panel_type": "codex-cli"},
    }
