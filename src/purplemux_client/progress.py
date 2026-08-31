from __future__ import annotations

import json
import os
import threading
from typing import Literal

StepStatus = Literal["started", "completed", "failed"]

PROGRESS_FD_ENV = "PURPLEMUX_RUNNER_PROGRESS_FD"
MAX_PROGRESS_EVENT_BYTES = 4096
_write_lock = threading.Lock()


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

    fd_text = os.environ.get(PROGRESS_FD_ENV)
    if fd_text is None:
        return
    try:
        fd = int(fd_text)
    except ValueError:
        return

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
    encoded = (
        json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()
    if len(encoded) > MAX_PROGRESS_EVENT_BYTES:
        return
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
