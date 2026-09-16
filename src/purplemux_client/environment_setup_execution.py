"""Run Environment Setup commands in observable, run-owned PurpleMux terminals."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from purplemux_client.client import PurpleMuxCLIClient, ShellCommandRequest
from purplemux_client.errors import ResultNotReady, WorkerFailure


def _capture(client: PurpleMuxCLIClient, tab: str) -> str:
    try:
        return client.capture_screen(tab)[-4096:]
    except WorkerFailure as exc:
        return f"pane capture failed: {exc}"


def _completed_command(
    client: PurpleMuxCLIClient,
    *,
    name: str,
    command: str,
    cwd: str,
    remaining: Callable[[], float],
) -> dict[str, object]:
    remaining()
    created: list[str] = []
    try:
        tab = client.start_shell(
            ShellCommandRequest(command, cwd, f"Environment Setup {name}"),
            on_created=lambda session, _result_path: created.append(session),
        )
    except WorkerFailure as exc:
        remaining()
        tab = created[0] if created else None
        return {
            "command": command,
            "tab_id": tab,
            "workspace_id": client.workspace_id,
            "error": str(exc),
            "output": _capture(client, tab) if tab else "",
        }
    try:
        client.wait_for_shell_completion(tab, remaining())
        result = client.read_shell_result(tab)
    except WorkerFailure as exc:
        try:
            remaining()
        except TimeoutError:
            try:
                client.interrupt(tab)
            except WorkerFailure:
                pass
            raise
        return {
            "command": command,
            "tab_id": tab,
            "workspace_id": client.workspace_id,
            "error": str(exc),
            "output": _capture(client, tab),
        }
    remaining()
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
    except ResultNotReady:
        status = client.read_status(tab)
        outcome["running"] = status.get("alive") is not False
        if status.get("alive") is False:
            outcome["error"] = "start terminal exited without a shell result"
    except WorkerFailure as exc:
        outcome["error"] = str(exc)
        outcome["running"] = False
    else:
        outcome["exit_code"] = result.exit_code
        outcome["running"] = False
    return outcome


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
) -> dict[str, Any]:
    """Run supplied stages in order; return observations and the first failure."""
    stages = ("build", "start", "ready_check")
    if resume_at not in stages:
        raise ValueError(f"invalid Environment Setup resume stage: {resume_at}")
    checks: dict[str, dict[str, object]] = {}
    verification: dict[str, object] | None = None
    failure: str | None = None
    failed_stage: str | None = None
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
        if stage == "start":
            remaining()
            created: list[str] = []
            try:
                service_tab = client.start_shell(
                    ShellCommandRequest(command, cwd, "Environment Setup start"),
                    on_created=lambda tab, _result_path: created.append(tab),
                )
            except WorkerFailure as exc:
                remaining()
                service_tab = created[0] if created else None
                outcome = {
                    "command": command,
                    "tab_id": service_tab,
                    "workspace_id": client.workspace_id,
                    "error": str(exc),
                    "output": _capture(client, service_tab) if service_tab else "",
                }
            else:
                try:
                    remaining()
                except TimeoutError:
                    try:
                        client.interrupt(service_tab)
                    except WorkerFailure:
                        pass
                    raise
                outcome = _service_outcome(client, service_tab, command)
        else:
            outcome = _completed_command(
                client, name=stage, command=command, cwd=cwd, remaining=remaining
            )
        if stage == "ready_check":
            verification = outcome
            if ready_check is not None:
                checks[stage] = outcome
        else:
            checks[stage] = outcome
        if outcome.get("error") or outcome.get("exit_code") not in (None, 0):
            failure = f"Environment Setup {stage} failed: {outcome}"
            failed_stage = stage
            break
    if failure is None and service_tab is not None and start is not None:
        service = _service_outcome(client, service_tab, start)
        checks["start"] = service
        if service.get("error") or service.get("exit_code") not in (None, 0):
            failure = f"Environment Setup start failed: {service}"
            failed_stage = "start"
    remaining()
    return {
        "checks": checks,
        "verification": verification,
        "service_tab": service_tab,
        "failure": failure,
        "failed_stage": failed_stage,
    }
