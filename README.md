# Agent Workflow Manager

Agent Workflow Manager contains a thin PurpleMux CLI adapter and a trusted
local UI for running arbitrary Python. It does not define a workflow language
or reproduce the state machine of another runtime.

This standalone project was initially migrated from
`apps/purplemux-client` in an older LangGraph fork. LangGraph itself is not a
runtime dependency and none of its workflow framework is included here.

## Principles

- Python code executed by the local runner remains plain Python.
- There is no workflow DSL.
- There are no graph semantics.
- There is no duplicated workflow state machine.
- External systems are used through public CLI or API contracts.
- PurpleMux is an agent runtime.
- The local runner executes, observes, and stops Python processes.

## PurpleMux adapter

`purplemux_client.PurpleMuxCLIClient` is a thin adapter over the public
`purplemux` CLI. `CreateSessionRequest` describes the provider session to
create. The adapter supports:

- `create_session()`
- `read_status()`
- `wait_until_ready()`
- `send_input()`
- `wait_for_turn_completion()`
- `read_result()`
- `interrupt()`
- `close_session()`
- `capture_screen()` for diagnostics only

Turn completion is correlated with `eventSeq`, `readyForReviewAt`, and
`completionTimestamp`. Stale results are rejected, including when the
ready-for-review UI is dismissed back to idle. Needs-input, dead/error states,
and interruptions are explicit. Read-only CLI timeouts can be retried;
mutation timeouts raise `MutationOutcomeUnknown` because the remote outcome is
unknown. Screen capture is never used to decide completion or as a result
fallback.

```python
from purplemux_client import CreateSessionRequest, PurpleMuxCLIClient

client = PurpleMuxCLIClient("ws-example")
session_id = client.create_session(
    CreateSessionRequest(worker="codex", cwd="/workspace/project", command="codex")
)

try:
    client.wait_until_ready(session_id, 60)
    client.send_input(session_id, "Return a concise answer.")
    client.wait_for_turn_completion(session_id, 900)
    print(client.read_result(session_id))
finally:
    client.close_session(session_id)
```

PurpleMux owns provider launch commands and the workspace directory. The
adapter never calls tmux, private PurpleMux APIs, or PurpleMux internal files.

## Local Python Runner UI

The trusted local Runner UI executes arbitrary Python with the current Python
interpreter and shows stdout, stderr, exit code, and the
idle/running/success/failed/stopped state. Run and Stop operate on one script at
a time. Stop and server shutdown clean up the script's POSIX process group,
including child processes.

Workflow scripts can explicitly report lightweight progress without changing
their execution logic. Calls made outside the Runner are no-ops. Events from
the latest execution remain visible after its process exits and are cleared by
the next Run.

```python
from purplemux_client import emit_step

emit_step("implementation", "started", workspace="ws-example", tab="tab-1")
# Run the workflow's ordinary Python logic.
emit_step("implementation", "completed")
emit_step("review", "started", iteration=1, message="Checking the result")
emit_step("review", "failed", iteration=1, error="Tests failed")
```

`emit_step` accepts only `started`, `completed`, and `failed`. In addition to
the step name and status, it accepts optional `iteration`, `attempt`, `message`,
`error`, `workspace`, and `tab` values. Progress is observational only: the
Runner does not infer workflow transitions or control the script from events.
To keep observation bounded, the Runner retains the latest 200 events and each
encoded event is limited to 4 KiB. Older events and oversized events are
discarded without affecting workflow execution.

```bash
make web
```

Then open <http://127.0.0.1:8765>. To choose another local port:

```bash
make web ARGS="--host 127.0.0.1 --port 9000"
```

The UI has local trusted execution semantics: it is not a sandbox, provides no
multi-user isolation, and must not be exposed to the public internet. It binds
to `127.0.0.1` by default and protects mutations with Host, browser Origin, and
per-server request-token checks.

## Development

Python 3.10 or later and `uv` are required.

```bash
make format
make lint
make typecheck
make test
```

The live lifecycle smoke uses the migrated adapter and only the public
PurpleMux CLI. It creates a Codex session, waits until ready, sends one prompt,
waits for a fresh structured result, verifies it, and closes the session:

```bash
make live-smoke ARGS="lifecycle --workspace ws-example --cwd /workspace/project"
```
