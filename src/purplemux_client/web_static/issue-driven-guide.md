# Issue Driven Guide

Give this guide directly to an AI when asking it to author Issue Driven JSON.

## Architecture and source of truth

```text
Issue Driven JSON
  = compact configuration/input

generated plain Python Workflow
  = execution/control-flow Source of Truth
```

The JSON is not a workflow runtime, DSL, graph, or state machine. It selects
supported behavior in a deterministic Python generator. Use Python Workflow mode
when arbitrary control flow is required.

## Repository semantics

`repository` is the path to the existing source repository. `integration_branch`
is the remote/integration branch to develop on. By default, the generated Python
calls `prepare_run_repository(repo=repository, base_branch=integration_branch)`
to create a fresh, run-specific worktree from that branch. The user does not need
to create a version worktree first.

Set `make_integration_branch` to `true` when the integration branch may not exist
yet. The workflow creates the isolated worktree at the exact remote
`final_branch` HEAD, creates and pushes `integration_branch` from that commit,
then uses it for Issue delivery. If the integration branch already exists, the
same recovery path requires it to contain that exact final-branch HEAD and
rejects unsafe remote movement or divergence. Base PR creation is deferred while
the authoritative remote heads are identical, then performed after the first
Issue merge creates a difference; an existing Base PR retains its normal recovery
semantics. During deferral, the unchanged planner stores its decisions and
dispatch position in an AWM-owned remote Git note without advancing either
branch. This also supports dynamic and empty one-shot plans. The final PR remains
`integration_branch` to `final_branch`.

For a multi-repository run, replace the four top-level repository fields with a
`repositories` array. Each entry independently declares `repository`,
`integration_branch`, `final_branch`, and `issues`. Their order is significant.
The other settings remain at the top level and apply to every entry. The
generated plain Python contains the complete `ISSUE_DRIVEN_REPOSITORIES`
representation and invokes the existing single-repository flow for each entry
in declared order. Each repository's inspection, worktree preparation, work
items, review, and final delivery complete before the next repository is
prepared. The JSON remains configuration rather than runtime control flow.

Correct:

```json
{
  "repository": "~/DevEnv/agent-workflow-manager",
  "integration_branch": "dev/v0.2.1"
}
```

Incorrect when that version worktree does not already exist:

```json
{
  "repository": "~/DevEnv/agent-workflow-manager-v0.2.1",
  "integration_branch": "dev/v0.2.1"
}
```

## Supported schema

Unknown fields are rejected. The single-repository form provides exactly one of
`one_shot_issue`, `work_items`, or the legacy `issues` field together with its
three repository fields. The multi-repository form provides `repositories`
instead. `mode`, `make_integration_branch`, `policy_issue`, `scope_max_reviews`,
`implementer_agent`, `reviewer_agent`, and `scenarios` are otherwise optional;
the run-wide review and delivery fields are always required.

| Field | Type | Meaning |
| --- | --- | --- |
| `mode` | string | Optional discriminator; when present it must be `issue-driven`. |
| `repository` | string | Existing source repository path in the backward-compatible single-repository form. |
| `repositories` | array | Multi-repository form: an ordered array of at least two entries containing exactly `repository`, `integration_branch`, `final_branch`, and `issues`. Do not combine it with the corresponding top-level fields, `work_items`, or `one_shot_issue`. Repository paths must be unique. |
| `integration_branch` | string | Remote/integration branch used as the development base; it may be created when `make_integration_branch` is true. |
| `final_branch` | string | Branch targeted by final delivery; it must differ from `integration_branch`. |
| `make_integration_branch` | boolean | Create/recover `integration_branch` from the exact remote `final_branch` HEAD; default `false`. |
| `policy_issue` | integer | Optional positive Issue number containing version-wide design context; it must not also appear as an implementation GitHub Issue item. |
| `one_shot_issue` | integer | Positive source Issue for a manager-planned one-shot run. It starts with no work items and cannot be combined with `issues` or `work_items`. |
| `issues` | array of integers | Legacy form for positive, unique GitHub Issue numbers, executed in the listed order. Do not combine it with `work_items`. |
| `work_items` | array | Ordered GitHub Issue numbers and/or inline mini-task objects. A mini task is exactly `{"id": "lowercase-kebab-id", "task": "authoritative instruction"}` and does not require a GitHub Issue. |
| `max_reviews` | integer | Correctness and whole-version review limit from 1 through 100; reaching it continues with a structured warning after exact topology checks. Use 4 unless the user requests another value. |
| `scope_max_reviews` | integer | Optional Scope / Design Review limit from 1 through 100; default 3 when omitted, recommended value 6. It does not affect Correctness or whole-version review. |
| `implementer_agent` | string | Agent used for implementation, fixes, and cleanup; `codex` or `claude`, default `codex`. |
| `reviewer_agent` | string | Agent used for Issue and whole-version review; `codex` or `claude`, default `codex`. |
| `scenarios` | array of strings | Optional human-authored Scenario List for Whole Review. The numbered list may contain at most 100 items and 64,000 UTF-8 bytes; each item may contain at most 4,000 characters. The Scenario Gate selects a risk-relevant subset and uses AI to judge each scenario's Before/After behavioral difference. Requires `final_review: true`. |
| `merge_to_integration` | boolean | Whether each safely deliverable Issue PR is merged into the integration branch, including explicit warning continuations. |
| `final_review` | boolean | Whether the completed integration branch receives a final review. |
| `merge_final` | boolean | Whether final delivery is automatically merged into `final_branch`. |

Do not add generic `if`, `while`, action, step, or arbitrary executable blocks.

Each implementation work item is reviewed in two ordered phases. Scope / Design
Review uses `scope_max_reviews` (default 3) to check that the change is necessary,
sufficient, minimal, and placed within the right responsibilities. After that
phase, Correctness Review uses `max_reviews` to check behavior, edge cases,
safety, regressions, and tests; Whole-version Review also uses `max_reviews`.
The recommended six/four allocation reserves Scope capacity for required rechecks
whenever a Correctness fix changes the head. The counters and outcomes are
independent. A phase that exhausts its limit may continue with an explicit warning
after exact clean, pushed PR topology is revalidated; it is never recorded as
approved.

When `scenarios` is present, Whole Review first runs a Scenario Gate against the
exact final-base commit (Before) and integration-head commit (After). Each string
should describe one observable scenario and may cover existing behavior, new
behavior, or a failure path. The reviewer selects a small risk-relevant subset,
records evidence for both commits, and judges whether each behavioral difference
is appropriate in the Issue and policy context. This is intentionally not a
fixed expected-output assertion, and the gate does not need to execute every
scenario. A requested change enters the existing Whole Review fix/re-review loop.
The generated plain Python contains the list, selection prompt, gate ordering,
and review loop, so it remains the control-flow Source of Truth.

For example:

```json
"scenarios": [
  "Existing: a normal Prompt run still completes and preserves its output.",
  "New: a one-shot Issue Driven run plans and dispatches its first mini task.",
  "Failure: malformed planner output is rejected without dispatching work."
]
```

Before creating a run worktree, Static Validation and Dry Run inspect every
declared work item's remote feature branch, current integration head, and bounded
GitHub PR history. A missing feature branch is safe for a new run; a branch that
contains the current integration base and has consistent PR topology is
recoverable; and a feature head already contained by integration is reported as
already integrated. Mismatched, contradictory, or ambiguous topology fails
validation with the Issue number and authoritative Git/GitHub reason. These
checks use live remote refs and perform no branch, PR, or repository mutation.

## Canonical example

```json
{
  "mode": "issue-driven",
  "repository": "~/DevEnv/agent-workflow-manager",
  "integration_branch": "dev/v0.2.1",
  "final_branch": "main",
  "make_integration_branch": true,
  "policy_issue": 80,
  "issues": [86, 99, 87, 84],
  "max_reviews": 4,
  "scope_max_reviews": 6,
  "implementer_agent": "codex",
  "reviewer_agent": "claude",
  "merge_to_integration": true,
  "final_review": true,
  "merge_final": false
}
```

The equivalent multi-repository shape keeps the run-wide behavior at the top
level and moves the four repository-specific fields into ordered entries:

```json
{
  "mode": "issue-driven",
  "repositories": [
    {
      "repository": "~/DevEnv/api",
      "integration_branch": "dev/v1.4.0",
      "final_branch": "main",
      "issues": [41, 44]
    },
    {
      "repository": "~/DevEnv/web",
      "integration_branch": "dev/v2.1.0",
      "final_branch": "main",
      "issues": [72]
    }
  ],
  "max_reviews": 4,
  "scope_max_reviews": 6,
  "implementer_agent": "codex",
  "reviewer_agent": "claude",
  "merge_to_integration": true,
  "final_review": true,
  "merge_final": false
}
```

To mix a GitHub Issue with a workflow-local task, replace `issues` with
`work_items`:

```json
{
  "mode": "issue-driven",
  "repository": "~/DevEnv/agent-workflow-manager",
  "integration_branch": "dev/v0.2.1",
  "final_branch": "main",
  "work_items": [
    86,
    {
      "id": "refresh-run-help",
      "task": "Clarify the New Run help text and cover the wording with its existing UI test."
    }
  ],
  "max_reviews": 4,
  "merge_to_integration": true,
  "final_review": true,
  "merge_final": false
}
```

To deliver one large Issue without preparing its work-item list, use
`one_shot_issue` instead:

```json
{
  "mode": "issue-driven",
  "repository": "~/DevEnv/agent-workflow-manager",
  "integration_branch": "dev/v0.3.0",
  "final_branch": "main",
  "one_shot_issue": 169,
  "max_reviews": 4,
  "merge_to_integration": true,
  "final_review": true,
  "merge_final": false
}
```

The workflow starts this mode with an empty plan. Before every dispatch, the
dedicated manager reads the source Issue and adds short inline mini tasks whose
instructions focus on purpose and non-negotiable design decisions. It may update
or skip pending tasks as progress changes what remains. It does not create GitHub
Issues or collapse the source Issue into one implementation item. Every generated
mini task uses the normal implementation, review, recovery, and delivery path.
The source Issue is bound into persisted recovery state and linked from the Base
PR and final human handoff. Numeric Issue additions are rejected, and each added
or revised mini task receives the same authoritative remote branch, PR-state,
SHA-containment, and fingerprint checks before its dispatch is persisted.

The generated Python embeds the mini-task instruction and uses the deterministic
branch `feature/work-item-refresh-run-help`. Its recovery declaration and Draft
PR record the SHA-256 fingerprint of the authoritative task text, and recovery
fails if the PR fingerprint is missing or different. Implementers and both review
phases receive that embedded instruction instead of running `gh issue view`.
GitHub Issue work items continue to use `feature/issue-N` and read Issue `N` with
`gh`. Both forms use the same recovery, Draft PR, review, and delivery functions.

Work-item order is significant and must be preserved. Here, `merge_final: false`
means the final PR is prepared but `main` is not automatically merged. It is
marked Ready after approval and stays Draft after a warning continuation. The
implementer and reviewer selections are independent. Omitting either agent field
selects `codex` for that role.

The configured tuple is an immutable seed for the generated Python's separate
`WorkItemPlan`, not a frozen execution schedule. Before each item is dispatched,
the workflow asks one dedicated planning-agent session for a bounded JSON
decision. The plain Python parser applies valid `add`, `update`, and `skip`
actions to pending work, or accepts completion only when no work remains. A key
is the GitHub Issue number or inline mini-task ID. Updates preserve the stable
key and branch, dispatched or completed items cannot be changed or reused, and
invalid or unbounded decisions fail closed without partially changing the plan.
Dynamic items still enter the same recovery, Draft PR, review, and delivery
flow. The effective final snapshot is passed explicitly to handoff generation;
the immutable configuration is not changed. This remains ordinary Python
control flow: planner prose, JSON input, Runner state, Progress, and the UI do not
become scheduling authorities.

Each `skip` action also supplies a concise reason. The workflow emits the
accepted decision as structured data, and Progress and the Issue Driven Summary
retain `SKIPPED` and its reason when viewing or reloading historical runs; stdout
is not used to infer it.

When Base PR creation is deferred because a newly created integration branch is
still identical to its final branch, an AWM-owned remote Git note anchored to the
reviewed final commit temporarily holds the same seed-bound serialized plan. It
is updated before every dispatch, so planner ordering and one-shot behavior stay
unchanged. After the first Issue merge advances integration, the Base PR is
created from that recovery state and becomes the normal authority again.

When `policy_issue` is present, implementation, Issue review/fix, and
whole-version review/fix agents read it first as shared design context. It is not
interpreted as workflow control or a DSL. A clear conflict with a listed
implementation work item is emitted as a structured warning and remains visible
for human handoff; execution continues with that work item taking priority.
The warning is persisted on its child PR (or on the Base PR for whole-version
findings) and restored during recovery, so whole-version review and Base PR
handoff retain it even when an already-merged Issue is skipped. The Base PR
references the policy Issue.

Reviewer approval and warning continuation remain distinct. When the final
whole-version review reaches the limit with requested changes, the run may
complete after exact clean/pushed PR topology checks, but the final PR stays
Draft for human handoff.

After that delivery state is fixed, the selected reviewer agent gets one prose-only
turn to create a concise Japanese overview, main-change summary, concrete human
checklist, and automated-verification summary. A validated managed section is
then added to the Base PR without changing Draft/Ready state or AWM correlation
metadata. Agent/validation failures and confirmed-safe update failures are
structured warnings; an unknown GitHub mutation outcome still fails closed.

Once work-item navigation is available, the run detail shows an Issue Driven
Summary with the repository and branches, each known work-item PR, available
review outcome data, whole-version review outcome, Base PR, policy Issue, and
the number of structured warning Findings. Each known child PR also links to
its implementation, Scope / Design Review, and Correctness Review tabs using
PurpleMux's canonical `/?workspace=<workspaceId>&tab=<tabId>` deep link. AWM
combines that path with the runtime's configured PurpleMux port and the browser's
current trusted hostname for Desktop or Mobile access. The generated workflow
publishes stable PurpleMux navigation through a dedicated event as soon as the
PR and tab identities are known, independently of the later result event. The
Runner retains both per run independently of the bounded Progress history, so
completed items stay linked while later items run and failed partial runs retain
their known links. The Summary is an observation surface and never controls
workflow execution. The New Run draft does not display a previous-run Summary.

Failed and stopped runs offer **Review & Resume**. A confirmation dialog shows
the original Issue Driven JSON before AWM starts a distinct run with that same
configuration and generated Python. Run history labels the new run with its
source run. The new workflow still recovers the Base PR, work-item plan, and
child PRs by inspecting their authoritative Git, GitHub, and PurpleMux state; it
does not reconstruct the terminated Python process.

## Rules for AI authors

- Use the existing source repository path, not a not-yet-created version worktree
  path.
- Preserve work-item order exactly as requested. Use `work_items` when any item
  is an inline mini task; use `issues` for compatibility with Issue-only input.
- Use `one_shot_issue` when the user supplies one large source Issue and wants the
  manager to create and revise the mini-task plan during the run.
- Give each mini task a stable lowercase kebab-case ID and a self-contained,
  short authoritative instruction. Do not create a GitHub Issue for it.
- Set `make_integration_branch` to true only when the integration branch should
  be created or validated as descending from the exact `final_branch` HEAD.
- Use `policy_issue` only for shared version design context, never for workflow
  ordering or conditions, and never repeat it in `issues`.
- Set `max_reviews` to 4 unless the user explicitly requests another value. This
  controls only Correctness and whole-version review.
- Omit `scope_max_reviews` to retain its default of 3, or set it to the recommended
  value of 6 unless the user explicitly requests another Scope / Design Review
  limit. Keeping the recommended Scope limit above `max_reviews` leaves capacity
  for rechecks after Correctness fixes.
- Use only `codex` or `claude` for either agent role. Omit an agent field to use
  its `codex` default.
- Set `merge_final` to false unless the user explicitly requests automatic final
  merging.
- Do not invent unsupported JSON fields.
- Use Python Workflow mode instead when arbitrary control flow is required.
