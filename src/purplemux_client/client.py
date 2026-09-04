from __future__ import annotations

import json
import os
import secrets
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from purplemux_client.errors import (
    MutationOutcomeUnknown,
    ResultNotReady,
    SessionReadyTimeout,
    TerminalSessionError,
    WorkerFailure,
    WorkerInterrupted,
    WorkerNeedsInput,
)
from purplemux_client.operations import (
    MutationConflict,
    MutationResolution,
    PossibleDispatchFailure,
    PreDispatchFailure,
    Reconciliation,
    execute_mutation,
)
from purplemux_client.progress import register_run_resource


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
    name: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class CreateWorkspaceRequest:
    cwd: str
    name: str
    correlation_id: str


@dataclass(frozen=True)
class WorkspaceState:
    id: str
    name: str
    directories: tuple[str, ...]


@dataclass(frozen=True)
class TabState:
    id: str
    workspace_id: str
    name: str
    panel_type: str | None
    provider: str | None
    alive: bool | None = None
    cli_state: str | None = None


@dataclass(frozen=True)
class AgentReadinessProbeResult:
    workspace_id: str
    tab_id: str
    provider: str
    probe_name: str
    correlation_id: str
    ready: bool
    cleanup_confirmed: bool


class AgentReadinessCleanupUnknown(MutationOutcomeUnknown):
    """Cleanup could not be confirmed after an identified readiness probe."""

    def __init__(
        self,
        message: str,
        *,
        tab: TabState,
        readiness_error: BaseException | None,
    ) -> None:
        super().__init__(message)
        self.tab = tab
        self.readiness_error = readiness_error


@dataclass(frozen=True)
class ShellCommandRequest:
    """Describe one observable Bash command in a named PurpleMux terminal."""

    command: str
    cwd: str
    name: str


@dataclass(frozen=True)
class ShellResult:
    """Structured completion plus display-only failure diagnostics."""

    exit_code: int
    diagnostic_output: str | None = None
    diagnostic_error: str | None = None
    cwd: str | None = None
    workspace_id: str | None = None
    tab_id: str | None = None

    def failure_message(self, step_name: str) -> str:
        """Format a failed step for display without deriving its outcome from text."""
        lines = [f"{step_name} failed (exit code {self.exit_code})"]
        if self.cwd:
            lines.append(f"cwd: {self.cwd}")
        if self.workspace_id and self.tab_id:
            lines.append(f"workspace/tab: {self.workspace_id} / {self.tab_id}")
        if self.diagnostic_output:
            lines.append(self.diagnostic_output)
        if self.diagnostic_error:
            lines.append(f"diagnostic capture failed: {self.diagnostic_error}")
        return "\n".join(lines)


@dataclass(frozen=True)
class _ShellRun:
    result_path: str
    cwd: str | None


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
_SHELL_DIAGNOSTIC_MAX_LINES = 40
_SHELL_DIAGNOSTIC_MAX_BYTES = 2_500


@dataclass(frozen=True)
class _TurnBaseline:
    completion_timestamp: int | float | None
    event_seq: int | None
    ready_for_review_at: int | float | None
    interrupted: bool


class PurpleMuxRuntime:
    """Inspection-aware adapter for public workspace-level PurpleMux operations."""

    def __init__(
        self,
        *,
        executable: str = "purplemux",
        command_timeout_seconds: float = 30.0,
        read_timeout_retries: int = 1,
        runner: SubprocessRunner = subprocess.run,
    ) -> None:
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        if read_timeout_retries < 0:
            raise ValueError("read_timeout_retries must not be negative")
        self.executable = executable
        self.command_timeout_seconds = command_timeout_seconds
        self.read_timeout_retries = read_timeout_retries
        self._runner = runner

    def list_workspaces(self) -> tuple[WorkspaceState, ...]:
        data = self._read_json(["workspaces"], "list workspaces")
        values = data.get("workspaces")
        if not isinstance(values, list):
            raise WorkerFailure("PurpleMux workspace listing is incomplete")
        if len(values) > 2_000:
            raise WorkerFailure(
                "PurpleMux workspace listing exceeds the authoritative cap"
            )
        workspaces: list[WorkspaceState] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, Mapping):
                raise WorkerFailure("PurpleMux workspace listing is malformed")
            workspace_id = value.get("id")
            name = value.get("name")
            directories = value.get("directories")
            if (
                not isinstance(workspace_id, str)
                or not workspace_id
                or workspace_id in seen
                or not isinstance(name, str)
                or not isinstance(directories, list)
                or any(not isinstance(item, str) for item in directories)
            ):
                raise WorkerFailure("PurpleMux workspace listing is malformed")
            seen.add(workspace_id)
            workspaces.append(WorkspaceState(workspace_id, name, tuple(directories)))
        return tuple(workspaces)

    def create_workspace(self, request: CreateWorkspaceRequest) -> WorkspaceState:
        cwd = os.path.abspath(os.path.expanduser(request.cwd))
        if not os.path.isdir(cwd):
            raise ValueError(f"workspace directory is not a directory: {cwd}")
        _validate_correlation(request.correlation_id)
        if not request.name.strip() or "\0" in request.name:
            raise ValueError("workspace name must be non-empty and contain no nulls")
        correlated_name = f"{request.name} [awm:{request.correlation_id}]"
        before = self.list_workspaces()
        before_ids = {item.id for item in before}
        if any(item.name == correlated_name for item in before):
            raise WorkerFailure("workspace creation correlation is already in use")
        response_id: str | None = None

        def matches() -> tuple[WorkspaceState, ...]:
            return tuple(
                item
                for item in self.list_workspaces()
                if item.id not in before_ids
                and item.name == correlated_name
                and cwd in {os.path.abspath(path) for path in item.directories}
            )

        def dispatch() -> WorkspaceState:
            nonlocal response_id
            data = self._mutation_json(
                ["workspace", "create", "--cwd", cwd, "--name", correlated_name],
                "create workspace",
            )
            candidate = data.get("id") or data.get("workspaceId")
            if isinstance(candidate, str) and candidate:
                response_id = candidate
            try:
                found = matches()
            except WorkerFailure as exc:
                raise PossibleDispatchFailure(
                    "workspace was dispatched but its postcondition could not be read"
                ) from exc
            if len(found) == 1 and (response_id is None or found[0].id == response_id):
                return found[0]
            raise PossibleDispatchFailure(
                "workspace create response could not be authoritatively correlated"
            )

        def reconcile(quiescent: bool) -> Reconciliation[WorkspaceState]:
            found = matches()
            if len(found) == 1 and (response_id is None or found[0].id == response_id):
                return Reconciliation(MutationResolution.DESIRED, found[0])
            if len(found) > 1 or (found and response_id not in {None, found[0].id}):
                return Reconciliation(
                    MutationResolution.CONFLICT, detail="ambiguous workspace identity"
                )
            if quiescent:
                return Reconciliation(
                    MutationResolution.REJECTED, detail="workspace absent"
                )
            return Reconciliation(
                MutationResolution.UNKNOWN, detail="workspace may appear later"
            )

        workspace = execute_mutation(
            operation="create PurpleMux workspace",
            target=correlated_name,
            pre_state=before,
            dispatch=dispatch,
            reconcile=reconcile,
            plan={"kind": "create_workspace", "cwd": cwd, "name": correlated_name},
        )
        register_run_resource(
            "purplemux_workspace",
            workspace.id,
            {
                "name": workspace.name,
                "directories": "\n".join(workspace.directories),
                "correlation_id": request.correlation_id,
            },
        )
        return workspace

    def workspace(self, workspace_id: str) -> PurpleMuxCLIClient:
        if workspace_id not in {item.id for item in self.list_workspaces()}:
            raise WorkerFailure(f"PurpleMux workspace {workspace_id!r} was not found")
        return PurpleMuxCLIClient(
            workspace_id,
            executable=self.executable,
            command_timeout_seconds=self.command_timeout_seconds,
            read_timeout_retries=self.read_timeout_retries,
            runner=self._runner,
        )

    def _read_json(self, args: Sequence[str], operation: str) -> dict[str, Any]:
        attempts = self.read_timeout_retries + 1
        for attempt in range(attempts):
            try:
                completed = self._runner(
                    [self.executable, *args],
                    capture_output=True,
                    text=True,
                    timeout=self.command_timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                if attempt + 1 < attempts:
                    continue
                raise WorkerFailure(f"PurpleMux {operation} timed out") from exc
            except OSError as exc:
                raise WorkerFailure(
                    f"could not execute PurpleMux {operation}: {exc}"
                ) from exc
            if completed.returncode != 0:
                raise WorkerFailure(
                    f"PurpleMux {operation} failed: {completed.stderr.strip() or 'no stderr'}"
                )
            return _parse_json_object(completed.stdout, operation)
        raise AssertionError("unreachable")

    def _mutation_json(self, args: Sequence[str], operation: str) -> dict[str, Any]:
        return _run_mutation_json(
            self._runner, self.executable, args, operation, self.command_timeout_seconds
        )


def _parse_json_object(output: str, operation: str) -> dict[str, Any]:
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError) as exc:
        raise WorkerFailure(f"PurpleMux {operation} returned malformed JSON") from exc
    if not isinstance(data, dict):
        raise WorkerFailure(f"PurpleMux {operation} returned non-object JSON")
    return cast(dict[str, Any], data)


def _run_mutation_json(
    runner: SubprocessRunner,
    executable: str,
    args: Sequence[str],
    operation: str,
    timeout: float,
) -> dict[str, Any]:
    try:
        completed = runner(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PossibleDispatchFailure(f"PurpleMux {operation} timed out") from exc
    except InterruptedError as exc:
        raise PossibleDispatchFailure(
            f"PurpleMux {operation} communication was interrupted"
        ) from exc
    except OSError as exc:
        raise PreDispatchFailure(
            f"could not execute PurpleMux {operation}: {exc}"
        ) from exc
    except KeyboardInterrupt as exc:
        raise PossibleDispatchFailure(f"PurpleMux {operation} was interrupted") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise PossibleDispatchFailure(
            f"PurpleMux {operation} failed with exit code {completed.returncode}: {detail}"
        )
    try:
        return _parse_json_object(completed.stdout, operation)
    except WorkerFailure as exc:
        raise PossibleDispatchFailure(str(exc)) from exc


def _validate_correlation(value: str) -> None:
    if (
        not value
        or len(value) > 64
        or not value.isascii()
        or any(not (character.isalnum() or character in "-_") for character in value)
    ):
        raise ValueError(
            "correlation ID must be 1-64 ASCII letters, digits, hyphens, or underscores"
        )


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
        self._shell_runs: dict[str, _ShellRun] = {}
        self._completed_shell_runs: dict[str, ShellResult] = {}

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
        correlation_id = request.correlation_id or secrets.token_hex(8)
        _validate_correlation(correlation_id)
        name = request.name or f"awm-{panel_type}-{correlation_id}"
        if request.name is not None and correlation_id not in name:
            name = f"{name} [awm:{correlation_id}]"
        tab = self._create_correlated_tab(
            panel_type=panel_type,
            provider="codex" if panel_type == "codex-cli" else "claude",
            name=name,
        )
        self._register_owned_tab(tab)
        return tab.id

    def list_sessions(self) -> tuple[TabState, ...]:
        """Return one complete structured tab listing for this workspace."""
        data = self._run_json(
            ["tab", "list", "-w", self.workspace_id],
            operation="list tabs",
            read_only=True,
        )
        values = data.get("tabs")
        if not isinstance(values, list):
            raise WorkerFailure("PurpleMux tab listing is incomplete")
        if len(values) > 2_000:
            raise WorkerFailure("PurpleMux tab listing exceeds the authoritative cap")
        tabs: list[TabState] = []
        seen: set[str] = set()
        for value in values:
            tab = self._parse_tab(value)
            if tab.workspace_id != self.workspace_id:
                raise WorkerFailure("PurpleMux tab listing crossed workspace identity")
            if tab.id in seen:
                raise WorkerFailure("PurpleMux tab listing contains duplicate IDs")
            seen.add(tab.id)
            tabs.append(tab)
        return tuple(tabs)

    def probe_agent_readiness(
        self,
        *,
        provider: str,
        probe_name: str,
        correlation_id: str,
        preexisting_tab_ids: Sequence[str],
        timeout_seconds: float,
        on_identified: Callable[[TabState], None] | None = None,
    ) -> AgentReadinessProbeResult:
        """Create, identify, inspect, and clean up one explicit provider probe."""
        panel_type = _PANEL_TYPES.get(provider.lower())
        if panel_type is None:
            raise ValueError("probe provider must be codex or claude-code")
        _validate_correlation(correlation_id)
        if correlation_id not in probe_name or not probe_name.strip():
            raise ValueError("probe name must contain its correlation ID")
        current = self.list_sessions()
        expected_ids = tuple(preexisting_tab_ids)
        if sum(len(item) + 1 for item in expected_ids) > 3_000:
            raise WorkerFailure(
                "probe preexisting tab set cannot fit its recovery record"
            )
        if len(set(expected_ids)) != len(expected_ids) or {
            tab.id for tab in current
        } != set(expected_ids):
            raise WorkerFailure(
                "probe preexisting tab set is not authoritative/current"
            )
        if any(tab.name == probe_name for tab in current):
            raise WorkerFailure("probe correlation identity is already in use")
        tab = self._create_correlated_tab(
            panel_type=panel_type,
            provider="codex" if panel_type == "codex-cli" else "claude",
            name=probe_name,
            before=current,
        )
        readiness_error: BaseException | None = None
        try:
            if on_identified is not None:
                on_identified(tab)
            self._wait_until_ready_structured(tab.id, timeout_seconds)
        except BaseException as exc:
            readiness_error = exc
        try:
            self.close_session(tab.id, expected_state=tab)
        except BaseException as exc:
            raise AgentReadinessCleanupUnknown(
                f"probe tab {tab.id} retained after cleanup uncertainty: {exc}",
                tab=tab,
                readiness_error=readiness_error,
            ) from exc
        if readiness_error is not None:
            raise readiness_error
        return AgentReadinessProbeResult(
            self.workspace_id,
            tab.id,
            provider,
            probe_name,
            correlation_id,
            True,
            True,
        )

    def start_shell(
        self,
        request: ShellCommandRequest,
        *,
        on_created: Callable[[str, str], None] | None = None,
    ) -> str:
        """Start one Bash command in a visible, named PurpleMux terminal."""
        if not request.command:
            raise ValueError("shell command must not be empty")
        if not request.name.strip():
            raise ValueError("shell terminal name must not be empty")
        if "\0" in request.command or "\0" in request.name or "\0" in request.cwd:
            raise ValueError("shell request values must not contain null bytes")
        cwd = os.path.abspath(os.path.expanduser(request.cwd))
        if not os.path.isdir(cwd):
            raise ValueError(f"shell working directory is not a directory: {cwd}")

        tab = self._create_correlated_tab(
            panel_type="terminal", provider=None, name=request.name
        )
        self._register_owned_tab(tab)
        session_id = tab.id

        result_dir = tempfile.mkdtemp(prefix="awm-shell-")
        result_path = os.path.join(result_dir, "result.json")
        self._shell_runs[session_id] = _ShellRun(result_path=result_path, cwd=cwd)
        if on_created is not None:
            on_created(session_id, result_path)
        wrapper = self._shell_wrapper(request.command, cwd, result_path)
        try:
            self._send_mutation(session_id, wrapper, operation="start shell command")
        except MutationOutcomeUnknown as exc:
            # Keep both the tab and correlation data: a timed-out send may have
            # started the command, and the terminal remains useful for inspection.
            raise MutationOutcomeUnknown(
                f"shell terminal {session_id} was created; {exc}"
            ) from exc
        except WorkerFailure as exc:
            raise WorkerFailure(
                f"shell terminal {session_id} was created but command start failed: {exc}"
            ) from exc
        return session_id

    @staticmethod
    def _register_owned_tab(tab: TabState) -> None:
        register_run_resource(
            "purplemux_tab",
            tab.id,
            {
                "workspace_id": tab.workspace_id,
                "name": tab.name,
                "panel_type": tab.panel_type or "",
                "provider": tab.provider or "",
            },
        )

    def resume_shell(
        self, session_id: str, result_path: str, *, cwd: str | None = None
    ) -> None:
        """Reattach a checkpointed managed shell without sending its command again."""
        resolved_cwd = (
            os.path.abspath(os.path.expanduser(cwd)) if cwd is not None else None
        )
        if resolved_cwd is not None and not os.path.isdir(resolved_cwd):
            raise ValueError(
                f"shell working directory is not a directory: {resolved_cwd}"
            )
        if session_id in self._shell_runs:
            if self._shell_runs[session_id].result_path != result_path:
                raise WorkerFailure(f"session {session_id} shell identity conflicts")
            if (
                resolved_cwd is not None
                and self._shell_runs[session_id].cwd != resolved_cwd
            ):
                raise WorkerFailure(f"session {session_id} shell cwd conflicts")
            return
        normalized = os.path.abspath(result_path)
        parent = os.path.dirname(normalized)
        if os.path.basename(normalized) != "result.json" or not os.path.basename(
            parent
        ).startswith("awm-shell-"):
            raise WorkerFailure("checkpointed shell result path is invalid")
        tabs = self.list_sessions()
        tab = next((item for item in tabs if item.id == session_id), None)
        if tab is None and not os.path.isfile(normalized):
            raise MutationOutcomeUnknown(
                f"checkpointed shell {session_id} and its result are not observable"
            )
        if tab is not None and tab.panel_type != "terminal":
            raise MutationConflict(
                f"checkpointed shell {session_id} is no longer a terminal"
            )
        self._shell_runs[session_id] = _ShellRun(
            result_path=normalized, cwd=resolved_cwd
        )

    def wait_for_shell_completion(
        self, session_id: str, timeout_seconds: float
    ) -> None:
        """Wait for a machine-readable shell result, never terminal screen text."""
        if session_id not in self._shell_runs:
            raise WorkerFailure(f"session {session_id} has no managed shell command")
        deadline = self._monotonic() + timeout_seconds
        last_status = "unknown"
        while True:
            result = self._read_shell_result_file(session_id)
            if result is not None:
                self._completed_shell_runs[session_id] = self._with_shell_diagnostic(
                    session_id, result
                )
                return
            status = self._status(session_id)
            panel_type = status.get("panelType")
            if panel_type != "terminal":
                raise WorkerFailure(f"session {session_id} is not a PurpleMux terminal")
            terminal_status = status.get("terminalStatus")
            if isinstance(terminal_status, str) and terminal_status:
                last_status = terminal_status
            else:
                cli_state = status.get("cliState")
                last_status = (
                    f"unavailable; cliState={cli_state}"
                    if isinstance(cli_state, str) and cli_state
                    else "unavailable"
                )
            if status.get("alive") is False:
                raise WorkerFailure(
                    f"shell terminal {session_id} exited before publishing a result"
                )
            if self._monotonic() >= deadline:
                raise WorkerFailure(
                    f"shell terminal {session_id} did not complete within "
                    f"{timeout_seconds}s (last terminalStatus={last_status})"
                )
            self._sleep(self.poll_interval_seconds)

    def read_shell_result(self, session_id: str) -> ShellResult:
        """Return the structured exit code for a completed managed shell command."""
        result = self._completed_shell_runs.get(session_id)
        if result is None:
            result = self._read_shell_result_file(session_id)
        if result is None:
            raise ResultNotReady(f"shell terminal {session_id} result is not ready")
        completed = self._with_shell_diagnostic(session_id, result)
        self._completed_shell_runs[session_id] = completed
        return completed

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
                diagnostic = self._startup_diagnostic(session_id)
                raise SessionReadyTimeout(
                    f"session {session_id} was not ready within {timeout_seconds}s "
                    f"(last cliState={state}){diagnostic}"
                )
            self._sleep(self.poll_interval_seconds)

    def _startup_diagnostic(self, session_id: str) -> str:
        """Capture pane text for timeout diagnosis without treating it as state."""
        try:
            content = self.capture_screen(session_id).strip()
        except WorkerFailure as exc:
            return f"; PurpleMux capture failed: {exc}"
        if not content:
            return "; PurpleMux pane capture was empty"
        return f"; PurpleMux pane capture (diagnostic only):\n{content}"

    def send_input(self, session_id: str, text: str) -> None:
        """Submit one prompt after recording a correlation baseline."""
        if not text:
            raise ValueError("text must not be empty")
        baseline = self._read_turn_baseline(session_id)
        self._send_mutation(session_id, text, operation="send")
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
        before = self._status(session_id)

        def dispatched() -> None:
            self._mutation_json(
                ["tab", "interrupt", "-w", self.workspace_id, session_id],
                "interrupt",
            )

        def desired() -> bool:
            current = self._status(session_id)
            event = current.get("lastEvent")
            return (
                isinstance(event, Mapping)
                and str(event.get("name", "")).lower() == "interrupt"
                and event.get("seq") != self._event_seq(before)
            )

        self._execute_runtime_mutation(
            operation="interrupt PurpleMux tab",
            target=f"{self.workspace_id}/{session_id}",
            pre_state=before,
            dispatch=dispatched,
            desired=desired,
            unchanged=lambda: self._status(session_id) == before,
            success_is_authoritative=True,
            plan={
                "kind": "interrupt_tab",
                "workspace": self.workspace_id,
                "tab": session_id,
            },
        )

    def close_session(
        self, session_id: str, *, expected_state: TabState | None = None
    ) -> None:
        """Close the tab and discard local correlation state."""
        before = self.list_sessions()
        selected = next((tab for tab in before if tab.id == session_id), None)
        if (
            expected_state is not None
            and selected is not None
            and self._tab_identity(selected) != self._tab_identity(expected_state)
        ):
            raise MutationConflict(
                f"tab {session_id} identity changed before close; refusing cleanup"
            )
        if selected is not None:
            selected_identity = self._tab_identity(selected)
            self._execute_runtime_mutation(
                operation="close PurpleMux tab",
                target=f"{self.workspace_id}/{session_id}",
                pre_state=selected,
                dispatch=lambda: self._mutation_json(
                    ["tab", "close", "-w", self.workspace_id, session_id], "close"
                ),
                desired=lambda: all(
                    tab.id != session_id for tab in self.list_sessions()
                ),
                unchanged=lambda: any(
                    self._tab_identity(tab) == selected_identity
                    for tab in self.list_sessions()
                ),
                success_is_authoritative=False,
                plan={
                    "kind": "close_tab",
                    "workspace": self.workspace_id,
                    "tab": session_id,
                },
            )
        self._turn_baselines.pop(session_id, None)
        self._completed_turns.pop(session_id, None)
        shell_run = self._shell_runs.pop(session_id, None)
        self._completed_shell_runs.pop(session_id, None)
        if shell_run is not None:
            self._cleanup_shell_result(shell_run)

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

    def _with_shell_diagnostic(
        self, session_id: str, result: ShellResult
    ) -> ShellResult:
        """Attach bounded pane text to failures without using it as control state."""
        if result.exit_code == 0 or result.tab_id is not None:
            return result
        output: str | None = None
        error: str | None = None
        try:
            output = self._bounded_shell_diagnostic(self.capture_screen(session_id))
        except TerminalSessionError as exc:
            error = str(exc)
        shell_run = self._shell_runs[session_id]
        return ShellResult(
            exit_code=result.exit_code,
            diagnostic_output=output,
            diagnostic_error=error,
            cwd=shell_run.cwd,
            workspace_id=self.workspace_id,
            tab_id=session_id,
        )

    @staticmethod
    def _bounded_shell_diagnostic(content: str) -> str | None:
        lines = content.strip().splitlines()[-_SHELL_DIAGNOSTIC_MAX_LINES:]
        tail = "\n".join(lines)
        encoded = tail.encode()
        if len(encoded) > _SHELL_DIAGNOSTIC_MAX_BYTES:
            tail = encoded[-_SHELL_DIAGNOSTIC_MAX_BYTES:].decode(errors="ignore")
        return tail or None

    @staticmethod
    def _shell_wrapper(command: str, cwd: str, result_path: str) -> str:
        command_text = shlex.quote(command)
        cwd_text = shlex.quote(cwd)
        result_text = shlex.quote(result_path)
        pending_result_text = shlex.quote(f"{result_path}.pending")
        return (
            f"__awm_exit=0; (cd -- {cwd_text} && bash -lc {command_text}) "
            f"|| __awm_exit=$?; printf '{{\"exitCode\":%s}}\\n' "
            f'"$__awm_exit" > {pending_result_text} && '
            f"mv -- {pending_result_text} {result_text}"
        )

    def _read_shell_result_file(self, session_id: str) -> ShellResult | None:
        shell_run = self._shell_runs.get(session_id)
        if shell_run is None:
            raise WorkerFailure(f"session {session_id} has no managed shell command")
        try:
            with open(shell_run.result_path, encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerFailure(
                f"shell terminal {session_id} published an invalid result"
            ) from exc
        exit_code = data.get("exitCode") if isinstance(data, dict) else None
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise WorkerFailure(
                f"shell terminal {session_id} published an invalid exit code"
            )
        return ShellResult(exit_code=exit_code)

    @staticmethod
    def _cleanup_shell_result(shell_run: _ShellRun) -> None:
        for path in (shell_run.result_path, f"{shell_run.result_path}.pending"):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(os.path.dirname(shell_run.result_path))
        except FileNotFoundError:
            pass

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

    def _create_correlated_tab(
        self,
        *,
        panel_type: str,
        provider: str | None,
        name: str,
        before: tuple[TabState, ...] | None = None,
    ) -> TabState:
        if not name.strip() or "\0" in name or len(name) > 200:
            raise ValueError("tab name must be 1-200 characters without nulls")
        captured = self.list_sessions() if before is None else before
        before_ids = {tab.id for tab in captured}
        if any(tab.name == name for tab in captured):
            raise WorkerFailure(f"tab correlation name {name!r} is already in use")
        response_id: str | None = None

        def matches() -> tuple[TabState, ...]:
            return tuple(
                tab
                for tab in self.list_sessions()
                if tab.id not in before_ids
                and tab.name == name
                and tab.panel_type == panel_type
                and (provider is None or tab.provider == provider)
            )

        def dispatch() -> TabState:
            nonlocal response_id
            data = self._mutation_json(
                [
                    "tab",
                    "create",
                    "-w",
                    self.workspace_id,
                    "-n",
                    name,
                    "-t",
                    panel_type,
                ],
                "create tab",
            )
            candidate = data.get("tabId") or data.get("tab_id") or data.get("id")
            if isinstance(candidate, str) and candidate:
                response_id = candidate
            try:
                found = matches()
            except WorkerFailure as exc:
                raise PossibleDispatchFailure(
                    "tab was dispatched but its postcondition could not be read"
                ) from exc
            if len(found) == 1 and response_id == found[0].id:
                return found[0]
            raise PossibleDispatchFailure(
                "tab create response could not be authoritatively correlated"
            )

        def reconcile(quiescent: bool) -> Reconciliation[TabState]:
            found = matches()
            if len(found) == 1 and (response_id is None or response_id == found[0].id):
                return Reconciliation(MutationResolution.DESIRED, found[0])
            if len(found) > 1 or (found and response_id not in {None, found[0].id}):
                return Reconciliation(
                    MutationResolution.CONFLICT,
                    detail="multiple or response-mismatched correlated tabs",
                )
            if quiescent:
                return Reconciliation(MutationResolution.REJECTED, detail="tab absent")
            return Reconciliation(
                MutationResolution.UNKNOWN, detail="tab may appear later"
            )

        return execute_mutation(
            operation="create PurpleMux tab",
            target=f"{self.workspace_id}/{name}",
            pre_state=captured,
            dispatch=dispatch,
            reconcile=reconcile,
            plan={
                "kind": "create_tab",
                "workspace": self.workspace_id,
                "name": name,
                "panelType": panel_type,
            },
        )

    @staticmethod
    def _parse_tab(value: object) -> TabState:
        if not isinstance(value, Mapping):
            raise WorkerFailure("PurpleMux tab listing is malformed")
        tab_id = value.get("tabId") or value.get("id")
        workspace_id = value.get("workspaceId")
        name = value.get("name", "")
        panel_type = value.get("panelType")
        provider = value.get("agentProviderId")
        alive = value.get("alive")
        cli_state = value.get("cliState")
        if (
            not isinstance(tab_id, str)
            or not tab_id
            or not isinstance(workspace_id, str)
            or not workspace_id
            or not isinstance(name, str)
            or panel_type is not None
            and not isinstance(panel_type, str)
            or provider is not None
            and not isinstance(provider, str)
            or alive is not None
            and not isinstance(alive, bool)
            or cli_state is not None
            and not isinstance(cli_state, str)
        ):
            raise WorkerFailure("PurpleMux tab listing is malformed")
        return TabState(
            tab_id, workspace_id, name, panel_type, provider, alive, cli_state
        )

    @staticmethod
    def _tab_identity(tab: TabState) -> tuple[str, str, str, str | None, str | None]:
        return (tab.id, tab.workspace_id, tab.name, tab.panel_type, tab.provider)

    def _send_mutation(self, session_id: str, text: str, *, operation: str) -> None:
        self._execute_runtime_mutation(
            operation=operation,
            target=f"{self.workspace_id}/{session_id}",
            pre_state={"workspace": self.workspace_id, "tab": session_id},
            dispatch=lambda: self._mutation_json(
                ["tab", "send", "-w", self.workspace_id, session_id, text], operation
            ),
            desired=lambda: False,
            unchanged=lambda: True,
            success_is_authoritative=True,
            plan={
                "kind": "send_tab",
                "workspace": self.workspace_id,
                "tab": session_id,
            },
        )

    def _execute_runtime_mutation(
        self,
        *,
        operation: str,
        target: str,
        pre_state: object,
        dispatch: Callable[[], object],
        desired: Callable[[], bool],
        unchanged: Callable[[], bool],
        success_is_authoritative: bool,
        plan: Mapping[str, object],
    ) -> None:
        def perform() -> None:
            dispatch()
            if success_is_authoritative:
                return
            try:
                postcondition_met = desired()
            except WorkerFailure as exc:
                raise PossibleDispatchFailure(
                    "mutation was dispatched but its postcondition could not be read"
                ) from exc
            if not postcondition_met:
                raise PossibleDispatchFailure(
                    "successful response lacked its postcondition"
                )

        def reconcile(quiescent: bool) -> Reconciliation[None]:
            if desired():
                return Reconciliation(MutationResolution.DESIRED)
            if quiescent and unchanged():
                return Reconciliation(MutationResolution.REJECTED)
            if quiescent:
                return Reconciliation(MutationResolution.CONFLICT)
            return Reconciliation(MutationResolution.UNKNOWN)

        execute_mutation(
            operation=operation,
            target=target,
            pre_state=pre_state,
            dispatch=perform,
            reconcile=reconcile,
            plan=plan,
        )

    @staticmethod
    def _event_seq(status: Mapping[str, Any]) -> int | None:
        value = status.get("eventSeq")
        if isinstance(value, int):
            return value
        event = status.get("lastEvent")
        value = event.get("seq") if isinstance(event, Mapping) else None
        return value if isinstance(value, int) else None

    def _wait_until_ready_structured(
        self, session_id: str, timeout_seconds: float
    ) -> None:
        deadline = self._monotonic() + timeout_seconds
        while True:
            status = self._status(session_id)
            state = self._state(status, session_id)
            self._raise_abnormal_state(session_id, state, status)
            if state in _READY_STATES:
                return
            if self._monotonic() >= deadline:
                raise SessionReadyTimeout(
                    f"probe session {session_id} was not ready within {timeout_seconds}s "
                    f"(last cliState={state})"
                )
            self._sleep(self.poll_interval_seconds)

    def _mutation_json(self, args: Sequence[str], operation: str) -> dict[str, Any]:
        return _run_mutation_json(
            self._runner, self.executable, args, operation, self.command_timeout_seconds
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
            message = f"PurpleMux {operation} returned malformed JSON"
            if not read_only:
                message += "; remote outcome is unknown"
                raise MutationOutcomeUnknown(message) from exc
            raise WorkerFailure(message) from exc
        if not isinstance(data, dict):
            message = f"PurpleMux {operation} returned non-object JSON"
            if not read_only:
                message += "; remote outcome is unknown"
                raise MutationOutcomeUnknown(message)
            raise WorkerFailure(message)
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
