from __future__ import annotations

import fcntl
import json
import os
import queue
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, cast

from purplemux_client.errors import WorkerFailure


def ensure_codex_project_trust(
    project: str,
    *,
    executable: str = "codex",
    timeout_seconds: float = 10.0,
) -> str:
    """Trust exactly one existing project through Codex's config app-server API."""
    if timeout_seconds <= 0:
        raise ValueError("Codex trust timeout must be positive")
    try:
        canonical = Path(project).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkerFailure(
            f"Codex project trust path could not be resolved: {exc}"
        ) from exc
    if not canonical.is_dir():
        raise WorkerFailure(f"Codex project trust path is not a directory: {canonical}")

    deadline = time.monotonic() + timeout_seconds
    with _trust_mutation_lock(deadline):
        return _write_and_verify_trust(canonical, executable, deadline)


def _write_and_verify_trust(canonical: Path, executable: str, deadline: float) -> str:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        try:
            process = subprocess.Popen(
                [executable, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise WorkerFailure(
                f"could not start Codex project trust configuration: {exc}"
            ) from exc

        try:
            if process.stdin is None or process.stdout is None:
                raise WorkerFailure("Codex project trust app-server has no stdio")
            inbox: queue.Queue[dict[str, Any] | WorkerFailure] = queue.Queue()
            reader = threading.Thread(
                target=_read_messages,
                args=(process, process.stdout, inbox),
                daemon=True,
            )
            reader.start()
            _send(
                process.stdin,
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "agent-workflow-manager",
                            "title": "Agent Workflow Manager",
                            "version": "0.1.0",
                        }
                    },
                },
            )
            _response(inbox, 1, deadline)
            _send(process.stdin, {"method": "initialized", "params": {}})
            key_path = f"projects.{json.dumps(str(canonical))}.trust_level"
            _send(
                process.stdin,
                {
                    "id": 2,
                    "method": "config/batchWrite",
                    "params": {
                        "edits": [
                            {
                                "keyPath": key_path,
                                "value": "trusted",
                                "mergeStrategy": "upsert",
                            }
                        ],
                        "reloadUserConfig": True,
                    },
                },
            )
            write_result = _result(_response(inbox, 2, deadline), "write")
            if write_result.get("status") not in {"ok", "okOverridden"}:
                raise WorkerFailure("Codex project trust write was not accepted")

            _send(
                process.stdin,
                {
                    "id": 3,
                    "method": "config/read",
                    "params": {"cwd": str(canonical), "includeLayers": False},
                },
            )
            read_result = _result(_response(inbox, 3, deadline), "verification")
            config = read_result.get("config")
            projects = config.get("projects") if isinstance(config, Mapping) else None
            selected = (
                projects.get(str(canonical)) if isinstance(projects, Mapping) else None
            )
            trust_level = (
                selected.get("trust_level") if isinstance(selected, Mapping) else None
            )
            if trust_level != "trusted":
                raise WorkerFailure(
                    f"Codex did not confirm project trust for {canonical}"
                )
            return str(canonical)
        except (BrokenPipeError, OSError) as exc:
            raise WorkerFailure(
                f"Codex project trust configuration failed: {exc}"
            ) from exc
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            _stop(process, deadline)
            if "reader" in locals():
                reader.join(timeout=1.0)


@contextmanager
def _trust_mutation_lock(deadline: float) -> Iterator[None]:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    descriptor: int | None = None
    try:
        codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = codex_home / ".agent-workflow-manager-project-trust.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            os.close(descriptor)
            raise WorkerFailure("Codex project trust lock is not a safe user file")
        os.fchmod(descriptor, 0o600)
    except WorkerFailure:
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise WorkerFailure(f"could not open Codex project trust lock: {exc}") from exc

    assert descriptor is not None
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WorkerFailure(
                        "Codex project trust configuration timed out waiting for lock"
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _send(stream: IO[str], message: Mapping[str, Any]) -> None:
    stream.write(json.dumps(message, separators=(",", ":")) + "\n")
    stream.flush()


def _read_messages(
    process: subprocess.Popen[str],
    stream: IO[str],
    inbox: queue.Queue[dict[str, Any] | WorkerFailure],
) -> None:
    for line in stream:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            inbox.put(
                WorkerFailure("Codex project trust app-server returned malformed JSON")
            )
            return
        if isinstance(message, dict):
            inbox.put(cast(dict[str, Any], message))
    code = process.poll()
    inbox.put(
        WorkerFailure(
            "Codex project trust app-server exited before responding"
            + (f" (exit code {code})" if code is not None else "")
        )
    )


def _response(
    inbox: queue.Queue[dict[str, Any] | WorkerFailure],
    request_id: int,
    deadline: float,
) -> dict[str, Any]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerFailure("Codex project trust configuration timed out")
        try:
            message = inbox.get(timeout=remaining)
        except queue.Empty as exc:
            raise WorkerFailure("Codex project trust configuration timed out") from exc
        if isinstance(message, WorkerFailure):
            raise message
        if message.get("id") != request_id:
            continue
        error = message.get("error")
        if error is not None:
            detail = error.get("message") if isinstance(error, Mapping) else error
            raise WorkerFailure(
                f"Codex project trust app-server rejected the request: {detail}"
            )
        return message


def _result(message: Mapping[str, Any], operation: str) -> Mapping[str, Any]:
    result = message.get("result")
    if not isinstance(result, Mapping):
        raise WorkerFailure(
            f"Codex project trust {operation} returned an invalid response"
        )
    return result


def _stop(process: subprocess.Popen[str], deadline: float) -> None:
    remaining = max(0.0, deadline - time.monotonic())
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
