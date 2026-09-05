from __future__ import annotations

import codecs
import json
import logging
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Literal, Protocol, cast

from purplemux_client.client import (
    WORKFLOW_HOST_WORKSPACE_ENV,
    CreateWorkspaceRequest,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    ShellCommandRequest,
    WorkspaceState,
)
from purplemux_client.correlation import RUN_IDENTITY_ENV
from purplemux_client.errors import MutationOutcomeUnknown
from purplemux_client.notifier import NotificationResult, TerminalState
from purplemux_client.operations import DRY_RUN_BOUNDARY_EXIT_CODE, DRY_RUN_FD_ENV
from purplemux_client.preflight import (
    ValidationIssue,
    ValidationResult,
    WorkflowValidator,
)
from purplemux_client.progress import (
    EVENT_TOKEN_ENV,
    EVENT_URL_ENV,
    MAX_PROGRESS_EVENT_BYTES,
    PROGRESS_FD_ENV,
    RESOURCE_ACK_FD_ENV,
    StepStatus,
)
from purplemux_client.prompt import PromptExecution

RunnerState = Literal[
    "idle",
    "running",
    "success",
    "failed",
    "stopped",
    "validation_failed",
]
ResourceCleanupState = Literal[
    "retained", "cleanup_pending", "cleanup_retryable", "cleaned", "blocked"
]
ResourceCleanupStatus = Literal[
    "retained", "cleaning", "partially_cleaned", "cleaned", "blocked"
]
DEFAULT_MAX_PROGRESS_EVENTS = 200
_MANAGED_OBSERVATION_RETRY_INITIAL_SECONDS = 0.25
_MANAGED_OBSERVATION_RETRY_MAX_SECONDS = 5.0
logger = logging.getLogger(__name__)
_SHELL_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class _ResourceOwnershipEvent:
    phase: Literal["pending", "verified"]
    token: str
    resource: RunResource


def _resource_cleanup_status(
    resources: Sequence[RunResource],
) -> ResourceCleanupStatus:
    if not resources or all(item.cleanup_state == "cleaned" for item in resources):
        return "cleaned"
    if any(item.cleanup_state == "cleanup_pending" for item in resources):
        return "cleaning"
    if any(
        item.cleanup_state in ("cleanup_retryable", "blocked") for item in resources
    ):
        return "blocked"
    if any(item.cleanup_state == "cleaned" for item in resources):
        return "partially_cleaned"
    return "retained"


def _path_identity(path: Path) -> str:
    try:
        state = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise OSError(f"could not inspect path identity for {path}: {exc}") from exc
    return f"{state.st_dev}:{state.st_ino}"


def _administrative_identity(path: Path) -> str:
    try:
        state = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise OSError(
            f"could not inspect administrative identity for {path}: {exc}"
        ) from exc
    return f"{state.st_dev}:{state.st_ino}:{state.st_ctime_ns}"


class TerminalNotifier(Protocol):
    def notify_terminal(
        self, *, run_id: int, state: TerminalState, exit_code: int | None
    ) -> NotificationResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ProgressEvent:
    name: str
    status: StepStatus
    iteration: int | None = None
    attempt: int | None = None
    message: str | None = None
    error: str | None = None
    workspace: str | None = None
    tab: str | None = None
    observed_at: str | None = None


@dataclass(frozen=True)
class TopologyFinding:
    category: Literal["runtime", "git", "github"]
    status: Literal["passed", "failed", "info"]
    message: str
    observed_at: str | None = None


@dataclass(frozen=True)
class DryRunResult:
    status: Literal["frontier", "complete", "failed", "ineligible"]
    stdout: str = ""
    stderr: str = ""
    findings: tuple[TopologyFinding, ...] = ()
    next_mutation: Mapping[str, object] | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "status": self.status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "findings": [_finding_json(item) for item in self.findings],
            "nextMutation": dict(self.next_mutation) if self.next_mutation else None,
        }


class AlreadyRunningError(RuntimeError):
    """Raised when a run is requested while another process is active."""


class RunnerClosedError(AlreadyRunningError):
    """Raised when a run is requested after Runner shutdown begins."""


class WorkflowValidationError(RuntimeError):
    """Raised when workflow preflight fails before a process is started."""

    def __init__(self, result: ValidationResult) -> None:
        super().__init__("workflow validation failed")
        self.result = result


class WorkflowDryRunError(RuntimeError):
    def __init__(self, result: ValidationResult) -> None:
        super().__init__("workflow is not eligible for Dry Run")
        self.result = result


class InvalidExecutionContextError(ValueError):
    """Raised when a requested run execution context cannot be used."""


class RunNotFoundError(LookupError):
    """Raised when a requested run identifier does not exist."""


class RunCleanupNotAllowedError(RuntimeError):
    """Raised when explicit cleanup is unsafe for the selected run."""


class RunCleanupInProgressError(RuntimeError):
    """Raised when cleanup is already active for the selected run."""


class RunStopUncertainError(RuntimeError):
    """Raised when PurpleMux cannot prove that a stopped Workflow terminated."""


@dataclass(frozen=True)
class RunResource:
    kind: str
    identity: str
    metadata: dict[str, str]
    cleanup_state: ResourceCleanupState = "retained"
    cleanup_error: str | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "identity": self.identity,
            "metadata": dict(self.metadata),
            "cleanupState": self.cleanup_state,
            "cleanupError": self.cleanup_error,
        }


def _is_verified_repository_context(resource: RunResource) -> bool:
    required = {
        "repository",
        "remote",
        "base_branch",
        "base_ref",
        "base_sha",
        "path_identity",
        "git_file_identity",
        "git_dir",
    }
    return (
        resource.kind == "git_worktree"
        and resource.metadata.get("registration_state") != "pending"
        and required.issubset(resource.metadata)
    )


@dataclass(frozen=True)
class RunAttempt:
    number: int
    state: Literal["success", "failed", "stopped"]
    exit_code: int


@dataclass(frozen=True)
class OutputEntry:
    observed_at: str
    text: str


def _progress_json(event: ProgressEvent) -> dict[str, object]:
    payload = asdict(event)
    observed_at = payload.pop("observed_at")
    if observed_at is not None:
        payload["observedAt"] = observed_at
    return payload


def _finding_json(finding: TopologyFinding) -> dict[str, object]:
    payload = asdict(finding)
    observed_at = payload.pop("observed_at")
    if observed_at is not None:
        payload["observedAt"] = observed_at
    return payload


@dataclass(frozen=True)
class RunnerSnapshot:
    state: RunnerState
    stdout: str
    stderr: str
    stdout_entries: tuple[OutputEntry, ...]
    stderr_entries: tuple[OutputEntry, ...]
    outline: tuple[str, ...]
    exit_code: int | None
    run_id: int | None
    progress: tuple[ProgressEvent, ...]
    validation: tuple[ValidationIssue, ...]
    dry_run_issues: tuple[ValidationIssue, ...]
    cwd: str
    args: tuple[str, ...]
    attempts: tuple[RunAttempt, ...]
    findings: tuple[TopologyFinding, ...]
    dry_run: DryRunResult | None
    resources: tuple[RunResource, ...] = ()
    # Submitted Python source for this run's immutable execution snapshot.
    # Populated only for an actual run (see ``_snapshot_run``); left ``None``
    # for the idle/validation preview, which is not tied to a persisted run.
    # Exposed via ``as_json`` (run detail) but intentionally left out of
    # ``as_summary_json`` (the ``/api/runs`` list) to keep that summary
    # lightweight.
    code: str | None = None
    prompt: PromptExecution | None = None

    def as_json(self) -> dict[str, object]:
        payload = asdict(self)
        prompt = payload.pop("prompt")
        payload["mode"] = "prompt" if prompt is not None else "workflow"
        if prompt is not None:
            payload["prompt"] = prompt
        payload["exitCode"] = payload.pop("exit_code")
        payload["runId"] = payload.pop("run_id")
        payload["stdoutEntries"] = [
            {"observedAt": entry.observed_at, "text": entry.text}
            for entry in self.stdout_entries
        ]
        payload["stderrEntries"] = [
            {"observedAt": entry.observed_at, "text": entry.text}
            for entry in self.stderr_entries
        ]
        payload.pop("stdout_entries")
        payload.pop("stderr_entries")
        payload["progress"] = [_progress_json(event) for event in self.progress]
        payload["findings"] = [_finding_json(item) for item in self.findings]
        payload["dryRun"] = self.dry_run.as_json() if self.dry_run else None
        payload.pop("dry_run")
        payload["validation"] = [issue.as_json() for issue in self.validation]
        payload["dryRunEligible"] = not self.dry_run_issues
        payload["dryRunIssues"] = [issue.as_json() for issue in self.dry_run_issues]
        payload.pop("dry_run_issues")
        payload["attempts"] = [
            {
                "number": attempt.number,
                "state": attempt.state,
                "exitCode": attempt.exit_code,
            }
            for attempt in self.attempts
        ]
        payload["resources"] = [resource.as_json() for resource in self.resources]
        payload["executionContext"] = self._execution_context_json()
        payload["resourceCleanupStatus"] = _resource_cleanup_status(self.resources)
        payload["cleanupAvailable"] = self.prompt is None and self.state not in (
            "idle",
            "running",
            "validation_failed",
        )
        return payload

    def as_summary_json(self) -> dict[str, object]:
        execution_context = self._execution_context_json()
        payload: dict[str, object] = {
            "mode": "prompt" if self.prompt is not None else "workflow",
            "state": self.state,
            "exitCode": self.exit_code,
            "runId": self.run_id,
            "cwd": self.cwd,
            "executionContext": execution_context,
            "args": list(self.args),
            "attempts": len(self.attempts),
            "resourceCleanupStatus": _resource_cleanup_status(self.resources),
            "resourceCount": len(self.resources),
        }
        if self.prompt is not None:
            payload["prompt"] = {
                "agent": self.prompt.agent,
                "cwd": self.prompt.cwd,
            }
        return payload

    def _execution_context_json(self) -> dict[str, str] | None:
        for resource in self.resources:
            if not _is_verified_repository_context(resource):
                continue
            metadata = resource.metadata
            return {
                "sourceRepository": metadata.get(
                    "source_repository", metadata.get("repository", "")
                ),
                "remote": metadata.get("remote", ""),
                "baseBranch": metadata.get("base_branch", ""),
                "baseRef": metadata.get("base_ref", ""),
                "baseSha": metadata.get("base_sha", metadata.get("head", "")),
                "executionRoot": resource.identity,
            }
        return None


@dataclass
class _RunRecord:
    run_id: int
    cwd: str
    args: tuple[str, ...]
    process: subprocess.Popen[bytes] | None
    process_group_id: int | None
    script_path: Path
    code: str
    outline: tuple[str, ...]
    state: RunnerState = "running"
    stdout: deque[OutputEntry] = field(default_factory=deque)
    stderr: deque[OutputEntry] = field(default_factory=deque)
    stdout_chars: int = 0
    stderr_chars: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    exit_code: int | None = None
    stop_requested: bool = False
    progress: deque[ProgressEvent] = field(default_factory=deque)
    findings: deque[TopologyFinding] = field(default_factory=deque)
    cleanup_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    attempts: list[RunAttempt] = field(default_factory=list)
    resources: list[RunResource] = field(default_factory=list)
    prompt: PromptExecution | None = None
    managed_client: PurpleMuxCLIClient | None = None
    managed_tab_id: str | None = None
    managed_tab_name: str | None = None
    credential_path: Path | None = None
    event_token: str | None = None


class PythonRunner:
    """Run and observe trusted local Python programs independently."""

    def __init__(
        self,
        *,
        stop_timeout: float = 3.0,
        max_output_chars: int = 1_000_000,
        max_progress_events: int = DEFAULT_MAX_PROGRESS_EVENTS,
        dry_run_timeout: float = 300.0,
        notifier: TerminalNotifier | None = None,
        validator: WorkflowValidator | None = None,
        workflow_cwd: str | os.PathLike[str] | None = None,
        runtime_factory: Callable[[], PurpleMuxRuntime] = PurpleMuxRuntime,
        managed_workflows: bool = True,
    ) -> None:
        if os.name != "posix":
            raise RuntimeError("PythonRunner requires a POSIX operating system")
        if max_output_chars < 1:
            raise ValueError("max_output_chars must be positive")
        if max_progress_events < 1:
            raise ValueError("max_progress_events must be positive")
        if dry_run_timeout <= 0:
            raise ValueError("dry_run_timeout must be positive")
        self._stop_timeout = stop_timeout
        self._max_output_chars = max_output_chars
        self._max_progress_events = max_progress_events
        self._dry_run_timeout = dry_run_timeout
        self._lock = threading.Lock()
        self._changes = threading.Condition(self._lock)
        self._change_revision = 0
        self._validation_lock = threading.Lock()
        self._runs: dict[int, _RunRecord] = {}
        self._next_run_id = 1
        self._correlation_instance = secrets.token_hex(16)
        self._notifier = notifier
        self._runtime_factory = runtime_factory
        # The local process host remains only as an explicit deterministic test
        # harness. Production Workflow runs use the PurpleMux host.
        self.managed_workflows = managed_workflows
        self._event_base_url: str | None = None
        self._wait_threads: set[threading.Thread] = set()
        self._closed = False
        try:
            self._workflow_cwd = (
                Path.cwd()
                if workflow_cwd is None
                else Path(workflow_cwd).expanduser().resolve()
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise InvalidExecutionContextError(
                f"Runner working directory could not be resolved: {exc}"
            ) from exc
        if not self._workflow_cwd.is_dir():
            raise InvalidExecutionContextError(
                f"Runner working directory is not a directory: {self._workflow_cwd}"
            )
        self._preview = RunnerSnapshot(
            state="idle",
            stdout="",
            stderr="",
            stdout_entries=(),
            stderr_entries=(),
            outline=(),
            exit_code=None,
            run_id=None,
            progress=(),
            validation=(),
            dry_run_issues=(),
            cwd=str(self._workflow_cwd),
            args=(),
            attempts=(),
            findings=(),
            dry_run=None,
        )
        self._validator = validator or WorkflowValidator()

    def configure_event_endpoint(self, base_url: str) -> None:
        """Enable PurpleMux-hosted Workflow execution for an attached HTTP server."""
        if not base_url.startswith("http://") or "\0" in base_url:
            raise ValueError("event endpoint must be a local HTTP URL")
        with self._lock:
            if self._runs:
                raise RuntimeError(
                    "event endpoint must be configured before runs start"
                )
            self._event_base_url = base_url.rstrip("/")

    def validate(
        self,
        code: str,
        *,
        args: Sequence[str] = (),
    ) -> ValidationResult:
        run_cwd, run_args, child_env = self._execution_context(args)
        with self._validation_lock:
            with self._lock:
                self._ensure_open()
            result = self._validator.validate(code, cwd=run_cwd, environment=child_env)
            with self._lock:
                self._ensure_open()
                self._apply_validation(result, run_cwd, run_args)
            return result

    def dry_run(
        self,
        code: str,
        *,
        args: Sequence[str] = (),
    ) -> DryRunResult:
        """Execute the real workflow until its first inspection-aware mutation."""
        run_cwd, run_args, child_env = self._execution_context(args)
        with self._validation_lock:
            with self._lock:
                self._ensure_open()
            validation = self._validator.validate(
                code, cwd=run_cwd, environment=child_env
            )
            if not validation.valid or validation.dry_run_issues:
                with self._lock:
                    self._apply_validation(validation, run_cwd, run_args)
                raise WorkflowDryRunError(validation)
            result = self._execute_dry_run(
                code, run_cwd=run_cwd, run_args=run_args, child_env=child_env
            )
            with self._lock:
                self._ensure_open()
                self._preview = RunnerSnapshot(
                    # A runtime Dry Run failure does not invalidate the separate
                    # side-effect-free Static Validation result.
                    state="idle",
                    stdout=result.stdout,
                    stderr=result.stderr,
                    stdout_entries=(),
                    stderr_entries=(),
                    outline=validation.outline,
                    exit_code=None,
                    run_id=None,
                    progress=(),
                    validation=validation.issues,
                    dry_run_issues=validation.dry_run_issues,
                    cwd=str(run_cwd),
                    args=run_args,
                    attempts=(),
                    findings=result.findings,
                    dry_run=result,
                )
                self._mark_changed()
            return result

    def _execute_dry_run(
        self,
        code: str,
        *,
        run_cwd: Path,
        run_args: tuple[str, ...],
        child_env: Mapping[str, str],
    ) -> DryRunResult:
        script = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", encoding="utf-8", delete=False
        )
        try:
            script.write(code)
        finally:
            script.close()
        script_path = Path(script.name)
        boundary_read, boundary_write = os.pipe()
        progress_read, progress_write = os.pipe()
        process_env = dict(child_env)
        process_env[DRY_RUN_FD_ENV] = str(boundary_write)
        process_env[PROGRESS_FD_ENV] = str(progress_write)
        try:
            process = subprocess.Popen(
                [sys.executable, str(script_path), *run_args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_env,
                cwd=run_cwd,
                pass_fds=(boundary_write, progress_write),
                shell=False,
                start_new_session=True,
            )
        except BaseException:
            os.close(boundary_read)
            os.close(boundary_write)
            os.close(progress_read)
            os.close(progress_write)
            script_path.unlink(missing_ok=True)
            raise
        os.close(boundary_write)
        os.close(progress_write)
        boundary_chunks: list[bytes] = []
        progress_chunks: list[bytes] = []

        def drain(fd: int, destination: list[bytes]) -> None:
            with os.fdopen(fd, "rb") as stream:
                destination.append(stream.read(1_000_001))

        readers = [
            threading.Thread(target=drain, args=(boundary_read, boundary_chunks)),
            threading.Thread(target=drain, args=(progress_read, progress_chunks)),
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=self._dry_run_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_process_group(process.pid)
            stdout, stderr = process.communicate()
        except BaseException:
            # The inspected program must not outlive an interrupted Dry Run.
            self._kill_process_group(process.pid)
            process.communicate()
            for reader in readers:
                reader.join()
            script_path.unlink(missing_ok=True)
            raise
        for reader in readers:
            reader.join()
        script_path.unlink(missing_ok=True)
        findings: list[TopologyFinding] = []
        for line in b"".join(progress_chunks).splitlines():
            parsed = self._parse_runner_event(line.decode("utf-8", errors="replace"))
            if parsed is not None and parsed[0] == "finding":
                findings.append(cast(TopologyFinding, parsed[1]))
        boundary = b"".join(boundary_chunks)
        if timed_out:
            return DryRunResult(
                "failed",
                stdout.decode(errors="replace"),
                f"Dry Run exceeded {self._dry_run_timeout:g}s",
                tuple(findings),
            )
        if process.returncode == DRY_RUN_BOUNDARY_EXIT_CODE:
            try:
                values = boundary.splitlines()
                if len(values) != 1:
                    raise ValueError
                mutation = json.loads(values[0])
                if not isinstance(mutation, dict) or mutation.get("protocol") != 1:
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                return DryRunResult(
                    "failed",
                    stdout.decode(errors="replace"),
                    "Dry Run boundary output was missing or malformed",
                    tuple(findings),
                )
            return DryRunResult(
                "frontier",
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
                tuple(findings),
                cast(dict[str, object], mutation),
            )
        if process.returncode == 0 and not boundary:
            return DryRunResult(
                "complete",
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
                tuple(findings),
            )
        detail = stderr.decode(errors="replace")
        if boundary:
            detail += "\nDry Run emitted an unexpected mutation boundary"
        return DryRunResult(
            "failed", stdout.decode(errors="replace"), detail, tuple(findings)
        )

    def start(
        self,
        code: str,
        *,
        args: Sequence[str] = (),
        prompt: PromptExecution | None = None,
    ) -> int:
        run_cwd, run_args, child_env = self._execution_context(args)
        with self._validation_lock:
            with self._lock:
                self._ensure_open()
            validation = self._validator.validate(
                code, cwd=run_cwd, environment=child_env
            )
            with self._lock:
                self._ensure_open()
                if not validation.valid:
                    self._apply_validation(validation, run_cwd, run_args)
                    raise WorkflowValidationError(validation)
                return self._start_validated(
                    code,
                    outline=validation.outline,
                    run_cwd=run_cwd,
                    run_args=run_args,
                    child_env=child_env,
                    prompt=prompt,
                )

    def _start_validated(
        self,
        code: str,
        *,
        outline: tuple[str, ...],
        run_cwd: Path,
        run_args: tuple[str, ...],
        child_env: Mapping[str, str],
        prompt: PromptExecution | None = None,
    ) -> int:
        run_id = self._next_run_id
        self._next_run_id += 1
        if prompt is None and self.managed_workflows:
            if self._event_base_url is None:
                raise RuntimeError(
                    "managed Workflow execution requires an attached event endpoint"
                )
            return self._start_managed_workflow(
                run_id,
                code,
                outline=outline,
                run_cwd=run_cwd,
                run_args=run_args,
                child_env=child_env,
            )
        run_environment = dict(child_env)
        run_environment[RUN_IDENTITY_ENV] = self._run_identity(run_id)
        process, script_path, progress_read_fd, resource_ack_fd = self._spawn_process(
            code,
            run_cwd=run_cwd,
            run_args=run_args,
            child_env=run_environment,
        )

        run = _RunRecord(
            run_id=run_id,
            cwd=prompt.cwd if prompt is not None else str(run_cwd),
            args=run_args,
            process=process,
            process_group_id=process.pid,
            script_path=script_path,
            code=code,
            outline=outline,
            progress=deque(maxlen=self._max_progress_events),
            findings=deque(maxlen=self._max_progress_events),
            prompt=prompt,
        )
        self._runs[run_id] = run

        self._start_attempt_threads(
            run, process, script_path, progress_read_fd, resource_ack_fd
        )
        self._mark_changed()
        return run_id

    def _start_managed_workflow(
        self,
        run_id: int,
        code: str,
        *,
        outline: tuple[str, ...],
        run_cwd: Path,
        run_args: tuple[str, ...],
        child_env: Mapping[str, str],
    ) -> int:
        script = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", encoding="utf-8", delete=False
        )
        try:
            script.write(code)
        finally:
            script.close()
        script_path = Path(script.name)
        event_token = secrets.token_urlsafe(32)
        credential = tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", encoding="utf-8", delete=False
        )
        event_url = f"{self._event_base_url}/api/runs/{run_id}/events"
        managed_env = dict(child_env)
        managed_env.pop(PROGRESS_FD_ENV, None)
        managed_env.pop(RESOURCE_ACK_FD_ENV, None)
        managed_env.pop(WORKFLOW_HOST_WORKSPACE_ENV, None)
        managed_env[EVENT_URL_ENV] = event_url
        managed_env[EVENT_TOKEN_ENV] = event_token
        managed_env[RUN_IDENTITY_ENV] = self._run_identity(run_id)
        try:
            for name, value in sorted(managed_env.items()):
                if _SHELL_ENV_NAME.fullmatch(name):
                    credential.write(f"export {name}={shlex.quote(value)}\n")
            credential.write(f"unset {PROGRESS_FD_ENV} {RESOURCE_ACK_FD_ENV}\n")
        finally:
            credential.close()
        credential_path = Path(credential.name)
        credential_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        run = _RunRecord(
            run_id=run_id,
            cwd=str(run_cwd),
            args=run_args,
            process=None,
            process_group_id=None,
            script_path=script_path,
            code=code,
            outline=outline,
            progress=deque(maxlen=self._max_progress_events),
            findings=deque(maxlen=self._max_progress_events),
            credential_path=credential_path,
            event_token=event_token,
        )
        self._runs[run_id] = run
        correlation = self._run_identity(run_id)
        client: PurpleMuxCLIClient | None = None
        workspace_id: str | None = None
        created_tab_id: str | None = None
        result_path: str | None = None
        try:
            runtime = self._runtime_factory()
            workspace = runtime.create_workspace(
                CreateWorkspaceRequest(
                    cwd=str(run_cwd),
                    name=f"Workflow {run_id}",
                    correlation_id=correlation,
                )
            )
            workspace_id = workspace.id
            self._register_managed_workspace(run, workspace, correlation)
            with credential_path.open("a", encoding="utf-8") as credential_stream:
                credential_stream.write(
                    f"export {WORKFLOW_HOST_WORKSPACE_ENV}="
                    f"{shlex.quote(workspace_id)}\n"
                )
            client = runtime.workspace(workspace.id)

            def created(tab_id: str, path: str) -> None:
                nonlocal created_tab_id, result_path
                created_tab_id = tab_id
                result_path = path

            command = (
                f"source {shlex.quote(str(credential_path))} && exec "
                f"{shlex.join([sys.executable, str(script_path), *run_args])}"
            )
            tab_name = f"Workflow {run_id}: Python"
            created_tab_id = client.start_shell(
                ShellCommandRequest(
                    command=command,
                    cwd=str(run_cwd),
                    name=tab_name,
                    correlation_id=correlation,
                ),
                on_created=created,
            )
            self._attach_managed_shell(
                run,
                client,
                workspace_id,
                created_tab_id,
                result_path,
                f"{tab_name} [awm:{correlation}]",
            )
        except MutationOutcomeUnknown as exc:
            if (
                client is None
                or workspace_id is None
                or created_tab_id is None
                or result_path is None
            ):
                self._fail_managed_launch(run, exc)
                return run_id
            self._attach_managed_shell(
                run,
                client,
                workspace_id,
                created_tab_id,
                result_path,
                f"Workflow {run_id}: Python [awm:{correlation}]",
            )
        except BaseException as exc:
            if (
                client is not None
                and workspace_id is not None
                and created_tab_id is not None
                and result_path is not None
            ):
                self._attach_managed_shell(
                    run,
                    client,
                    workspace_id,
                    created_tab_id,
                    result_path,
                    f"Workflow {run_id}: Python [awm:{correlation}]",
                )
            self._fail_managed_launch(run, exc)
            return run_id
        wait_thread = threading.Thread(
            target=self._wait_for_managed_workflow,
            args=(run,),
            name=f"python-runner-managed-wait-{run_id}",
            daemon=True,
        )
        self._wait_threads.add(wait_thread)
        wait_thread.start()
        self._mark_changed()
        return run_id

    def _fail_managed_launch(self, run: _RunRecord, exc: BaseException) -> None:
        run.script_path.unlink(missing_ok=True)
        if run.credential_path is not None:
            run.credential_path.unlink(missing_ok=True)
        run.exit_code = 1
        run.state = "failed"
        self._append_output(
            run, "stderr", f"Workflow launch failed: {exc}\n", lock_held=True
        )
        run.attempts.append(RunAttempt(1, "failed", 1))
        self._mark_changed()

    def _attach_managed_shell(
        self,
        run: _RunRecord,
        client: PurpleMuxCLIClient,
        workspace_id: str,
        tab_id: str,
        result_path: str | None,
        tab_name: str,
    ) -> None:
        if result_path is None:
            raise RuntimeError("managed shell did not identify its result path")
        run.managed_client = client
        run.managed_tab_id = tab_id
        run.managed_tab_name = tab_name
        self._register_resource(
            run,
            RunResource(
                "purplemux_tab",
                tab_id,
                {
                    "workspace_id": workspace_id,
                    "name": tab_name,
                    "panel_type": "terminal",
                    "provider": "",
                },
            ),
        )
        result_dir = Path(result_path).parent
        self._register_resource(
            run,
            RunResource(
                "managed_shell_result",
                str(result_dir),
                {
                    "result_path": result_path,
                    "tab_id": tab_id,
                    "directory_identity": _path_identity(result_dir),
                },
            ),
        )

    def _register_managed_workspace(
        self, run: _RunRecord, workspace: WorkspaceState, correlation: str
    ) -> None:
        self._register_resource(
            run,
            RunResource(
                "purplemux_workspace",
                workspace.id,
                {
                    "name": workspace.name,
                    "directories": "\n".join(workspace.directories),
                    "correlation_id": correlation,
                },
            ),
        )

    def _run_identity(self, run_id: int) -> str:
        return f"{self._correlation_instance}-{run_id}"

    @staticmethod
    def _spawn_process(
        code: str,
        *,
        run_cwd: Path,
        run_args: tuple[str, ...],
        child_env: Mapping[str, str],
    ) -> tuple[subprocess.Popen[bytes], Path, int, int]:
        script = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", encoding="utf-8", delete=False
        )
        try:
            script.write(code)
        finally:
            script.close()
        script_path = Path(script.name)
        progress_read_fd, progress_write_fd = os.pipe()
        resource_ack_read_fd, resource_ack_write_fd = os.pipe()
        process_env = dict(child_env)
        process_env[PROGRESS_FD_ENV] = str(progress_write_fd)
        process_env[RESOURCE_ACK_FD_ENV] = str(resource_ack_read_fd)
        try:
            process = subprocess.Popen(
                [sys.executable, str(script_path), *run_args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_env,
                cwd=run_cwd,
                pass_fds=(progress_write_fd, resource_ack_read_fd),
                shell=False,
                start_new_session=True,
            )
        except BaseException:
            os.close(progress_read_fd)
            os.close(progress_write_fd)
            os.close(resource_ack_read_fd)
            os.close(resource_ack_write_fd)
            script_path.unlink(missing_ok=True)
            raise
        os.close(progress_write_fd)
        os.close(resource_ack_read_fd)
        return process, script_path, progress_read_fd, resource_ack_write_fd

    def _start_attempt_threads(
        self,
        run: _RunRecord,
        process: subprocess.Popen[bytes],
        script_path: Path,
        progress_read_fd: int,
        resource_ack_fd: int,
    ) -> None:
        stdout_thread = threading.Thread(
            target=self._read_stream,
            args=(run, process.stdout, "stdout"),
            name=f"python-runner-stdout-{run.run_id}",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stream,
            args=(run, process.stderr, "stderr"),
            name=f"python-runner-stderr-{run.run_id}",
            daemon=True,
        )
        progress_thread = threading.Thread(
            target=self._read_progress,
            args=(run, progress_read_fd, resource_ack_fd),
            name=f"python-runner-progress-{run.run_id}",
            daemon=True,
        )
        wait_thread = threading.Thread(
            target=self._wait_for_process,
            args=(
                run,
                process,
                script_path,
                stdout_thread,
                stderr_thread,
                progress_thread,
            ),
            name=f"python-runner-wait-{run.run_id}",
            daemon=True,
        )
        self._wait_threads.add(wait_thread)
        stdout_thread.start()
        stderr_thread.start()
        progress_thread.start()
        wait_thread.start()

    def _apply_validation(
        self, result: ValidationResult, run_cwd: Path, run_args: tuple[str, ...]
    ) -> None:
        self._preview = RunnerSnapshot(
            state="idle" if result.valid else "validation_failed",
            stdout="",
            stderr="",
            stdout_entries=(),
            stderr_entries=(),
            outline=result.outline,
            exit_code=None,
            run_id=None,
            progress=(),
            validation=result.issues,
            dry_run_issues=result.dry_run_issues,
            cwd=str(run_cwd),
            args=run_args,
            attempts=(),
            findings=(),
            dry_run=None,
        )
        self._mark_changed()

    def change_revision(self) -> int:
        """Return the current observation revision without exposing runner state."""
        with self._lock:
            return self._change_revision

    def wait_for_change(self, after: int, timeout: float) -> int | None:
        """Wait for observable state to change, returning None after shutdown."""
        with self._changes:
            self._changes.wait_for(
                lambda: self._change_revision > after or self._closed,
                timeout=timeout,
            )
            if self._closed:
                return None
            return self._change_revision

    def _mark_changed(self) -> None:
        """Advance the observation revision while ``self._lock`` is held."""
        self._change_revision += 1
        self._changes.notify_all()

    def snapshot(self, run_id: int | None = None) -> RunnerSnapshot:
        with self._lock:
            if run_id is None:
                if not self._runs:
                    return self._preview
                run_id = next(reversed(self._runs))
            return self._snapshot_run(self._get_run(run_id))

    def snapshots(self) -> tuple[RunnerSnapshot, ...]:
        with self._lock:
            return tuple(self._snapshot_run(run) for run in self._runs.values())

    def validation_snapshot(self) -> RunnerSnapshot:
        with self._lock:
            return self._preview

    def _snapshot_run(self, run: _RunRecord) -> RunnerSnapshot:
        stdout_entries = self._render_output_entries(run.stdout, run.stdout_truncated)
        stderr_entries = self._render_output_entries(run.stderr, run.stderr_truncated)
        return RunnerSnapshot(
            state=run.state,
            stdout="".join(entry.text for entry in stdout_entries),
            stderr="".join(entry.text for entry in stderr_entries),
            stdout_entries=stdout_entries,
            stderr_entries=stderr_entries,
            outline=run.outline,
            exit_code=run.exit_code,
            run_id=run.run_id,
            progress=tuple(run.progress),
            validation=(),
            dry_run_issues=(),
            cwd=run.cwd,
            args=run.args,
            attempts=tuple(run.attempts),
            findings=tuple(run.findings),
            dry_run=None,
            code=None if run.prompt is not None else run.code,
            resources=tuple(run.resources),
            prompt=run.prompt,
        )

    def _get_run(self, run_id: int) -> _RunRecord:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"run {run_id} was not found") from exc

    def _execution_context(
        self,
        args: Sequence[str],
    ) -> tuple[Path, tuple[str, ...], dict[str, str]]:
        run_cwd = self._workflow_cwd
        if not run_cwd.is_dir():
            raise InvalidExecutionContextError(
                f"Runner working directory is not a directory: {run_cwd}"
            )
        if isinstance(args, (str, bytes)):
            raise InvalidExecutionContextError("args must be a sequence of strings")
        run_args = tuple(args)
        if any(not isinstance(argument, str) for argument in run_args):
            raise InvalidExecutionContextError("args must contain only strings")
        if any("\0" in argument for argument in run_args):
            raise InvalidExecutionContextError("args must not contain null bytes")

        child_env = os.environ.copy()
        # An obsolete value inherited from a pre-0.2.1 Runner must never cause
        # an old workflow checkpoint to be replayed by a newly started run.
        child_env.pop("PURPLEMUX_RUNNER_RESUME_CHECKPOINT", None)
        child_env.pop("PURPLEMUX_RUNNER_REPOSITORY_CONTEXT", None)
        child_env.pop("PURPLEMUX_RUNNER_PENDING_REPOSITORY_CONTEXT", None)
        child_env.pop(WORKFLOW_HOST_WORKSPACE_ENV, None)
        return run_cwd, run_args, child_env

    def _ensure_open(self) -> None:
        if self._closed:
            raise RunnerClosedError("the Python Runner is closed")

    def stop(self, run_id: int | None = None) -> bool:
        with self._lock:
            if run_id is None:
                if not self._runs:
                    return False
                run = self._runs[next(reversed(self._runs))]
            else:
                run = self._get_run(run_id)
            if run.state != "running" or (
                run.process is not None and run.process.poll() is not None
            ):
                return False
            run.stop_requested = True
            managed_client = run.managed_client
            managed_tab_id = run.managed_tab_id

        if managed_client is not None and managed_tab_id is not None:
            lifecycle_error: BaseException | None = None
            try:
                managed_client.interrupt(managed_tab_id)
                managed_client.wait_for_shell_completion(
                    managed_tab_id, self._stop_timeout
                )
                result = managed_client.read_shell_result(managed_tab_id)
                self._finish_managed_workflow(run, result.exit_code)
                return True
            except Exception as exc:
                lifecycle_error = exc
                logger.warning(
                    "Workflow tab %s did not publish a result after interrupt; "
                    "closing it: %s",
                    managed_tab_id,
                    exc,
                )
                try:
                    managed_client.close_session(managed_tab_id)
                except Exception as close_exc:
                    message = (
                        f"Workflow termination is uncertain after interrupt/result "
                        f"failure ({lifecycle_error}) and tab-close failure "
                        f"({close_exc})"
                    )
                    self._record_managed_uncertainty(run, message)
                    raise RunStopUncertainError(message) from close_exc
                self._finish_managed_workflow(
                    run, 130, diagnostic=f"Workflow tab closed after: {exc}"
                )
            return True
        self._terminate_process_group(run)
        return True

    def cleanup(self, run_id: int) -> RunnerSnapshot:
        """Explicitly release resources owned by one completed Workflow run."""
        with self._lock:
            self._ensure_open()
            run = self._get_run(run_id)
            cleanup_lock = run.cleanup_lock
        if not cleanup_lock.acquire(blocking=False):
            raise RunCleanupInProgressError(f"run {run_id} cleanup is already active")
        try:
            with self._lock:
                self._ensure_open()
                run = self._get_run(run_id)
                if run.prompt is not None:
                    raise RunCleanupNotAllowedError(
                        f"run {run_id} is a Prompt run; Workflow cleanup does not apply"
                    )
                if run.state in ("idle", "running", "validation_failed"):
                    raise RunCleanupNotAllowedError(
                        f"run {run_id} is {run.state}; cleanup requires a non-running run"
                    )
                ordered = sorted(
                    enumerate(run.resources),
                    key=lambda item: (
                        self._resource_cleanup_priority(item[1]),
                        -item[0] if item[1].kind == "purplemux_tab" else item[0],
                    ),
                )

            failed_priority: int | None = None
            for index, original in ordered:
                priority = self._resource_cleanup_priority(original)
                if failed_priority is not None and priority > failed_priority:
                    break
                with self._lock:
                    current = run.resources[index]
                    if current.cleanup_state == "cleaned":
                        continue
                if current.cleanup_state in ("cleanup_pending", "blocked"):
                    try:
                        absent = self._resource_is_absent(current)
                    except Exception:
                        absent = False
                    if absent:
                        with self._lock:
                            run.resources[index] = RunResource(
                                current.kind,
                                current.identity,
                                current.metadata,
                                "cleaned",
                            )
                            self._mark_changed()
                    else:
                        failed_priority = priority
                    continue
                with self._lock:
                    pending = RunResource(
                        current.kind,
                        current.identity,
                        current.metadata,
                        "cleanup_pending",
                    )
                    run.resources[index] = pending
                    self._mark_changed()
                try:
                    self._cleanup_resource(pending)
                except Exception as exc:
                    cleanup_state: ResourceCleanupState = (
                        "blocked"
                        if isinstance(exc, MutationOutcomeUnknown)
                        else "cleanup_retryable"
                    )
                    with self._lock:
                        run.resources[index] = RunResource(
                            pending.kind,
                            pending.identity,
                            pending.metadata,
                            cleanup_state,
                            str(exc),
                        )
                        self._mark_changed()
                    failed_priority = priority
                else:
                    with self._lock:
                        run.resources[index] = RunResource(
                            pending.kind,
                            pending.identity,
                            pending.metadata,
                            "cleaned",
                        )
                        self._mark_changed()
            with self._lock:
                return self._snapshot_run(run)
        finally:
            cleanup_lock.release()

    @staticmethod
    def _resource_cleanup_priority(resource: RunResource) -> int:
        return {
            "purplemux_tab": 0,
            "managed_shell_result": 1,
            "purplemux_workspace": 2,
            "git_worktree": 3,
        }.get(resource.kind, 3)

    def _cleanup_resource(self, resource: RunResource) -> None:
        if resource.kind == "purplemux_tab":
            workspace_id = resource.metadata.get("workspace_id")
            if not workspace_id:
                raise OSError("PurpleMux tab ownership lacks workspace_id")
            client = PurpleMuxCLIClient(workspace_id)
            tabs = client.list_sessions()
            selected = next((tab for tab in tabs if tab.id == resource.identity), None)
            if selected is None:
                return
            expected_name = resource.metadata.get("name")
            expected_panel = resource.metadata.get("panel_type")
            if expected_name is not None and selected.name != expected_name:
                raise OSError("PurpleMux tab name changed; refusing cleanup")
            if (
                expected_panel is not None
                and (selected.panel_type or "") != expected_panel
            ):
                raise OSError("PurpleMux tab type changed; refusing cleanup")
            expected_provider = resource.metadata.get("provider")
            if (
                expected_provider is not None
                and (selected.provider or "") != expected_provider
            ):
                raise OSError("PurpleMux tab provider changed; refusing cleanup")
            client.close_session(resource.identity, expected_state=selected)
            return
        if resource.kind == "managed_shell_result":
            self._cleanup_managed_shell_result(resource)
            return
        if resource.kind == "purplemux_workspace":
            runtime = PurpleMuxRuntime()
            selected = next(
                (
                    workspace
                    for workspace in runtime.list_workspaces()
                    if workspace.id == resource.identity
                ),
                None,
            )
            if selected is None:
                return
            expected_name = resource.metadata.get("name")
            if expected_name is not None and selected.name != expected_name:
                raise OSError("PurpleMux workspace name changed; refusing cleanup")
            expected_directories = resource.metadata.get("directories")
            if expected_directories is not None and selected.directories != tuple(
                expected_directories.splitlines()
            ):
                raise OSError(
                    "PurpleMux workspace directories changed; refusing cleanup"
                )
            if PurpleMuxCLIClient(resource.identity).list_sessions():
                raise OSError(
                    "PurpleMux workspace contains unregistered tabs; refusing cleanup"
                )
            runtime.delete_workspace(resource.identity, expected_state=selected)
            return
        if resource.kind == "git_worktree":
            self._cleanup_git_worktree(resource)
            return
        raise OSError(f"unsupported run resource kind: {resource.kind}")

    @staticmethod
    def _resource_is_absent(resource: RunResource) -> bool:
        if resource.kind == "purplemux_tab":
            workspace_id = resource.metadata.get("workspace_id")
            if not workspace_id:
                raise OSError("PurpleMux tab ownership lacks workspace_id")
            return all(
                tab.id != resource.identity
                for tab in PurpleMuxCLIClient(workspace_id).list_sessions()
            )
        if resource.kind == "purplemux_workspace":
            return all(
                workspace.id != resource.identity
                for workspace in PurpleMuxRuntime().list_workspaces()
            )
        if resource.kind == "managed_shell_result":
            return not os.path.lexists(resource.identity)
        if resource.kind == "git_worktree":
            repository = resource.metadata.get("repository")
            if repository is None:
                raise OSError("Git worktree ownership lacks repository metadata")
            listed = subprocess.run(
                ["git", "-C", repository, "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if listed.returncode != 0:
                raise OSError(
                    "could not reconcile owned Git worktree: "
                    + (listed.stderr.strip() or "no stderr")
                )
            registered = any(
                line == f"worktree {resource.identity}"
                for line in listed.stdout.splitlines()
            )
            return not registered and not os.path.lexists(resource.identity)
        raise OSError(f"unsupported run resource kind: {resource.kind}")

    @staticmethod
    def _cleanup_managed_shell_result(resource: RunResource) -> None:
        directory = Path(resource.identity)
        temp_root = Path(tempfile.gettempdir()).resolve()
        if (
            not directory.is_absolute()
            or directory.parent != temp_root
            or not directory.name.startswith("awm-shell-")
        ):
            raise OSError("managed shell result directory identity is invalid")
        expected_result = directory / "result.json"
        if resource.metadata.get("result_path") != str(expected_result):
            raise OSError("managed shell result path metadata conflicts")
        expected_identity = resource.metadata.get("directory_identity")
        if expected_identity is None:
            raise OSError(
                "managed shell result directory ownership identity is missing"
            )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            descriptor = os.open(directory, flags)
        except FileNotFoundError:
            if os.path.lexists(directory):
                raise OSError(
                    "managed shell result path identity changed; refusing cleanup"
                )
            return
        except OSError as exc:
            raise OSError(
                "managed shell result directory is not the owned directory; "
                "refusing cleanup"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            opened_identity = f"{opened.st_dev}:{opened.st_ino}"
            if opened_identity != expected_identity:
                raise OSError(
                    "managed shell result directory identity changed; refusing cleanup"
                )
            entries = os.listdir(descriptor)
            owned_entries = ("result.json", "result.json.pending")
            if any(name not in owned_entries for name in entries):
                raise OSError(
                    "managed shell result directory contains unexpected files"
                )
            for entry in owned_entries:
                if entry not in entries:
                    continue
                result_state = os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISREG(result_state.st_mode):
                    raise OSError(
                        "managed shell result entry is not a regular file; "
                        "refusing cleanup"
                    )
                os.unlink(entry, dir_fd=descriptor)
            current = os.stat(directory, follow_symlinks=False)
            current_identity = f"{current.st_dev}:{current.st_ino}"
            if (
                not stat.S_ISDIR(current.st_mode)
                or current_identity != expected_identity
            ):
                raise OSError(
                    "managed shell result directory identity changed; refusing cleanup"
                )
            os.rmdir(directory)
        finally:
            os.close(descriptor)

    @staticmethod
    def _cleanup_git_worktree(resource: RunResource) -> None:
        worktree = Path(resource.identity)
        repository_text = resource.metadata.get("repository")
        if repository_text is None:
            raise OSError("Git worktree ownership lacks repository metadata")
        repository = Path(repository_text)
        if not worktree.is_absolute() or not repository.is_absolute():
            raise OSError("Git worktree cleanup requires absolute paths")

        listed = subprocess.run(
            ["git", "-C", str(repository), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if listed.returncode != 0:
            raise OSError(
                "could not inspect owned Git worktrees: "
                + (listed.stderr.strip() or "no stderr")
            )
        paths = {
            line.removeprefix("worktree ")
            for line in listed.stdout.splitlines()
            if line.startswith("worktree ")
        }
        if str(worktree) not in paths:
            if os.path.lexists(worktree):
                raise OSError(
                    "Git worktree path exists but is not registered; refusing "
                    "partial-state cleanup"
                )
            return
        required_identity = {
            "path_identity",
            "git_file_identity",
            "git_dir",
        }
        missing = sorted(required_identity.difference(resource.metadata))
        if missing:
            if resource.metadata.get("registration_state") != "pending":
                raise OSError(
                    "Git worktree ownership metadata is incomplete: "
                    + ", ".join(missing)
                )
            observed_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            observed_branch = subprocess.run(
                ["git", "-C", str(worktree), "symbolic-ref", "-q", "HEAD"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if (
                observed_head.returncode != 0
                or observed_head.stdout.strip() != resource.metadata.get("base_sha")
                or observed_branch.returncode != 1
            ):
                raise OSError(
                    "pending Git worktree does not match its reserved identity"
                )
            path_identity = _path_identity(worktree)
            git_file_identity = _administrative_identity(worktree / ".git")
        else:
            path_identity = resource.metadata["path_identity"]
            git_file_identity = resource.metadata["git_file_identity"]
        if _path_identity(worktree) != path_identity:
            raise OSError("Git worktree path identity changed; refusing cleanup")
        git_file = worktree / ".git"
        if _administrative_identity(git_file) != git_file_identity:
            raise OSError(
                "Git worktree administrative identity changed; refusing cleanup"
            )
        observed_git_dir = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if observed_git_dir.returncode != 0:
            raise OSError("Git worktree git_dir identity changed; refusing cleanup")
        if (
            not missing
            and observed_git_dir.stdout.strip() != resource.metadata["git_dir"]
        ):
            raise OSError("Git worktree git_dir identity changed; refusing cleanup")
        dirty = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if dirty.returncode != 0:
            raise OSError(
                "could not inspect owned Git worktree: "
                + (dirty.stderr.strip() or "no stderr")
            )
        if dirty.stdout:
            raise OSError("Git worktree has uncommitted changes; refusing cleanup")
        removed = subprocess.run(
            ["git", "-C", str(repository), "worktree", "remove", str(worktree)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if removed.returncode == 0:
            return
        reconciled = subprocess.run(
            ["git", "-C", str(repository), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if reconciled.returncode == 0 and not any(
            line == f"worktree {worktree}" for line in reconciled.stdout.splitlines()
        ):
            return
        raise MutationOutcomeUnknown(
            "Git worktree removal could not be confirmed: "
            + (removed.stderr.strip() or "no stderr")
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._changes.notify_all()
            active_runs = tuple(
                run
                for run in self._runs.values()
                if run.state == "running"
                and (run.process is None or run.process.poll() is None)
            )
            for run in active_runs:
                run.stop_requested = True
        self._validator.close()
        cleanup_threads = tuple(
            threading.Thread(
                target=self.stop,
                args=(run.run_id,),
                name=f"python-runner-cleanup-{run.run_id}",
                daemon=True,
            )
            for run in active_runs
        )
        for thread in cleanup_threads:
            thread.start()
        for thread in cleanup_threads:
            thread.join()
        for run in active_runs:
            if run.process is None:
                continue
            try:
                run.process.wait(timeout=self._stop_timeout + 1)
            except subprocess.TimeoutExpired:
                assert run.process_group_id is not None
                self._kill_process_group(run.process_group_id)
                run.process.wait()
        notifier = self._notifier
        if notifier is not None:
            try:
                notifier.close()
            except Exception:
                logger.warning("Failed to close terminal notifier")
        current_thread = threading.current_thread()
        with self._lock:
            wait_threads = tuple(
                thread for thread in self._wait_threads if thread is not current_thread
            )
        deadline = time.monotonic() + self._stop_timeout + 1
        for thread in wait_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def _read_stream(
        self,
        run: _RunRecord,
        stream: IO[bytes] | None,
        destination: Literal["stdout", "stderr"],
    ) -> None:
        if stream is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while chunk := os.read(stream.fileno(), 4096):
                text = decoder.decode(chunk)
                self._append_output(run, destination, text)
            final_text = decoder.decode(b"", final=True)
            if final_text:
                self._append_output(run, destination, final_text)
        finally:
            stream.close()

    def _append_output(
        self,
        run: _RunRecord,
        destination: Literal["stdout", "stderr"],
        text: str,
        *,
        lock_held: bool = False,
    ) -> None:
        observed_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")

        def append() -> None:
            chunks = run.stdout if destination == "stdout" else run.stderr
            size_attribute = (
                "stdout_chars" if destination == "stdout" else "stderr_chars"
            )
            truncated_attribute = (
                "stdout_truncated" if destination == "stdout" else "stderr_truncated"
            )
            chunks.append(OutputEntry(observed_at=observed_at, text=text))
            size = getattr(run, size_attribute) + len(text)
            was_truncated = size > self._max_output_chars
            while size > self._max_output_chars:
                overflow = size - self._max_output_chars
                first = chunks[0]
                if len(first.text) <= overflow:
                    size -= len(chunks.popleft().text)
                else:
                    chunks[0] = OutputEntry(
                        observed_at=first.observed_at,
                        text=first.text[overflow:],
                    )
                    size -= overflow
            setattr(run, size_attribute, size)
            if was_truncated:
                setattr(run, truncated_attribute, True)
            self._mark_changed()

        if lock_held:
            append()
        else:
            with self._lock:
                append()

    def _read_progress(self, run: _RunRecord, fd: int, resource_ack_fd: int) -> None:
        try:
            with (
                os.fdopen(fd, "rb") as stream,
                os.fdopen(resource_ack_fd, "wb", buffering=0) as acknowledgements,
            ):
                while line := stream.readline(MAX_PROGRESS_EVENT_BYTES + 1):
                    if len(line) > MAX_PROGRESS_EVENT_BYTES or not line.endswith(b"\n"):
                        while line and not line.endswith(b"\n"):
                            line = stream.readline(MAX_PROGRESS_EVENT_BYTES + 1)
                        continue
                    value = self._parse_runner_event(
                        line.decode("utf-8", errors="replace")
                    )
                    if value is not None:
                        with self._lock:
                            accepted = self._accept_parsed_event(run, value)
                            if value[0] == "resource_ownership":
                                ownership = cast(_ResourceOwnershipEvent, value[1])
                                acknowledgements.write(
                                    json.dumps(
                                        {
                                            "token": ownership.token,
                                            "accepted": accepted,
                                        },
                                        separators=(",", ":"),
                                    ).encode("utf-8")
                                    + b"\n"
                                )
                            self._mark_changed()
        except OSError:
            return

    def accept_event(self, run_id: int, token: str, payload: str) -> dict[str, object]:
        """Accept one authenticated, observation-only event for a running run."""
        with self._lock:
            run = self._get_run(run_id)
            if (
                run.state != "running"
                or run.event_token is None
                or not secrets.compare_digest(token, run.event_token)
            ):
                raise PermissionError("run event credential is invalid")
            parsed = self._parse_runner_event(payload)
            if parsed is None:
                raise ValueError("run event is invalid")
            accepted = self._accept_parsed_event(run, parsed)
            self._mark_changed()
            if parsed[0] == "resource_ownership":
                ownership = cast(_ResourceOwnershipEvent, parsed[1])
                return {"token": ownership.token, "accepted": accepted}
            return {"accepted": True}

    @staticmethod
    def _accepted_at() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def _accept_parsed_event(
        self,
        run: _RunRecord,
        parsed: tuple[str, object],
    ) -> bool:
        event_type, event = parsed
        if event_type == "finding":
            finding = cast(TopologyFinding, event)
            run.findings.append(replace(finding, observed_at=self._accepted_at()))
        elif event_type == "resource":
            self._register_resource(run, cast(RunResource, event))
        elif event_type == "resource_ownership":
            return self._register_owned_resource(
                run, cast(_ResourceOwnershipEvent, event)
            )
        else:
            progress = cast(ProgressEvent, event)
            run.progress.append(replace(progress, observed_at=self._accepted_at()))
        return True

    @staticmethod
    def _parse_runner_event(
        line: str,
    ) -> (
        tuple[
            Literal[
                "progress",
                "finding",
                "resource",
                "resource_ownership",
            ],
            object,
        ]
        | None
    ):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        event_type = value.get("type")
        if event_type == "finding":
            category = value.get("category")
            status = value.get("status")
            message = value.get("message")
            if (
                category in ("runtime", "git", "github")
                and status in ("passed", "failed", "info")
                and isinstance(message, str)
                and message.strip()
            ):
                return "finding", TopologyFinding(
                    cast(Literal["runtime", "git", "github"], category),
                    cast(Literal["passed", "failed", "info"], status),
                    message,
                )
            return None
        if event_type == "resource":
            kind = value.get("kind")
            identity = value.get("identity")
            metadata = value.get("metadata")
            if (
                kind
                not in (
                    "purplemux_tab",
                    "managed_shell_result",
                    "purplemux_workspace",
                    "git_worktree",
                )
                or not isinstance(identity, str)
                or not identity
                or not isinstance(metadata, dict)
                or any(
                    not isinstance(key, str) or not key or not isinstance(item, str)
                    for key, item in metadata.items()
                )
            ):
                return None
            return "resource", RunResource(kind, identity, dict(metadata))
        if event_type == "resource_ownership":
            phase = value.get("phase")
            token = value.get("token")
            kind = value.get("kind")
            identity = value.get("identity")
            metadata = value.get("metadata")
            if (
                phase not in ("pending", "verified")
                or not isinstance(token, str)
                or not token
                or kind != "git_worktree"
                or not isinstance(identity, str)
                or not identity
                or not isinstance(metadata, dict)
                or any(
                    not isinstance(key, str) or not key or not isinstance(item, str)
                    for key, item in metadata.items()
                )
            ):
                return None
            return "resource_ownership", _ResourceOwnershipEvent(
                cast(Literal["pending", "verified"], phase),
                token,
                RunResource("git_worktree", identity, dict(metadata)),
            )
        name = value.get("name")
        status = value.get("status")
        if not isinstance(name, str) or not name.strip():
            return None
        if status not in ("started", "completed", "failed"):
            return None
        optional_strings = ("message", "error", "workspace", "tab")
        if any(
            value.get(key) is not None and not isinstance(value.get(key), str)
            for key in optional_strings
        ):
            return None
        for key in ("iteration", "attempt"):
            number = value.get(key)
            if number is not None and (
                isinstance(number, bool) or not isinstance(number, int) or number < 1
            ):
                return None
        return (
            "progress",
            ProgressEvent(
                name=name,
                status=cast(StepStatus, status),
                iteration=value.get("iteration"),
                attempt=value.get("attempt"),
                message=value.get("message"),
                error=value.get("error"),
                workspace=value.get("workspace"),
                tab=value.get("tab"),
            ),
        )

    @staticmethod
    def _register_resource(run: _RunRecord, resource: RunResource) -> None:
        for existing in run.resources:
            if (
                existing.kind == resource.kind
                and existing.identity == resource.identity
            ):
                # Repeated registration is idempotent only when the ownership
                # evidence remains exactly the same.
                if existing.metadata != resource.metadata:
                    logger.warning(
                        "Ignored conflicting registration for run %s resource %s/%s",
                        run.run_id,
                        resource.kind,
                        resource.identity,
                    )
                return
        run.resources.append(resource)

    @staticmethod
    def _register_owned_resource(
        run: _RunRecord, ownership: _ResourceOwnershipEvent
    ) -> bool:
        resource = ownership.resource
        for index, existing in enumerate(run.resources):
            if existing.kind != resource.kind or existing.identity != resource.identity:
                continue
            if ownership.phase == "pending":
                return existing.metadata == resource.metadata
            if existing.metadata == resource.metadata:
                return True
            if existing.metadata.get("registration_state") != "pending" or any(
                key == "registration_state"
                and resource.metadata.get(key) != "verified"
                or key != "registration_state"
                and resource.metadata.get(key) != value
                for key, value in existing.metadata.items()
            ):
                return False
            run.resources[index] = resource
            return True
        if ownership.phase != "pending":
            return False
        run.resources.append(resource)
        return True

    @staticmethod
    def _render_output_entries(
        chunks: deque[OutputEntry], truncated: bool
    ) -> tuple[OutputEntry, ...]:
        entries = tuple(chunks)
        if not truncated or not entries:
            return entries
        first, *remaining = entries
        return (
            OutputEntry(
                observed_at=first.observed_at,
                text="[output truncated; showing tail]\n" + first.text,
            ),
            *remaining,
        )

    def _wait_for_process(
        self,
        run: _RunRecord,
        process: subprocess.Popen[bytes],
        script_path: Path,
        stdout_thread: threading.Thread,
        stderr_thread: threading.Thread,
        progress_thread: threading.Thread,
    ) -> None:
        try:
            exit_code = process.wait()
            self._terminate_process_group(run)
            drain_deadline = time.monotonic() + self._stop_timeout
            for thread in (stdout_thread, stderr_thread, progress_thread):
                remaining = drain_deadline - time.monotonic()
                if remaining <= 0:
                    break
                thread.join(remaining)
            script_path.unlink(missing_ok=True)
            with self._lock:
                if run.process is not process:
                    return
                run.exit_code = exit_code
                run.state = (
                    "stopped"
                    if run.stop_requested
                    else "success"
                    if exit_code == 0
                    else "failed"
                )
                attempt_state = run.state
                run.attempts.append(
                    RunAttempt(
                        number=len(run.attempts) + 1,
                        state=attempt_state,
                        exit_code=exit_code,
                    )
                )
                terminal_state = run.state
                self._mark_changed()

            self._notify_terminal(
                run_id=run.run_id, state=terminal_state, exit_code=exit_code
            )
        finally:
            with self._lock:
                self._wait_threads.discard(threading.current_thread())

    def _wait_for_managed_workflow(self, run: _RunRecord) -> None:
        client = run.managed_client
        tab_id = run.managed_tab_id
        assert client is not None and tab_id is not None
        last_diagnostic: str | None = None
        retry_delay = _MANAGED_OBSERVATION_RETRY_INITIAL_SECONDS
        try:
            while True:
                with self._lock:
                    if run.state != "running" or self._closed:
                        return
                try:
                    client.wait_for_shell_completion(tab_id, 365 * 24 * 60 * 60)
                    result = client.read_shell_result(tab_id)
                except Exception as exc:
                    diagnostic = f"Workflow result observation is uncertain: {exc}"
                    if diagnostic != last_diagnostic:
                        self._record_managed_uncertainty(run, diagnostic)
                        last_diagnostic = diagnostic
                    with self._changes:
                        self._changes.wait_for(
                            lambda: run.state != "running" or self._closed,
                            timeout=retry_delay,
                        )
                    retry_delay = min(
                        retry_delay * 2, _MANAGED_OBSERVATION_RETRY_MAX_SECONDS
                    )
                    continue
                diagnostic = (
                    result.failure_message("Workflow")
                    if result.exit_code != 0
                    else None
                )
                self._finish_managed_workflow(
                    run, result.exit_code, diagnostic=diagnostic
                )
                return
        finally:
            with self._lock:
                self._wait_threads.discard(threading.current_thread())

    def _record_managed_uncertainty(self, run: _RunRecord, diagnostic: str) -> None:
        with self._lock:
            if run.state != "running":
                return
            self._append_output(run, "stderr", diagnostic + "\n", lock_held=True)
            self._mark_changed()

    def _finish_managed_workflow(
        self, run: _RunRecord, exit_code: int, *, diagnostic: str | None = None
    ) -> None:
        with self._lock:
            if run.state != "running":
                return
            run.exit_code = exit_code
            run.state = (
                "stopped"
                if run.stop_requested
                else "success"
                if exit_code == 0
                else "failed"
            )
            if diagnostic:
                self._append_output(run, "stderr", diagnostic + "\n", lock_held=True)
            run.attempts.append(
                RunAttempt(
                    number=len(run.attempts) + 1,
                    state=run.state,
                    exit_code=exit_code,
                )
            )
            terminal_state = run.state
            self._mark_changed()
        run.script_path.unlink(missing_ok=True)
        if run.credential_path is not None:
            run.credential_path.unlink(missing_ok=True)
        self._notify_terminal(
            run_id=run.run_id, state=terminal_state, exit_code=exit_code
        )

    def _notify_terminal(
        self, *, run_id: int | None, state: RunnerState, exit_code: int
    ) -> None:
        notifier = self._notifier
        if (
            notifier is None
            or run_id is None
            or state
            not in (
                "success",
                "failed",
                "stopped",
            )
        ):
            return
        try:
            result = notifier.notify_terminal(
                run_id=run_id, state=state, exit_code=exit_code
            )
        except Exception:
            logger.warning(
                "Terminal notification failed for run %d (%s): notifier error",
                run_id,
                state,
            )
            return
        if result.attempted and not result.delivered:
            logger.warning(
                "Terminal notification failed for run %d (%s): %s",
                run_id,
                state,
                result.diagnostic,
            )

    def _terminate_process_group(self, run: _RunRecord) -> None:
        process_group_id = run.process_group_id
        if process_group_id is None:
            return
        with run.cleanup_lock:
            if not self._process_group_exists(process_group_id):
                return
            try:
                os.killpg(process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + self._stop_timeout
            while time.monotonic() < deadline:
                if not self._process_group_exists(process_group_id):
                    return
                time.sleep(0.02)
            self._kill_process_group(process_group_id)

    @staticmethod
    def _kill_process_group(process_group_id: int) -> None:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        return True
