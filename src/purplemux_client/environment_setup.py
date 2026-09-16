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


def serialize_environment_setup_result(result: dict[str, Any]) -> str:
    """Keep the single JSON result below the runner's stdout retention limit."""

    def compact(value: Any, string_limit: int, list_limit: int, depth: int = 0) -> Any:
        if isinstance(value, str):
            return value if len(value) <= string_limit else value[:string_limit] + "…"
        if depth >= 6:
            return "…"
        if isinstance(value, list):
            return [
                compact(item, string_limit, list_limit, depth + 1)
                for item in value[-list_limit:]
            ]
        if isinstance(value, dict):
            return {
                str(key)[:128]: compact(item, string_limit, list_limit, depth + 1)
                for key, item in list(value.items())[:24]
            }
        return value

    for string_limit, list_limit in ((2048, 8), (512, 4), (128, 1)):
        candidate = compact(result, string_limit, list_limit)
        omitted: dict[str, int] = {}
        attempts = result.get("attempts")
        if isinstance(attempts, list) and len(attempts) > list_limit:
            omitted["attempts"] = len(attempts) - list_limit
        facts = result.get("observed_facts")
        if isinstance(facts, dict):
            reports = facts.get("agent_reports")
            if isinstance(reports, list) and len(reports) > list_limit:
                omitted["agent_reports"] = len(reports) - list_limit
        if omitted:
            candidate["history_truncated"] = omitted
        payload = json.dumps(candidate)
        if len(payload) < 100_000:
            return payload
    return json.dumps(
        {
            "status": result["status"],
            "summary": str(result.get("summary", ""))[:512],
            "resolved_revision": result.get("resolved_revision"),
            "working_path": str(result.get("working_path"))[:512]
            if result.get("working_path")
            else None,
            "connection": compact(result.get("connection", {}), 512, 1),
            "observed_facts": {
                "error": "Detailed observations exceeded the result size limit"
            },
        }
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
        "You may make temporary setup changes in the execution directory. "
        "Report BLOCKED if readiness requires a permanent product fix or "
        "the environment cannot be prepared.",
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
            "from observed command outcomes. Include an endpoint only if you "
            "have observed a usable connection address. Include errors in a "
            "BLOCKED summary.",
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
from purplemux_client.environment_setup import serialize_environment_setup_result

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
context = None
workspace = None
cwd = None
checks = {{}}
verification = None
service_tab = None
attempts = []
agent_reports = []
report = None
result = None
current_attempt = None
try:
    deadline = time.monotonic() + {config.timeout}
    context = prepare_run_revision(
        repo={config.repository!r}, revision={config.revision!r},
        deadline_check=remaining,
    )
    cwd = str(context.execution_root)
    remaining()
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
    agent_reports.append(report)
    if report.get("status") != "READY":
        raise RuntimeError(f"Environment Setup agent blocked: {{report['summary']}}")
    resume_at = "build"
    while True:
        current_attempt = {{}}
        attempts.append(current_attempt)
        attempt = execute_environment_setup_commands(
            client=client, build={config.build!r}, start={config.start!r},
            ready_check={config.ready_check!r},
            verification_command=report.get("verification_command"),
            cwd=cwd, remaining=remaining, resume_at=resume_at,
            service_tab=service_tab,
            observation=current_attempt,
        )
        checks.update(attempt["checks"])
        service_tab = attempt["service_tab"]
        verification = attempt["verification"]
        if attempt["failure"] is None:
            current_attempt = None
            break
        recovery_prompt = (
            "Execution failed or readiness was not reached after the required "
            "first attempt. Inspect repository files and managed terminal logs, "
            "including the failed command output and the start terminal if present. "
            "Make necessary temporary environment or setup changes in the "
            "execution directory, then report READY so the workflow can retry "
            "the failed stage within the timeout. Report BLOCKED if readiness "
            "requires a permanent product fix or the environment cannot be "
            "repaired. Do not change product code to conceal a product failure. "
            "Return one JSON object "
            "with status, non-empty summary, verification_command if no "
            "ready_check was supplied, and endpoint only if you observed a "
            "usable connection address. Failure observations: "
            + json.dumps(attempt)
        )
        report = ask_agent(recovery_prompt)
        agent_reports.append(report)
        if report.get("status") != "READY":
            raise RuntimeError(f"Environment Setup agent blocked: {{report['summary']}}; {{attempt['failure']}}")
        resume_at = attempt["failed_stage"]
    remaining()
    report = ask_agent(
        "The managed commands and usability check succeeded. Inspect their "
        "observed output and the running service when present. Return one JSON "
        "object with status READY, a non-empty summary, and endpoint only if "
        "you observed a usable connection address. Do not infer an address "
        "from configuration alone. Command observations: "
        + json.dumps({{"checks": checks, "verification": verification}})
    )
    agent_reports.append(report)
    if report.get("status") != "READY":
        raise RuntimeError(f"Environment Setup agent blocked: {{report['summary']}}")
    remaining()
    result = {{
        "status": "READY",
        "summary": report["summary"],
        "execution_summary": "All supplied commands completed successfully.",
        "readiness_summary": "The usability check succeeded.",
    }}
except BaseException as exc:
    if current_attempt is not None:
        checks.update(current_attempt.get("checks", {{}}))
        service_tab = current_attempt.get("service_tab", service_tab)
        verification = current_attempt.get("verification", verification)
        if current_attempt.get("failure") is None:
            current_attempt["failure"] = str(exc)
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
    result = {{
        "status": "BLOCKED",
        "summary": error,
        "execution_summary": (
            attempts[-1]["failure"] if attempts and attempts[-1]["failure"]
            else "No failed command outcome was observed."
        ),
        "readiness_summary": "Readiness was not established.",
        "observed_facts": {{
            "error": error,
            "failed_stage": attempts[-1]["failed_stage"] if attempts else None,
            "agent_reports": agent_reports,
        }},
    }}
else:
    emit_step("Environment Setup", "completed", workspace=workspace.id, tab=tab)
result.update({{
    "resolved_revision": context.base_sha if context is not None else None,
    "working_path": cwd,
    "connection": {{
        "workspace_id": workspace.id if workspace is not None else None,
        "agent_tab_id": tab,
    }},
    "process": checks.get("start"),
    "service_tab": service_tab,
    "checks": checks,
    "verification": verification,
    "attempts": attempts,
}})
last_report = agent_reports[-1] if agent_reports else None
if last_report is not None:
    endpoint = last_report.get("endpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        result["connection"]["endpoint"] = endpoint.strip()
print(serialize_environment_setup_result(result))
"""
