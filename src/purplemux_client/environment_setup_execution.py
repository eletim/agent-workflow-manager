"""Run Environment Setup commands in observable, run-owned PurpleMux terminals."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from purplemux_client.client import PurpleMuxCLIClient, ShellCommandRequest
from purplemux_client.errors import (
    MutationOutcomeUnknown,
    ResultNotReady,
    WorkerFailure,
)


def _capture(client: PurpleMuxCLIClient, tab: str) -> str:
    try:
        return client.capture_screen(tab)[-4096:]
    except WorkerFailure as exc:
        return f"pane capture failed: {exc}"


def _remaining_or_interrupt(
    client: PurpleMuxCLIClient, tab: str | None, remaining: Callable[[], float]
) -> float:
    try:
        return remaining()
    except TimeoutError:
        if tab is not None:
            try:
                client.interrupt(tab)
            except WorkerFailure:
                pass
        raise


def _completed_command(
    client: PurpleMuxCLIClient,
    *,
    name: str,
    command: str,
    cwd: str,
    remaining: Callable[[], float],
    on_created: Callable[[str], None] | None = None,
) -> dict[str, object]:
    remaining()
    created: list[str] = []

    def record_created(session: str, _result_path: str) -> None:
        created.append(session)
        if on_created is not None:
            on_created(session)

    try:
        tab = client.start_shell(
            ShellCommandRequest(
                command, cwd, f"Environment Setup {name}", deadline_check=remaining
            ),
            on_created=record_created,
        )
        if on_created is not None:
            on_created(tab)
    except MutationOutcomeUnknown:
        tab = created[0] if created else None
        _remaining_or_interrupt(client, tab, remaining)
        if tab is None:
            raise
        try:
            client.wait_for_shell_completion(
                tab, _remaining_or_interrupt(client, tab, remaining)
            )
            result = client.read_shell_result(tab)
        except WorkerFailure as observation_error:
            _remaining_or_interrupt(client, tab, remaining)
            raise MutationOutcomeUnknown(
                f"Environment Setup {name} launch is unresolved in terminal {tab}"
            ) from observation_error
        _remaining_or_interrupt(client, tab, remaining)
        return {
            "command": command,
            "tab_id": tab,
            "workspace_id": client.workspace_id,
            "exit_code": result.exit_code,
            "output": _capture(client, tab),
        }
    except WorkerFailure as exc:
        tab = created[0] if created else None
        _remaining_or_interrupt(client, tab, remaining)
        return {
            "command": command,
            "tab_id": tab,
            "workspace_id": client.workspace_id,
            "error": str(exc),
            "output": _capture(client, tab) if tab else "",
        }
    _remaining_or_interrupt(client, tab, remaining)
    try:
        client.wait_for_shell_completion(
            tab, _remaining_or_interrupt(client, tab, remaining)
        )
        result = client.read_shell_result(tab)
    except WorkerFailure as exc:
        _remaining_or_interrupt(client, tab, remaining)
        return {
            "command": command,
            "tab_id": tab,
            "workspace_id": client.workspace_id,
            "error": str(exc),
            "output": _capture(client, tab),
        }
    _remaining_or_interrupt(client, tab, remaining)
    return {
        "command": command,
        "tab_id": tab,
        "workspace_id": client.workspace_id,
        "exit_code": result.exit_code,
        "output": _capture(client, tab),
    }


def _service_outcome(
    client: PurpleMuxCLIClient, tab: str, command: str
) -> dict[str, object]:
    outcome: dict[str, object] = {
        "command": command,
        "tab_id": tab,
        "workspace_id": client.workspace_id,
        "output": _capture(client, tab),
    }
    try:
        result = client.read_shell_result(tab)
    except (ResultNotReady, WorkerFailure) as exc:
        try:
            status = client.read_status(tab)
        except WorkerFailure as status_error:
            outcome["running"] = None
            outcome["error"] = f"{exc}; status unavailable: {status_error}"
        else:
            alive = status.get("alive")
            outcome["running"] = alive if isinstance(alive, bool) else None
            if isinstance(exc, ResultNotReady):
                if alive is False:
                    outcome["error"] = "start terminal exited without a shell result"
                elif alive is not True:
                    outcome["error"] = "start terminal state is unknown"
            else:
                outcome["error"] = str(exc)
    else:
        outcome["exit_code"] = result.exit_code
        outcome["running"] = False
    return outcome


def _local_check_port(command: str | None) -> tuple[int, str] | None:
    if not command:
        return None
    urls = re.findall(r"https?://[^\s\"']+", command)
    endpoints: set[tuple[int, str]] = set()
    for url in urls:
        parsed = urlsplit(url.rstrip(";,)]"))
        host = parsed.hostname
        if host not in {"localhost", "127.0.0.1", "[::1]", "::1"}:
            continue
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None
        family = "any" if host == "localhost" else "ipv6" if host == "::1" else "ipv4"
        endpoints.add((port, family))
    return next(iter(endpoints)) if len(endpoints) == 1 else None


def _listener_inodes(endpoint: tuple[int, str]) -> set[str]:
    port, family = endpoint
    files = (
        ("tcp", "tcp6")
        if family == "any"
        else (("tcp6",) if family == "ipv6" else ("tcp",))
    )
    inodes: set[str] = set()
    for name in files:
        try:
            lines = (
                Path(f"/proc/net/{name}").read_text(encoding="ascii").splitlines()[1:]
            )
        except OSError:
            return set()
        for line in lines:
            fields = line.split()
            address, hex_port = fields[1].split(":")
            if fields[3] != "0A" or int(hex_port, 16) != port:
                continue
            allowed = (
                {"00000000", "0100007F"}
                if name == "tcp"
                else {"0" * 32, "00000000000000000000000001000000"}
            )
            if address in allowed:
                inodes.add(fields[9])
    return inodes


def _detached_service_provenance(
    cwd: str, endpoint: tuple[int, str], baseline: set[str]
) -> dict[str, object] | None:
    if baseline:
        return None
    candidates = _listener_inodes(endpoint) - baseline
    if not candidates:
        return None
    root = os.path.realpath(cwd)
    try:
        processes = list(Path("/proc").iterdir())
    except OSError:
        return None
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            process_cwd = os.path.realpath(os.readlink(process / "cwd"))
            if os.path.commonpath((root, process_cwd)) != root:
                continue
            for fd in (process / "fd").iterdir():
                target = os.readlink(fd)
                if target.startswith("socket:[") and target[8:-1] in candidates:
                    return {
                        "pid": int(process.name),
                        "socket_inode": target[8:-1],
                        "port": endpoint[0],
                        "family": endpoint[1],
                    }
        except (OSError, ValueError):
            continue
    return None


def verify_detached_service_provenance(cwd: str, provenance: dict[str, object]) -> None:
    """Ensure the same worktree process still owns the observed local listener."""
    pid = provenance.get("pid")
    inode = provenance.get("socket_inode")
    port = provenance.get("port")
    family = provenance.get("family")
    if (
        not isinstance(pid, int)
        or not isinstance(inode, str)
        or not isinstance(port, int)
        or not isinstance(family, str)
        or family not in {"any", "ipv4", "ipv6"}
    ):
        raise RuntimeError("Environment Setup detached service provenance is invalid")
    process = Path("/proc") / str(pid)
    try:
        root = os.path.realpath(cwd)
        process_cwd = os.path.realpath(os.readlink(process / "cwd"))
        owned = os.path.commonpath((root, process_cwd)) == root
        sockets = {os.readlink(fd) for fd in (process / "fd").iterdir()}
    except (OSError, ValueError):
        owned = False
        sockets = set()
    if (
        not owned
        or f"socket:[{inode}]" not in sockets
        or inode not in _listener_inodes((port, family))
    ):
        raise RuntimeError("Environment Setup detached service is no longer verified")


def execute_environment_setup_commands(
    *,
    client: PurpleMuxCLIClient,
    build: str | None,
    start: str | None,
    ready_check: str | None,
    verification_command: str | None,
    cwd: str,
    remaining: Callable[[], float],
    resume_at: str = "build",
    service_tab: str | None = None,
    observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run supplied stages in order; return observations and the first failure."""
    stages = ("build", "start", "ready_check")
    if resume_at not in stages:
        raise ValueError(f"invalid Environment Setup resume stage: {resume_at}")
    attempt = observation if observation is not None else {}
    checks: dict[str, dict[str, object]] = {}
    verification: dict[str, object] | None = None
    failure: str | None = None
    failed_stage: str | None = None
    detached_endpoint: tuple[int, str] | None = None
    listener_baseline: set[str] = set()
    attempt.update(
        checks=checks,
        verification=verification,
        service_tab=service_tab,
        failure=failure,
        failed_stage=failed_stage,
    )
    for stage in stages[stages.index(resume_at) :]:
        command = {"build": build, "start": start, "ready_check": ready_check}[stage]
        outcome: dict[str, object]
        if stage == "ready_check":
            command = ready_check if ready_check is not None else verification_command
            if not isinstance(command, str) or not command.strip():
                failure = "Environment Setup needs a usability check command"
                failed_stage = stage
                break
        elif command is None:
            continue
        pending: dict[str, object] = {
            "command": command,
            "workspace_id": client.workspace_id,
        }
        checks[stage] = pending
        if stage == "ready_check":
            attempt["verification"] = pending
        attempt["failed_stage"] = stage

        def record_tab(created_tab: str) -> None:
            pending["tab_id"] = created_tab
            if stage == "start":
                attempt["service_tab"] = created_tab

        if stage == "start":
            remaining()
            detached_endpoint = _local_check_port(ready_check or verification_command)
            if detached_endpoint is not None:
                listener_baseline = _listener_inodes(detached_endpoint)
            created: list[str] = []

            def record_created(tab: str, _result_path: str) -> None:
                created.append(tab)
                record_tab(tab)

            try:
                service_tab = client.start_shell(
                    ShellCommandRequest(
                        command,
                        cwd,
                        "Environment Setup start",
                        deadline_check=remaining,
                    ),
                    on_created=record_created,
                )
                record_tab(service_tab)
            except MutationOutcomeUnknown:
                service_tab = created[0] if created else None
                _remaining_or_interrupt(client, service_tab, remaining)
                if service_tab is None:
                    raise
                try:
                    result = client.read_shell_result(service_tab)
                except WorkerFailure as observation_error:
                    _remaining_or_interrupt(client, service_tab, remaining)
                    try:
                        status = client.read_status(service_tab)
                    except WorkerFailure as status_error:
                        status = {"error": str(status_error)}
                    raise MutationOutcomeUnknown(
                        f"Environment Setup start launch is unresolved in terminal "
                        f"{service_tab}; status={status}; output={_capture(client, service_tab)}"
                    ) from observation_error
                _remaining_or_interrupt(client, service_tab, remaining)
                outcome = {
                    "command": command,
                    "tab_id": service_tab,
                    "workspace_id": client.workspace_id,
                    "exit_code": result.exit_code,
                    "running": False,
                    "output": _capture(client, service_tab),
                }
            except WorkerFailure as exc:
                service_tab = created[0] if created else None
                _remaining_or_interrupt(client, service_tab, remaining)
                outcome = {
                    "command": command,
                    "tab_id": service_tab,
                    "workspace_id": client.workspace_id,
                    "error": str(exc),
                    "output": _capture(client, service_tab) if service_tab else "",
                }
            else:
                _remaining_or_interrupt(client, service_tab, remaining)
                outcome = _service_outcome(client, service_tab, command)
        else:
            outcome = _completed_command(
                client,
                name=stage,
                command=command,
                cwd=cwd,
                remaining=remaining,
                on_created=record_tab,
            )
        if stage == "ready_check":
            verification = outcome
            attempt["verification"] = outcome
            if ready_check is not None:
                checks[stage] = outcome
            else:
                checks.pop(stage, None)
        else:
            checks[stage] = outcome
        if stage == "start":
            attempt["service_tab"] = service_tab
        if outcome.get("error") or outcome.get("exit_code") not in (None, 0):
            failure = f"Environment Setup {stage} failed: {outcome}"
            failed_stage = stage
            if (
                stage == "start"
                and "running" in outcome
                and outcome["running"] is not False
            ):
                failed_stage = "ready_check"
            break
    if (
        service_tab is not None
        and start is not None
        and (failure is None or failed_stage == "ready_check")
    ):
        service = _service_outcome(client, service_tab, start)
        checks["start"] = service
        if service.get("running") is False:
            provenance = (
                _detached_service_provenance(cwd, detached_endpoint, listener_baseline)
                if detached_endpoint is not None and service.get("exit_code") == 0
                else None
            )
            if provenance is None:
                failure = (
                    "Environment Setup start terminal exited without verified "
                    f"service provenance: {service}"
                )
                failed_stage = "start"
            else:
                service["provenance"] = provenance
        elif service.get("error"):
            failure = f"Environment Setup start observation failed: {service}"
            failed_stage = "ready_check"
    remaining()
    attempt.update(
        verification=verification,
        service_tab=service_tab,
        failure=failure,
        failed_stage=failed_stage,
    )
    return attempt
