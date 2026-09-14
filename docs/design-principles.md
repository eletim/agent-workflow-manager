# Design Principles

This document is the single authoritative source for Agent Workflow Manager's
design principles. Other documentation may describe how the system applies
them, but must not maintain a competing principle list.

- **Existing PurpleMux.** AWM uses PurpleMux as the agent and
  managed-terminal runtime; it does not reproduce PurpleMux lifecycle or
  runtime state.
- **Plain-Python workflows.** Workflow authors use ordinary Python syntax,
  libraries, and processes, not a workflow DSL, graph model, or parallel state
  machine.
- **Python control flow is the source of truth.** Sequencing, branching,
  retries, success criteria, and cleanup decisions live in the workflow's
  Python code, never in the Runner, frontend, progress events, or terminal
  output.
- **Minimal context.** Give each agent turn only the task, role,
  constraints, and authoritative state it needs; do not make accumulated chat,
  terminal text, or unrelated repository history implicit workflow state.
- **Role separation.** Implementation, scope review, and correctness
  review are independent responsibilities with bounded handoffs; observation,
  runtime ownership, and workflow decisions likewise stay with their designated
  components.
- **Execution is frontend-independent.** The frontend starts, stops, and
  observes workflows, but workflow behavior does not depend on UI state or a
  duplicated UI-side orchestration model.
- **Public contracts.** AWM talks to PurpleMux and other
  external systems through supported public CLI or API contracts, never private
  internals such as tmux state or screen-text parsing.
- **Durable traceability.** Commits, pull requests, structured results,
  and registered resource identities provide inspectable evidence and recovery
  anchors; ephemeral process memory and progress displays are not authorities.
