#!/usr/bin/env python3
"""Sequential version development with explicit new-run recovery.

Git and GitHub are inspected before every delivery mutation. PurpleMux resources
belong to this run and remain inspectable after failure or stop; a recovery starts
a new run and creates new runtime resources.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    GitHubRepository,
    GitRepository,
    PullRequestState,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    ShellCommandRequest,
    WorkerFailure,
    emit_finding,
    emit_step,
    run_correlation,
)

WORKFLOW_PREFLIGHT = {"commands": ["git", "gh", "purplemux"]}
WORKFLOW_DRY_RUN = 1
WORKFLOW_OUTLINE = [
    "Inspect authoritative Issue topology",
    "Prepare or reuse the feature branch",
    "Implement and independently review",
    "Deliver the exact approved Issue topology",
    "Review and deliver the whole version",
]
MAX_REVIEWS = 4
READY_TIMEOUT = 120
TURN_TIMEOUT = 3600
SHELL_TIMEOUT = 1800
COMMAND_TIMEOUT = 30
MERGE_TO_INTEGRATION = True
FINAL_REVIEW = True
MERGE_FINAL = False


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
    return Config(
        args.repo.resolve(),
        args.slug,
        args.integration_branch,
        args.main_branch,
        tuple(issues),
        args.check_command,
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


def create_agent(client: PurpleMuxCLIClient, config: Config, *, name: str) -> str:
    return client.create_session(
        CreateSessionRequest("codex", str(config.repo), "codex", name=name)
    )


def run_turn(
    client: PurpleMuxCLIClient,
    tab: str,
    name: str,
    prompt: str,
    *,
    iteration: int | None = None,
) -> str:
    emit_step(
        name, "started", iteration=iteration, workspace=client.workspace_id, tab=tab
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
        )
        raise
    emit_step(
        name, "completed", iteration=iteration, workspace=client.workspace_id, tab=tab
    )
    return result


def decision(result: str) -> str:
    verdict = next((line.strip() for line in result.splitlines() if line.strip()), "")
    if verdict not in {"APPROVED", "CHANGES_REQUESTED"}:
        raise WorkerFailure("reviewer must begin with APPROVED or CHANGES_REQUESTED")
    return verdict


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
    if repo.inspect_worktree().dirty:
        run_turn(
            client,
            tab,
            "Commit agent result",
            f"""The worktree for {branch} is dirty. Commit every intended change
and leave it clean. Do not reset, stash, clean, discard, push, change a PR, merge,
or start another review.""",
            iteration=iteration,
        )
    result = repo.require_committed_result(
        branch, previous_sha=previous_sha, allow_unchanged=allow_unchanged
    )
    assert result.local_sha is not None
    emit_finding("git", f"{branch} is clean at {result.local_sha}")
    return result.local_sha, result.local_sha != previous_sha


def issue_prompts(issue: Issue, config: Config) -> tuple[str, str]:
    implementation = f"""Implement Issue #{issue.number} in {config.slug} on the
existing branch {issue.branch}, based on {config.integration_branch}. Read the
Issue with gh. Inspect existing Git and GitHub state before editing because this
may be a new recovery run. Edit, test, commit, and leave the working tree clean.
You may push and create one Draft PR to {config.integration_branch},
but the Workflow safely completes either omitted delivery step. Never reset,
force-push, merge, or target {config.main_branch}. Return a concise summary."""
    review = f"""Independently review Issue #{issue.number} and its PR from
{issue.branch} to {config.integration_branch}. Do not mutate files or PR state.
Return APPROVED or CHANGES_REQUESTED first, followed by actionable findings."""
    return implementation, review


def prepare_issue(
    repo: GitRepository,
    github: GitHubRepository,
    issue: Issue,
    config: Config,
) -> tuple[PullRequestState | None, str, bool] | None:
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
        return None
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
        if not open_pr.is_draft:
            open_pr = github.set_draft(
                open_pr.number,
                draft=True,
                expected_head=issue.branch,
                expected_head_sha=open_pr.head_sha,
                expected_base=config.integration_branch,
                expected_base_sha=open_pr.base_sha,
            )
    assert feature.local_sha is not None
    emit_finding(
        "git",
        f"{issue.branch} contains {config.integration_branch} @ {integration.remote_sha}",
    )
    return open_pr, feature.local_sha, reused_existing_work


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


def process_issue(
    issue: Issue,
    config: Config,
    client: PurpleMuxCLIClient,
    repo: GitRepository,
    github: GitHubRepository,
) -> None:
    prepared = prepare_issue(repo, github, issue, config)
    if prepared is None:
        print(f"Skipping already-merged Issue #{issue.number}", flush=True)
        return
    existing_pr, start_sha, reused_existing_work = prepared
    implementer = create_agent(client, config, name=f"Issue {issue.number} implementer")
    reviewer = create_agent(client, config, name=f"Issue {issue.number} reviewer")
    implementation_prompt, review_prompt = issue_prompts(issue, config)
    run_turn(
        client,
        implementer,
        f"Issue #{issue.number} implementation",
        implementation_prompt,
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
    if pr.head_sha != implementation_sha:
        raise WorkerFailure("delivered PR head does not match committed result")
    approved_head: str | None = None
    approved_base: str | None = None
    for review_number in range(1, MAX_REVIEWS + 1):
        result = run_turn(
            client,
            reviewer,
            f"Issue #{issue.number} review",
            f"{review_prompt}\nReview exact head {pr.head_sha} against base {pr.base_sha}.",
            iteration=review_number,
        )
        current = github.require_pr(
            number=pr.number,
            head=issue.branch,
            base=config.integration_branch,
            state="OPEN",
            expected_head_sha=pr.head_sha,
            expected_base_sha=pr.base_sha,
            draft=True,
        )
        if decision(result) == "APPROVED":
            approved_head, approved_base = current.head_sha, current.base_sha
            break
        if review_number == MAX_REVIEWS:
            break
        run_turn(
            client,
            implementer,
            f"Issue #{issue.number} fixes",
            f"""Re-evaluate every finding below. If warranted, fix, test, commit,
and leave the worktree clean. If no change is warranted, leave it clean and
explain why; do not create an empty commit.\n\n{result}""",
            iteration=review_number,
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
            warning = (
                "WARN: reviewer requested changes, but implementer re-evaluated "
                "the finding and produced no code changes. Continuing by policy."
            )
            print(warning, flush=True)
            emit_finding("git", warning, status="info")
            approved_head, approved_base = current.head_sha, current.base_sha
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
    if approved_head is None or approved_base is None:
        raise WorkerFailure(f"Issue #{issue.number} ended without approval")
    pr = github.set_draft(
        pr.number,
        draft=False,
        expected_head=issue.branch,
        expected_head_sha=approved_head,
        expected_base=config.integration_branch,
        expected_base_sha=approved_base,
    )
    if not MERGE_TO_INTEGRATION:
        print(f"Approved Issue #{issue.number} PR is Ready: {pr.url}", flush=True)
        return
    merged = github.merge_pr(
        pr.number,
        expected_head=issue.branch,
        expected_head_sha=approved_head,
        expected_base=config.integration_branch,
        expected_base_sha=approved_base,
    )
    repo.advance_after_merge(
        config.integration_branch,
        previous_sha=approved_base,
        merge_commit_sha=merged.merge_commit_sha,
        required_commit_sha=approved_head,
    )
    print(f"Merged approved Issue #{issue.number} PR: {merged.pr.url}", flush=True)


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
    if pr is None:
        pr = github.create_draft_pr(
            head=config.integration_branch,
            base=config.main_branch,
            expected_head_sha=integration.remote_sha,
            expected_base_sha=main.remote_sha,
            title=f"Integrate {config.integration_branch}",
            body="Sequential integration; Ready only after whole-version checks.",
            correlation_id=run_correlation("integration-pr"),
        )
    elif not pr.is_draft:
        pr = github.set_draft(
            pr.number,
            draft=True,
            expected_head=config.integration_branch,
            expected_head_sha=pr.head_sha,
            expected_base=config.main_branch,
            expected_base_sha=pr.base_sha,
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
    approved_head, approved_base = pr.head_sha, pr.base_sha
    if FINAL_REVIEW:
        fixer = create_agent(client, config, name="Whole-version fixer")
        reviewer = create_agent(client, config, name="Whole-version reviewer")
        approved_head = approved_base = None
        for review_number in range(1, MAX_REVIEWS + 1):
            result = run_turn(
                client,
                reviewer,
                "Whole-version review",
                f"Review exact head {pr.head_sha} against final base {pr.base_sha}. "
                "Return APPROVED or CHANGES_REQUESTED first; do not mutate anything.",
                iteration=review_number,
            )
            current = github.require_pr(
                number=pr.number,
                head=config.integration_branch,
                base=config.main_branch,
                state="OPEN",
                expected_head_sha=pr.head_sha,
                expected_base_sha=pr.base_sha,
                draft=True,
            )
            if decision(result) == "APPROVED":
                approved_head, approved_base = current.head_sha, current.base_sha
                break
            if review_number == MAX_REVIEWS:
                break
            run_turn(
                client,
                fixer,
                "Whole-version fixes",
                f"""Re-evaluate every finding. If warranted, fix, test, commit,
and leave the worktree clean. If not, leave it clean and explain why.\n\n{result}""",
                iteration=review_number,
            )
            fixed_sha, changed = require_agent_result(
                repo,
                client,
                fixer,
                config.integration_branch,
                current.head_sha,
                allow_unchanged=True,
                iteration=review_number,
            )
            if not changed:
                approved_head, approved_base = current.head_sha, current.base_sha
                break
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
        if approved_head is None or approved_base is None:
            raise WorkerFailure("whole-version review ended without approval")
    assert approved_head is not None and approved_base is not None
    run_final_checks(client, config)
    ready = github.set_draft(
        pr.number,
        draft=False,
        expected_head=config.integration_branch,
        expected_head_sha=approved_head,
        expected_base=config.main_branch,
        expected_base_sha=approved_base,
    )
    if not MERGE_FINAL:
        return ready
    merged = github.merge_pr(
        ready.number,
        expected_head=config.integration_branch,
        expected_head_sha=ready.head_sha,
        expected_base=config.main_branch,
        expected_base_sha=ready.base_sha,
    )
    repo.advance_after_merge(
        config.main_branch,
        previous_sha=ready.base_sha,
        merge_commit_sha=merged.merge_commit_sha,
        required_commit_sha=ready.head_sha,
    )
    return merged.pr


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
        process_issue(issue, config, client, repo, github)
    ready = integration_delivery(config, client, repo, github)
    outcome = "Merged" if MERGE_FINAL else "Ready (not merged)"
    print(f"Whole-version PR is {outcome}: {ready.url}", flush=True)


if __name__ == "__main__":
    main()
