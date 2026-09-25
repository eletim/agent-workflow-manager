from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from queue import SimpleQueue
from typing import Literal
from urllib import error, request
from urllib.parse import urlparse

StepStatus = Literal["started", "completed", "failed"]
FindingCategory = Literal["runtime", "git", "github", "policy_issue"]
FindingStatus = Literal["passed", "warning", "failed", "info"]
IssueOutcome = Literal["approved", "continued_with_warning", "skipped"]
RepositoryStatus = Literal["started", "completed"]
RunResourceKind = Literal[
    "purplemux_tab",
    "managed_shell_result",
    "purplemux_workspace",
    "git_worktree",
]

PROGRESS_FD_ENV = "PURPLEMUX_RUNNER_PROGRESS_FD"
RESOURCE_ACK_FD_ENV = "PURPLEMUX_RUNNER_RESOURCE_ACK_FD"
EVENT_URL_ENV = "AGENT_WORKFLOW_MANAGER_EVENT_URL"
EVENT_TOKEN_ENV = "AGENT_WORKFLOW_MANAGER_EVENT_TOKEN"
AGENT_TURN_TRACE_FILE_ENV = "AGENT_WORKFLOW_MANAGER_AGENT_TURN_TRACE_FILE"
MAX_PROGRESS_EVENT_BYTES = 4096
_AGENT_TURN_CHUNK_CHARS = 2_400
_TRUNCATED_ERROR_SUFFIX = "\n[error truncated]"
_write_lock = threading.Lock()
_agent_turn_http_queue: SimpleQueue[tuple[str, str, tuple[dict[str, object], ...]]] = (
    SimpleQueue()
)
_agent_turn_http_thread: threading.Thread | None = None
_agent_turn_http_thread_lock = threading.Lock()


def emit_step(
    name: str,
    status: StepStatus,
    *,
    iteration: int | None = None,
    attempt: int | None = None,
    message: str | None = None,
    error: str | None = None,
    workspace: str | None = None,
    tab: str | None = None,
    pr_number: int | None = None,
    pr_url: str | None = None,
) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("step name must be a non-empty string")
    if status not in ("started", "completed", "failed"):
        raise ValueError("status must be started, completed, or failed")
    _validate_number("iteration", iteration)
    _validate_number("attempt", attempt)
    _validate_pr("step", pr_number, pr_url)
    for field_name, value in (
        ("message", message),
        ("error", error),
        ("workspace", workspace),
        ("tab", tab),
        ("pr_url", pr_url),
    ):
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{field_name} must be a string or None")

    event: dict[str, str | int] = {"name": name, "status": status}
    for key, value in (
        ("iteration", iteration),
        ("attempt", attempt),
        ("message", message),
        ("error", error),
        ("workspace", workspace),
        ("tab", tab),
        ("pr_number", pr_number),
        ("pr_url", pr_url),
    ):
        if value is not None:
            event[key] = value
    _write_event(event, drop_oversized=True)


def emit_agent_turn(
    turn_id: int,
    purpose: str,
    role: str,
    attempt: int,
    status: Literal["started", "completed", "failed"],
    *,
    phase: str | None = None,
    work_item_id: int | str | None = None,
    work_item_label: str | None = None,
    transition_outcome: str | None = None,
    commit_sha: str | None = None,
    prompt: str | None = None,
    result: str | None = None,
    error: str | None = None,
) -> None:
    """Publish one observation-only Issue Driven agent-turn transition.

    Large prompts and results are transported in independently bounded chunks.
    Delivery is best-effort like every other progress event and therefore can
    never become workflow control state.
    """
    _validate_positive_number("turn_id", turn_id)
    _validate_positive_number("attempt", attempt)
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("agent turn purpose must be a non-empty string")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("agent turn role must be a non-empty string")
    if phase is not None and (not isinstance(phase, str) or not phase.strip()):
        raise ValueError("agent turn phase must be a non-empty string or None")
    if isinstance(work_item_id, bool) or (
        work_item_id is not None and not isinstance(work_item_id, (int, str))
    ):
        raise TypeError("agent turn work_item_id must be an int, string, or None")
    if isinstance(work_item_id, int) and work_item_id < 1:
        raise ValueError("agent turn integer work_item_id must be positive")
    if isinstance(work_item_id, str) and not work_item_id.strip():
        raise ValueError("agent turn string work_item_id must be non-empty")
    if (work_item_id is None) != (work_item_label is None):
        raise ValueError("agent turn work-item id and label must be provided together")
    if work_item_label is not None and (
        not isinstance(work_item_label, str) or not work_item_label.strip()
    ):
        raise ValueError("agent turn work_item_label must be non-empty")
    if status == "started":
        if not isinstance(prompt, str) or result is not None or error is not None:
            raise ValueError("a started agent turn requires only its exact prompt")
    elif status == "completed":
        if not isinstance(result, str) or prompt is not None or error is not None:
            raise ValueError("a completed agent turn requires only its result")
        if transition_outcome is not None and (
            not isinstance(transition_outcome, str) or not transition_outcome.strip()
        ):
            raise ValueError("agent turn transition outcome must be non-empty")
    elif status == "failed":
        if (
            not isinstance(error, str)
            or not error
            or prompt is not None
            or result is not None
        ):
            raise ValueError("a failed agent turn requires only its error")
        if transition_outcome is not None:
            raise ValueError("a failed agent turn cannot have a transition outcome")
    else:
        raise ValueError("agent turn status must be started, completed, or failed")
    if commit_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ValueError("agent turn commit_sha must be a full lowercase Git SHA")

    payload: dict[str, object] = {
        "turn_id": turn_id,
        "purpose": purpose,
        "role": role,
        "attempt": attempt,
        "status": status,
    }
    if phase is not None:
        payload["phase"] = phase
    if work_item_id is not None:
        payload["work_item_id"] = work_item_id
        payload["work_item_label"] = work_item_label
    if transition_outcome is not None:
        payload["transition_outcome"] = transition_outcome
    if commit_sha is not None:
        payload["commit_sha"] = commit_sha
    if prompt is not None:
        payload["prompt"] = prompt
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode("ascii")
    chunks = tuple(
        encoded[index : index + _AGENT_TURN_CHUNK_CHARS]
        for index in range(0, len(encoded), _AGENT_TURN_CHUNK_CHARS)
    )
    message_id = f"{turn_id}:{status}"
    events: tuple[dict[str, object], ...] = tuple(
        {
            "type": "agent_turn_trace_chunk",
            "message_id": message_id,
            "chunk_index": index,
            "chunk_count": len(chunks),
            "data": chunk,
        }
        for index, chunk in enumerate(chunks)
    )
    _append_agent_turn_spool(events)
    event_url = os.environ.get(EVENT_URL_ENV)
    event_token = os.environ.get(EVENT_TOKEN_ENV)
    if event_url is not None and event_token is not None:
        _enqueue_agent_turn_http(event_url, event_token, events)
        return
    for event in events:
        _write_event(event, drop_oversized=True)


def _enqueue_agent_turn_http(
    event_url: str,
    event_token: str,
    events: tuple[dict[str, object], ...],
) -> None:
    """Queue ordered trace delivery without waiting at the agent send boundary."""
    global _agent_turn_http_thread

    _agent_turn_http_queue.put((event_url, event_token, events))
    with _agent_turn_http_thread_lock:
        if _agent_turn_http_thread is not None and _agent_turn_http_thread.is_alive():
            return
        _agent_turn_http_thread = threading.Thread(
            target=_deliver_agent_turn_http,
            name="agent-turn-trace-delivery",
            # Observation must never keep authoritative workflow execution alive.
            daemon=True,
        )
        _agent_turn_http_thread.start()


def _deliver_agent_turn_http() -> None:
    """Preserve transition order and retry ambiguous failures until shutdown."""
    while True:
        event_url, event_token, events = _agent_turn_http_queue.get()
        for event in events:
            retry_delay = 0.05
            while True:
                outcome = _post_agent_turn_event(event_url, event_token, event)
                if outcome == "delivered":
                    break
                if outcome == "rejected":
                    break
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 1.0)
            if outcome == "rejected":
                break


def _append_agent_turn_spool(events: tuple[dict[str, object], ...]) -> None:
    path = os.environ.get(AGENT_TURN_TRACE_FILE_ENV)
    if path is None:
        return
    encoded = b"".join(_encode_event(event) for event in events)
    try:
        flags = os.O_WRONLY | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            with _write_lock:
                view = memoryview(encoded)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
        finally:
            os.close(fd)
    except OSError:
        return


def _post_agent_turn_event(
    url: str,
    token: str,
    event: Mapping[str, object],
) -> Literal["delivered", "retry", "rejected"]:
    encoded = _encode_event(event)
    if len(encoded) > MAX_PROGRESS_EVENT_BYTES:
        return "rejected"
    submitted = request.Request(
        url,
        data=encoded,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-AWM-Run-Token": token,
        },
    )
    try:
        with request.urlopen(submitted, timeout=5) as response:
            response.read(MAX_PROGRESS_EVENT_BYTES + 1)
    except error.HTTPError as exc:
        return "retry" if 500 <= exc.code < 600 else "rejected"
    except (error.URLError, OSError):
        return "retry"
    return "delivered"


def emit_run_pr(pr_number: int, pr_url: str) -> None:
    """Publish the authoritative Base/Integration PR for the current run."""
    _validate_pr("run", pr_number, pr_url)
    _write_event(
        {"type": "run_pr", "pr_number": pr_number, "pr_url": pr_url},
        drop_oversized=True,
    )


def emit_issue_result(
    issue: int | str,
    outcome: IssueOutcome,
    reviews: int,
    pr_number: int,
    pr_url: str,
    *,
    warnings: tuple[str, ...] = (),
    label: str | None = None,
    workspace_id: str | None = None,
    implementation_tab_id: str | None = None,
    scope_review_tab_id: str | None = None,
    correctness_review_tab_id: str | None = None,
) -> None:
    """Publish one final, structured work-item outcome for the current run."""
    if isinstance(issue, bool) or not isinstance(issue, (int, str)):
        raise ValueError("issue must be a positive number or non-empty string")
    if isinstance(issue, int):
        _validate_positive_number("issue", issue)
    elif not issue.strip() or len(issue) > 100:
        raise ValueError("issue must be a positive number or non-empty string")
    if label is not None and (
        not isinstance(label, str) or not label.strip() or len(label) > 100
    ):
        raise ValueError("label must be a non-empty string of at most 100 characters")
    _validate_outcome(outcome)
    _validate_review_count(reviews)
    _validate_pr("issue result", pr_number, pr_url)
    _validate_warnings(warnings)
    _validate_issue_terminals(
        workspace_id,
        implementation_tab_id,
        scope_review_tab_id,
        correctness_review_tab_id,
    )
    event: dict[str, object] = {
        "type": "issue_result",
        "issue": issue,
        "outcome": outcome,
        "reviews": reviews,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "warnings": list(warnings),
    }
    if label is not None:
        event["label"] = label
    if workspace_id is not None:
        event["workspace_id"] = workspace_id
        event["implementation_tab_id"] = implementation_tab_id
        event["scope_review_tab_id"] = scope_review_tab_id
        event["correctness_review_tab_id"] = correctness_review_tab_id
    _write_event(event)


def emit_issue_navigation(
    issue: int | str,
    pr_number: int,
    pr_url: str,
    *,
    workspace_id: str,
    implementation_tab_id: str,
    scope_review_tab_id: str,
    correctness_review_tab_id: str,
    label: str | None = None,
) -> None:
    """Publish durable work-item navigation independently of its final outcome."""
    if isinstance(issue, bool) or not isinstance(issue, (int, str)):
        raise ValueError("issue must be a positive number or non-empty string")
    if isinstance(issue, int):
        _validate_positive_number("issue", issue)
    elif not issue.strip() or len(issue) > 100:
        raise ValueError("issue must be a positive number or non-empty string")
    if label is not None and (
        not isinstance(label, str) or not label.strip() or len(label) > 100
    ):
        raise ValueError("label must be a non-empty string of at most 100 characters")
    _validate_pr("issue navigation", pr_number, pr_url)
    _validate_issue_terminals(
        workspace_id,
        implementation_tab_id,
        scope_review_tab_id,
        correctness_review_tab_id,
    )
    event: dict[str, object] = {
        "type": "issue_navigation",
        "issue": issue,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "workspace_id": workspace_id,
        "implementation_tab_id": implementation_tab_id,
        "scope_review_tab_id": scope_review_tab_id,
        "correctness_review_tab_id": correctness_review_tab_id,
    }
    if label is not None:
        event["label"] = label
    _write_event(event)


def emit_planner_skip(
    issue: int | str,
    reason: str,
    *,
    label: str | None = None,
) -> None:
    """Publish an authoritative planner decision to skip one work item."""
    if isinstance(issue, bool) or not isinstance(issue, (int, str)):
        raise ValueError("issue must be a positive number or non-empty string")
    if isinstance(issue, int):
        _validate_positive_number("issue", issue)
    elif not issue.strip() or len(issue) > 100:
        raise ValueError("issue must be a positive number or non-empty string")
    if (
        not isinstance(reason, str)
        or not reason
        or reason != reason.strip()
        or "\0" in reason
        or len(reason) > 500
        or any(0xD800 <= ord(character) <= 0xDFFF for character in reason)
    ):
        raise ValueError("reason must be a non-empty string of at most 500 characters")
    if label is not None and (
        not isinstance(label, str) or not label.strip() or len(label) > 100
    ):
        raise ValueError("label must be a non-empty string of at most 100 characters")
    event: dict[str, object] = {
        "type": "planner_skip",
        "issue": issue,
        "reason": reason,
    }
    if label is not None:
        event["label"] = label
    _write_event(event, drop_oversized=True)


def emit_whole_review_result(
    outcome: IssueOutcome,
    reviews: int,
    *,
    warnings: tuple[str, ...] = (),
) -> None:
    """Publish the final whole-version review outcome for the current run."""
    _validate_outcome(outcome)
    _validate_review_count(reviews)
    _validate_warnings(warnings)
    _write_event(
        {
            "type": "whole_review_result",
            "outcome": outcome,
            "reviews": reviews,
            "warnings": list(warnings),
        }
    )


def emit_issue_driven_context(
    repository: str,
    integration_branch: str,
    final_branch: str,
    *,
    policy_issue: int | None = None,
) -> None:
    """Identify a run as Issue Driven without introducing workflow control state."""
    for name, value in (
        ("repository", repository),
        ("integration_branch", integration_branch),
        ("final_branch", final_branch),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    if policy_issue is not None:
        _validate_positive_number("policy_issue", policy_issue)
    event: dict[str, str | int] = {
        "type": "issue_driven_context",
        "repository": repository,
        "integration_branch": integration_branch,
        "final_branch": final_branch,
    }
    if policy_issue is not None:
        event["policy_issue"] = policy_issue
    _write_event(event)


def emit_issue_driven_repositories(
    repositories: tuple[tuple[str, str, str, int | None], ...],
) -> None:
    """Declare the complete ordered repository set for one Issue Driven run."""
    _write_event(_issue_driven_repositories_event(repositories))


def _issue_driven_repositories_event(
    repositories: tuple[tuple[str, str, str, int | None], ...],
) -> dict[str, object]:
    if not isinstance(repositories, tuple) or len(repositories) < 2:
        raise ValueError("repositories must be a tuple containing at least two items")
    declared: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in repositories:
        if not isinstance(item, tuple) or len(item) != 4:
            raise TypeError("each repository must be a four-item tuple")
        repository, integration_branch, final_branch, policy_issue = item
        for name, value in (
            ("repository", repository),
            ("integration_branch", integration_branch),
            ("final_branch", final_branch),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if repository in seen:
            raise ValueError("repository declarations must be unique")
        if policy_issue is not None:
            _validate_positive_number("policy_issue", policy_issue)
        seen.add(repository)
        declaration: dict[str, object] = {
            "repository": repository,
            "integration_branch": integration_branch,
            "final_branch": final_branch,
        }
        if policy_issue is not None:
            declaration["policy_issue"] = policy_issue
        declared.append(declaration)
    return {"type": "issue_driven_repositories", "repositories": declared}


def _issue_driven_repositories_event_fits(
    repositories: tuple[tuple[str, str, str, int | None], ...],
) -> bool:
    event = _issue_driven_repositories_event(repositories)
    try:
        return len(_encode_event(event)) <= MAX_PROGRESS_EVENT_BYTES
    except UnicodeEncodeError:
        return False


def emit_issue_driven_repository(
    repository_index: int,
    status: RepositoryStatus,
) -> None:
    """Publish one declared repository's preparation/execution lifecycle."""
    _validate_positive_number("repository_index", repository_index)
    if status not in ("started", "completed"):
        raise ValueError("status must be started or completed")
    _write_event(
        {
            "type": "issue_driven_repository",
            "repository_index": repository_index,
            "status": status,
        }
    )


def _validate_positive_number(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be positive")


def _validate_outcome(outcome: str) -> None:
    if outcome not in ("approved", "continued_with_warning", "skipped"):
        raise ValueError("outcome must be approved, continued_with_warning, or skipped")


def _validate_review_count(reviews: int) -> None:
    if isinstance(reviews, bool) or not isinstance(reviews, int) or reviews < 0:
        raise ValueError("reviews must be a non-negative integer")


def _validate_warnings(warnings: tuple[str, ...]) -> None:
    if not isinstance(warnings, tuple) or any(
        not isinstance(item, str) or not item.strip() for item in warnings
    ):
        raise TypeError("warnings must be a tuple of non-empty strings")


def _validate_pr(context: str, pr_number: int | None, pr_url: str | None) -> None:
    if (pr_number is None) != (pr_url is None):
        raise ValueError(f"{context} PR number and URL must be provided together")
    if pr_number is None:
        return
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        raise ValueError(f"{context} PR number must be positive")
    if not isinstance(pr_url, str):
        raise TypeError(f"{context} PR URL must be a string")
    parsed = urlparse(pr_url)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path:
        raise ValueError(f"{context} PR URL must be an absolute HTTPS URL")


def _validate_issue_terminals(
    workspace_id: str | None,
    implementation_tab_id: str | None,
    scope_review_tab_id: str | None,
    correctness_review_tab_id: str | None,
) -> None:
    identities = (
        workspace_id,
        implementation_tab_id,
        scope_review_tab_id,
        correctness_review_tab_id,
    )
    if all(identity is None for identity in identities):
        return
    if any(
        not isinstance(identity, str) or not identity.strip() for identity in identities
    ):
        raise ValueError(
            "PurpleMux workspaceId and all work-item tabIds must be non-empty strings"
        )


def emit_finding(
    category: FindingCategory, message: str, *, status: FindingStatus = "passed"
) -> None:
    """Publish an observed readiness/topology fact without controlling execution."""
    if category not in ("runtime", "git", "github", "policy_issue"):
        raise ValueError(
            "finding category must be runtime, git, github, or policy_issue"
        )
    if status not in ("passed", "warning", "failed", "info"):
        raise ValueError("finding status must be passed, warning, failed, or info")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("finding message must be non-empty")
    _write_event(
        {"type": "finding", "category": category, "status": status, "message": message},
        drop_oversized=True,
    )


def register_run_resource(
    kind: RunResourceKind,
    identity: str,
    metadata: Mapping[str, str] | None = None,
) -> None:
    """Register an authoritatively identified resource with the current run.

    Registration is observational and does not mutate the resource. Outside the
    Runner it is a no-op, like progress events.
    """
    if kind not in (
        "purplemux_tab",
        "managed_shell_result",
        "purplemux_workspace",
        "git_worktree",
    ):
        raise ValueError("unsupported run resource kind")
    if not isinstance(identity, str) or not identity or "\0" in identity:
        raise ValueError("resource identity must be a non-empty string without nulls")
    resource_metadata = dict(metadata or {})
    if any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, str)
        or "\0" in key
        or "\0" in value
        for key, value in resource_metadata.items()
    ):
        raise TypeError(
            "resource metadata must contain non-empty string keys and string values"
        )
    _write_event(
        {
            "type": "resource",
            "kind": kind,
            "identity": identity,
            "metadata": resource_metadata,
        }
    )


def acknowledge_run_resource(
    phase: Literal["pending", "verified"],
    kind: RunResourceKind,
    identity: str,
    metadata: Mapping[str, str],
) -> None:
    """Synchronously establish or finalize manager-visible resource ownership.

    Outside a managed Runner this retains the public helper's historical no-op
    behavior. Inside one, the workflow cannot continue until the RunRecord has
    accepted the ownership evidence.
    """
    if phase not in ("pending", "verified"):
        raise ValueError("resource phase must be pending or verified")
    # Dry Run has a diagnostic progress pipe but deliberately no ownership
    # manager; its mutation boundary exits before a resource can be created.
    if os.environ.get("AGENT_WORKFLOW_MANAGER_DRY_RUN_FD") is not None:
        return
    event_url = os.environ.get(EVENT_URL_ENV)
    event_token = os.environ.get(EVENT_TOKEN_ENV)
    if event_url is not None or event_token is not None:
        if event_url is None or event_token is None:
            raise RuntimeError("Runner event endpoint is incomplete")
        token = uuid.uuid4().hex
        event = {
            "type": "resource_ownership",
            "phase": phase,
            "token": token,
            "kind": kind,
            "identity": identity,
            "metadata": dict(metadata),
        }
        acknowledgement = _post_event(event_url, event_token, event, required=True)
        if acknowledgement != {"token": token, "accepted": True}:
            raise RuntimeError("Runner rejected resource ownership evidence")
        return

    progress_fd = os.environ.get(PROGRESS_FD_ENV)
    ack_fd = os.environ.get(RESOURCE_ACK_FD_ENV)
    if progress_fd is None and ack_fd is None:
        return
    if progress_fd is None or ack_fd is None:
        raise RuntimeError("Runner resource ownership channel is incomplete")
    try:
        output_fd = int(progress_fd)
        input_fd = int(ack_fd)
    except ValueError as exc:
        raise RuntimeError("Runner resource ownership channel is invalid") from exc

    token = uuid.uuid4().hex
    event = {
        "type": "resource_ownership",
        "phase": phase,
        "token": token,
        "kind": kind,
        "identity": identity,
        "metadata": dict(metadata),
    }
    encoded = _encode_event(event)
    if len(encoded) > MAX_PROGRESS_EVENT_BYTES:
        raise ValueError("Runner resource ownership event exceeds 4096 encoded bytes")
    with _write_lock:
        view = memoryview(encoded)
        while view:
            try:
                written = os.write(output_fd, view)
            except OSError as exc:
                raise RuntimeError(
                    "Runner resource ownership event could not be delivered"
                ) from exc
            view = view[written:]
        response = bytearray()
        while not response.endswith(b"\n"):
            try:
                chunk = os.read(input_fd, MAX_PROGRESS_EVENT_BYTES + 1 - len(response))
            except OSError as exc:
                raise RuntimeError(
                    "Runner resource ownership acknowledgement failed"
                ) from exc
            if not chunk:
                raise RuntimeError("Runner resource ownership was not acknowledged")
            response.extend(chunk)
            if len(response) > MAX_PROGRESS_EVENT_BYTES:
                raise RuntimeError(
                    "Runner resource ownership acknowledgement is invalid"
                )
        try:
            acknowledgement = json.loads(response)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Runner resource ownership acknowledgement is invalid"
            ) from exc
        if acknowledgement != {"token": token, "accepted": True}:
            raise RuntimeError("Runner rejected resource ownership evidence")


def _write_event(event: Mapping[str, object], *, drop_oversized: bool = False) -> None:
    event_url = os.environ.get(EVENT_URL_ENV)
    event_token = os.environ.get(EVENT_TOKEN_ENV)
    if event_url is not None and event_token is not None:
        encoded = _encode_event(event)
        if len(encoded) > MAX_PROGRESS_EVENT_BYTES:
            if drop_oversized:
                encoded = _truncate_event_error(event)
                if encoded is None:
                    return
                event = json.loads(encoded)
            else:
                raise ValueError("Runner event exceeds 4096 encoded bytes")
        _post_event(event_url, event_token, event, required=False)
        return
    fd_text = os.environ.get(PROGRESS_FD_ENV)
    if fd_text is None:
        return
    try:
        fd = int(fd_text)
    except ValueError:
        return
    encoded = _encode_event(event)
    if len(encoded) > MAX_PROGRESS_EVENT_BYTES:
        if drop_oversized:
            encoded = _truncate_event_error(event)
            if encoded is None:
                return
        else:
            raise ValueError("Runner event exceeds 4096 encoded bytes")
    with _write_lock:
        view = memoryview(encoded)
        while view:
            try:
                written = os.write(fd, view)
            except OSError:
                return
            view = view[written:]


def _post_event(
    url: str,
    token: str,
    event: Mapping[str, object],
    *,
    required: bool,
) -> object | None:
    encoded = _encode_event(event)
    if len(encoded) > MAX_PROGRESS_EVENT_BYTES:
        if required:
            raise ValueError("Runner event exceeds 4096 encoded bytes")
        return None
    submitted = request.Request(
        url,
        data=encoded,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-AWM-Run-Token": token,
        },
    )
    try:
        with request.urlopen(submitted, timeout=5) as response:
            payload = response.read(MAX_PROGRESS_EVENT_BYTES + 1)
    except (error.URLError, OSError) as exc:
        if required:
            raise RuntimeError(
                "Runner resource ownership event could not be delivered"
            ) from exc
        return None
    if not required:
        return None
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            "Runner resource ownership acknowledgement is invalid"
        ) from exc


def _encode_event(event: Mapping[str, object]) -> bytes:
    return (
        json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()


def _truncate_event_error(event: Mapping[str, object]) -> bytes | None:
    """Fit a display error exactly while preserving the event's structured fields."""
    error = event.get("error")
    if not isinstance(error, str):
        return None
    truncated = dict(event)
    truncated["error"] = _TRUNCATED_ERROR_SUFFIX
    if len(_encode_event(truncated)) > MAX_PROGRESS_EVENT_BYTES:
        return None

    low = 0
    high = len(error)
    while low < high:
        keep = (low + high + 1) // 2
        truncated["error"] = error[:keep] + _TRUNCATED_ERROR_SUFFIX
        if len(_encode_event(truncated)) <= MAX_PROGRESS_EVENT_BYTES:
            low = keep
        else:
            high = keep - 1
    truncated["error"] = error[:low] + _TRUNCATED_ERROR_SUFFIX
    return _encode_event(truncated)


def _validate_number(name: str, value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError(f"{name} must be an integer or None")
    if value is not None and value < 1:
        raise ValueError(f"{name} must be positive")
