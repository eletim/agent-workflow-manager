from __future__ import annotations

import codecs
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Literal, cast

from purplemux_client.progress import (
    MAX_PROGRESS_EVENT_BYTES,
    PROGRESS_FD_ENV,
    StepStatus,
)

RunnerState = Literal["idle", "running", "success", "failed", "stopped"]
DEFAULT_MAX_PROGRESS_EVENTS = 200


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


@dataclass(frozen=True)
class RunnerSnapshot:
    state: RunnerState
    stdout: str
    stderr: str
    exit_code: int | None
    run_id: int | None
    progress: tuple[ProgressEvent, ...]

    def as_json(self) -> dict[str, object]:
        payload = asdict(self)
        payload["exitCode"] = payload.pop("exit_code")
        payload["runId"] = payload.pop("run_id")
        payload["progress"] = [asdict(event) for event in self.progress]
        return payload


class PythonRunner:
    """Run one trusted local Python program at a time."""

    def __init__(
        self,
        *,
        stop_timeout: float = 3.0,
        max_output_chars: int = 1_000_000,
        max_progress_events: int = DEFAULT_MAX_PROGRESS_EVENTS,
    ) -> None:
        if os.name != "posix":
            raise RuntimeError("PythonRunner requires a POSIX operating system")
        if max_output_chars < 1:
            raise ValueError("max_output_chars must be positive")
        if max_progress_events < 1:
            raise ValueError("max_progress_events must be positive")
        self._stop_timeout = stop_timeout
        self._max_output_chars = max_output_chars
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._process_group_id: int | None = None
        self._group_cleanup_lock = threading.Lock()
        self._script_path: Path | None = None
        self._state: RunnerState = "idle"
        self._stdout: deque[str] = deque()
        self._stderr: deque[str] = deque()
        self._stdout_chars = 0
        self._stderr_chars = 0
        self._stdout_truncated = False
        self._stderr_truncated = False
        self._exit_code: int | None = None
        self._run_id: int | None = None
        self._next_run_id = 1
        self._stop_requested = False
        self._progress: deque[ProgressEvent] = deque(maxlen=max_progress_events)

    def start(self, code: str) -> int:
        with self._lock:
            if self._process is not None:
                raise AlreadyRunningError("a Python process is already running")

            script = tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", encoding="utf-8", delete=False
            )
            try:
                script.write(code)
            finally:
                script.close()
            script_path = Path(script.name)

            progress_read_fd, progress_write_fd = os.pipe()
            child_env = os.environ.copy()
            child_env[PROGRESS_FD_ENV] = str(progress_write_fd)

            try:
                process = subprocess.Popen(
                    [sys.executable, str(script_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=child_env,
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

            run_id = self._next_run_id
            self._next_run_id += 1
            self._process = process
            self._process_group_id = process.pid
            self._script_path = script_path
            self._state = "running"
            self._stdout.clear()
            self._stderr.clear()
            self._stdout_chars = 0
            self._stderr_chars = 0
            self._stdout_truncated = False
            self._stderr_truncated = False
            self._exit_code = None
            self._run_id = run_id
            self._stop_requested = False
            self._progress.clear()

        stdout_thread = threading.Thread(
            target=self._read_stream,
            args=(process.stdout, "stdout"),
            name=f"python-runner-stdout-{run_id}",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stream,
            args=(process.stderr, "stderr"),
            name=f"python-runner-stderr-{run_id}",
            daemon=True,
        )
        progress_thread = threading.Thread(
            target=self._read_progress,
            args=(progress_read_fd,),
            name=f"python-runner-progress-{run_id}",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        progress_thread.start()
        threading.Thread(
            target=self._wait_for_process,
            args=(
                process,
                script_path,
                stdout_thread,
                stderr_thread,
                progress_thread,
            ),
            name=f"python-runner-wait-{run_id}",
            daemon=True,
        ).start()
        return run_id

    def snapshot(self) -> RunnerSnapshot:
        with self._lock:
            return RunnerSnapshot(
                state=self._state,
                stdout=self._render_output(self._stdout, self._stdout_truncated),
                stderr=self._render_output(self._stderr, self._stderr_truncated),
                exit_code=self._exit_code,
                run_id=self._run_id,
                progress=tuple(self._progress),
            )

    def stop(self) -> bool:
        with self._lock:
            process = self._process
            process_group_id = self._process_group_id
            if process is None or process_group_id is None:
                return False
            self._stop_requested = True

        self._terminate_process_group(process_group_id)
        return True

    def close(self) -> None:
        self.stop()
        with self._lock:
            process = self._process
        if process is not None:
            try:
                process.wait(timeout=self._stop_timeout + 1)
            except subprocess.TimeoutExpired:
                process_group_id = self._process_group_id
                if process_group_id is not None:
                    self._kill_process_group(process_group_id)
                process.wait()

    def _read_stream(
        self, stream: IO[bytes] | None, destination: Literal["stdout", "stderr"]
    ) -> None:
        if stream is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while chunk := os.read(stream.fileno(), 4096):
                text = decoder.decode(chunk)
                self._append_output(destination, text)
            final_text = decoder.decode(b"", final=True)
            if final_text:
                self._append_output(destination, final_text)
        finally:
            stream.close()

    def _append_output(
        self, destination: Literal["stdout", "stderr"], text: str
    ) -> None:
        with self._lock:
            chunks = self._stdout if destination == "stdout" else self._stderr
            size_attribute = (
                "_stdout_chars" if destination == "stdout" else "_stderr_chars"
            )
            truncated_attribute = (
                "_stdout_truncated" if destination == "stdout" else "_stderr_truncated"
            )
            chunks.append(text)
            size = getattr(self, size_attribute) + len(text)
            was_truncated = size > self._max_output_chars
            while size > self._max_output_chars:
                overflow = size - self._max_output_chars
                first = chunks[0]
                if len(first) <= overflow:
                    size -= len(chunks.popleft())
                else:
                    chunks[0] = first[overflow:]
                    size -= overflow
            setattr(self, size_attribute, size)
            if was_truncated:
                setattr(self, truncated_attribute, True)

    def _read_progress(self, fd: int) -> None:
        try:
            with os.fdopen(fd, "rb") as stream:
                while line := stream.readline(MAX_PROGRESS_EVENT_BYTES + 1):
                    if len(line) > MAX_PROGRESS_EVENT_BYTES or not line.endswith(b"\n"):
                        while line and not line.endswith(b"\n"):
                            line = stream.readline(MAX_PROGRESS_EVENT_BYTES + 1)
                        continue
                    event = self._parse_progress_event(
                        line.decode("utf-8", errors="replace")
                    )
                    if event is not None:
                        with self._lock:
                            self._progress.append(event)
        except OSError:
            return

    @staticmethod
    def _parse_progress_event(line: str) -> ProgressEvent | None:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
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
        return ProgressEvent(
            name=name,
            status=cast(StepStatus, status),
            iteration=value.get("iteration"),
            attempt=value.get("attempt"),
            message=value.get("message"),
            error=value.get("error"),
            workspace=value.get("workspace"),
            tab=value.get("tab"),
        )

    @staticmethod
    def _render_output(chunks: deque[str], truncated: bool) -> str:
        prefix = "[output truncated; showing tail]\n" if truncated else ""
        return prefix + "".join(chunks)

    def _wait_for_process(
        self,
        process: subprocess.Popen[bytes],
        script_path: Path,
        stdout_thread: threading.Thread,
        stderr_thread: threading.Thread,
        progress_thread: threading.Thread,
    ) -> None:
        exit_code = process.wait()
        self._terminate_process_group(process.pid)
        stdout_thread.join()
        stderr_thread.join()
        progress_thread.join()
        script_path.unlink(missing_ok=True)
        with self._lock:
            if self._process is not process:
                return
            self._exit_code = exit_code
            self._state = (
                "stopped"
                if self._stop_requested
                else "success"
                if exit_code == 0
                else "failed"
            )
            self._process = None
            self._process_group_id = None
            self._script_path = None

    def _terminate_process_group(self, process_group_id: int) -> None:
        with self._group_cleanup_lock:
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
