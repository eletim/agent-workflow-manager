from __future__ import annotations

import errno
import json
import os
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from purplemux_client.errors import WorkerFailure

_LOCK_STALE_SECONDS = 10.0
_LOCK_UPDATE_SECONDS = 1.0


def ensure_claude_project_trust(
    project: str,
    *,
    timeout_seconds: float = 10.0,
) -> str:
    """Trust exactly one existing workspace through Claude Code project state."""
    if timeout_seconds <= 0:
        raise ValueError("Claude trust timeout must be positive")
    try:
        canonical = Path(project).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkerFailure(
            f"Claude project trust path could not be resolved: {exc}"
        ) from exc
    if not canonical.is_dir():
        raise WorkerFailure(
            f"Claude project trust path is not a directory: {canonical}"
        )

    try:
        home = Path.home().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkerFailure(
            f"Claude home directory could not be resolved: {exc}"
        ) from exc
    if canonical == home:
        raise WorkerFailure(
            "Claude does not persist workspace trust for the home directory; "
            "select a repository directory instead"
        )

    config_path = _claude_state_path(home)
    deadline = time.monotonic() + timeout_seconds
    with _trust_mutation_lock(config_path, deadline):
        state, mode = _read_state(config_path)
        projects = state.get("projects")
        if projects is None:
            projects = {}
            state["projects"] = projects
        if not isinstance(projects, dict):
            raise WorkerFailure("Claude project state has an invalid projects value")
        selected = projects.get(str(canonical))
        if selected is None:
            selected = {}
            projects[str(canonical)] = selected
        if not isinstance(selected, dict):
            raise WorkerFailure(
                f"Claude project state for {canonical} is not an object"
            )
        selected["hasTrustDialogAccepted"] = True
        _write_state(config_path, state, mode)
        verified, _ = _read_state(config_path)
        verified_projects = verified.get("projects")
        verified_project = (
            verified_projects.get(str(canonical))
            if isinstance(verified_projects, Mapping)
            else None
        )
        if not isinstance(verified_project, Mapping) or (
            verified_project.get("hasTrustDialogAccepted") is not True
        ):
            raise WorkerFailure(f"Claude did not confirm project trust for {canonical}")
    return str(canonical)


def _claude_state_path(home: Path) -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured is None:
        config_directory = home / ".claude"
        legacy_directory = home
    else:
        if not configured.strip() or "\0" in configured:
            raise WorkerFailure("CLAUDE_CONFIG_DIR is not a valid directory")
        config_directory = Path(configured)
        if not config_directory.is_absolute():
            raise WorkerFailure("CLAUDE_CONFIG_DIR must be an absolute path")
        legacy_directory = config_directory
    current_state = config_directory / ".config.json"
    if current_state.exists():
        return current_state
    filename = (
        ".claude-custom-oauth.json"
        if os.environ.get("CLAUDE_CODE_CUSTOM_OAUTH_URL")
        else ".claude.json"
    )
    return legacy_directory / filename


def _read_state(path: Path) -> tuple[dict[str, Any], int]:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {}, 0o600
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise WorkerFailure("Claude project state is not a safe user file") from exc
        raise WorkerFailure(f"could not inspect Claude project state: {exc}") from exc
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        os.close(descriptor)
        raise WorkerFailure("Claude project state is not a safe user file")
    try:
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = None
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerFailure(f"could not read Claude project state: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise WorkerFailure("Claude project state is not a JSON object")
    return value, stat.S_IMODE(details.st_mode)


def _write_state(path: Path, state: Mapping[str, Any], mode: int) -> None:
    descriptor: int | None = None
    temporary: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.awm-", dir=path.parent
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except (OSError, TypeError, ValueError) as exc:
        raise WorkerFailure(f"could not write Claude project trust: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


@contextmanager
def _trust_mutation_lock(path: Path, deadline: float) -> Iterator[None]:
    lock_path = Path(f"{path}.lock")
    identity: tuple[int, int] | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        while True:
            try:
                lock_path.mkdir(mode=0o700)
                details = lock_path.stat(follow_symlinks=False)
                identity = (details.st_dev, details.st_ino)
                break
            except FileExistsError:
                try:
                    details = lock_path.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
                    raise WorkerFailure(
                        "Claude project trust lock is not a safe user directory"
                    )
                if time.time() - details.st_mtime > _LOCK_STALE_SECONDS:
                    try:
                        current = lock_path.stat(follow_symlinks=False)
                        if (
                            current.st_dev,
                            current.st_ino,
                            current.st_mtime_ns,
                        ) == (
                            details.st_dev,
                            details.st_ino,
                            details.st_mtime_ns,
                        ):
                            lock_path.rmdir()
                            continue
                    except FileNotFoundError:
                        continue
                    except OSError:
                        pass
                if time.monotonic() >= deadline:
                    raise WorkerFailure(
                        "Claude project trust configuration timed out waiting for lock"
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    except WorkerFailure:
        raise
    except OSError as exc:
        raise WorkerFailure(f"could not open Claude project trust lock: {exc}") from exc

    assert identity is not None
    heartbeat_stop = threading.Event()
    heartbeat_errors: list[OSError | WorkerFailure] = []

    def refresh_lock() -> None:
        while not heartbeat_stop.wait(_LOCK_UPDATE_SECONDS):
            try:
                details = lock_path.stat(follow_symlinks=False)
                if (details.st_dev, details.st_ino) != identity:
                    raise WorkerFailure(
                        "Claude project trust lock changed while it was held"
                    )
                os.utime(lock_path, follow_symlinks=False)
            except (OSError, WorkerFailure) as exc:
                heartbeat_errors.append(exc)
                return

    heartbeat = threading.Thread(
        target=refresh_lock,
        name="claude-trust-lock-heartbeat",
        daemon=True,
    )
    heartbeat.start()
    try:
        yield
        if heartbeat_errors:
            raise WorkerFailure(
                f"could not refresh Claude project trust lock: {heartbeat_errors[0]}"
            )
    finally:
        heartbeat_stop.set()
        heartbeat.join()
        try:
            details = lock_path.stat(follow_symlinks=False)
            if (details.st_dev, details.st_ino) != identity:
                raise WorkerFailure(
                    "Claude project trust lock changed before it could be released"
                )
            lock_path.rmdir()
        except FileNotFoundError as exc:
            raise WorkerFailure(
                "Claude project trust lock disappeared before it could be released"
            ) from exc
        except WorkerFailure:
            raise
        except OSError as exc:
            raise WorkerFailure(
                f"could not release Claude project trust lock: {exc}"
            ) from exc


def main() -> int:
    """Apply trust from the environment of the process Claude will inherit."""
    if len(sys.argv) != 2:
        print(
            "usage: python -I -m purplemux_client.claude_trust PROJECT",
            file=sys.stderr,
        )
        return 2
    try:
        ensure_claude_project_trust(sys.argv[1])
    except (ValueError, WorkerFailure) as exc:
        print(f"Claude project trust failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
