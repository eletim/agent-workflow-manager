#!/usr/bin/env python3
"""Sequential version development with explicit new-run recovery.

Git and GitHub are inspected before every delivery mutation. PurpleMux resources
belong to this run and remain inspectable after failure or stop; a recovery starts
a new run and creates new runtime resources.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    GitHubRepository,
    GitRepository,
    MergeResult,
    MutationOutcomeUnknown,
    PullRequestState,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    ShellCommandRequest,
    WorkerFailure,
    emit_finding,
    emit_issue_driven_context,
    emit_issue_result,
    emit_run_pr,
    emit_step,
    emit_whole_review_result,
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
MAX_SCOPE_REVIEWS = 3
MAX_WORK_ITEMS = 200
MAX_PLANNER_TURNS = MAX_WORK_ITEMS + 1
MAX_PLANNER_ACTIONS = 100
MAX_PLAN_STATE_CHARS = 32_000
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
IMPLEMENTATION_PRINCIPLE = (
    "Reuse the existing implementation where appropriate and keep the change scope "
    "to the minimum required for this Issue. Do not achieve a minimal diff or "
    "reduced code size by mixing responsibilities unnaturally or by "
    "over-generalizing distinct behavior into shared abstractions."
)
POLICY_CONFLICT_MARKER = "POLICY_CONFLICT:"
POLICY_CONFLICT_PR_MARKER = "agent-workflow-manager:policy-conflict:"
INLINE_TASK_FINGERPRINT_MARKER = "agent-workflow-manager:inline-task-sha256:"
POLICY_CONFLICT_WARNINGS: list[tuple[int | str | None, str]] = []
HUMAN_HANDOFF_START = "<!-- agent-workflow-manager:human-handoff:start -->"
HUMAN_HANDOFF_END = "<!-- agent-workflow-manager:human-handoff:end -->"
MAX_HUMAN_HANDOFF_CHARS = 12_000
WORK_ITEM_PLAN_MARKER = "agent-workflow-manager:work-item-plan:"


@dataclass(frozen=True)
class Issue:
    number: int | None
    branch: str
    task_id: str | None = None
    task: str | None = None
    task_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.number is not None:
            if any(
                value is not None
                for value in (self.task_id, self.task, self.task_fingerprint)
            ):
                raise ValueError("GitHub Issues cannot include inline task metadata")
            return
        if None in (self.task_id, self.task, self.task_fingerprint):
            raise ValueError("inline tasks require an ID, task, and fingerprint")
        assert self.task_id is not None
        assert self.task is not None
        assert self.task_fingerprint is not None
        actual = hashlib.sha256(self.task.encode()).hexdigest()
        if self.task_fingerprint != actual:
            raise ValueError("inline task fingerprint does not match its task")
        if self.branch != f"feature/work-item-{self.task_id}":
            raise ValueError("inline task branch does not match its ID")

    @property
    def label(self) -> str:
        if self.number is not None:
            return f"Issue #{self.number}"
        assert self.task_id is not None
        return f"Mini task {self.task_id}"

    @property
    def result_id(self) -> int | str:
        return self.number if self.number is not None else f"mini-task:{self.task_id}"

    @property
    def key(self) -> int | str:
        """Return the stable key used to revise a pending work item."""
        assert self.number is not None or self.task_id is not None
        return self.number if self.number is not None else self.task_id

    @property
    def correlation_id(self) -> str:
        return (
            f"issue-{self.number}"
            if self.number is not None
            else f"mini-task-{self.task_id}"
        )

    @property
    def requirement(self) -> str:
        if self.number is not None:
            return (
                f"Read Issue #{self.number} with gh before editing; its body is the "
                "authoritative requirement."
            )
        assert self.task is not None
        return f"The following inline mini task is authoritative:\n\n{self.task}"

    @property
    def pr_body(self) -> str:
        body = f"Sequential implementation of {self.label}."
        if self.task_fingerprint is None:
            return body
        assert self.task is not None
        return (
            f"<!-- {INLINE_TASK_FINGERPRINT_MARKER}{self.task_fingerprint} -->\n\n"
            f"{body}\n\nInline task:\n\n{self.task}"
        )


@dataclass(frozen=True)
class Config:
    repo: Path
    slug: str
    integration_branch: str
    main_branch: str
    issues: tuple[Issue, ...]
    check_command: str
    policy_issue: int | None = None
    one_shot_issue: int | None = None


@dataclass
class WorkItemPlan:
    """Mutable work-item order owned by this plain-Python workflow."""

    config: Config
    items: list[Issue] = field(init=False)
    position: int = 0
    finalized: bool = False

    def __post_init__(self) -> None:
        self.items = list(self.config.issues)
        self._validate(self.items)

    def _validate(self, issues: list[Issue]) -> None:
        if len(issues) > MAX_WORK_ITEMS:
            raise ValueError(f"work-item plan cannot exceed {MAX_WORK_ITEMS} items")
        identities = [issue.key for issue in issues]
        branches = [issue.branch for issue in issues]
        if len(set(identities)) != len(identities):
            raise ValueError("work-item identities must be unique")
        if len(set(branches)) != len(branches):
            raise ValueError("work-item branches must be unique")
        reserved = {self.config.integration_branch, self.config.main_branch}
        if len(reserved) != 2 or any(branch in reserved for branch in branches):
            raise ValueError(
                "integration, main, and every work-item branch must be distinct"
            )
        if self.config.policy_issue in identities:
            raise ValueError(
                "policy Issue must differ from every implementation work item"
            )

    def add(self, issue: Issue) -> None:
        candidate = [*self.items, issue]
        self._validate(candidate)
        self.items.append(issue)

    def update(self, identity: int | str, issue: Issue) -> None:
        index = self._remaining_index(identity)
        current = self.items[index]
        if issue.result_id != current.result_id or issue.branch != current.branch:
            raise ValueError("updated work item must preserve its identity and branch")
        candidate = list(self.items)
        candidate[index] = issue
        self._validate(candidate)
        self.items[index] = issue

    def skip(self, identity: int | str) -> Issue:
        return self.items.pop(self._remaining_index(identity))

    def take_next(self) -> Issue | None:
        if self.position == len(self.items):
            return None
        issue = self.items[self.position]
        self.position += 1
        return issue

    @property
    def snapshot(self) -> tuple[Issue, ...]:
        return tuple(self.items)

    @property
    def remaining(self) -> tuple[Issue, ...]:
        return tuple(self.items[self.position :])

    def _remaining_index(self, identity: int | str) -> int:
        for index in range(self.position, len(self.items)):
            if self.items[index].key == identity:
                return index
        raise ValueError(f"no unprocessed work item with identity {identity!r}")


@dataclass(frozen=True)
class ReviewDelivery:
    outcome: Literal["approved", "continued_with_warning", "skipped"]
    head_sha: str
    base_sha: str
    reviews: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class IssueHandoffResult:
    issue: int | str
    label: str
    pr_number: int
    pr_url: str
    outcome: str
    reviews: int
    warnings: tuple[str, ...] = ()


ISSUE_HANDOFF_RESULTS: list[IssueHandoffResult] = []


@dataclass(frozen=True)
class IssueReviewPhaseResult:
    pr: PullRequestState
    outcome: Literal["approved", "continued_with_warning", "head_changed"]
    head_sha: str
    base_sha: str
    reviews: int
    warnings: tuple[str, ...] = ()


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Sequential reviewed Issue development"
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--slug", required=True)
    parser.add_argument("--integration-branch", required=True)
    parser.add_argument("--main-branch", default="main")
    work = parser.add_mutually_exclusive_group(required=True)
    work.add_argument("--issue", action="append")
    work.add_argument("--one-shot-issue", type=int)
    parser.add_argument("--check-command", required=True)
    parser.add_argument("--policy-issue", type=int)
    args = parser.parse_args()
    issues: list[Issue] = []
    for value in args.issue or ():
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
    if args.one_shot_issue is not None and args.one_shot_issue < 1:
        parser.error("one-shot Issue must be a positive integer")
    return Config(
        args.repo.resolve(),
        args.slug,
        args.integration_branch,
        args.main_branch,
        tuple(issues),
        args.check_command,
        args.policy_issue,
        args.one_shot_issue,
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


def implementer_prompt(prompt: str) -> str:
    """Add the shared change-boundary policy to an implementation turn."""
    return f"{prompt.rstrip()}\n\n{IMPLEMENTATION_PRINCIPLE}"


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


_REVIEW_VERDICTS = {"APPROVED", "CHANGES_REQUESTED"}
_VERDICT_PREFIX = re.compile(r"^VERDICT\s*:\s*", re.IGNORECASE)


def _normalized_verdict(line: str) -> str | None:
    normalized = line.strip().upper()
    normalized = re.sub(r"^#{1,6}\s*", "", normalized)
    normalized = normalized.strip(" \t*_`")
    normalized = _VERDICT_PREFIX.sub("", normalized)
    normalized = normalized.strip(" \t*_`")
    normalized = " ".join(normalized.split())
    return normalized if normalized in _REVIEW_VERDICTS else None


def decision(result: str) -> str:
    leading_lines = [line for line in result.splitlines() if line.strip()][:3]
    verdicts = [
        verdict
        for line in leading_lines
        if (verdict := _normalized_verdict(line)) is not None
    ]
    if len(set(verdicts)) > 1:
        raise WorkerFailure("reviewer verdict is ambiguous")
    if verdicts:
        return verdicts[0]
    raise WorkerFailure(
        "reviewer must provide APPROVED or CHANGES_REQUESTED near the beginning"
    )


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
implementation work item remains the primary requirement. If you find a clear
conflict, continue by following the implementation Issue and include a line
starting with {POLICY_CONFLICT_MARKER} that truthfully describes the conflict.
{known_conflicts}

"""


def record_policy_conflict(issue_number: int | str | None, warning: str) -> None:
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
    issue_number: int | str | None = None,
) -> None:
    if config.policy_issue is None:
        return
    for line in result.splitlines():
        marker, separator, detail = line.strip().partition(POLICY_CONFLICT_MARKER)
        if separator and not marker and detail.strip():
            warning = (
                f"Policy Issue #{config.policy_issue} conflicts with {scope}: "
                f"{detail.strip()[:500]}; continuing with the implementation "
                "work item as the primary requirement."
            )
            record_policy_conflict(issue_number, warning)


def encoded_policy_conflict_marker(warning: str) -> str:
    encoded = base64.b64encode(warning.encode("utf-8")).decode("ascii")
    return f"<!-- {POLICY_CONFLICT_PR_MARKER}{encoded} -->"


def summary_warnings(
    issue_number: int | str | None, additional: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Keep the result event narrow while retaining its primary warnings."""
    warnings = list(additional)
    warnings.extend(
        warning
        for warning_issue, warning in POLICY_CONFLICT_WARNINGS
        if warning_issue == issue_number
    )
    return tuple(dict.fromkeys(warnings))[:3]


def record_issue_handoff_result(
    issue: int | str,
    label: str,
    pr: PullRequestState,
    outcome: str,
    reviews: int,
    warnings: tuple[str, ...],
) -> None:
    """Retain the same bounded facts emitted by the structured run summary."""
    result = IssueHandoffResult(
        issue, label, pr.number, pr.url, outcome, reviews, warnings
    )
    ISSUE_HANDOFF_RESULTS[:] = [
        existing for existing in ISSUE_HANDOFF_RESULTS if existing.issue != issue
    ]
    ISSUE_HANDOFF_RESULTS.append(result)


def human_handoff_prompt(
    config: Config,
    work_items: tuple[Issue, ...],
    pr: PullRequestState,
    delivery: ReviewDelivery,
    warnings: tuple[str, ...],
) -> str:
    issue_lines = "\n".join(
        f"- {item.label}: PR #{item.pr_number} ({item.pr_url}), "
        f"outcome={item.outcome}, reviews={item.reviews}, "
        f"warning_count={len(item.warnings)}"
        for item in ISSUE_HANDOFF_RESULTS
    ) or "- No implementation Issue result was recorded in this run."
    policy = (
        f"Policy Issue: https://github.com/{config.slug}/issues/{config.policy_issue}"
        if config.policy_issue is not None
        else "Policy Issue: none"
    )
    one_shot = (
        "One-shot source Issue: "
        f"https://github.com/{config.slug}/issues/{config.one_shot_issue}"
        if config.one_shot_issue is not None
        else "One-shot source Issue: none"
    )
    warning_lines = "\n".join(f"- {item}" for item in warnings) or "- none"
    issue_numbers = ", ".join(
        str(item.number) for item in work_items if item.number is not None
    )
    issue_source_guidance = (
        "Before writing, read every implementation Issue body with `gh issue view "
        f"NUMBER --repo {config.slug}` for Issue numbers: {issue_numbers}."
        if issue_numbers
        else "There are no implementation GitHub Issues to read for this run."
    )
    one_shot_source = (
        "Before writing, read the one-shot source Issue with `gh issue view "
        f"{config.one_shot_issue} --repo {config.slug}`."
        if config.one_shot_issue is not None
        else "This is not a one-shot run."
    )
    mini_tasks = "\n".join(
        f"- {item.label}: {item.task}" for item in work_items if item.task is not None
    ) or "- none"
    return f"""Create the final human handoff Markdown for Base PR #{pr.number}.
You are the Reviewer role Agent selected by reviewer_agent. This turn generates
prose only and does not change any review verdict. Do not edit files, run GitHub
mutations, or change Git/PR state.

{one_shot_source} {issue_source_guidance} If a Policy Issue is listed below, read it first with
`gh issue view` in the same way. Inspect the PR diff when useful, but
do not include raw logs, environment values, credentials, tokens, or secrets.
Inline mini tasks do not have GitHub Issues; use these embedded requirements:
{mini_tasks}

Authoritative handoff context:
- repository: {config.slug}
- integration/final: {config.integration_branch} @ {pr.head_sha} ->
  {config.main_branch} @ {pr.base_sha}
- Base PR: #{pr.number} {pr.url}; state={"Draft" if pr.is_draft else "Ready"}
- whole review: outcome={delivery.outcome}, reviews={delivery.reviews}
- automated verification: configured final checks passed on the exact head
- {one_shot}
- {policy}
- implementation results:
{issue_lines}
- warnings:
{warning_lines}

Return only Japanese Markdown, with these headings exactly once and in order:
## 概要
## 主な変更
## 人間による確認
## 自動検証
Add `## 注意事項` only when warnings are listed above. When a Policy Issue or
one-shot source Issue is listed, include its full URL in the prose. Under
人間による確認, use 1 to 12
unchecked `- [ ]` items. Each item must describe one concrete, quickly answerable
Yes/No observation, primarily in a browser or real environment. Do not ask a
human to rerun checks already covered by automation and do not require terminal
commands. Keep the entire response concise and under {MAX_HUMAN_HANDOFF_CHARS}
characters. Do not emit HTML comments, code fences, prefaces, or extra headings."""


def validate_human_handoff(
    markdown: str, config: Config, *, has_warnings: bool
) -> str:
    """Validate the agent's prose before it can enter the Base PR body."""
    value = markdown.strip().replace("\r\n", "\n").replace("\r", "\n")
    if not value or len(value) > MAX_HUMAN_HANDOFF_CHARS or "\0" in value:
        raise WorkerFailure("human handoff Markdown is empty or exceeds its bound")
    if "<!--" in value or "-->" in value or "```" in value:
        raise WorkerFailure("human handoff Markdown contains forbidden metadata")
    headings = re.findall(r"(?m)^## .+$", value)
    required = ["## 概要", "## 主な変更", "## 人間による確認", "## 自動検証"]
    expected = required + (["## 注意事項"] if has_warnings else [])
    if headings != expected:
        raise WorkerFailure("human handoff Markdown has an invalid section contract")
    prose = "\n".join(
        line for line in value.splitlines() if not line.startswith("## ")
    )
    if not re.search(r"[ぁ-んァ-ヶ一-龠]", prose):
        raise WorkerFailure("human handoff Markdown must be written in Japanese")
    checklist_section = value.split("## 人間による確認\n", 1)[1].split(
        "\n## 自動検証", 1
    )[0]
    checklist_lines = [
        line for line in checklist_section.splitlines() if line.strip()
    ]
    checklist = [line[6:] for line in checklist_lines if line.startswith("- [ ] ")]
    if (
        not 1 <= len(checklist) <= 12
        or len(checklist) != len(checklist_lines)
        or any(not item.strip() for item in checklist)
    ):
        raise WorkerFailure("human handoff checklist must contain 1..12 items")
    if config.policy_issue is not None:
        reference = f"https://github.com/{config.slug}/issues/{config.policy_issue}"
        if reference not in value:
            raise WorkerFailure("human handoff Markdown lacks the Policy Issue URL")
    if config.one_shot_issue is not None:
        reference = f"https://github.com/{config.slug}/issues/{config.one_shot_issue}"
        if reference not in value:
            raise WorkerFailure("human handoff Markdown lacks the one-shot Issue URL")
    return value


def with_human_handoff(existing_body: str, handoff: str) -> str:
    """Replace only AWM's managed section and preserve all other PR metadata."""
    start_count = existing_body.count(HUMAN_HANDOFF_START)
    end_count = existing_body.count(HUMAN_HANDOFF_END)
    if start_count != end_count or start_count > 1:
        raise WorkerFailure("Base PR has ambiguous human handoff markers")
    managed = f"{HUMAN_HANDOFF_START}\n{handoff}\n{HUMAN_HANDOFF_END}"
    if start_count == 0:
        prefix = existing_body.rstrip()
        return f"{prefix}\n\n{managed}" if prefix else managed
    start = existing_body.index(HUMAN_HANDOFF_START)
    end = existing_body.index(HUMAN_HANDOFF_END, start) + len(HUMAN_HANDOFF_END)
    return f"{existing_body[:start]}{managed}{existing_body[end:]}"


def warn_human_handoff(message: str) -> None:
    warning = f"Base PR human handoff was not updated: {message}"
    print(f"WARN: {warning}", flush=True)
    emit_finding("github", warning, status="warning")


def human_handoff_warnings(delivery: ReviewDelivery) -> tuple[str, ...]:
    warnings = list(delivery.warnings)
    for item in ISSUE_HANDOFF_RESULTS:
        warnings.extend(item.warnings)
    warnings.extend(warning for _, warning in POLICY_CONFLICT_WARNINGS)
    return tuple(dict.fromkeys(warnings))[:12]


def update_base_pr_human_handoff(
    config: Config,
    work_items: tuple[Issue, ...],
    client: PurpleMuxCLIClient,
    github: GitHubRepository,
    pr: PullRequestState,
    delivery: ReviewDelivery,
) -> PullRequestState:
    """Generate once, validate, then safely update without changing PR state."""
    warnings = human_handoff_warnings(delivery)
    try:
        writer = create_agent(
            client,
            config,
            agent_type=REVIEWER_AGENT,
            name="Base PR human handoff writer",
        )
        result = run_turn(
            client,
            writer,
            "Base PR human handoff",
            human_handoff_prompt(config, work_items, pr, delivery, warnings),
            pr=pr,
        )
        handoff = validate_human_handoff(
            result, config, has_warnings=bool(warnings)
        )
        body = with_human_handoff(pr.body, handoff)
    except Exception as exc:
        warn_human_handoff(short_error(exc))
        return pr
    try:
        return github.update_pr_body(
            pr.number,
            body=body,
            expected_head=config.integration_branch,
            expected_head_sha=pr.head_sha,
            expected_base=config.main_branch,
            expected_base_sha=pr.base_sha,
        )
    except MutationOutcomeUnknown:
        raise
    except WorkerFailure as exc:
        warn_human_handoff(short_error(exc))
        return pr


def rehydrate_policy_conflicts(
    body: str, config: Config, *, issue_number: int | str | None
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
        if issue_number != issue.result_id:
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
        "and implementation work items remain authoritative."
        f"{conflict_notes}"
    )


def one_shot_pr_notes(config: Config) -> str:
    if config.one_shot_issue is None:
        return ""
    reference = f"https://github.com/{config.slug}/issues/{config.one_shot_issue}"
    return f"\n\nOne-shot source Issue: {reference}"


def ensure_base_pr_one_shot_notes(
    github: GitHubRepository, pr: PullRequestState, config: Config
) -> PullRequestState:
    if config.one_shot_issue is None:
        return pr
    reference = f"https://github.com/{config.slug}/issues/{config.one_shot_issue}"
    if reference in pr.body:
        return pr
    return github.update_pr_body(
        pr.number,
        body=f"{pr.body.rstrip()}{one_shot_pr_notes(config)}",
        expected_head=config.integration_branch,
        expected_head_sha=pr.head_sha,
        expected_base=config.main_branch,
        expected_base_sha=pr.base_sha,
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
        implementer_prompt(f"""Your only task is to make the current repository state clean and
correct before {context}. The worktree is on {branch!r}. Inspect Git status and
every existing diff first. Preserve and commit all intended source, test, and
configuration changes. Add only narrow, appropriate .gitignore entries for
generated build or cache artifacts. Remove only clearly disposable generated
artifacts when safe. Do not reinterpret or reimplement the original Issue.

Do not push, modify PR state, merge, start a review, reset, stash, rebase,
force, or discard uncertain work. If any dirty path is ambiguous, preserve it
and clearly explain why it cannot be resolved safely. Finish with a clean
worktree when safe and return a concise summary of exactly what you committed,
ignored, removed, or could not resolve."""),
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


def issue_prompts(issue: Issue, config: Config) -> tuple[str, str, str]:
    context = policy_context(config, scope=issue.label)
    implementation = context + implementer_prompt(f"""Implement {issue.label} in {config.slug} on the
existing branch {issue.branch}, based on {config.integration_branch}. Read the
work-item requirement below. Inspect existing Git and GitHub state before editing
because this may be a new recovery run. Implement only the requested work item and run appropriate
project tests and checks. Commit every intended source, test, and configuration
change, leaving none uncommitted or untracked. Push the exact feature branch
{issue.branch} after committing. Create or update exactly one Draft PR from
{issue.branch} to {config.integration_branch}. Finish with a clean worktree.

{issue.requirement}

Never reset, rebase, stash, force-push, merge the work-item PR, target
{config.main_branch}, create unrelated PRs, or discard ambiguous local work.
Return a concise summary including the commit SHA and PR number or URL when
available.""")
    scope_review = context + f"""Perform only the Scope / Design Review for
{issue.label} and its PR from {issue.branch} to {config.integration_branch}.
{issue.requirement} Inspect the PR diff. Decide whether the changed targets,
amount of change, and responsibility placement are necessary and sufficient for
the work item. Check for unrelated work or unnecessary refactors, failure to reuse
appropriate existing implementation, unnatural mixing of responsibilities to
minimize the diff, over-generalization of meaningfully distinct behavior, and
unnecessary violations of the existing architecture or Source of Truth. If the
Issue identifies a policy Issue, use that version-design context; the
implementation work item remains authoritative when they conflict, and report the
conflict as a warning. Do not focus on detailed implementation bugs in this
phase. Do not mutate files or PR state. Return APPROVED or CHANGES_REQUESTED
first, followed by actionable findings."""
    correctness_review = context + f"""Perform only the Correctness Review for
{issue.label} and its PR from {issue.branch} to {config.integration_branch}.
{issue.requirement}
The change scope has already completed Scope / Design Review. Concentrate on
whether that implementation is correct and safe: functional behavior, edge
cases, state and lifecycle consistency, error handling, races or stale state,
Git/GitHub topology, regressions, missing tests, cleanup and resource ownership,
and security or secret handling. Do not reopen scope preferences unless they
cause a concrete correctness problem. Do not mutate files or PR state. Return
APPROVED or CHANGES_REQUESTED first, followed by actionable findings."""
    return implementation, scope_review, correctness_review


def require_inline_task_pr_identity(
    pr: PullRequestState, issue: Issue
) -> PullRequestState:
    if issue.task_fingerprint is None:
        return pr
    prefix = f"<!-- {INLINE_TASK_FINGERPRINT_MARKER}"
    suffix = " -->"
    lines = pr.body.splitlines()
    marker = lines[0].strip() if lines else ""
    if (
        not marker.startswith(prefix)
        or not marker.endswith(suffix)
        or marker[len(prefix) : -len(suffix)] != issue.task_fingerprint
    ):
        raise WorkerFailure(
            f"PR #{pr.number} inline task fingerprint is missing or does not match "
            "the declared task"
        )
    return pr


def inline_task_pr_fingerprint(pr: PullRequestState) -> str | None:
    prefix = f"<!-- {INLINE_TASK_FINGERPRINT_MARKER}"
    lines = pr.body.splitlines()
    marker = lines[0].strip() if lines else ""
    if not marker.startswith(prefix):
        return None
    suffix = " -->"
    return marker[len(prefix) : -len(suffix)] if marker.endswith(suffix) else ""


def prepare_issue(
    repo: GitRepository,
    github: GitHubRepository,
    issue: Issue,
    config: Config,
) -> tuple[PullRequestState | None, str, bool] | PullRequestState:
    open_pr = inspect_pr(github, head=issue.branch, base=config.integration_branch)
    if open_pr is not None:
        require_inline_task_pr_identity(open_pr, issue)
    merged = github.find_pr(
        head=issue.branch, base=config.integration_branch, state="MERGED"
    )
    if merged is not None:
        require_inline_task_pr_identity(merged, issue)
        if open_pr is not None:
            raise WorkerFailure("merged Issue also has an open same-head PR")
        emit_finding(
            "github", f"{issue.label} already merged as #{merged.number}"
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
    may_initialize_inline_identity: bool = False,
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
            title=issue.label,
            body=issue.pr_body,
            correlation_id=run_correlation(f"{issue.correlation_id}-pr"),
        )
    current = github.require_pr(
        number=pr.number,
        head=issue.branch,
        base=config.integration_branch,
        state="OPEN",
        expected_head_sha=feature.remote_sha,
        expected_base_sha=expected_base_sha,
        draft=True,
    )
    if (
        may_initialize_inline_identity
        and issue.task_fingerprint is not None
        and inline_task_pr_fingerprint(current) is None
    ):
        marker = f"<!-- {INLINE_TASK_FINGERPRINT_MARKER}{issue.task_fingerprint} -->"
        body = f"{marker}\n\n{current.body}" if current.body else marker
        current = github.update_pr_body(
            current.number,
            body=body,
            expected_head=issue.branch,
            expected_head_sha=feature.remote_sha,
            expected_base=config.integration_branch,
            expected_base_sha=expected_base_sha,
        )
    return require_inline_task_pr_identity(current, issue)


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
    """Revalidate exact clean, pushed topology before unapproved continuation."""
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


def review_issue_phase(
    issue: Issue,
    config: Config,
    client: PurpleMuxCLIClient,
    repo: GitRepository,
    github: GitHubRepository,
    implementer: str,
    reviewer: str,
    pr: PullRequestState,
    *,
    phase: str,
    prompt: str,
    max_reviews: int,
    review_offset: int = 0,
    restart_scope_on_change: bool = False,
) -> IssueReviewPhaseResult:
    """Run one independently counted Issue review/fix phase."""
    if review_offset >= max_reviews:
        warning = (
            f"{issue.label} {phase} review limit {max_reviews} was already "
            "reached before the current head could complete this phase; continuing "
            "without reviewer approval."
        )
        current = require_warning_delivery(
            repo,
            github,
            pr,
            head=issue.branch,
            base=config.integration_branch,
            expected_head_sha=pr.head_sha,
            expected_base_sha=pr.base_sha,
        )
        print(f"WARN: {warning}", flush=True)
        emit_finding("git", warning, status="warning")
        return IssueReviewPhaseResult(
            current,
            "continued_with_warning",
            current.head_sha,
            current.base_sha,
            review_offset,
            (warning,),
        )
    for review_number in range(review_offset + 1, max_reviews + 1):
        result = run_turn(
            client,
            reviewer,
            f"{issue.label} {phase} review",
            f"{prompt}\nReview exact head {pr.head_sha} against base {pr.base_sha}.",
            iteration=review_number,
            pr=pr,
        )
        emit_policy_conflicts(
            result,
            config,
            scope=f"{phase} review of {issue.label}",
            issue_number=issue.result_id,
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
        reviewed_sha, reviewer_changed = require_agent_result(
            repo,
            client,
            implementer,
            issue.branch,
            current.head_sha,
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
                expected_base_sha=current.base_sha,
                draft=True,
            )
            pr = ensure_issue_pr_policy_conflicts(github, pr, issue, config)
            emit_finding(
                "git",
                f"{phase} review changed {issue.branch}; outcome invalidated at "
                f"{reviewed_sha}",
            )
            if restart_scope_on_change:
                return IssueReviewPhaseResult(
                    pr, "head_changed", pr.head_sha, pr.base_sha, review_number
                )
            continue
        current = ensure_issue_pr_policy_conflicts(github, current, issue, config)
        if decision(result) == "APPROVED":
            return IssueReviewPhaseResult(
                current, "approved", current.head_sha, current.base_sha, review_number
            )
        if review_number == max_reviews:
            warning = (
                f"{issue.label} {phase} review limit {max_reviews} reached "
                "with CHANGES_REQUESTED; continuing without reviewer approval."
            )
            current = require_warning_delivery(
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
            return IssueReviewPhaseResult(
                current,
                "continued_with_warning",
                current.head_sha,
                current.base_sha,
                review_number,
                (warning,),
            )
        fix_result = run_turn(
            client,
            implementer,
            f"{issue.label} {phase} fixes",
            implementer_prompt(
                policy_context(config, scope=f"fixes for {issue.label}")
                + f"""Re-evaluate every {phase} review finding below. If warranted,
fix, test, commit, and leave the worktree clean. If no change is warranted,
leave it clean and explain why; do not create an empty commit.\n\n{result}"""
            ),
            iteration=review_number,
            pr=pr,
        )
        emit_policy_conflicts(
            fix_result,
            config,
            scope=f"{phase} fixes for {issue.label}",
            issue_number=issue.result_id,
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
                f"{issue.label} {phase} reviewer requested changes, but "
                "the implementer re-evaluated the finding and produced no code "
                "changes; continuing without reviewer approval."
            )
            current = require_warning_delivery(
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
            return IssueReviewPhaseResult(
                current,
                "continued_with_warning",
                current.head_sha,
                current.base_sha,
                review_number,
                (warning,),
            )
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
        if restart_scope_on_change:
            return IssueReviewPhaseResult(
                pr, "head_changed", pr.head_sha, pr.base_sha, review_number
            )
    raise WorkerFailure(f"{issue.label} {phase} review ended unexpectedly")


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
            name=f"{issue.label} worktree cleanup",
        )
        require_clean_worktree(
            repo,
            client,
            cleanup,
            context=f"preparing {issue.label}",
        )
    prepared = prepare_issue(repo, github, issue, config)
    if isinstance(prepared, PullRequestState):
        rehydrate_policy_conflicts(prepared.body, config, issue_number=issue.result_id)
        print(f"Skipping already-merged {issue.label}", flush=True)
        warnings = summary_warnings(issue.result_id)
        record_issue_handoff_result(
            issue.result_id, issue.label, prepared, "skipped", 0, warnings
        )
        emit_issue_result(
            issue.result_id,
            "skipped",
            0,
            prepared.number,
            prepared.url,
            warnings=warnings,
            label=issue.label,
        )
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
            f"{issue.label}",
            "started",
            pr_number=existing_pr.number,
            pr_url=existing_pr.url,
        )
    implementer = create_agent(
        client,
        config,
        agent_type=IMPLEMENTER_AGENT,
        name=f"{issue.label} implementer",
    )
    scope_reviewer = create_agent(
        client,
        config,
        agent_type=REVIEWER_AGENT,
        name=f"{issue.label} scope reviewer",
    )
    correctness_reviewer = create_agent(
        client,
        config,
        agent_type=REVIEWER_AGENT,
        name=f"{issue.label} correctness reviewer",
    )
    implementation_prompt, scope_prompt, correctness_prompt = issue_prompts(
        issue, config
    )
    implementation_result = run_turn(
        client,
        implementer,
        f"{issue.label} implementation",
        implementation_prompt,
        pr=existing_pr,
    )
    emit_policy_conflicts(
        implementation_result,
        config,
        scope=f"implementation {issue.label}",
        issue_number=issue.result_id,
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
        repo,
        github,
        issue,
        config,
        expected_base_sha=integration.remote_sha,
        may_initialize_inline_identity=existing_pr is None,
    )
    pr = ensure_issue_pr_policy_conflicts(github, pr, issue, config)
    if pr.head_sha != implementation_sha:
        raise WorkerFailure("delivered PR head does not match committed result")
    emit_step(
        f"{issue.label}",
        "started",
        pr_number=pr.number,
        pr_url=pr.url,
    )
    scope_reviews = 0
    correctness_reviews = 0
    while True:
        scope = review_issue_phase(
            issue,
            config,
            client,
            repo,
            github,
            implementer,
            scope_reviewer,
            pr,
            phase="scope/design",
            prompt=scope_prompt,
            max_reviews=MAX_SCOPE_REVIEWS,
            review_offset=scope_reviews,
        )
        scope_reviews = scope.reviews
        current_correctness_prompt = correctness_prompt
        if scope.outcome == "continued_with_warning":
            current_correctness_prompt += (
                "\nThe Scope / Design phase reached warning continuation without "
                "reviewer approval. Do not describe it as approved, and preserve "
                "that distinction in your findings."
            )
        correctness = review_issue_phase(
            issue,
            config,
            client,
            repo,
            github,
            implementer,
            correctness_reviewer,
            scope.pr,
            phase="correctness",
            prompt=current_correctness_prompt,
            max_reviews=MAX_REVIEWS,
            review_offset=correctness_reviews,
            restart_scope_on_change=True,
        )
        correctness_reviews = correctness.reviews
        if correctness.outcome != "head_changed":
            break
        emit_finding(
            "git",
            f"{issue.label} correctness changed the head to "
            f"{correctness.head_sha}; restarting Scope / Design Review",
        )
        pr = correctness.pr
    emit_finding(
        "git",
        f"{issue.label} review summary: scope_reviews={scope_reviews}, "
        f"correctness_reviews={correctness_reviews}, "
        f"scope_outcome={scope.outcome}, "
        f"correctness_outcome={correctness.outcome}",
    )
    fully_approved = scope.outcome == correctness.outcome == "approved"
    delivery = ReviewDelivery(
        "approved" if fully_approved else "continued_with_warning",
        correctness.head_sha,
        correctness.base_sha,
        scope_reviews + correctness_reviews,
        scope.warnings + correctness.warnings,
    )
    pr = github.set_draft(
        correctness.pr.number,
        draft=False,
        expected_head=issue.branch,
        expected_head_sha=delivery.head_sha,
        expected_base=config.integration_branch,
        expected_base_sha=delivery.base_sha,
    )
    warnings = summary_warnings(issue.result_id, delivery.warnings)
    if not MERGE_TO_INTEGRATION:
        qualifier = (
            "Approved"
            if delivery.outcome == "approved"
            else "Unapproved warning-continuation"
        )
        print(f"{qualifier} {issue.label} PR is Ready: {pr.url}", flush=True)
        emit_issue_result(
            issue.result_id,
            delivery.outcome,
            delivery.reviews,
            pr.number,
            pr.url,
            warnings=warnings,
            label=issue.label,
        )
        record_issue_handoff_result(
            issue.result_id, issue.label, pr, delivery.outcome, delivery.reviews, warnings
        )
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
    print(f"Merged {qualifier} {issue.label} PR: {merged.pr.url}", flush=True)
    emit_issue_result(
        issue.result_id,
        delivery.outcome,
        delivery.reviews,
        merged.pr.number,
        merged.pr.url,
        warnings=warnings,
        label=issue.label,
    )
    record_issue_handoff_result(
        issue.result_id,
        issue.label,
        merged.pr,
        delivery.outcome,
        delivery.reviews,
        warnings,
    )
    return merged.pr


def planner_work_item_json(issue: Issue) -> int | dict[str, str]:
    if issue.number is not None:
        return issue.number
    assert issue.task_id is not None and issue.task is not None
    return {"id": issue.task_id, "task": issue.task}


def planner_prompt(plan: WorkItemPlan, config: Config) -> str:
    processed = [planner_work_item_json(issue) for issue in plan.items[: plan.position]]
    remaining = [planner_work_item_json(issue) for issue in plan.remaining]
    one_shot_context = ""
    if config.one_shot_issue is not None:
        one_shot_context = f"""This is a one-shot run sourced from GitHub Issue
#{config.one_shot_issue}. Before deciding, read it with `gh issue view
{config.one_shot_issue} --repo {config.slug}`. Manage its delivery by decomposing
the remaining work into short inline mini tasks. Each task must state its purpose
and any non-negotiable design decision, while leaving implementation detail to
the implementer. Do not create GitHub Issues or implement the source Issue as one
undivided work item.

"""
    return f"""Review the workflow-owned work-item plan before its next dispatch.
You are the planning role only: do not edit files, implement work, or mutate Git
or GitHub. Inspect repository and GitHub state read-only when useful. Preserve
the current plan unless progress provides a concrete reason to add necessary
work, refine a pending inline mini task, or skip obsolete/redundant pending work.
Never update or skip a processed item. GitHub Issue work uses its positive number;
inline work uses a stable lowercase kebab-case ID and a concise authoritative task.

{one_shot_context}Repository: {config.slug}
Integration branch: {config.integration_branch}
Processed work items: {json.dumps(processed, ensure_ascii=False)}
Pending work items: {json.dumps(remaining, ensure_ascii=False)}

Return exactly one JSON object with keys "actions" and "complete". Actions run
in order and have one of these exact shapes:
- {{"action":"add","item":123}}
- {{"action":"add","item":{{"id":"task-id","task":"instruction"}}}}
- {{"action":"update","key":"task-id","task":"revised instruction"}}
- {{"action":"skip","key":123}}
- {{"action":"skip","key":"task-id"}}
Use complete=true only when no pending or newly added work remains and the
workflow should proceed to whole-version delivery. Otherwise use complete=false.
Do not use Markdown fences or add explanation outside the JSON object."""


def planner_inline_issue(task_id: object, task: object) -> Issue:
    if (
        not isinstance(task_id, str)
        or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", task_id) is None
        or len(task_id) > 50
    ):
        raise WorkerFailure("planner mini-task ID is invalid")
    task_has_surrogate = isinstance(task, str) and any(
        0xD800 <= ord(character) <= 0xDFFF for character in task
    )
    if (
        not isinstance(task, str)
        or not task
        or task != task.strip()
        or "\0" in task
        or len(task) > 4000
        or task_has_surrogate
    ):
        raise WorkerFailure("planner mini-task instruction is invalid")
    fingerprint = hashlib.sha256(task.encode()).hexdigest()
    return Issue(
        None,
        f"feature/work-item-{task_id}",
        task_id,
        task,
        fingerprint,
    )


def planner_key(value: object) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise WorkerFailure("planner work-item key is invalid")
    if isinstance(value, int):
        if value < 1:
            raise WorkerFailure("planner Issue key is invalid")
        return value
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is None or len(value) > 50:
        raise WorkerFailure("planner mini-task key is invalid")
    return value


def planner_added_issue(value: object) -> Issue:
    if isinstance(value, bool):
        raise WorkerFailure("planner added work item is invalid")
    if isinstance(value, int):
        if value < 1:
            raise WorkerFailure("planner added Issue is invalid")
        return Issue(value, f"feature/issue-{value}")
    if not isinstance(value, dict) or set(value) != {"id", "task"}:
        raise WorkerFailure("planner added work item is invalid")
    return planner_inline_issue(value["id"], value["task"])


def apply_planner_decision(plan: WorkItemPlan, source: str) -> bool:
    try:
        decision = json.loads(source)
    except json.JSONDecodeError as exc:
        raise WorkerFailure(f"planner returned invalid JSON: {exc.msg}") from exc
    if not isinstance(decision, dict) or set(decision) != {"actions", "complete"}:
        raise WorkerFailure("planner decision must contain only actions and complete")
    actions = decision["actions"]
    complete = decision["complete"]
    if (
        not isinstance(actions, list)
        or len(actions) > MAX_PLANNER_ACTIONS
        or not isinstance(complete, bool)
    ):
        raise WorkerFailure("planner decision has invalid actions or complete value")

    candidate = WorkItemPlan(plan.config)
    candidate.items = list(plan.items)
    candidate.position = plan.position
    try:
        for action in actions:
            if not isinstance(action, dict) or not isinstance(
                action.get("action"), str
            ):
                raise WorkerFailure("planner action is invalid")
            kind = action["action"]
            if kind == "add" and set(action) == {"action", "item"}:
                candidate.add(planner_added_issue(action["item"]))
            elif kind == "update" and set(action) == {"action", "key", "task"}:
                key = planner_key(action["key"])
                index = candidate._remaining_index(key)
                current = candidate.items[index]
                if current.task_id is None:
                    raise WorkerFailure("planner can update only an inline mini task")
                candidate.update(
                    key, planner_inline_issue(current.task_id, action["task"])
                )
            elif kind == "skip" and set(action) == {"action", "key"}:
                candidate.skip(planner_key(action["key"]))
            else:
                raise WorkerFailure("planner action has an unsupported shape")
    except ValueError as exc:
        raise WorkerFailure(f"planner decision is invalid: {exc}") from exc

    if complete and candidate.remaining:
        raise WorkerFailure("planner cannot complete while work items remain")
    if not complete and not candidate.remaining:
        raise WorkerFailure("planner must add work or complete an empty plan")
    candidate.finalized = complete
    validate_work_item_plan_capacity(candidate)
    plan.items = candidate.items
    plan.finalized = complete
    return complete


def plan_seed_fingerprint(config: Config) -> str:
    seed_value: object = [planner_work_item_json(issue) for issue in config.issues]
    if config.one_shot_issue is not None:
        seed_value = {"one_shot_issue": config.one_shot_issue, "items": seed_value}
    seed = json.dumps(
        seed_value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(seed.encode()).hexdigest()


def work_item_plan_source(
    plan: WorkItemPlan, *, position: int, finalized: bool
) -> str:
    payload = {
        "version": 1,
        "seed_sha256": plan_seed_fingerprint(plan.config),
        "items": [planner_work_item_json(issue) for issue in plan.items],
        "position": position,
        "finalized": finalized,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def require_work_item_plan_size(source: str) -> None:
    if len(source.encode()) > MAX_PLAN_STATE_CHARS:
        raise WorkerFailure("work-item plan recovery state exceeds its size limit")


def validate_work_item_plan_capacity(plan: WorkItemPlan) -> None:
    source = work_item_plan_source(
        plan,
        position=len(plan.items),
        finalized=False,
    )
    require_work_item_plan_size(source)


def serialized_work_item_plan(plan: WorkItemPlan) -> str:
    source = work_item_plan_source(
        plan,
        position=plan.position,
        finalized=plan.finalized,
    )
    require_work_item_plan_size(source)
    encoded = base64.urlsafe_b64encode(source.encode()).decode()
    return f"<!-- {WORK_ITEM_PLAN_MARKER}{encoded} -->"


def work_item_plan_from_body(body: str, config: Config) -> WorkItemPlan:
    prefix = f"<!-- {WORK_ITEM_PLAN_MARKER}"
    markers = [
        line.strip()
        for line in body.splitlines()
        if line.strip().startswith(prefix)
    ]
    if not markers:
        raise WorkerFailure("Base PR is missing work-item plan recovery state")
    if len(markers) != 1 or not markers[0].endswith(" -->"):
        raise WorkerFailure("Base PR has ambiguous work-item plan recovery state")
    encoded = markers[0][len(prefix) : -len(" -->")]
    try:
        source = base64.b64decode(encoded, altchars=b"-_", validate=True).decode()
        payload = json.loads(source)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkerFailure("Base PR work-item plan recovery state is invalid") from exc
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if source != canonical or len(source.encode()) > MAX_PLAN_STATE_CHARS:
        raise WorkerFailure("Base PR work-item plan recovery state is not canonical")
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "seed_sha256",
        "items",
        "position",
        "finalized",
    }:
        raise WorkerFailure("Base PR work-item plan recovery state has invalid fields")
    if payload["version"] != 1 or payload["seed_sha256"] != plan_seed_fingerprint(
        config
    ):
        raise WorkerFailure("Base PR work-item plan does not match the workflow seed")
    items = payload["items"]
    position = payload["position"]
    finalized = payload["finalized"]
    if (
        not isinstance(items, list)
        or isinstance(position, bool)
        or not isinstance(position, int)
        or not 0 <= position <= len(items)
        or not isinstance(finalized, bool)
    ):
        raise WorkerFailure("Base PR work-item plan recovery values are invalid")
    plan = WorkItemPlan(config)
    try:
        plan.items = [planner_added_issue(item) for item in items]
        plan._validate(plan.items)
    except ValueError as exc:
        raise WorkerFailure(f"Base PR work-item plan is invalid: {exc}") from exc
    plan.position = position
    plan.finalized = finalized
    if finalized and position != len(plan.items):
        raise WorkerFailure("Base PR work-item plan completion state is inconsistent")
    return plan


def with_work_item_plan(body: str, plan: WorkItemPlan) -> str:
    marker = serialized_work_item_plan(plan)
    prefix = f"<!-- {WORK_ITEM_PLAN_MARKER}"
    lines = body.splitlines()
    indexes = [
        index for index, line in enumerate(lines) if line.strip().startswith(prefix)
    ]
    if len(indexes) > 1:
        raise WorkerFailure("Base PR has ambiguous work-item plan recovery state")
    if indexes:
        lines[indexes[0]] = marker
        return "\n".join(lines)
    return f"{body.rstrip()}\n\n{marker}" if body.strip() else marker


def prepare_work_item_plan_pr(
    config: Config,
    repo: GitRepository,
    github: GitHubRepository,
) -> tuple[PullRequestState, WorkItemPlan]:
    integration = repo.synchronize_branch(config.integration_branch)
    final = repo.inspect_branch(config.main_branch)
    if integration.remote_sha is None or final.remote_sha is None:
        raise WorkerFailure("integration or final remote branch is missing")
    pr = inspect_pr(github, head=config.integration_branch, base=config.main_branch)
    merged = github.find_pr(
        head=config.integration_branch, base=config.main_branch, state="MERGED"
    )
    if merged is not None:
        if pr is not None:
            raise WorkerFailure("merged final delivery also has an open same-head PR")
        merged = github.require_pr(
            number=merged.number,
            head=config.integration_branch,
            base=config.main_branch,
            state="MERGED",
            expected_head_sha=integration.remote_sha,
        )
        if f"<!-- {WORK_ITEM_PLAN_MARKER}" in merged.body:
            plan = work_item_plan_from_body(merged.body, config)
        else:
            # A merged Base PR from before durable dynamic plans authoritatively
            # completed the immutable seed; it cannot contain dynamic decisions.
            plan = WorkItemPlan(config)
            plan.position = len(plan.items)
            plan.finalized = True
        if not plan.finalized:
            raise WorkerFailure("merged final PR has an unfinished work-item plan")
        return merged, plan
    if pr is None:
        initial_plan = WorkItemPlan(config)
        pr = github.create_draft_pr(
            head=config.integration_branch,
            base=config.main_branch,
            expected_head_sha=integration.remote_sha,
            expected_base_sha=final.remote_sha,
            title=f"Integrate {config.integration_branch}",
            body=with_work_item_plan(
                "Sequential integration; Ready only after whole-version checks."
                f"{one_shot_pr_notes(config)}"
                f"{policy_pr_notes(config)}",
                initial_plan,
            ),
            correlation_id=run_correlation("integration-pr"),
        )
        plan = initial_plan
    else:
        plan = work_item_plan_from_body(pr.body, config)
        pr = return_to_draft_for_review(
            github,
            pr,
            head=config.integration_branch,
            base=config.main_branch,
        )
        pr = ensure_base_pr_one_shot_notes(github, pr, config)
    pr = github.require_pr(
        number=pr.number,
        head=config.integration_branch,
        base=config.main_branch,
        state="OPEN",
        expected_head_sha=integration.remote_sha,
        expected_base_sha=final.remote_sha,
        draft=True,
    )
    emit_run_pr(pr.number, pr.url)
    return pr, plan


def persist_work_item_plan(
    plan: WorkItemPlan,
    config: Config,
    repo: GitRepository,
    github: GitHubRepository,
    pr: PullRequestState,
) -> PullRequestState:
    if pr.state == "MERGED":
        if with_work_item_plan(pr.body, plan) != pr.body:
            raise WorkerFailure("cannot change work-item plan after final PR merge")
        return pr
    integration = repo.synchronize_branch(config.integration_branch)
    final = repo.inspect_branch(config.main_branch)
    if integration.remote_sha is None or final.remote_sha is None:
        raise WorkerFailure("integration or final remote branch is missing")
    current = github.require_pr(
        number=pr.number,
        head=config.integration_branch,
        base=config.main_branch,
        state="OPEN",
        expected_head_sha=integration.remote_sha,
        expected_base_sha=final.remote_sha,
        draft=True,
    )
    body = with_work_item_plan(current.body, plan)
    if body == current.body:
        return current
    return github.update_pr_body(
        current.number,
        body=body,
        expected_head=config.integration_branch,
        expected_head_sha=current.head_sha,
        expected_base=config.main_branch,
        expected_base_sha=current.base_sha,
    )


def process_work_items(
    config: Config,
    client: PurpleMuxCLIClient,
    repo: GitRepository,
    github: GitHubRepository,
    plan_pr: PullRequestState,
    plan: WorkItemPlan,
) -> tuple[Issue, ...]:
    for recovered_issue in plan.items[: plan.position]:
        run_outline_step(
            recovered_issue.label,
            lambda issue=recovered_issue: process_issue(
                issue, config, client, repo, github
            ),
        )
    if plan.finalized:
        return plan.snapshot
    planner = create_agent(
        client,
        config,
        agent_type=REVIEWER_AGENT,
        name="Work-item planner",
    )
    for planner_turn in range(1, MAX_PLANNER_TURNS + 1):
        decision = run_turn(
            client,
            planner,
            "Work-item planning",
            policy_context(config, scope="work-item planning")
            + planner_prompt(plan, config),
            iteration=planner_turn,
        )
        complete = apply_planner_decision(plan, decision)
        plan_pr = persist_work_item_plan(plan, config, repo, github, plan_pr)
        if complete:
            return plan.snapshot
        issue = plan.take_next()
        assert issue is not None
        plan_pr = persist_work_item_plan(plan, config, repo, github, plan_pr)
        run_outline_step(
            issue.label,
            lambda issue=issue: process_issue(issue, config, client, repo, github),
        )
    raise WorkerFailure(f"work-item planning exceeded {MAX_PLANNER_TURNS} turns")


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
            + f"Review the whole version at exact head {pr.head_sha} against final "
            f"base {pr.base_sha}. Examine integration consistency across Issues, "
            "duplication between their implementations, cross-feature interactions "
            "and regressions, and whether shared versus feature-specific "
            "responsibilities are placed at the right boundaries. Also review the "
            "combined version for correctness, safety, and missing integration "
            "coverage. Return APPROVED or CHANGES_REQUESTED first, followed by "
            "actionable findings; do not mutate anything.",
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
                    implementer_prompt(
                        policy_context(config, scope="whole-version fixes")
                        + f"""Re-evaluate every finding. If warranted, fix, test, commit,
and leave the worktree clean. If not, leave it clean and explain why.\n\n{result}""",
                    ),
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
            delivery = ReviewDelivery(
                "approved", current.head_sha, current.base_sha, review_number
            )
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
                "continued_with_warning",
                current.head_sha,
                current.base_sha,
                review_number,
                (warning,),
            )
        break
    if delivery is None:
        raise WorkerFailure("whole-version review ended without a review outcome")
    return pr, delivery


def integration_delivery(
    config: Config,
    work_items: tuple[Issue, ...],
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
        emit_whole_review_result(
            "skipped",
            0,
            warnings=summary_warnings(None),
        )
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
                f"{one_shot_pr_notes(config)}"
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
        delivery = ReviewDelivery("skipped", pr.head_sha, pr.base_sha, 0)

    emit_whole_review_result(
        delivery.outcome,
        delivery.reviews,
        warnings=summary_warnings(None, delivery.warnings),
    )

    def finalize() -> PullRequestState:
        if delivery.outcome == "continued_with_warning":
            delivered = require_warning_delivery(
                repo,
                github,
                pr,
                head=config.integration_branch,
                base=config.main_branch,
                expected_head_sha=delivery.head_sha,
                expected_base_sha=delivery.base_sha,
            )
        else:
            delivered = github.set_draft(
                pr.number,
                draft=False,
                expected_head=config.integration_branch,
                expected_head_sha=delivery.head_sha,
                expected_base=config.main_branch,
                expected_base_sha=delivery.base_sha,
            )
        delivered = update_base_pr_human_handoff(
            config, work_items, client, github, delivered, delivery
        )
        if delivery.outcome == "continued_with_warning":
            return delivered
        if not MERGE_FINAL:
            return delivered
        merged = merge_pr_and_advance(
            repo,
            github,
            number=delivered.number,
            head=config.integration_branch,
            head_sha=delivered.head_sha,
            base=config.main_branch,
            base_sha=delivered.base_sha,
        )
        return merged.pr

    return run_outline_step("Final integration PR", finalize)


def main() -> None:
    config = parse_args()
    emit_issue_driven_context(
        config.slug,
        config.integration_branch,
        config.main_branch,
        policy_issue=config.policy_issue,
    )
    repo = GitRepository.open(
        config.repo,
        expected_github_slug=config.slug,
        command_timeout_seconds=COMMAND_TIMEOUT,
    )
    github = GitHubRepository.open(config.slug, command_timeout_seconds=COMMAND_TIMEOUT)
    client = create_runtime(config)
    plan_pr, plan = prepare_work_item_plan_pr(config, repo, github)
    work_items = run_outline_step(
        "Work items",
        lambda: process_work_items(config, client, repo, github, plan_pr, plan),
    )
    ready = integration_delivery(config, work_items, client, repo, github)
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
