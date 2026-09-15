"""Control local and registered external child Runs from a running Python Workflow."""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from urllib import error, request

CONTROL_URL_ENV = "AGENT_WORKFLOW_MANAGER_CONTROL_URL"
CONTROL_TOKEN_ENV = "AGENT_WORKFLOW_MANAGER_CONTROL_TOKEN"


@dataclass(frozen=True)
class ChildRunResult:
    run_id: int
    state: str
    exit_code: int | None
    stdout: str
    stderr: str


def _control(operation: str, *, request_timeout: float = 35, **payload: object) -> dict:
    url = os.environ.get(CONTROL_URL_ENV)
    token = os.environ.get(CONTROL_TOKEN_ENV)
    if not url or not token:
        raise RuntimeError("child Runs require a running Workflow")
    message = request.Request(
        url,
        data=json.dumps({"operation": operation, **payload}).encode(),
        headers={"Content-Type": "application/json", "X-AWM-Run-Token": token},
        method="POST",
    )
    try:
        with request.urlopen(message, timeout=request_timeout) as response:
            return json.load(response)
    except error.HTTPError as exc:
        value = json.load(exc)
        from purplemux_client.external_runs import (
            ExternalRunError,
            ExternalRunLaunchUnknown,
        )

        failures = {
            "ExternalRunError": ExternalRunError,
            "ExternalRunLaunchUnknown": ExternalRunLaunchUnknown,
            "TimeoutError": TimeoutError,
        }
        if value.get("error_type") in failures:
            raise failures[value["error_type"]](value["error"]) from None
        raise


def start_child_run(
    code: str, *, args: Sequence[str] = (), target_id: str | None = None
) -> int:
    """Start a distinct local or registered external Run, persisting its parent before execution.

    Do not retry an uncertain start request: inspect Runner history first.
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")
    if isinstance(args, str) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("args must be a sequence of strings")
    return _control("start", code=code, args=list(args), target_id=target_id)["run_id"]


def get_child_run_result(
    run_id: int, *, target_id: str | None = None, _timeout: float | None = None
) -> ChildRunResult | None:
    """Return the Runner's final result, or None while the child is running."""
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise ValueError("run_id must be a positive integer")
    result = _control(
        "result",
        run_id=run_id,
        target_id=target_id,
        timeout=_timeout,
        request_timeout=35 if _timeout is None else _timeout,
    )
    return ChildRunResult(**result) if result else None


def wait_child_run(
    run_id: int, *, timeout: float | None = None, target_id: str | None = None
) -> ChildRunResult:
    """Wait in Python; failed and stopped children return ordinary final results."""
    if timeout is not None and (
        isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 0
    ):
        raise ValueError("timeout must be non-negative")
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError(f"child Run {run_id} is still unknown")
        result = get_child_run_result(
            run_id,
            target_id=target_id,
            _timeout=remaining,
        )
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"child Run {run_id} is still unknown")
        if result is not None:
            return result
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"child Run {run_id} is still running")
        time.sleep(
            0.05 if deadline is None else max(0, min(0.05, deadline - time.monotonic()))
        )
