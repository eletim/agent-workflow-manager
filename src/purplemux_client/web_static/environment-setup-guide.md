# Environment Setup

Enter a JSON declaration with these required fields:

```json
{
  "mode": "environment-setup",
  "repository": "/path/to/existing/local/git/repository",
  "revision": "origin/main",
  "environment_agent": "codex",
  "timeout": 3600
}
```

`revision` may be an existing origin branch, tag, or full local commit SHA.
`environment_agent` is `codex` or `claude-code`; `timeout` is 1–86400 seconds.
Optional non-empty command strings are `build`, `start`, and `ready_check`.
Commands run in that order in managed PurpleMux terminals. The agent supplies
a usability check if `ready_check` is omitted.

Choose **Validate JSON & Generate** to inspect the plain Python workflow.
Then use **Validate**, **Dry Run**, and **Run**. This is an ordinary Run:
Progress, Result, Stop, and history behave as for other Python workflows.
The original JSON and generated Python are retained in Run history.

The workflow prints one JSON result to stdout. `status` is `READY` or
`BLOCKED`; `summary`, `resolved_revision`, and `working_path` identify the
outcome and prepared worktree. `connection` records the workspace and agent
tab, and may include an observed endpoint. `process`, `checks`,
`verification`, `attempts`, and execution and readiness summaries contain
observed evidence. `BLOCKED` includes `observed_facts`. A stopped Run may
have no complete result. Use Run state and exit code for workflow execution
and the JSON `status` for environment readiness.
