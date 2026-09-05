#!/usr/bin/env python3
"""Lower-level direct plain-Python sequential version-development workflow.

AWM owns structural Git/GitHub/runtime safety and delivery gap absorption.
Agents own edits, project checks, commits, and a clean worktree. Dry Run executes
this program to its first mutation. This
configurable CLI example intentionally uses the direct repository path; normal
repository-modifying Workflow mode should use ``prepare_run_repository()``.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    GitHubRepository,
    GitRepository,
    MutationOutcomeUnknown,
    PullRequestState,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    ResumeCheckpoint,
    ShellCommandRequest,
    WorkerFailure,
    emit_finding,
    emit_step,
    resume_checkpoint,
    run_correlation,
    save_checkpoint,
)

WORKFLOW_PREFLIGHT = {"commands": ["git", "gh", "purplemux"]}
WORKFLOW_DRY_RUN = 1
WORKFLOW_OUTLINE = [
    "Inspect Issue PR topology",
    "Prepare feature branch",
    "Run implementation and independent review",
    "Merge exact approved Issue topology",
    "Review whole version and make integration PR Ready",
]
MAX_REVIEWS = 4
READY_TIMEOUT = 120
TURN_TIMEOUT = 3600
SHELL_TIMEOUT = 1800
COMMAND_TIMEOUT = 30


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

    @property
    def signature(self) -> str:
        values = [
            str(self.repo),
            self.slug,
            self.integration_branch,
            self.main_branch,
            self.check_command,
            *(f"{item.number}:{item.branch}" for item in self.issues),
        ]
        return hashlib.sha256("\0".join(values).encode()).hexdigest()[:16]


@dataclass
class Recovery:
    workspace: str | None = None
    phase: str = "initialized"
    issue_number: int | None = None
    implementer: str | None = None
    reviewer: str | None = None
    reviews_used: int = 0
    approved_sha: str | None = None
    approved_base_sha: str | None = None
    review_outcome: str | None = None
    agent_turn_start_sha: str | None = None
    turn_sha: str | None = None
    turn_base_sha: str | None = None
    prepared_base_sha: str | None = None
    correlation_id: str | None = None
    check_shell: str | None = None
    check_result_path: str | None = None

    def checkpoint(self, config: Config) -> None:
        data = {
            "config": config.signature,
            "phase": self.phase,
            "reviews_used": str(self.reviews_used),
        }
        values = {
            "workspace": self.workspace,
            "issue": str(self.issue_number) if self.issue_number is not None else None,
            "implementer": self.implementer,
            "reviewer": self.reviewer,
            "approved_sha": self.approved_sha,
            "approved_base_sha": self.approved_base_sha,
            "review_outcome": self.review_outcome,
            "agent_turn_start_sha": self.agent_turn_start_sha,
            "turn_sha": self.turn_sha,
            "turn_base_sha": self.turn_base_sha,
            "prepared_base_sha": self.prepared_base_sha,
            "correlation_id": self.correlation_id,
            "check_shell": self.check_shell,
            "check_result_path": self.check_result_path,
        }
        data.update({key: value for key, value in values.items() if value is not None})
        save_checkpoint(self.phase, data)


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


def load_recovery(config: Config, checkpoint: ResumeCheckpoint | None) -> Recovery:
    if checkpoint is None:
        return Recovery()
    data = checkpoint.data
    if data.get("config") != config.signature:
        raise WorkerFailure("resume arguments do not match the checkpointed workflow")
    try:
        recovery = Recovery(
            workspace=data.get("workspace"),
            phase=data["phase"],
            issue_number=int(data["issue"]) if "issue" in data else None,
            implementer=data.get("implementer"),
            reviewer=data.get("reviewer"),
            reviews_used=int(data.get("reviews_used", "0")),
            approved_sha=data.get("approved_sha"),
            approved_base_sha=data.get("approved_base_sha"),
            review_outcome=data.get("review_outcome"),
            agent_turn_start_sha=data.get("agent_turn_start_sha"),
            turn_sha=data.get("turn_sha"),
            turn_base_sha=data.get("turn_base_sha"),
            prepared_base_sha=data.get("prepared_base_sha"),
            correlation_id=data.get("correlation_id"),
            check_shell=data.get("check_shell"),
            check_result_path=data.get("check_result_path"),
        )
    except (KeyError, ValueError) as exc:
        raise WorkerFailure("checkpoint is incomplete or malformed") from exc
    if (
        checkpoint.name != recovery.phase
        or not 0 <= recovery.reviews_used <= MAX_REVIEWS
        or recovery.review_outcome not in {None, "approved", "no-change-policy"}
    ):
        raise WorkerFailure("checkpoint phase or review count is invalid")
    return recovery


def short_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:500]


def inspect_issue_pr_topology(
    github: GitHubRepository, issue: Issue, config: Config
) -> PullRequestState | None:
    try:
        pr = github.find_pr(
            head=issue.branch, base=config.integration_branch, state="OPEN"
        )
    except WorkerFailure as exc:
        emit_finding("github", short_error(exc), status="failed")
        raise
    message = (
        f"no open same-head PR for {issue.branch}"
        if pr is None
        else f"PR #{pr.number} {issue.branch} @ {pr.head_sha} -> "
        f"{config.integration_branch} @ {pr.base_sha}"
    )
    emit_finding("github", message)
    return pr


def inspect_integration_pr_topology(
    github: GitHubRepository, config: Config, recovery: Recovery
) -> PullRequestState | None:
    try:
        pr = github.find_pr(
            head=config.integration_branch, base=config.main_branch, state="OPEN"
        )
    except WorkerFailure as exc:
        emit_finding("github", short_error(exc), status="failed")
        raise
    message = (
        f"no open same-head PR for {config.integration_branch}"
        if pr is None
        else f"PR #{pr.number} {config.integration_branch} @ {pr.head_sha} -> "
        f"{config.main_branch} @ {pr.base_sha}"
    )
    emit_finding("github", message)
    if (
        pr is not None
        and not pr.is_draft
        and not (
            recovery.approved_sha == pr.head_sha
            and recovery.approved_base_sha == pr.base_sha
        )
    ):
        raise WorkerFailure(
            f"integration PR #{pr.number} is Ready without checkpointed approval "
            "for its exact head/base SHAs"
        )
    return pr


def ensure_workspace(
    runtime: PurpleMuxRuntime, config: Config, recovery: Recovery
) -> PurpleMuxCLIClient:
    if recovery.workspace is None:
        if recovery.phase == "workspace_create_pending":
            raise MutationOutcomeUnknown(
                "workspace creation may have completed; inspect the run-correlated "
                "workspace before any new mutation"
            )
        recovery.phase = "workspace_create_pending"
        recovery.checkpoint(config)
        workspace = runtime.create_workspace(
            CreateWorkspaceRequest(
                str(config.repo),
                f"{config.slug} {config.integration_branch}",
            )
        )
        recovery.workspace = workspace.id
        recovery.phase = "workspace_ready"
        recovery.checkpoint(config)
    emit_finding("runtime", f"PurpleMux workspace {recovery.workspace} is selected")
    return runtime.workspace(recovery.workspace)


def create_agent(client: PurpleMuxCLIClient, config: Config, *, name: str) -> str:
    return client.create_session(
        CreateSessionRequest(
            "codex",
            str(config.repo),
            "codex",
            name=name,
        )
    )


def run_turn(
    client: PurpleMuxCLIClient,
    tab: str,
    name: str,
    prompt: str,
    recovery: Recovery,
    config: Config,
    pending_phase: str,
    completed_phase: str,
    *,
    iteration: int | None = None,
) -> str:
    emit_step(
        name, "started", iteration=iteration, workspace=client.workspace_id, tab=tab
    )
    try:
        client.wait_until_ready(tab, READY_TIMEOUT)
        recovery.phase = pending_phase
        recovery.checkpoint(config)
        client.send_input(tab, prompt)
        client.wait_for_turn_completion(tab, TURN_TIMEOUT)
        result = client.read_result(tab)
        recovery.phase = completed_phase
        recovery.checkpoint(config)
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


def reopen_if_topology_drifted(
    pr: PullRequestState, recovery: Recovery, phase: str, config: Config
) -> bool:
    if (
        recovery.approved_sha == pr.head_sha
        and recovery.approved_base_sha == pr.base_sha
    ):
        return False
    recovery.approved_sha = recovery.approved_base_sha = None
    recovery.review_outcome = None
    recovery.turn_sha = recovery.turn_base_sha = None
    recovery.phase = phase
    recovery.checkpoint(config)
    return True


def require_reviewed_topology(
    github: GitHubRepository, pr: PullRequestState, recovery: Recovery
) -> PullRequestState:
    if (
        recovery.approved_sha is None
        or recovery.approved_base_sha is None
        or recovery.review_outcome not in {"approved", "no-change-policy"}
    ):
        raise WorkerFailure("review checkpoint lacks an accepted exact topology")
    return github.require_pr(
        number=pr.number,
        head=pr.head_branch,
        base=pr.base_branch,
        state="OPEN",
        expected_head_sha=recovery.approved_sha,
        expected_base_sha=recovery.approved_base_sha,
    )


def ensure_issue_pr(
    repo: GitRepository,
    github: GitHubRepository,
    issue: Issue,
    config: Config,
    recovery: Recovery,
    number: int | None = None,
) -> PullRequestState:
    local = repo.require_current_branch(issue.branch)
    if local.local_sha is None:
        raise WorkerFailure(f"local branch {issue.branch!r} does not exist")
    feature = repo.ensure_pushed(issue.branch, expected_local_sha=local.local_sha)
    assert feature.remote_sha is not None
    pr = github.find_pr(head=issue.branch, base=config.integration_branch, state="OPEN")
    if pr is None:
        if recovery.prepared_base_sha is None:
            raise WorkerFailure("prepared Issue checkpoint lacks its base SHA")
        recovery.phase = "issue_pr_create_pending"
        recovery.correlation_id = run_correlation(f"issue-{issue.number}-pr")
        recovery.checkpoint(config)
        pr = github.create_draft_pr(
            head=issue.branch,
            base=config.integration_branch,
            expected_head_sha=feature.remote_sha,
            expected_base_sha=recovery.prepared_base_sha,
            title=f"Issue #{issue.number}",
            body=f"Sequential implementation of Issue #{issue.number}.",
            correlation_id=recovery.correlation_id,
        )
    accepted_ready = (
        not pr.is_draft
        and recovery.review_outcome in {"approved", "no-change-policy"}
        and recovery.approved_sha == feature.remote_sha
        and recovery.approved_base_sha == recovery.prepared_base_sha
    )
    pr = github.require_pr(
        number=number if number is not None else pr.number,
        head=issue.branch,
        base=config.integration_branch,
        state="OPEN",
        expected_head_sha=feature.remote_sha,
        expected_base_sha=recovery.prepared_base_sha,
        draft=False if accepted_ready else True,
    )
    recovery.phase = "issue_delivery_done"
    recovery.checkpoint(config)
    emit_finding(
        "git", f"{issue.branch} pushed at {feature.remote_sha}; base {pr.base_sha}"
    )
    return pr


def require_agent_result(
    repo: GitRepository,
    client: PurpleMuxCLIClient,
    tab: str,
    issue: Issue,
    recovery: Recovery,
    config: Config,
    *,
    allow_unchanged: bool,
    pending_phase: str,
    completed_phase: str,
    iteration: int | None = None,
) -> tuple[str, bool]:
    if recovery.agent_turn_start_sha is None:
        raise WorkerFailure("agent turn checkpoint lacks its starting commit")
    repo.require_current_branch(issue.branch)
    if repo.inspect_worktree().dirty:
        run_turn(
            client,
            tab,
            f"Issue #{issue.number} postcondition recovery",
            f"""The worktree for {issue.branch} is dirty. Commit every intended
change from your completed turn and leave the working tree clean. Do not reset,
stash, clean, discard, push, create a PR, merge, or start another review.""",
            recovery,
            config,
            pending_phase,
            completed_phase,
            iteration=iteration,
        )
    result = repo.require_committed_result(
        issue.branch,
        previous_sha=recovery.agent_turn_start_sha,
        allow_unchanged=allow_unchanged,
    )
    assert result.local_sha is not None
    changed = result.local_sha != recovery.agent_turn_start_sha
    emit_finding(
        "git",
        f"{issue.branch} is clean at committed result {result.local_sha}",
    )
    return result.local_sha, changed


def run_final_checks(
    client: PurpleMuxCLIClient, config: Config, recovery: Recovery
) -> None:
    if recovery.phase in {
        "integration_checks_start_pending",
        "integration_checks_create_pending",
    }:
        raise MutationOutcomeUnknown(
            "final-check shell creation may have completed; inspect its saved "
            "correlation before any new mutation"
        )
    if recovery.phase in {
        "integration_checks_running",
        "integration_checks_complete",
    }:
        if recovery.check_shell is None or recovery.check_result_path is None:
            raise WorkerFailure("final-check checkpoint lacks shell identity")
        shell = recovery.check_shell
        client.resume_shell(shell, recovery.check_result_path, cwd=str(config.repo))
    elif recovery.phase not in {
        "integration_checks_complete",
    }:
        recovery.phase = "integration_checks_create_pending"
        recovery.checkpoint(config)

        def shell_created(tab: str, result_path: str) -> None:
            recovery.check_shell = tab
            recovery.check_result_path = result_path
            recovery.phase = "integration_checks_running"
            recovery.checkpoint(config)

        shell = client.start_shell(
            ShellCommandRequest(
                config.check_command,
                str(config.repo),
                "Final whole-version checks",
            ),
            on_created=shell_created,
        )
        if shell != recovery.check_shell:
            raise MutationOutcomeUnknown(
                "final-check shell identity changed after start"
            )

    if recovery.phase == "integration_checks_running":
        assert recovery.check_shell is not None
        client.wait_for_shell_completion(recovery.check_shell, SHELL_TIMEOUT)
        result = client.read_shell_result(recovery.check_shell)
        if result.exit_code != 0:
            failure = result.failure_message("final whole-version checks")
            emit_step(
                "final whole-version checks",
                "failed",
                error=failure,
                workspace=client.workspace_id,
                tab=recovery.check_shell,
            )
            raise WorkerFailure(failure)
        recovery.phase = "integration_checks_complete"
        recovery.checkpoint(config)


def prepare_issue(
    repo: GitRepository,
    github: GitHubRepository,
    issue: Issue,
    config: Config,
    recovery: Recovery,
) -> PullRequestState | None:
    open_pr = inspect_issue_pr_topology(github, issue, config)
    merged = github.find_pr(
        head=issue.branch, base=config.integration_branch, state="MERGED"
    )
    if merged is not None:
        if open_pr is not None:
            raise WorkerFailure("merged Issue also has an open same-head PR")
        emit_finding(
            "github", f"Issue #{issue.number} already merged as PR #{merged.number}"
        )
        return merged
    inspect_issue_pr_topology(github, issue, config)
    worktree = repo.inspect_worktree()
    resumed = recovery.issue_number == issue.number and recovery.phase.startswith(
        "issue_"
    )
    if resumed:
        if recovery.prepared_base_sha is None:
            raise WorkerFailure("prepared Issue checkpoint lacks its base SHA")
        repo.require_current_branch(issue.branch)
        prepared = repo.inspect_feature_preparation(
            issue.branch,
            base=config.integration_branch,
            expected_base_sha=recovery.prepared_base_sha,
        )
        if prepared.base_is_ancestor is not True:
            raise WorkerFailure(
                f"resumed branch {issue.branch} does not contain checkpointed base "
                f"{recovery.prepared_base_sha}"
            )
        emit_finding(
            "git",
            f"resumed {issue.branch} still contains {config.integration_branch} "
            f"@ {recovery.prepared_base_sha}",
        )
    elif worktree.dirty:
        raise WorkerFailure("dirty worktree is not an active prepared Issue resume")
    else:
        integration = repo.synchronize_branch(config.integration_branch)
        assert integration.remote_sha is not None
        feature = repo.prepare_feature_branch(
            issue.branch,
            base=config.integration_branch,
            expected_base_sha=integration.remote_sha,
        )
        recovery.prepared_base_sha = integration.remote_sha
        emit_finding(
            "git",
            f"{feature.name} contains {config.integration_branch} "
            f"@ {integration.remote_sha}",
        )
    return open_pr


def issue_prompts(issue: Issue, config: Config) -> tuple[str, str]:
    implementation = f"""Implement Issue #{issue.number} in {config.slug} on the existing
branch {issue.branch}, based on {config.integration_branch}. Read the Issue with gh.
Edit, test, and commit the result. Leave the working tree clean. You may push and
create one Draft PR to {config.integration_branch}, but the Workflow will safely
complete either omitted delivery step. Never reset, force-push, merge, or target
{config.main_branch}. Return a concise summary."""
    review = f"""Independently review Issue #{issue.number} and its PR from
{issue.branch} to {config.integration_branch}. Do not mutate files or PR state.
Return APPROVED or CHANGES_REQUESTED first, followed by actionable findings."""
    return implementation, review


def process_issue(
    issue: Issue,
    config: Config,
    runtime: PurpleMuxRuntime,
    repo: GitRepository,
    github: GitHubRepository,
    recovery: Recovery,
) -> None:
    if recovery.phase.endswith("_create_pending"):
        raise MutationOutcomeUnknown(
            "a prior workspace/session creation may have completed; inspect its "
            "saved identity and do not retry"
        )
    if (
        recovery.phase.endswith("_turn_pending")
        and recovery.issue_number == issue.number
    ):
        raise MutationOutcomeUnknown(
            "a prior agent turn may have been sent; do not resend"
        )
    existing = prepare_issue(repo, github, issue, config, recovery)
    if existing is not None and existing.state == "MERGED":
        print(
            f"Skipping already-merged Issue #{issue.number}: {existing.url}", flush=True
        )
        return
    inspect_issue_pr_topology(github, issue, config)
    client = ensure_workspace(runtime, config, recovery)
    if recovery.issue_number != issue.number:
        recovery.issue_number = issue.number
        recovery.phase = "issue_prepared"
        recovery.implementer = recovery.reviewer = None
        recovery.reviews_used = 0
        recovery.approved_sha = recovery.approved_base_sha = None
        recovery.review_outcome = None
        recovery.agent_turn_start_sha = None
        recovery.checkpoint(config)
    if recovery.implementer is None:
        inspect_issue_pr_topology(github, issue, config)
        recovery.phase = "issue_implementer_create_pending"
        recovery.checkpoint(config)
        recovery.implementer = create_agent(
            client,
            config,
            name=f"Issue {issue.number} implementer",
        )
        recovery.phase = "issue_implementer_ready"
        recovery.checkpoint(config)
    if recovery.reviewer is None:
        inspect_issue_pr_topology(github, issue, config)
        recovery.phase = "issue_reviewer_create_pending"
        recovery.checkpoint(config)
        recovery.reviewer = create_agent(
            client,
            config,
            name=f"Issue {issue.number} reviewer",
        )
        recovery.phase = "issue_sessions_ready"
        recovery.checkpoint(config)
    assert recovery.implementer and recovery.reviewer
    implementation_prompt, review_prompt = issue_prompts(issue, config)
    if recovery.phase in {"issue_implementer_ready", "issue_sessions_ready"}:
        start = repo.require_current_branch(issue.branch)
        if start.local_sha is None:
            raise WorkerFailure(f"local branch {issue.branch!r} does not exist")
        recovery.agent_turn_start_sha = start.local_sha
        recovery.checkpoint(config)
        run_turn(
            client,
            recovery.implementer,
            f"Issue #{issue.number} implementation",
            implementation_prompt,
            recovery,
            config,
            "issue_implementation_turn_pending",
            "issue_implementation_done",
        )
    if recovery.phase in {
        "issue_implementation_done",
        "issue_implementation_postcondition_done",
    }:
        implementation_sha, _ = require_agent_result(
            repo,
            client,
            recovery.implementer,
            issue,
            recovery,
            config,
            allow_unchanged=False,
            pending_phase="issue_implementation_postcondition_turn_pending",
            completed_phase="issue_implementation_postcondition_done",
        )
        recovery.phase = "issue_implementation_verified"
        recovery.checkpoint(config)
    else:
        implementation = repo.require_current_branch(issue.branch)
        if implementation.local_sha is None:
            raise WorkerFailure(f"local branch {issue.branch!r} does not exist")
        implementation_sha = implementation.local_sha

    if recovery.phase in {"issue_fix_done", "issue_fix_postcondition_done"}:
        fix_sha, changed = require_agent_result(
            repo,
            client,
            recovery.implementer,
            issue,
            recovery,
            config,
            allow_unchanged=True,
            pending_phase="issue_fix_postcondition_turn_pending",
            completed_phase="issue_fix_postcondition_done",
            iteration=recovery.reviews_used,
        )
        if not changed:
            if recovery.turn_sha is None or recovery.turn_base_sha is None:
                raise WorkerFailure("no-change continuation lacks reviewed topology")
            warning = (
                "WARN: reviewer requested changes, but implementer re-evaluated "
                "the finding and produced no code changes. Continuing by workflow "
                "policy."
            )
            print(warning, flush=True)
            emit_finding("git", warning, status="info")
            recovery.approved_sha = recovery.turn_sha
            recovery.approved_base_sha = recovery.turn_base_sha
            recovery.review_outcome = "no-change-policy"
            recovery.phase = "issue_no_change_policy"
        else:
            implementation_sha = fix_sha
            recovery.phase = "issue_fix_verified"
        recovery.checkpoint(config)
    pr = ensure_issue_pr(
        repo,
        github,
        issue,
        config,
        recovery,
        existing.number if existing else None,
    )
    if pr.head_sha != implementation_sha:
        raise WorkerFailure("delivered PR head does not match the committed result")
    approved = recovery.review_outcome in {
        "approved",
        "no-change-policy",
    } and not reopen_if_topology_drifted(pr, recovery, "issue_delivery_done", config)
    while not approved and recovery.reviews_used < MAX_REVIEWS:
        review_number = recovery.reviews_used + 1
        pr = ensure_issue_pr(repo, github, issue, config, recovery, pr.number)
        recovery.turn_sha, recovery.turn_base_sha = pr.head_sha, pr.base_sha
        result = run_turn(
            client,
            recovery.reviewer,
            f"Issue #{issue.number} review",
            f"{review_prompt}\nReview head {pr.head_sha} against base {pr.base_sha}.",
            recovery,
            config,
            "issue_review_turn_pending",
            "issue_review_turn_done",
            iteration=review_number,
        )
        current = github.require_pr(
            number=pr.number,
            head=issue.branch,
            base=config.integration_branch,
            state="OPEN",
            expected_head_sha=pr.head_sha,
            expected_base_sha=pr.base_sha,
        )
        if decision(result) == "APPROVED":
            recovery.reviews_used = review_number
            recovery.approved_sha, recovery.approved_base_sha = (
                current.head_sha,
                current.base_sha,
            )
            recovery.review_outcome = "approved"
            recovery.phase = "issue_approved"
            recovery.checkpoint(config)
            approved = True
            break
        if review_number == MAX_REVIEWS:
            break
        recovery.agent_turn_start_sha = current.head_sha
        recovery.reviews_used = review_number
        recovery.checkpoint(config)
        run_turn(
            client,
            recovery.implementer,
            f"Issue #{issue.number} fixes",
            f"""Re-evaluate every finding below. If changes are warranted, fix,
test, commit, and leave the working tree clean. Push is optional because the
Workflow completes delivery. If no change is warranted, leave the tree clean
and explain why; do not create an empty commit.\n\n{result}""",
            recovery,
            config,
            "issue_fix_turn_pending",
            "issue_fix_done",
            iteration=review_number,
        )
        fix_sha, changed = require_agent_result(
            repo,
            client,
            recovery.implementer,
            issue,
            recovery,
            config,
            allow_unchanged=True,
            pending_phase="issue_fix_postcondition_turn_pending",
            completed_phase="issue_fix_postcondition_done",
            iteration=review_number,
        )
        recovery.approved_sha = recovery.approved_base_sha = None
        recovery.review_outcome = None
        if not changed:
            warning = (
                "WARN: reviewer requested changes, but implementer re-evaluated "
                "the finding and produced no code changes. Continuing by workflow "
                "policy."
            )
            print(warning, flush=True)
            emit_finding("git", warning, status="info")
            recovery.approved_sha = current.head_sha
            recovery.approved_base_sha = current.base_sha
            recovery.review_outcome = "no-change-policy"
            recovery.phase = "issue_no_change_policy"
            recovery.checkpoint(config)
            approved = True
            break
        if fix_sha == current.head_sha:
            raise WorkerFailure("fix commit did not advance the PR head")
        recovery.phase = "issue_fix_verified"
        recovery.checkpoint(config)
    if not approved:
        raise WorkerFailure(f"Issue #{issue.number} ended without approval")
    pr = require_reviewed_topology(github, pr, recovery)
    if pr.is_draft:
        pr = github.set_draft(
            pr.number,
            draft=False,
            expected_head=issue.branch,
            expected_head_sha=recovery.approved_sha or "",
            expected_base=config.integration_branch,
            expected_base_sha=recovery.approved_base_sha or "",
        )
        recovery.phase = "issue_ready"
        recovery.checkpoint(config)
    merged = github.merge_pr(
        pr.number,
        expected_head=issue.branch,
        expected_head_sha=recovery.approved_sha or "",
        expected_base=config.integration_branch,
        expected_base_sha=recovery.approved_base_sha or "",
    )
    repo.advance_after_merge(
        config.integration_branch,
        previous_sha=recovery.approved_base_sha or "",
        merge_commit_sha=merged.merge_commit_sha,
        required_commit_sha=recovery.approved_sha or "",
    )
    print(f"Merged approved Issue #{issue.number} PR: {merged.pr.url}", flush=True)
    workspace = recovery.workspace
    recovery.__dict__.update(Recovery(workspace=workspace).__dict__)
    recovery.phase = "workspace_ready"
    recovery.checkpoint(config)


def integration_review(
    config: Config,
    runtime: PurpleMuxRuntime,
    repo: GitRepository,
    github: GitHubRepository,
    recovery: Recovery,
) -> PullRequestState:
    if recovery.phase in {
        "integration_review_turn_pending",
        "integration_fix_turn_pending",
    }:
        raise MutationOutcomeUnknown(
            "a prior integration agent turn may have been sent; do not resend "
            "until its exact outcome is reconciled"
        )
    if recovery.phase == "integration_checks_start_pending":
        raise MutationOutcomeUnknown(
            "final-check shell creation may have completed under the prior "
            "checkpoint; inspect before any new mutation"
        )
    if recovery.phase.endswith("_create_pending"):
        raise MutationOutcomeUnknown(
            "a prior integration workspace/session creation may have completed; "
            "inspect its saved identity and do not retry"
        )
    pr = inspect_integration_pr_topology(github, config, recovery)
    if recovery.phase in {
        "integration_fix_done",
        "integration_fix_postcondition_done",
    }:
        if pr is None or recovery.implementer is None:
            raise WorkerFailure("integration fix recovery lacks PR or fixer identity")
        if recovery.agent_turn_start_sha is None:
            raise WorkerFailure("integration fix lacks its starting commit")
        client = ensure_workspace(runtime, config, recovery)
        repo.require_current_branch(config.integration_branch)
        if repo.inspect_worktree().dirty:
            run_turn(
                client,
                recovery.implementer,
                "Whole-version postcondition recovery",
                f"""The worktree for {config.integration_branch} is dirty. Commit
every intended change and leave the working tree clean. Do not reset, stash,
clean, discard, push, change PR state, merge, or start another review.""",
                recovery,
                config,
                "integration_fix_postcondition_turn_pending",
                "integration_fix_postcondition_done",
                iteration=recovery.reviews_used,
            )
        result = repo.require_committed_result(
            config.integration_branch,
            previous_sha=recovery.agent_turn_start_sha,
            allow_unchanged=True,
        )
        assert result.local_sha is not None
        if result.local_sha == recovery.agent_turn_start_sha:
            warning = (
                "WARN: reviewer requested changes, but implementer re-evaluated "
                "the finding and produced no code changes. Continuing by workflow "
                "policy."
            )
            print(warning, flush=True)
            emit_finding("git", warning, status="info")
            recovery.approved_sha = pr.head_sha
            recovery.approved_base_sha = pr.base_sha
            recovery.review_outcome = "no-change-policy"
            recovery.phase = "integration_no_change_policy"
        else:
            repo.ensure_pushed(
                config.integration_branch, expected_local_sha=result.local_sha
            )
            recovery.approved_sha = recovery.approved_base_sha = None
            recovery.review_outcome = None
            recovery.phase = "integration_fix_verified"
        recovery.checkpoint(config)
    integration = repo.synchronize_branch(config.integration_branch)
    main = repo.inspect_branch(config.main_branch)
    if integration.remote_sha is None or main.remote_sha is None:
        raise WorkerFailure("integration or main remote branch is missing")
    pr = inspect_integration_pr_topology(github, config, recovery)
    if pr is None:
        correlation = recovery.correlation_id or run_correlation("integration-pr")
        recovery.correlation_id = correlation
        recovery.phase = "integration_pr_create_pending"
        recovery.checkpoint(config)
        pr = github.create_draft_pr(
            head=config.integration_branch,
            base=config.main_branch,
            expected_head_sha=integration.remote_sha,
            expected_base_sha=main.remote_sha,
            title=f"Integrate {config.integration_branch}",
            body="Sequential integration; Ready only after whole-version approval.",
            correlation_id=correlation,
        )
    emit_finding(
        "github", f"integration PR #{pr.number}: {pr.head_sha} -> {pr.base_sha}"
    )
    inspect_integration_pr_topology(github, config, recovery)
    client = ensure_workspace(runtime, config, recovery)
    if recovery.phase.startswith("integration_checks_") and (
        recovery.implementer is None or recovery.reviewer is None
    ):
        raise WorkerFailure(
            "final-check recovery lacks the original agent tab identities"
        )
    if recovery.implementer is None:
        inspect_integration_pr_topology(github, config, recovery)
        recovery.phase = "integration_fixer_create_pending"
        recovery.checkpoint(config)
        recovery.implementer = create_agent(
            client,
            config,
            name="Whole-version fixer",
        )
        recovery.phase = "integration_fixer_ready"
        recovery.checkpoint(config)
    if recovery.reviewer is None:
        inspect_integration_pr_topology(github, config, recovery)
        recovery.phase = "integration_reviewer_create_pending"
        recovery.checkpoint(config)
        recovery.reviewer = create_agent(
            client,
            config,
            name="Whole-version reviewer",
        )
        recovery.phase = "integration_sessions_ready"
        recovery.checkpoint(config)
    assert recovery.implementer and recovery.reviewer
    if recovery.phase.startswith("integration_checks_"):
        if (
            recovery.approved_sha != pr.head_sha
            or recovery.approved_base_sha != pr.base_sha
        ):
            raise MutationOutcomeUnknown(
                "approved topology changed while the final-check shell may be active"
            )
        approved = True
    else:
        approved = recovery.review_outcome in {
            "approved",
            "no-change-policy",
        } and not reopen_if_topology_drifted(
            pr, recovery, "integration_fix_verified", config
        )
    while not approved and recovery.reviews_used < MAX_REVIEWS:
        review_number = recovery.reviews_used + 1
        pr = github.require_pr(
            number=pr.number,
            head=config.integration_branch,
            base=config.main_branch,
            state="OPEN",
            expected_head_sha=integration.remote_sha,
            expected_base_sha=main.remote_sha,
        )
        result = run_turn(
            client,
            recovery.reviewer,
            "Whole-version review",
            f"Review exact head {pr.head_sha} against main {pr.base_sha}. Return "
            "APPROVED or CHANGES_REQUESTED first; do not mutate anything.",
            recovery,
            config,
            "integration_review_turn_pending",
            "integration_review_turn_done",
            iteration=review_number,
        )
        github.require_pr(
            number=pr.number,
            head=config.integration_branch,
            base=config.main_branch,
            state="OPEN",
            expected_head_sha=pr.head_sha,
            expected_base_sha=pr.base_sha,
        )
        if decision(result) == "APPROVED":
            recovery.reviews_used = review_number
            recovery.approved_sha, recovery.approved_base_sha = pr.head_sha, pr.base_sha
            recovery.review_outcome = "approved"
            recovery.phase = "integration_approved"
            recovery.checkpoint(config)
            approved = True
            break
        if review_number == MAX_REVIEWS:
            break
        recovery.agent_turn_start_sha = pr.head_sha
        recovery.reviews_used = review_number
        recovery.checkpoint(config)
        run_turn(
            client,
            recovery.implementer,
            "Whole-version fixes",
            f"""Re-evaluate every finding. If changes are warranted, fix, test,
commit, and leave the working tree clean; push is optional. If no change is
warranted, leave the tree clean and explain why without an empty commit.\n\n{result}""",
            recovery,
            config,
            "integration_fix_turn_pending",
            "integration_fix_done",
            iteration=review_number,
        )
        repo.require_current_branch(config.integration_branch)
        if repo.inspect_worktree().dirty:
            run_turn(
                client,
                recovery.implementer,
                "Whole-version postcondition recovery",
                f"""The worktree for {config.integration_branch} is dirty. Commit
every intended change and leave the working tree clean. Do not reset, stash,
clean, discard, push, change PR state, merge, or start another review.""",
                recovery,
                config,
                "integration_fix_postcondition_turn_pending",
                "integration_fix_postcondition_done",
                iteration=review_number,
            )
        fixed = repo.require_committed_result(
            config.integration_branch,
            previous_sha=recovery.agent_turn_start_sha,
            allow_unchanged=True,
        )
        assert fixed.local_sha is not None
        recovery.approved_sha = recovery.approved_base_sha = None
        recovery.review_outcome = None
        if fixed.local_sha == recovery.agent_turn_start_sha:
            warning = (
                "WARN: reviewer requested changes, but implementer re-evaluated "
                "the finding and produced no code changes. Continuing by workflow "
                "policy."
            )
            print(warning, flush=True)
            emit_finding("git", warning, status="info")
            recovery.approved_sha = pr.head_sha
            recovery.approved_base_sha = pr.base_sha
            recovery.review_outcome = "no-change-policy"
            recovery.phase = "integration_no_change_policy"
            recovery.checkpoint(config)
            approved = True
            break
        integration = repo.ensure_pushed(
            config.integration_branch, expected_local_sha=fixed.local_sha
        )
        assert integration.remote_sha is not None
        recovery.phase = "integration_fix_verified"
        recovery.checkpoint(config)
    if not approved:
        raise WorkerFailure("whole-version review ended without approval")
    pr = require_reviewed_topology(github, pr, recovery)
    run_final_checks(client, config, recovery)
    pr = github.set_draft(
        pr.number,
        draft=False,
        expected_head=config.integration_branch,
        expected_head_sha=recovery.approved_sha or "",
        expected_base=config.main_branch,
        expected_base_sha=recovery.approved_base_sha or "",
    )
    recovery.phase = "integration_ready"
    recovery.checkpoint(config)
    return pr


def main() -> None:
    config = parse_args()
    recovery = load_recovery(config, resume_checkpoint())
    if recovery.phase == "workspace_create_pending":
        raise MutationOutcomeUnknown(
            "workspace creation may have completed; reconcile the run-correlated "
            "workspace before any further mutation"
        )
    runtime = PurpleMuxRuntime(
        command_timeout_seconds=COMMAND_TIMEOUT, owned_by_run=True
    )
    repo = GitRepository.open(
        config.repo,
        expected_github_slug=config.slug,
        command_timeout_seconds=COMMAND_TIMEOUT,
    )
    github = GitHubRepository.open(config.slug, command_timeout_seconds=COMMAND_TIMEOUT)
    for issue in config.issues:
        process_issue(issue, config, runtime, repo, github, recovery)
    ready = integration_review(config, runtime, repo, github, recovery)
    print(f"Whole-version PR is Ready (not merged): {ready.url}", flush=True)


if __name__ == "__main__":
    main()
