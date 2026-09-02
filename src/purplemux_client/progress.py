from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

StepStatus = Literal["started", "completed", "failed"]
FindingCategory = Literal["runtime", "git", "github"]
FindingStatus = Literal["passed", "failed", "info"]

PROGRESS_FD_ENV = "PURPLEMUX_RUNNER_PROGRESS_FD"
RESUME_CHECKPOINT_ENV = "PURPLEMUX_RUNNER_RESUME_CHECKPOINT"
MAX_PROGRESS_EVENT_BYTES = 4096
SUSPENDED_EXIT_CODE = 75
_write_lock = threading.Lock()


@dataclass(frozen=True)
class ResumeCheckpoint:
    """A workflow-defined safe boundary supplied to an explicit resumed attempt."""

    name: str
    data: dict[str, str]


def emit_step(
    name: str,
    status: StepStatus,
    *,
    iteration: int | None = None,
    attempt: int | None = None,
    message: str | None = None,
    error: str | None = None,
    workspace: str | None = None,
    tab: str | None = None,
) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("step name must be a non-empty string")
    if status not in ("started", "completed", "failed"):
        raise ValueError("status must be started, completed, or failed")
    _validate_number("iteration", iteration)
    _validate_number("attempt", attempt)
    for field_name, value in (
        ("message", message),
        ("error", error),
        ("workspace", workspace),
        ("tab", tab),
    ):
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{field_name} must be a string or None")

    event: dict[str, str | int] = {"name": name, "status": status}
    for key, value in (
        ("iteration", iteration),
        ("attempt", attempt),
        ("message", message),
        ("error", error),
        ("workspace", workspace),
        ("tab", tab),
    ):
        if value is not None:
            event[key] = value
    _write_event(event, drop_oversized=True)


def emit_finding(
    category: FindingCategory, message: str, *, status: FindingStatus = "passed"
) -> None:
    """Publish an observed readiness/topology fact without controlling execution."""
    if category not in ("runtime", "git", "github"):
        raise ValueError("finding category must be runtime, git, or github")
    if status not in ("passed", "failed", "info"):
        raise ValueError("finding status must be passed, failed, or info")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("finding message must be non-empty")
    _write_event(
        {"type": "finding", "category": category, "status": status, "message": message},
        drop_oversized=True,
    )


def save_checkpoint(name: str, data: Mapping[str, str] | None = None) -> None:
    """Publish a safe resume boundary after its side effects are complete.

    The workflow remains responsible for using :func:`resume_checkpoint` to skip
    completed work and for validating any external state before continuing. The
    continuation from this point must remain replay-safe until another checkpoint
    replaces it.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("checkpoint name must be a non-empty string")
    checkpoint_data = dict(data or {})
    if any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, str)
        or "\0" in key
        or "\0" in value
        for key, value in checkpoint_data.items()
    ):
        raise TypeError(
            "checkpoint data must contain non-empty string keys and string values"
        )
    _write_event({"type": "checkpoint", "name": name, "data": checkpoint_data})


def resume_checkpoint() -> ResumeCheckpoint | None:
    """Return the explicit checkpoint for this attempt, if it is a resume."""
    value = os.environ.get(RESUME_CHECKPOINT_ENV)
    if value is None:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Runner supplied an invalid resume checkpoint") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Runner supplied an invalid resume checkpoint")
    name = payload.get("name")
    data = payload.get("data")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(data, dict)
        or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in data.items()
        )
    ):
        raise RuntimeError("Runner supplied an invalid resume checkpoint")
    return ResumeCheckpoint(name=name, data=dict(data))


def suspend_run(reason: str) -> None:
    """End this attempt as human-suspended while preserving its checkpoint."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("suspension reason must be a non-empty string")
    _write_event({"type": "suspended", "reason": reason})
    raise SystemExit(SUSPENDED_EXIT_CODE)


def _write_event(event: Mapping[str, object], *, drop_oversized: bool = False) -> None:
    fd_text = os.environ.get(PROGRESS_FD_ENV)
    if fd_text is None:
        return
    try:
        fd = int(fd_text)
    except ValueError:
        return
    encoded = (
        json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()
    if len(encoded) > MAX_PROGRESS_EVENT_BYTES:
        if drop_oversized:
            return
        raise ValueError("Runner event exceeds 4096 encoded bytes")
    with _write_lock:
        view = memoryview(encoded)
        while view:
            try:
                written = os.write(fd, view)
            except OSError:
                return
            view = view[written:]


def _validate_number(name: str, value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError(f"{name} must be an integer or None")
    if value is not None and value < 1:
        raise ValueError(f"{name} must be positive")
