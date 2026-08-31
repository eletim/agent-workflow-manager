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

The normal setup and startup path is:

```bash
bash start.sh
```

Run it from the repository root. On the first run, the script copies the
committed, secret-free `sample_config.sh` to the gitignored `config.sh`, walks
through the short network setup, and saves the selected runtime settings.
Later starts load that file without repeating setup questions. The script then
validates the configuration, syncs locked Python dependencies, verifies `uv`
and `purplemux`, and starts the UI at the configured URL.

`config.sh` owns Agent Workflow Manager startup settings:

```bash
AGENT_WORKFLOW_MANAGER_HOST="127.0.0.1"
AGENT_WORKFLOW_MANAGER_PORT="8765"
AGENT_WORKFLOW_MANAGER_NOTIFICATIONS="auto"
NOTIFY_CONFIG="$HOME/.config/notify/config"
```

Edit `config.sh` to change a later startup. Do not put `NOTIFY_TOKEN` in it.
The file named by `NOTIFY_CONFIG` is owned by the public `notify` CLI and is
the sole persistent location for its server, topic, and credentials.

If the public `notify` CLI is missing, `start.sh` downloads the installer and
CLI source from
[`eletim/notify-server`](https://github.com/eletim/notify-server) into a
temporary directory and runs that repository's supported `install-cli.sh`.
No notify-server implementation is copied into this project.

### Trusted-network browser access

The default remains local-only at `http://127.0.0.1:8765`. During first-run
setup, `start.sh` optionally runs `tailscale ip -4` as a convenience. If it
finds one usable address, it offers to save that address in `config.sh`; if
Tailscale is absent or unavailable, setup safely offers localhost instead.
Tailscale is never required.

To use another trusted LAN or interface address, decline the localhost prompt
and enter its explicit IPv4 address, or later edit this value in `config.sh`:

```bash
AGENT_WORKFLOW_MANAGER_HOST="192.168.50.20"
```

`start.sh` prints the exact browser URL and rejects hostnames, invalid
addresses, and the wildcard `0.0.0.0`; it never invokes Tailscale Serve or
Funnel and introduces no reverse proxy. After configuration, normal startup is
always simply `bash start.sh`.

```text
browser on another trusted device
  -> http://<explicit trusted IPv4>:8765
  -> Agent Workflow Manager Runner UI
```

The configured address and port are the only accepted remote HTTP Host and
browser Origin. Unknown Hosts and Origins remain rejected, and every mutation
still requires the per-server request token. Network access is an outer trust
boundary, not a replacement for these checks.

This UI executes arbitrary trusted Python and is not a sandbox or multi-user
service. Bind only to an interface whose network and connected devices you
trust. Never use a public IP/interface, `0.0.0.0`, port forwarding, a public
reverse proxy, Tailscale Funnel, or any other public-internet exposure.

On an interactive terminal, a missing token can be entered without echo and
is saved outside Git in the file selected by `NOTIFY_CONFIG` (by default
`~/.config/notify/config`). Leaving it blank disables notifications without
blocking Runner startup. To skip notification setup, set this in `config.sh`:

```bash
AGENT_WORKFLOW_MANAGER_NOTIFICATIONS="disabled"
```

`NOTIFY_SERVER` and `NOTIFY_TOPIC` default to `https://eletim.jp` and `agents`.
Supply `NOTIFY_TOKEN` through the notify CLI config or environment; never
place it in `config.sh` or the repository. Set
`AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED=1` to opt in to stopped-run
notifications (success and failure are enabled after notify configuration
succeeds).

Open the URL printed under `Agent Workflow Manager:`. To choose another local
port, edit `AGENT_WORKFLOW_MANAGER_PORT` in `config.sh` and run `bash start.sh`
again.

The UI has trusted execution semantics: it is not a sandbox, provides no
multi-user isolation, and must not be exposed to the public internet. It binds
to `127.0.0.1` by default and only permits an explicitly selected IPv4 address
for remote use. Host, browser Origin, and per-server request-token checks stay
enabled in both modes.

Terminal notifications are a best-effort observation side effect through
`notify send`. The Python process alone determines success, failure, or stopped
state; notify failures cannot change it. The complete ownership and message
contract is in [the workflow/runtime specification](docs/workflow-runtime-spec.md).

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
