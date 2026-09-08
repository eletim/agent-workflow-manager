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
rejects unsafe remote movement or divergence. The final PR remains
`integration_branch` to `final_branch`.

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

Unknown fields are rejected. Provide exactly one of `work_items` or the legacy
`issues` field. `mode`, `make_integration_branch`, `policy_issue`,
`implementer_agent`, and `reviewer_agent` are otherwise optional; every other
field is required.

| Field | Type | Meaning |
| --- | --- | --- |
| `mode` | string | Optional discriminator; when present it must be `issue-driven`. |
| `repository` | string | Existing source repository path. |
| `integration_branch` | string | Remote/integration branch used as the development base; it may be created when `make_integration_branch` is true. |
| `final_branch` | string | Branch targeted by final delivery; it must differ from `integration_branch`. |
| `make_integration_branch` | boolean | Create/recover `integration_branch` from the exact remote `final_branch` HEAD; default `false`. |
| `policy_issue` | integer | Optional positive Issue number containing version-wide design context; it must not also appear as an implementation GitHub Issue item. |
| `issues` | array of integers | Legacy form for positive, unique GitHub Issue numbers, executed in the listed order. Do not combine it with `work_items`. |
| `work_items` | array | Ordered GitHub Issue numbers and/or inline mini-task objects. A mini task is exactly `{"id": "lowercase-kebab-id", "task": "authoritative instruction"}` and does not require a GitHub Issue. |
| `max_reviews` | integer | Correctness and whole-version review limit from 1 through 100; reaching it continues with a structured warning after exact topology checks. Use 5 unless the user requests another value. |
| `implementer_agent` | string | Agent used for implementation, fixes, and cleanup; `codex` or `claude`, default `codex`. |
| `reviewer_agent` | string | Agent used for Issue and whole-version review; `codex` or `claude`, default `codex`. |
| `merge_to_integration` | boolean | Whether each safely deliverable Issue PR is merged into the integration branch, including explicit warning continuations. |
| `final_review` | boolean | Whether the completed integration branch receives a final review. |
| `merge_final` | boolean | Whether final delivery is automatically merged into `final_branch`. |

Do not add generic `if`, `while`, action, step, or arbitrary executable blocks.

Each implementation work item is reviewed in two ordered phases. A fixed internal
limit of three Scope / Design reviews checks that the change is necessary,
sufficient, minimal, and placed within the right responsibilities. After that
phase, Correctness Review uses `max_reviews` to check behavior, edge cases,
safety, regressions, and tests. The counters and outcomes are independent. A
phase that exhausts its limit may continue with an explicit warning after exact
clean, pushed PR topology is revalidated; it is never recorded as approved.
Whole-version Review remains a separate integration and cross-Issue review.

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
  "max_reviews": 5,
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
  "max_reviews": 5,
  "merge_to_integration": true,
  "final_review": true,
  "merge_final": false
}
```

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

After a run reaches a terminal state, its detail view shows an Issue Driven
Summary with the repository and branches, each work-item PR and exact completed
review-turn count, whole-version review outcome, Base PR, policy Issue, and the
number of structured warning Findings. The generated workflow publishes these
facts through dedicated result events as they become final. The Runner retains
them per run independently of the bounded Progress history; the Summary is an
observation surface and never controls workflow execution. Running workflows
and the New Run draft do not display a premature or previous-run Summary.

## Rules for AI authors

- Use the existing source repository path, not a not-yet-created version worktree
  path.
- Preserve work-item order exactly as requested. Use `work_items` when any item
  is an inline mini task; use `issues` for compatibility with Issue-only input.
- Give each mini task a stable lowercase kebab-case ID and a self-contained,
  short authoritative instruction. Do not create a GitHub Issue for it.
- Set `make_integration_branch` to true only when the integration branch should
  be created or validated as descending from the exact `final_branch` HEAD.
- Use `policy_issue` only for shared version design context, never for workflow
  ordering or conditions, and never repeat it in `issues`.
- Set `max_reviews` to 5 unless the user explicitly requests another value.
- Use only `codex` or `claude` for either agent role. Omit an agent field to use
  its `codex` default.
- Set `merge_final` to false unless the user explicitly requests automatic final
  merging.
- Do not invent unsupported JSON fields.
- Use Python Workflow mode instead when arbitrary control flow is required.
