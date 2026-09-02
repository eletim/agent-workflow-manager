#!/usr/bin/env python3
"""Canonical plain-Python sequential version-development workflow.

AWM owns structural Git/GitHub/runtime safety. Agents own edits, project checks,
commits, and pushes. Dry Run executes this program to its first mutation.
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Sequence
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
    TerminalSessionError,
    WorkerFailure,
    emit_finding,
    emit_step,
    resume_checkpoint,
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
    turn_sha: str | None = None
    turn_base_sha: str | None = None
    prepared_base_sha: str | None = None
    correlation_id: str | None = None

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
            "turn_sha": self.turn_sha,
            "turn_base_sha": self.turn_base_sha,
            "prepared_base_sha": self.prepared_base_sha,
            "correlation_id": self.correlation_id,
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
            turn_sha=data.get("turn_sha"),
            turn_base_sha=data.get("turn_base_sha"),
            prepared_base_sha=data.get("prepared_base_sha"),
            correlation_id=data.get("correlation_id"),
        )
    except (KeyError, ValueError) as exc:
        raise WorkerFailure("checkpoint is incomplete or malformed") from exc
    if (
        checkpoint.name != recovery.phase
        or not 0 <= recovery.reviews_used <= MAX_REVIEWS
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
                "workspace creation may have completed; inspect its saved correlation"
            )
        recovery.phase = "workspace_create_pending"
        recovery.correlation_id = f"workspace-{config.signature}"
        recovery.checkpoint(config)
        workspace = runtime.create_workspace(
            CreateWorkspaceRequest(
                str(config.repo),
                f"{config.slug} {config.integration_branch}",
                recovery.correlation_id,
            )
        )
        recovery.workspace = workspace.id
        recovery.phase = "workspace_ready"
        recovery.checkpoint(config)
    emit_finding("runtime", f"PurpleMux workspace {recovery.workspace} is selected")
    return runtime.workspace(recovery.workspace)


def create_agent(
    client: PurpleMuxCLIClient, config: Config, *, name: str, correlation_id: str
) -> str:
    return client.create_session(
        CreateSessionRequest(
            "codex",
            str(config.repo),
            "codex",
            name=name,
            correlation_id=correlation_id,
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


def close_tabs(client: PurpleMuxCLIClient, tabs: Sequence[str | None]) -> None:
    errors: list[str] = []
    for tab in reversed(tuple(dict.fromkeys(item for item in tabs if item))):
        try:
            client.close_session(tab)
        except TerminalSessionError as exc:
            errors.append(f"{tab}: {exc}")
    if errors:
        raise WorkerFailure("session cleanup failed: " + "; ".join(errors))


def reopen_if_topology_drifted(
    pr: PullRequestState, recovery: Recovery, phase: str, config: Config
) -> bool:
    if (
        recovery.approved_sha == pr.head_sha
        and recovery.approved_base_sha == pr.base_sha
    ):
        return False
    recovery.approved_sha = recovery.approved_base_sha = None
    recovery.turn_sha = recovery.turn_base_sha = None
    recovery.phase = phase
    recovery.checkpoint(config)
    return True


def require_reviewed_topology(
    github: GitHubRepository, pr: PullRequestState, recovery: Recovery
) -> PullRequestState:
    if recovery.approved_sha is None or recovery.approved_base_sha is None:
        raise WorkerFailure("approval checkpoint lacks reviewed head/base SHAs")
    return github.require_pr(
        number=pr.number,
        head=pr.head_branch,
        base=pr.base_branch,
        state="OPEN",
        expected_head_sha=recovery.approved_sha,
        expected_base_sha=recovery.approved_base_sha,
    )


def require_issue_pr(
    repo: GitRepository,
    github: GitHubRepository,
    issue: Issue,
    config: Config,
    number: int | None = None,
) -> PullRequestState:
    feature = repo.require_pushed(issue.branch)
    assert feature.remote_sha is not None
    pr = github.require_pr(
        number=number,
        head=issue.branch,
        base=config.integration_branch,
        state="OPEN",
        expected_head_sha=feature.remote_sha,
    )
    emit_finding(
        "git", f"{issue.branch} pushed at {feature.remote_sha}; base {pr.base_sha}"
    )
    return pr


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
    if worktree.dirty:
        if not resumed or recovery.prepared_base_sha is None:
            raise WorkerFailure("dirty worktree is not an active prepared Issue resume")
        repo.require_current_branch(issue.branch)
        repo.require_contains(issue.branch, recovery.prepared_base_sha)
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
Edit, test, commit, and push. Create exactly one Draft PR to
{config.integration_branch}. Never reset, force-push, merge, or target
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
        recovery.checkpoint(config)
    if recovery.implementer is None:
        inspect_issue_pr_topology(github, issue, config)
        recovery.phase = "issue_implementer_create_pending"
        recovery.checkpoint(config)
        recovery.implementer = create_agent(
            client,
            config,
            name=f"Issue {issue.number} implementer",
            correlation_id=f"issue-{issue.number}-implementer-{config.signature}",
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
            correlation_id=f"issue-{issue.number}-reviewer-{config.signature}",
        )
        recovery.phase = "issue_sessions_ready"
        recovery.checkpoint(config)
    assert recovery.implementer and recovery.reviewer
    implementation_prompt, review_prompt = issue_prompts(issue, config)
    if recovery.phase in {"issue_implementer_ready", "issue_sessions_ready"}:
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
    pr = require_issue_pr(
        repo, github, issue, config, existing.number if existing else None
    )
    approved = recovery.phase == "issue_approved" and not reopen_if_topology_drifted(
        pr, recovery, "issue_fix_done", config
    )
    while not approved and recovery.reviews_used < MAX_REVIEWS:
        review_number = recovery.reviews_used + 1
        pr = require_issue_pr(repo, github, issue, config, pr.number)
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
            recovery.phase = "issue_approved"
            recovery.checkpoint(config)
            approved = True
            break
        if review_number == MAX_REVIEWS:
            break
        run_turn(
            client,
            recovery.implementer,
            f"Issue #{issue.number} fixes",
            f"Fix all findings, test, commit, and push.\n\n{result}",
            recovery,
            config,
            "issue_fix_turn_pending",
            "issue_fix_done",
            iteration=review_number,
        )
        recovery.reviews_used = review_number
        recovery.approved_sha = recovery.approved_base_sha = None
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
    close_tabs(client, (recovery.implementer, recovery.reviewer))
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
    if recovery.phase.endswith("_create_pending"):
        raise MutationOutcomeUnknown(
            "a prior integration workspace/session creation may have completed; "
            "inspect its saved identity and do not retry"
        )
    pr = inspect_integration_pr_topology(github, config, recovery)
    integration = repo.synchronize_branch(config.integration_branch)
    main = repo.inspect_branch(config.main_branch)
    if integration.remote_sha is None or main.remote_sha is None:
        raise WorkerFailure("integration or main remote branch is missing")
    pr = inspect_integration_pr_topology(github, config, recovery)
    if pr is None:
        correlation = recovery.correlation_id or f"integration-{config.signature}"
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
    if recovery.implementer is None:
        inspect_integration_pr_topology(github, config, recovery)
        recovery.phase = "integration_fixer_create_pending"
        recovery.checkpoint(config)
        recovery.implementer = create_agent(
            client,
            config,
            name="Whole-version fixer",
            correlation_id=f"integration-fixer-{config.signature}",
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
            correlation_id=f"integration-reviewer-{config.signature}",
        )
        recovery.phase = "integration_sessions_ready"
        recovery.checkpoint(config)
    assert recovery.implementer and recovery.reviewer
    approved = (
        recovery.phase == "integration_approved"
        and not reopen_if_topology_drifted(pr, recovery, "integration_fix_done", config)
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
            recovery.phase = "integration_approved"
            recovery.checkpoint(config)
            approved = True
            break
        if review_number == MAX_REVIEWS:
            break
        run_turn(
            client,
            recovery.implementer,
            "Whole-version fixes",
            f"Fix findings, test, commit, and push.\n\n{result}",
            recovery,
            config,
            "integration_fix_turn_pending",
            "integration_fix_done",
            iteration=review_number,
        )
        integration = repo.require_pushed(config.integration_branch)
        assert integration.remote_sha is not None
        recovery.reviews_used = review_number
        recovery.approved_sha = recovery.approved_base_sha = None
        recovery.checkpoint(config)
    if not approved:
        raise WorkerFailure("whole-version review ended without approval")
    pr = require_reviewed_topology(github, pr, recovery)
    recovery.phase = "integration_checks_start_pending"
    recovery.checkpoint(config)
    shell = client.start_shell(
        ShellCommandRequest(
            config.check_command, str(config.repo), "Final whole-version checks"
        )
    )
    client.wait_for_shell_completion(shell, SHELL_TIMEOUT)
    if client.read_shell_result(shell).exit_code != 0:
        raise WorkerFailure("final whole-version checks failed")
    client.close_session(shell)
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
    close_tabs(client, (recovery.implementer, recovery.reviewer))
    return pr


def main() -> None:
    config = parse_args()
    recovery = load_recovery(config, resume_checkpoint())
    if recovery.phase == "workspace_create_pending":
        raise MutationOutcomeUnknown(
            "workspace creation may have completed; reconcile the saved correlation "
            "before any further mutation"
        )
    runtime = PurpleMuxRuntime(command_timeout_seconds=COMMAND_TIMEOUT)
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
