from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Literal

TerminalState = Literal["success", "failed", "suspended", "stopped"]


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
        notify_success: bool = True,
        notify_failure: bool = True,
        notify_stopped: bool = False,
        timeout: float = 20.0,
        executable: str = "notify",
        config_path: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("notification timeout must be positive")
        self.enabled = enabled
        self.notify_success = notify_success
        self.notify_failure = notify_failure
        self.notify_stopped = notify_stopped
        self.timeout = timeout
        self.executable = executable
        self.config_path = config_path
        self._transport_overrides: dict[str, str] = {}
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._closed = False

    @classmethod
    def from_environment(cls) -> NotifyCLI:
        return cls(
            enabled=_environment_flag("AGENT_WORKFLOW_MANAGER_NOTIFICATIONS", False),
            notify_success=_environment_flag(
                "AGENT_WORKFLOW_MANAGER_NOTIFY_SUCCESS", True
            ),
            notify_failure=_environment_flag(
                "AGENT_WORKFLOW_MANAGER_NOTIFY_FAILURE", True
            ),
            notify_stopped=_environment_flag(
                "AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED", False
            ),
            config_path=os.environ.get("NOTIFY_CONFIG"),
        )

    def policy(self) -> tuple[bool, bool, bool, bool]:
        with self._lock:
            return (
                self.enabled,
                self.notify_success,
                self.notify_failure,
                self.notify_stopped,
            )

    def configure_policy(
        self,
        *,
        enabled: bool,
        notify_success: bool,
        notify_failure: bool,
        notify_stopped: bool,
    ) -> None:
        with self._lock:
            self.enabled = enabled
            self.notify_success = notify_success
            self.notify_failure = notify_failure
            self.notify_stopped = notify_stopped

    def configure_transport(
        self, *, server: str, topic: str, replacement_token: str | None
    ) -> None:
        with self._lock:
            self._transport_overrides["NOTIFY_SERVER"] = server
            self._transport_overrides["NOTIFY_TOPIC"] = topic
            if replacement_token is not None:
                self._transport_overrides["NOTIFY_TOKEN"] = replacement_token

    def notify_terminal(
        self, *, run_id: int, state: TerminalState, exit_code: int | None
    ) -> NotificationResult:
        enabled, notify_success, notify_failure, notify_stopped = self.policy()
        if not enabled:
            return NotificationResult(False, False, "notifications disabled")
        if state == "success" and not notify_success:
            return NotificationResult(False, False, "success notifications disabled")
        if state == "failed" and not notify_failure:
            return NotificationResult(False, False, "failure notifications disabled")
        if state == "stopped" and not notify_stopped:
            return NotificationResult(False, False, "stopped notifications disabled")

        title, message = _terminal_message(run_id, state, exit_code)
        return self._send(title=title, message=message)

    def send_test(self) -> NotificationResult:
        return self._send(title="Agent Workflow Manager", message="Test notification")

    def _send(self, *, title: str, message: str) -> NotificationResult:
        executable = shutil.which(self.executable)
        if executable is None:
            return NotificationResult(True, False, "notify command unavailable")

        with self._lock:
            if self._closed:
                return NotificationResult(False, False, "notifier closed")
            environment = None
            if self.config_path is not None or self._transport_overrides:
                environment = os.environ.copy()
                if self.config_path is not None:
                    environment["NOTIFY_CONFIG"] = self.config_path
                environment.update(self._transport_overrides)
            try:
                process = subprocess.Popen(
                    [executable, "send", "--title", title, "--message", message],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=environment,
                )
            except OSError:
                return NotificationResult(True, False, "notify command could not start")
            self._processes.add(process)
        try:
            try:
                return_code = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self._kill_process_group(process)
                process.wait()
                return NotificationResult(True, False, "notify command timed out")
        finally:
            with self._lock:
                self._processes.discard(process)
        if return_code != 0:
            return NotificationResult(
                True, False, f"notify exited with status {return_code}"
            )
        return NotificationResult(True, True, "notification sent")

    def close(self) -> None:
        """Stop and reap notification process groups still active at shutdown."""
        with self._lock:
            self._closed = True
            processes = tuple(self._processes)
        for process in processes:
            self._kill_process_group(process)
        for process in processes:
            process.wait()

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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
    if state == "suspended":
        return (
            "Workflow needs attention",
            f"Run {run_id} is suspended and waiting for manual input or recovery",
        )
    return "Workflow stopped", f"Run {run_id} finished with state: stopped"


def _environment_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
