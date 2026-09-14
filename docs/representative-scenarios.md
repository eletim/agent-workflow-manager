# Representative Scenarios

These scenarios are a small, stable description of how people typically use
Agent Workflow Manager (AWM) and what they should expect. They provide shared
human context for design and review; they are not an exhaustive feature list.

## One bounded agent task

A person opens Prompt mode, selects an existing working directory and agent,
and asks for one bounded task. AWM runs the agent in PurpleMux and reports its
structured result. The workspace and agent tab remain available in PurpleMux
for inspection after the run.

## A custom workflow in ordinary Python

A workflow author writes Python that sequences tools, branches, retries, and
decides when work is complete. AWM validates and runs that program while the UI
reports progress and findings. The Python program, rather than the UI or
Runner, remains the authority for control flow. For dry-run-eligible workflows,
Dry Run stops before the first reachable mutation.

## Reviewed delivery from a work-item plan

A maintainer starts Issue Driven mode with GitHub Issues and/or inline mini
tasks. AWM turns the configuration into inspectable Python. Each dispatched work
item requiring implementation receives its own branch and Draft pull request,
separate scope and correctness reviews, and advances only when exact Git and
GitHub topology is confirmed.
Configured policy decides whether reviewed work is integrated and whether the
final delivery pull request is merely made ready or merged.

## Recovery after interruption

An Issue Driven run stops after some work has already reached commits or pull
requests. A new run inspects those durable artifacts and the persisted work-item
plan, then continues from the confirmed state without restoring the old Python
process or treating terminal output and chat history as workflow state.

## Ambiguous or unsafe state

A run encounters invalid configuration, conflicting branch or pull-request
topology, a dirty delivery boundary, or an uncertain mutation result. AWM fails
closed or records an explicit warning where the documented policy allows
continuation. It does not silently reset work, guess ownership, or present an
unapproved result as approved.

## Scope of this document

This curated set is documentation, not the `scenarios` inventory supplied for a
particular Issue Driven run. A run-specific inventory should describe behavior
relevant to that version so its Scenario Gate can select and compare appropriate
Before/After cases. This document is also separate from exhaustive automated
tests and from any larger validation corpus. Those artifacts may evolve with
implementation coverage without turning this human-oriented set into a catalog
of every case.
