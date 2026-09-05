## Personal self-hosted setup

```bash
# Install and start the custom PurpleMux fork (keep this terminal running).
git clone https://github.com/eletim/purplemux.git "$HOME/DevEnv/purplemux"
cd "$HOME/DevEnv/purplemux"
corepack enable
pnpm install
pnpm start
```

```bash
# In another terminal, expose the fork's custom CLI and start this project.
mkdir -p "$HOME/.local/bin"
printf '%s\n' '#!/usr/bin/env bash' \
  'exec node "$HOME/DevEnv/purplemux/bin/cli.js" "$@"' \
  >"$HOME/.local/bin/purplemux"
chmod 0755 "$HOME/.local/bin/purplemux"
export PATH="$HOME/.local/bin:$PATH"

git clone https://github.com/eletim/agent-workflow-manager.git \
  "$HOME/DevEnv/agent-workflow-manager"
cd "$HOME/DevEnv/agent-workflow-manager"
bash start.sh
```

This setup intentionally uses the custom CLI from
[`eletim/purplemux`](https://github.com/eletim/purplemux). Do **not** substitute
the upstream `npm install -g purplemux` package: it does not provide the CLI
contract required by Agent Workflow Manager. No token needs to be copied into
these commands; the custom CLI reads the runtime connection files created under
`~/.purplemux/`.

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

## Issue Driven mode

The UI offers `Prompt | Issue Driven | Python Workflow`. Issue Driven mode accepts
only a small JSON configuration, validates it separately from Python, and
deterministically expands it into the canonical sequential plain-Python workflow.
The generated Python is visible for inspection and is then passed unchanged to the
existing Static Validation, Dry Run, and Run path. JSON is configuration, not an
executable DSL, and there is no Issue Driven runtime or UI-side control-flow model.

```json
{
  "mode": "issue-driven",
  "repository": "~/DevEnv/project",
  "integration_branch": "dev/v0.2.0",
  "final_branch": "main",
  "issues": [90, 89],
  "max_reviews": 5,
  "merge_to_integration": true,
  "final_review": true,
  "merge_final": false
}
```

All fields except the optional, fixed `mode` discriminator are required. Issue
numbers are positive, unique, and retain their array order. Unknown fields are
rejected so generic actions, conditions, loops, and nested executable blocks cannot
grow into a second workflow language. With `merge_final: false`, the generated
final control flow makes the integration PR Ready but contains no final merge call.

## Git and GitHub topology operations

Plain Python workflows can enforce repository and pull-request structure through
`GitRepository` and `GitHubRepository`. These validated handles recheck repository
identity on every public operation, keep read-named methods mutation-free, and only
permit branch creation/tracking/switching and fast-forward Git changes. They never
reset, rebase, force-push, delete branches, stash changes, or resolve conflicts.

```python
from purplemux_client import (
    GitHubRepository,
    GitRepository,
    prepare_run_repository,
)

context = prepare_run_repository(
    repo="~/DevEnv/project",
    base_branch="dev/v1.2.3",
)

repo = GitRepository.open(
    context.execution_root,
    expected_github_slug="owner/project",
)
github = GitHubRepository.open("owner/project")

feature = repo.prepare_feature_branch(
    "feature/issue-123",
    base="dev/v1.2.3",
    expected_base_sha=context.base_sha,
)

# Capture the pre-turn SHA before invoking the CodingAgent. Its hard
# postcondition is a new commit on the expected branch and a clean worktree.
turn_start_sha = feature.local_sha
# ... run the CodingAgent ...
feature = repo.require_committed_result(
    "feature/issue-123",
    previous_sha=turn_start_sha,
)
# Push is orchestration-owned gap absorption. This only creates the exact
# remote branch or fast-forwards it; remote-ahead/diverged states fail closed.
feature = repo.ensure_pushed(
    "feature/issue-123",
    expected_local_sha=feature.local_sha,
)
pull_request = github.require_pr(
    head="feature/issue-123",
    base="dev/v1.2.3",
    expected_head_sha=feature.remote_sha,
)
```

When a workflow does not already know the repository slug, omitting
`expected_github_slug` derives and pins it from the validated GitHub origin. The
origin is still rechecked on every topology operation.

The Workflow similarly creates or reuses the exact Draft PR when the agent did
not create one, then verifies its head, base, SHAs, and Draft state before
review. A dirty agent result cannot advance. A review-fix turn normally has the
same new-commit/clean contract; if the implementer explicitly re-evaluates a
finding and returns clean without a commit, the Workflow records a WARN policy
outcome rather than pretending that the reviewer approved it.

PR discovery exhausts a bounded sequence of authoritative GitHub API pages. An
open PR for the requested head but a different base, multiple exact candidates,
or an unproven final page fails closed. Ready/Draft changes require the exact
reviewed head and base SHAs. `merge_pr()` uses only GitHub's immediate merge
endpoint with merge-commit mode; it never invokes `gh pr merge`, enables
auto-merge, or enters a merge queue, and it verifies the merge commit's parents
and resulting base branch.

Each mutation is dispatched once. A timeout, lost response, malformed response,
or ambiguous nonzero result triggers read-only, operation-specific reconciliation.
If later completion cannot be excluded, the operation raises
`MutationOutcomeUnknown` even when an immediate read still matches the pre-state.
This operation layer supplies safety primitives only: Issue order, review loops,
approval provenance, and release policy remain ordinary Python control flow.

## PurpleMux adapter

`purplemux_client.PurpleMuxCLIClient` is a thin adapter over the public
`purplemux` CLI. `CreateSessionRequest` describes the provider session to
create. The adapter supports:

- `create_session()`
- `list_sessions()` (complete structured tab discovery)
- `read_status()`
- `wait_until_ready()`
- `send_input()`
- `wait_for_turn_completion()`
- `read_result()`
- `interrupt()`
- `close_session()`
- `capture_screen()` for diagnostics only
- `start_shell()`
- `wait_for_shell_completion()`
- `read_shell_result()`

Turn completion is correlated with `eventSeq`, `readyForReviewAt`, and
`completionTimestamp`. Stale results are rejected, including when the
ready-for-review UI is dismissed back to idle. Needs-input, dead/error states,
and interruptions are explicit. Read-only CLI timeouts can be retried;
mutation timeouts raise `MutationOutcomeUnknown` because the remote outcome is
unknown. Screen capture is never used to decide completion or as a result
fallback.

`PurpleMuxRuntime` adds authoritative workspace listing and correlated workspace
creation. Workspace and tab create responses are accepted only after their IDs and
structured identities are confirmed by complete public listings. All first-party
runtime mutations participate in the same first-mutation boundary used by whole-
program Dry Run.

Named workspace, agent-session, and managed-shell creation derives a valid
correlation identity from the Runner's Run and the supplied logical `name`.
Repeated use of that name within one Run is stable, while another Run gets a
different identity even when an older resource is retained. Direct Python use
outside the Runner falls back to one process-stable random namespace. The public
`run_correlation(name)` helper is available for APIs such as correlated PR
creation that still need an explicit value. Correlations identify creation and
reconciliation; Cleanup ownership continues to use returned concrete workspace,
tab, and filesystem identities.

Static Validation reports Dry Run eligibility separately. Eligible trusted
workflows declare `WORKFLOW_DRY_RUN = 1`; Dry Run executes that same Python program
through real inspections and stops before the first reachable mutation. The Runner
shows reached runtime/Git/GitHub findings and that next mutation without fabricating
future state or interpreting workflow control flow.

The Runner also offers a separate, explicit **Agent readiness** action. It uses
only an operator-selected existing PurpleMux workspace: AWM records the complete
pre-create tab set, creates one uniquely named provider tab, correlates it through
structured public listings, waits for structured readiness, and attempts to close
that exact tab once. Readiness and cleanup are reported independently, with the
retained tab identity and recovery guidance when cleanup cannot be confirmed. This
mutating probe never runs as part of Static Validation or Dry Run.
Unresolved identities are persisted across Runner restarts and block new probes.
File-backed ownership serializes probe and reconciliation decisions across Runner
processes sharing that recovery record.
The explicit reconciliation action clears that block only after structured public
inspection proves the correlated tab (or its original workspace) is absent; it
never retries the close mutation.

Observable Bash work runs in named PurpleMux `terminal` tabs. The adapter sends
the command with an explicit working directory and correlates completion with a
machine-readable exit-code sidecar; it does not parse pane text or guess from a
shell prompt. Starting commands is non-blocking, so shell tabs, agent sessions,
and separate workflows can run concurrently. A completed tab stays open until
the workflow explicitly closes it, which lets failed commands remain available
for inspection.

```python
from purplemux_client import CreateSessionRequest, PurpleMuxRuntime

client = PurpleMuxRuntime().workspace("ws-example")
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

```python
from purplemux_client import ShellCommandRequest

shell_tab = client.start_shell(
    ShellCommandRequest(
        command="uv run pytest",
        cwd="/workspace/project",
        name="Run 12: tests",
    )
)
client.wait_for_shell_completion(shell_tab, 900)
shell_result = client.read_shell_result(shell_tab)
if shell_result.exit_code != 0:
    raise RuntimeError(f"tests failed in {shell_tab}: {shell_result.exit_code}")
client.close_session(shell_tab)
```

PurpleMux owns provider launch commands and the workspace directory. The
adapter never calls tmux, private PurpleMux APIs, or PurpleMux internal files.
Before creating a Codex tab, the adapter canonicalizes the requested working
directory, re-reads the selected workspace, and requires it to match the
workspace's current first directory—the directory PurpleMux actually launches.
It marks only that path trusted through Codex's supported app-server
`config/batchWrite` API. It reads the effective Codex configuration back before
launch and fails immediately if trust cannot be confirmed. This also covers
fresh run worktrees, whose new paths do not inherit trust from their source
repositories. The operation does not change Codex sandbox or approval settings
and does not trust a parent, secondary, or unrelated path.

Claude Code has no equivalent narrow path-trust mutation in the integration
used here. AWM does not use a broad permission bypass or terminal keystroke
automation for Claude; its provider-specific trust behavior remains owned by
Claude Code and PurpleMux.

## Local Python Runner UI

The trusted local Runner UI has two explicit modes. **Prompt** accepts an agent,
an existing working directory, and one prompt. It generates a single-step plain
Python execution that creates a PurpleMux workspace rooted at that exact directory,
creates the selected provider tab, and observes its structured turn result. Prompt
resources stay directly available in PurpleMux; they are not registered as
Workflow-owned resources and have no automatic or explicit Workflow cleanup path.
The generated Python remains an implementation detail rather than an editable or
historical UI field.

**Workflow** executes arbitrary Python with the current Python interpreter in a
visible PurpleMux-managed Bash tab. PurpleMux terminal output is the detailed
stdout/stderr inspection surface; AWM shows structured Progress, Findings,
bounded failure diagnostics, the managed-shell exit code, and the
idle/running/success/failed/stopped/validation_failed state. Validate remains a
side-effect-free static check and Dry Run remains a pre-execution local
inspection. Run performs preflight before creating the PurpleMux workspace and
tab. Stop uses the public PurpleMux interrupt/result lifecycle, closing the tab
only if needed to reach a deterministic stopped state. If neither structured
completion nor tab closure can be confirmed, AWM reports the uncertainty and
keeps the run non-terminal so events remain accepted and Cleanup stays disabled.

Failed and stopped runs remain available for inspection, including their output
and run-owned resources, but are never continued in place. Recovery starts a new
run. Its ordinary Python logic must explicitly inspect and reuse authoritative
Git, GitHub, or PurpleMux state where appropriate. The Runner does not expose a
workflow checkpoint API or reconstruct terminated Python control flow.

Runs are independent and may execute concurrently. The UI lists every run and
lets the operator select its state, output, progress, execution context, Stop,
and explicit Cleanup action without changing another run. Workflow-owned
resources remain inspectable after every terminal result and are registered on
the existing run record rather than a separate lifecycle store. Canonical
Workflow runtimes opt into ownership registration; direct/Prompt adapter use is
registration-free by default. `GET /api/runs` lists compact summaries,
`GET /api/runs/{runId}` reads one snapshot, and
`POST /api/runs/{runId}/stop` stops only that run.
`POST /api/runs/{runId}/cleanup` releases registered resources without deleting
run history. Workspace release requires
PurpleMux's public atomic `workspace delete -w ID --if-empty` CLI contract;
startup rejects unsupported versions so canonical Cleanup cannot be stranded
behind an incompatible runtime. The original
`/api/status`, `/api/output`, and `/api/stop` routes remain available and address
the most recently created run. `GET /api/events` streams revision-only SSE change
notifications; initial load, notifications, and reconnects all reconcile through
the authoritative read APIs rather than treating the stream as workflow state.

Workflow processes use one stable Runner-controlled directory; it is not a
project selector and is not a Workflow form input. Repository-modifying
workflows declare their source and base in Python. `prepare_run_repository()`
validates the remote base, resolves its exact commit, creates a fresh detached
worktree under `~/.local/share/agent-workflow-manager/worktrees/`, registers it
with the current run, and returns its structured execution identity:

```python
from purplemux_client import prepare_run_repository

context = prepare_run_repository(
    repo="~/DevEnv/project",
    base_branch="main",
)
```

Use `context.execution_root` explicitly for Git/GitHub operations, PurpleMux
workspace creation, shells, and agents. Run details expose the source
repository, configured remote/base ref, exact base SHA, and execution root.
The HTTP Workflow form is `{"code": "...", "args": ["value"]}`; `cwd` is
rejected. Lower-level PurpleMux workspace/session `cwd` parameters remain
available for direct execution and Prompt mode.

Preflight always checks syntax. It also checks direct module-level imports,
direct module-level `os.environ["NAME"]` access, and names imported directly from
the public `purplemux_client` API. Guarded, conditional, and deferred uses are
not assumed to be mandatory. Workflows can declare requirements that are known
before agent work with a literal module-level value:

```python
WORKFLOW_PREFLIGHT = {
    "commands": ["git", "gh", "uv"],
    "imports": ["project_package"],
    "environment": ["GH_TOKEN"],
    "paths": ["pyproject.toml", "/absolute/input/data.json"],
}
```

All four keys are optional lists of non-empty strings. Commands are located on
the workflow child's `PATH`, imports use the Runner interpreter, environment
names must be present, and relative paths resolve from the selected run working
directory. The declaration is parsed as data; neither it nor any other workflow
code is executed during validation. Read-only discovery is bounded; a stalled
lookup is reported as a validation timeout without blocking Runner status or
shutdown. Preflight is deliberately best-effort: dynamic Python behavior and
external state can still fail after execution starts.

The normal setup and startup path is:

```bash
bash start.sh
```

Run it from the repository root. On the first run, the script copies the
committed, secret-free `sample_config.sh` to the gitignored `config.sh`, walks
through the short network setup, and saves the selected runtime settings.
Later starts load that file without repeating setup questions. The script then
validates the configuration, syncs locked Python dependencies, verifies `uv`
and the custom `eletim/purplemux` command contract, confirms the PurpleMux
runtime responds to a bounded read-only workspace query, and starts the UI at
the configured URL. An incompatible upstream npm CLI or an unreachable runtime
fails startup with installation and connection guidance before the UI launches.

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
to allow that exact hostname. Accepting atomically appends one effective
`AGENT_WORKFLOW_MANAGER_HOST_ALIASES` assignment; all existing configuration
text remains intact. Runtime config writers share a sidecar lock and the
migration refuses a stale replacement rather than losing a concurrent update.
Declining or failed detection leaves the file unchanged and startup continues.
An already configured alias or explicit environment override skips detection
and the migration prompt.

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

Local, LAN, and private-VPN HTTP is a first-class Runner deployment mode, not a
degraded compatibility mode. Core actions—guide and output copy, validation,
Run, Stop, Continue after fix, settings, progress, and read APIs—must remain
usable at the printed `http://` URL. Browser features may use secure-context
APIs when available, but must retain an HTTP-compatible path. Clipboard actions
therefore try the Clipboard API first, then copy from a controlled textarea;
if neither programmatic path succeeds, the UI presents the exact intended text
selected for manual copying. Same-origin EventSource/SSE observation and
favicon/status presentation do not require HTTPS and must remain compatible
with this deployment model.

This support does not weaken transport-independent application protections.
Exact Host and matching Origin checks and the per-server request token apply as
described above on HTTP. The supported trust boundary is a private local,
LAN, or VPN network, never public-Internet exposure.

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
Browser notification permission and Web Push are intentionally outside Agent
Workflow Manager core. Notification delivery belongs to the external `notify`
CLI/service boundary, so the Runner itself remains fully usable on local/VPN
HTTP without a service worker, Push API, or secure browser context.

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

The opt-in Codex trust integration test creates a fresh linked worktree and
PurpleMux workspace, proves the first real Codex turn completes without an
interactive trust prompt, then repeats the launch for the already-trusted path:

```bash
AGENT_WORKFLOW_MANAGER_RUN_LIVE_CODEX_TRUST=1 \
  uv run pytest tests/test_live_codex_trust.py
```
