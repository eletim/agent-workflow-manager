from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Mapping
from typing import Literal

StepStatus = Literal["started", "completed", "failed"]
FindingCategory = Literal["runtime", "git", "github"]
FindingStatus = Literal["passed", "failed", "info"]
RunResourceKind = Literal[
    "purplemux_tab",
    "managed_shell_result",
    "purplemux_workspace",
    "git_worktree",
]

PROGRESS_FD_ENV = "PURPLEMUX_RUNNER_PROGRESS_FD"
RESOURCE_ACK_FD_ENV = "PURPLEMUX_RUNNER_RESOURCE_ACK_FD"
MAX_PROGRESS_EVENT_BYTES = 4096
_TRUNCATED_ERROR_SUFFIX = "\n[error truncated]"
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


def register_run_resource(
    kind: RunResourceKind,
    identity: str,
    metadata: Mapping[str, str] | None = None,
) -> None:
    """Register an authoritatively identified resource with the current run.

    Registration is observational and does not mutate the resource. Outside the
    Runner it is a no-op, like progress events.
    """
    if kind not in (
        "purplemux_tab",
        "managed_shell_result",
        "purplemux_workspace",
        "git_worktree",
    ):
        raise ValueError("unsupported run resource kind")
    if not isinstance(identity, str) or not identity or "\0" in identity:
        raise ValueError("resource identity must be a non-empty string without nulls")
    resource_metadata = dict(metadata or {})
    if any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, str)
        or "\0" in key
        or "\0" in value
        for key, value in resource_metadata.items()
    ):
        raise TypeError(
            "resource metadata must contain non-empty string keys and string values"
        )
    _write_event(
        {
            "type": "resource",
            "kind": kind,
            "identity": identity,
            "metadata": resource_metadata,
        }
    )


def acknowledge_run_resource(
    phase: Literal["pending", "verified"],
    kind: RunResourceKind,
    identity: str,
    metadata: Mapping[str, str],
) -> None:
    """Synchronously establish or finalize manager-visible resource ownership.

    Outside a managed Runner this retains the public helper's historical no-op
    behavior. Inside one, the workflow cannot continue until the RunRecord has
    accepted the ownership evidence.
    """
    if phase not in ("pending", "verified"):
        raise ValueError("resource phase must be pending or verified")
    # Dry Run has a diagnostic progress pipe but deliberately no ownership
    # manager; its mutation boundary exits before a resource can be created.
    if os.environ.get("AGENT_WORKFLOW_MANAGER_DRY_RUN_FD") is not None:
        return
    progress_fd = os.environ.get(PROGRESS_FD_ENV)
    ack_fd = os.environ.get(RESOURCE_ACK_FD_ENV)
    if progress_fd is None and ack_fd is None:
        return
    if progress_fd is None or ack_fd is None:
        raise RuntimeError("Runner resource ownership channel is incomplete")
    try:
        output_fd = int(progress_fd)
        input_fd = int(ack_fd)
    except ValueError as exc:
        raise RuntimeError("Runner resource ownership channel is invalid") from exc

    token = uuid.uuid4().hex
    event = {
        "type": "resource_ownership",
        "phase": phase,
        "token": token,
        "kind": kind,
        "identity": identity,
        "metadata": dict(metadata),
    }
    encoded = _encode_event(event)
    if len(encoded) > MAX_PROGRESS_EVENT_BYTES:
        raise ValueError("Runner resource ownership event exceeds 4096 encoded bytes")
    with _write_lock:
        view = memoryview(encoded)
        while view:
            try:
                written = os.write(output_fd, view)
            except OSError as exc:
                raise RuntimeError(
                    "Runner resource ownership event could not be delivered"
                ) from exc
            view = view[written:]
        response = bytearray()
        while not response.endswith(b"\n"):
            try:
                chunk = os.read(input_fd, MAX_PROGRESS_EVENT_BYTES + 1 - len(response))
            except OSError as exc:
                raise RuntimeError(
                    "Runner resource ownership acknowledgement failed"
                ) from exc
            if not chunk:
                raise RuntimeError("Runner resource ownership was not acknowledged")
            response.extend(chunk)
            if len(response) > MAX_PROGRESS_EVENT_BYTES:
                raise RuntimeError(
                    "Runner resource ownership acknowledgement is invalid"
                )
        try:
            acknowledgement = json.loads(response)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Runner resource ownership acknowledgement is invalid"
            ) from exc
        if acknowledgement != {"token": token, "accepted": True}:
            raise RuntimeError("Runner rejected resource ownership evidence")


def _write_event(event: Mapping[str, object], *, drop_oversized: bool = False) -> None:
    fd_text = os.environ.get(PROGRESS_FD_ENV)
    if fd_text is None:
        return
    try:
        fd = int(fd_text)
    except ValueError:
        return
    encoded = _encode_event(event)
    if len(encoded) > MAX_PROGRESS_EVENT_BYTES:
        if drop_oversized:
            encoded = _truncate_event_error(event)
            if encoded is None:
                return
        else:
            raise ValueError("Runner event exceeds 4096 encoded bytes")
    with _write_lock:
        view = memoryview(encoded)
        while view:
            try:
                written = os.write(fd, view)
            except OSError:
                return
            view = view[written:]


def _encode_event(event: Mapping[str, object]) -> bytes:
    return (
        json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()


def _truncate_event_error(event: Mapping[str, object]) -> bytes | None:
    """Fit a display error exactly while preserving the event's structured fields."""
    error = event.get("error")
    if not isinstance(error, str):
        return None
    truncated = dict(event)
    truncated["error"] = _TRUNCATED_ERROR_SUFFIX
    if len(_encode_event(truncated)) > MAX_PROGRESS_EVENT_BYTES:
        return None

    low = 0
    high = len(error)
    while low < high:
        keep = (low + high + 1) // 2
        truncated["error"] = error[:keep] + _TRUNCATED_ERROR_SUFFIX
        if len(_encode_event(truncated)) <= MAX_PROGRESS_EVENT_BYTES:
            low = keep
        else:
            high = keep - 1
    truncated["error"] = error[:low] + _TRUNCATED_ERROR_SUFFIX
    return _encode_event(truncated)


def _validate_number(name: str, value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError(f"{name} must be an integer or None")
    if value is not None and value < 1:
        raise ValueError(f"{name} must be positive")
