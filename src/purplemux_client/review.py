"""Declarative Review input and ordinary Python Workflow generation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
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
            raise ValueError(
                f"repositories[{index}] cannot be resolved: {exc}"
            ) from exc
        if not path.is_dir():
            raise ValueError(f"repositories[{index}] must be a directory")
        try:
            command = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(
                f"repositories[{index}] cannot be inspected: {exc}"
            ) from exc
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
    return ReviewInput(
        tuple(resolved), check, value.get("start"), value.get("finish"), agent, timeout
    )


def serialize_review_result(
    report: dict[str, Any],
    repositories: tuple[str, ...],
    *,
    finish_failure: str | None = None,
) -> str:
    """Keep a complete Review JSON value within the runner's stdout limit."""
    summary = report["summary"]
    result: dict[str, Any] = {
        "verdict": report["verdict"],
        "summary": summary[:16384],
        "repositories": list(repositories[:32]),
    }
    truncated: dict[str, int] = {}
    if len(summary) > 16384:
        truncated["summary_chars"] = len(summary) - 16384
    array_names = (
        "findings",
        "observed_facts",
        "evidence",
        "hypotheses",
        "observability_gaps",
    )
    for name in array_names:
        if (
            name not in report
            and name != "findings"
            and not (name == "observability_gaps" and finish_failure is not None)
        ):
            continue
        entries = report.get(name, [])
        if name == "observability_gaps" and finish_failure is not None:
            entries = [finish_failure, *entries]
        result[name] = [entry[:512] for entry in entries[:100]]
        if len(entries) > 100:
            truncated[name] = len(entries) - 100
        clipped = sum(len(entry) > 512 for entry in entries[:100])
        if clipped:
            truncated["finding_texts" if name == "findings" else f"{name}_texts"] = (
                clipped
            )
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
    while len(payload) > max_chars:
        populated = [
            name
            for name in array_names
            if result.get(name)
            and not (
                name == "observability_gaps"
                and finish_failure is not None
                and len(result[name]) == 1
            )
        ]
        if not populated:
            raise ValueError("Review result exceeds stdout limit after compaction")
        name = max(populated, key=lambda item: len(json.dumps(result[item][-1])))
        result[name].pop()
        truncated[name] = truncated.get(name, 0) + 1
        result["truncated"] = truncated
        payload = json.dumps(result)
    return payload


def snapshot_review_repositories(repositories: tuple[str, ...]) -> tuple[str, ...]:
    """Fingerprint Git state and file contents, including ignored and untracked files."""
    snapshots = []
    for repository in repositories:
        digest = hashlib.sha256()

        def field(target: Any, value: bytes) -> None:
            target.update(len(value).to_bytes(8, "big"))
            target.update(value)

        def entry(path: Path, relative: Path) -> None:
            info = path.lstat()
            record = hashlib.sha256()
            field(record, os.fsencode(relative))
            field(record, str(stat.S_IMODE(info.st_mode)).encode())
            if path.is_symlink():
                field(record, b"link")
                field(record, os.fsencode(os.readlink(path)))
            elif path.is_file():
                field(record, b"file")
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        record.update(chunk)
            elif path.is_dir():
                field(record, b"directory")
            else:
                field(record, b"other")
            digest.update(record.digest())

        def fail_walk(error: OSError) -> None:
            raise error

        def tree(root: Path, *, exclude_git: bool = False) -> None:
            for current, directories, files in os.walk(
                root, followlinks=False, onerror=fail_walk
            ):
                if exclude_git and current == str(root):
                    directories[:] = [name for name in directories if name != ".git"]
                    files[:] = [name for name in files if name != ".git"]
                directories.sort()
                for name in sorted(directories + files):
                    path = Path(current) / name
                    entry(path, path.relative_to(root))

        for args in (
            ("rev-parse", "HEAD"),
            ("symbolic-ref", "-q", "HEAD"),
            ("show-ref",),
            ("ls-files", "--stage", "-z"),
            ("config", "--local", "--list", "--null", "--show-origin"),
            ("rev-parse", "--git-dir"),
            ("rev-parse", "--git-common-dir"),
        ):
            result = subprocess.run(
                ["git", "-C", repository, *args],
                capture_output=True,
                timeout=30,
                check=False,
            )
            field(digest, b" ".join(part.encode() for part in args))
            field(digest, str(result.returncode).encode())
            field(digest, result.stdout)
            if result.returncode and args in (
                ("ls-files", "--stage", "-z"),
                ("config", "--local", "--list", "--null", "--show-origin"),
                ("rev-parse", "--git-dir"),
                ("rev-parse", "--git-common-dir"),
            ):
                raise RuntimeError(
                    f"Could not inspect Git state in {repository}: {result.stderr.decode(errors='replace')}"
                )
            if args == ("rev-parse", "--git-dir"):
                git_dir = (
                    Path(repository) / os.fsdecode(result.stdout.removesuffix(b"\n"))
                ).resolve()
            elif args == ("rev-parse", "--git-common-dir"):
                common_dir = (
                    Path(repository) / os.fsdecode(result.stdout.removesuffix(b"\n"))
                ).resolve()

        tree(Path(repository), exclude_git=True)
        for admin_dir in dict.fromkeys((git_dir, common_dir)):
            field(digest, os.fsencode(admin_dir))
            tree(admin_dir)
        snapshots.append(digest.hexdigest())
    return tuple(snapshots)


def require_ext_review_contract(*, timeout: float = 10) -> str:
    """Require the CLI command and connected PurpleMux server API for external review."""
    executable = shutil.which("purplemux")
    if executable is None:
        raise RuntimeError("Review requires the PurpleMux 0.5.0 ext-review CLI")
    for args, required in (
        (("help",), "ext-review create --socket"),
        (("api-guide",), "POST /api/cli/ext-reviews"),
    ):
        try:
            result = subprocess.run(
                [executable, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                f"PurpleMux ext-review contract could not be verified: {exc}"
            ) from exc
        if result.returncode or required not in result.stdout:
            raise RuntimeError(
                "Review requires a running PurpleMux 0.5.0 or newer server "
                "and matching CLI with public ext-review support"
            )
    return str(Path(executable).resolve())


def generate_review_workflow(config: ReviewInput) -> str:
    """Place Review sequencing and result checks in visible, plain Python."""
    return f"""import json
import time

from purplemux_client import CreateSessionRequest, CreateWorkspaceRequest, PurpleMuxRuntime, emit_step
from purplemux_client.errors import MutationOutcomeUnknown, SessionReadyTimeout, WorkerFailure, WorkerInterrupted
from purplemux_client.review import require_ext_review_contract, serialize_review_result, snapshot_review_repositories

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


def turn(message, *, finish=False):
    seconds = max(remaining() if not finish else deadline - time.monotonic(), 0)
    if finish:
        seconds = max(seconds, 60)
    try:
        client.send_input(tab, message)
        client.wait_for_turn_completion(tab, seconds, on_busy_timeout=busy_timeout)
        if not finish:
            remaining()
        return client.read_result(tab)
    finally:
        verify_repositories()


def verify_repositories():
    current = snapshot_review_repositories(REPOSITORIES)
    changed = [path for path, before, after in zip(REPOSITORIES, baseline, current) if before != after]
    if changed:
        raise RuntimeError("Review repository change detected: " + json.dumps(changed))


emit_step("Review", "started")
deadline = time.monotonic() + {config.timeout}
baseline = None
runtime = PurpleMuxRuntime(owned_by_run=True)
client = None
tab = None
try:
    ext_review_cli = require_ext_review_contract(timeout=min(10, remaining()))
    baseline = snapshot_review_repositories(REPOSITORIES)
    workspace = runtime.create_workspace(CreateWorkspaceRequest(
        cwd=REPOSITORIES[0], name="AWM Review", deadline_check=remaining,
    ))
    client = runtime.workspace(workspace.id)
    tab = client.create_session(CreateSessionRequest(
        worker=AGENT, cwd=REPOSITORIES[0], command=AGENT,
        name="Review agent", deadline_check=remaining,
    ))
    result = None
    try:
        client.wait_until_ready(tab, min(remaining(), 60))
    except (SessionReadyTimeout, TimeoutError, WorkerFailure) as exc:
        if isinstance(exc, (WorkerInterrupted, MutationOutcomeUnknown)):
            raise
        result = {{
            "verdict": "BLOCKED",
            "summary": "Review agent was unavailable before observation: " + str(exc),
            "observability_gaps": ["Agent readiness could not be confirmed: " + str(exc)],
        }}
    context = ("Review these local repositories: " + json.dumps(REPOSITORIES)
               + ". Read and inspect every declared repository as needed, using any available tool. "
        + "You may operate a browser through any available browser tool; no particular library is required. "
        + "For read-only observation of an external terminal, use the verified PurpleMux CLI " + json.dumps(ext_review_cli) + " ext-review create --socket PATH --session SESSION --window @ID with a known socket, session, and allowed window targets; open its returned browser URL. "
               + "Do not modify the repositories or send input to observed external terminals. ")
    start_completed = result is None
    if result is None and START is not None:
        try:
            turn(context + "First, follow this start instruction and report what you did: " + START)
        except (TimeoutError, WorkerFailure) as exc:
            if isinstance(exc, (WorkerInterrupted, MutationOutcomeUnknown)):
                raise
            start_completed = False
            result = {{
                "verdict": "BLOCKED",
                "summary": "Review start observation was unavailable: " + str(exc),
                "observability_gaps": ["Start completion could not be confirmed: " + str(exc)],
            }}
    check_completed = result is None
    if result is None:
        try:
            report = turn(context + "Perform this check: " + CHECK + "\\nReturn one JSON object with verdict PASS, FAIL, or BLOCKED and a non-empty summary. Optional findings, observed_facts, evidence, hypotheses, and observability_gaps are arrays of strings. Report BLOCKED when observation times out or is unavailable; describe what could not be observed in observability_gaps. Base PASS or FAIL on observed evidence.")
        except (TimeoutError, WorkerFailure) as exc:
            if isinstance(exc, (WorkerInterrupted, MutationOutcomeUnknown)):
                raise
            check_completed = False
            result = {{
                "verdict": "BLOCKED",
                "summary": "Review observation was unavailable: " + str(exc),
                "observability_gaps": [str(exc)],
            }}
        else:
            try:
                result = json.loads(report)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Review agent did not return JSON") from exc
    if (not isinstance(result, dict) or result.get("verdict") not in ("PASS", "FAIL", "BLOCKED")
            or not isinstance(result.get("summary"), str) or not result["summary"].strip()
            or any(not isinstance(result.get(name, []), list)
                   or any(not isinstance(item, str) for item in result.get(name, []))
                   for name in ("findings", "observed_facts", "evidence", "hypotheses", "observability_gaps"))):
        raise RuntimeError("Review agent returned an invalid report")
    serialized_result = serialize_review_result(result, REPOSITORIES)
    if FINISH is not None and start_completed and check_completed:
        try:
            turn("The check produced this report: " + serialized_result + "\\nNow follow this finish instruction and report what you did: " + FINISH, finish=True)
        except (TimeoutError, WorkerFailure) as exc:
            if isinstance(exc, (WorkerInterrupted, MutationOutcomeUnknown)):
                raise
            serialized_result = serialize_review_result(
                result, REPOSITORIES,
                finish_failure="Finish could not be confirmed: " + str(exc),
            )
    elif FINISH is not None and start_completed:
        serialized_result = serialize_review_result(
            result, REPOSITORIES,
            finish_failure="Finish could not run because check completion was not confirmed",
        )
    client.close_session(tab)
    tab = None
    verify_repositories()
    print(serialized_result)
except BaseException as exc:
    failure = exc
    if client is not None and tab is not None:
        try:
            client.close_session(tab)
            tab = None
        except BaseException as stop_error:
            failure = RuntimeError("Review agent could not be stopped: " + str(stop_error) + "; prior failure: " + str(failure))
    if baseline is not None:
        try:
            verify_repositories()
        except BaseException as check_error:
            failure = check_error
    emit_step("Review", "failed", error=str(failure))
    raise failure
else:
    emit_step("Review", "completed", workspace=workspace.id)
"""
