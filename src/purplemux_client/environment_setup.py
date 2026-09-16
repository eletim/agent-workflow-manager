"""Declarative Environment Setup inputs and Python Workflow generation."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
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
    """Keep one complete JSON value within the runner's stdout retention limit."""
    max_chars = 999_999  # Leave one character for print's newline.
    payload = json.dumps(result)
    if len(payload) <= max_chars:
        return payload

    def trim_history(candidate: dict[str, Any], limit: int) -> None:
        omitted: dict[str, int] = {}
        attempts = result.get("attempts", [])
        if len(attempts) > limit:
            candidate["attempts"] = deepcopy(attempts[-limit:])
            omitted["attempts"] = len(attempts) - limit
        facts = result.get("observed_facts")
        if isinstance(facts, dict):
            reports = facts.get("agent_reports", [])
            if len(reports) > limit:
                candidate["observed_facts"]["agent_reports"] = deepcopy(
                    reports[-limit:]
                )
                omitted["agent_reports"] = len(reports) - limit
        if omitted:
            candidate["history_truncated"] = omitted

    def trim_logs(value: Any, limit: int) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "output" and isinstance(item, str) and len(item) > limit:
                    value[key] = item[-limit:] if limit else ""
                else:
                    trim_logs(item, limit)
        elif isinstance(value, list):
            for item in value:
                trim_logs(item, limit)

    for history_limit, log_limit in ((32, 4096), (8, 1024), (1, 0)):
        candidate = deepcopy(result)
        trim_history(candidate, history_limit)
        for field in ("attempts", "checks", "process", "verification"):
            trim_logs(candidate.get(field), log_limit)
        payload = json.dumps(candidate)
        if len(payload) <= max_chars:
            return payload

    def outcome_details(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {
            key: item if not isinstance(item, str) else item[:4096]
            for key, item in value.items()
            if key
            in {
                "command",
                "tab_id",
                "workspace_id",
                "exit_code",
                "running",
                "error",
                "provenance",
            }
        }

    last_attempt = result.get("attempts", [])[-1:]
    compact_attempts = []
    for attempt in last_attempt:
        compact_attempts.append(
            {
                "checks": {
                    stage: outcome_details(outcome)
                    for stage, outcome in attempt.get("checks", {}).items()
                },
                "verification": outcome_details(attempt.get("verification")),
                "service_tab": attempt.get("service_tab"),
                "failed_stage": attempt.get("failed_stage"),
                "failure": str(attempt.get("failure"))[:4096]
                if attempt.get("failure")
                else None,
            }
        )
    facts = result.get("observed_facts", {})
    fallback = {
        "status": result["status"],
        "summary": str(result.get("summary", ""))[:4096],
        "execution_summary": str(result.get("execution_summary", ""))[:4096],
        "readiness_summary": str(result.get("readiness_summary", ""))[:4096],
        "endpoint_report_error": str(result.get("endpoint_report_error", ""))[:4096]
        if result.get("endpoint_report_error")
        else None,
        "resolved_revision": result.get("resolved_revision"),
        "working_path": result.get("working_path"),
        "connection": deepcopy(result.get("connection", {})),
        "process": outcome_details(result.get("process")),
        "service_tab": result.get("service_tab"),
        "checks": {
            stage: outcome_details(outcome)
            for stage, outcome in result.get("checks", {}).items()
        },
        "verification": outcome_details(result.get("verification")),
        "attempts": compact_attempts,
        "observed_facts": {
            "error": str(facts.get("error", ""))[:4096],
            "failed_stage": facts.get("failed_stage"),
            "agent_reports": [
                {
                    "status": str(report.get("status", ""))[:64],
                    "summary": str(report.get("summary", ""))[:4096],
                }
                for report in facts.get("agent_reports", [])[-1:]
            ],
        }
        if facts
        else None,
        "history_truncated": {
            "attempts": max(0, len(result.get("attempts", [])) - 1),
            "agent_reports": max(0, len(facts.get("agent_reports", [])) - 1),
        },
    }

    def bound_fields(value: Any) -> Any:
        if isinstance(value, str):
            return value[:4096]
        if isinstance(value, list):
            return [bound_fields(item) for item in value[-4:]]
        if isinstance(value, dict):
            return {
                str(key)[:128]: bound_fields(item)
                for key, item in list(value.items())[:24]
            }
        return value

    fallback = bound_fields(fallback)
    # Preserve usable connection details and paths exactly when they fit.
    fallback["working_path"] = result.get("working_path")
    fallback["connection"] = deepcopy(result.get("connection", {}))
    payload = json.dumps(fallback)
    if len(payload) > max_chars:
        omitted = []
        for key, value in list(fallback["connection"].items()):
            if len(payload) <= max_chars:
                break
            if isinstance(value, str) and len(value) > 4096:
                fallback["connection"].pop(key)
                omitted.append(key)
                payload = json.dumps(fallback)
        if omitted:
            fallback["connection_details_omitted"] = omitted
            payload = json.dumps(fallback)
    if len(payload) > max_chars and isinstance(fallback["working_path"], str):
        fallback["working_path"] = None
        fallback["working_path_error"] = "Path exceeded the result size limit"
        payload = json.dumps(fallback)
    return payload


def verify_environment_setup_revision(
    working_path: str, expected_sha: str, remaining: Callable[[], float]
) -> None:
    """Reject readiness when the prepared worktree no longer has its selected HEAD."""
    completed = subprocess.run(
        ["git", "-C", str(Path(working_path)), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        timeout=remaining(),
        check=False,
    )
    remaining()
    if completed.returncode != 0:
        raise RuntimeError(
            f"Environment Setup cannot verify working path HEAD: {completed.stderr.strip()}"
        )
    actual_sha = completed.stdout.strip().lower()
    if actual_sha != expected_sha.lower():
        raise RuntimeError(
            f"Environment Setup working path HEAD changed: expected {expected_sha}, "
            f"found {actual_sha}"
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
from purplemux_client.environment_setup_execution import (
    execute_environment_setup_commands,
    verify_detached_service_provenance,
)
from purplemux_client.environment_setup import (
    serialize_environment_setup_result,
    verify_environment_setup_revision,
)

WORKFLOW_OUTLINE = ["Environment Setup"]
REVISION_VALIDATION = {config.revision_validation!r}


def remaining():
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Environment Setup timed out")
    return seconds


def busy_timeout(_warning):
    raise TimeoutError("Environment Setup timed out while the agent was busy")


def ask_agent(message, turn_limit=None):
    global turn_active
    remaining()
    client.send_input(tab, message)
    turn_active = True
    client.wait_for_turn_completion(
        tab, min(remaining(), turn_limit) if turn_limit is not None else remaining(),
        on_busy_timeout=busy_timeout,
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
endpoint_report_error = None
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
    if report.get("status") != "READY" and not any(
        ({config.build!r}, {config.start!r}, {config.ready_check!r})
    ):
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
    try:
        endpoint_report = ask_agent(
            "The managed commands and usability check succeeded. Inspect their "
            "observed output and the running service when present. Return one "
            "JSON object with status READY or BLOCKED and a non-empty summary. "
            "Report BLOCKED if the service is no longer usable. Include endpoint "
            "only if you observed a usable connection address; no endpoint is "
            "needed for READY. "
            "Do not infer an address from configuration alone. Command "
            "observations: "
            + json.dumps({{"checks": checks, "verification": verification}}),
            turn_limit=30,
        )
    except Exception as endpoint_error:
        endpoint_report_error = f"Endpoint inspection failed: {{str(endpoint_error)[:4096]}}"
        if turn_active:
            try:
                client.interrupt(tab)
            except Exception as interruption:
                raise RuntimeError(
                    f"Environment Setup final agent turn failed: {{endpoint_error}}; "
                    f"agent interruption failed: {{interruption}}"
                ) from interruption
            turn_active = False
        if report.get("status") != "READY":
            raise RuntimeError(
                f"Environment Setup agent blocked: {{report['summary']}}; "
                f"{{endpoint_report_error}}"
            )
    else:
        endpoint_status = endpoint_report.get("status")
        if endpoint_status == "BLOCKED":
            agent_reports.append(endpoint_report)
            raise RuntimeError(
                f"Environment Setup agent blocked: {{endpoint_report['summary']}}"
            )
        if endpoint_status == "READY":
            agent_reports.append(endpoint_report)
            report = endpoint_report
        else:
            endpoint_report_error = "Environment Setup final agent returned an invalid status"
            if report.get("status") != "READY":
                raise RuntimeError(
                    f"Environment Setup agent blocked: {{report['summary']}}; "
                    f"{{endpoint_report_error}}"
                )
    verify_environment_setup_revision(cwd, context.base_sha, remaining)
    detached = checks.get("start", {{}}).get("provenance")
    if detached is not None:
        verify_detached_service_provenance(cwd, detached)
    result = {{
        "status": "READY",
        "summary": report["summary"],
        "execution_summary": "All supplied commands completed successfully.",
        "readiness_summary": "The usability check succeeded.",
    }}
    if endpoint_report_error is not None:
        result["endpoint_report_error"] = endpoint_report_error
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
last_report = agent_reports[-1] if agent_reports and result["status"] == "READY" else None
if last_report is not None:
    endpoint = last_report.get("endpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        result["connection"]["endpoint"] = endpoint.strip()
print(serialize_environment_setup_result(result))
"""
