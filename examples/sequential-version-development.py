#!/usr/bin/env python3
"""Canonical sequential multi-Issue version-development workflow.

Adapt the command-line values and prompts to the target repository. Run this from
Agent Workflow Manager with the target repository selected as Working directory.
The script intentionally keeps orchestration in ordinary Python; GitHub is the
source of truth for branch and PR state, while checkpoints retain only enough
correlation data to reuse PurpleMux sessions after an explicit manual resume.

Equivalent command-line example (put each argument on its own line in the UI):
    uv run python examples/sequential-version-development.py --slug OWNER/REPOSITORY
      --integration-branch dev/v1.2.3 --issue 101:feature/issue-101
      --issue 102:feature/issue-102 --check-command "make lint && make test"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

from purplemux_client import (
    CreateSessionRequest,
    MutationOutcomeUnknown,
    PurpleMuxCLIClient,
    ResumeCheckpoint,
    ShellCommandRequest,
    TerminalSessionError,
    WorkerFailure,
    WorkerNeedsInput,
    emit_step,
    resume_checkpoint,
    save_checkpoint,
    suspend_run,
)

WORKFLOW_PREFLIGHT = {
    "commands": ["git", "gh", "purplemux"],
}

MAX_REVIEWS = 4
READY_TIMEOUT = 120
TURN_TIMEOUT = 3600
SHELL_TIMEOUT = 1800
COMMAND_TIMEOUT = 30


@dataclass(frozen=True)
class Issue:
    number: int
    branch: str

    @property
    def url_path(self) -> str:
        return f"issues/{self.number}"


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
            *(f"{issue.number}:{issue.branch}" for issue in self.issues),
        ]
        return hashlib.sha256("\0".join(values).encode()).hexdigest()[:16]


@dataclass
class Recovery:
    workspace: str
    phase: str = "workspace_ready"
    issue_number: int | None = None
    implementer: str | None = None
    reviewer: str | None = None
    reviews_used: int = 0
    approved_sha: str | None = None

    def checkpoint(self, config: Config) -> None:
        data = {
            "config": config.signature,
            "workspace": self.workspace,
            "phase": self.phase,
            "reviews_used": str(self.reviews_used),
        }
        if self.issue_number is not None:
            data["issue"] = str(self.issue_number)
        if self.implementer is not None:
            data["implementer"] = self.implementer
        if self.reviewer is not None:
            data["reviewer"] = self.reviewer
        if self.approved_sha is not None:
            data["approved_sha"] = self.approved_sha
        save_checkpoint(self.phase, data)


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Sequential Issue development with mandatory integration review."
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--slug", required=True, help="GitHub OWNER/REPOSITORY")
    parser.add_argument("--integration-branch", required=True, help="dev/vX.Y.Z")
    parser.add_argument("--main-branch", default="main")
    parser.add_argument(
        "--issue",
        action="append",
        required=True,
        metavar="NUMBER:BRANCH",
        help="repeat in the exact sequential implementation order",
    )
    parser.add_argument(
        "--check-command",
        required=True,
        help="quoted target-repository format/lint/typecheck/test command",
    )
    args = parser.parse_args()

    issues: list[Issue] = []
    for value in args.issue:
        number_text, separator, branch = value.partition(":")
        if not separator or not number_text.isdigit() or not branch.strip():
            parser.error(f"invalid --issue {value!r}; expected NUMBER:BRANCH")
        issues.append(Issue(int(number_text), branch.strip()))
    if len({issue.number for issue in issues}) != len(issues):
        parser.error("Issue numbers must be unique")
    if len({issue.branch for issue in issues}) != len(issues):
        parser.error("Issue branches must be unique")
    if args.integration_branch == args.main_branch:
        parser.error("the integration branch must differ from the main branch")
    if any(
        issue.branch in {args.integration_branch, args.main_branch} for issue in issues
    ):
        parser.error("every Issue branch must differ from integration and main")
    return Config(
        repo=args.repo.resolve(),
        slug=args.slug,
        integration_branch=args.integration_branch,
        main_branch=args.main_branch,
        issues=tuple(issues),
        check_command=args.check_command,
    )


def short_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:500]


def run_command(
    args: Sequence[str],
    *,
    cwd: Path,
    mutation: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        command = " ".join(args)
        if mutation:
            raise MutationOutcomeUnknown(
                f"{command} timed out; its outcome is unknown; inspect before resuming"
            ) from exc
        raise WorkerFailure(f"read-only command timed out: {command}") from exc
    except OSError as exc:
        raise WorkerFailure(f"could not execute {args[0]}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise WorkerFailure(f"{' '.join(args)} failed: {detail}")
    return completed


def read_text(args: Sequence[str], config: Config) -> str:
    return run_command(args, cwd=config.repo).stdout.strip()


def mutate(args: Sequence[str], config: Config) -> str:
    # A timed-out remote/local mutation is never replayed automatically. The last
    # checkpoint and retained tabs let an operator reconcile it before Resume.
    return run_command(args, cwd=config.repo, mutation=True).stdout.strip()


def gh_json(args: Sequence[str], config: Config) -> Any:
    output = read_text(["gh", *args, "--repo", config.slug], config)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise WorkerFailure(f"gh returned malformed JSON for {' '.join(args)}") from exc


def worktree_is_dirty(config: Config) -> bool:
    return bool(read_text(["git", "status", "--porcelain"], config))


def github_origin_slug(origin: str) -> str:
    path: str
    if origin.startswith("git@github.com:"):
        path = origin.removeprefix("git@github.com:")
    else:
        try:
            parsed = urlsplit(origin)
            if parsed.scheme == "https":
                valid = (
                    parsed.hostname == "github.com"
                    and parsed.username is None
                    and parsed.password is None
                    and parsed.port is None
                )
            elif parsed.scheme == "ssh":
                valid = (
                    parsed.hostname == "github.com"
                    and parsed.username == "git"
                    and parsed.password is None
                    and parsed.port is None
                )
            else:
                valid = False
        except ValueError as exc:
            raise WorkerFailure(f"invalid origin URL: {origin!r}") from exc
        if not valid or parsed.query or parsed.fragment:
            raise WorkerFailure(f"unsupported or non-GitHub origin URL: {origin!r}")
        path = parsed.path.removeprefix("/")
    path = path.removesuffix(".git")
    parts = path.split("/")
    if (
        len(parts) != 2
        or any(not part for part in parts)
        or any(
            not part.isascii()
            or not all(character.isalnum() or character in "-_." for character in part)
            for part in parts
        )
    ):
        raise WorkerFailure(f"origin has an invalid GitHub repository path: {origin!r}")
    return "/".join(parts)


def validate_repository(config: Config, checkpoint: ResumeCheckpoint | None) -> None:
    if not config.repo.is_dir():
        raise WorkerFailure(f"repository directory does not exist: {config.repo}")
    root = Path(read_text(["git", "rev-parse", "--show-toplevel"], config)).resolve()
    if root != config.repo:
        raise WorkerFailure(f"--repo must be the worktree root: expected {root}")
    dirty = worktree_is_dirty(config)
    active_resume = checkpoint is not None and (
        checkpoint.name.startswith("issue_")
        or checkpoint.name.startswith("integration_")
    )
    if dirty and not active_resume:
        raise WorkerFailure(
            "worktree must be clean unless resuming an active Issue/integration stage"
        )
    origin = read_text(["git", "remote", "get-url", "origin"], config)
    if github_origin_slug(origin).lower() != config.slug.lower():
        raise WorkerFailure(
            f"origin {origin!r} does not match configured repository {config.slug!r}"
        )
    read_text(["gh", "auth", "status", "--hostname", "github.com"], config)
    repository_output = read_text(
        ["gh", "repo", "view", config.slug, "--json", "nameWithOwner"], config
    )
    try:
        repository = json.loads(repository_output)
    except json.JSONDecodeError as exc:
        raise WorkerFailure("gh repo view returned malformed JSON") from exc
    if repository.get("nameWithOwner", "").lower() != config.slug.lower():
        raise WorkerFailure("gh resolved a different repository than --slug")
    mutate(["git", "fetch", "origin", config.integration_branch], config)
    read_text(
        ["git", "rev-parse", "--verify", f"origin/{config.integration_branch}"], config
    )


def create_workspace(config: Config) -> str:
    try:
        completed = subprocess.run(
            [
                "purplemux",
                "workspace",
                "create",
                "--cwd",
                str(config.repo),
                "--name",
                f"{config.slug} {config.integration_branch}",
            ],
            cwd=config.repo,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MutationOutcomeUnknown(
            "workspace creation timed out; list workspaces and reconcile before retrying"
        ) from exc
    except OSError as exc:
        raise WorkerFailure(f"could not execute PurpleMux: {exc}") from exc
    if completed.returncode != 0:
        raise WorkerFailure(
            "workspace creation failed: "
            + (completed.stderr.strip() or completed.stdout.strip() or "no output")
        )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MutationOutcomeUnknown(
            "workspace may have been created but its response was malformed"
        ) from exc
    workspace = data.get("id") if isinstance(data, dict) else None
    if not isinstance(workspace, str) or not workspace:
        raise MutationOutcomeUnknown("workspace creation returned no usable id")
    return workspace


def load_recovery(config: Config, checkpoint: ResumeCheckpoint | None) -> Recovery:
    if checkpoint is None:
        workspace = create_workspace(config)
        recovery = Recovery(workspace=workspace)
        recovery.checkpoint(config)
        return recovery
    data = checkpoint.data
    if data.get("config") != config.signature:
        raise WorkerFailure("resume arguments do not match the checkpointed workflow")
    try:
        issue_number = int(data["issue"]) if "issue" in data else None
        reviews_used = int(data.get("reviews_used", "0"))
        recovery = Recovery(
            workspace=data["workspace"],
            phase=data["phase"],
            issue_number=issue_number,
            implementer=data.get("implementer"),
            reviewer=data.get("reviewer"),
            reviews_used=reviews_used,
            approved_sha=data.get("approved_sha"),
        )
    except (KeyError, ValueError) as exc:
        raise WorkerFailure("checkpoint is incomplete or malformed") from exc
    if checkpoint.name != recovery.phase or not 0 <= reviews_used <= MAX_REVIEWS:
        raise WorkerFailure("checkpoint phase or review count is invalid")
    return recovery


def branch_exists(ref: str, config: Config) -> bool:
    completed = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", ref],
        cwd=config.repo,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
        check=False,
    )
    if completed.returncode not in (0, 1):
        raise WorkerFailure(f"could not inspect Git ref {ref}")
    return completed.returncode == 0


def require_ancestor(ancestor: str, descendant: str, config: Config) -> None:
    try:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=config.repo,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkerFailure("Git ancestry check timed out") from exc
    except OSError as exc:
        raise WorkerFailure(f"could not inspect Git ancestry: {exc}") from exc
    if completed.returncode == 1:
        raise WorkerFailure(
            f"{descendant} is not based on the latest {ancestor}; reconcile the "
            "branch explicitly without resetting or force-pushing"
        )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "no stderr"
        raise WorkerFailure(f"could not inspect Git ancestry: {detail}")


def preserve_dirty_resume_branch(
    expected_branch: str, stage: str, config: Config
) -> bool:
    if not worktree_is_dirty(config):
        return False
    current_branch = read_text(["git", "branch", "--show-current"], config)
    if current_branch != expected_branch:
        raise WorkerFailure(
            f"resumed {stage} work is on {current_branch!r}, expected "
            f"{expected_branch!r}; reconcile it without resetting"
        )
    # Preserve the partial tree exactly. Refresh remote state, but do not switch or
    # merge because either operation could move edits onto a different commit.
    mutate(["git", "fetch", "origin"], config)
    return True


def switch_to_integration(config: Config) -> None:
    mutate(["git", "fetch", "origin", config.integration_branch], config)
    local_ref = f"refs/heads/{config.integration_branch}"
    if branch_exists(local_ref, config):
        mutate(["git", "switch", config.integration_branch], config)
    else:
        mutate(
            [
                "git",
                "switch",
                "--track",
                "-c",
                config.integration_branch,
                f"origin/{config.integration_branch}",
            ],
            config,
        )
    mutate(["git", "merge", "--ff-only", f"origin/{config.integration_branch}"], config)
    local = read_text(["git", "rev-parse", "HEAD"], config)
    remote = read_text(
        ["git", "rev-parse", f"origin/{config.integration_branch}"], config
    )
    if local != remote:
        raise WorkerFailure(
            "local integration branch is ahead of origin; reconcile it explicitly"
        )


def issue_prs(issue: Issue, config: Config, state: str = "all") -> list[dict[str, Any]]:
    data = gh_json(
        [
            "pr",
            "list",
            "--head",
            issue.branch,
            "--base",
            config.integration_branch,
            "--state",
            state,
            "--limit",
            "20",
            "--json",
            "number,url,state,isDraft,headRefName,headRefOid,baseRefName,mergedAt",
        ],
        config,
    )
    if not isinstance(data, list):
        raise WorkerFailure("gh PR list returned an unexpected value")
    return data


def merged_issue_pr(issue: Issue, config: Config) -> dict[str, Any] | None:
    merged = [pr for pr in issue_prs(issue, config, "merged") if pr.get("mergedAt")]
    if len(merged) > 1:
        raise WorkerFailure(f"Issue branch {issue.branch} has multiple merged PRs")
    return merged[0] if merged else None


def prepare_issue_branch(issue: Issue, config: Config) -> None:
    if issue.branch in {config.integration_branch, config.main_branch}:
        raise WorkerFailure("unsafe Issue branch configuration")
    switch_to_integration(config)
    mutate(["git", "fetch", "origin"], config)
    local_ref = f"refs/heads/{issue.branch}"
    remote_ref = f"refs/remotes/origin/{issue.branch}"
    integration_ref = f"origin/{config.integration_branch}"
    if branch_exists(local_ref, config):
        mutate(["git", "switch", issue.branch], config)
        if branch_exists(remote_ref, config):
            # Fast-forward only. Local commits are preserved for an implementer to
            # inspect/push; divergence is rejected without a reset or force push.
            base = read_text(["git", "merge-base", "HEAD", remote_ref], config)
            remote = read_text(["git", "rev-parse", remote_ref], config)
            local = read_text(["git", "rev-parse", "HEAD"], config)
            if base == local:
                mutate(["git", "merge", "--ff-only", remote_ref], config)
            elif base != remote:
                raise WorkerFailure(f"local and remote {issue.branch} have diverged")
    elif branch_exists(remote_ref, config):
        mutate(["git", "switch", "--track", "-c", issue.branch, remote_ref], config)
    else:
        mutate(
            [
                "git",
                "switch",
                "-c",
                issue.branch,
                f"origin/{config.integration_branch}",
            ],
            config,
        )
    # Check the reconciled checkout. A local branch may safely fast-forward to a
    # valid remote, and a remote may safely lag valid local commits.
    require_ancestor(integration_ref, "HEAD", config)


def require_branch_pushed(branch: str, config: Config) -> str:
    if worktree_is_dirty(config):
        raise WorkerFailure(f"{branch} has uncommitted work after an agent turn")
    current = read_text(["git", "branch", "--show-current"], config)
    if current != branch:
        raise WorkerFailure(
            f"agent left the worktree on {current!r}, expected {branch!r}"
        )
    mutate(["git", "fetch", "origin", branch], config)
    local_sha = read_text(["git", "rev-parse", "HEAD"], config)
    remote_sha = read_text(["git", "rev-parse", f"origin/{branch}"], config)
    if local_sha != remote_sha:
        raise WorkerFailure(f"{branch} is not fully pushed to origin")
    return local_sha


def issue_url(issue: Issue, config: Config) -> str:
    return f"https://github.com/{config.slug}/{issue.url_path}"


def create_agent(client: PurpleMuxCLIClient, config: Config) -> str:
    return client.create_session(
        CreateSessionRequest(worker="codex", cwd=str(config.repo), command="codex")
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
        name,
        "started",
        iteration=iteration,
        workspace=client.workspace_id,
        tab=tab,
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
        name,
        "completed",
        iteration=iteration,
        workspace=client.workspace_id,
        tab=tab,
    )
    return result


def decision(result: str) -> str:
    first_line = next(
        (line.strip() for line in result.splitlines() if line.strip()), ""
    )
    if first_line not in {"APPROVED", "CHANGES_REQUESTED"}:
        raise WorkerFailure(
            "reviewer violated the verdict contract; expected APPROVED or "
            "CHANGES_REQUESTED on the first non-empty line"
        )
    return first_line


def require_open_issue_pr(issue: Issue, config: Config) -> dict[str, Any]:
    prs = [pr for pr in issue_prs(issue, config) if pr.get("state") == "OPEN"]
    if len(prs) != 1:
        raise WorkerFailure(
            f"expected exactly one open PR from {issue.branch} to "
            f"{config.integration_branch}; found {len(prs)}"
        )
    pr = prs[0]
    if pr.get("headRefName") != issue.branch:
        raise WorkerFailure("Issue PR head changed unexpectedly")
    if not isinstance(pr.get("headRefOid"), str) or not pr["headRefOid"]:
        raise WorkerFailure("Issue PR has no usable head commit")
    if pr.get("baseRefName") != config.integration_branch:
        raise WorkerFailure("Issue PR must target the integration branch, never main")
    return pr


def require_issue_head(issue: Issue, config: Config) -> tuple[str, dict[str, Any]]:
    branch_sha = require_branch_pushed(issue.branch, config)
    pr = require_open_issue_pr(issue, config)
    if pr["headRefOid"] != branch_sha:
        raise WorkerFailure(
            f"Issue #{issue.number} local/origin head {branch_sha} does not match "
            f"PR head {pr['headRefOid']}"
        )
    return branch_sha, pr


def require_approved_head(actual_sha: str, recovery: Recovery, stage: str) -> None:
    if recovery.approved_sha is None:
        raise WorkerFailure(f"{stage} checkpoint has no approved commit SHA")
    if actual_sha != recovery.approved_sha:
        raise WorkerFailure(
            f"{stage} head changed after approval: approved "
            f"{recovery.approved_sha}, current {actual_sha}; run a new review"
        )


def reopen_review_if_approval_drifted(
    actual_sha: str,
    recovery: Recovery,
    reviewable_phase: str,
    config: Config,
) -> bool:
    if recovery.approved_sha == actual_sha:
        return False
    recovery.approved_sha = None
    # The reviewed subject changed, so start a new bounded loop for the new head.
    recovery.reviews_used = 0
    recovery.phase = reviewable_phase
    recovery.checkpoint(config)
    return True


def merge_issue_pr(issue: Issue, config: Config, recovery: Recovery) -> dict[str, Any]:
    already_merged = merged_issue_pr(issue, config)
    if already_merged is not None:
        require_approved_head(str(already_merged["headRefOid"]), recovery, "Issue PR")
        return already_merged
    approved_sha, pr = require_issue_head(issue, config)
    require_approved_head(approved_sha, recovery, "Issue PR")
    if pr.get("isDraft"):
        mutate(["gh", "pr", "ready", str(pr["number"]), "--repo", config.slug], config)
        current_sha, pr = require_issue_head(issue, config)
        require_approved_head(current_sha, recovery, "Issue PR")
    mutate(
        [
            "gh",
            "pr",
            "merge",
            str(pr["number"]),
            "--repo",
            config.slug,
            "--merge",
            "--match-head-commit",
            str(pr["headRefOid"]),
        ],
        config,
    )
    merged = merged_issue_pr(issue, config)
    if merged is None:
        raise MutationOutcomeUnknown(
            f"PR #{pr['number']} merge returned but merged state is not visible"
        )
    require_approved_head(str(merged["headRefOid"]), recovery, "merged Issue PR")
    return merged


def issue_prompts(issue: Issue, config: Config) -> tuple[str, str]:
    implementation = f"""You are the implementer for {issue_url(issue, config)}.
Read the current Issue body with gh and inspect the repository before changing it.
Use only the existing branch {issue.branch}, based on the latest merged
{config.integration_branch}. Continue existing partial work/PR state safely; never
reset, force-push, delete, merge, or recreate a mutation whose outcome is unknown.
Implement only this Issue with the smallest design consistent with the existing
architecture. Resolve ordinary engineering choices by inspecting existing patterns,
document non-obvious decisions, and continue. Stop only for contradictory
requirements, unavailable credentials, unsafe/destructive operations, or an external
blocker that cannot safely be repaired. A trust/login/onboarding screen is a startup
blocker, not completion.
Run the repository's normal format, lint, typecheck, tests, and git diff --check.
Commit and push all intended changes. Ensure exactly one Draft PR exists from
{issue.branch} to {config.integration_branch}; never target {config.main_branch}.
Do not review or merge the PR. Return a concise implementation/check summary."""
    review = f"""You are the independent, read-only reviewer for
{issue_url(issue, config)}. Inspect the Issue with gh and review the current PR diff
from {issue.branch} to {config.integration_branch}. Run proportionate checks, but do
not modify files, commit, push, change PR state, merge, or delegate review.
Check correctness, regressions, security, test coverage, and consistency with the
existing thin-layer/plain-Python architecture. Return exactly APPROVED on the first
non-empty line if there are no actionable blocking findings. Otherwise return exactly
CHANGES_REQUESTED on that line, followed by concrete actionable findings."""
    return implementation, review


def close_tabs(client: PurpleMuxCLIClient, tabs: Sequence[str]) -> None:
    errors: list[str] = []
    for tab in reversed(tuple(dict.fromkeys(tabs))):
        try:
            client.read_status(tab)
        except WorkerFailure as exc:
            if "not found" in str(exc).lower():
                continue
            errors.append(f"{tab}: could not inspect before close: {exc}")
            continue
        try:
            client.close_session(tab)  # One mutation attempt; never blind retry.
        except TerminalSessionError as exc:
            errors.append(f"{tab}: {exc}")
    if errors:
        raise WorkerFailure("session cleanup failed: " + "; ".join(errors))


def process_issue(
    issue: Issue,
    config: Config,
    client: PurpleMuxCLIClient,
    recovery: Recovery,
) -> None:
    merged_before_start = merged_issue_pr(issue, config)
    if merged_before_start is not None:
        if worktree_is_dirty(config):
            raise WorkerFailure(
                f"Issue #{issue.number} is merged but the worktree has uncommitted "
                "changes; reconcile them without discarding work before resuming"
            )
        if recovery.issue_number == issue.number and recovery.phase.startswith(
            "issue_"
        ):
            require_approved_head(
                str(merged_before_start["headRefOid"]), recovery, "merged Issue PR"
            )
            retained = [
                tab
                for tab in (recovery.implementer, recovery.reviewer)
                if tab is not None
            ]
            close_tabs(client, retained)
            recovery.phase = "workspace_ready"
            recovery.issue_number = None
            recovery.implementer = None
            recovery.reviewer = None
            recovery.reviews_used = 0
            recovery.approved_sha = None
            recovery.checkpoint(config)
        print(
            f"Skipping already-merged Issue #{issue.number} PR: "
            f"{merged_before_start['url']}",
            flush=True,
        )
        return
    resumed_here = recovery.issue_number == issue.number and recovery.phase.startswith(
        "issue_"
    )
    preserved_dirty_issue = resumed_here and preserve_dirty_resume_branch(
        issue.branch, "Issue", config
    )
    if preserved_dirty_issue:
        require_ancestor(f"origin/{config.integration_branch}", "HEAD", config)
    else:
        prepare_issue_branch(issue, config)
    if not resumed_here:
        recovery.phase = "issue_implementer_create_pending"
        recovery.issue_number = issue.number
        recovery.implementer = None
        recovery.reviewer = None
        recovery.reviews_used = 0
        recovery.approved_sha = None
        recovery.checkpoint(config)
        recovery.implementer = create_agent(client, config)
        recovery.phase = "issue_implementer_ready"
        recovery.checkpoint(config)
    elif recovery.phase.endswith("_create_pending"):
        raise MutationOutcomeUnknown(
            "a prior Issue session creation may have succeeded; inspect the "
            "checkpointed workspace and start a workflow-specific recovery after "
            "reconciling the unknown tab; do not replay creation"
        )
    elif recovery.implementer is None:
        raise WorkerFailure("Issue checkpoint is missing its implementer tab")

    if recovery.reviewer is None:
        recovery.phase = "issue_reviewer_create_pending"
        recovery.checkpoint(config)
        recovery.reviewer = create_agent(client, config)
        recovery.phase = "issue_sessions_ready"
        recovery.checkpoint(config)

    assert recovery.implementer is not None
    assert recovery.reviewer is not None
    implementation_prompt, review_prompt = issue_prompts(issue, config)

    if recovery.phase in {"issue_implementer_ready", "issue_sessions_ready"}:
        run_turn(
            client,
            recovery.implementer,
            f"Issue #{issue.number} implementation",
            implementation_prompt,
        )
        require_branch_pushed(issue.branch, config)
        require_open_issue_pr(issue, config)
        recovery.phase = "issue_implementation_done"
        recovery.approved_sha = None
        recovery.checkpoint(config)

    approved = recovery.phase == "issue_approved"
    if approved:
        current_sha, _ = require_issue_head(issue, config)
        approved = not reopen_review_if_approval_drifted(
            current_sha, recovery, "issue_fix_done", config
        )
    while not approved and recovery.reviews_used < MAX_REVIEWS:
        review_number = recovery.reviews_used + 1
        reviewed_sha, _ = require_issue_head(issue, config)
        result = run_turn(
            client,
            recovery.reviewer,
            f"Issue #{issue.number} review",
            f"{review_prompt}\nReview exactly commit {reviewed_sha}.",
            iteration=review_number,
        )
        current_sha, _ = require_issue_head(issue, config)
        if current_sha != reviewed_sha:
            raise WorkerFailure(
                f"Issue #{issue.number} head changed during review; discard this "
                "verdict and run a new review"
            )
        if decision(result) == "APPROVED":
            recovery.reviews_used = review_number
            recovery.phase = "issue_approved"
            recovery.approved_sha = reviewed_sha
            recovery.checkpoint(config)
            approved = True
            break
        if review_number == MAX_REVIEWS:
            raise WorkerFailure(f"Issue #{issue.number} reached {MAX_REVIEWS} reviews")
        run_turn(
            client,
            recovery.implementer,
            f"Issue #{issue.number} fixes",
            f"""Fix every actionable finding from independent review round
{review_number} for {issue_url(issue, config)}. Inspect the current branch/PR first.
Keep {issue.branch}; do not create or merge PRs. Make the smallest coherent fixes,
run all normal checks and git diff --check, then commit and push.

REVIEW RESULT:
{result}""",
            iteration=review_number,
        )
        require_branch_pushed(issue.branch, config)
        require_open_issue_pr(issue, config)
        recovery.reviews_used = review_number
        recovery.phase = "issue_fix_done"
        recovery.approved_sha = None
        recovery.checkpoint(config)

    if not approved:
        raise WorkerFailure(f"Issue #{issue.number} ended without approval")
    approved_sha, _ = require_issue_head(issue, config)
    require_approved_head(approved_sha, recovery, "Issue PR")
    merged = merge_issue_pr(issue, config, recovery)
    recovery.phase = "issue_merged"
    recovery.checkpoint(config)
    close_tabs(client, [recovery.implementer, recovery.reviewer])
    print(f"Merged approved Issue #{issue.number} PR: {merged['url']}", flush=True)
    recovery.phase = "workspace_ready"
    recovery.issue_number = None
    recovery.implementer = None
    recovery.reviewer = None
    recovery.reviews_used = 0
    recovery.approved_sha = None
    recovery.checkpoint(config)


def integration_prs(config: Config, state: str = "all") -> list[dict[str, Any]]:
    data = gh_json(
        [
            "pr",
            "list",
            "--head",
            config.integration_branch,
            "--base",
            config.main_branch,
            "--state",
            state,
            "--limit",
            "20",
            "--json",
            "number,url,state,isDraft,headRefName,headRefOid,baseRefName",
        ],
        config,
    )
    if not isinstance(data, list):
        raise WorkerFailure("gh integration PR list returned an unexpected value")
    return data


def require_integration_head(
    pr_number: int, config: Config
) -> tuple[str, dict[str, Any]]:
    branch_sha = require_branch_pushed(config.integration_branch, config)
    open_prs = [pr for pr in integration_prs(config) if pr.get("state") == "OPEN"]
    if len(open_prs) != 1 or open_prs[0].get("number") != pr_number:
        raise WorkerFailure("integration PR identity changed unexpectedly")
    pr = open_prs[0]
    if pr.get("headRefName") != config.integration_branch:
        raise WorkerFailure("integration PR head branch changed unexpectedly")
    if pr.get("baseRefName") != config.main_branch:
        raise WorkerFailure("integration PR base changed unexpectedly")
    if pr.get("headRefOid") != branch_sha:
        raise WorkerFailure(
            f"integration local/origin head {branch_sha} does not match "
            f"PR head {pr.get('headRefOid')}"
        )
    return branch_sha, pr


def restore_draft_if_ready_head_is_unapproved(
    pr: dict[str, Any], recovery: Recovery, config: Config
) -> None:
    if pr.get("headRefOid") == recovery.approved_sha:
        return
    if not pr.get("isDraft"):
        mutate(
            ["gh", "pr", "ready", str(pr["number"]), "--undo", "--repo", config.slug],
            config,
        )
    reopen_review_if_approval_drifted(
        str(pr.get("headRefOid")), recovery, "integration_fix_done", config
    )
    raise WorkerFailure(
        "integration PR head changed after approval; Draft status was restored and "
        "a new whole-version review is required"
    )


def ensure_draft_integration_pr(config: Config) -> dict[str, Any]:
    open_prs = [pr for pr in integration_prs(config) if pr.get("state") == "OPEN"]
    if len(open_prs) > 1:
        raise WorkerFailure(
            "multiple open integration PRs require manual reconciliation"
        )
    if open_prs:
        pr = open_prs[0]
        if not pr.get("isDraft"):
            raise WorkerFailure(
                "integration PR became Ready before whole-version approval; restore "
                "Draft status manually before resuming"
            )
        return pr
    title = f"Integrate {config.integration_branch}"
    body = (
        "Sequentially integrates the configured Issue PRs. This PR remains Draft "
        "until the mandatory independent whole-version review approves it. It is "
        "never merged automatically."
    )
    mutate(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            config.slug,
            "--head",
            config.integration_branch,
            "--base",
            config.main_branch,
            "--draft",
            "--title",
            title,
            "--body",
            body,
        ],
        config,
    )
    open_prs = [pr for pr in integration_prs(config) if pr.get("state") == "OPEN"]
    if len(open_prs) != 1:
        raise MutationOutcomeUnknown("integration PR creation could not be confirmed")
    return open_prs[0]


def run_checks_in_purplemux(
    client: PurpleMuxCLIClient,
    config: Config,
    recovery: Recovery,
    name: str,
) -> None:
    recovery.phase = "integration_checks_start_pending"
    recovery.checkpoint(config)
    tab = client.start_shell(
        ShellCommandRequest(
            command=config.check_command, cwd=str(config.repo), name=name
        )
    )
    emit_step(name, "started", workspace=client.workspace_id, tab=tab)
    try:
        client.wait_for_shell_completion(tab, SHELL_TIMEOUT)
        result = client.read_shell_result(tab)
        if result.exit_code != 0:
            raise WorkerFailure(
                f"checks exited {result.exit_code}; inspect {client.workspace_id}/{tab}"
            )
    except BaseException as exc:
        emit_step(
            name,
            "failed",
            error=short_error(exc),
            workspace=client.workspace_id,
            tab=tab,
        )
        # Failed shell tabs remain open for inspection, just like failed agents.
        raise
    emit_step(name, "completed", workspace=client.workspace_id, tab=tab)
    close_tabs(client, [tab])
    recovery.phase = "integration_checks_done"
    recovery.checkpoint(config)


def integration_review(
    config: Config,
    client: PurpleMuxCLIClient,
    recovery: Recovery,
) -> dict[str, Any]:
    resumed_here = recovery.phase.startswith("integration_")
    preserved_dirty_integration = resumed_here and preserve_dirty_resume_branch(
        config.integration_branch, "integration", config
    )
    if preserved_dirty_integration:
        require_ancestor(f"origin/{config.integration_branch}", "HEAD", config)
    else:
        switch_to_integration(config)
    open_before_start = [
        item for item in integration_prs(config) if item.get("state") == "OPEN"
    ]
    if len(open_before_start) == 1 and not open_before_start[0].get("isDraft"):
        if recovery.phase not in {
            "integration_approved",
            "integration_checks_done",
            "integration_ready",
        }:
            raise WorkerFailure(
                "integration PR became Ready before whole-version approval; restore "
                "Draft status manually before resuming"
            )
        restore_draft_if_ready_head_is_unapproved(
            open_before_start[0], recovery, config
        )
        current_sha, current_pr = require_integration_head(
            int(open_before_start[0]["number"]), config
        )
        require_approved_head(current_sha, recovery, "integration PR")
        if recovery.phase == "integration_approved":
            # Re-running checks is safe if Ready succeeded but its response was lost.
            run_checks_in_purplemux(
                client, config, recovery, "final whole-version checks"
            )
            checked_sha, current_pr = require_integration_head(
                int(open_before_start[0]["number"]), config
            )
            require_approved_head(checked_sha, recovery, "integration PR")
            recovery.phase = "integration_ready"
            recovery.checkpoint(config)
        retained = [
            tab for tab in (recovery.implementer, recovery.reviewer) if tab is not None
        ]
        close_tabs(client, retained)
        return current_pr
    pr = ensure_draft_integration_pr(config)
    if not resumed_here:
        recovery.phase = "integration_fixer_create_pending"
        recovery.issue_number = None
        recovery.implementer = None
        recovery.reviewer = None
        recovery.reviews_used = 0
        recovery.approved_sha = None
        recovery.checkpoint(config)
        recovery.implementer = create_agent(client, config)
        recovery.phase = "integration_fixer_ready"
        recovery.checkpoint(config)
    elif recovery.phase.endswith("_create_pending"):
        raise MutationOutcomeUnknown(
            "a prior integration session creation may have succeeded; inspect the "
            "checkpointed workspace and start a workflow-specific recovery after "
            "reconciling the unknown tab; do not replay creation"
        )
    elif recovery.phase == "integration_checks_start_pending":
        raise MutationOutcomeUnknown(
            "the managed final-check tab may have started; inspect it before "
            "starting a new workflow-specific recovery; do not replay the command"
        )
    elif recovery.implementer is None:
        raise WorkerFailure("integration checkpoint is missing its fixer tab")
    if recovery.reviewer is None:
        recovery.phase = "integration_reviewer_create_pending"
        recovery.checkpoint(config)
        recovery.reviewer = create_agent(client, config)
        recovery.phase = "integration_sessions_ready"
        recovery.checkpoint(config)
    assert recovery.implementer is not None
    assert recovery.reviewer is not None

    review_prompt = f"""You are the new independent whole-version integration
reviewer for PR #{pr["number"]} ({config.integration_branch} ->
{config.main_branch}) in {config.slug}. Review the full combined diff and behavior as
one system. Do not modify files, commit, push, change PR state, merge, or delegate.
Run proportionate checks. Specifically look for cross-feature failures; shared-state
or Source-of-Truth inconsistency; concurrency, stale-response, Stop/process-exit, and
shutdown cleanup races; resume x execution-context x preflight inconsistency; legacy
API x new API behavior; mutation timeout/unknown-outcome safety; Host/Origin/request-
token security regressions; UI selected/backend-state incoherence; and unnecessary
accumulated architecture. Return exactly APPROVED on the first non-empty line only
when no blocking actionable findings remain. Otherwise return exactly
CHANGES_REQUESTED, followed by concrete findings."""

    approved = recovery.phase in {"integration_approved", "integration_checks_done"}
    if approved:
        current_sha, _ = require_integration_head(int(pr["number"]), config)
        approved = not reopen_review_if_approval_drifted(
            current_sha, recovery, "integration_fix_done", config
        )
    while not approved and recovery.reviews_used < MAX_REVIEWS:
        review_number = recovery.reviews_used + 1
        reviewed_sha, _ = require_integration_head(int(pr["number"]), config)
        result = run_turn(
            client,
            recovery.reviewer,
            "whole-version integration review",
            f"{review_prompt}\nReview exactly commit {reviewed_sha}.",
            iteration=review_number,
        )
        current_sha, _ = require_integration_head(int(pr["number"]), config)
        if current_sha != reviewed_sha:
            raise WorkerFailure(
                "integration head changed during review; discard this verdict and "
                "run a new review"
            )
        if decision(result) == "APPROVED":
            recovery.reviews_used = review_number
            recovery.phase = "integration_approved"
            recovery.approved_sha = reviewed_sha
            recovery.checkpoint(config)
            approved = True
            break
        if review_number == MAX_REVIEWS:
            raise WorkerFailure(
                f"whole-version integration review reached {MAX_REVIEWS} rounds"
            )
        run_turn(
            client,
            recovery.implementer,
            "integration fixes",
            f"""You are the integration fixer for PR #{pr["number"]} in {config.slug}.
Fix only the actionable whole-version findings below on the existing
{config.integration_branch} branch. Inspect current state before mutation. Follow the
existing architecture and choose the smallest coherent fix. Do not create another PR,
change PR readiness, merge, or delegate review. Run normal format/lint/typecheck/tests
and git diff --check, commit, and push to {config.integration_branch}.

INTEGRATION REVIEW RESULT:
{result}""",
            iteration=review_number,
        )
        require_branch_pushed(config.integration_branch, config)
        recovery.reviews_used = review_number
        recovery.phase = "integration_fix_done"
        recovery.approved_sha = None
        recovery.checkpoint(config)

    if not approved:
        raise WorkerFailure("whole-version integration review ended without approval")
    approved_sha, _ = require_integration_head(int(pr["number"]), config)
    require_approved_head(approved_sha, recovery, "integration PR")
    if recovery.phase != "integration_checks_done":
        run_checks_in_purplemux(client, config, recovery, "final whole-version checks")
    checked_sha, current_pr = require_integration_head(int(pr["number"]), config)
    require_approved_head(checked_sha, recovery, "integration PR")
    if current_pr.get("isDraft"):
        mutate(["gh", "pr", "ready", str(pr["number"]), "--repo", config.slug], config)
    ready_candidates = [
        item for item in integration_prs(config) if item.get("state") == "OPEN"
    ]
    if len(ready_candidates) != 1 or ready_candidates[0].get("number") != pr.get(
        "number"
    ):
        raise MutationOutcomeUnknown("integration PR Ready transition is unconfirmed")
    restore_draft_if_ready_head_is_unapproved(ready_candidates[0], recovery, config)
    ready_sha, ready_pr = require_integration_head(int(pr["number"]), config)
    require_approved_head(ready_sha, recovery, "integration PR")
    if ready_pr.get("isDraft"):
        raise MutationOutcomeUnknown("integration PR Ready transition is unconfirmed")

    # Deliberate terminal state: Ready for human review. There is no command or
    # helper anywhere in this workflow that merges the main-targeting PR.
    recovery.phase = "integration_ready"
    recovery.checkpoint(config)
    close_tabs(client, [recovery.implementer, recovery.reviewer])
    return ready_pr


def diagnose(client: PurpleMuxCLIClient, recovery: Recovery) -> None:
    print(f"Preserving PurpleMux workspace for inspection: {recovery.workspace}")
    for tab in (recovery.implementer, recovery.reviewer):
        if tab is None:
            continue
        print(f"Preserving tab: {tab}")
        try:
            capture = client.capture_screen(tab)
        except TerminalSessionError as exc:
            print(f"Diagnostic capture failed for {tab}: {exc}")
        else:
            # Capture is diagnostic only; workflow decisions always use structured
            # status/results from wait_for_turn_completion() and read_result().
            print(f"Diagnostic capture for {tab}:\n{capture}")


def fail(
    client: PurpleMuxCLIClient, recovery: Recovery, exc: BaseException
) -> NoReturn:
    emit_step(
        "workflow",
        "failed",
        error=short_error(exc),
        workspace=recovery.workspace,
    )
    diagnose(client, recovery)
    raise exc


def main() -> None:
    config = parse_args()
    checkpoint = resume_checkpoint()
    validate_repository(config, checkpoint)
    recovery = load_recovery(config, checkpoint)
    client = PurpleMuxCLIClient(recovery.workspace)
    try:
        for issue in config.issues:
            process_issue(issue, config, client, recovery)
        ready_pr = integration_review(config, client, recovery)
    except WorkerNeedsInput as exc:
        # The latest checkpoint was published before the turn mutation. Keep the
        # sessions, let a human answer/repair in PurpleMux, then explicitly Resume.
        diagnose(client, recovery)
        suspend_run(str(exc))
    except BaseException as exc:
        fail(client, recovery, exc)
    emit_step("workflow", "completed", workspace=recovery.workspace)
    print(
        f"Integration PR #{ready_pr['number']} is Ready: {ready_pr['url']}\n"
        f"It was NOT merged into {config.main_branch}.",
        flush=True,
    )


if __name__ == "__main__":
    main()
