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
is the remote/integration branch to develop on. The generated Python calls
`prepare_run_repository(repo=repository, base_branch=integration_branch)` to
create a fresh, run-specific worktree from that branch. The user does not need to
create a version worktree first.

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

Unknown fields are rejected. `mode` is optional; every other field is required.

| Field | Type | Meaning |
| --- | --- | --- |
| `mode` | string | Optional discriminator; when present it must be `issue-driven`. |
| `repository` | string | Existing source repository path. |
| `integration_branch` | string | Existing remote/integration branch used as the development base. |
| `final_branch` | string | Branch targeted by final delivery; it must differ from `integration_branch`. |
| `issues` | array of integers | Positive, unique Issue numbers, executed in the listed order. |
| `max_reviews` | integer | Review limit from 1 through 100; use 5 unless the user requests another value. |
| `merge_to_integration` | boolean | Whether each approved Issue PR is merged into the integration branch. |
| `final_review` | boolean | Whether the completed integration branch receives a final review. |
| `merge_final` | boolean | Whether final delivery is automatically merged into `final_branch`. |

Do not add generic `if`, `while`, action, step, or arbitrary executable blocks.

## Canonical example

```json
{
  "mode": "issue-driven",
  "repository": "~/DevEnv/agent-workflow-manager",
  "integration_branch": "dev/v0.2.1",
  "final_branch": "main",
  "issues": [86, 99, 87, 84],
  "max_reviews": 5,
  "merge_to_integration": true,
  "final_review": true,
  "merge_final": false
}
```

Issue order is significant and must be preserved. Here, `merge_final: false`
means the final PR is prepared and marked Ready, but `main` is not automatically
merged.

## Rules for AI authors

- Use the existing source repository path, not a not-yet-created version worktree
  path.
- Preserve Issue order exactly as requested.
- Set `max_reviews` to 5 unless the user explicitly requests another value.
- Set `merge_final` to false unless the user explicitly requests automatic final
  merging.
- Do not invent unsupported JSON fields.
- Use Python Workflow mode instead when arbitrary control flow is required.
