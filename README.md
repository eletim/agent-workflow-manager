# Agent Workflow Manager

Agent Workflow Manager is a lightweight runner/manager for AI-generated plain
Python/Bash workflows. It executes, observes, and stops scripts; it does not
define a workflow language or reproduce the state machine of another runtime.

This standalone project was initially migrated from
`apps/purplemux-client` in an older LangGraph fork. LangGraph itself is not a
runtime dependency and none of its workflow framework is included here.

## Principles

- A plain Python or Bash script is the Source of Truth.
- There is no workflow DSL.
- There are no graph semantics.
- There is no duplicated workflow state machine.
- External systems are used through public CLI or API contracts.
- PurpleMux is an agent runtime.
- This project executes, observes, and stops scripts.
- Notification support may be added later; it is not implemented today.
- Successful runtimes may be cleaned up.
- Failed runtimes should remain inspectable where appropriate.

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
