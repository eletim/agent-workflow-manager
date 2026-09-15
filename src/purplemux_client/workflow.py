"""Control local child Runs from a running Python Workflow."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from urllib import request

CONTROL_URL_ENV = "AGENT_WORKFLOW_MANAGER_CONTROL_URL"
CONTROL_TOKEN_ENV = "AGENT_WORKFLOW_MANAGER_CONTROL_TOKEN"


@dataclass(frozen=True)
class ChildRunResult:
    run_id: int
    state: str
    exit_code: int | None
    stdout: str
    stderr: str


def _control(operation: str, **payload: object) -> dict:
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
    with request.urlopen(message, timeout=30) as response:
        return json.load(response)


def start_child_run(code: str, *, args: Sequence[str] = ()) -> int:
    """Start a distinct local Run, persisting its parent before execution.

    Do not retry an uncertain start request: inspect Runner history first.
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")
    if isinstance(args, str) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("args must be a sequence of strings")
    return _control("start", code=code, args=list(args))["run_id"]


def get_child_run_result(run_id: int) -> ChildRunResult | None:
    """Return the Runner's final result, or None while the child is running."""
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise ValueError("run_id must be a positive integer")
    result = _control("result", run_id=run_id)
    return ChildRunResult(**result) if result else None


def wait_child_run(run_id: int, *, timeout: float | None = None) -> ChildRunResult:
    """Wait in Python; failed and stopped children return ordinary final results."""
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative")
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        result = get_child_run_result(run_id)
        if result is not None:
            return result
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"child Run {run_id} is still running")
        time.sleep(0.05)
