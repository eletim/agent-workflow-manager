# Review Fix

Choose **Review Fix** under **Developer & detail views** and enter a JSON
declaration:

```json
{
  "mode": "review-fix",
  "repository": "/path/to/repo",
  "start": {
    "command": "./start-dev.sh",
    "ready_check": "http://127.0.0.1:3000/api/health"
  },
  "check": "Use the browser to verify the feature.",
  "max_iterations": 5,
  "review_agent": "codex",
  "implementation_agent": "codex",
  "timeout": 3600
}
```

`mode`, `repository`, `start`, `check`, and `max_iterations` are required.
`repository` must be an existing local GitHub repository root with a current
branch and supported `origin`. `start.command` launches the service there and
`start.ready_check` is an HTTP(S) URL used to establish service readiness.
`check` is the read-only Review instruction. `max_iterations` is an integer
from 1 to 50. The optional `review_agent` and `implementation_agent` values are
`codex` (the default) or `claude-code`. `timeout` defaults to 3600 seconds and
must be an integer from 1 to 86400.

Select **Validate JSON & Generate** to inspect the generated plain-Python
workflow, then use **Validate**, **Dry Run**, and **Run**. The declaration and
generated Python are retained with Run history. Progress shows service startup
and Review Fix execution, while the result panel shows the terminal `PASS`,
`FAIL`, or `BLOCKED` JSON result and its iteration history. A stopped or failed
Run can lack a complete structured result; inspect stdout, stderr, retained
resources, and Progress in that case.

API clients can POST `{"json":"..."}` to `/api/review-fix/generate`, use the
returned `generatedCode` with the ordinary validation and dry-run endpoints,
then POST
`{"code":generatedCode,"args":[],"reviewFixJson":originalJson}` to `/api/run`.
The submitted code must exactly match the code generated from the declaration.

## Workflow ownership and roles

The generated Python owns service startup, readiness checks, Review child Runs,
FAIL-to-fix decisions, iteration limits, deadlines, restarts, cleanup, and the
final structured result. The Runner UI only generates, submits, and observes
that workflow; closing the page does not transfer any of its control flow into
the browser.

Each Review is read-only and runs as `review_agent`. Only a `FAIL` result can be
handed to the separate `implementation_agent`, which modifies the declared
repository and must commit a clean result before the workflow restarts the
service and reviews again. `PASS` ends successfully. `BLOCKED` ends without an
implementation attempt. Reaching `max_iterations` with another `FAIL` returns
`FAIL`. Readiness, Review, implementation, or cleanup problems can produce a
`BLOCKED` result; outcome-unknown interruptions fail the Run instead of being
treated as an ordinary review decision.

The readiness URL must not already respond before the managed service starts.
The start command must remain alive after readiness. Use a dedicated local
endpoint and begin with a clean repository on the branch that should receive
fix commits.
