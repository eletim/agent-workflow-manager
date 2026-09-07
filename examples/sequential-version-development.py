#!/usr/bin/env python3
"""Sequential version development with explicit new-run recovery.

Git and GitHub are inspected before every delivery mutation. PurpleMux resources
belong to this run and remain inspectable after failure or stop; a recovery starts
a new run and creates new runtime resources.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    GitHubRepository,
    GitRepository,
    MergeResult,
    PullRequestState,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    ShellCommandRequest,
    WorkerFailure,
    emit_finding,
    emit_run_pr,
    emit_step,
    run_correlation,
)

WORKFLOW_PREFLIGHT = {"commands": ["git", "gh", "purplemux"]}
WORKFLOW_DRY_RUN = 1
WORKFLOW_OUTLINE = [
    "Inspect authoritative Issue topology",
    "Prepare or reuse the feature branch",
    "Implement and independently review",
    "Deliver the exact Issue topology",
    "Review and deliver the whole version",
]
MAX_REVIEWS = 5
IMPLEMENTER_AGENT = "codex"
REVIEWER_AGENT = "codex"
WORKFLOW_POLICY_ISSUE = None
READY_TIMEOUT = 120
TURN_TIMEOUT = 3600
SHELL_TIMEOUT = 1800
COMMAND_TIMEOUT = 30
MERGE_TO_INTEGRATION = True
FINAL_REVIEW = True
MERGE_FINAL = False
POLICY_CONFLICT_MARKER = "POLICY_CONFLICT:"
POLICY_CONFLICT_PR_MARKER = "agent-workflow-manager:policy-conflict:"
POLICY_CONFLICT_WARNINGS: list[tuple[int | None, str]] = []


@dataclass(frozen=True)
class Issue:
    number: int
    branch: str


@dataclass(frozen=True)
class Config:
    repo: Path
    slug: str
    integration_branch: str
    main_branch: str
    issues: tuple[Issue, ...]
    check_command: str
    policy_issue: int | None = None


@dataclass(frozen=True)
class ReviewDelivery:
    outcome: Literal["approved", "continued_with_warning", "review_skipped"]
    head_sha: str
    base_sha: str


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Sequential reviewed Issue development"
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--slug", required=True)
    parser.add_argument("--integration-branch", required=True)
    parser.add_argument("--main-branch", default="main")
    parser.add_argument("--issue", action="append", required=True)
    parser.add_argument("--check-command", required=True)
    parser.add_argument("--policy-issue", type=int)
    args = parser.parse_args()
    issues: list[Issue] = []
    for value in args.issue:
        number, separator, branch = value.partition(":")
        if not separator or not number.isdigit() or not branch.strip():
            parser.error(f"invalid --issue {value!r}; expected NUMBER:BRANCH")
        issues.append(Issue(int(number), branch.strip()))
    branches = [item.branch for item in issues]
    if len({item.number for item in issues}) != len(issues):
        parser.error("Issue numbers must be unique")
    if len(set(branches)) != len(branches):
        parser.error("Issue branches must be unique")
    reserved = {args.integration_branch, args.main_branch}
    if len(reserved) != 2 or any(branch in reserved for branch in branches):
        parser.error("integration, main, and every Issue branch must be distinct")
    if args.policy_issue is not None and args.policy_issue < 1:
        parser.error("policy Issue must be a positive integer")
    if args.policy_issue is not None and args.policy_issue in {
        item.number for item in issues
    }:
        parser.error("policy Issue must differ from every implementation Issue")
    return Config(
        args.repo.resolve(),
        args.slug,
        args.integration_branch,
        args.main_branch,
        tuple(issues),
        args.check_command,
        args.policy_issue,
    )


def short_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:500]


def inspect_pr(
    github: GitHubRepository, *, head: str, base: str
) -> PullRequestState | None:
    try:
        pr = github.find_pr(head=head, base=base, state="OPEN")
    except WorkerFailure as exc:
        emit_finding("github", short_error(exc), status="failed")
        raise
    emit_finding(
        "github",
        f"no open PR for {head} -> {base}"
        if pr is None
        else f"PR #{pr.number} {head} @ {pr.head_sha} -> {base} @ {pr.base_sha}",
    )
    return pr


def create_runtime(config: Config) -> PurpleMuxCLIClient:
    runtime = PurpleMuxRuntime(
        command_timeout_seconds=COMMAND_TIMEOUT, owned_by_run=True
    )
    workspace = runtime.create_workspace(
        CreateWorkspaceRequest(
            str(config.repo),
            f"{config.slug} {config.integration_branch}",
            correlation_id=run_correlation("workflow-workspace"),
        )
    )
    emit_finding("runtime", f"created run-owned PurpleMux workspace {workspace.id}")
    return runtime.workspace(workspace.id)


def create_agent(
    client: PurpleMuxCLIClient, config: Config, *, agent_type: str, name: str
) -> str:
    return client.create_session(
        CreateSessionRequest(agent_type, str(config.repo), agent_type, name=name)
    )


def run_turn(
    client: PurpleMuxCLIClient,
    tab: str,
    name: str,
    prompt: str,
    *,
    iteration: int | None = None,
    pr: PullRequestState | None = None,
) -> str:
    navigation = {"pr_number": pr.number, "pr_url": pr.url} if pr is not None else {}
    emit_step(
        name,
        "started",
        iteration=iteration,
        workspace=client.workspace_id,
        tab=tab,
        **navigation,
    )
    try:
        client.wait_until_ready(tab, READY_TIMEOUT)
        client.send_input(tab, prompt)
        client.wait_for_turn_completion(tab, TURN_TIMEOUT)
        result = client.read_result(tab)
    except BaseException as exc:
        emit_step(
            name,
            "failed",
            iteration=iteration,
            error=short_error(exc),
            workspace=client.workspace_id,
            tab=tab,
            **navigation,
        )
        raise
    emit_step(
        name,
        "completed",
        iteration=iteration,
        workspace=client.workspace_id,
        tab=tab,
        **navigation,
    )
    return result


def run_outline_step(name: str, action):
    """Run one concrete outline unit while retaining detailed nested progress."""
    emit_step(name, "started")
    try:
        result = action()
    except BaseException as exc:
        emit_step(name, "failed", error=short_error(exc))
        raise
    navigation = (
        {"pr_number": result.number, "pr_url": result.url}
        if isinstance(result, PullRequestState)
        else {}
    )
    emit_step(name, "completed", **navigation)
    return result


def decision(result: str) -> str:
    verdict = next((line.strip() for line in result.splitlines() if line.strip()), "")
    if verdict not in {"APPROVED", "CHANGES_REQUESTED"}:
        raise WorkerFailure("reviewer must begin with APPROVED or CHANGES_REQUESTED")
    return verdict


def policy_context(config: Config, *, scope: str) -> str:
    """Return agent guidance without changing prompts when no policy is set."""
    if config.policy_issue is None:
        return ""
    known_conflicts = "".join(
        f"\n- {warning}" for _, warning in POLICY_CONFLICT_WARNINGS
    )
    if known_conflicts:
        known_conflicts = (
            "\nKnown policy conflicts recovered or detected earlier in this workflow:"
            f"{known_conflicts}\n"
        )
    return f"""Before doing anything else, run `gh issue view {config.policy_issue}
--repo {config.slug}` and read policy Issue #{config.policy_issue}. Treat it as
the version-wide design context for {scope},
not as a workflow DSL or a source of ordering, retry, or merge behavior. The
implementation Issue remains the primary requirement. If you find a clear
conflict, continue by following the implementation Issue and include a line
starting with {POLICY_CONFLICT_MARKER} that truthfully describes the conflict.
{known_conflicts}

"""


def record_policy_conflict(issue_number: int | None, warning: str) -> None:
    if any(existing == warning for _, existing in POLICY_CONFLICT_WARNINGS):
        return
    record = (issue_number, warning)
    POLICY_CONFLICT_WARNINGS.append(record)
    print(f"WARN: {warning}", flush=True)
    emit_finding("policy_issue", warning, status="warning")


def emit_policy_conflicts(
    result: str,
    config: Config,
    *,
    scope: str,
    issue_number: int | None = None,
) -> None:
    if config.policy_issue is None:
        return
    for line in result.splitlines():
        marker, separator, detail = line.strip().partition(POLICY_CONFLICT_MARKER)
        if separator and not marker and detail.strip():
            warning = (
                f"Policy Issue #{config.policy_issue} conflicts with {scope}: "
                f"{detail.strip()[:500]}; continuing with the implementation "
                "Issue as the primary requirement."
            )
            record_policy_conflict(issue_number, warning)


def encoded_policy_conflict_marker(warning: str) -> str:
    encoded = base64.b64encode(warning.encode("utf-8")).decode("ascii")
    return f"<!-- {POLICY_CONFLICT_PR_MARKER}{encoded} -->"


def rehydrate_policy_conflicts(
    body: str, config: Config, *, issue_number: int | None
) -> None:
    if config.policy_issue is None:
        return
    prefix = f"<!-- {POLICY_CONFLICT_PR_MARKER}"
    for line in body.splitlines():
        candidate = line.strip()
        if not candidate.startswith(prefix) or not candidate.endswith(" -->"):
            continue
        encoded = candidate[len(prefix) : -len(" -->")]
        try:
            warning = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (UnicodeError, ValueError):
            continue
        record_policy_conflict(issue_number, warning)


def ensure_issue_pr_policy_conflicts(
    github: GitHubRepository,
    pr: PullRequestState,
    issue: Issue,
    config: Config,
) -> PullRequestState:
    if config.policy_issue is None:
        return pr
    body = pr.body
    for issue_number, warning in POLICY_CONFLICT_WARNINGS:
        if issue_number != issue.number:
            continue
        marker = encoded_policy_conflict_marker(warning)
        if marker not in body:
            body = f"{body.rstrip()}\n\nPolicy conflict warning: {warning}\n{marker}"
    return github.update_pr_body(
        pr.number,
        body=body,
        expected_head=issue.branch,
        expected_head_sha=pr.head_sha,
        expected_base=config.integration_branch,
        expected_base_sha=pr.base_sha,
    )


def policy_pr_notes(config: Config) -> str:
    if config.policy_issue is None:
        return ""
    reference = f"https://github.com/{config.slug}/issues/{config.policy_issue}"
    conflict_notes = "".join(
        f"\n- {warning}\n{encoded_policy_conflict_marker(warning)}"
        for _, warning in POLICY_CONFLICT_WARNINGS
    )
    if conflict_notes:
        conflict_notes = f"\n\nPolicy conflict warnings:{conflict_notes}"
    return (
        f"\n\nPolicy context: {reference}\n\n"
        "Policy conflicts, if any, are reported as structured warning findings "
        "and implementation Issues remain authoritative."
        f"{conflict_notes}"
    )


def ensure_base_pr_policy_notes(
    github: GitHubRepository, pr: PullRequestState, config: Config
) -> PullRequestState:
    if config.policy_issue is None:
        return pr
    reference = f"https://github.com/{config.slug}/issues/{config.policy_issue}"
    body = pr.body
    if reference not in body:
        body = f"{body.rstrip()}{policy_pr_notes(config)}"
    else:
        for _, warning in POLICY_CONFLICT_WARNINGS:
            marker = encoded_policy_conflict_marker(warning)
            if marker not in body:
                body = (
                    f"{body.rstrip()}\n\nPolicy conflict warning: {warning}\n{marker}"
                )
    return github.update_pr_body(
        pr.number,
        body=body,
        expected_head=config.integration_branch,
        expected_head_sha=pr.head_sha,
        expected_base=config.main_branch,
        expected_base_sha=pr.base_sha,
    )


def require_clean_worktree(
    repo: GitRepository,
    client: PurpleMuxCLIClient,
    tab: str,
    *,
    context: str,
    iteration: int | None = None,
) -> None:
    state = repo.inspect_worktree()
    if not state.dirty:
        return
    branch = state.current_branch or "detached HEAD"
    run_turn(
        client,
        tab,
        "Clean worktree",
        f"""Your only task is to make the current repository state clean and
correct before {context}. The worktree is on {branch!r}. Inspect Git status and
every existing diff first. Preserve and commit all intended source, test, and
configuration changes. Add only narrow, appropriate .gitignore entries for
generated build or cache artifacts. Remove only clearly disposable generated
artifacts when safe. Do not reinterpret or reimplement the original Issue.

Do not push, modify PR state, merge, start a review, reset, stash, rebase,
force, or discard uncertain work. If any dirty path is ambiguous, preserve it
and clearly explain why it cannot be resolved safely. Finish with a clean
worktree when safe and return a concise summary of exactly what you committed,
ignored, removed, or could not resolve.""",
        iteration=iteration,
    )
    remaining = repo.inspect_worktree()
    if remaining.dirty:
        details = "; ".join(remaining.status[:10])
        if len(remaining.status) > 10:
            details += f"; ... ({len(remaining.status) - 10} more)"
        raise WorkerFailure(
            "cleanup turn could not safely resolve the worktree; "
            f"remaining changes: {details}"
        )
    emit_finding("git", f"cleanup turn left {branch!r} clean before {context}")


def require_agent_result(
    repo: GitRepository,
    client: PurpleMuxCLIClient,
    tab: str,
    branch: str,
    previous_sha: str,
    *,
    allow_unchanged: bool,
    iteration: int | None = None,
) -> tuple[str, bool]:
    repo.require_current_branch(branch)
    require_clean_worktree(
        repo,
        client,
        tab,
        context=f"verifying the coding result on {branch!r}",
        iteration=iteration,
    )
    result = repo.require_committed_result(
        branch, previous_sha=previous_sha, allow_unchanged=allow_unchanged
    )
    assert result.local_sha is not None
    emit_finding("git", f"{branch} is clean at {result.local_sha}")
    return result.local_sha, result.local_sha != previous_sha


def require_warning_delivery(
    repo: GitRepository,
    github: GitHubRepository,
    pr: PullRequestState,
    *,
    head: str,
    base: str,
    expected_head_sha: str,
    expected_base_sha: str,
) -> PullRequestState:
    """Revalidate exact clean, pushed PR topology before unapproved delivery."""
    pushed = repo.require_pushed(head)
    if pushed.local_sha != expected_head_sha:
        raise WorkerFailure(
            f"warning delivery head changed: expected {expected_head_sha}, "
            f"found {pushed.local_sha}"
        )
    current = github.require_pr(
        number=pr.number,
        head=head,
        base=base,
        state="OPEN",
        expected_head_sha=expected_head_sha,
        expected_base_sha=expected_base_sha,
        draft=True,
    )
    if current.auto_merge_enabled:
        raise WorkerFailure(
            f"warning delivery PR #{current.number} has auto-merge enabled"
        )
    if current.merge_queue_entry is not None:
        raise WorkerFailure(
            f"warning delivery PR #{current.number} has a merge queue entry"
        )
    return current


def issue_prompts(issue: Issue, config: Config) -> tuple[str, str]:
    context = policy_context(config, scope=f"Issue #{issue.number}")
    implementation = (
        context
        + f"""Implement Issue #{issue.number} in {config.slug} on the
existing branch {issue.branch}, based on {config.integration_branch}. Read the
Issue with gh. Inspect existing Git and GitHub state before editing because this
may be a new recovery run. Implement only the requested Issue and run appropriate
project tests and checks. Commit every intended source, test, and configuration
change, leaving none uncommitted or untracked. Push the exact feature branch
{issue.branch} after committing. Create or update exactly one Draft PR from
{issue.branch} to {config.integration_branch}. Finish with a clean worktree.

Never reset, rebase, stash, force-push, merge the Issue PR, target
{config.main_branch}, create unrelated PRs, or discard ambiguous local work.
Return a concise summary including the commit SHA and PR number or URL when
available."""
    )
    review = (
        context
        + f"""Independently review Issue #{issue.number} and its PR from
{issue.branch} to {config.integration_branch}. Do not mutate files or PR state.
Return APPROVED or CHANGES_REQUESTED first, followed by actionable findings."""
    )
    return implementation, review


def prepare_issue(
    repo: GitRepository,
    github: GitHubRepository,
    issue: Issue,
    config: Config,
) -> tuple[PullRequestState | None, str, bool] | PullRequestState:
    open_pr = inspect_pr(github, head=issue.branch, base=config.integration_branch)
    merged = github.find_pr(
        head=issue.branch, base=config.integration_branch, state="MERGED"
    )
    if merged is not None:
        if open_pr is not None:
            raise WorkerFailure("merged Issue also has an open same-head PR")
        emit_finding(
            "github", f"Issue #{issue.number} already merged as #{merged.number}"
        )
        return merged
    repo.require_clean()
    integration = repo.synchronize_branch(config.integration_branch)
    assert integration.remote_sha is not None
    if open_pr is None:
        recovery = repo.recover_feature_branch(
            issue.branch,
            base=config.integration_branch,
            expected_base_sha=integration.remote_sha,
        )
        feature = recovery.branch
        reused_existing_work = recovery.reused_existing_work
    else:
        feature = repo.synchronize_branch(issue.branch)
        reused_existing_work = True
        prepared = repo.inspect_feature_preparation(
            issue.branch,
            base=config.integration_branch,
            expected_base_sha=integration.remote_sha,
        )
        if prepared.base_is_ancestor is not True:
            raise WorkerFailure(
                f"existing {issue.branch} does not contain authoritative base "
                f"{integration.remote_sha}; reconcile it before starting a new run"
            )
    assert feature.local_sha is not None
    emit_finding(
        "git",
        f"{issue.branch} contains {config.integration_branch} @ {integration.remote_sha}",
    )
    return (
        open_pr,
        feature.local_sha,
        reused_existing_work,
    )


def return_to_draft_for_review(
    github: GitHubRepository,
    pr: PullRequestState,
    *,
    head: str,
    base: str,
) -> PullRequestState:
    """Ensure an open PR cannot use Ready state as approval provenance."""
    if pr.is_draft:
        return pr
    emit_finding(
        "github",
        f"PR #{pr.number} is Ready without review provenance; returning it to Draft",
    )
    return github.set_draft(
        pr.number,
        draft=True,
        expected_head=head,
        expected_head_sha=pr.head_sha,
        expected_base=base,
        expected_base_sha=pr.base_sha,
    )


def ensure_issue_pr(
    repo: GitRepository,
    github: GitHubRepository,
    issue: Issue,
    config: Config,
    *,
    expected_base_sha: str,
) -> PullRequestState:
    local = repo.require_current_branch(issue.branch)
    assert local.local_sha is not None
    feature = repo.ensure_pushed(issue.branch, expected_local_sha=local.local_sha)
    assert feature.remote_sha is not None
    pr = inspect_pr(github, head=issue.branch, base=config.integration_branch)
    if pr is None:
        pr = github.create_draft_pr(
            head=issue.branch,
            base=config.integration_branch,
            expected_head_sha=feature.remote_sha,
            expected_base_sha=expected_base_sha,
            title=f"Issue #{issue.number}",
            body=f"Sequential implementation of Issue #{issue.number}.",
            correlation_id=run_correlation(f"issue-{issue.number}-pr"),
        )
    return github.require_pr(
        number=pr.number,
        head=issue.branch,
        base=config.integration_branch,
        state="OPEN",
        expected_head_sha=feature.remote_sha,
        expected_base_sha=expected_base_sha,
        draft=True,
    )


def merge_pr_and_advance(
    repo: GitRepository,
    github: GitHubRepository,
    *,
    number: int,
    head: str,
    head_sha: str,
    base: str,
    base_sha: str,
) -> MergeResult:
    """Merge exact reviewed topology from a synchronized local base."""
    synchronized = repo.synchronize_branch(base)
    if synchronized.local_sha != base_sha:
        raise WorkerFailure(f"base branch {base!r} changed before approved merge")
    merged = github.merge_pr(
        number,
        expected_head=head,
        expected_head_sha=head_sha,
        expected_base=base,
        expected_base_sha=base_sha,
    )
    repo.advance_after_merge(
        base,
        previous_sha=base_sha,
        merge_commit_sha=merged.merge_commit_sha,
        required_commit_sha=head_sha,
    )
    return merged


def process_issue(
    issue: Issue,
    config: Config,
    client: PurpleMuxCLIClient,
    repo: GitRepository,
    github: GitHubRepository,
) -> PullRequestState:
    if repo.inspect_worktree().dirty:
        cleanup = create_agent(
            client,
            config,
            agent_type=IMPLEMENTER_AGENT,
            name=f"Issue {issue.number} worktree cleanup",
        )
        require_clean_worktree(
            repo,
            client,
            cleanup,
            context=f"preparing Issue #{issue.number}",
        )
    prepared = prepare_issue(repo, github, issue, config)
    if isinstance(prepared, PullRequestState):
        rehydrate_policy_conflicts(prepared.body, config, issue_number=issue.number)
        print(f"Skipping already-merged Issue #{issue.number}", flush=True)
        return prepared
    existing_pr, start_sha, reused_existing_work = prepared
    if existing_pr is not None:
        existing_pr = return_to_draft_for_review(
            github,
            existing_pr,
            head=issue.branch,
            base=config.integration_branch,
        )
        emit_step(
            f"Issue #{issue.number}",
            "started",
            pr_number=existing_pr.number,
            pr_url=existing_pr.url,
        )
    implementer = create_agent(
        client,
        config,
        agent_type=IMPLEMENTER_AGENT,
        name=f"Issue {issue.number} implementer",
    )
    reviewer = create_agent(
        client,
        config,
        agent_type=REVIEWER_AGENT,
        name=f"Issue {issue.number} reviewer",
    )
    implementation_prompt, review_prompt = issue_prompts(issue, config)
    implementation_result = run_turn(
        client,
        implementer,
        f"Issue #{issue.number} implementation",
        implementation_prompt,
        pr=existing_pr,
    )
    emit_policy_conflicts(
        implementation_result,
        config,
        scope=f"implementation Issue #{issue.number}",
        issue_number=issue.number,
    )
    implementation_sha, _ = require_agent_result(
        repo,
        client,
        implementer,
        issue.branch,
        start_sha,
        allow_unchanged=existing_pr is not None or reused_existing_work,
    )
    integration = repo.inspect_branch(config.integration_branch)
    if integration.remote_sha is None:
        raise WorkerFailure("integration remote branch disappeared")
    pr = ensure_issue_pr(
        repo, github, issue, config, expected_base_sha=integration.remote_sha
    )
    pr = ensure_issue_pr_policy_conflicts(github, pr, issue, config)
    if pr.head_sha != implementation_sha:
        raise WorkerFailure("delivered PR head does not match committed result")
    emit_step(
        f"Issue #{issue.number}",
        "started",
        pr_number=pr.number,
        pr_url=pr.url,
    )
    delivery: ReviewDelivery | None = None
    for review_number in range(1, MAX_REVIEWS + 1):
        result = run_turn(
            client,
            reviewer,
            f"Issue #{issue.number} review",
            f"{review_prompt}\nReview exact head {pr.head_sha} against base {pr.base_sha}.",
            iteration=review_number,
            pr=pr,
        )
        emit_policy_conflicts(
            result,
            config,
            scope=f"review of Issue #{issue.number}",
            issue_number=issue.number,
        )
        reviewed_sha, reviewer_changed = require_agent_result(
            repo,
            client,
            implementer,
            issue.branch,
            pr.head_sha,
            allow_unchanged=True,
            iteration=review_number,
        )
        if reviewer_changed:
            pushed = repo.ensure_pushed(issue.branch, expected_local_sha=reviewed_sha)
            assert pushed.remote_sha is not None
            pr = github.require_pr(
                number=pr.number,
                head=issue.branch,
                base=config.integration_branch,
                state="OPEN",
                expected_head_sha=pushed.remote_sha,
                expected_base_sha=pr.base_sha,
                draft=True,
            )
            pr = ensure_issue_pr_policy_conflicts(github, pr, issue, config)
            emit_finding(
                "git",
                f"review changed {issue.branch}; approval invalidated at "
                f"{reviewed_sha}",
            )
            continue
        current = github.require_pr(
            number=pr.number,
            head=issue.branch,
            base=config.integration_branch,
            state="OPEN",
            expected_head_sha=pr.head_sha,
            expected_base_sha=pr.base_sha,
            draft=True,
        )
        current = ensure_issue_pr_policy_conflicts(github, current, issue, config)
        if decision(result) == "APPROVED":
            delivery = ReviewDelivery("approved", current.head_sha, current.base_sha)
            break
        if review_number == MAX_REVIEWS:
            pr = require_warning_delivery(
                repo,
                github,
                current,
                head=issue.branch,
                base=config.integration_branch,
                expected_head_sha=current.head_sha,
                expected_base_sha=current.base_sha,
            )
            warning = (
                f"Issue #{issue.number} review limit {MAX_REVIEWS} reached with "
                "CHANGES_REQUESTED; continuing without reviewer approval."
            )
            print(f"WARN: {warning}", flush=True)
            emit_finding("github", warning, status="warning")
            delivery = ReviewDelivery(
                "continued_with_warning", current.head_sha, current.base_sha
            )
            break
        fix_result = run_turn(
            client,
            implementer,
            f"Issue #{issue.number} fixes",
            policy_context(config, scope=f"fixes for Issue #{issue.number}")
            + f"""Re-evaluate every finding below. If warranted, fix, test, commit,
and leave the worktree clean. If no change is warranted, leave it clean and
            explain why; do not create an empty commit.\n\n{result}""",
            iteration=review_number,
            pr=pr,
        )
        emit_policy_conflicts(
            fix_result,
            config,
            scope=f"fixes for Issue #{issue.number}",
            issue_number=issue.number,
        )
        fixed_sha, changed = require_agent_result(
            repo,
            client,
            implementer,
            issue.branch,
            current.head_sha,
            allow_unchanged=True,
            iteration=review_number,
        )
        if not changed:
            current = ensure_issue_pr_policy_conflicts(github, current, issue, config)
            warning = (
                f"Issue #{issue.number} reviewer requested changes, but the "
                "implementer re-evaluated the finding and produced no code "
                "changes; continuing without reviewer approval."
            )
            pr = require_warning_delivery(
                repo,
                github,
                current,
                head=issue.branch,
                base=config.integration_branch,
                expected_head_sha=current.head_sha,
                expected_base_sha=current.base_sha,
            )
            print(f"WARN: {warning}", flush=True)
            emit_finding("git", warning, status="warning")
            delivery = ReviewDelivery(
                "continued_with_warning", current.head_sha, current.base_sha
            )
            break
        pushed = repo.ensure_pushed(issue.branch, expected_local_sha=fixed_sha)
        assert pushed.remote_sha is not None
        pr = github.require_pr(
            number=pr.number,
            head=issue.branch,
            base=config.integration_branch,
            state="OPEN",
            expected_head_sha=pushed.remote_sha,
            expected_base_sha=current.base_sha,
            draft=True,
        )
        pr = ensure_issue_pr_policy_conflicts(github, pr, issue, config)
    if delivery is None:
        raise WorkerFailure(f"Issue #{issue.number} ended without a review outcome")
    pr = github.set_draft(
        pr.number,
        draft=False,
        expected_head=issue.branch,
        expected_head_sha=delivery.head_sha,
        expected_base=config.integration_branch,
        expected_base_sha=delivery.base_sha,
    )
    if not MERGE_TO_INTEGRATION:
        qualifier = (
            "Approved"
            if delivery.outcome == "approved"
            else "Unapproved warning-continuation"
        )
        print(f"{qualifier} Issue #{issue.number} PR is Ready: {pr.url}", flush=True)
        return pr
    merged = merge_pr_and_advance(
        repo,
        github,
        number=pr.number,
        head=issue.branch,
        head_sha=delivery.head_sha,
        base=config.integration_branch,
        base_sha=delivery.base_sha,
    )
    qualifier = "approved" if delivery.outcome == "approved" else "unapproved"
    print(f"Merged {qualifier} Issue #{issue.number} PR: {merged.pr.url}", flush=True)
    return merged.pr


def run_final_checks(client: PurpleMuxCLIClient, config: Config) -> None:
    shell = client.start_shell(
        ShellCommandRequest(config.check_command, str(config.repo), "Final checks")
    )
    client.wait_for_shell_completion(shell, SHELL_TIMEOUT)
    result = client.read_shell_result(shell)
    if result.exit_code != 0:
        failure = result.failure_message("final whole-version checks")
        emit_step(
            "final whole-version checks",
            "failed",
            error=failure,
            workspace=client.workspace_id,
            tab=shell,
        )
        raise WorkerFailure(failure)


def review_whole_version(
    config: Config,
    client: PurpleMuxCLIClient,
    repo: GitRepository,
    github: GitHubRepository,
    pr: PullRequestState,
) -> tuple[PullRequestState, ReviewDelivery]:
    """Review, fix, and check the whole version as one outline-level phase."""
    fixer = create_agent(
        client,
        config,
        agent_type=IMPLEMENTER_AGENT,
        name="Whole-version fixer",
    )
    reviewer = create_agent(
        client,
        config,
        agent_type=REVIEWER_AGENT,
        name="Whole-version reviewer",
    )
    delivery: ReviewDelivery | None = None
    for review_number in range(1, MAX_REVIEWS + 1):
        result = run_turn(
            client,
            reviewer,
            "Whole-version reviewer turn",
            policy_context(config, scope="the whole-version review")
            + f"Review exact head {pr.head_sha} against final base {pr.base_sha}. "
            "Review the integration branch as one version, including design "
            "consistency, duplication, cross-feature problems, and responsibility "
            "boundaries; do not merely repeat individual PR reviews. Return "
            "APPROVED or CHANGES_REQUESTED first; do not mutate anything.",
            iteration=review_number,
        )
        emit_policy_conflicts(result, config, scope="the integrated version")
        reviewed_sha, reviewer_changed = require_agent_result(
            repo,
            client,
            fixer,
            config.integration_branch,
            pr.head_sha,
            allow_unchanged=True,
            iteration=review_number,
        )
        if reviewer_changed:
            pushed = repo.ensure_pushed(
                config.integration_branch, expected_local_sha=reviewed_sha
            )
            assert pushed.remote_sha is not None
            pr = github.require_pr(
                number=pr.number,
                head=config.integration_branch,
                base=config.main_branch,
                state="OPEN",
                expected_head_sha=pushed.remote_sha,
                expected_base_sha=pr.base_sha,
                draft=True,
            )
            pr = ensure_base_pr_policy_notes(github, pr, config)
            emit_finding(
                "git",
                "whole-version review changed the integration branch; "
                f"approval invalidated at {reviewed_sha}",
            )
            continue
        current = github.require_pr(
            number=pr.number,
            head=config.integration_branch,
            base=config.main_branch,
            state="OPEN",
            expected_head_sha=pr.head_sha,
            expected_base_sha=pr.base_sha,
            draft=True,
        )
        current = ensure_base_pr_policy_notes(github, current, config)
        verdict = decision(result)
        warning: str | None = None
        if verdict == "CHANGES_REQUESTED":
            if review_number == MAX_REVIEWS:
                warning = (
                    f"Whole-version review limit {MAX_REVIEWS} reached with "
                    "CHANGES_REQUESTED; keeping the Base PR Draft and continuing "
                    "without reviewer approval."
                )
            else:
                fix_result = run_turn(
                    client,
                    fixer,
                    "Whole-version fixes",
                    policy_context(config, scope="whole-version fixes")
                    + f"""Re-evaluate every finding. If warranted, fix, test, commit,
and leave the worktree clean. If not, leave it clean and explain why.\n\n{result}""",
                    iteration=review_number,
                )
                emit_policy_conflicts(fix_result, config, scope="whole-version fixes")
                fixed_sha, changed = require_agent_result(
                    repo,
                    client,
                    fixer,
                    config.integration_branch,
                    current.head_sha,
                    allow_unchanged=True,
                    iteration=review_number,
                )
                if changed:
                    pushed = repo.ensure_pushed(
                        config.integration_branch, expected_local_sha=fixed_sha
                    )
                    assert pushed.remote_sha is not None
                    pr = github.require_pr(
                        number=pr.number,
                        head=config.integration_branch,
                        base=config.main_branch,
                        state="OPEN",
                        expected_head_sha=pushed.remote_sha,
                        expected_base_sha=current.base_sha,
                        draft=True,
                    )
                    pr = ensure_base_pr_policy_notes(github, pr, config)
                    continue
                current = ensure_base_pr_policy_notes(github, current, config)
                warning = (
                    "Whole-version reviewer requested changes, but the fixer "
                    "re-evaluated the findings and produced no code changes; "
                    "keeping the Base PR Draft and continuing without reviewer "
                    "approval."
                )
        run_final_checks(client, config)
        checked_sha, checks_changed = require_agent_result(
            repo,
            client,
            fixer,
            config.integration_branch,
            current.head_sha,
            allow_unchanged=True,
            iteration=review_number,
        )
        if checks_changed:
            if review_number == MAX_REVIEWS:
                raise WorkerFailure(
                    "final checks changed the integration branch at the review "
                    "limit; refusing unreviewed delivery"
                )
            pushed = repo.ensure_pushed(
                config.integration_branch, expected_local_sha=checked_sha
            )
            assert pushed.remote_sha is not None
            pr = github.require_pr(
                number=pr.number,
                head=config.integration_branch,
                base=config.main_branch,
                state="OPEN",
                expected_head_sha=pushed.remote_sha,
                expected_base_sha=current.base_sha,
                draft=True,
            )
            emit_finding(
                "git",
                "final checks changed the integration branch; approval "
                f"invalidated at {checked_sha}",
            )
            continue
        if warning is None:
            delivery = ReviewDelivery("approved", current.head_sha, current.base_sha)
        else:
            pr = require_warning_delivery(
                repo,
                github,
                current,
                head=config.integration_branch,
                base=config.main_branch,
                expected_head_sha=current.head_sha,
                expected_base_sha=current.base_sha,
            )
            print(f"WARN: {warning}", flush=True)
            emit_finding("github", warning, status="warning")
            delivery = ReviewDelivery(
                "continued_with_warning", current.head_sha, current.base_sha
            )
        break
    if delivery is None:
        raise WorkerFailure("whole-version review ended without a review outcome")
    return pr, delivery


def integration_delivery(
    config: Config,
    client: PurpleMuxCLIClient,
    repo: GitRepository,
    github: GitHubRepository,
) -> PullRequestState:
    integration = repo.synchronize_branch(config.integration_branch)
    main = repo.inspect_branch(config.main_branch)
    if integration.remote_sha is None or main.remote_sha is None:
        raise WorkerFailure("integration or final remote branch is missing")
    pr = inspect_pr(github, head=config.integration_branch, base=config.main_branch)
    merged_pr = github.find_pr(
        head=config.integration_branch, base=config.main_branch, state="MERGED"
    )
    if merged_pr is not None:
        if pr is not None:
            raise WorkerFailure("merged final delivery also has an open same-head PR")
        if merged_pr.head_sha != integration.remote_sha:
            raise WorkerFailure(
                f"historical merged final PR #{merged_pr.number} has head "
                f"{merged_pr.head_sha}, but current {config.integration_branch} "
                f"is {integration.remote_sha}"
            )
        merged_pr = github.require_pr(
            number=merged_pr.number,
            head=config.integration_branch,
            base=config.main_branch,
            state="MERGED",
            expected_head_sha=integration.remote_sha,
        )
        rehydrate_policy_conflicts(merged_pr.body, config, issue_number=None)
        final_branch = repo.synchronize_branch(config.main_branch)
        if final_branch.remote_sha is None:
            raise WorkerFailure("final remote branch disappeared during recovery")
        repo.require_contains(config.main_branch, integration.remote_sha)
        emit_finding(
            "github",
            f"final delivery already merged as #{merged_pr.number} at "
            f"{integration.remote_sha}",
        )
        emit_run_pr(merged_pr.number, merged_pr.url)
        if FINAL_REVIEW:
            emit_step(
                "Whole-version review",
                "completed",
                message=f"delivery already merged as PR #{merged_pr.number}",
            )
        emit_step(
            "Final integration PR",
            "completed",
            message=f"already merged as PR #{merged_pr.number}",
        )
        return merged_pr
    if pr is None:
        pr = github.create_draft_pr(
            head=config.integration_branch,
            base=config.main_branch,
            expected_head_sha=integration.remote_sha,
            expected_base_sha=main.remote_sha,
            title=f"Integrate {config.integration_branch}",
            body=(
                "Sequential integration; Ready only after whole-version checks."
                f"{policy_pr_notes(config)}"
            ),
            correlation_id=run_correlation("integration-pr"),
        )
    else:
        pr = return_to_draft_for_review(
            github,
            pr,
            head=config.integration_branch,
            base=config.main_branch,
        )
    pr = github.require_pr(
        number=pr.number,
        head=config.integration_branch,
        base=config.main_branch,
        state="OPEN",
        expected_head_sha=integration.remote_sha,
        expected_base_sha=main.remote_sha,
        draft=True,
    )
    rehydrate_policy_conflicts(pr.body, config, issue_number=None)
    pr = ensure_base_pr_policy_notes(github, pr, config)
    emit_run_pr(pr.number, pr.url)
    if FINAL_REVIEW:
        pr, delivery = run_outline_step(
            "Whole-version review",
            lambda: review_whole_version(config, client, repo, github, pr),
        )
        pr = ensure_base_pr_policy_notes(github, pr, config)
    else:
        cleanup: str | None = None
        for check_number in range(1, MAX_REVIEWS + 1):
            run_final_checks(client, config)
            state = repo.inspect_worktree()
            if state.dirty and cleanup is None:
                cleanup = create_agent(
                    client,
                    config,
                    agent_type=IMPLEMENTER_AGENT,
                    name="Whole-version cleanup",
                )
            if cleanup is None:
                checked = repo.require_committed_result(
                    config.integration_branch,
                    previous_sha=pr.head_sha,
                    allow_unchanged=True,
                )
                assert checked.local_sha is not None
                checked_sha = checked.local_sha
                checks_changed = checked_sha != pr.head_sha
            else:
                checked_sha, checks_changed = require_agent_result(
                    repo,
                    client,
                    cleanup,
                    config.integration_branch,
                    pr.head_sha,
                    allow_unchanged=True,
                    iteration=check_number,
                )
            if not checks_changed:
                break
            pushed = repo.ensure_pushed(
                config.integration_branch, expected_local_sha=checked_sha
            )
            assert pushed.remote_sha is not None
            pr = github.require_pr(
                number=pr.number,
                head=config.integration_branch,
                base=config.main_branch,
                state="OPEN",
                expected_head_sha=pushed.remote_sha,
                expected_base_sha=pr.base_sha,
                draft=True,
            )
        else:
            raise WorkerFailure("final checks kept changing the integration branch")
        delivery = ReviewDelivery("review_skipped", pr.head_sha, pr.base_sha)

    def finalize() -> PullRequestState:
        if delivery.outcome == "continued_with_warning":
            return require_warning_delivery(
                repo,
                github,
                pr,
                head=config.integration_branch,
                base=config.main_branch,
                expected_head_sha=delivery.head_sha,
                expected_base_sha=delivery.base_sha,
            )
        ready = github.set_draft(
            pr.number,
            draft=False,
            expected_head=config.integration_branch,
            expected_head_sha=delivery.head_sha,
            expected_base=config.main_branch,
            expected_base_sha=delivery.base_sha,
        )
        if not MERGE_FINAL:
            return ready
        merged = merge_pr_and_advance(
            repo,
            github,
            number=ready.number,
            head=config.integration_branch,
            head_sha=ready.head_sha,
            base=config.main_branch,
            base_sha=ready.base_sha,
        )
        return merged.pr

    return run_outline_step("Final integration PR", finalize)


def main() -> None:
    config = parse_args()
    repo = GitRepository.open(
        config.repo,
        expected_github_slug=config.slug,
        command_timeout_seconds=COMMAND_TIMEOUT,
    )
    github = GitHubRepository.open(config.slug, command_timeout_seconds=COMMAND_TIMEOUT)
    client = create_runtime(config)
    for issue in config.issues:
        run_outline_step(
            f"Issue #{issue.number}",
            lambda issue=issue: process_issue(issue, config, client, repo, github),
        )
    ready = integration_delivery(config, client, repo, github)
    if ready.state == "MERGED":
        outcome = "Merged"
    elif ready.is_draft:
        outcome = "Draft (warning continuation)"
    else:
        outcome = "Ready (not merged)"
    print(f"Whole-version PR is {outcome}: {ready.url}", flush=True)
    if config.policy_issue is not None:
        print(f"Policy Issue: {config.slug}#{config.policy_issue}", flush=True)


if __name__ == "__main__":
    main()
