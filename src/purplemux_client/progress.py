from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Mapping
from typing import Literal
from urllib import error, request
from urllib.parse import urlparse

StepStatus = Literal["started", "completed", "failed"]
FindingCategory = Literal["runtime", "git", "github", "policy_issue"]
FindingStatus = Literal["passed", "warning", "failed", "info"]
IssueOutcome = Literal["approved", "continued_with_warning", "skipped"]
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
MAX_PROGRESS_EVENT_BYTES = 4096
_TRUNCATED_ERROR_SUFFIX = "\n[error truncated]"
_write_lock = threading.Lock()


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
    _write_event(event)


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
