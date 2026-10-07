"""Declarative Review Fix input and ordinary Python Workflow generation."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ReviewFixStart:
    command: str
    ready_check: str

    def as_json(self) -> dict[str, str]:
        return {"command": self.command, "ready_check": self.ready_check}


@dataclass(frozen=True)
class ReviewFixInput:
    repository: str
    start: ReviewFixStart
    check: str
    max_iterations: int
    review_agent: str = "codex"
    implementation_agent: str = "codex"
    timeout: int = 3600

    def as_json(self) -> dict[str, object]:
        return {
            "mode": "review-fix",
            "repository": self.repository,
            "start": self.start.as_json(),
            "check": self.check,
            "max_iterations": self.max_iterations,
            "review_agent": self.review_agent,
            "implementation_agent": self.implementation_agent,
            "timeout": self.timeout,
        }


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{name} must be a non-empty string without nulls")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{name} must contain only Unicode scalar values")
    return value


def parse_review_fix_json(source: str) -> ReviewFixInput:
    """Validate a Review Fix declaration and resolve its local Git repository."""
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
    required = {"mode", "repository", "start", "check", "max_iterations"}
    optional = {"review_agent", "implementation_agent", "timeout"}
    if missing := required - value.keys():
        raise ValueError(f"missing fields: {', '.join(sorted(missing))}")
    if unknown := value.keys() - required - optional:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    if value["mode"] != "review-fix":
        raise ValueError("mode must be 'review-fix'")

    repository = Path(_text(value["repository"], "repository")).expanduser()
    try:
        repository = repository.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"repository cannot be resolved: {exc}") from exc
    if not repository.is_dir():
        raise ValueError("repository must be a directory")
    try:
        inspected = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"repository cannot be inspected: {exc}") from exc
    if inspected.returncode or Path(inspected.stdout.strip()).resolve() != repository:
        raise ValueError("repository must be a Git repository root")

    start = value["start"]
    if not isinstance(start, dict):
        raise ValueError("start must be an object")
    if set(start) != {"command", "ready_check"}:
        raise ValueError("start must contain only command and ready_check")
    command = _text(start["command"], "start.command")
    ready_check = _text(start["ready_check"], "start.ready_check")
    parsed_ready_check = urlsplit(ready_check)
    if (
        parsed_ready_check.scheme not in {"http", "https"}
        or not parsed_ready_check.hostname
        or parsed_ready_check.username is not None
        or parsed_ready_check.password is not None
        or parsed_ready_check.fragment
    ):
        raise ValueError(
            "start.ready_check must be an HTTP(S) URL without credentials or a fragment"
        )
    try:
        parsed_ready_check.port
    except ValueError as exc:
        raise ValueError("start.ready_check has an invalid port") from exc

    check = _text(value["check"], "check")
    max_iterations = value["max_iterations"]
    if type(max_iterations) is not int or not 1 <= max_iterations <= 50:
        raise ValueError("max_iterations must be an integer from 1 to 50")
    agents = {
        "review_agent": value.get("review_agent", "codex"),
        "implementation_agent": value.get("implementation_agent", "codex"),
    }
    for name, agent in agents.items():
        if agent not in {"codex", "claude-code"}:
            raise ValueError(f"{name} must be codex or claude-code")
    timeout = value.get("timeout", 3600)
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise ValueError("timeout must be an integer from 1 to 86400 seconds")
    return ReviewFixInput(
        str(repository),
        ReviewFixStart(command, ready_check),
        check,
        max_iterations,
        agents["review_agent"],
        agents["implementation_agent"],
        timeout,
    )


def generate_review_fix_workflow(config: ReviewFixInput) -> str:
    """Generate a workflow whose Python owns the complete Review Fix loop."""
    return f"""import json
import shlex
import sys
import time

from purplemux_client import CreateSessionRequest, CreateWorkspaceRequest, PurpleMuxRuntime, ShellCommandRequest, emit_step
from purplemux_client.errors import MutationOutcomeUnknown, ResultNotReady, SessionReadyTimeout, WorkerFailure, WorkerInterrupted
from purplemux_client.review import ReviewInput, generate_review_workflow, validate_review_result
from purplemux_client.workflow import start_child_run, wait_child_run

WORKFLOW_OUTLINE = ["Start service", "Review Fix"]
REPOSITORY = {config.repository!r}
START_COMMAND = {config.start.command!r}
READY_URL = {config.start.ready_check!r}
CHECK = {config.check!r}
MAX_ITERATIONS = {config.max_iterations}
REVIEW_AGENT = {config.review_agent!r}
IMPLEMENTATION_AGENT = {config.implementation_agent!r}
TIMEOUT = {config.timeout}

deadline = time.monotonic() + TIMEOUT
runtime = PurpleMuxRuntime(owned_by_run=True)
client = None
service_tab = None
implementation_tab = None
readiness_tabs = []
iterations = []
workspace = None
result = None


def remaining():
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Review Fix timed out")
    return seconds


def close_tab(tab):
    if client is None or tab is None:
        return
    client.close_session(tab)


def start_service():
    global service_tab
    service_tab = client.start_shell(ShellCommandRequest(
        START_COMMAND, REPOSITORY, "Review Fix service", deadline_check=remaining,
    ))


def establish_readiness():
    probe = (shlex.quote(sys.executable) + " -c "
             + shlex.quote("import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=5).read(1)")
             + " " + shlex.quote(READY_URL))
    last = "readiness was not observed"
    attempt = 0
    while True:
        attempt += 1
        tab = client.start_shell(ShellCommandRequest(
            probe, REPOSITORY, "Review Fix readiness " + str(attempt),
            deadline_check=remaining, max_output_chars=4096,
        ))
        readiness_tabs.append(tab)
        try:
            client.wait_for_shell_completion(tab, min(remaining(), 10))
            shell_result = client.read_shell_result(tab)
            if shell_result.exit_code == 0:
                close_tab(tab)
                readiness_tabs.remove(tab)
                return {{"url": READY_URL, "attempts": attempt}}
            last = shell_result.failure_message("Review Fix readiness")
        except (TimeoutError, WorkerFailure) as exc:
            if isinstance(exc, (WorkerInterrupted, MutationOutcomeUnknown)):
                raise
            last = str(exc)
        finally:
            if tab in readiness_tabs:
                try:
                    close_tab(tab)
                    readiness_tabs.remove(tab)
                except BaseException:
                    pass
        try:
            status = client.read_status(service_tab)
        except WorkerFailure as exc:
            raise RuntimeError("service state became unavailable: " + str(exc)) from exc
        if status.get("alive") is False:
            try:
                service_result = client.read_shell_result(service_tab)
                detail = service_result.failure_message("Review Fix service")
            except (ResultNotReady, WorkerFailure) as exc:
                detail = str(exc)
            raise RuntimeError("service exited before readiness: " + detail)
        if deadline - time.monotonic() <= 0:
            raise TimeoutError("service readiness timed out: " + last)
        time.sleep(min(0.5, max(deadline - time.monotonic(), 0)))


def review(iteration):
    review_timeout = max(1, min(int(remaining()), 86400))
    review_code = generate_review_workflow(ReviewInput(
        (REPOSITORY,), CHECK, agent=REVIEW_AGENT, timeout=review_timeout,
    ))
    run_id = start_child_run(review_code)
    child = wait_child_run(run_id, timeout=remaining())
    if child.state != "success" or child.exit_code != 0:
        raise RuntimeError("Review child Run failed: " + child.stderr[-4096:])
    try:
        report = validate_review_result(json.loads(child.stdout))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Review child Run returned an invalid result") from exc
    compact = {{"verdict": report["verdict"], "summary": report["summary"][:4096]}}
    for name in ("findings", "observed_facts", "evidence", "hypotheses", "observability_gaps"):
        if name in report:
            compact[name] = [item[:512] for item in report[name][:5]]
    iterations.append({{"iteration": iteration, "review_run_id": run_id, "review": compact}})
    return report


def implement(iteration, report):
    global implementation_tab
    implementation_tab = client.create_session(CreateSessionRequest(
        worker=IMPLEMENTATION_AGENT, cwd=REPOSITORY, command=IMPLEMENTATION_AGENT,
        name="Review Fix implementation " + str(iteration), deadline_check=remaining,
    ))
    client.wait_until_ready(implementation_tab, min(remaining(), 60))
    prompt = (
        "You are the repository-modifying implementation role. Fix only the failures "
        "identified by this read-only Review report, in " + REPOSITORY + ". "
        "Inspect and modify the repository, run focused checks, and do not merely describe a fix. "
        "Do not start or control the Review role. Review evidence: " + json.dumps(report)
    )
    client.send_input(implementation_tab, prompt)
    client.wait_for_turn_completion(implementation_tab, remaining())
    implementation_result = client.read_result(implementation_tab)
    iterations[-1]["implementation"] = implementation_result[-4096:]
    close_tab(implementation_tab)
    implementation_tab = None


emit_step("Start service", "started")
try:
    workspace = runtime.create_workspace(CreateWorkspaceRequest(
        cwd=REPOSITORY, name="AWM Review Fix", deadline_check=remaining,
    ))
    client = runtime.workspace(workspace.id)
    start_service()
    readiness = establish_readiness()
except BaseException as exc:
    emit_step("Start service", "failed", error=str(exc))
    if isinstance(exc, (WorkerInterrupted, MutationOutcomeUnknown)):
        raise
    result = {{
        "verdict": "BLOCKED", "summary": "Service readiness was not established: " + str(exc),
        "repository": REPOSITORY, "iterations": iterations,
    }}
else:
    emit_step("Start service", "completed", workspace=workspace.id, tab=service_tab)
    emit_step("Review Fix", "started")
    try:
        for iteration in range(1, MAX_ITERATIONS + 1):
            report = review(iteration)
            if report["verdict"] in ("PASS", "BLOCKED"):
                result = {{
                    "verdict": report["verdict"], "summary": report["summary"],
                    "repository": REPOSITORY, "iterations": iterations,
                    "readiness": readiness,
                }}
                break
            if iteration == MAX_ITERATIONS:
                result = {{
                    "verdict": "FAIL",
                    "summary": "Review still failed after max_iterations",
                    "repository": REPOSITORY, "iterations": iterations,
                    "readiness": readiness,
                }}
                break
            implement(iteration, report)
        emit_step("Review Fix", "completed", workspace=workspace.id)
    except Exception as exc:
        if isinstance(exc, (WorkerInterrupted, MutationOutcomeUnknown)):
            emit_step("Review Fix", "failed", error=str(exc))
            raise
        result = {{
            "verdict": "BLOCKED", "summary": "Review Fix could not continue: " + str(exc),
            "repository": REPOSITORY, "iterations": iterations,
            "readiness": readiness,
        }}
        emit_step("Review Fix", "completed", workspace=workspace.id)
finally:
    cleanup_errors = []
    for cleanup_tab in [implementation_tab, *readiness_tabs, service_tab]:
        if cleanup_tab is None:
            continue
        try:
            close_tab(cleanup_tab)
        except BaseException as exc:
            cleanup_errors.append(str(exc))
    if cleanup_errors:
        if result is not None:
            result["cleanup_errors"] = cleanup_errors
        if result is not None and result["verdict"] == "PASS":
            result["verdict"] = "BLOCKED"
            result["summary"] = "Review passed but managed service cleanup was not confirmed"

print(json.dumps(result))
"""
