# Review

Choose **Review** in the Runner and enter a JSON declaration:

```json
{
  "mode": "review",
  "repositories": ["~/DevEnv/project"],
  "check": "Inspect the repository and report findings with evidence.",
  "agent": "codex",
  "timeout": 3600
}
```

`mode`, `repositories`, and `check` are required. `repositories` is a non-empty
array of distinct, existing local Git repository **root** directories; paths
are expanded and resolved. `check` is a non-empty instruction describing what
to verify. Optional non-empty `start` and `finish` instructions run before and
after the check. `agent` is `codex` (the default) or `claude-code`. `timeout`
defaults to 3600 seconds and must be an integer from 1 to 86400.

Select **Validate JSON & Generate** to inspect the plain Python workflow.
Then use **Validate**, **Dry Run**, and **Run**. Review is an ordinary Run:
Progress, Stop, Result, and history work as for other Python workflows. The
original declaration and generated Python are saved in history. API clients
can POST `{"json":"..."}` to `/api/review/generate`, use its `generatedCode`
with the usual validation and dry-run endpoints, then POST
`{"code":generatedCode,"args":[],"reviewJson":originalJson}` to `/api/run`.
The submitted code must match the declaration's generated code.

## Read-only observation

The agent may inspect every declared repository and use any available browser
tool for read-only observation; Review requires no particular browser library.
Avoid browser actions that change application state. For a terminal outside
the Review workspace, use a known PurpleMux socket, session, and allowed window
target:

```bash
purplemux ext-review create --socket PATH --session SESSION --window @ID
```

Open the returned browser URL. This is for read-only
observation. Do not send input to an observed terminal or modify a declared
repository. Review requires a running PurpleMux 0.5.0 or newer server and a
matching CLI with public `ext-review` support; it checks this contract before
creating its workspace. On Linux, the workflow monitors writes to every declared
repository and its Git administrative directories, including restored writes
in monitored paths. It exempts Git index refresh operations because read-only
inspection can update cached metadata; an index change reverted before the
final fingerprint is not detected. The final fingerprint covers tracked,
ignored, and untracked files plus meaningful Git state, including staged
entries, refs, config, and Git objects in linked worktrees. It tolerates index
metadata refreshes caused by read-only Git inspection. A detected write or
changed fingerprint fails the Run
instead of producing a trustworthy Review verdict. Review fails closed if write
monitoring is unavailable.

## Verdict and Run status

The check returns one JSON object with `verdict` and a non-empty `summary`.
Optional `findings`, `observed_facts`, `evidence`, `hypotheses`, and
`observability_gaps` are arrays of strings. The result also lists the reviewed
repositories; oversized content may be shortened with counts in `truncated`.

- `PASS`: observed evidence supports the check passing.
- `FAIL`: observed evidence supports a problem; inspect `findings` and `evidence`.
- `BLOCKED`: observation timed out or was unavailable, so the check could not be
  established. Inspect `observability_gaps` and retry when the observation is
  available.

The verdict describes the check; the Run state and exit code describe whether
the workflow executed successfully. A completed Run may therefore have a
`FAIL` or `BLOCKED` verdict. Invalid agent output, an unavailable PurpleMux
contract, or a repository change can fail the Run without a structured verdict.
If the start observation times out or is unavailable, the Run completes with a
`BLOCKED` verdict and an observability gap; it skips the check and finish because
start completion cannot be confirmed. If the agent does not become ready, the
Run also completes with `BLOCKED` and skips all turns. If check completion cannot
be confirmed, finish is skipped and an observability gap records why. A stopped
Run may also have no complete result.
If an optional `finish` turn times out or is unavailable, the check verdict is
retained and the gap is added to `observability_gaps`.
