"""Observe supplied Environment Setup commands in the prepared worktree."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait()


def _run(command: str, cwd: str, remaining: Callable[[], float]) -> dict[str, object]:
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            ["/bin/sh", "-c", command],
            cwd=cwd,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            process.wait(timeout=remaining())
            remaining()
        except subprocess.TimeoutExpired as exc:
            _stop_group(process)
            raise TimeoutError("Environment Setup timed out") from exc
        except BaseException:
            _stop_group(process)
            raise
        output.seek(0)
        observed = output.read(4096).decode("utf-8", errors="replace")
    return {"command": command, "exit_code": process.returncode, "output": observed}


def execute_environment_setup_commands(
    *,
    build: str | None,
    start: str | None,
    ready_check: str | None,
    verification_command: str | None,
    cwd: str,
    remaining: Callable[[], float],
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    """Run declared commands in order and return observed outcomes."""
    checks: dict[str, dict[str, object]] = {}
    started: subprocess.Popen[bytes] | None = None
    try:
        if build is not None:
            checks["build"] = _run(build, cwd, remaining)
            if checks["build"]["exit_code"] != 0:
                raise RuntimeError(f"Environment Setup build failed: {checks['build']}")
        if start is not None:
            remaining()
            started = subprocess.Popen(
                ["/bin/sh", "-c", start],
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(min(0.05, remaining()))
            remaining()
            exit_code = started.poll()
            checks["start"] = {
                "command": start,
                "pid": started.pid,
                "exit_code": exit_code,
                "running": exit_code is None,
            }
            if exit_code is not None and exit_code != 0:
                raise RuntimeError(f"Environment Setup start failed: {checks['start']}")
        command = ready_check if ready_check is not None else verification_command
        if not isinstance(command, str) or not command.strip():
            raise RuntimeError("Environment Setup needs a usability check command")
        verification = _run(command, cwd, remaining)
        if ready_check is not None:
            checks["ready_check"] = verification
        if verification["exit_code"] != 0:
            raise RuntimeError(
                f"Environment Setup usability check failed: {verification}"
            )
        if started is not None and started.poll() not in (None, 0):
            raise RuntimeError("Environment Setup start command exited unsuccessfully")
        remaining()
        return checks, verification
    except BaseException:
        if started is not None:
            _stop_group(started)
        raise
