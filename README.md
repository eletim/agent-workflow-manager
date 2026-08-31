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
AGENT_WORKFLOW_MANAGER_HOST_ALIASES=""
AGENT_WORKFLOW_MANAGER_PORT="8765"
AGENT_WORKFLOW_MANAGER_NOTIFICATIONS="auto"
AGENT_WORKFLOW_MANAGER_NOTIFY_SUCCESS="true"
AGENT_WORKFLOW_MANAGER_NOTIFY_FAILURE="true"
AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED="false"
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
Tailscale is never required. Accepting a detected address selects it;
declining that detected address selects localhost, as prompted.

When no Tailscale address can be detected, declining the localhost prompt lets
you enter another trusted LAN or interface IPv4 address. You can also later
edit this value in `config.sh`:

```bash
AGENT_WORKFLOW_MANAGER_HOST="192.168.50.20"
```

`start.sh` prints the exact browser URL and rejects hostnames as bind values,
invalid addresses, and the wildcard `0.0.0.0`; it never invokes Tailscale Serve or
Funnel and introduces no reverse proxy. After configuration, normal startup is
always simply `bash start.sh`.

The bind address and browser hostname trust are separate settings. The Runner
still binds one explicit IPv4 from `AGENT_WORKFLOW_MANAGER_HOST`. Optional
comma-separated `AGENT_WORKFLOW_MANAGER_HOST_ALIASES` values only extend the
exact HTTP Host and Origin allowlist; they never affect the socket bind:

```bash
AGENT_WORKFLOW_MANAGER_HOST="100.x.y.z"
AGENT_WORKFLOW_MANAGER_HOST_ALIASES="e-ryzen.tail6bc726.ts.net"
```

When first-run setup detects and you accept a Tailscale IPv4, it also tries to
detect the local MagicDNS name and offers to persist it as an alias. Both
Tailscale checks are bounded optional conveniences: startup continues if either fails,
and an exact LAN or MagicDNS hostname can be entered directly in `config.sh`
without the Tailscale CLI. Aliases are normalized to lowercase DNS names and
must not contain a scheme, port, path, query, user information, wildcard,
whitespace, or control character. A trailing DNS root dot is removed. No
`*.ts.net` or other suffix is implicitly trusted.

Existing installations are migrated without manual editing. When
`bash start.sh` loads an existing `config.sh` with a non-loopback IPv4 bind and no
hostname aliases, it performs the same bounded MagicDNS detection and offers
to allow that exact hostname. Accepting atomically replaces or appends only
`AGENT_WORKFLOW_MANAGER_HOST_ALIASES`; all other configuration remains intact.
Declining or failed detection leaves the file unchanged and startup continues.
An already configured alias skips detection and the migration prompt.

For compatibility and one-off launches, explicit environment values override
the corresponding saved values for that process without rewriting
`config.sh`; for example,
`AGENT_WORKFLOW_MANAGER_HOST=100.x.y.z bash start.sh`. Persistent changes
belong in `config.sh`.

```text
browser on another trusted device
  -> http://<explicit trusted IPv4 or configured hostname>:8765
  -> DNS / MagicDNS (for a hostname)
  -> <explicit bound IPv4>:8765
  -> Agent Workflow Manager Runner UI
```

The configured address, exact aliases, and port are the only accepted remote
HTTP Host and browser Origin combinations. `start.sh` prints the direct IP URL
and every configured alias URL. Unknown Hosts and Origins remain rejected, and
every mutation still requires the per-server request token. Network access is
an outer trust boundary, not a replacement for these checks.

This UI executes arbitrary trusted Python and is not a sandbox or multi-user
service. Bind only to an interface whose network and connected devices you
trust. Never use a public IP/interface, `0.0.0.0`, port forwarding, a public
reverse proxy, Tailscale Funnel, or any other public-internet exposure.

### Notification settings

Open **Settings → Notifications** in the Runner UI for day-to-day notification
configuration. The compact panel can enable notifications, select success,
failure, and stopped terminal states, edit the notify server and topic, report
credentials as only `Configured` or `Missing`, replace the token through a
write-only password field, and send a real test notification.

Policy is split deliberately between two files:

- Gitignored repository `config.sh` owns whether notifications are enabled and
  the success/failure/stopped policy.
- The external file selected by `NOTIFY_CONFIG` owns `NOTIFY_SERVER`,
  `NOTIFY_TOPIC`, and `NOTIFY_TOKEN` for the public `notify` CLI.

Saving the notification policy updates the active notifier immediately and
persists it atomically; no workflow or Runner restart is required. Server,
topic, and an optional replacement token are atomically written to the notify
CLI config with owner-only permissions. The existing token is never returned,
displayed, logged, or copied into `config.sh`.

Explicit `NOTIFY_SERVER`, `NOTIFY_TOPIC`, or `NOTIFY_TOKEN` values inherited by
the Runner process keep the public notify CLI's normal precedence and are
reflected in the displayed effective settings. After a UI save, the validated
saved server/topic and any replacement token take precedence immediately for
this Runner process as well as being persisted to the notify CLI config.

The test button invokes only `notify send --title "Agent Workflow Manager"
--message "Test notification"`. It uses a bounded timeout and no retries, and
returns only a sanitized actionable result. It never reads or changes the
Python Runner state. Both settings mutation and test-send use the same exact
Host, matching Origin, and per-server request-token checks as Run and Stop.

On an interactive terminal, a missing token can be entered without echo and
is saved outside Git in the file selected by `NOTIFY_CONFIG` (by default
`~/.config/notify/config`). Leaving it blank disables notifications without
blocking Runner startup. To skip notification setup, set this in `config.sh`:

```bash
AGENT_WORKFLOW_MANAGER_NOTIFICATIONS="disabled"
```

`NOTIFY_SERVER` and `NOTIFY_TOPIC` default to `https://eletim.jp` and `agents`.
Supply `NOTIFY_TOKEN` through the notify CLI config or environment; never
place it in `config.sh` or the repository. Success and failure notifications
default on, while stopped notifications default off; the Settings panel or
the corresponding `config.sh` values control each policy.

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
