# Workflow and Runtime Notification Specification

## Architecture

```text
plain Python workflow
  -> purplemux_client
  -> PurpleMux

Runner
  -> execute / stop / observe
  -> terminal notification side effect

Agent Workflow Manager UI
  -> protected notification settings/test APIs
  -> public notify CLI

notify CLI
  -> notify-server / ntfy

browser on another trusted device
  -> http://<trusted hostname>:8765
  -> DNS / MagicDNS
  -> <explicit bound IPv4>:8765
  -> Agent Workflow Manager Runner UI
```

The plain Python workflow is the sole source of truth for workflow control
flow. It owns sequencing, branching, retries, success criteria, and cleanup.
Those decisions must not be copied into the Runner, encoded in progress
events, or replaced by a workflow framework, graph, DSL, or state machine.

The Runner owns only Python process execution and stop requests, stdout and
stderr capture, process-derived state, and optional progress observation. Its
terminal state is determined solely by the Python process:

- exit code zero becomes `success`;
- a nonzero exit becomes `failed`;
- a process terminated after a Runner stop request becomes `stopped`.

Progress events are observational. They do not decide terminal state and must
never drive workflow sequencing, branching, retries, cleanup, or completion.
The Runner does not operate tmux.

PurpleMux owns the agent runtime. `purplemux_client` uses PurpleMux's public
CLI contract and does not replace its runtime or access tmux directly.

## Browser access and network trust

The safe default bind is `127.0.0.1`. Remote browser access is enabled only by
setting `AGENT_WORKFLOW_MANAGER_HOST` in `config.sh` to one explicit IPv4
address assigned to a trusted interface, typically the machine's Tailscale
IPv4 or a trusted LAN address. Wildcard binding to `0.0.0.0` is rejected. This
integration does not use Tailscale Serve, Tailscale Funnel, or any reverse
proxy.

`bash start.sh` is the normal startup command. If the gitignored `config.sh`
does not exist, startup copies the committed, secret-free `sample_config.sh`,
runs interactive setup, persists the selected values with owner-only file
permissions, and continues startup. If it already exists, startup loads and
validates it without repeating first-run questions. First-run setup may call
`tailscale ip -4` to suggest one address, but this is optional detection only:
failure or absence defaults safely to a localhost offer, and a user can enter
another explicit trusted interface IPv4 manually. Startup never depends on
the Tailscale CLI. Declining an address that was successfully detected selects
localhost; manual entry is offered when detection fails and the localhost
offer is declined. Explicit environment values may override saved values for
one process without rewriting `config.sh`.

`AGENT_WORKFLOW_MANAGER_HOST` is exclusively the socket bind IPv4.
`AGENT_WORKFLOW_MANAGER_HOST_ALIASES` is an optional comma-separated set of
exact DNS names that extends browser Host and Origin trust without changing
the bind. A browser may therefore resolve a configured MagicDNS or LAN
hostname to the bound IPv4 while the Runner continues listening only on that
IPv4. The alias list does not use a reverse proxy and does not cause Serve or
Funnel configuration.

After a detected Tailscale IPv4 is accepted, first-run setup may query
Tailscale status for the machine's MagicDNS FQDN and offer to save it. This is
bounded optional convenience detection only; failure is nonfatal, and aliases may be
configured manually without Tailscale. Each alias is validated as one exact
ASCII DNS hostname, normalized to lowercase with an optional trailing root dot
removed, and matched only on the configured port. Schemes, ports, paths,
queries, user information, whitespace, control characters, IP addresses, and
wildcards are rejected. In particular, no `*.ts.net` suffix or all-MagicDNS
trust is supported.

The Runner derives allowed HTTP Hosts and browser Origins from the actual bind
address, exact configured hostname aliases, and port. GET requests require one
of those Hosts. Browser POST requests require an allowed Host, its matching
exact normalized `http://` Origin, and the current per-server request token.
Missing browser Origin keeps the existing trusted non-browser client behavior,
but unknown or unconfigured Host/Origin values remain forbidden. Direct IP
access continues to work; localhost is additionally trusted only for a
`127.0.0.1` bind.

Direct network access is only an outer trust boundary and does not replace
application validation. The Runner executes arbitrary trusted Python, is not a
sandbox, and provides no multi-user isolation. It must only bind to a trusted
private interface and must never be exposed through a public interface,
wildcard address, port forwarding, Funnel, or a public proxy.

## Terminal notification contract

After a Python process reaches one terminal state, the Runner may invoke the
public `notify send` CLI once as an observation side effect. Notifications do
not drive workflow control flow and do not participate in orchestration.

| State | Title | Message |
| --- | --- | --- |
| `success` | `Workflow completed` | Includes the run ID and `success` state. |
| `failed` | `Workflow failed` | Includes the run ID, `failed` state, and exit code when available. |
| `stopped` | `Workflow stopped` | Includes the run ID and `stopped` state; disabled by default. |

At runtime, `AGENT_WORKFLOW_MANAGER_NOTIFICATIONS=1` enables terminal
notifications. `AGENT_WORKFLOW_MANAGER_NOTIFY_SUCCESS`,
`AGENT_WORKFLOW_MANAGER_NOTIFY_FAILURE`, and
`AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED` select terminal states independently;
success and failure default on, and stopped defaults off. In `config.sh`, the
first setting is expressed as `auto`, `enabled`, or `disabled`; `start.sh`
resolves it to the runtime boolean after notify setup.

Each enabled terminal notification has one attempt, an outer bounded timeout,
and no retry loop. The CLI runs in its own process group, which is terminated
and reaped as a unit if that outer timeout expires. Missing or disabled
`notify`, timeout, process launch error, or nonzero CLI exit is recorded as a
generic diagnostic. Notification stderr is not copied into Runner output or
logs because an external command could include credentials there. Notification
failure never mutates Runner state, the Python exit code, or the workflow
result.

Runner shutdown owns pending notification cleanup: it closes the notifier and
joins terminal waiter threads. Any active notify CLI process group is
terminated and reaped before shutdown completes, so notification descendants
cannot outlive the trusted local Runner. Shutdown is terminal: once close
begins, the Runner atomically rejects new workflow starts.

The `notify` CLI is the integration boundary. It owns notify-server/ntfy HTTP,
authentication, delivery configuration, and its internal request timeout.
Agent Workflow Manager must not construct raw ntfy HTTP requests or duplicate
notify-server authentication behavior.

## Notification settings and test API

The UI exposes one compact Notifications settings area rather than a generic
settings framework. Its backend is limited to the configured repository
`config.sh` and external notify CLI config path; the browser cannot select a
file path or perform arbitrary file edits. Values are validated before being
written, files are replaced atomically where practical, and the secret-bearing
notify config is mode `0600`.

Notification policy changes are applied immediately to the same notifier used
for future terminal-state observations and persisted to `config.sh`. They do
not alter a running or completed workflow state. Host and port are not exposed
in this UI, so no setting requires unsafe network rebinding or a restart.

The settings read response contains server, topic, policy booleans, and only a
`configured`/`missing` credential status. It never contains the existing token,
its prefix, or its length. A replacement token is accepted only as a validated
write-only value and is persisted solely to the external notify config.
Inherited `NOTIFY_SERVER`, `NOTIFY_TOPIC`, and `NOTIFY_TOKEN` environment
overrides retain the public CLI's precedence and are included when calculating
effective values and credential status. A UI save updates the external config
and the active notifier's private child-process overrides together, so saved
values take effect immediately even when the Runner inherited older values.

Settings mutations and test-send requests require the configured exact Host,
matching browser Origin when present, and current per-server request token—the
same trusted-request boundary as Run and Stop. Test delivery calls public
`notify send` once with title `Agent Workflow Manager` and message
`Test notification`, under the same bounded process timeout and process-group
cleanup as terminal delivery. CLI absence, invalid/missing config or token,
timeout, and nonzero exit produce sanitized actionable messages; CLI output is
discarded and never returned. A test notification never reads or mutates
PythonRunner state.

## Credentials and configuration

Agent Workflow Manager runtime/startup configuration lives in the gitignored
`config.sh`, initially generated from committed `sample_config.sh`. Its
required values are the explicit bind host, trusted hostname aliases, port,
notification enabled mode,
success/failure/stopped policy, and `NOTIFY_CONFIG` path. It contains no
notification credentials.

Notify configuration lives outside Git at `~/.config/notify/config` by default
(or the path selected by `NOTIFY_CONFIG`):

```text
NOTIFY_SERVER=https://eletim.jp
NOTIFY_TOPIC=agents
NOTIFY_TOKEN=tk_...
```

The server and topic shown above are defaults. The token is a secret and must
never be hardcoded, copied into `config.sh`, committed, passed as a command
argument, or printed. Credential storage and notification delivery belong to
the notify CLI and Notify Server respectively.
