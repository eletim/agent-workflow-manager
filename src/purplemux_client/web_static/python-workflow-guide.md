# Python Workflow Guide

Use this file as the contract when generating a workflow script. The generated
plain Python script is the source of truth. Do not create a workflow framework,
graph, DSL, state machine, or UI-side copy of its control flow.

## Architecture and responsibility

```text
plain Python workflow
  -> purplemux_client
  -> PurpleMux public CLI/runtime
  -> Codex, Claude, or managed Bash terminal

Runner UI = execute / stop / observe stdout, stderr, process state, and progress
PurpleMux UI = runtime inspection and manual intervention
```

- The Python script owns sequencing, branching, retry limits, prompts, Git
  constraints, success criteria, and cleanup policy.
- `purplemux_client` is a thin adapter over public `purplemux` CLI commands.
- PurpleMux owns agent runtime state, launch commands, and workspace directories.
- The Runner executes one trusted Python process, can stop its process group, and
  observes output and explicitly emitted progress. It is not a workflow engine.
- Never operate tmux directly. Never infer completion or results from terminal
  screen text. Do not assume Graph, Node, Edge, LangGraph, or a workflow DSL.
- Shell work that should be observable or run in parallel must use
  `start_shell()` so it appears as a named PurpleMux terminal associated with
  the workflow workspace and target path. Do not hide that work in a local
  `subprocess` call.

## Run execution context

Choose the target repository in the Runner's **Working directory** field. Put
each workflow argument on its own line in **Arguments**; spaces within a line
belong to that single argument. The resolved directory and argument list are
shown with the run and apply to both preflight and execution. They are not
global workflow configuration.

Selecting a working directory prevents the workflow child from inheriting
Agent Workflow Manager's `VIRTUAL_ENV` and matching virtualenv `bin` path. It
does not activate the target repository's environment. Commands that require a
project environment should make it explicit, such as `uv run --project
/absolute/repo python -m package.module` or an absolute interpreter path.

## Preflight requirements

Run performs static validation before starting the workflow. Syntax, direct
module-level imports, direct module-level `os.environ["NAME"]` requirements, and
direct public `purplemux_client` imports are checked without importing or
executing the script. Guarded, conditional, and deferred uses are not assumed to
be mandatory. Declare requirements known before expensive agent work with an
optional literal module-level value:

```python
WORKFLOW_PREFLIGHT = {
    "commands": ["git", "gh", "uv"],
    "imports": ["project_package"],
    "environment": ["GH_TOKEN"],
    "paths": ["pyproject.toml", "/absolute/input/data.json"],
}
```

All keys are optional lists of non-empty strings. Relative paths resolve from
the selected run working directory. Keep these checks deterministic and
side-effect-free; do not use metadata as a workflow DSL. Read-only discovery is
bounded and reports a validation timeout if a lookup stalls. Preflight is
best-effort and cannot prove that dynamic code or later external operations will
succeed.

## Installed Python API

Import the public names from `purplemux_client`:

```python
from purplemux_client import (
    CreateSessionRequest,
    MutationOutcomeUnknown,
    PurpleMuxCLIClient,
    ResultNotReady,
    ShellCommandRequest,
    ShellResult,
    SessionReadyTimeout,
    TerminalSessionError,
    WorkerFailure,
    WorkerInterrupted,
    WorkerNeedsInput,
    emit_step,
)
```

Construct a client for an existing workspace:

```python
client = PurpleMuxCLIClient(
    workspace_id,
    poll_interval_seconds=1.0,
    command_timeout_seconds=30.0,
    read_timeout_retries=1,
)
```

The public session operations are:

```python
session_id = client.create_session(
    CreateSessionRequest(
        worker="codex",  # Codex or Claude alias; see below
        cwd="/absolute/repo",
        command="codex",
        metadata={},
    )
)
status = client.read_status(session_id)
client.wait_until_ready(session_id, timeout_seconds=60)
client.send_input(session_id, "one bounded task")
client.wait_for_turn_completion(session_id, timeout_seconds=900)
result_text = client.read_result(session_id)
client.interrupt(session_id)
diagnostic_text = client.capture_screen(session_id)
client.close_session(session_id)
```

Shell operations are separate from agent turn operations:

```python
shell_tab = client.start_shell(
    ShellCommandRequest(
        command="uv run pytest tests/test_feature.py",
        cwd="/absolute/repo",
        name="Issue 123 run: focused tests",
    )
)
emit_step(
    "focused tests",
    "started",
    workspace=workspace_id,
    tab=shell_tab,
)

# start_shell() returns after launch. Create/use agent sessions or start other
# shell tabs here when the tasks are independent.
client.wait_for_shell_completion(shell_tab, timeout_seconds=900)
shell_result = client.read_shell_result(shell_tab)
if shell_result.exit_code != 0:
    emit_step(
        "focused tests",
        "failed",
        error=f"exit code {shell_result.exit_code}",
        workspace=workspace_id,
        tab=shell_tab,
    )
    raise WorkerFailure(
        f"focused tests failed with exit code {shell_result.exit_code}; "
        f"inspect {workspace_id} / {shell_tab}"
    )
emit_step(
    "focused tests",
    "completed",
    workspace=workspace_id,
    tab=shell_tab,
)
```

The non-empty `name` is the PurpleMux UI label; include the logical run and task
so an operator can find the tab. `cwd` is resolved and validated before the tab
is created, then applied explicitly inside the terminal. The command runs under
`bash -lc`, so select a project environment explicitly when needed.

`wait_for_shell_completion()` uses PurpleMux's structured `alive` lifecycle and,
when available, its optional `terminalStatus` field for timeout diagnostics.
Completion and `ShellResult.exit_code` come from a machine-readable sidecar
written by the command wrapper. It never parses pane text and cannot miss a fast
command that runs between status polls. `read_shell_result()` raises
`ResultNotReady` before completion. A nonzero exit code is an explicit result,
not an automatic cleanup decision; plain Python decides whether to fail, retry,
or retain the tab.

`worker` selects `codex-cli` or `claude-code`; recognized aliases are `codex`,
`codex-cli`, `claude`, and `claude-code`. If `worker` is unrecognized, the
adapter checks `command` against the same aliases. The current adapter does not
pass `cwd`, `command`, or `metadata` through as arbitrary launch configuration;
PurpleMux owns provider launch and the workspace directory. Keep these request
fields accurate, but do not claim they configure unsupported runtime behavior.

Relevant errors all derive from `TerminalSessionError`:

- `SessionReadyTimeout`: the session did not become ready in time.
- `WorkerFailure`: CLI failure, invalid state/result, turn timeout, or another
  worker failure.
- `WorkerNeedsInput`: the agent needs additional input.
- `WorkerInterrupted`: the current turn was interrupted.
- `ResultNotReady`: no fresh structured result is ready.
- `MutationOutcomeUnknown`: a mutation timed out and may have happened remotely.

## Workspace creation

There is no workspace-creation method on `PurpleMuxCLIClient`. When a new
workspace is required, call the public CLI once and parse its JSON response:

```python
import json
import subprocess
from pathlib import Path

from purplemux_client import MutationOutcomeUnknown, WorkerFailure


def create_workspace(repo: Path, name: str) -> str:
    try:
        completed = subprocess.run(
            [
                "purplemux",
                "workspace",
                "create",
                "--cwd",
                str(repo),
                "--name",
                name,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MutationOutcomeUnknown(
            "workspace create timed out; remote outcome is unknown"
        ) from exc
    except OSError as exc:
        raise WorkerFailure(f"could not execute workspace create: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "no stderr"
        raise WorkerFailure(f"workspace create failed: {detail}")
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MutationOutcomeUnknown(
            "workspace was created but its JSON response was malformed; "
            "remote outcome is unknown"
        ) from exc
    workspace_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(workspace_id, str) or not workspace_id:
        raise MutationOutcomeUnknown(
            "workspace create returned no id; remote outcome is unknown"
        )
    return workspace_id
```

The public command is:

```text
purplemux workspace create --cwd PATH [--name NAME]
```

Workspace creation is a non-idempotent mutation. Never blindly retry it after a
timeout; inspect/list workspaces first or stop for human reconciliation.

## Mutation and read semantics

Mutations are `workspace create`, `create_session`, `start_shell` (tab creation
and one send), `send_input`, `interrupt`, and `close_session`. The adapter
attempts each mutation once. If one times out, it raises
`MutationOutcomeUnknown`: the remote side may have applied it. Do not catch that
exception and immediately repeat the mutation. A shell-start failure after tab
creation includes the tab ID in the error so it can be inspected. Preserve the
workspace/session references and reconcile the remote state first.

Read-only operations (`read_status`, result/status polling used by completion,
and `capture_screen`) may retry command timeouts according to
`read_timeout_retries`. This retry policy does not make mutations retryable.

## Turn completion and results

For every prompt, keep this order:

```python
client.wait_until_ready(session_id, 60)
client.send_input(session_id, prompt)
client.wait_for_turn_completion(session_id, 900)
text = client.read_result(session_id)
```

`send_input` records a status/result baseline. `wait_for_turn_completion`
correlates a fresh PurpleMux completion and structured result with that turn.
`read_result` returns the structured provider result text and rejects stale,
interrupted, unavailable, or not-ready results.

`capture_screen` is diagnostic pane text only. It may be printed or retained for
failure inspection, but never parse it to decide completion, approval, or the
agent result.

## Plain Python control flow

Use ordinary `for`, `if`, functions, exceptions, `try`, and `finally`:

```python
implementation = run_turn(implementer, implement_prompt)
for attempt in range(1, MAX_REVIEWS + 1):
    review = run_turn(reviewer, review_prompt)
    if decision(review) == "APPROVED":
        break
    if attempt == MAX_REVIEWS:
        raise RuntimeError("review limit reached")
    run_turn(implementer, fix_prompt(review))
```

The script—not an agent prompt and not the UI—owns this loop and its limit.

## Cleanup policy

Recommended default:

```text
SUCCESS -> close every created session once
FAILURE -> keep sessions open and print workspace/tab references for inspection
```

Do not retry a timed-out close blindly. There is currently no workspace deletion
method in `purplemux_client`; do not invent one. A workflow may use
`capture_screen` after failure for diagnostics, without parsing it as a result.

## Progress instrumentation

Progress is optional observation, not control flow:

```python
emit_step("implementation", "started", workspace=workspace_id, tab=session_id)
emit_step("implementation", "completed", workspace=workspace_id, tab=session_id)
emit_step(
    "review",
    "failed",
    iteration=2,
    error="tests failed",
    workspace=workspace_id,
    tab=reviewer_id,
)
```

The exact signature is:

```python
emit_step(
    name,
    status,                 # "started", "completed", or "failed"
    *,
    iteration=None,         # positive int
    attempt=None,           # positive int
    message=None,           # short str
    error=None,             # short str
    workspace=None,         # PurpleMux workspace id
    tab=None,               # PurpleMux tab id
)
```

Outside the Runner it is a no-op. Inside the Runner, encoded events over 4 KiB
are dropped and only the latest 200 events are retained. Do not use events to
drive the workflow, add statuses, or build decorators/state machines around it.

## Recommended patterns

One-shot task:

```python
result = run_turn(session_id, "Run the requested tests and report the result.")
```

Implement/review/fix: use separate implementer and reviewer sessions. Give each
turn one bounded role; parse a small explicit review protocol such as a first
line of `APPROVED` or `CHANGES_REQUESTED`; keep the loop in Python.

Maximum attempts: use `range(1, maximum + 1)` and fail explicitly when the last
review still requests changes. Never start an unbounded agent or Python retry
loop.

Failure: let typed client errors propagate after emitting a failed progress
event. Keep sessions open and report their IDs. A mutation with unknown outcome
requires inspection, not automatic retry.

## Anti-patterns

Do not delegate the orchestration itself to one agent:

```python
# Wrong: review separation, limits, and cleanup are hidden in one prompt.
client.send_input(session_id, "Implement, review up to four times, fix, and finish")
```

Do not:

- parse `capture_screen()` or `read_status()` message text as the agent result;
- call `read_result()` as a substitute for `wait_for_turn_completion()`;
- retry `create_session`, `send_input`, `interrupt`, `close_session`, or
  workspace creation after an unknown timeout;
- let the UI decide the next step;
- ask an implementer to self-review when an independent review is required;
- run observable/parallel Bash work as an invisible local subprocess;
- invent checkpoint/resume, graph semantics, or missing APIs.

## Complete example: implement, review, fix, and ready a PR

This example creates a workspace and separate Codex sessions, asks the
implementer to create a Draft PR, performs at most four independent reviews,
routes actionable findings back to the implementer, marks the PR ready after
approval, closes sessions on success, and keeps them for inspection on failure.

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from purplemux_client import (
    CreateSessionRequest,
    MutationOutcomeUnknown,
    PurpleMuxCLIClient,
    TerminalSessionError,
    WorkerFailure,
    emit_step,
)

REPO = Path("/absolute/path/to/repository").resolve()
ISSUE_URL = "https://github.com/OWNER/REPO/issues/123"
BASE_BRANCH = "dev/v0.1.0"
FEATURE_BRANCH = "feature/issue-123"
MAX_REVIEWS = 4
READY_TIMEOUT = 60
TURN_TIMEOUT = 900


def create_workspace() -> str:
    try:
        completed = subprocess.run(
            [
                "purplemux",
                "workspace",
                "create",
                "--cwd",
                str(REPO),
                "--name",
                "Issue 123 workflow",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MutationOutcomeUnknown(
            "workspace create timed out; remote outcome is unknown"
        ) from exc
    except OSError as exc:
        raise WorkerFailure(f"could not create workspace: {exc}") from exc
    if completed.returncode != 0:
        raise WorkerFailure(
            "workspace create failed: " + (completed.stderr.strip() or "no stderr")
        )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MutationOutcomeUnknown(
            "workspace was created but its JSON response was malformed; "
            "remote outcome is unknown"
        ) from exc
    workspace_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(workspace_id, str) or not workspace_id:
        raise MutationOutcomeUnknown(
            "workspace create returned no id; remote outcome is unknown"
        )
    return workspace_id


def short_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:500]


def run_turn(
    client: PurpleMuxCLIClient,
    tab: str,
    step: str,
    prompt: str,
    *,
    iteration: int | None = None,
) -> str:
    emit_step(
        step,
        "started",
        iteration=iteration,
        workspace=client.workspace_id,
        tab=tab,
    )
    try:
        client.wait_until_ready(tab, READY_TIMEOUT)
        client.send_input(tab, prompt)
        client.wait_for_turn_completion(tab, TURN_TIMEOUT)
        result = client.read_result(tab)
    except BaseException as exc:
        emit_step(
            step,
            "failed",
            iteration=iteration,
            error=short_error(exc),
            workspace=client.workspace_id,
            tab=tab,
        )
        raise
    emit_step(
        step,
        "completed",
        iteration=iteration,
        workspace=client.workspace_id,
        tab=tab,
    )
    return result


def review_decision(result: str) -> str:
    first_line = next(
        (line.strip() for line in result.splitlines() if line.strip()), ""
    )
    if first_line in {"APPROVED", "CHANGES_REQUESTED"}:
        return first_line
    raise WorkerFailure("review result must start with APPROVED or CHANGES_REQUESTED")


emit_step("workspace create", "started")
workspace_id = create_workspace()  # Do not retry automatically on timeout.
emit_step("workspace create", "completed", workspace=workspace_id)

client = PurpleMuxCLIClient(workspace_id)
session_ids: list[str] = []
workflow_succeeded = False

try:
    emit_step("implementer create", "started", workspace=workspace_id)
    implementer = client.create_session(
        CreateSessionRequest(worker="codex", cwd=str(REPO), command="codex")
    )
    session_ids.append(implementer)
    emit_step(
        "implementer create",
        "completed",
        workspace=workspace_id,
        tab=implementer,
    )

    emit_step("reviewer create", "started", workspace=workspace_id)
    reviewer = client.create_session(
        CreateSessionRequest(worker="codex", cwd=str(REPO), command="codex")
    )
    session_ids.append(reviewer)
    emit_step("reviewer create", "completed", workspace=workspace_id, tab=reviewer)

    run_turn(
        client,
        implementer,
        "implementation",
        f"""Implement {ISSUE_URL}. Treat the Issue body as Source of Truth.
Use the existing branch {FEATURE_BRANCH}, based on {BASE_BRANCH}; create no other branch.
Run required format/lint/typecheck/tests and git diff --check.
Commit, push, and create a Draft PR targeting {BASE_BRANCH}.
Do not merge main or {BASE_BRANCH}. Do not start a review yourself.
Return a concise implementation and verification summary.""",
    )

    approved = False
    for review_number in range(1, MAX_REVIEWS + 1):
        review = run_turn(
            client,
            reviewer,
            "review",
            f"""Independently review {FEATURE_BRANCH} against {BASE_BRANCH} and {ISSUE_URL}.
Do not modify files, commit, push, merge, or start another reviewer.
Check the current diff and run proportionate tests.
Return exactly APPROVED on the first non-empty line when there are no actionable findings.
Otherwise return CHANGES_REQUESTED on the first non-empty line, followed by specific blocking/actionable findings.""",
            iteration=review_number,
        )
        decision = review_decision(review)
        if decision == "APPROVED":
            approved = True
            break
        if review_number == MAX_REVIEWS:
            raise WorkerFailure(f"review limit {MAX_REVIEWS} reached")
        run_turn(
            client,
            implementer,
            "fix",
            f"""Fix only the actionable review findings below for {ISSUE_URL}.
Keep branch {FEATURE_BRANCH}; do not create another branch or merge anything.
Run required checks, commit, and push to the existing Draft PR.
Do not start a review yourself.

REVIEW FINDINGS:
{review}""",
            iteration=review_number,
        )

    if not approved:
        raise WorkerFailure("workflow ended without approval")

    run_turn(
        client,
        implementer,
        "pr ready",
        f"""Run final required checks for {FEATURE_BRANCH}, confirm it is pushed,
and convert its existing Draft PR targeting {BASE_BRANCH} to Ready for review.
Do not merge the PR and do not start another review. Return the PR URL and final status.""",
    )
    workflow_succeeded = True
except BaseException as exc:
    emit_step(
        "workflow",
        "failed",
        error=short_error(exc),
        workspace=workspace_id,
    )
    print(f"Workflow failed; keep workspace for inspection: {workspace_id}")
    for session_id in session_ids:
        print(f"Keep tab for inspection: {session_id}")
        try:
            diagnostic = client.capture_screen(session_id)
        except TerminalSessionError as diagnostic_error:
            print(f"Could not capture {session_id}: {diagnostic_error}")
        else:
            print(f"Diagnostic capture for {session_id}:\n{diagnostic}")
    raise
finally:
    if workflow_succeeded:
        emit_step("cleanup", "started", workspace=workspace_id)
        cleanup_errors: list[str] = []
        for session_id in reversed(session_ids):
            try:
                client.close_session(session_id)  # One attempt; never blind retry.
            except TerminalSessionError as exc:
                cleanup_errors.append(f"{session_id}: {exc}")
        if cleanup_errors:
            message = "; ".join(cleanup_errors)
            emit_step("cleanup", "failed", error=message[:500], workspace=workspace_id)
            raise WorkerFailure(f"session cleanup failed: {message}")
        emit_step("cleanup", "completed", workspace=workspace_id)
        emit_step("workflow", "completed", workspace=workspace_id)
```

Replace constants and prompts for the requested Issue, but keep orchestration,
review separation, bounded attempts, mutation handling, and cleanup explicit in
plain Python.
