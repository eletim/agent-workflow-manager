"""Declarative Review Fix input and ordinary Python Workflow generation."""

from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from purplemux_client.errors import WorkerFailure
from purplemux_client.git import GitRepository


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
        GitRepository.open(repository, command_timeout_seconds=10)
    except (OSError, RuntimeError, subprocess.SubprocessError, WorkerFailure) as exc:
        raise ValueError(f"repository must be a GitHub repository root: {exc}") from exc

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


def serialize_review_fix_result(result: dict[str, Any]) -> str:
    """Keep the terminal Review Fix JSON value within stdout retention."""
    max_chars = 999_999
    payload = json.dumps(result, ensure_ascii=False)
    if len(payload) <= max_chars:
        return payload

    iterations = result.get("iterations", [])
    for history_limit, array_limit, text_limit in (
        (50, 5, 2048),
        (20, 3, 1024),
        (8, 2, 512),
        (1, 1, 256),
    ):
        candidate: dict[str, Any] = {
            "verdict": result["verdict"],
            "summary": str(result.get("summary", ""))[:4096],
            "repository": result.get("repository"),
            "iterations": [],
        }
        if "readiness" in result:
            candidate["readiness"] = deepcopy(result["readiness"])
        if "cleanup_errors" in result:
            candidate["cleanup_errors"] = [
                str(item)[:512] for item in result["cleanup_errors"][:20]
            ]
        if len(iterations) > history_limit:
            candidate["iterations_omitted"] = len(iterations) - history_limit
        for item in iterations[-history_limit:]:
            compact: dict[str, Any] = {
                key: item[key]
                for key in ("iteration", "review_run_id", "implementation_sha")
                if key in item
            }
            review = item.get("review")
            if isinstance(review, dict):
                compact_review: dict[str, Any] = {
                    "verdict": review.get("verdict"),
                    "summary": str(review.get("summary", ""))[:text_limit],
                }
                for name in (
                    "findings",
                    "observed_facts",
                    "evidence",
                    "hypotheses",
                    "observability_gaps",
                ):
                    if isinstance(review.get(name), list):
                        compact_review[name] = [
                            str(value)[:text_limit]
                            for value in review[name][:array_limit]
                        ]
                compact["review"] = compact_review
            if "implementation" in item:
                compact["implementation"] = str(item["implementation"])[-text_limit:]
            if "readiness" in item:
                compact["readiness"] = deepcopy(item["readiness"])
            candidate["iterations"].append(compact)
        payload = json.dumps(candidate, ensure_ascii=False)
        if len(payload) <= max_chars:
            return payload

    fallback = {
        "verdict": result["verdict"],
        "summary": str(result.get("summary", ""))[:4096],
        "repository": result.get("repository"),
        "iterations": [],
        "iterations_omitted": len(iterations),
    }
    return json.dumps(fallback, ensure_ascii=False)


def validate_review_fix_result(value: Any) -> dict[str, Any]:
    """Validate a terminal Review Fix result before it enters durable history."""
    if not isinstance(value, dict):
        raise ValueError("Review Fix result must be an object")
    if value.get("verdict") not in {"PASS", "FAIL", "BLOCKED"}:
        raise ValueError("invalid Review Fix verdict")
    if not isinstance(value.get("summary"), str) or not isinstance(
        value.get("repository"), str
    ):
        raise ValueError("invalid Review Fix summary or repository")
    iterations = value.get("iterations")
    if not isinstance(iterations, list) or any(
        not isinstance(item, dict) for item in iterations
    ):
        raise ValueError("invalid Review Fix iterations")
    cleanup_errors = value.get("cleanup_errors", [])
    if not isinstance(cleanup_errors, list) or any(
        not isinstance(item, str) for item in cleanup_errors
    ):
        raise ValueError("invalid Review Fix cleanup errors")
    omitted = value.get("iterations_omitted")
    if omitted is not None and (
        isinstance(omitted, bool) or not isinstance(omitted, int) or omitted < 1
    ):
        raise ValueError("invalid omitted Review Fix iteration count")
    if value.keys() - {
        "verdict",
        "summary",
        "repository",
        "iterations",
        "readiness",
        "cleanup_errors",
        "iterations_omitted",
    }:
        raise ValueError("unknown Review Fix result fields")
    return value


def publish_review_fix_result(value: dict[str, Any]) -> None:
    """Send the Review Fix outcome to its Run independently of stdout."""
    from purplemux_client.workflow import CONTROL_TOKEN_ENV, CONTROL_URL_ENV, _control

    validate_review_fix_result(value)
    if CONTROL_URL_ENV not in os.environ and CONTROL_TOKEN_ENV not in os.environ:
        return
    _control("review_fix_result", result=value)


def generate_review_fix_workflow(config: ReviewFixInput) -> str:
    """Generate a workflow whose Python owns the complete Review Fix loop."""
    return f"""import json
import shlex
import signal
import sys
import time

from purplemux_client import CreateSessionRequest, CreateWorkspaceRequest, GitRepository, PurpleMuxRuntime, ShellCommandRequest, agent_commit_coauthor, emit_step
from purplemux_client.errors import MutationOutcomeUnknown, ResultNotReady, SessionReadyTimeout, WorkerFailure, WorkerInterrupted
from purplemux_client.review import ReviewInput, generate_review_workflow, validate_review_result
from purplemux_client.review_fix import publish_review_fix_result, serialize_review_fix_result
from purplemux_client.workflow import start_child_run, stop_child_run, wait_child_run

WORKFLOW_OUTLINE = ["Start service", "Review Fix"]
REPOSITORY = {config.repository!r}
START_COMMAND = {config.start.command!r}
READY_URL = {config.start.ready_check!r}
CHECK = {config.check!r}
MAX_ITERATIONS = {config.max_iterations}
REVIEW_AGENT = {config.review_agent!r}
IMPLEMENTATION_AGENT = {config.implementation_agent!r}
COMMIT_AGENT = "claude" if IMPLEMENTATION_AGENT == "claude-code" else IMPLEMENTATION_AGENT
TIMEOUT = {config.timeout}
CLEANUP_GRACE_SECONDS = 5

deadline = time.monotonic() + TIMEOUT
runtime = PurpleMuxRuntime(owned_by_run=True)
client = None
service_tab = None
implementation_tab = None
readiness_tabs = []
iterations = []
workspace = None
result = None
active_review_run = None


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


def restart_service():
    global service_tab
    close_tab(service_tab)
    service_tab = None
    require_endpoint_absent()
    start_service()
    return establish_readiness()


def stop_active_review():
    global active_review_run
    if active_review_run is None:
        return
    run_id = active_review_run
    stop_child_run(
        run_id,
        timeout=max(CLEANUP_GRACE_SECONDS, deadline - time.monotonic()),
    )
    active_review_run = None


def stop_workflow(signum, _frame):
    try:
        stop_active_review()
    finally:
        raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, stop_workflow)


def require_service_alive():
    try:
        status = client.read_status(service_tab)
    except WorkerFailure as exc:
        raise RuntimeError("service state became unavailable: " + str(exc)) from exc
    if status.get("alive") is True:
        return
    if status.get("alive") is False:
        try:
            service_result = client.read_shell_result(service_tab)
            detail = service_result.failure_message("Review Fix service")
        except (ResultNotReady, WorkerFailure) as exc:
            detail = str(exc)
        raise RuntimeError("service exited before readiness: " + detail)
    raise RuntimeError("service state did not confirm that the start command is alive")


def require_endpoint_absent():
    probe = (shlex.quote(sys.executable) + " -c "
             + shlex.quote("import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=5).read(1)")
             + " " + shlex.quote(READY_URL))
    tab = client.start_shell(ShellCommandRequest(
        probe, REPOSITORY, "Review Fix endpoint ownership",
        deadline_check=remaining, max_output_chars=4096,
    ))
    readiness_tabs.append(tab)
    try:
        client.wait_for_shell_completion(tab, min(remaining(), 10))
        shell_result = client.read_shell_result(tab)
        if shell_result.exit_code == 0:
            raise RuntimeError(
                "readiness endpoint responded before the managed service was started"
            )
    finally:
        if tab in readiness_tabs:
            close_tab(tab)
            readiness_tabs.remove(tab)


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
                require_service_alive()
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
        require_service_alive()
        if deadline - time.monotonic() <= 0:
            raise TimeoutError("service readiness timed out: " + last)
        time.sleep(min(0.5, max(deadline - time.monotonic(), 0)))


def review(iteration):
    global active_review_run
    review_timeout = max(1, min(int(remaining()), 86400))
    service_context = (
        "The declared service is already running in managed PurpleMux workspace "
        + workspace.id + ", tab " + service_tab + ". Its readiness probe passed at "
        + READY_URL + ". Inspect that service and endpoint as part of the check. "
        "Do not restart the service or send input to its managed tab."
    )
    review_config = ReviewInput(
        (REPOSITORY,), CHECK, start=service_context,
        agent=REVIEW_AGENT, timeout=review_timeout,
    )
    review_code = generate_review_workflow(review_config)
    previous_mask = signal.pthread_sigmask(
        signal.SIG_BLOCK, {{signal.SIGINT, signal.SIGTERM}}
    )
    try:
        run_id = start_child_run(
            review_code,
            timeout=remaining(),
            stop_with_parent=True,
            review_json=json.dumps(review_config.as_json()),
        )
        active_review_run = run_id
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    try:
        child = wait_child_run(run_id, timeout=remaining())
    except BaseException as exc:
        try:
            stop_active_review()
        except BaseException as stop_error:
            if isinstance(exc, (SystemExit, KeyboardInterrupt)):
                raise exc from stop_error
            raise RuntimeError(
                "Review child Run could not be stopped: " + str(stop_error)
            ) from exc
        raise
    active_review_run = None
    if child.state != "success" or child.exit_code != 0:
        raise RuntimeError("Review child Run failed: " + child.stderr[-4096:])
    if child.review_result is None:
        raise RuntimeError("Review child Run did not save a structured result")
    try:
        report = validate_review_result(child.review_result)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Review child Run saved an invalid result") from exc
    compact = {{"verdict": report["verdict"], "summary": report["summary"][:4096]}}
    for name in ("findings", "observed_facts", "evidence", "hypotheses", "observability_gaps"):
        if name in report:
            compact[name] = [item[:512] for item in report[name][:5]]
    iterations.append({{"iteration": iteration, "review_run_id": run_id, "review": compact}})
    return report


def implement(iteration, report):
    global implementation_tab
    repo = GitRepository.open(REPOSITORY)
    before = repo.inspect_worktree()
    if before.current_branch is None:
        raise WorkerFailure("Review Fix implementation requires a current branch")
    repo.require_clean()
    branch = before.current_branch
    branch_before = repo.inspect_branch(branch)
    if branch_before.local_sha is None:
        raise WorkerFailure("Review Fix implementation branch has no commit")
    previous_sha = branch_before.local_sha
    implementation_tab = client.create_session(CreateSessionRequest(
        worker=IMPLEMENTATION_AGENT, cwd=REPOSITORY, command=IMPLEMENTATION_AGENT,
        name="Review Fix implementation " + str(iteration), deadline_check=remaining,
    ))
    client.wait_until_ready(implementation_tab, min(remaining(), 60))
    coauthor = agent_commit_coauthor(COMMIT_AGENT)
    prompt = (
        "You are the repository-modifying implementation role. Fix only the failures "
        "identified by this read-only Review report, in " + REPOSITORY + ". "
        "Stay on branch " + branch + ". Inspect and modify the repository, run focused checks, "
        "commit every intended source, test, and configuration change, and finish with a clean "
        "worktree. Do not merely describe a fix. Do not reset, rebase, stash, force-push, merge, "
        "discard ambiguous work, or start or control the Review role. Do not create, remove, or "
        "edit agent-workflow-manager fingerprint markers. Every commit you create must end with "
        "these exact Git trailers, preserving any additional trailers:\\n"
        "Co-authored-by: " + coauthor + "\\nAWM-Agent: " + COMMIT_AGENT
        + "\\nAWM-Process: implementation\\n\\nReview evidence: " + json.dumps(report)
    )
    implementation_result = None
    committed = None
    for recovery_attempt in range(3):
        client.send_input(implementation_tab, prompt)
        client.wait_for_turn_completion(implementation_tab, remaining())
        implementation_result = client.read_result(implementation_tab)
        try:
            committed = repo.require_committed_result(
                branch, previous_sha=previous_sha, expected_agent=COMMIT_AGENT,
                expected_process="implementation",
            )
            break
        except WorkerFailure as exc:
            if recovery_attempt == 2:
                raise WorkerFailure(
                    "implementation result remained invalid after same-agent recovery: "
                    + str(exc)
                ) from exc
            prompt = (
                "AWM rejected your coding result during authoritative validation: "
                + str(exc) + "\\nContinue in this same session and repair only that failure. "
                "Preserve correct work and stay on branch " + branch + ". Commit all intended "
                "changes and leave the worktree clean. Every new commit must use these trailers:\\n"
                "Co-authored-by: " + coauthor + "\\nAWM-Agent: " + COMMIT_AGENT
                + "\\nAWM-Process: implementation"
            )
    assert implementation_result is not None and committed is not None
    iterations[-1]["implementation"] = implementation_result[-4096:]
    iterations[-1]["implementation_sha"] = committed.local_sha
    close_tab(implementation_tab)
    implementation_tab = None


emit_step("Start service", "started")
try:
    workspace = runtime.create_workspace(CreateWorkspaceRequest(
        cwd=REPOSITORY, name="AWM Review Fix", deadline_check=remaining,
    ))
    client = runtime.workspace(workspace.id)
    require_endpoint_absent()
    start_service()
    readiness = establish_readiness()
except Exception as exc:
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
            readiness = restart_service()
            iterations[-1]["readiness"] = readiness
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
    if active_review_run is not None:
        try:
            stop_active_review()
        except BaseException as exc:
            cleanup_errors.append("Review child cleanup failed: " + str(exc))
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

serialized_result = serialize_review_fix_result(result)
publish_review_fix_result(json.loads(serialized_result))
print(serialized_result)
"""
