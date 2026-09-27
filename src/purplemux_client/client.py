from __future__ import annotations

import base64
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from purplemux_client.codex_trust import ensure_codex_project_trust
from purplemux_client.correlation import run_correlation
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
    AuthoritativeMutationRejection,
    MutationConflict,
    MutationResolution,
    PossibleDispatchFailure,
    PreDispatchFailure,
    Reconciliation,
    execute_mutation,
)
from purplemux_client.progress import emit_finding, register_run_resource


@dataclass(frozen=True)
class CreateSessionRequest:
    """Describe the provider session to create in a PurpleMux workspace.

    PurpleMux owns provider launch commands and the workspace directory. `worker`
    selects the provider; `cwd`, `command`, and `metadata` describe caller intent and
    are retained for generated-workflow APIs. A supplied `name` is also the logical
    resource name used for automatic run-scoped correlation. `restriction` selects
    an explicit reusable capability boundary for commit-producing agent turns.
    """

    worker: str
    cwd: str
    command: str
    metadata: Mapping[str, str] = field(default_factory=dict)
    name: str | None = None
    correlation_id: str | None = None
    deadline_check: Callable[[], float] | None = None
    restriction: Literal["local-git-only", "publication-disabled"] | None = None


@dataclass(frozen=True)
class CreateWorkspaceRequest:
    """Describe a workspace whose display name is its logical correlation name."""

    cwd: str
    name: str
    correlation_id: str | None = None
    deadline_check: Callable[[], float] | None = None


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
class WorkspaceState:
    id: str
    name: str
    directories: tuple[str, ...]
    # Present only on the result of a successful create. Workspace listings do
    # not claim provenance for tabs that may have been created independently.
    initial_tab: TabState | None = field(default=None, compare=False)
    initial_tab_discovery_pending: bool = field(default=False, compare=False)


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
    """Describe one observable command with a logical/display terminal name."""

    command: str
    cwd: str
    name: str
    correlation_id: str | None = None
    deadline_check: Callable[[], float] | None = None
    max_output_chars: int = 1_000_000


@dataclass(frozen=True)
class ShellResult:
    """Structured completion, captured streams, and display-only diagnostics."""

    exit_code: int
    diagnostic_output: str | None = None
    diagnostic_error: str | None = None
    cwd: str | None = None
    workspace_id: str | None = None
    tab_id: str | None = None
    stdout: str = ""
    stderr: str = ""

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


@dataclass
class _RestrictedSession:
    worker: str
    cwd: str
    restriction: Literal["local-git-only", "publication-disabled"]
    initial_prompt: str | None = None


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
_TURN_RESULT_PUBLICATION_GRACE_SECONDS = 30.0
WORKFLOW_HOST_WORKSPACE_ENV = "AGENT_WORKFLOW_MANAGER_HOST_WORKSPACE_ID"


def _filesystem_identity(path: str) -> str:
    state = os.stat(path, follow_symlinks=False)
    return f"{state.st_dev}:{state.st_ino}"


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
        owned_by_run: bool = False,
        workspace_deleter: Callable[[str], bool | None] | None = None,
    ) -> None:
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        if read_timeout_retries < 0:
            raise ValueError("read_timeout_retries must not be negative")
        self.executable = executable
        self.command_timeout_seconds = command_timeout_seconds
        self.read_timeout_retries = read_timeout_retries
        self._runner = runner
        self.owned_by_run = owned_by_run
        self._workspace_deleter = workspace_deleter or self._delete_empty_workspace

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
        if request.deadline_check is not None:
            request.deadline_check()
        cwd = os.path.abspath(os.path.expanduser(request.cwd))
        if not os.path.isdir(cwd):
            raise ValueError(f"workspace directory is not a directory: {cwd}")
        if not request.name.strip() or "\0" in request.name:
            raise ValueError("workspace name must be non-empty and contain no nulls")
        host_workspace_id = os.environ.get(WORKFLOW_HOST_WORKSPACE_ENV)
        if host_workspace_id:
            host_workspace = next(
                (
                    workspace
                    for workspace in self.list_workspaces()
                    if workspace.id == host_workspace_id
                ),
                None,
            )
            if host_workspace is None:
                raise WorkerFailure(
                    "the run-owned Workflow host workspace no longer exists"
                )
            if cwd in {
                os.path.abspath(directory) for directory in host_workspace.directories
            }:
                return host_workspace
        correlation_id = request.correlation_id or run_correlation(request.name)
        _validate_correlation(correlation_id)
        correlated_name = f"{request.name} [awm:{correlation_id}]"
        before = self.list_workspaces()
        before_ids = {item.id for item in before}
        if any(item.name == correlated_name for item in before):
            raise WorkerFailure("workspace creation correlation is already in use")
        response_id: str | None = None
        response_initial_tab: TabState | None = None

        def matches() -> tuple[WorkspaceState, ...]:
            return tuple(
                item
                for item in self.list_workspaces()
                if item.id not in before_ids
                and item.name == correlated_name
                and cwd in {os.path.abspath(path) for path in item.directories}
            )

        def dispatch() -> WorkspaceState:
            nonlocal response_id, response_initial_tab
            timeout_seconds = (
                min(self.command_timeout_seconds, request.deadline_check())
                if request.deadline_check is not None
                else self.command_timeout_seconds
            )
            data = self._mutation_json(
                ["workspace", "create", "--cwd", cwd, "--name", correlated_name],
                "create workspace",
                timeout_seconds=timeout_seconds,
            )
            candidate = data.get("id") or data.get("workspaceId")
            if isinstance(candidate, str) and candidate:
                response_id = candidate
                try:
                    parsed_initial_tab = PurpleMuxCLIClient._parse_tab(
                        data.get("initialTab")
                    )
                except WorkerFailure:
                    pass
                else:
                    if parsed_initial_tab.workspace_id == response_id:
                        response_initial_tab = parsed_initial_tab
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
        initial_tab = response_initial_tab
        # A later listing cannot prove which tab was created with the workspace.
        # Preserve an unresolved cleanup checkpoint whenever the authoritative
        # mutation response was unavailable or omitted that identity.
        initial_tab_discovery_pending = initial_tab is None
        workspace = WorkspaceState(
            workspace.id,
            workspace.name,
            workspace.directories,
            initial_tab,
            initial_tab_discovery_pending,
        )
        if self.owned_by_run:
            workspace_metadata = {
                "name": workspace.name,
                "directories": "\n".join(workspace.directories),
                "correlation_id": correlation_id,
            }
            if initial_tab_discovery_pending:
                workspace_metadata["initial_tab_discovery"] = "pending"
            elif initial_tab is not None:
                workspace_metadata.update(
                    {
                        "initial_tab_id": initial_tab.id,
                        "initial_tab_name": initial_tab.name,
                        "initial_tab_panel_type": initial_tab.panel_type or "",
                        "initial_tab_provider": initial_tab.provider or "",
                    }
                )
            register_run_resource(
                "purplemux_workspace",
                workspace.id,
                workspace_metadata,
            )
        return workspace

    def delete_workspace(
        self, workspace_id: str, *, expected_state: WorkspaceState
    ) -> None:
        """Delete one empty, identity-verified workspace with reconciliation."""
        before = self.list_workspaces()
        selected = next((item for item in before if item.id == workspace_id), None)
        if selected is None:
            return
        if selected != expected_state:
            raise MutationConflict(
                f"workspace {workspace_id} identity changed before cleanup"
            )

        def desired() -> bool:
            return all(item.id != workspace_id for item in self.list_workspaces())

        def dispatch() -> None:
            authoritative_absence = self._workspace_deleter(workspace_id)
            if authoritative_absence is True:
                return
            try:
                deleted = desired()
            except WorkerFailure as exc:
                raise PossibleDispatchFailure(
                    "workspace deletion was dispatched but could not be observed"
                ) from exc
            if not deleted:
                raise PossibleDispatchFailure(
                    "workspace deletion response lacked its postcondition"
                )

        def reconcile(quiescent: bool) -> Reconciliation[None]:
            current = self.list_workspaces()
            if all(item.id != workspace_id for item in current):
                return Reconciliation(MutationResolution.DESIRED)
            unchanged = any(item == selected for item in current)
            if quiescent and unchanged:
                return Reconciliation(MutationResolution.REJECTED)
            if quiescent:
                return Reconciliation(MutationResolution.CONFLICT)
            return Reconciliation(MutationResolution.UNKNOWN)

        execute_mutation(
            operation="delete PurpleMux workspace",
            target=workspace_id,
            pre_state=selected,
            dispatch=dispatch,
            reconcile=reconcile,
            plan={"kind": "delete_workspace", "workspace": workspace_id},
        )

    def _delete_empty_workspace(self, workspace_id: str) -> bool:
        """Use PurpleMux's public, atomic empty-workspace deletion contract."""
        try:
            completed = self._runner(
                [
                    self.executable,
                    "workspace",
                    "delete",
                    "-w",
                    workspace_id,
                    "--if-empty",
                ],
                capture_output=True,
                text=True,
                timeout=self.command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PossibleDispatchFailure(
                "PurpleMux empty workspace deletion timed out"
            ) from exc
        except InterruptedError as exc:
            raise PossibleDispatchFailure(
                "PurpleMux empty workspace deletion was interrupted"
            ) from exc
        except OSError as exc:
            raise PreDispatchFailure(
                f"could not execute PurpleMux empty workspace deletion: {exc}"
            ) from exc
        except KeyboardInterrupt as exc:
            raise PossibleDispatchFailure(
                "PurpleMux empty workspace deletion was interrupted"
            ) from exc
        try:
            response = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            response = None
        if isinstance(response, dict) and response.get("workspaceId") == workspace_id:
            status = response.get("status")
            if status in ("deleted", "already-absent"):
                return True
            if status == "not-empty":
                raise AuthoritativeMutationRejection(
                    "PurpleMux atomically refused non-empty workspace deletion"
                )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
            raise PossibleDispatchFailure(
                "PurpleMux empty workspace deletion failed with exit code "
                f"{completed.returncode}: {detail}"
            )
        raise PossibleDispatchFailure(
            "PurpleMux empty workspace deletion returned an invalid response"
        )

    def workspace(self, workspace_id: str) -> PurpleMuxCLIClient:
        workspace = next(
            (item for item in self.list_workspaces() if item.id == workspace_id), None
        )
        if workspace is None:
            raise WorkerFailure(f"PurpleMux workspace {workspace_id!r} was not found")
        return PurpleMuxCLIClient(
            workspace_id,
            executable=self.executable,
            command_timeout_seconds=self.command_timeout_seconds,
            read_timeout_retries=self.read_timeout_retries,
            runner=self._runner,
            owned_by_run=self.owned_by_run,
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

    def _mutation_json(
        self,
        args: Sequence[str],
        operation: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return _run_mutation_json(
            self._runner,
            self.executable,
            args,
            operation,
            self.command_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds,
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
        owned_by_run: bool = False,
        codex_project_truster: Callable[[str], str] = ensure_codex_project_trust,
        claude_project_truster: Callable[[str], str] | None = None,
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
        self.owned_by_run = owned_by_run
        self._codex_project_truster = codex_project_truster
        self._claude_project_truster = claude_project_truster
        self._turn_baselines: dict[str, _TurnBaseline] = {}
        self._completed_turns: dict[str, dict[str, Any]] = {}
        self._shell_runs: dict[str, _ShellRun] = {}
        self._completed_shell_runs: dict[str, ShellResult] = {}
        self._restricted_sessions: dict[str, _RestrictedSession] = {}

    @staticmethod
    def correlated_session_name(name: str, correlation_id: str) -> str:
        """Return the exact public display name for a correlated Agent tab."""
        _validate_correlation(correlation_id)
        if not name.strip() or "\0" in name:
            raise ValueError("tab name must be non-empty and contain no nulls")
        if correlation_id in name:
            return name
        return f"{name} [awm:{correlation_id}]"

    def create_session(self, request: CreateSessionRequest) -> str:
        """Create and launch a Codex or Claude session."""
        if request.deadline_check is not None:
            request.deadline_check()
        if request.restriction not in (
            None,
            "local-git-only",
            "publication-disabled",
        ):
            raise ValueError("unsupported agent session restriction")
        panel_type = _PANEL_TYPES.get(request.worker.lower())
        if panel_type is None:
            panel_type = _PANEL_TYPES.get(request.command.lower())
        if panel_type is None:
            raise WorkerFailure(
                f"unsupported PurpleMux worker {request.worker!r}; "
                "expected codex or claude-code"
            )
        provider_name = "Codex" if panel_type == "codex-cli" else "Claude"
        launch_directory = self._current_workspace_launch_directory(provider_name)
        try:
            requested = os.path.realpath(os.path.expanduser(request.cwd))
        except (OSError, ValueError) as exc:
            raise WorkerFailure(
                f"{provider_name} project trust path could not be resolved: {exc}"
            ) from exc
        if requested != launch_directory:
            raise WorkerFailure(
                f"{provider_name} request cwd does not match the current PurpleMux "
                "workspace launch directory"
            )
        if panel_type == "codex-cli":
            self._codex_project_truster(launch_directory)
        else:
            self._ensure_claude_project_trust(launch_directory)
        correlation_id = request.correlation_id or (
            run_correlation(request.name)
            if request.name is not None
            else secrets.token_hex(8)
        )
        _validate_correlation(correlation_id)
        name = request.name or f"awm-{panel_type}-{correlation_id}"
        if request.name is not None:
            name = self.correlated_session_name(name, correlation_id)
        restricted = request.restriction is not None
        tab = self._create_correlated_tab(
            panel_type="terminal" if restricted else panel_type,
            provider=None
            if restricted
            else ("codex" if panel_type == "codex-cli" else "claude"),
            name=name,
            deadline_check=request.deadline_check,
            bound_reads=restricted,
        )
        if self.owned_by_run:
            self._register_owned_tab(tab)
        if restricted:
            self._restricted_sessions[tab.id] = _RestrictedSession(
                "codex" if panel_type == "codex-cli" else "claude",
                launch_directory,
                request.restriction,
            )
        return tab.id

    def list_sessions(
        self, *, deadline_check: Callable[[], float] | None = None
    ) -> tuple[TabState, ...]:
        """Return one complete structured tab listing for this workspace."""
        data = self._run_json(
            ["tab", "list", "-w", self.workspace_id],
            operation="list tabs",
            read_only=True,
            deadline_check=deadline_check,
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
        if panel_type == "codex-cli":
            self._codex_project_truster(
                self._current_workspace_launch_directory("Codex")
            )
        else:
            self._ensure_claude_project_trust(
                self._current_workspace_launch_directory("Claude")
            )
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

    def _current_workspace_launch_directory(self, provider_name: str) -> str:
        runtime = PurpleMuxRuntime(
            executable=self.executable,
            command_timeout_seconds=self.command_timeout_seconds,
            read_timeout_retries=self.read_timeout_retries,
            runner=self._runner,
        )
        selected = next(
            (
                workspace
                for workspace in runtime.list_workspaces()
                if workspace.id == self.workspace_id
            ),
            None,
        )
        if selected is None:
            raise WorkerFailure(
                f"PurpleMux workspace {self.workspace_id!r} was not found "
                f"before {provider_name} project trust"
            )
        if not selected.directories:
            raise WorkerFailure(
                "selected PurpleMux workspace has no directory for "
                f"{provider_name} project trust"
            )
        directory = selected.directories[0]
        if not directory or "\0" in directory:
            raise WorkerFailure(
                "selected PurpleMux workspace has an invalid "
                f"{provider_name} launch directory"
            )
        return os.path.realpath(os.path.expanduser(directory))

    def _ensure_claude_project_trust(self, launch_directory: str) -> None:
        if self._claude_project_truster is not None:
            self._claude_project_truster(launch_directory)
            return

        correlation_id = f"claude-trust-{secrets.token_hex(6)}"
        command = shlex.join(
            [
                sys.executable,
                "-I",
                "-m",
                "purplemux_client.claude_trust",
                launch_directory,
            ]
        )
        created: list[str] = []
        try:
            session_id = self.start_shell(
                ShellCommandRequest(
                    command=command,
                    cwd=launch_directory,
                    name=f"Claude project trust [awm:{correlation_id}]",
                    correlation_id=correlation_id,
                ),
                on_created=lambda tab_id, _result_path: created.append(tab_id),
            )
            self.wait_for_shell_completion(
                session_id, timeout_seconds=self.command_timeout_seconds
            )
            result = self.read_shell_result(session_id)
            if result.exit_code != 0:
                raise WorkerFailure(result.failure_message("Claude project trust"))
        except BaseException as trust_error:
            if created:
                try:
                    self.close_session(created[0])
                except BaseException as cleanup_error:
                    raise WorkerFailure(
                        f"{trust_error}; transient trust terminal cleanup failed: "
                        f"{cleanup_error}"
                    ) from cleanup_error
            raise
        self.close_session(session_id)

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
        if request.max_output_chars < 1:
            raise ValueError("max_output_chars must be positive")
        cwd = os.path.abspath(os.path.expanduser(request.cwd))
        if not os.path.isdir(cwd):
            raise ValueError(f"shell working directory is not a directory: {cwd}")

        correlation_id = request.correlation_id or run_correlation(request.name)
        _validate_correlation(correlation_id)
        name = request.name
        if correlation_id not in name:
            name = f"{name} [awm:{correlation_id}]"

        tab = self._create_correlated_tab(
            panel_type="terminal",
            provider=None,
            name=name,
            deadline_check=request.deadline_check,
            bound_reads=request.deadline_check is not None,
        )
        if self.owned_by_run:
            self._register_owned_tab(tab)
        session_id = tab.id

        self._start_shell_run(
            session_id,
            request,
            cwd,
            on_created=on_created,
        )
        return session_id

    def _start_shell_run(
        self,
        session_id: str,
        request: ShellCommandRequest,
        cwd: str,
        *,
        on_created: Callable[[str, str], None] | None = None,
    ) -> None:
        prior = self._shell_runs.pop(session_id, None)
        self._completed_shell_runs.pop(session_id, None)
        if prior is not None:
            self._cleanup_shell_result(prior)

        result_dir = tempfile.mkdtemp(prefix="awm-shell-")
        result_path = os.path.join(result_dir, "result.json")
        if self.owned_by_run:
            register_run_resource(
                "managed_shell_result",
                result_dir,
                {
                    "result_path": result_path,
                    "tab_id": session_id,
                    "directory_identity": _filesystem_identity(result_dir),
                },
            )
        self._shell_runs[session_id] = _ShellRun(result_path=result_path, cwd=cwd)
        if on_created is not None:
            on_created(session_id, result_path)
        wrapper = self._shell_wrapper(
            request.command, cwd, result_path, request.max_output_chars
        )
        try:
            self._send_mutation(
                session_id,
                wrapper,
                operation="start shell command",
                deadline_check=request.deadline_check,
            )
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
        """Return the structured result for a completed managed shell command."""
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
        if session_id in self._restricted_sessions:
            status = self._status(session_id)
            if status.get("panelType") != "terminal" or status.get("alive") is False:
                raise WorkerFailure(
                    f"restricted session {session_id} terminal is unavailable"
                )
            return
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
        restricted = self._restricted_sessions.get(session_id)
        if restricted is not None:
            prompt = text
            if restricted.initial_prompt is None:
                restricted.initial_prompt = text
            else:
                prompt = f"{restricted.initial_prompt}\n\nFollow-up instruction:\n{text}"
            self._start_shell_run(
                session_id,
                ShellCommandRequest(
                    (
                        self._restricted_agent_command(restricted.worker, prompt)
                        if restricted.restriction == "local-git-only"
                        else self._publication_disabled_agent_command(
                            restricted.worker, prompt
                        )
                    ),
                    restricted.cwd,
                    "Restricted agent turn",
                ),
                restricted.cwd,
            )
            return
        baseline = self._read_turn_baseline(session_id)
        self._send_mutation(session_id, text, operation="send")
        self._turn_baselines[session_id] = baseline
        self._completed_turns.pop(session_id, None)

    def wait_for_turn_completion(
        self,
        session_id: str,
        timeout_seconds: float,
        *,
        on_busy_timeout: Callable[[str], None] | None = None,
    ) -> None:
        """Wait for a fresh completed turn and its structured result.

        The timeout is a warning threshold while the authoritative session state
        remains busy. Once crossed in that state, monitoring continues while it
        stays busy. A subsequent non-busy state gets a bounded grace period to
        publish a fresh result; returning to busy cancels that grace period.
        """
        if session_id in self._restricted_sessions:
            self.wait_for_shell_completion(session_id, timeout_seconds)
            return
        deadline = self._monotonic() + timeout_seconds
        baseline = self._turn_baselines.get(session_id)
        if baseline is None:
            raise WorkerFailure(f"session {session_id} has no pending input")
        saw_busy = False
        last_state = "unknown"
        busy_timeout_reported = False
        result_publication_deadline: float | None = None
        while True:
            status = self._status(session_id)
            state = self._state(status, session_id)
            last_state = state
            self._raise_abnormal_state(session_id, state, status)
            if self._is_fresh_interrupt(status, baseline):
                raise WorkerInterrupted(f"session {session_id} turn was interrupted")
            if busy_timeout_reported:
                if state == "busy":
                    result_publication_deadline = None
                elif result_publication_deadline is None:
                    result_publication_deadline = (
                        self._monotonic() + _TURN_RESULT_PUBLICATION_GRACE_SECONDS
                    )
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
            if busy_timeout_reported:
                if (
                    result_publication_deadline is not None
                    and self._monotonic() >= result_publication_deadline
                ):
                    raise WorkerFailure(
                        f"session {session_id} did not publish a fresh result within "
                        f"{_TURN_RESULT_PUBLICATION_GRACE_SECONDS:g}s after leaving "
                        f"busy (last cliState={state})"
                    )
                self._sleep(self.poll_interval_seconds)
                continue
            if not busy_timeout_reported and self._monotonic() >= deadline:
                if state == "busy":
                    warning = (
                        f"session {session_id} exceeded the agent turn timeout of "
                        f"{timeout_seconds}s while still busy; continuing to monitor "
                        "while the session remains busy"
                    )
                    if on_busy_timeout is None:
                        emit_finding("runtime", warning, status="warning")
                    else:
                        on_busy_timeout(warning)
                    busy_timeout_reported = True
                    self._sleep(self.poll_interval_seconds)
                    continue
                raise WorkerFailure(
                    f"session {session_id} did not complete a turn within "
                    f"{timeout_seconds}s (saw_busy={saw_busy}, "
                    f"last cliState={last_state})"
                )
            self._sleep(self.poll_interval_seconds)

    def read_result(self, session_id: str) -> str:
        """Read the latest structured result, rejecting stale pending-turn data."""
        if session_id in self._restricted_sessions:
            result = self.read_shell_result(session_id)
            if result.exit_code != 0:
                raise WorkerFailure(result.failure_message("restricted agent turn"))
            output = result.stdout.strip()
            if not output:
                raise WorkerFailure("restricted agent returned an empty result")
            return output
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
        self._restricted_sessions.pop(session_id, None)
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

    @staticmethod
    def _restricted_agent_command(worker: str, prompt: str) -> str:
        """Launch a Recovery agent with tightly bounded local Git capability."""
        encoded = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
        reference_hook = base64.b64encode(
            b"""#!/bin/sh
phase=$1
[ "$phase" = prepared ] || exit 0
zero=0000000000000000000000000000000000000000
while read old new ref; do
    if [ "$ref" = ORIG_HEAD ]; then
        protected=$(git rev-parse "$AWM_RECOVERY_PROTECTED_REF") || exit 1
        [ "$new" = "$protected" ] || exit 1
        continue
    fi
    [ "$ref" = HEAD ] || [ "$ref" = "$AWM_RECOVERY_PROTECTED_REF" ] || exit 1
    [ "$old" != "$zero" ] || exit 1
    [ "$new" != "$zero" ] || exit 1
    git merge-base --is-ancestor "$old" "$new" || exit 1
done
"""
        ).decode("ascii")
        pre_push_hook = base64.b64encode(b"#!/bin/sh\nexit 1\n").decode("ascii")
        environment_options = [
            "env",
            "-u",
            "GIT_ASKPASS",
            "-u",
            "SSH_ASKPASS",
            "-u",
            "SSH_AUTH_SOCK",
            "-u",
            "GIT_DIR",
            "-u",
            "GIT_WORK_TREE",
        ]
        git_environment = [
            "GIT_CONFIG_GLOBAL=/dev/null",
            "GIT_CONFIG_SYSTEM=/dev/null",
            "GIT_TERMINAL_PROMPT=0",
            "GCM_INTERACTIVE=never",
            "GIT_SSH_COMMAND=false",
            "GIT_CONFIG_COUNT=2",
            "GIT_CONFIG_KEY_0=credential.helper",
            "GIT_CONFIG_VALUE_0=",
            "GIT_CONFIG_KEY_1=core.hooksPath",
        ]
        if worker == "codex":
            command = [
                *environment_options,
                "-u",
                "GH_TOKEN",
                "-u",
                "GITHUB_TOKEN",
                *git_environment,
                "GH_CONFIG_DIR=/dev/null",
                "codex",
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "never",
                "--search",
                "--config",
                "sandbox_workspace_write.network_access=false",
                "--config",
                "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                "--config",
                "sandbox_workspace_write.exclude_slash_tmp=true",
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--color",
                "never",
                "--cd",
                ".",
                "-",
            ]
        elif worker == "claude":
            safe_tools = ",".join(
                (
                    "Read",
                    "Edit",
                    "Write",
                    "Glob",
                    "Grep",
                    "WebFetch",
                    "Bash(git add *)",
                    "Bash(git branch --show-current)",
                    "Bash(git commit -m *)",
                    "Bash(git diff *)",
                    "Bash(git log *)",
                    "Bash(git merge --ff-only *)",
                    "Bash(git merge-base *)",
                    "Bash(git rev-parse *)",
                    "Bash(git show *)",
                    "Bash(git status *)",
                    "Bash(gh pr view *)",
                    "Bash(gh pr edit *)",
                    "Bash(gh pr ready *)",
                    "Bash(gh pr reopen *)",
                    "Bash(gh issue view *)",
                    "Bash(gh issue edit *)",
                    "Bash(gh issue close *)",
                    "Bash(gh issue reopen *)",
                    "Bash(gh issue comment *)",
                )
            )
            command = [
                *environment_options,
                *git_environment,
                "claude",
                "--print",
                "--no-session-persistence",
                "--safe-mode",
                "--strict-mcp-config",
                "--restricted",
                "--allowed-tools",
                safe_tools,
                "--permission-mode",
                "dontAsk",
                "--permission-prompts",
                "none",
                "--output-format",
                "text",
            ]
        else:
            raise WorkerFailure("restricted session worker must be codex or claude")
        launch = shlex.join(command)
        return (
            "awm_recovery_hooks_root=$(git rev-parse --git-path hooks) && "
            "mkdir -p -- \"$awm_recovery_hooks_root\" && "
            "awm_recovery_hooks_root=$(cd \"$awm_recovery_hooks_root\" && pwd -P) && "
            'awm_recovery_hooks=$(mktemp -d '
            '"$awm_recovery_hooks_root/awm-recovery.XXXXXX") && '
            "trap 'rm -r -- \"$awm_recovery_hooks\"' EXIT && "
            "awm_recovery_ref=$(git symbolic-ref -q HEAD) && "
            f"printf %s {reference_hook} | base64 --decode > "
            '"$awm_recovery_hooks/reference-transaction" && '
            f"printf %s {pre_push_hook} | base64 --decode > "
            '"$awm_recovery_hooks/pre-push" && '
            'chmod 500 "$awm_recovery_hooks/reference-transaction" '
            '"$awm_recovery_hooks/pre-push" && '
            f"printf %s {encoded} | base64 --decode | "
            'AWM_RECOVERY_PROTECTED_REF="$awm_recovery_ref" '
            'GIT_CONFIG_VALUE_1="$awm_recovery_hooks" '
            f"{launch}"
        )

    @staticmethod
    def _publication_disabled_agent_command(worker: str, prompt: str) -> str:
        """Launch a development agent with local commits but no publication."""
        encoded = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
        reference_hook = base64.b64encode(
            b"""#!/bin/sh
phase=$1
[ "$phase" = prepared ] || exit 0
[ -n "${AWM_DELIVERY_PROTECTED_REF:-}" ] || exit 0
zero=0000000000000000000000000000000000000000
while read old new ref; do
    if [ "$ref" = ORIG_HEAD ]; then
        protected=$(git rev-parse "$AWM_DELIVERY_PROTECTED_REF") || exit 1
        [ "$new" = "$protected" ] || exit 1
        continue
    fi
    [ "$ref" = HEAD ] || [ "$ref" = "$AWM_DELIVERY_PROTECTED_REF" ] || exit 1
    [ "$old" != "$zero" ] || exit 1
    [ "$new" != "$zero" ] || exit 1
    git merge-base --is-ancestor "$old" "$new" || exit 1
done
"""
        ).decode("ascii")
        pre_push_hook = base64.b64encode(b"#!/bin/sh\nexit 1\n").decode("ascii")
        receive_pack_wrapper = base64.b64encode(
            b"#!/bin/sh\n"
            b"printf '%s\\n' 'git push is disabled for this session' >&2\n"
            b"exit 1\n"
        ).decode("ascii")
        resource_allocator = base64.b64encode(
            b"""import os
import secrets
import signal
import sys

manifest, parent, prefix = sys.argv[1:]
blocked = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
previous = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
while True:
    path = os.path.join(parent, prefix + secrets.token_hex(16))
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        continue
    break
try:
    descriptor = os.open(manifest, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(descriptor, os.fsencode(path) + b"\\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
except BaseException:
    os.rmdir(path)
    raise
print(path, flush=True)
signal.pthread_sigmask(signal.SIG_SETMASK, previous)
"""
        ).decode("ascii")
        recovery_snapshot = base64.b64encode(
            b"""import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys

(
    git,
    timeout_command,
    shadow,
    worktree,
    recovery,
    protected_ref,
    old,
    common_git_dir,
) = sys.argv[1:]
blocked = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
recovery_path = Path(recovery)
base_environment = os.environ.copy()
base_environment.update({"GIT_DIR": shadow, "GIT_WORK_TREE": worktree})


def run_git(arguments, *, environment=None, stdout=subprocess.PIPE, input_text=None):
    result = subprocess.run(
        [
            timeout_command,
            "--signal=TERM",
            "--kill-after=1s",
            "60s",
            git,
            *arguments,
        ],
        cwd=worktree,
        env=environment or base_environment,
        input=input_text,
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=True,
        timeout=65,
        check=False,
        start_new_session=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or "Git command failed"
        raise RuntimeError(detail[:1000])
    return result


def commit_tree(tree, parent, message):
    return run_git(
        ["commit-tree", tree, "-p", parent], input_text=message + "\\n"
    ).stdout.strip()


def nested_git_roots():
    roots = []
    for current, directories, files in os.walk(worktree, followlinks=False):
        if current != worktree and (".git" in directories or ".git" in files):
            roots.append(Path(current))
        if ".git" in directories:
            directories.remove(".git")
    return roots


def is_within(path, parent):
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def run_nested(root, arguments, *, environment=None, stdout=subprocess.PIPE):
    environment = (environment or os.environ).copy()
    requested_git_dir = environment.get("GIT_DIR")
    requested_worktree = environment.get("GIT_WORK_TREE")
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    if requested_git_dir is not None:
        environment["GIT_DIR"] = requested_git_dir
    if requested_worktree is not None:
        environment["GIT_WORK_TREE"] = requested_worktree
    result = subprocess.run(
        [
            timeout_command,
            "--signal=TERM",
            "--kill-after=1s",
            "60s",
            git,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(root),
            *arguments,
        ],
        stdout=stdout,
        stderr=subprocess.PIPE,
        timeout=65,
        check=False,
        start_new_session=True,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError((detail or "nested Git command failed")[:1000])
    return result


def capture_nested_repository(root, destination):
    isolated = Path(worktree).resolve()
    common = Path(common_git_dir).resolve()
    git_dir = Path(
        os.fsdecode(run_nested(root, ["rev-parse", "--absolute-git-dir"]).stdout).strip()
    ).resolve()
    if not (is_within(git_dir, isolated) or is_within(git_dir, common)):
        raise RuntimeError("nested Git directory escapes owned repository state")
    nested_environment = os.environ.copy()
    nested_environment.update(
        {"GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(root)}
    )
    object_dir = Path(
        os.fsdecode(
            run_nested(
                root,
                ["rev-parse", "--path-format=absolute", "--git-path", "objects"],
                environment=nested_environment,
            ).stdout
        ).strip()
    ).resolve()
    if not (is_within(object_dir, isolated) or is_within(object_dir, common)):
        raise RuntimeError("nested Git object directory escapes owned repository state")
    index_path = Path(
        os.fsdecode(
            run_nested(
                root,
                ["rev-parse", "--path-format=absolute", "--git-path", "index"],
                environment=nested_environment,
            ).stdout
        ).strip()
    ).resolve()
    if not (is_within(index_path, isolated) or is_within(index_path, common)):
        raise RuntimeError("nested Git index escapes owned repository state")

    destination.mkdir(mode=0o700)
    side_git = destination / "snapshot.git"
    initialize = subprocess.run(
        [
            timeout_command,
            "--signal=TERM",
            "--kill-after=1s",
            "60s",
            git,
            "init",
            "--bare",
            "--quiet",
            str(side_git),
        ],
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=65,
        check=False,
        start_new_session=True,
    )
    if initialize.returncode:
        detail = initialize.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError((detail or "nested recovery repository init failed")[:1000])
    (side_git / "objects" / "info" / "alternates").write_text(
        str(object_dir) + "\\n", encoding="utf-8"
    )
    side_environment = os.environ.copy()
    side_environment.pop("GIT_COMMON_DIR", None)
    side_environment.update(
        {
            "GIT_DIR": str(side_git),
            "GIT_WORK_TREE": str(root),
            "GIT_AUTHOR_NAME": "Agent Workflow Manager",
            "GIT_AUTHOR_EMAIL": "recovery@localhost",
            "GIT_COMMITTER_NAME": "Agent Workflow Manager",
            "GIT_COMMITTER_EMAIL": "recovery@localhost",
        }
    )

    def run_side(arguments, *, input_text=None):
        return run_git(arguments, environment=side_environment, input_text=input_text)

    head_result = subprocess.run(
        [git, "rev-parse", "--verify", "HEAD"],
        env={
            **os.environ,
            "GIT_DIR": str(git_dir),
            "GIT_WORK_TREE": str(root),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=60,
        check=False,
        start_new_session=True,
    )
    head = head_result.stdout.strip() if head_result.returncode == 0 else None
    if head is None:
        baseline_tree = run_side(
            ["hash-object", "-t", "tree", "-w", "--stdin"], input_text=""
        ).stdout.strip()
    else:
        baseline_tree = os.fsdecode(
            run_nested(
                root, ["rev-parse", "HEAD^{tree}"], environment=nested_environment
            ).stdout
        ).strip()
    staged_index = destination / "staged.index"
    staged_environment = side_environment.copy()
    staged_environment["GIT_INDEX_FILE"] = str(staged_index)
    if index_path.is_file():
        shutil.copyfile(index_path, staged_index)
    else:
        run_git(["read-tree", "--empty"], environment=staged_environment)
    staged_tree = run_git(
        ["write-tree"], environment=staged_environment
    ).stdout.strip()
    staged_index.unlink()
    baseline_commit = run_side(
        ["commit-tree", baseline_tree], input_text="AWM nested recovery: baseline\\n"
    ).stdout.strip()
    staged_commit = run_side(
        ["commit-tree", staged_tree, "-p", baseline_commit],
        input_text="AWM nested recovery: staged state\\n",
    ).stdout.strip()
    worktree_index = destination / "worktree.index"
    worktree_environment = side_environment.copy()
    worktree_environment["GIT_INDEX_FILE"] = str(worktree_index)
    run_git(["read-tree", staged_tree], environment=worktree_environment)
    run_git(["add", "-A", "--", "."], environment=worktree_environment)
    worktree_tree = run_git(
        ["write-tree"], environment=worktree_environment
    ).stdout.strip()
    worktree_commit = run_side(
        ["commit-tree", worktree_tree, "-p", staged_commit],
        input_text="AWM nested recovery: final worktree\\n",
    ).stdout.strip()
    worktree_index.unlink()
    run_side(["update-ref", "refs/awm-delivery/baseline", baseline_commit])
    run_side(["update-ref", "refs/awm-delivery/staged", staged_commit])
    run_side(["update-ref", "refs/awm-delivery/worktree", worktree_commit])
    bundle_temporary = destination / "staged.bundle.tmp"
    run_side(
        [
            "bundle",
            "create",
            str(bundle_temporary),
            "refs/awm-delivery/baseline",
            "refs/awm-delivery/staged",
            "refs/awm-delivery/worktree",
        ]
    )
    os.replace(bundle_temporary, destination / "staged.bundle")
    metadata = {
        "formatVersion": 1,
        "path": os.path.relpath(root, worktree),
        "headCommit": head,
        "baselineCommit": baseline_commit,
        "stagedCommit": staged_commit,
        "worktreeCommit": worktree_commit,
    }
    (destination / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=True, sort_keys=True) + "\\n",
        encoding="ascii",
    )
    shutil.rmtree(side_git)
    return baseline_tree != staged_tree or staged_tree != worktree_tree


try:
    shadow_commit = run_git(["rev-parse", protected_ref]).stdout.strip()
    staged_tree = run_git(["write-tree"]).stdout.strip()
    staged_commit = commit_tree(
        staged_tree, shadow_commit, "AWM recovery: staged agent state"
    )

    recovery_index = recovery_path / "worktree.index"
    worktree_environment = base_environment.copy()
    worktree_environment["GIT_INDEX_FILE"] = str(recovery_index)
    run_git(["read-tree", staged_tree], environment=worktree_environment)
    run_git(["add", "-A", "--", "."], environment=worktree_environment)
    worktree_tree = run_git(
        ["write-tree"], environment=worktree_environment
    ).stdout.strip()
    worktree_commit = commit_tree(
        worktree_tree, staged_commit, "AWM recovery: final agent worktree"
    )
    recovery_index.unlink()

    recovery_refs = {
        "refs/awm-delivery/shadow": shadow_commit,
        "refs/awm-delivery/staged": staged_commit,
        "refs/awm-delivery/worktree": worktree_commit,
    }
    for reference, commit in recovery_refs.items():
        run_git(["update-ref", reference, commit])

    bundle_temporary = recovery_path / "recovery.bundle.tmp"
    bundle = recovery_path / "recovery.bundle"
    run_git(
        [
            "bundle",
            "create",
            str(bundle_temporary),
            *recovery_refs,
            "^" + old,
        ]
    )
    os.replace(bundle_temporary, bundle)

    nested_root = recovery_path / "nested"
    nested_repositories = nested_git_roots()
    nested_residual = False
    for index, nested_repository in enumerate(nested_repositories):
        if index == 0:
            nested_root.mkdir(mode=0o700)
        nested_residual = (
            capture_nested_repository(
                nested_repository, nested_root / f"{index:04d}"
            )
            or nested_residual
        )

    status_path = recovery_path / "status.porcelain"
    cleanliness = "verified"
    try:
        with status_path.open("w", encoding="utf-8") as status_output:
            status = subprocess.run(
                [
                    timeout_command,
                    "--signal=TERM",
                    "--kill-after=1s",
                    "60s",
                    git,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ],
                cwd=worktree,
                env=base_environment,
                stdout=status_output,
                stderr=subprocess.PIPE,
                text=True,
                timeout=65,
                check=False,
                start_new_session=True,
            )
        if status.returncode:
            cleanliness = "unverified"
        elif status_path.stat().st_size or nested_residual:
            cleanliness = "residual"
    except (OSError, subprocess.SubprocessError):
        cleanliness = "unverified"

    metadata = {
        "formatVersion": 1,
        "protectedRef": protected_ref,
        "baseCommit": old,
        "shadowCommit": shadow_commit,
        "stagedCommit": staged_commit,
        "worktreeCommit": worktree_commit,
        "cleanliness": cleanliness,
        "nestedRecoveryCount": len(nested_repositories),
    }
    metadata_temporary = recovery_path / "metadata.json.tmp"
    metadata_temporary.write_text(
        json.dumps(metadata, sort_keys=True) + "\\n", encoding="utf-8"
    )
    os.replace(metadata_temporary, recovery_path / "metadata.json")
    complete_temporary = recovery_path / ".complete.tmp"
    complete_temporary.write_text("1\\n", encoding="ascii")
    os.replace(complete_temporary, recovery_path / ".complete")
    print(cleanliness)
except (OSError, RuntimeError, subprocess.SubprocessError) as error:
    print(f"publication-disabled recovery failed: {error}", file=sys.stderr)
    raise SystemExit(74)
"""
        ).decode("ascii")
        git_wrapper = base64.b64encode(
            b"""#!/bin/sh
probe=$PWD
command=
real_path=$PATH
while :; do
    real_git=$(PATH=$real_path command -v git) || exit 1
    case $real_git in
        "$0") case $real_path in *:*) real_path=${real_path#*:} ;; *) exit 1 ;; esac ;;
        /*) break ;;
        *) exit 1 ;;
    esac
done
wrapper_root=${0%/*}/..
shadow_git_dir=$(tr -d '\n' < "$wrapper_root/shadow-git-dir") || exit 1
protected_root=$(tr -d '\n' < "$wrapper_root/protected-root") || exit 1
next_is_c=false
skip_next=false
for argument do
    if [ "$skip_next" = true ]; then
        skip_next=false
        continue
    fi
    if [ "$next_is_c" = true ]; then
        probe=$(cd "$probe" && cd "$argument" && pwd -P) || exec "$real_git" "$@"
        next_is_c=false
        continue
    fi
    case $argument in
        -C) next_is_c=true ;;
        -C?*) probe=$(cd "$probe" && cd "${argument#-C}" && pwd -P) || exec "$real_git" "$@" ;;
        -c|-c?*|--config-env|--config-env=*) exit 1 ;;
        --git-dir|--work-tree|--namespace|--super-prefix) skip_next=true ;;
        -*) ;;
        *) command=$argument; break ;;
    esac
done
[ "$command" != push ] || exit 1
root=$("$real_git" -C "$probe" rev-parse --show-toplevel 2>/dev/null) || {
    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
    exec "$real_git" "$@"
}
root=$(cd "$root" && pwd -P) || exit 1
if [ "$root" = "$protected_root" ]; then
    GIT_DIR=$shadow_git_dir
    GIT_WORK_TREE=$protected_root
    export GIT_DIR GIT_WORK_TREE
else
    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
    unset AWM_DELIVERY_PROTECTED_REF
fi
exec "$real_git" "$@"
"""
        ).decode("ascii")
        environment_options = [
            "env",
            "-u",
            "GH_TOKEN",
            "-u",
            "GITHUB_TOKEN",
            "-u",
            "GIT_ASKPASS",
            "-u",
            "SSH_ASKPASS",
            "-u",
            "SSH_AUTH_SOCK",
            "-u",
            "GIT_DIR",
            "-u",
            "GIT_WORK_TREE",
            "-u",
            "GIT_INDEX_FILE",
            "-u",
            "GIT_OBJECT_DIRECTORY",
            "-u",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "-u",
            "GIT_COMMON_DIR",
            "-u",
            "AWM_DELIVERY_REAL_GIT",
            "-u",
            "AWM_DELIVERY_SHADOW_GIT_DIR",
            "-u",
            "AWM_DELIVERY_PROTECTED_ROOT",
        ]
        git_environment = [
            "GIT_CONFIG_GLOBAL=/dev/null",
            "GIT_CONFIG_SYSTEM=/dev/null",
            "GIT_TERMINAL_PROMPT=0",
            "GCM_INTERACTIVE=never",
            "GIT_SSH_COMMAND=false",
            "GIT_CONFIG_COUNT=2",
            "GIT_CONFIG_KEY_0=credential.helper",
            "GIT_CONFIG_VALUE_0=",
            "GIT_CONFIG_KEY_1=core.hooksPath",
        ]
        if worker == "codex":
            command = [
                *environment_options,
                *git_environment,
                "codex",
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "never",
                "--search",
                "--config",
                "sandbox_workspace_write.network_access=false",
                "--config",
                "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                "--config",
                "sandbox_workspace_write.exclude_slash_tmp=true",
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--color",
                "never",
                "--cd",
                ".",
                "-",
            ]
        elif worker == "claude":
            sandbox_settings = json.dumps(
                {
                    "sandbox": {
                        "enabled": True,
                        "allowUnsandboxedCommands": False,
                        "failIfUnavailable": True,
                        "filesystem": {"denyWrite": ["./.git"]},
                        "network": {
                            "allowedDomains": [],
                            "deniedDomains": ["github.com", "*.github.com"],
                            "strictAllowlist": True,
                        },
                        "credentials": {
                            "envVars": [
                                {"name": "GH_TOKEN", "mode": "deny"},
                                {"name": "GITHUB_TOKEN", "mode": "deny"},
                            ],
                            "files": [
                                {"path": "~/.config/gh", "mode": "deny"},
                                {"path": "~/.git-credentials", "mode": "deny"},
                                {"path": "~/.ssh", "mode": "deny"},
                            ],
                        },
                    }
                },
                separators=(",", ":"),
            )
            safe_tools = ",".join(
                (
                    "Read",
                    "Edit",
                    "Write",
                    "Glob",
                    "Grep",
                    "WebFetch",
                    "Bash",
                )
            )
            command = [
                *environment_options,
                *git_environment,
                "claude",
                "--print",
                "--no-session-persistence",
                "--safe-mode",
                "--strict-mcp-config",
                "--restricted",
                "--settings",
                sandbox_settings,
                "--allowed-tools",
                safe_tools,
                "--permission-mode",
                "dontAsk",
                "--permission-prompts",
                "none",
                "--output-format",
                "text",
            ]
        else:
            raise WorkerFailure(
                "publication-disabled session worker must be codex or claude"
            )
        additional_git_directories = '--add-dir "$awm_delivery_shadow_git_dir"'
        option_index = (
            command.index("exec") if worker == "codex" else command.index("claude") + 1
        )
        launch = (
            f"{shlex.join(command[:option_index])} {additional_git_directories} "
            f"{shlex.join(command[option_index:])}"
        )
        return (
            "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY "
            "GIT_EXEC_PATH GIT_COMMON_DIR "
            "GIT_ALTERNATE_OBJECT_DIRECTORIES && "
            "awm_delivery_real_git=$(command -v git) && "
            'case "$awm_delivery_real_git" in /*) ;; *) exit 1 ;; esac && '
            "awm_delivery_chmod=$(command -v chmod) && "
            'case "$awm_delivery_chmod" in /*) ;; *) exit 1 ;; esac && '
            "awm_delivery_rm=$(command -v rm) && "
            'case "$awm_delivery_rm" in /*) ;; *) exit 1 ;; esac && '
            "awm_delivery_ln=$(command -v ln) && "
            'case "$awm_delivery_ln" in /*) ;; *) exit 1 ;; esac && '
            "awm_delivery_cp=$(command -v cp) && "
            'case "$awm_delivery_cp" in /*) ;; *) exit 1 ;; esac && '
            "awm_delivery_bwrap=$(command -v bwrap) || { "
            "printf '%s\\n' 'publication-disabled sessions require Bubblewrap "
            "(bwrap); install it and restart Agent Workflow Manager' >&2; "
            "exit 1; } && "
            'case "$awm_delivery_bwrap" in /*) ;; *) exit 1 ;; esac && '
            'if ! "$awm_delivery_bwrap" --die-with-parent --new-session '
            "--ro-bind / / --dev-bind /dev /dev --proc /proc --tmpfs /tmp "
            "-- /bin/true "
            ">/dev/null 2>&1; then "
            "printf '%s\\n' 'Bubblewrap cannot create the required sandbox; "
            "enable unprivileged user namespaces or install a distribution "
            "Bubblewrap package with supported privilege setup' >&2; exit 1; fi && "
            "awm_delivery_python=$(command -v python3) && "
            'case "$awm_delivery_python" in /*) ;; *) exit 1 ;; esac && '
            "awm_delivery_timeout=$(command -v timeout) && "
            'case "$awm_delivery_timeout" in /*) ;; *) exit 1 ;; esac && '
            "awm_delivery_original_path=$PATH && "
            f"awm_delivery_worker=$(command -v {worker}) && "
            'case "$awm_delivery_worker" in /*) ;; *) exit 1 ;; esac && '
            'awm_delivery_worker_dir=${awm_delivery_worker%/*} && '
            "awm_delivery_root=$(pwd -P) && "
            'awm_delivery_resource_parent=${awm_delivery_root%/*} && '
            '[ -n "$awm_delivery_resource_parent" ] || '
            'awm_delivery_resource_parent=/ && '
            "awm_delivery_git_dir=$(git rev-parse --path-format=absolute "
            "--absolute-git-dir) && "
            'awm_delivery_git_dir=$(cd "$awm_delivery_git_dir" && pwd -P) && '
            "awm_delivery_common_git_dir=$(git rev-parse --path-format=absolute "
            "--git-common-dir) && "
            'awm_delivery_common_git_dir=$(cd "$awm_delivery_common_git_dir" '
            '&& pwd -P) && '
            "awm_delivery_object_dir=$(git rev-parse --path-format=absolute "
            "--git-path objects) && "
            'awm_delivery_object_dir=$(cd "$awm_delivery_object_dir" && pwd -P) && '
            "awm_delivery_ref=$(git symbolic-ref -q HEAD) && "
            'awm_delivery_old=$(git rev-parse "$awm_delivery_ref") && '
            "awm_delivery_sparse=$(git config --bool core.sparseCheckout "
            "2>/dev/null || :) && "
            'if [ "$awm_delivery_sparse" = true ]; then '
            "printf '%s\\n' 'publication-disabled sessions do not support "
            "sparse checkouts' >&2; exit 1; fi && "
            "awm_delivery_user_name=$(git config --get user.name) && "
            "awm_delivery_user_email=$(git config --get user.email) && "
            "awm_delivery_hooks_root=$(git rev-parse --git-path hooks) && "
            'mkdir -p -- "$awm_delivery_hooks_root" && '
            'awm_delivery_hooks_root=$(cd "$awm_delivery_hooks_root" && pwd -P) && '
            "umask 077 && "
            'awm_delivery_manifest="$awm_delivery_hooks_root/'
            '.awm-delivery.$$.resources" && '
            "awm_delivery_hooks='' && "
            "awm_delivery_shadow_git_dir='' && "
            "awm_delivery_isolated_root='' && "
            "awm_delivery_recovery='' && "
            "awm_delivery_cleanup() { "
            "trap - EXIT HUP INT TERM; "
            "awm_delivery_primary_status=$1; "
            "awm_delivery_cleanup_failed=0; "
            'if [ -f "$awm_delivery_manifest" ]; then '
            'while IFS= read -r awm_delivery_cleanup_dir; do '
            '[ -n "$awm_delivery_cleanup_dir" ] || continue; '
            'if [ "$awm_delivery_primary_status" -ne 0 ] && '
            '[ "$awm_delivery_cleanup_dir" = "$awm_delivery_recovery" ] && '
            '[ -f "$awm_delivery_recovery/.complete" ]; then continue; fi; '
            'if ! "$awm_delivery_timeout" --signal=TERM --kill-after=0.2s 1s '
            '"$awm_delivery_chmod" -R u+rwX -- "$awm_delivery_cleanup_dir" '
            "2>/dev/null; then "
            "awm_delivery_cleanup_failed=1; "
            "fi; "
            'if ! "$awm_delivery_timeout" --signal=TERM --kill-after=0.2s 1s '
            '"$awm_delivery_rm" -rf -- "$awm_delivery_cleanup_dir" '
            "2>/dev/null; then "
            "awm_delivery_cleanup_failed=1; "
            "fi; "
            'done < "$awm_delivery_manifest"; '
            'if ! "$awm_delivery_timeout" --signal=TERM --kill-after=0.2s 1s '
            '"$awm_delivery_rm" -f -- "$awm_delivery_manifest" '
            "2>/dev/null; then "
            "awm_delivery_cleanup_failed=1; "
            "fi; "
            "fi; "
            'if [ "$awm_delivery_cleanup_failed" -ne 0 ]; then '
            "printf '%s\\n' 'publication-disabled session cleanup failed; "
            "temporary resources may remain' >&2; "
            'if [ "$awm_delivery_primary_status" -ne 0 ]; then '
            'exit "$awm_delivery_primary_status"; '
            "fi; "
            "exit 1; "
            "fi; "
            'if [ "$awm_delivery_primary_status" -ne 0 ] && '
            '[ -n "$awm_delivery_recovery" ] && '
            '[ -f "$awm_delivery_recovery/.complete" ]; then '
            "printf '%s\\n' \"publication-disabled agent output recovery "
            'retained at $awm_delivery_recovery" >&2; fi; '
            'exit "$awm_delivery_primary_status"; '
            "} && "
            "trap 'awm_delivery_cleanup $?' EXIT && "
            "trap 'awm_delivery_cleanup 129' HUP && "
            "trap 'awm_delivery_cleanup 130' INT && "
            "trap 'awm_delivery_cleanup 143' TERM && "
            ': > "$awm_delivery_manifest" && '
            f"awm_delivery_allocate=$(printf %s {resource_allocator} | "
            "base64 --decode) && "
            'awm_delivery_hooks=$("$awm_delivery_python" -c '
            '"$awm_delivery_allocate" "$awm_delivery_manifest" '
            '"$awm_delivery_hooks_root" "awm-delivery.") && '
            'awm_delivery_shadow_git_dir=$("$awm_delivery_python" -c '
            '"$awm_delivery_allocate" "$awm_delivery_manifest" '
            '"$awm_delivery_resource_parent" "awm-delivery-shadow.") && '
            'awm_delivery_isolated_root=$("$awm_delivery_python" -c '
            '"$awm_delivery_allocate" "$awm_delivery_manifest" '
            '"$awm_delivery_resource_parent" "awm-delivery-worktree.") && '
            'awm_delivery_recovery_root="$awm_delivery_common_git_dir/'
            'awm-delivery-recovery" && '
            'mkdir -p -- "$awm_delivery_recovery_root" && '
            'awm_delivery_recovery=$("$awm_delivery_python" -c '
            '"$awm_delivery_allocate" "$awm_delivery_manifest" '
            '"$awm_delivery_recovery_root" "output.") && '
            '"$awm_delivery_cp" -a -- "$awm_delivery_root/." '
            '"$awm_delivery_isolated_root/" && '
            'mkdir -p -- "$awm_delivery_hooks/gh" "$awm_delivery_hooks/bin" && '
            f"printf %s {reference_hook} | base64 --decode > "
            '"$awm_delivery_hooks/reference-transaction" && '
            f"printf %s {pre_push_hook} | base64 --decode > "
            '"$awm_delivery_hooks/pre-push" && '
            f"printf %s {git_wrapper} | base64 --decode > "
            '"$awm_delivery_hooks/bin/git" && '
            f"printf %s {receive_pack_wrapper} | base64 --decode > "
            '"$awm_delivery_hooks/bin/git-receive-pack" && '
            'printf \'%s\\n\' "$awm_delivery_shadow_git_dir" > '
            '"$awm_delivery_hooks/shadow-git-dir" && '
            'printf \'%s\\n\' "$awm_delivery_root" > '
            '"$awm_delivery_hooks/protected-root" && '
            'chmod 500 "$awm_delivery_hooks/reference-transaction" '
            '"$awm_delivery_hooks/pre-push" "$awm_delivery_hooks/bin/git" '
            '"$awm_delivery_hooks/bin/git-receive-pack" && '
            'chmod 400 "$awm_delivery_hooks/shadow-git-dir" '
            '"$awm_delivery_hooks/protected-root" && '
            'awm_delivery_git_exec_path=$("$awm_delivery_real_git" '
            '--exec-path) && '
            'case "$awm_delivery_git_exec_path" in /*) ;; *) exit 1 ;; esac && '
            'for awm_delivery_git_helper in '
            '"$awm_delivery_git_exec_path"/git-*; do '
            '[ -f "$awm_delivery_git_helper" ] || continue; '
            'awm_delivery_git_helper_name=${awm_delivery_git_helper##*/}; '
            '[ "$awm_delivery_git_helper_name" = git-receive-pack ] || '
            '"$awm_delivery_ln" -s -- "$awm_delivery_git_helper" '
            '"$awm_delivery_hooks/bin/$awm_delivery_git_helper_name" || exit; '
            'done && '
            '"$awm_delivery_real_git" init --bare --quiet '
            '"$awm_delivery_shadow_git_dir" && '
            '"$awm_delivery_real_git" --git-dir="$awm_delivery_shadow_git_dir" '
            "config core.bare false && "
            '"$awm_delivery_real_git" --git-dir="$awm_delivery_shadow_git_dir" '
            'config core.worktree "$awm_delivery_root" && '
            '"$awm_delivery_real_git" --git-dir="$awm_delivery_shadow_git_dir" '
            'config user.name "$awm_delivery_user_name" && '
            '"$awm_delivery_real_git" --git-dir="$awm_delivery_shadow_git_dir" '
            'config user.email "$awm_delivery_user_email" && '
            "printf '%s\\n' \"$awm_delivery_object_dir\" > "
            '"$awm_delivery_shadow_git_dir/objects/info/alternates" && '
            '"$awm_delivery_real_git" --git-dir="$awm_delivery_shadow_git_dir" '
            'symbolic-ref HEAD "$awm_delivery_ref" && '
            '"$awm_delivery_real_git" --git-dir="$awm_delivery_shadow_git_dir" '
            'update-ref "$awm_delivery_ref" "$awm_delivery_old" && '
            'GIT_DIR="$awm_delivery_shadow_git_dir" '
            'GIT_WORK_TREE="$awm_delivery_root" '
            '"$awm_delivery_real_git" read-tree "$awm_delivery_old" && '
            'PATH="$awm_delivery_hooks/bin:$PATH" && '
            "export PATH && "
            'set -- "$awm_delivery_bwrap" --die-with-parent --new-session '
            '--ro-bind / / --dev-bind /dev /dev --proc /proc '
            '--tmpfs /tmp '
            '--bind "$awm_delivery_isolated_root" "$awm_delivery_root" '
            '--bind "$awm_delivery_shadow_git_dir" '
            '"$awm_delivery_shadow_git_dir" '
            '--ro-bind "$awm_delivery_common_git_dir" '
            '"$awm_delivery_common_git_dir" '
            '--ro-bind "$awm_delivery_git_dir" "$awm_delivery_git_dir" '
            '--ro-bind "$awm_delivery_object_dir" "$awm_delivery_object_dir" '
            '--ro-bind "$awm_delivery_hooks" "$awm_delivery_hooks" '
            '--chdir "$awm_delivery_root" && '
            'case "$awm_delivery_worker_dir" in '
            '"$awm_delivery_root"|"$awm_delivery_root"/*) ;; '
            '*) set -- "$@" --ro-bind "$awm_delivery_worker_dir" '
            '"$awm_delivery_worker_dir" ;; esac && '
            f"printf %s {encoded} | base64 --decode | "
            'AWM_DELIVERY_PROTECTED_REF="$awm_delivery_ref" '
            "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 "
            'GH_CONFIG_DIR="$awm_delivery_hooks/gh" '
            'GIT_EXEC_PATH="$awm_delivery_hooks/bin" '
            'GIT_CONFIG_VALUE_1="$awm_delivery_hooks" '
            f'"$@" {launch}; '
            "awm_delivery_status=$?; "
            "PATH=$awm_delivery_original_path; export PATH; "
            f"awm_delivery_snapshot=$(printf %s {recovery_snapshot} | "
            "base64 --decode) && "
            'awm_delivery_cleanliness=$("$awm_delivery_python" -c '
            '"$awm_delivery_snapshot" "$awm_delivery_real_git" '
            '"$awm_delivery_timeout" '
            '"$awm_delivery_shadow_git_dir" "$awm_delivery_isolated_root" '
            '"$awm_delivery_recovery" "$awm_delivery_ref" '
            '"$awm_delivery_old" "$awm_delivery_common_git_dir") || { '
            "printf '%s\\n' "
            '"publication-disabled agent output recovery failed" >&2; '
            'if [ "$awm_delivery_status" -ne 0 ]; then '
            'exit "$awm_delivery_status"; fi; exit 74; }; '
            'if [ "$awm_delivery_cleanliness" = unverified ]; then '
            "printf '%s\\n' "
            '"publication-disabled session cleanliness could not be verified" >&2; '
            'if [ "$awm_delivery_status" -ne 0 ]; then '
            'exit "$awm_delivery_status"; fi; exit 1; fi; '
            'if [ "$awm_delivery_cleanliness" = residual ]; then '
            "printf '%s\\n' "
            '"publication-disabled session left uncommitted changes" >&2; '
            'if [ "$awm_delivery_status" -ne 0 ]; then '
            'exit "$awm_delivery_status"; fi; exit 1; fi; '
            '[ "$awm_delivery_status" -eq 0 ] || exit "$awm_delivery_status"; '
            'awm_delivery_new=$(tr -d \'\\n\' < '
            '"$awm_delivery_shadow_git_dir/$awm_delivery_ref") && '
            'case "$awm_delivery_new" in \'\'|*[!0-9a-f]*) exit 1 ;; esac && '
            'awm_delivery_current=$("$awm_delivery_real_git" rev-parse '
            '"$awm_delivery_ref") && '
            '[ "$awm_delivery_current" = "$awm_delivery_old" ] && '
            'GIT_ALTERNATE_OBJECT_DIRECTORIES="$awm_delivery_shadow_git_dir/objects" '
            '"$awm_delivery_real_git" cat-file -e "$awm_delivery_new^{commit}" && '
            'GIT_ALTERNATE_OBJECT_DIRECTORIES="$awm_delivery_shadow_git_dir/objects" '
            '"$awm_delivery_real_git" merge-base --is-ancestor '
            '"$awm_delivery_old" "$awm_delivery_new" && '
            "printf '%s\\n^%s\\n' \"$awm_delivery_new\" "
            '"$awm_delivery_old" | '
            'GIT_ALTERNATE_OBJECT_DIRECTORIES="$awm_delivery_shadow_git_dir/objects" '
            '"$awm_delivery_real_git" '
            "pack-objects --quiet --stdout --revs | "
            '"$awm_delivery_real_git" index-pack --stdin --fix-thin --strict '
            ">/dev/null && "
            'AWM_DELIVERY_PROTECTED_REF="$awm_delivery_ref" '
            'AWM_DELIVERY_PROTECTED_ROOT="$awm_delivery_root" '
            '"$awm_delivery_real_git" -c '
            'core.hooksPath="$awm_delivery_hooks" update-ref '
            '"$awm_delivery_ref" "$awm_delivery_new" "$awm_delivery_old" && '
            '{ "$awm_delivery_real_git" read-tree --reset -u '
            '"$awm_delivery_new"; '
            "awm_delivery_index_status=$?; "
            'if [ "$awm_delivery_index_status" -ne 0 ]; then '
            'if ! "$awm_delivery_real_git" -c core.hooksPath=/dev/null '
            'update-ref "$awm_delivery_ref" "$awm_delivery_old" '
            '"$awm_delivery_new"; then '
            "printf '%s\\n' 'publication-disabled delivery rollback failed' >&2; "
            "fi; "
            '"$awm_delivery_real_git" read-tree "$awm_delivery_old" '
            "2>/dev/null || :; "
            'exit "$awm_delivery_index_status"; '
            "fi; }"
        )

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
            stdout=result.stdout,
            stderr=result.stderr,
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
    def _shell_wrapper(
        command: str, cwd: str, result_path: str, max_output_chars: int = 1_000_000
    ) -> str:
        command_text = shlex.quote(command)
        cwd_text = shlex.quote(cwd)
        result_text = shlex.quote(result_path)
        pending_result_text = shlex.quote(f"{result_path}.pending")
        stdout_text = shlex.quote(f"{result_path}.stdout")
        stderr_text = shlex.quote(f"{result_path}.stderr")
        stdout_pipe = shlex.quote(f"{result_path}.stdout.pipe")
        stderr_pipe = shlex.quote(f"{result_path}.stderr.pipe")
        command_done = shlex.quote(f"{result_path}.command_done")
        capture = f"{shlex.quote(sys.executable)} -m purplemux_client.shell_capture"
        capture_chars = max_output_chars + 1
        return (
            f"mkfifo -- {stdout_pipe} {stderr_pipe} || exit 1; "
            f"{capture} {stdout_text} {command_done} {capture_chars} 1 "
            f"< {stdout_pipe} & __awm_stdout_pid=$!; "
            f"{capture} {stderr_text} {command_done} {capture_chars} 2 "
            f"< {stderr_pipe} & __awm_stderr_pid=$!; "
            f"__awm_exit=0; (cd -- {cwd_text} && bash -lc {command_text}) "
            f"> {stdout_pipe} 2> {stderr_pipe} || __awm_exit=$?; "
            f": > {command_done}; "
            f"until test -f {stdout_text} && test -f {stderr_text}; do "
            f"kill -0 $__awm_stdout_pid && kill -0 $__awm_stderr_pid "
            f"|| exit 1; sleep 0.02; done; "
            f"rm -- {stdout_pipe} {stderr_pipe} {command_done}; "
            f"printf '{{\"exitCode\":%s}}\\n' "
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
        stdout_path = Path(f"{shell_run.result_path}.stdout")
        stderr_path = Path(f"{shell_run.result_path}.stderr")
        if stdout_path.exists() or stderr_path.exists():
            try:
                stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise WorkerFailure(
                    f"shell terminal {session_id} published unreadable output"
                ) from exc
        else:
            # Older result files published only the exit code.
            stdout = stderr = ""
        return ShellResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    @staticmethod
    def _cleanup_shell_result(shell_run: _ShellRun) -> None:
        for path in (
            shell_run.result_path,
            f"{shell_run.result_path}.pending",
            f"{shell_run.result_path}.stdout",
            f"{shell_run.result_path}.stderr",
            f"{shell_run.result_path}.stdout.pending",
            f"{shell_run.result_path}.stderr.pending",
            f"{shell_run.result_path}.command_done",
            f"{shell_run.result_path}.stdout.pipe",
            f"{shell_run.result_path}.stderr.pipe",
        ):
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
        deadline_check: Callable[[], float] | None = None,
        bound_reads: bool = False,
    ) -> TabState:
        if not name.strip() or "\0" in name or len(name) > 200:
            raise ValueError("tab name must be 1-200 characters without nulls")

        def current_tabs() -> tuple[TabState, ...]:
            if not bound_reads or deadline_check is None:
                return self.list_sessions()
            return self.list_sessions(deadline_check=deadline_check)

        captured = current_tabs() if before is None else before
        before_ids = {tab.id for tab in captured}
        if any(tab.name == name for tab in captured):
            raise WorkerFailure(f"tab correlation name {name!r} is already in use")
        response_id: str | None = None

        def matches() -> tuple[TabState, ...]:
            return tuple(
                tab
                for tab in current_tabs()
                if tab.id not in before_ids
                and tab.name == name
                and tab.panel_type == panel_type
                and (provider is None or tab.provider == provider)
            )

        def dispatch() -> TabState:
            nonlocal response_id
            timeout_seconds = (
                min(self.command_timeout_seconds, deadline_check())
                if deadline_check is not None
                else self.command_timeout_seconds
            )
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
                timeout_seconds=timeout_seconds,
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

    def _send_mutation(
        self,
        session_id: str,
        text: str,
        *,
        operation: str,
        deadline_check: Callable[[], float] | None = None,
    ) -> None:
        self._execute_runtime_mutation(
            operation=operation,
            target=f"{self.workspace_id}/{session_id}",
            pre_state={"workspace": self.workspace_id, "tab": session_id},
            dispatch=lambda: self._mutation_json(
                ["tab", "send", "-w", self.workspace_id, session_id, text],
                operation,
                timeout_seconds=(
                    min(self.command_timeout_seconds, deadline_check())
                    if deadline_check is not None
                    else None
                ),
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

    def _mutation_json(
        self,
        args: Sequence[str],
        operation: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return _run_mutation_json(
            self._runner,
            self.executable,
            args,
            operation,
            self.command_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds,
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
        self,
        args: Sequence[str],
        *,
        operation: str,
        read_only: bool,
        deadline_check: Callable[[], float] | None = None,
    ) -> dict[str, Any]:
        completed = self._run(
            args,
            operation=operation,
            read_only=read_only,
            deadline_check=deadline_check,
        )
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
        self,
        args: Sequence[str],
        *,
        operation: str,
        read_only: bool,
        deadline_check: Callable[[], float] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.executable, *args]
        attempts = self.read_timeout_retries + 1 if read_only else 1
        for attempt in range(attempts):
            timeout_seconds = (
                min(self.command_timeout_seconds, deadline_check())
                if deadline_check is not None
                else self.command_timeout_seconds
            )
            try:
                completed = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                if attempt + 1 < attempts:
                    continue
                if read_only:
                    raise WorkerFailure(
                        f"PurpleMux {operation} timed out after {timeout_seconds}s"
                    ) from exc
                raise MutationOutcomeUnknown(
                    f"PurpleMux {operation} timed out after "
                    f"{timeout_seconds}s; remote outcome is unknown"
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
