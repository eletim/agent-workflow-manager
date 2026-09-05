# Python Workflow Guide

Use this file as the contract when generating a workflow script. The generated
plain Python script is the source of truth. Do not create a workflow framework,
graph, DSL, state machine, or UI-side copy of its control flow.

**Lower-level direct-execution sample:**
[sequential multi-Issue implementation and review](../../../examples/sequential-version-development.py).
That configurable CLI sample intentionally operates on its explicit repository
path. For normal repository-modifying Workflow mode, use the isolated preparation
pattern documented below.
It is the primary adaptable reference for sequential Issue PRs, independent
per-Issue review/fix loops, explicit new-run recovery, and a final version PR. Its separate
whole-version review is mandatory because defects in shared state, lifecycle,
security, and cross-feature behavior may appear only after approved changes are
combined. The final PR becomes Ready only after that review approves it and is
never merged into `main` automatically.

## Issue Driven generation

Issue Driven mode is a deterministic authoring aid in front of this same contract.
It validates a deliberately small JSON object (repository, integration/final
branches, ordered Issue numbers, bounded review count, and merge/review policies),
then displays a complete generated plain-Python workflow. Static Validation, Dry
Run, and Run operate on that generated Python through the existing Runner path.
The JSON is never interpreted as runtime control flow, and it has no generic
actions, conditions, loops, graph edges, or executable nesting.

The generated workflow uses the canonical commit-and-clean CodingAgent
postcondition, fills safe push/Draft-PR gaps before independent review, and uses
Runner-scoped correlation through named PurpleMux resources and
`run_correlation()`. It does not generate UUIDs or correlation tokens. With
`merge_final: false`, it generates no final-branch merge call. The displayed
Python remains the sole execution and control-flow source of truth.

## Architecture and responsibility

```text
plain Python workflow
  -> purplemux_client
  -> PurpleMux public CLI/runtime
  -> Codex, Claude, or managed Bash terminal

Runner UI = execute / stop / observe state, Progress, Findings, and result
PurpleMux UI = Workflow/child terminal output, runtime inspection, intervention
```

- The Python script owns sequencing, branching, retry limits, prompts, Git
  constraints, success criteria, and cleanup policy.
- `purplemux_client` is a thin adapter over public `purplemux` CLI commands.
- PurpleMux owns agent runtime state, launch commands, and workspace directories.
- The Runner launches one trusted Python Workflow in a visible PurpleMux Bash
  tab, stops it through PurpleMux, and observes its structured exit result and
  explicitly emitted progress. It is not a workflow engine.
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

The Python workflow is the source of truth for repository execution context.
Declare the source repository and remote base with literal keyword values so
Static Validation and Dry Run can inspect them without guessing path variables:

```python
from purplemux_client import prepare_run_repository

context = prepare_run_repository(
    repo="~/DevEnv/project",
    base_branch="main",
)
REPO = context.execution_root
```

The helper resolves the source repository and exact current remote base SHA,
creates and verifies a fresh detached run worktree under the AWM-owned data
directory, registers it for explicit Cleanup, and returns both source and
execution identities. Use `context.execution_root` for Git/GitHub operations,
PurpleMux workspace creation, shell steps, and agent `cwd` values. The original
checkout is never switched, reset, stashed, or cleaned.

The validated topology layer accepts that clean detached root as the exact base
for `GitRepository.recover_feature_branch(..., expected_base_sha=context.base_sha)`.
This keeps branch preparation inside the isolated worktree even when the source
checkout already has the configured base branch checked out. If the logical
feature branch is also checked out in another worktree, the topology layer uses
a unique `awm-run/...` local branch. Recovery inspects logical and prior-run
private refs, selects their single furthest descendant of the exact authoritative
base, and fails closed if safe candidates diverge. A recovered commit can satisfy
an unchanged agent turn; otherwise require the CodingAgent's new commit and clean
worktree with `require_committed_result()`. Use `ensure_pushed()` to complete
delivery through the logical remote branch name. It creates an absent branch or
fast-forwards a behind branch only; remote-ahead and divergence fail closed. The
Workflow must then create or reuse and verify the exact Draft PR before starting
review. Push and PR creation may be agent conveniences, but are not CodingAgent
hard postconditions.

The workflow process itself runs in a PurpleMux Bash tab from a stable
Runner-controlled directory;
that directory is not the project and is not editable in Workflow mode. Put
each workflow argument on its own line in **Arguments**. Lower-level PurpleMux
workspace/session APIs still accept an explicit `cwd` for direct execution and
Prompt mode.

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
the Runner-controlled subprocess directory. Keep these checks deterministic and
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
branching, retries, cleanup, or any other workflow decision. Keep all
control flow in plain Python; do not encode graphs, dependencies, or conditions
in the outline.

## Installed Python API

Import the public names from `purplemux_client`:

```python
from purplemux_client import (
    AgentReadinessProbeResult,
    BranchState,
    CreateSessionRequest,
    CreateWorkspaceRequest,
    FeaturePreparationState,
    FeatureRecoveryState,
    GitHubRepository,
    GitRepository,
    IncompletePullRequestEnumeration,
    MergeResult,
    MutationConflict,
    MutationOutcomeUnknown,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    PullRequestState,
    PullRequestTopologyError,
    ResultNotReady,
    RepositoryExecutionContext,
    RepositoryPreparation,
    ShellCommandRequest,
    ShellResult,
    SessionReadyTimeout,
    TerminalSessionError,
    TabState,
    WorkerFailure,
    WorkerInterrupted,
    WorkerNeedsInput,
    WorkspaceState,
    WorktreeState,
    emit_step,
    emit_finding,
    inspect_run_repository,
    prepare_run_repository,
    register_run_resource,
    run_correlation,
)
```

These are the workflow-facing exports. `IssueDrivenConfig`,
`IssueDrivenFinding`, `IssueDrivenValidationError`,
`parse_issue_driven_json()`, and `generate_issue_driven_workflow()` are also
public, but they author workflows before execution; generated workflows do not
use them as runtime orchestration primitives. Names from package submodules that
are absent from `purplemux_client.__all__` are private implementation details.
There is no public checkpoint, `ResumeCheckpoint`, `save_checkpoint()`,
`resume_checkpoint()`, or `resume_shell()` API.

The request and returned-state shapes used by workflow code are:

```python
CreateWorkspaceRequest(cwd, name, correlation_id=None)
CreateSessionRequest(worker, cwd, command, metadata={}, name=None,
                     correlation_id=None)
ShellCommandRequest(command, cwd, name, correlation_id=None)

WorkspaceState(id, name, directories)
TabState(id, workspace_id, name, panel_type, provider, alive=None,
         cli_state=None)
ShellResult(exit_code, diagnostic_output=None, diagnostic_error=None, cwd=None,
            workspace_id=None, tab_id=None)
BranchState(name, local_sha, remote_sha, current)
WorktreeState(root, current_branch, dirty, status)
FeaturePreparationState(branch, base, expected_base_sha, base_is_ancestor,
                        action)
FeatureRecoveryState(branch, reused_existing_work)
PullRequestState(number, url, state, is_draft, head_repository, head_branch,
                 head_sha, base_repository, base_branch, base_sha,
                 merge_commit_sha, auto_merge_enabled, merge_queue_entry,
                 node_id, body)
MergeResult(pr, merge_commit_sha, reconciled=False)
RepositoryPreparation(source_repository, remote, base_branch, base_ref, base_sha)
RepositoryExecutionContext(source_repository, remote, base_branch, base_ref,
                           base_sha, execution_root)
```

Treat returned state as immutable observations. Re-read authoritative state
after another actor could have changed it; do not edit these values to represent
a desired result.

Construct a client for an existing workspace:

```python
client = PurpleMuxCLIClient(
    workspace_id,
    poll_interval_seconds=1.0,
    command_timeout_seconds=30.0,
    read_timeout_retries=1,
)
```

The public session operations and their exact workflow-facing signatures are:

```python
session_id = client.create_session(
    CreateSessionRequest(
        worker="codex",  # Codex or Claude alias; see below
        cwd="/absolute/repo",
        command="codex",
        metadata={},
        name="Issue 123 implementer",
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

In full, the less commonly used inspection/cleanup signatures are:

```python
tabs = client.list_sessions()
probe = client.probe_agent_readiness(
    provider="codex",
    probe_name="preflight [awm:CORRELATION]",
    correlation_id="CORRELATION",
    preexisting_tab_ids=tuple(tab.id for tab in tabs),
    timeout_seconds=60,
    on_identified=None,  # Callable[[TabState], None] | None
)
client.close_session(session_id, expected_state=tab_state)
```

`probe_agent_readiness()` is a specialized create/inspect/close preflight. Its
input tab IDs must be a complete current listing, its name must contain the
correlation ID, and it returns `AgentReadinessProbeResult`. Prefer ordinary
session creation in workflow bodies. Pass `expected_state` when explicitly
closing an identified tab so cleanup fails on an identity mismatch.

Shell operations are separate from agent turn operations:

```python
shell_tab = client.start_shell(
    ShellCommandRequest(
        command="uv run pytest tests/test_feature.py",
        cwd="/absolute/repo",
        name="Issue 123 run: focused tests",
    ),
    on_created=None,  # Callable[[tab_id, result_path], None] | None
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

The exact lifecycle signatures are:

```python
shell_tab = client.start_shell(request, on_created=callback)
client.wait_for_shell_completion(shell_tab, timeout_seconds=900)
result = client.read_shell_result(shell_tab)
```

`start_shell()` first creates and authoritatively identifies the terminal, then
registers its tab and managed-result directory when `owned_by_run=True`, records
their in-process association, calls `on_created(tab_id, result_path)` if supplied,
and only then sends the command wrapper. The callback is therefore the supported
hook for a caller that must durably record both identities before command
dispatch. It must return normally; if it raises, the identified tab and result
path are deliberately retained. For normal Runner workflows, use
`owned_by_run=True`: automatic resource registration already provides that
durable cleanup inventory, so no callback is needed.

The callback does not create a resumable shell handle. `read_shell_result()` and
`wait_for_shell_completion()` require the same live client object's in-memory
association. A terminated workflow must be recovered as a new run; there is no
`resume_shell()` operation.

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
- `WorkerNeedsInput`: the agent needs additional input. This derives directly
  from `TerminalSessionError`; the current run fails and retains diagnostics so
  the operator can answer or inspect before starting a new run.
- `WorkerInterrupted`: the current turn was interrupted.
- `ResultNotReady`: no fresh structured result is ready.
- `MutationOutcomeUnknown`: a mutation timed out and may have happened remotely.

`MutationConflict` reports a quiescent but conflicting post-state.
`PullRequestTopologyError` reports unsafe or ambiguous PR topology, and
`IncompletePullRequestEnumeration` means a bounded GitHub listing could not
prove it was exhaustive. These derive from `WorkerFailure`.

## Repository and GitHub API

Open pinned repository adapters with:

```python
repo = GitRepository.open(
    path,
    remote="origin",
    expected_github_slug="OWNER/REPO",
    command_timeout_seconds=30.0,
)
github = GitHubRepository.open(
    "OWNER/REPO",
    executable="gh",
    command_timeout_seconds=30.0,
    read_timeout_retries=1,
    page_size=100,
    max_pages=10,
)
```

The supported Git inspection and assertion methods are:

```python
repo.inspect_worktree() -> WorktreeState
repo.inspect_branch(branch) -> BranchState
repo.inspect_feature_preparation(
    branch, *, base, expected_base_sha=None
) -> FeaturePreparationState
repo.require_clean() -> None
repo.require_current_branch(branch) -> BranchState
repo.require_pushed(branch) -> BranchState
repo.require_committed_result(
    branch, *, previous_sha, allow_unchanged=False
) -> BranchState
repo.require_contains(branch, commit_sha) -> None
```

The inspection-aware Git operations that may mutate are:

```python
repo.ensure_pushed(branch, *, expected_local_sha) -> BranchState
repo.synchronize_branch(branch) -> BranchState
repo.prepare_feature_branch(
    branch, *, base, expected_base_sha
) -> BranchState
repo.recover_feature_branch(
    branch, *, base, expected_base_sha
) -> FeatureRecoveryState
repo.advance_after_merge(
    branch, *, previous_sha, merge_commit_sha, required_commit_sha
) -> BranchState
```

They validate repository identity, cleanliness, exact SHAs, ancestry, and
fast-forward-only topology. They may return without mutation when the desired
state already exists. They never reset, force-push, or hide divergence.

The supported GitHub inspections and mutations are:

```python
github.find_pr(*, head, base, state) -> PullRequestState | None
github.require_pr(
    *, head, base, number=None, state="OPEN", expected_head_sha=None,
    expected_base_sha=None, draft=None
) -> PullRequestState
github.create_draft_pr(
    *, head, base, expected_head_sha, expected_base_sha, title, body,
    correlation_id
) -> PullRequestState
github.set_draft(
    pr, *, draft, expected_head, expected_head_sha, expected_base,
    expected_base_sha
) -> PullRequestState
github.merge_pr(
    pr, *, expected_head, expected_head_sha, expected_base, expected_base_sha,
    method="merge"
) -> MergeResult
```

`state` is exactly `"OPEN"`, `"MERGED"`, or `"CLOSED"`. Open same-head PRs to
the wrong base, duplicate exact PRs, changing SHAs, auto-merge, and merge-queue
state fail closed. `create_draft_pr()` embeds the required correlation marker.
`merge_pr()` supports only an immediate merge commit and verifies its parents
and the resulting base ref; it never queues, squashes, rebases, or enables
auto-merge.

The repository execution helpers are:

```python
inspect_run_repository(
    *, repo, base_branch, remote="origin", command_timeout_seconds=30.0
) -> RepositoryPreparation
prepare_run_repository(
    *, repo, base_branch, remote="origin", worktree_root=None,
    command_timeout_seconds=30.0
) -> RepositoryExecutionContext
run_correlation(logical_name) -> str
```

`inspect_run_repository()` is read-only. `prepare_run_repository()` is an
inspection-aware mutation that creates and registers an isolated worktree, or
reconciles the exact correlated worktree if the mutation outcome was uncertain.
`run_correlation()` is deterministic within one Runner run (and process-stable
outside it); use it for logical resource names, never as a secret.

## Workspace creation

Use the inspection-aware runtime adapter rather than a raw subprocess:

```python
runtime = PurpleMuxRuntime(owned_by_run=True)
workspace = runtime.create_workspace(
    CreateWorkspaceRequest(
        cwd="/absolute/repo",
        name="owner/project dev/v1.2.3",
    )
)
client = runtime.workspace(workspace.id)
```

The exact workspace-level signatures are:

```python
runtime.list_workspaces() -> tuple[WorkspaceState, ...]
runtime.create_workspace(request) -> WorkspaceState
runtime.workspace(workspace_id) -> PurpleMuxCLIClient
runtime.delete_workspace(workspace_id, *, expected_state) -> None
```

Explicit deletion is an identity-checked, empty-workspace-only cleanup primitive.
Normal Workflow code must leave owned resources for the Runner's manual Cleanup
action instead of calling it during success or failure handling.

The adapter derives a stable correlation from the Runner's run identity and the
logical `name`, captures a complete workspace listing, creates exactly once, and
confirms any response ID against a new matching workspace in a second
authoritative listing. The same logical name is stable within one Run and differs
between Runs. Direct execution outside the Runner uses one process-stable random
namespace. Pass an explicit `correlation_id` only for compatibility or special
reconciliation needs. Unknown outcomes are never retried.
`list_sessions()` provides the corresponding complete structured tab discovery.
No screen or tmux state participates in identity.

## Mutation and read semantics

The following categories are normative:

- **Read-only:** runtime/session listings; status, result, completion, and screen
  reads; repository and PR `inspect_*`, `find_*`, and `require_*` operations;
  `inspect_run_repository()`; and correlation/progress metadata. These may retry
  read timeouts according to the adapter's `read_timeout_retries`.
- **Inspection-aware, reconciliation-capable mutation:**
  `prepare_run_repository()`; workspace/tab creation and identity-checked
  deletion/close; `interrupt()`; every Git mutation listed above; and
  `create_draft_pr()`, `set_draft()`, and `merge_pr()`. Each captures exact
  preconditions, dispatches at most once, and inspects an authoritative
  postcondition. It can return the confirmed desired result, report a proven
  rejection/conflict, or raise `MutationOutcomeUnknown` if inspection still
  cannot distinguish the outcome. Reconciliation is not a promise of success.
- **Unknown-outcome mutation without a sufficient remote postcondition:**
  `send_input()`. A successful synchronous response is accepted, but a timeout
  cannot prove whether the prompt was delivered. Do not retry it. `start_shell()`
  is compound: tab creation is correlated and reconcilable, while its command
  send has the same unknown-outcome rule. An error after creation includes the
  tab ID; retain it for inspection.

All supported mutation helpers call the common Dry Run boundary immediately
before each actual dispatch. Dry Run performs the same identity/topology reads
and no-op checks as Run. If the desired state already exists, a higher-level
helper may return normally and execution can reach a later boundary. If a
mutation is required, Dry Run reports that exact planned operation and exits
without dispatching it, running callbacks, or fabricating a result. Raw
subprocess/CLI mutation calls are not inspection-aware and make Static
Validation mark the workflow Dry-Run-ineligible.

Never catch `MutationOutcomeUnknown` and immediately repeat the operation. A
later run may invoke a high-level reconciliation-capable helper only after its
ordinary authoritative preflight proves whether work remains. There is no such
safe retry for an ambiguously delivered agent prompt or shell command.

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
as a result. Prompt mode does not use this Workflow resource model. Cleanup is
explicit and does not delete the historical run record.

## Manual recovery through a new run

Checkpoint and in-place Resume are not supported Workflow APIs. Failed and
stopped runs remain inspectable, including output and owned PurpleMux resources,
but their terminated Python processes are not reconstructed.

Start a new run to recover. Its ordinary Python code should inspect exact Git
branches and commits, GitHub PR topology, and any relevant PurpleMux resources
before reusing external work or making a new mutation. Keep mutation-once and
`MutationOutcomeUnknown` protections: reconcile a possibly dispatched mutation
from authoritative state and never retry it blindly. This recovery model does
not add a graph, state machine, durable execution store, or automatic retry.

Use these examples when reasoning about resumability, even if an external caller
records its own phase label:

- **Safe after completed side effects:** a committed clean agent result plus its
  exact SHA, or a pushed branch plus an exact verified Draft PR, can be observed
  by a new run. Re-enter through `recover_feature_branch()`, `ensure_pushed()`,
  and `require_pr()`; do not replay the completed agent prompt.
- **Unsafe before a non-idempotent mutation:** a label such as `send_pending` or
  `merge_pending` says only that an operation may have happened. It is not a
  resumable checkpoint. A merge can be reconciled from exact PR/base state; an
  ambiguously delivered prompt cannot, so the run must fail for operator
  inspection rather than send it again.
- **Completed agent turn:** waiting is not enough. The workflow must read the
  fresh structured result, validate the expected committed/clean Git
  postcondition, and retain the exact resulting SHA in authoritative Git before
  treating that work as recoverable. Screen text, a progress event, or a local
  `turn_completed` flag cannot prove the result belongs to that prompt.
- **Cleanup/close uncertainty:** never mark a resource cleaned merely because a
  close was requested. Retain its identity until authoritative listing proves
  absence. The Runner's Cleanup stops before dependent parents when that proof
  is unavailable.
- **Terminal state:** an already merged Issue PR, an already Ready final PR when
  policy says not to merge it, or a final PR whose merged head is the exact
  current integration head and is contained by the final branch is success to
  inspect and return. A historical merged PR for an older integration head is
  not terminal delivery. A recovery run must not re-run the approval/final-check
  turns that produced a verified terminal state.

Thus a durable phase marker is useful only when all earlier side effects have
authoritative postconditions and re-entering from that phase performs inspection
before any new mutation. A `*_pending` marker alone never satisfies that rule.

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

Findings and advanced resource registration use:

```python
emit_finding(
    category,                # "runtime", "git", or "github"
    message,
    *,
    status="passed",        # "passed", "failed", or "info"
)
register_run_resource(kind, identity, metadata=None)
```

`emit_finding()` is observational like `emit_step()`. `register_run_resource()`
accepts `purplemux_tab`, `managed_shell_result`, `purplemux_workspace`, or
`git_worktree` plus string metadata. Runtime/worktree helpers register their own
resources when run ownership is enabled; call this advanced hook only when a
documented lifecycle requires it. Registration records an already identified
resource and is not proof that its creating mutation succeeded.

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
- invent a checkpoint, graph, state-machine, or durable-execution abstraction.

## Complete example: implement, review, fix, and ready a PR

This lower-level example focuses on agent turn orchestration. Production
repository workflows should combine it with the commit/clean and delivery gates
above; the canonical `examples/sequential-version-development.py` shows safe
push and exact Draft-PR gap absorption. All owned resources remain available
until explicit Cleanup.

```python
from __future__ import annotations

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    GitRepository,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    TerminalSessionError,
    WorkerFailure,
    emit_step,
    prepare_run_repository,
)

context = prepare_run_repository(
    repo="/absolute/path/to/repository",
    base_branch="dev/v0.1.0",
)
REPO = context.execution_root
ISSUE_URL = "https://github.com/OWNER/REPO/issues/123"
BASE_BRANCH = "dev/v0.1.0"
FEATURE_BRANCH = "feature/issue-123"
MAX_REVIEWS = 4
READY_TIMEOUT = 60
TURN_TIMEOUT = 900
WORKFLOW_DRY_RUN = 1

repository = GitRepository.open(
    REPO,
    expected_github_slug="OWNER/REPO",
)
repository.prepare_feature_branch(
    FEATURE_BRANCH,
    base=BASE_BRANCH,
    expected_base_sha=context.base_sha,
)


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
        )
    )
    session_ids.append(reviewer)
    emit_step("reviewer create", "completed", workspace=workspace_id, tab=reviewer)

    run_turn(
        client,
        implementer,
        "implementation",
        f"""Implement {ISSUE_URL}. Treat the Issue body as Source of Truth.
Work in the prepared checkout based on {BASE_BRANCH}; publish its HEAD to the
logical branch {FEATURE_BRANCH} and create no other branch yourself.
Run required format/lint/typecheck/tests and git diff --check.
Commit, push with `git push origin HEAD:refs/heads/{FEATURE_BRANCH}`, and create a
Draft PR targeting {BASE_BRANCH}.
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
Run required checks, commit, and push with
`git push origin HEAD:refs/heads/{FEATURE_BRANCH}` to the existing Draft PR.
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
