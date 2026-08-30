from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast


class TerminalSessionError(RuntimeError):
    """Base error for PurpleMux terminal session operations."""


class SessionReadyTimeout(TerminalSessionError):
    """Raised when a PurpleMux session does not become ready in time."""


class WorkerFailure(TerminalSessionError):
    """Raised when a PurpleMux-backed worker or CLI operation fails."""


class WorkerNeedsInput(WorkerFailure):
    """Raised when a worker cannot complete without additional input."""


class WorkerInterrupted(WorkerFailure):
    """Raised when a worker turn is interrupted."""


class ResultNotReady(WorkerFailure):
    """Raised when a fresh structured worker result is not ready."""


class MutationOutcomeUnknown(WorkerFailure):
    """Raised when a timed-out mutation may have been applied remotely."""


@dataclass(frozen=True)
class CreateSessionRequest:
    """Describe the provider session to create in a PurpleMux workspace.

    PurpleMux owns provider launch commands and the workspace directory. `worker`
    selects the provider; `cwd`, `command`, and `metadata` describe caller intent and
    are retained for generated-workflow APIs.
    """

    worker: str
    cwd: str
    command: str
    metadata: Mapping[str, str] = field(default_factory=dict)


class SubprocessRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


_PANEL_TYPES = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex-cli",
    "codex-cli": "codex-cli",
}
_READY_STATES = {"idle", "ready-for-review"}
_FAILED_STATES = {"cancelled", "dead", "error", "failed", "stopped", "exited"}
_RESULT_STATUSES = {
    "completed",
    "not-ready",
    "interrupted",
    "not-applicable",
    "unavailable",
}


@dataclass(frozen=True)
class _TurnBaseline:
    completion_timestamp: int | float | None
    event_seq: int | None
    ready_for_review_at: int | float | None
    interrupted: bool


class PurpleMuxCLIClient:
    """Thin Python adapter over the public PurpleMux CLI."""

    def __init__(
        self,
        workspace_id: str,
        *,
        executable: str = "purplemux",
        poll_interval_seconds: float = 1.0,
        command_timeout_seconds: float = 30.0,
        read_timeout_retries: int = 1,
        runner: SubprocessRunner = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must not be negative")
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        if read_timeout_retries < 0:
            raise ValueError("read_timeout_retries must not be negative")
        self.workspace_id = workspace_id
        self.executable = executable
        self.poll_interval_seconds = poll_interval_seconds
        self.command_timeout_seconds = command_timeout_seconds
        self.read_timeout_retries = read_timeout_retries
        self._runner = runner
        self._sleep = sleep
        self._monotonic = monotonic
        self._turn_baselines: dict[str, _TurnBaseline] = {}
        self._completed_turns: dict[str, dict[str, Any]] = {}

    def create_session(self, request: CreateSessionRequest) -> str:
        """Create and launch a Codex or Claude session."""
        panel_type = _PANEL_TYPES.get(request.worker.lower())
        if panel_type is None:
            panel_type = _PANEL_TYPES.get(request.command.lower())
        if panel_type is None:
            raise WorkerFailure(
                f"unsupported PurpleMux worker {request.worker!r}; "
                "expected codex or claude-code"
            )
        data = self._run_json(
            ["tab", "create", "-w", self.workspace_id, "-t", panel_type],
            operation="create",
            read_only=False,
        )
        session_id = data.get("tabId") or data.get("tab_id") or data.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise WorkerFailure("PurpleMux create did not return a tab ID")
        return session_id

    def read_status(self, session_id: str) -> dict[str, Any]:
        """Read authoritative agent state from PurpleMux StatusManager output."""
        return self._status(session_id)

    def wait_until_ready(self, session_id: str, timeout_seconds: float) -> None:
        """Wait until the agent can accept input."""
        deadline = self._monotonic() + timeout_seconds
        while True:
            status = self._status(session_id)
            state = self._state(status, session_id)
            self._raise_abnormal_state(session_id, state, status)
            if state in _READY_STATES:
                return
            if self._monotonic() >= deadline:
                raise SessionReadyTimeout(
                    f"session {session_id} was not ready within {timeout_seconds}s "
                    f"(last cliState={state})"
                )
            self._sleep(self.poll_interval_seconds)

    def send_input(self, session_id: str, text: str) -> None:
        """Submit one prompt after recording a correlation baseline."""
        if not text:
            raise ValueError("text must not be empty")
        baseline = self._read_turn_baseline(session_id)
        self._run_json(
            ["tab", "send", "-w", self.workspace_id, session_id, text],
            operation="send",
            read_only=False,
        )
        self._turn_baselines[session_id] = baseline
        self._completed_turns.pop(session_id, None)

    def wait_for_turn_completion(self, session_id: str, timeout_seconds: float) -> None:
        """Wait for a fresh completed turn and its structured result."""
        deadline = self._monotonic() + timeout_seconds
        baseline = self._turn_baselines.get(session_id)
        if baseline is None:
            raise WorkerFailure(f"session {session_id} has no pending input")
        saw_busy = False
        last_state = "unknown"
        while True:
            status = self._status(session_id)
            state = self._state(status, session_id)
            last_state = state
            self._raise_abnormal_state(session_id, state, status)
            if self._is_fresh_interrupt(status, baseline):
                raise WorkerInterrupted(f"session {session_id} turn was interrupted")
            if state == "busy":
                saw_busy = True
            elif state == "inactive":
                raise WorkerFailure(
                    f"session {session_id} agent became inactive during its turn"
                )
            elif state == "ready-for-review" or (
                state == "idle" and self._has_fresh_completion_event(status, baseline)
            ):
                # PurpleMux can return an acknowledged ready-for-review state to
                # idle while retaining its fresh stop event and structured result.
                result = self._result_data(session_id)
                if self._accept_fresh_result(session_id, result, baseline):
                    return
            elif state == "idle" and saw_busy:
                result = self._result_data(session_id)
                if self._result_is_interrupted(result):
                    raise WorkerInterrupted(
                        f"session {session_id} turn was interrupted"
                    )
            if self._monotonic() >= deadline:
                raise WorkerFailure(
                    f"session {session_id} did not complete a turn within "
                    f"{timeout_seconds}s (saw_busy={saw_busy}, "
                    f"last cliState={last_state})"
                )
            self._sleep(self.poll_interval_seconds)

    def read_result(self, session_id: str) -> str:
        """Read the latest structured result, rejecting stale pending-turn data."""
        data = self._completed_turns.pop(session_id, None)
        if data is None:
            data = self._result_data(session_id)
        status = self._result_status(data, session_id)
        reason = data.get("reason")
        detail = f": {reason}" if isinstance(reason, str) and reason else ""
        baseline = self._turn_baselines.get(session_id)
        if self._result_is_interrupted(data):
            if baseline is not None and baseline.interrupted:
                raise ResultNotReady(
                    f"session {session_id} interrupt result is stale for the "
                    "pending turn"
                )
            raise WorkerInterrupted(
                f"session {session_id} turn was interrupted{detail}"
            )
        if status == "completed":
            if baseline is not None and not self._is_fresh_result(data, baseline):
                raise ResultNotReady(
                    f"session {session_id} result is stale for the pending turn"
                )
            text = data.get("text")
            if not isinstance(text, str):
                raise WorkerFailure(
                    f"session {session_id} completed result has no text"
                )
            return text
        if status == "not-ready":
            raise ResultNotReady(f"session {session_id} result is not ready{detail}")
        raise WorkerFailure(f"session {session_id} result is {status}{detail}")

    def interrupt(self, session_id: str) -> None:
        """Request interruption of the foreground agent turn."""
        self._run_json(
            ["tab", "interrupt", "-w", self.workspace_id, session_id],
            operation="interrupt",
            read_only=False,
        )

    def close_session(self, session_id: str) -> None:
        """Close the tab and discard local correlation state."""
        self._run(
            ["tab", "close", "-w", self.workspace_id, session_id],
            operation="close",
            read_only=False,
        )
        self._turn_baselines.pop(session_id, None)
        self._completed_turns.pop(session_id, None)

    def capture_screen(self, session_id: str) -> str:
        """Capture diagnostic pane text; never use this as an agent result."""
        data = self._run_json(
            ["tab", "capture", "-w", self.workspace_id, session_id],
            operation="capture",
            read_only=True,
        )
        content = data.get("content")
        if not isinstance(content, str):
            raise WorkerFailure("PurpleMux capture did not return text content")
        return content

    def _accept_fresh_result(
        self,
        session_id: str,
        result: dict[str, Any],
        baseline: _TurnBaseline,
    ) -> bool:
        result_status = self._result_status(result, session_id)
        if self._result_is_interrupted(result):
            if baseline.interrupted:
                return False
            raise WorkerInterrupted(f"session {session_id} turn was interrupted")
        if result_status == "completed" and self._is_fresh_result(result, baseline):
            self._completed_turns[session_id] = result
            self._turn_baselines.pop(session_id, None)
            return True
        if result_status in {"not-applicable", "unavailable"}:
            reason = result.get("reason")
            raise WorkerFailure(
                f"session {session_id} result is {result_status}: {reason}"
            )
        return False

    @staticmethod
    def _result_is_interrupted(data: Mapping[str, Any]) -> bool:
        return data.get("status") == "interrupted" or data.get("interrupted") is True

    def _status(self, session_id: str) -> dict[str, Any]:
        return self._run_json(
            ["tab", "status", "-w", self.workspace_id, session_id],
            operation="status",
            read_only=True,
        )

    def _result_data(self, session_id: str) -> dict[str, Any]:
        return self._run_json(
            ["tab", "result", "-w", self.workspace_id, session_id],
            operation="result",
            read_only=True,
        )

    def _read_turn_baseline(self, session_id: str) -> _TurnBaseline:
        status_data = self._status(session_id)
        state = self._state(status_data, session_id)
        self._raise_abnormal_state(session_id, state, status_data)
        event_seq = status_data.get("eventSeq")
        if not isinstance(event_seq, int):
            last_event = status_data.get("lastEvent")
            last_event_seq = (
                last_event.get("seq") if isinstance(last_event, Mapping) else None
            )
            event_seq = last_event_seq if isinstance(last_event_seq, int) else None
        if event_seq is None:
            raise WorkerFailure(
                f"session {session_id} status has no event sequence for turn "
                "correlation"
            )
        ready_for_review_at = status_data.get("readyForReviewAt")
        if not isinstance(ready_for_review_at, int | float):
            ready_for_review_at = None
        data = self._result_data(session_id)
        status = self._result_status(data, session_id)
        if status == "completed":
            timestamp = data.get("completionTimestamp")
            if not isinstance(timestamp, int | float):
                raise WorkerFailure(
                    f"session {session_id} completed result has no completionTimestamp"
                )
            return _TurnBaseline(
                timestamp,
                event_seq,
                ready_for_review_at,
                self._result_is_interrupted(data),
            )
        if status in {"not-ready", "interrupted"}:
            return _TurnBaseline(
                None,
                event_seq,
                ready_for_review_at,
                self._result_is_interrupted(data),
            )
        reason = data.get("reason")
        raise WorkerFailure(
            f"session {session_id} cannot start a correlated turn: {status}: {reason}"
        )

    @staticmethod
    def _is_fresh_result(data: Mapping[str, Any], baseline: _TurnBaseline) -> bool:
        timestamp = data.get("completionTimestamp")
        if not isinstance(timestamp, int | float):
            raise WorkerFailure("PurpleMux completed result has no completionTimestamp")
        if baseline.completion_timestamp is None:
            return True
        return timestamp > baseline.completion_timestamp

    @staticmethod
    def _has_fresh_completion_event(
        data: Mapping[str, Any], baseline: _TurnBaseline
    ) -> bool:
        ready_for_review_at = data.get("readyForReviewAt")
        if isinstance(ready_for_review_at, int | float) and (
            baseline.ready_for_review_at is None
            or ready_for_review_at > baseline.ready_for_review_at
        ):
            return True
        last_event = data.get("lastEvent")
        if not isinstance(last_event, Mapping):
            return False
        if str(last_event.get("name", "")).lower() != "stop":
            return False
        event_seq = last_event.get("seq")
        return (
            isinstance(event_seq, int)
            and baseline.event_seq is not None
            and event_seq > baseline.event_seq
        )

    @staticmethod
    def _is_fresh_interrupt(data: Mapping[str, Any], baseline: _TurnBaseline) -> bool:
        last_event = data.get("lastEvent")
        if not isinstance(last_event, Mapping):
            return False
        if str(last_event.get("name", "")).lower() != "interrupt":
            return False
        event_seq = last_event.get("seq")
        return (
            isinstance(event_seq, int)
            and baseline.event_seq is not None
            and event_seq > baseline.event_seq
        )

    @staticmethod
    def _state(data: Mapping[str, Any], session_id: str) -> str:
        state = data.get("cliState")
        if not isinstance(state, str) or not state:
            raise WorkerFailure(f"PurpleMux status for {session_id} has no cliState")
        return state.lower()

    @staticmethod
    def _result_status(data: Mapping[str, Any], session_id: str) -> str:
        status = data.get("status")
        if not isinstance(status, str) or status not in _RESULT_STATUSES:
            raise WorkerFailure(
                f"PurpleMux result for {session_id} has invalid status {status!r}"
            )
        return status

    @staticmethod
    def _raise_abnormal_state(
        session_id: str, state: str, data: Mapping[str, Any]
    ) -> None:
        if state == "needs-input":
            raise WorkerNeedsInput(f"session {session_id} needs input")
        if state in _FAILED_STATES or data.get("alive") is False:
            raise WorkerFailure(f"session {session_id} entered {state}")

    def _run_json(
        self, args: Sequence[str], *, operation: str, read_only: bool
    ) -> dict[str, Any]:
        completed = self._run(args, operation=operation, read_only=read_only)
        try:
            data = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise WorkerFailure(
                f"PurpleMux {operation} returned malformed JSON"
            ) from exc
        if not isinstance(data, dict):
            raise WorkerFailure(f"PurpleMux {operation} returned non-object JSON")
        return cast(dict[str, Any], data)

    def _run(
        self, args: Sequence[str], *, operation: str, read_only: bool
    ) -> subprocess.CompletedProcess[str]:
        command = [self.executable, *args]
        attempts = self.read_timeout_retries + 1 if read_only else 1
        for attempt in range(attempts):
            try:
                completed = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.command_timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                if attempt + 1 < attempts:
                    continue
                if read_only:
                    raise WorkerFailure(
                        f"PurpleMux {operation} timed out after "
                        f"{self.command_timeout_seconds}s"
                    ) from exc
                raise MutationOutcomeUnknown(
                    f"PurpleMux {operation} timed out after "
                    f"{self.command_timeout_seconds}s; remote outcome is unknown"
                ) from exc
            except OSError as exc:
                raise WorkerFailure(
                    f"could not execute PurpleMux {operation}: {exc}"
                ) from exc
            if completed.returncode != 0:
                stderr = completed.stderr.strip() or "no stderr"
                raise WorkerFailure(
                    f"PurpleMux {operation} failed with exit code "
                    f"{completed.returncode}: {stderr}"
                )
            return completed
        raise AssertionError("unreachable")
