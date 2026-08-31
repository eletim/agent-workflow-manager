# Workflow and Runtime Notification Specification

## Architecture

```text
plain Python workflow
  -> purplemux_client
  -> PurpleMux

Runner
  -> execute / stop / observe
  -> terminal notification side effect

notify CLI
  -> notify-server / ntfy
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

## Terminal notification contract

After a Python process reaches one terminal state, the Runner may invoke the
public `notify send` CLI once as an observation side effect. Notifications do
not drive workflow control flow and do not participate in orchestration.

| State | Title | Message |
| --- | --- | --- |
| `success` | `Workflow completed` | Includes the run ID and `success` state. |
| `failed` | `Workflow failed` | Includes the run ID, `failed` state, and exit code when available. |
| `stopped` | `Workflow stopped` | Includes the run ID and `stopped` state; disabled by default. |

`AGENT_WORKFLOW_MANAGER_NOTIFICATIONS=1` enables terminal notifications.
`AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED=1` additionally enables stopped
notifications. `start.sh` sets the first value after setup; the second remains
an explicit operator choice.

Each enabled terminal notification has one attempt, an outer bounded timeout,
and no retry loop. The CLI runs in its own process group, which is terminated
and reaped as a unit if that outer timeout expires. Missing or disabled
`notify`, timeout, process launch error, or nonzero CLI exit is recorded as a
generic diagnostic. Notification stderr is not copied into Runner output or
logs because an external command could include credentials there. Notification
failure never mutates Runner state, the Python exit code, or the workflow
result.

The `notify` CLI is the integration boundary. It owns notify-server/ntfy HTTP,
authentication, delivery configuration, and its internal request timeout.
Agent Workflow Manager must not construct raw ntfy HTTP requests or duplicate
notify-server authentication behavior.

## Credentials and configuration

Notify configuration lives outside Git at `~/.config/notify/config` by default
(or the path selected by `NOTIFY_CONFIG`/`XDG_CONFIG_HOME`):

```text
NOTIFY_SERVER=https://eletim.jp
NOTIFY_TOPIC=agents
NOTIFY_TOKEN=tk_...
```

The server and topic shown above are defaults. The token is a secret and must
never be hardcoded, committed, passed as a command argument, or printed.
Credential storage and notification delivery belong to the notify CLI and
Notify Server respectively.
