# Python Workflow Guide

Use this file as the contract when generating a workflow script. The generated
plain Python script is the source of truth. Do not create a workflow framework,
graph, DSL, state machine, or UI-side copy of its control flow.

**Canonical version-development sample:**
[sequential multi-Issue implementation and review](../../../examples/sequential-version-development.py).
It is the primary adaptable reference for sequential Issue PRs, independent
per-Issue review/fix loops, safe resume, and a final version PR. Its separate
whole-version review is mandatory because defects in shared state, lifecycle,
security, and cross-feature behavior may appear only after approved changes are
combined. The final PR becomes Ready only after that review approves it and is
never merged into `main` automatically.

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

## Static Validation and whole-program Dry Run

Static Validation remains side-effect-free. To opt a trusted workflow into Dry
Run, declare the supported contract literally:

```python
WORKFLOW_DRY_RUN = 1
```

Validation reports detectable raw mutation-capable calls as Dry-Run-ineligible.
This is a workflow contract check, not a security sandbox. Dry Run executes the
same plain Python program and its real read-only inspections, then terminates at
the first inspection-aware mutation boundary. It never invents the mutation's
result or continues into later Python branches. Use `emit_finding()` to expose
runtime and Git/GitHub facts reached before that frontier.

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

## Optional execution outline

A workflow may declare a coarse, static list of expected steps for operator
orientation:

```python
WORKFLOW_OUTLINE = [
    "prepare integration branch",
    "implement Issue",
    "review Issue",
    "final integration review",
    "ready PR",
]
```

`WORKFLOW_OUTLINE` must be a literal `list[str]` with at most 100 items. Each
label must be non-empty, printable human-readable text of at most 200
characters. Validation reads this declaration from the syntax tree and never
executes workflow code to discover it. The declaration is optional.

The Runner snapshots the outline with the submitted run. A matching
`emit_step()` name may update its display from pending to running, completed, or
failed. Dynamic or unmatched progress remains visible in the Progress panel.
The outline is observation metadata only: it must never drive sequencing,
branching, retries, cleanup, Resume, or any other workflow decision. Keep all
control flow in plain Python; do not encode graphs, dependencies, or conditions
in the outline.

## Installed Python API

Import the public names from `purplemux_client`:

```python
from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    MutationOutcomeUnknown,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    ResultNotReady,
    ResumeCheckpoint,
    ShellCommandRequest,
    ShellResult,
    SessionReadyTimeout,
    TerminalSessionError,
    WorkerFailure,
    WorkerInterrupted,
    WorkerNeedsInput,
    emit_step,
    emit_finding,
    resume_checkpoint,
    save_checkpoint,
    suspend_run,
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
        name="Issue 123 implementer",
        correlation_id="issue-123-implementer-ab12",
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
    failure = shell_result.failure_message("focused tests")
    emit_step(
        "focused tests",
        "failed",
        error=failure,
        workspace=workspace_id,
        tab=shell_tab,
    )
    raise WorkerFailure(failure)
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
or retain the tab. For a nonzero result, the client also attempts a diagnostic-only
screen capture and retains a bounded tail on `ShellResult`. `failure_message()`
formats that tail with the resolved cwd and workspace/tab references for Runner
display. Capture text is never parsed for completion or workflow control, and a
capture error is reported as secondary context without replacing the exit code.

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
- `WorkerNeedsInput`: the agent needs additional input. Unlike hard worker
  failure, this derives directly from `TerminalSessionError` so a workflow can
  suspend for a human response explicitly.
- `WorkerInterrupted`: the current turn was interrupted.
- `ResultNotReady`: no fresh structured result is ready.
- `MutationOutcomeUnknown`: a mutation timed out and may have happened remotely.

## Workspace creation

Use the inspection-aware runtime adapter rather than a raw subprocess:

```python
runtime = PurpleMuxRuntime(owned_by_run=True)
workspace = runtime.create_workspace(
    CreateWorkspaceRequest(
        cwd="/absolute/repo",
        name="owner/project dev/v1.2.3",
        correlation_id="version-development-ab12",
    )
)
client = runtime.workspace(workspace.id)
```

The adapter captures a complete workspace listing, creates exactly once with the
saved non-secret correlation, and confirms any response ID against a new matching
workspace in a second authoritative listing. Unknown outcomes are never retried.
`list_sessions()` provides the corresponding complete structured tab discovery.
No screen or tmux state participates in identity.

## Mutation and read semantics

Mutations are workspace creation, `create_session`, `start_shell` (tab creation
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

Workflow runs opt into automatic PurpleMux resource registration:

```python
runtime = PurpleMuxRuntime(owned_by_run=True)
workspace = runtime.create_workspace(request)
client = runtime.workspace(workspace.id)
```

The default `owned_by_run=False` path is intentionally registration-free for
Prompt mode and other direct adapter use. Git worktree registration must include
the repository and immutable ownership evidence: the absolute Git directory and
filesystem identities for both the worktree path and its `.git` administrative
file. Registration-time HEAD and branch may be retained as diagnostics, but
Cleanup does not require them to remain fixed because normal branch preparation
and commits change both after a detached worktree is created.

Do not automatically close a Workflow run's tabs on success or failure. The
Runner retains the structured inventory on the run record and exposes one manual
Cleanup action after execution ends. Cleanup verifies identities, closes child
tabs in reverse deterministic order, removes managed-shell result directories,
deletes an identity-verified empty workspace through PurpleMux's public atomic
`workspace delete -w ID --if-empty` contract, and then handles the Git worktree.
Startup rejects PurpleMux versions without that contract. Only its structured
`not-empty` response proves rejection; transport errors and other nonzero exits
remain uncertain until authoritative workspace listing reconciles them.
Managed-shell directories are registered with their no-follow filesystem
identity. Cleanup stops before dependent parent resources when an outcome is
blocked. A workflow may use `capture_screen` for diagnostics, without parsing it
as a result. Prompt mode does not use this Workflow resource model. Cleanup and
Resume are mutually exclusive, and cleanup permanently disables Resume for that
run.

## Explicit checkpoints and manual recovery

The Runner resumes only a failed or suspended run that published a safe
checkpoint. Checkpoints are execution metadata, not progress and not a workflow
graph. Save one only after preceding side effects have completed:

```python
checkpoint = resume_checkpoint()
if checkpoint is None:
    workspace_id = create_workspace()
    client = PurpleMuxCLIClient(workspace_id)
    implementer = client.create_session(...)
    save_checkpoint(
        "sessions ready",
        {"workspace": workspace_id, "implementer": implementer},
    )
else:
    if checkpoint.name != "sessions ready":
        raise WorkerFailure(f"unsupported checkpoint: {checkpoint.name}")
    workspace_id = checkpoint.data["workspace"]
    implementer = checkpoint.data["implementer"]
    client = PurpleMuxCLIClient(workspace_id)
    # Validate the retained tab/repository/manual repair before continuing.
```

The workflow must branch before any completed non-idempotent action, reuse
checkpoint IDs, and validate assumptions that manual repair could change.
It may leave a checkpoint active only when every operation from that point to
the next checkpoint is safe/idempotent to re-enter after an arbitrary failure.
Checkpoint values are exposed in the UI/API, so store only short non-secret
strings. A checkpoint event over 4 KiB is rejected. On each Resume click the
Runner re-runs preflight and the same saved script, cwd, and arguments under the
same run ID; output and terminal attempt history are appended. It never edits
or restores repository files, so manual changes are preserved unless the
workflow itself overwrites them.

For an agent question, save a safe checkpoint and convert the typed condition
to a suspended run while leaving the PurpleMux tab open:

```python
try:
    client.wait_for_turn_completion(implementer, TURN_TIMEOUT)
except WorkerNeedsInput as exc:
    save_checkpoint(
        "implementer needs input",
        {"workspace": workspace_id, "implementer": implementer},
    )
    suspend_run(str(exc))
```

If safe continuation cannot be represented this way, do not publish a
checkpoint. The UI will explain that the run cannot be resumed, and the user
must start a new workflow-specific recovery path. Resume state lasts only for
the current Runner process; this is deliberately not a persistence framework.

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

## Inspect a running workflow agent

After a workflow emits progress with `workspace` and `tab`, the Runner displays
the pair as `workspace / tab`. Copy those IDs into the existing public PurpleMux
CLI commands to inspect the agent without interrupting it:

```text
purplemux tab status -w WS_ID TAB_ID
purplemux tab result -w WS_ID TAB_ID
purplemux tab capture -w WS_ID TAB_ID
```

Each command has a distinct purpose:

- `status` is the structured runtime state for the tab. Use it to check the
  current lifecycle, process, and agent state.
- `result` is the latest completed structured agent response. While the current
  turn is still running, it may be unavailable or still refer to an earlier
  completed turn; the workflow must continue to use the client's correlated
  wait/read sequence for control.
- `capture` is a diagnostic pane snapshot only. It can help an operator see
  current terminal activity, but its screen text is not runtime state or an
  agent result.

These commands are for observation and manual diagnosis. Do not parse `capture`
output to decide workflow completion, branching, retries, or approval. Keep
those decisions in plain Python and use structured PurpleMux status/results
through `purplemux_client`.

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
- invent additional checkpoint/graph semantics or missing APIs beyond the
  explicit `save_checkpoint()` / `resume_checkpoint()` contract.

## Complete example: implement, review, fix, and ready a PR

This example creates a workspace and separate Codex sessions, asks the
implementer to create a Draft PR, performs at most four independent reviews,
routes actionable findings back to the implementer, marks the PR ready after
approval, closes sessions on success, and keeps them for inspection on failure.

```python
from __future__ import annotations

from pathlib import Path

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
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
WORKFLOW_DRY_RUN = 1


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
runtime = PurpleMuxRuntime(owned_by_run=True)
workspace = runtime.create_workspace(
    CreateWorkspaceRequest(
        cwd=str(REPO),
        name="Issue 123 workflow",
        correlation_id="issue-123-workspace",
    )
)
workspace_id = workspace.id  # Do not retry automatically on an unknown outcome.
emit_step("workspace create", "completed", workspace=workspace_id)

client = runtime.workspace(workspace_id)
session_ids: list[str] = []
workflow_succeeded = False

try:
    emit_step("implementer create", "started", workspace=workspace_id)
    implementer = client.create_session(
        CreateSessionRequest(
            worker="codex",
            cwd=str(REPO),
            command="codex",
            name="Issue 123 implementer",
            correlation_id="issue-123-implementer",
        )
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
        CreateSessionRequest(
            worker="codex",
            cwd=str(REPO),
            command="codex",
            name="Issue 123 reviewer",
            correlation_id="issue-123-reviewer",
        )
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
```

Replace constants and prompts for the requested Issue, but keep orchestration,
review separation, bounded attempts, and mutation handling explicit in plain
Python. Resource destruction is the separate, explicit run Cleanup action.
