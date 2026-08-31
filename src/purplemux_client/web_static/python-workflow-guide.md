# Python Workflow Guide

Use this file as the contract when generating workflow scripts. The plain Python script is the source of truth. Keep orchestration in ordinary Python (`for`, `if`, `try`, `finally`) and use `purplemux_client` only as a thin adapter over PurpleMux public CLI/runtime semantics.

## Core rules

- Script owns sequencing, review/fix limits, Git constraints, success criteria, and cleanup policy.
- PurpleMux owns runtime/session state.
- Runner UI only executes/stops/observes; it is not a workflow engine.
- Never parse terminal screen text to decide completion or results.
- Never blindly retry mutations after unknown outcome.
- Use separate implementer/reviewer sessions when independent review is required.

## Public API

```python
from purplemux_client import (
    CreateSessionRequest,
    MutationOutcomeUnknown,
    PurpleMuxCLIClient,
    ResultNotReady,
    SessionReadyTimeout,
    TerminalSessionError,
    WorkerFailure,
    WorkerInterrupted,
    WorkerNeedsInput,
    emit_step,
)
```

Typical turn:

```python
client.wait_until_ready(session_id, 60)
client.send_input(session_id, prompt)
client.wait_for_turn_completion(session_id, 900)
result = client.read_result(session_id)
```

Workspace creation remains a direct public CLI call:

```python
subprocess.run([
    "purplemux", "workspace", "create",
    "--cwd", str(repo),
], check=True, capture_output=True, text=True)
```

## Progress

```python
emit_step("implementation", "started", workspace=workspace_id, tab=implementer)
emit_step("review", "completed", iteration=1, workspace=workspace_id, tab=reviewer)
emit_step("workflow", "failed", error="reason", workspace=workspace_id)
```

Statuses are only `started`, `completed`, and `failed`. Progress is observation only; do not drive workflow control from UI progress state.

## Recommended cleanup

```text
SUCCESS -> close created sessions
FAILURE -> keep workspace/sessions for inspection
```

## Recommended implement/review/fix pattern

```python
implementation = run_turn(implementer, implement_prompt)
for review_no in range(1, 5):
    review = run_turn(reviewer, review_prompt)
    if review_decision(review) == "APPROVED":
        break
    if review_no == 4:
        raise RuntimeError("review limit reached")
    run_turn(implementer, fix_prompt(review))
```

Keep the PR Draft during the loop, mark it Ready only after approval, and do not merge `main` unless explicitly requested.
