"""Declarative Environment Setup inputs and Python Workflow generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from purplemux_client.errors import WorkerFailure
from purplemux_client.execution_context import inspect_run_revision

_REQUIRED = {"mode", "repository", "revision", "environment_agent", "timeout"}
_OPTIONAL = {"build", "start", "ready_check"}
_AGENTS = {"codex", "claude-code"}


@dataclass(frozen=True)
class EnvironmentSetupInput:
    repository: str
    revision: str
    environment_agent: str
    timeout: int
    build: str | None = None
    start: str | None = None
    ready_check: str | None = None
    revision_validation: str = "verified"

    def as_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "mode": "environment-setup",
            "repository": self.repository,
            "revision": self.revision,
            "environment_agent": self.environment_agent,
            "timeout": self.timeout,
        }
        for name in ("build", "start", "ready_check"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


def parse_environment_setup_json(source: str) -> EnvironmentSetupInput:
    """Validate one Environment Setup declaration and its remote revision."""
    if not isinstance(source, str):
        raise ValueError("source must be a JSON string")
    duplicates: set[str] = set()

    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicates.add(key)
            result[key] = value
        return result

    try:
        value = json.loads(source, object_pairs_hook=object_from_pairs)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("top-level value must be an object")
    if duplicates:
        raise ValueError(f"duplicate fields: {', '.join(sorted(duplicates))}")
    missing = _REQUIRED - value.keys()
    unknown = value.keys() - _REQUIRED - _OPTIONAL
    if missing:
        raise ValueError(f"missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    if value["mode"] != "environment-setup":
        raise ValueError("mode must be 'environment-setup'")
    for name in ("repository", "revision", "environment_agent"):
        item = value[name]
        if not isinstance(item, str) or not item.strip() or "\0" in item:
            raise ValueError(f"{name} must be a non-empty string without nulls")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in item):
            raise ValueError(f"{name} must contain only Unicode scalar values")
    for name in _OPTIONAL & value.keys():
        item = value[name]
        if not isinstance(item, str) or not item.strip() or "\0" in item:
            raise ValueError(f"{name} must be a non-empty string without nulls")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in item):
            raise ValueError(f"{name} must contain only Unicode scalar values")
    if value["environment_agent"] not in _AGENTS:
        raise ValueError("environment_agent must be codex or claude-code")
    timeout = value["timeout"]
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise ValueError("timeout must be an integer from 1 to 86400 seconds")
    try:
        preparation, _kind = inspect_run_revision(
            repo=value["repository"], revision=value["revision"]
        )
    except (ValueError, WorkerFailure) as exc:
        raise ValueError(f"repository/revision: {exc}") from exc
    return EnvironmentSetupInput(
        repository=str(preparation.source_repository),
        revision=value["revision"],
        environment_agent=value["environment_agent"],
        timeout=timeout,
        build=value.get("build"),
        start=value.get("start"),
        ready_check=value.get("ready_check"),
        revision_validation=preparation.revision_validation,
    )


def generate_environment_setup_workflow(config: EnvironmentSetupInput) -> str:
    """Generate a plain Python Workflow from validated declarative inputs."""
    instructions = [
        "Set up the repository at the selected revision for development.",
        "Work only in the supplied execution directory.",
        "Prepare prerequisites for the supplied commands. The workflow will run "
        "each supplied build, start, and ready_check command exactly as given, "
        "in that order, before considering any alternative. Do not run or "
        "substitute these commands yourself. Skip instructions that were omitted. "
        "Report BLOCKED if the environment cannot be prepared.",
    ]
    for label, command in (
        ("Build", config.build),
        ("Start", config.start),
        ("Ready check", config.ready_check),
    ):
        if command is not None:
            instructions.append(f"{label} command: {command}")
    instructions.extend(
        (
            "If no ready_check was supplied, include a non-empty "
            "verification_command that exercises the target's intended use; "
            "a trivial always-successful command is insufficient. The workflow "
            "will execute it after the supplied commands, even if all were omitted.",
            "Return only one JSON object with status READY or BLOCKED, a non-empty "
            "summary, and verification_command when required. READY means "
            "preparation is complete; the workflow will decide final readiness "
            "from observed command outcomes. Include errors in a BLOCKED summary.",
        )
    )
    prompt = "\n".join(instructions)
    return f"""import json
import time

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    PurpleMuxRuntime,
    emit_step,
    prepare_run_revision,
)
from purplemux_client.environment_setup_execution import execute_environment_setup_commands

WORKFLOW_OUTLINE = ["Environment Setup"]
REVISION_VALIDATION = {config.revision_validation!r}


def remaining():
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Environment Setup timed out")
    return seconds


def busy_timeout(_warning):
    raise TimeoutError("Environment Setup timed out while the agent was busy")


def ask_agent(message):
    global turn_active
    remaining()
    client.send_input(tab, message)
    turn_active = True
    client.wait_for_turn_completion(
        tab, remaining(), on_busy_timeout=busy_timeout
    )
    turn_active = False
    remaining()
    response = json.loads(client.read_result(tab))
    remaining()
    if not isinstance(response, dict) or not isinstance(response.get("summary"), str) or not response["summary"].strip():
        raise RuntimeError("Environment Setup agent returned an invalid report")
    return response


emit_step("Environment Setup", "started")
client = None
tab = None
turn_active = False
try:
    deadline = time.monotonic() + {config.timeout}
    context = prepare_run_revision(
        repo={config.repository!r}, revision={config.revision!r},
        deadline_check=remaining,
    )
    remaining()
    cwd = str(context.execution_root)
    runtime = PurpleMuxRuntime(owned_by_run=True)
    workspace = runtime.create_workspace(
        CreateWorkspaceRequest(
            cwd=cwd, name="AWM Environment Setup", deadline_check=remaining
        )
    )
    remaining()
    client = runtime.workspace(workspace.id)
    tab = client.create_session(
        CreateSessionRequest(
            worker={config.environment_agent!r},
            cwd=cwd,
            command={config.environment_agent!r},
            deadline_check=remaining,
        )
    )
    remaining()
    client.wait_until_ready(tab, min(remaining(), 60))
    report = ask_agent({prompt!r})
    if report.get("status") != "READY":
        raise RuntimeError(f"Environment Setup agent blocked: {{report['summary']}}")
    checks = {{}}
    resume_at = "build"
    service_tab = None
    while True:
        attempt = execute_environment_setup_commands(
            client=client, build={config.build!r}, start={config.start!r},
            ready_check={config.ready_check!r},
            verification_command=report.get("verification_command"),
            cwd=cwd, remaining=remaining, resume_at=resume_at,
            service_tab=service_tab,
        )
        checks.update(attempt["checks"])
        service_tab = attempt["service_tab"]
        if attempt["failure"] is None:
            verification = attempt["verification"]
            break
        recovery_prompt = (
            "The supplied command failed after its required first attempt. "
            "Inspect the repository, README, scripts, configuration, and the "
            "managed terminal logs. Repair the environment, then report READY "
            "so the workflow can retry the failed command within the timeout; "
            "report BLOCKED if repair is impossible. Return one JSON object "
            "with status, non-empty summary, and verification_command if no "
            "ready_check was supplied. Failure observations: "
            + json.dumps(attempt)
        )
        report = ask_agent(recovery_prompt)
        if report.get("status") != "READY":
            raise RuntimeError(f"Environment Setup agent blocked: {{report['summary']}}; {{attempt['failure']}}")
        resume_at = attempt["failed_stage"]
    remaining()
    print(json.dumps({{
        "status": "READY",
        "summary": report["summary"],
        "checks": checks,
        "verification": verification,
        "service_tab": service_tab,
        "resolved_revision": context.base_sha,
        "working_path": cwd,
    }}))
except BaseException as exc:
    interrupt_error = None
    if isinstance(exc, TimeoutError) and turn_active and client is not None and tab is not None:
        try:
            client.interrupt(tab)
        except BaseException as interruption:
            interrupt_error = str(interruption)
    error = str(exc)
    if interrupt_error is not None:
        error = f"{{error}}; agent interruption failed: {{interrupt_error}}"
    emit_step("Environment Setup", "failed", error=error)
    raise
else:
    emit_step("Environment Setup", "completed", workspace=workspace.id, tab=tab)
"""
