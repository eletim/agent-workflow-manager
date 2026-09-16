"""Declarative Review input and ordinary Python Workflow generation."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REQUIRED = {"mode", "repositories", "check"}
_OPTIONAL = {"start", "finish", "agent", "timeout"}


@dataclass(frozen=True)
class ReviewInput:
    repositories: tuple[str, ...]
    check: str
    start: str | None = None
    finish: str | None = None
    agent: str = "codex"
    timeout: int = 3600

    def as_json(self) -> dict[str, object]:
        value: dict[str, object] = {
            "mode": "review",
            "repositories": list(self.repositories),
            "check": self.check,
            "agent": self.agent,
            "timeout": self.timeout,
        }
        if self.start is not None:
            value["start"] = self.start
        if self.finish is not None:
            value["finish"] = self.finish
        return value


def _nonempty_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{name} must be a non-empty string without nulls")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{name} must contain only Unicode scalar values")
    return value


def parse_review_json(source: str) -> ReviewInput:
    """Validate Review fields and resolve each existing local Git repository."""
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
    if missing := _REQUIRED - value.keys():
        raise ValueError(f"missing fields: {', '.join(sorted(missing))}")
    if unknown := value.keys() - _REQUIRED - _OPTIONAL:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    if value["mode"] != "review":
        raise ValueError("mode must be 'review'")
    repositories = value["repositories"]
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("repositories must be a non-empty array")
    resolved = []
    for index, item in enumerate(repositories):
        path = Path(_nonempty_text(item, f"repositories[{index}]")).expanduser()
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"repositories[{index}] cannot be resolved: {exc}") from exc
        if not path.is_dir():
            raise ValueError(f"repositories[{index}] must be a directory")
        try:
            command = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"repositories[{index}] cannot be inspected: {exc}") from exc
        if command.returncode or Path(command.stdout.strip()).resolve() != path:
            raise ValueError(f"repositories[{index}] must be a Git repository root")
        if str(path) in resolved:
            raise ValueError("repositories must not repeat the same path")
        resolved.append(str(path))
    check = _nonempty_text(value["check"], "check")
    for name in ("start", "finish"):
        if name in value:
            _nonempty_text(value[name], name)
    agent = value.get("agent", "codex")
    if agent not in ("codex", "claude-code"):
        raise ValueError("agent must be codex or claude-code")
    timeout = value.get("timeout", 3600)
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise ValueError("timeout must be an integer from 1 to 86400 seconds")
    return ReviewInput(tuple(resolved), check, value.get("start"), value.get("finish"), agent, timeout)


def serialize_review_result(report: dict[str, Any], repositories: tuple[str, ...]) -> str:
    """Keep a complete Review JSON value within the runner's stdout limit."""
    summary = report["summary"]
    findings = report.get("findings", [])
    result: dict[str, Any] = {
        "verdict": report["verdict"],
        "summary": summary[:16384],
        "findings": [finding[:512] for finding in findings[:100]],
        "repositories": list(repositories[:32]),
    }
    truncated: dict[str, int] = {}
    if len(summary) > 16384:
        truncated["summary_chars"] = len(summary) - 16384
    if len(findings) > 100:
        truncated["findings"] = len(findings) - 100
    clipped_findings = sum(len(finding) > 512 for finding in findings[:100])
    if clipped_findings:
        truncated["finding_texts"] = clipped_findings
    omitted_repositories = len(repositories) - len(result["repositories"])
    if omitted_repositories:
        truncated["repositories"] = omitted_repositories
    if truncated:
        result["truncated"] = truncated

    max_chars = 999_999  # Reserve one character for print's newline.
    payload = json.dumps(result)
    while len(payload) > max_chars and result["repositories"]:
        result["repositories"].pop()
        truncated["repositories"] = truncated.get("repositories", 0) + 1
        result["truncated"] = truncated
        payload = json.dumps(result)
    if len(payload) > max_chars:
        raise ValueError("Review result exceeds stdout limit after compaction")
    return payload


def generate_review_workflow(config: ReviewInput) -> str:
    """Place Review sequencing and result checks in visible, plain Python."""
    return f'''import json
import time

from purplemux_client import CreateSessionRequest, CreateWorkspaceRequest, PurpleMuxRuntime, emit_step
from purplemux_client.review import serialize_review_result

WORKFLOW_OUTLINE = ["Review"]
REPOSITORIES = {config.repositories!r}
CHECK = {config.check!r}
START = {config.start!r}
FINISH = {config.finish!r}
AGENT = {config.agent!r}


def remaining():
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Review timed out")
    return seconds


def busy_timeout(_warning):
    raise TimeoutError("Review timed out while the agent was busy")


def turn(message):
    remaining()
    client.send_input(tab, message)
    client.wait_for_turn_completion(tab, remaining(), on_busy_timeout=busy_timeout)
    remaining()
    return client.read_result(tab)


emit_step("Review", "started")
deadline = time.monotonic() + {config.timeout}
runtime = PurpleMuxRuntime(owned_by_run=True)
client = None
tab = None
try:
    workspace = runtime.create_workspace(CreateWorkspaceRequest(
        cwd=REPOSITORIES[0], name="AWM Review", deadline_check=remaining,
    ))
    client = runtime.workspace(workspace.id)
    tab = client.create_session(CreateSessionRequest(
        worker=AGENT, cwd=REPOSITORIES[0], command=AGENT,
        name="Review agent", deadline_check=remaining,
    ))
    client.wait_until_ready(tab, min(remaining(), 60))
    context = "Review these local repositories: " + json.dumps(REPOSITORIES) + ". Do not modify them. "
    if START is not None:
        turn(context + "First, follow this start instruction and report what you did: " + START)
    report = turn(context + "Perform this check: " + CHECK + "\\nReturn a JSON object with verdict PASS, FAIL, or BLOCKED, a non-empty summary, and an optional findings array of strings. Base the verdict on observed evidence.")
    try:
        result = json.loads(report)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Review agent did not return JSON") from exc
    if (not isinstance(result, dict) or result.get("verdict") not in ("PASS", "FAIL", "BLOCKED")
            or not isinstance(result.get("summary"), str) or not result["summary"].strip()
            or not isinstance(result.get("findings", []), list)
            or any(not isinstance(item, str) for item in result.get("findings", []))):
        raise RuntimeError("Review agent returned an invalid report")
    serialized_result = serialize_review_result(result, REPOSITORIES)
    if FINISH is not None:
        turn("The check produced this report: " + serialized_result + "\\nNow follow this finish instruction and report what you did: " + FINISH)
    print(serialized_result)
except BaseException as exc:
    if isinstance(exc, TimeoutError) and client is not None and tab is not None:
        client.interrupt(tab)
    emit_step("Review", "failed", error=str(exc))
    raise
else:
    emit_step("Review", "completed", workspace=workspace.id, tab=tab)
'''
