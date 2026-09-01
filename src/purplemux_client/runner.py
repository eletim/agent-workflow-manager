from __future__ import annotations

import codecs
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Literal, Protocol, cast

from purplemux_client.notifier import NotificationResult, TerminalState
from purplemux_client.preflight import (
    ValidationIssue,
    ValidationResult,
    WorkflowValidator,
)
from purplemux_client.progress import (
    MAX_PROGRESS_EVENT_BYTES,
    PROGRESS_FD_ENV,
    RESUME_CHECKPOINT_ENV,
    SUSPENDED_EXIT_CODE,
    ResumeCheckpoint,
    StepStatus,
)

RunnerState = Literal[
    "idle",
    "running",
    "success",
    "failed",
    "suspended",
    "stopped",
    "validation_failed",
]
DEFAULT_MAX_PROGRESS_EVENTS = 200
logger = logging.getLogger(__name__)


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


class AlreadyRunningError(RuntimeError):
    """Raised when a run is requested while another process is active."""


class RunnerClosedError(AlreadyRunningError):
    """Raised when a run is requested after Runner shutdown begins."""


class WorkflowValidationError(RuntimeError):
    """Raised when workflow preflight fails before a process is started."""

    def __init__(self, result: ValidationResult) -> None:
        super().__init__("workflow validation failed")
        self.result = result


class InvalidExecutionContextError(ValueError):
    """Raised when a requested run execution context cannot be used."""


class RunNotFoundError(LookupError):
    """Raised when a requested run identifier does not exist."""


class RunNotResumableError(RuntimeError):
    """Raised when a run has no workflow-proven safe continuation point."""


@dataclass(frozen=True)
class RunAttempt:
    number: int
    state: Literal["success", "failed", "suspended", "stopped"]
    exit_code: int
    resumed_from: str | None = None


@dataclass(frozen=True)
class RunnerSnapshot:
    state: RunnerState
    stdout: str
    stderr: str
    exit_code: int | None
    run_id: int | None
    progress: tuple[ProgressEvent, ...]
    validation: tuple[ValidationIssue, ...]
    cwd: str
    args: tuple[str, ...]
    checkpoint: ResumeCheckpoint | None
    attempts: tuple[RunAttempt, ...]
    suspension_reason: str | None

    def as_json(self) -> dict[str, object]:
        payload = asdict(self)
        payload["exitCode"] = payload.pop("exit_code")
        payload["runId"] = payload.pop("run_id")
        payload["suspensionReason"] = payload.pop("suspension_reason")
        payload["progress"] = [asdict(event) for event in self.progress]
        payload["validation"] = [issue.as_json() for issue in self.validation]
        payload["attempts"] = [
            {
                "number": attempt.number,
                "state": attempt.state,
                "exitCode": attempt.exit_code,
                "resumedFrom": attempt.resumed_from,
            }
            for attempt in self.attempts
        ]
        payload["resumable"] = self.state in ("failed", "suspended") and (
            self.checkpoint is not None
        )
        return payload

    def as_summary_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "exitCode": self.exit_code,
            "runId": self.run_id,
            "cwd": self.cwd,
            "args": list(self.args),
            "checkpoint": asdict(self.checkpoint) if self.checkpoint else None,
            "attempts": len(self.attempts),
            "resumable": self.state in ("failed", "suspended")
            and self.checkpoint is not None,
        }


@dataclass
class _RunRecord:
    run_id: int
    cwd: str
    args: tuple[str, ...]
    process: subprocess.Popen[bytes]
    process_group_id: int
    script_path: Path
    code: str
    state: RunnerState = "running"
    stdout: deque[str] = field(default_factory=deque)
    stderr: deque[str] = field(default_factory=deque)
    stdout_chars: int = 0
    stderr_chars: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    exit_code: int | None = None
    stop_requested: bool = False
    progress: deque[ProgressEvent] = field(default_factory=deque)
    cleanup_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    checkpoint: ResumeCheckpoint | None = None
    resumed_from: str | None = None
    attempts: list[RunAttempt] = field(default_factory=list)
    suspension_reason: str | None = None


class PythonRunner:
    """Run and observe trusted local Python programs independently."""

    def __init__(
        self,
        *,
        stop_timeout: float = 3.0,
        max_output_chars: int = 1_000_000,
        max_progress_events: int = DEFAULT_MAX_PROGRESS_EVENTS,
        notifier: TerminalNotifier | None = None,
        validator: WorkflowValidator | None = None,
    ) -> None:
        if os.name != "posix":
            raise RuntimeError("PythonRunner requires a POSIX operating system")
        if max_output_chars < 1:
            raise ValueError("max_output_chars must be positive")
        if max_progress_events < 1:
            raise ValueError("max_progress_events must be positive")
        self._stop_timeout = stop_timeout
        self._max_output_chars = max_output_chars
        self._max_progress_events = max_progress_events
        self._lock = threading.Lock()
        self._validation_lock = threading.Lock()
        self._runs: dict[int, _RunRecord] = {}
        self._next_run_id = 1
        self._notifier = notifier
        self._wait_threads: set[threading.Thread] = set()
        self._closed = False
        self._preview = RunnerSnapshot(
            state="idle",
            stdout="",
            stderr="",
            exit_code=None,
            run_id=None,
            progress=(),
            validation=(),
            cwd=str(Path.cwd()),
            args=(),
            checkpoint=None,
            attempts=(),
            suspension_reason=None,
        )
        self._validator = validator or WorkflowValidator()

    def validate(
        self,
        code: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        args: Sequence[str] = (),
    ) -> ValidationResult:
        run_cwd, run_args, child_env = self._execution_context(cwd, args)
        with self._validation_lock:
            with self._lock:
                self._ensure_open()
            result = self._validator.validate(code, cwd=run_cwd, environment=child_env)
            with self._lock:
                self._ensure_open()
                self._apply_validation(result, run_cwd, run_args)
            return result

    def start(
        self,
        code: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        args: Sequence[str] = (),
    ) -> int:
        run_cwd, run_args, child_env = self._execution_context(cwd, args)
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
                    code, run_cwd=run_cwd, run_args=run_args, child_env=child_env
                )

    def resume(self, run_id: int) -> None:
        """Explicitly continue a failed run from its latest workflow checkpoint."""
        with self._validation_lock:
            with self._lock:
                self._ensure_open()
                run = self._get_run(run_id)
                self._ensure_resumable(run)
                code = run.code
                checkpoint = run.checkpoint
                cwd = run.cwd
                args = run.args
            run_cwd, run_args, child_env = self._execution_context(cwd, args)
            validation = self._validator.validate(
                code, cwd=run_cwd, environment=child_env
            )
            with self._lock:
                self._ensure_open()
                run = self._get_run(run_id)
                self._ensure_resumable(run)
                if run.checkpoint != checkpoint:
                    raise RunNotResumableError(
                        f"run {run_id} checkpoint changed while resume was prepared"
                    )
                if not validation.valid:
                    raise WorkflowValidationError(validation)
                assert checkpoint is not None
                child_env[RESUME_CHECKPOINT_ENV] = json.dumps(
                    asdict(checkpoint), ensure_ascii=False, separators=(",", ":")
                )
                process, script_path, progress_read_fd = self._spawn_process(
                    code,
                    run_cwd=run_cwd,
                    run_args=run_args,
                    child_env=child_env,
                )
                self._append_output(
                    run,
                    "stdout",
                    f"\n[resume attempt {len(run.attempts) + 1} "
                    f"from checkpoint {checkpoint.name!r}]\n",
                    lock_held=True,
                )
                self._append_output(
                    run,
                    "stderr",
                    f"\n[resume attempt {len(run.attempts) + 1} "
                    f"from checkpoint {checkpoint.name!r}]\n",
                    lock_held=True,
                )
                run.process = process
                run.process_group_id = process.pid
                run.script_path = script_path
                run.state = "running"
                run.exit_code = None
                run.stop_requested = False
                run.resumed_from = checkpoint.name
                run.suspension_reason = None
                self._start_attempt_threads(run, process, script_path, progress_read_fd)

    @staticmethod
    def _ensure_resumable(run: _RunRecord) -> None:
        if run.state not in ("failed", "suspended"):
            raise RunNotResumableError(
                f"run {run.run_id} is {run.state}; only failed or suspended runs can resume"
            )
        if run.checkpoint is None:
            raise RunNotResumableError(
                f"run {run.run_id} has no safe checkpoint; start a new run or update "
                "the workflow to call save_checkpoint() after completed side effects"
            )

    def _start_validated(
        self,
        code: str,
        *,
        run_cwd: Path,
        run_args: tuple[str, ...],
        child_env: Mapping[str, str],
    ) -> int:
        process, script_path, progress_read_fd = self._spawn_process(
            code,
            run_cwd=run_cwd,
            run_args=run_args,
            child_env=child_env,
        )

        run_id = self._next_run_id
        self._next_run_id += 1
        run = _RunRecord(
            run_id=run_id,
            cwd=str(run_cwd),
            args=run_args,
            process=process,
            process_group_id=process.pid,
            script_path=script_path,
            code=code,
            progress=deque(maxlen=self._max_progress_events),
        )
        self._runs[run_id] = run

        self._start_attempt_threads(run, process, script_path, progress_read_fd)
        return run_id

    @staticmethod
    def _spawn_process(
        code: str,
        *,
        run_cwd: Path,
        run_args: tuple[str, ...],
        child_env: Mapping[str, str],
    ) -> tuple[subprocess.Popen[bytes], Path, int]:
        script = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", encoding="utf-8", delete=False
        )
        try:
            script.write(code)
        finally:
            script.close()
        script_path = Path(script.name)
        progress_read_fd, progress_write_fd = os.pipe()
        process_env = dict(child_env)
        process_env[PROGRESS_FD_ENV] = str(progress_write_fd)
        try:
            process = subprocess.Popen(
                [sys.executable, str(script_path), *run_args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_env,
                cwd=run_cwd,
                pass_fds=(progress_write_fd,),
                shell=False,
                start_new_session=True,
            )
        except BaseException:
            os.close(progress_read_fd)
            os.close(progress_write_fd)
            script_path.unlink(missing_ok=True)
            raise
        os.close(progress_write_fd)
        return process, script_path, progress_read_fd

    def _start_attempt_threads(
        self,
        run: _RunRecord,
        process: subprocess.Popen[bytes],
        script_path: Path,
        progress_read_fd: int,
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
            args=(run, progress_read_fd),
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
            exit_code=None,
            run_id=None,
            progress=(),
            validation=result.issues,
            cwd=str(run_cwd),
            args=run_args,
            checkpoint=None,
            attempts=(),
            suspension_reason=None,
        )

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
        return RunnerSnapshot(
            state=run.state,
            stdout=self._render_output(run.stdout, run.stdout_truncated),
            stderr=self._render_output(run.stderr, run.stderr_truncated),
            exit_code=run.exit_code,
            run_id=run.run_id,
            progress=tuple(run.progress),
            validation=(),
            cwd=run.cwd,
            args=run.args,
            checkpoint=run.checkpoint,
            attempts=tuple(run.attempts),
            suspension_reason=run.suspension_reason,
        )

    def _get_run(self, run_id: int) -> _RunRecord:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"run {run_id} was not found") from exc

    @staticmethod
    def _execution_context(
        cwd: str | os.PathLike[str] | None, args: Sequence[str]
    ) -> tuple[Path, tuple[str, ...], dict[str, str]]:
        explicit_cwd = cwd is not None
        try:
            run_cwd = Path.cwd() if cwd is None else Path(cwd).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise InvalidExecutionContextError(
                f"working directory could not be resolved: {exc}"
            ) from exc
        if not run_cwd.is_dir():
            raise InvalidExecutionContextError(
                f"working directory is not a directory: {run_cwd}"
            )
        if isinstance(args, (str, bytes)):
            raise InvalidExecutionContextError("args must be a sequence of strings")
        run_args = tuple(args)
        if any(not isinstance(argument, str) for argument in run_args):
            raise InvalidExecutionContextError("args must contain only strings")
        if any("\0" in argument for argument in run_args):
            raise InvalidExecutionContextError("args must not contain null bytes")

        child_env = os.environ.copy()
        child_env.pop(RESUME_CHECKPOINT_ENV, None)
        if explicit_cwd:
            virtual_env = child_env.pop("VIRTUAL_ENV", None)
            virtual_envs = {virtual_env} if virtual_env else set()
            if sys.prefix != sys.base_prefix:
                virtual_envs.add(sys.prefix)
            virtual_env_bins = {
                os.path.normpath(os.path.join(environment, "bin"))
                for environment in virtual_envs
            }
            child_env["PATH"] = os.pathsep.join(
                entry
                for entry in child_env.get("PATH", "").split(os.pathsep)
                if os.path.normpath(entry) not in virtual_env_bins
            )
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
            if run.state != "running":
                return False
            run.stop_requested = True

        self._terminate_process_group(run)
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            active_runs = tuple(
                run for run in self._runs.values() if run.state == "running"
            )
            for run in active_runs:
                run.stop_requested = True
        self._validator.close()
        cleanup_threads = tuple(
            threading.Thread(
                target=self._terminate_process_group,
                args=(run,),
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
            try:
                run.process.wait(timeout=self._stop_timeout + 1)
            except subprocess.TimeoutExpired:
                self._kill_process_group(run.process_group_id)
                run.process.wait()
        notifier = self._notifier
        if notifier is not None:
            try:
                notifier.close()
            except Exception:
                logger.warning("Failed to close terminal notifier")
        current_thread = threading.current_thread()
        while True:
            with self._lock:
                wait_threads = tuple(
                    thread
                    for thread in self._wait_threads
                    if thread is not current_thread
                )
            if not wait_threads:
                break
            for thread in wait_threads:
                thread.join()

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
        def append() -> None:
            chunks = run.stdout if destination == "stdout" else run.stderr
            size_attribute = (
                "stdout_chars" if destination == "stdout" else "stderr_chars"
            )
            truncated_attribute = (
                "stdout_truncated" if destination == "stdout" else "stderr_truncated"
            )
            chunks.append(text)
            size = getattr(run, size_attribute) + len(text)
            was_truncated = size > self._max_output_chars
            while size > self._max_output_chars:
                overflow = size - self._max_output_chars
                first = chunks[0]
                if len(first) <= overflow:
                    size -= len(chunks.popleft())
                else:
                    chunks[0] = first[overflow:]
                    size -= overflow
            setattr(run, size_attribute, size)
            if was_truncated:
                setattr(run, truncated_attribute, True)

        if lock_held:
            append()
        else:
            with self._lock:
                append()

    def _read_progress(self, run: _RunRecord, fd: int) -> None:
        try:
            with os.fdopen(fd, "rb") as stream:
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
                            event_type, event = value
                            if event_type == "checkpoint":
                                run.checkpoint = cast(ResumeCheckpoint, event)
                            elif event_type == "suspended":
                                run.suspension_reason = cast(str, event)
                            else:
                                assert isinstance(event, ProgressEvent)
                                run.progress.append(event)
        except OSError:
            return

    @staticmethod
    def _parse_runner_event(
        line: str,
    ) -> tuple[Literal["progress", "checkpoint", "suspended"], object] | None:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        event_type = value.get("type")
        if event_type == "checkpoint":
            name = value.get("name")
            data = value.get("data")
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(data, dict)
                or any(
                    not isinstance(key, str) or not isinstance(item, str)
                    for key, item in data.items()
                )
            ):
                return None
            return "checkpoint", ResumeCheckpoint(name=name, data=dict(data))
        if event_type == "suspended":
            reason = value.get("reason")
            if isinstance(reason, str) and reason.strip():
                return "suspended", reason
            return None
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
    def _render_output(chunks: deque[str], truncated: bool) -> str:
        prefix = "[output truncated; showing tail]\n" if truncated else ""
        return prefix + "".join(chunks)

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
            stdout_thread.join()
            stderr_thread.join()
            progress_thread.join()
            script_path.unlink(missing_ok=True)
            with self._lock:
                if run.process is not process:
                    return
                run.exit_code = exit_code
                run.state = (
                    "stopped"
                    if run.stop_requested
                    else "suspended"
                    if exit_code == SUSPENDED_EXIT_CODE
                    and run.suspension_reason is not None
                    else "success"
                    if exit_code == 0
                    else "failed"
                )
                if run.state != "suspended":
                    run.suspension_reason = None
                attempt_state = run.state
                run.attempts.append(
                    RunAttempt(
                        number=len(run.attempts) + 1,
                        state=attempt_state,
                        exit_code=exit_code,
                        resumed_from=run.resumed_from,
                    )
                )
                terminal_state = run.state

            self._notify_terminal(
                run_id=run.run_id, state=terminal_state, exit_code=exit_code
            )
        finally:
            with self._lock:
                self._wait_threads.discard(threading.current_thread())

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
