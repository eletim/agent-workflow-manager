from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from typing import Literal

TerminalState = Literal["success", "failed", "stopped"]


@dataclass(frozen=True)
class NotificationResult:
    attempted: bool
    delivered: bool
    diagnostic: str


class NotifyCLI:
    """Best-effort terminal notifications through the public ``notify`` CLI."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        notify_stopped: bool = False,
        timeout: float = 20.0,
        executable: str = "notify",
    ) -> None:
        if timeout <= 0:
            raise ValueError("notification timeout must be positive")
        self.enabled = enabled
        self.notify_stopped = notify_stopped
        self.timeout = timeout
        self.executable = executable

    @classmethod
    def from_environment(cls) -> NotifyCLI:
        return cls(
            enabled=_environment_flag("AGENT_WORKFLOW_MANAGER_NOTIFICATIONS", False),
            notify_stopped=_environment_flag(
                "AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED", False
            ),
        )

    def notify_terminal(
        self, *, run_id: int, state: TerminalState, exit_code: int | None
    ) -> NotificationResult:
        if not self.enabled:
            return NotificationResult(False, False, "notifications disabled")
        if state == "stopped" and not self.notify_stopped:
            return NotificationResult(False, False, "stopped notifications disabled")

        executable = shutil.which(self.executable)
        if executable is None:
            return NotificationResult(True, False, "notify command unavailable")

        title, message = _terminal_message(run_id, state, exit_code)
        try:
            process = subprocess.Popen(
                [executable, "send", "--title", title, "--message", message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return NotificationResult(True, False, "notify command could not start")
        try:
            return_code = process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return NotificationResult(True, False, "notify command timed out")
        if return_code != 0:
            return NotificationResult(
                True, False, f"notify exited with status {return_code}"
            )
        return NotificationResult(True, True, "notification sent")


def _terminal_message(
    run_id: int, state: TerminalState, exit_code: int | None
) -> tuple[str, str]:
    if state == "success":
        return "Workflow completed", f"Run {run_id} finished with state: success"
    if state == "failed":
        exit_detail = f" and exit code {exit_code}" if exit_code is not None else ""
        return (
            "Workflow failed",
            f"Run {run_id} finished with state: failed{exit_detail}",
        )
    return "Workflow stopped", f"Run {run_id} finished with state: stopped"


def _environment_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
