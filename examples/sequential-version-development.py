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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeVar

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
    WorkerInterrupted,
    agent_commit_coauthor,
    emit_finding,
    emit_issue_driven_context,
    emit_issue_driven_repositories,
    emit_issue_navigation,
    emit_issue_result,
    emit_planner_skip,
    emit_run_pr,
    emit_step,
    emit_whole_review_result,
    inspect_issue_driven_work_item_topology,
    reconcile_inline_task_pr_body,
    recover_issue_driven_work_item_topology,
    require_inline_task_pr_fingerprint,
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
MAX_REVIEWS = 4
MAX_SCOPE_REVIEWS = 6
MAX_WORK_ITEMS = 200
MAX_PLANNER_TURNS = MAX_WORK_ITEMS + 1
MAX_PLANNER_ACTIONS = 100
MAX_PLANNER_RATIONALE_BYTES = 2_000
MAX_PLAN_STATE_CHARS = 32_000
MAX_MACHINE_OUTPUT_CORRECTIONS = 2
MAX_RECOVERY_STATE_BYTES = 32_000
MAX_RECOVERY_REPORT_BYTES = 2_000
MAX_REPOSITORY_RECOVERIES = 2
IMPLEMENTER_AGENT = "codex"
REVIEWER_AGENT = "codex"
WORKFLOW_POLICY_ISSUE = None
SCENARIOS: tuple[str, ...] = ()
READY_TIMEOUT = 120
TURN_TIMEOUT = 7200
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
REVIEWER_CHECKOUT_GUARD = (
    "Never change the checkout: do not run git checkout, git switch, git restore, "
    "gh pr checkout, git rebase, or git bisect. Inspect the diff with git diff, "
    "git show, or gh pr diff only."
)


class MissingReviewAudit(WorkerFailure):
    """A recorded review cannot be given its fix disposition."""


POLICY_CONFLICT_MARKER = "POLICY_CONFLICT:"
POLICY_CONFLICT_PR_MARKER = "agent-workflow-manager:policy-conflict:"
INLINE_TASK_FINGERPRINT_MARKER = "agent-workflow-manager:inline-task-sha256:"
POLICY_CONFLICT_WARNINGS: list[tuple[int | str | None, str]] = []
HUMAN_HANDOFF_START = "<!-- agent-workflow-manager:human-handoff:start -->"
HUMAN_HANDOFF_END = "<!-- agent-workflow-manager:human-handoff:end -->"
MAX_HUMAN_HANDOFF_CHARS = 12_000
WORK_ITEM_PLAN_MARKER = "agent-workflow-manager:work-item-plan:"
REVIEW_AUDIT_START = "<!-- agent-workflow-manager:review-audit:start -->"
REVIEW_AUDIT_END = "<!-- agent-workflow-manager:review-audit:end -->"
REVIEW_AUDIT_MARKER = "agent-workflow-manager:review-audit:data:"
MAX_PLANNER_POLICY_CONFLICTS = 3
MAX_POLICY_CONFLICT_DETAIL_CHARS = 500
MAX_POLICY_CONFLICT_WARNINGS = 8
MAX_REVIEW_AUDIT_RECORDS = 16
MAX_REVIEW_AUDIT_BYTES = 16_000
MIN_REVIEW_AUDIT_RESERVE_BYTES = 8_000
MAX_REVIEW_FINDINGS = 3
MAX_REVIEW_FINDING_BYTES = 200
MAX_BASE_PR_BODY_BYTES = 65_536
REVIEWER_AUDIT_GUARD = (
    'Return exactly one JSON object with keys "verdict", "findings", and '
    '"policy_conflicts". verdict must be "APPROVED" or "CHANGES_REQUESTED"; '
    f"findings must contain at most {MAX_REVIEW_FINDINGS} concise, single-line "
    f"actionable strings of at most {MAX_REVIEW_FINDING_BYTES} UTF-8 bytes; "
    f"policy_conflicts must contain at most {MAX_PLANNER_POLICY_CONFLICTS} "
    f"concise strings of at most {MAX_POLICY_CONFLICT_DETAIL_CHARS} UTF-8 bytes "
    "and must be empty unless a configured policy Issue clearly conflicts. Do "
    "not use Markdown fences or include raw logs, environment values, "
    "credentials, tokens, secrets, or any extra keys or prose."
)


def terminal_progress(
    event: str,
    subject: str,
    *,
    iteration: int | None = None,
    detail: str | None = None,
) -> None:
    """Show concise human progress without making terminal output authoritative."""
    iteration_text = f" (iteration {iteration})" if iteration is not None else ""
    detail_text = f": {detail}" if detail is not None else ""
    print(f"[workflow] {event} {subject}{iteration_text}{detail_text}", flush=True)


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


@dataclass(frozen=True)
class AgentTurnTimeoutWarning:
    result_scope: int | str | None
    turn_name: str
    message: str


AGENT_TURN_TIMEOUT_WARNINGS: list[AgentTurnTimeoutWarning] = []


@dataclass(frozen=True)
class PlannerSkip:
    issue: Issue
    reason: str


@dataclass
class WorkItemPlan:
    """Mutable work-item order owned by this plain-Python workflow."""

    config: Config
    items: list[Issue] = field(init=False)
    position: int = 0
    finalized: bool = False
    persisted_source: str | None = None
    skipped: list[PlannerSkip] = field(default_factory=list)
    active: Issue | None = field(default=None, repr=False)

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
        if self.config.one_shot_issue is not None and any(
            issue.number is not None for issue in issues
        ):
            raise ValueError("one-shot plans can contain only inline mini tasks")

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

    def skip(self, identity: int | str, reason: str | None = None) -> Issue:
        issue = self.items.pop(self._remaining_index(identity))
        if reason is not None:
            self.skipped.append(PlannerSkip(issue, reason))
        return issue

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
class ReviewAuditRecord:
    audit_id: str
    role: str
    round: int
    verdict: Literal["APPROVED", "CHANGES_REQUESTED"]
    reviewed_sha: str
    findings: tuple[str, ...]
    fix_disposition: str
    fix_sha: str | None = None


@dataclass(frozen=True)
class ReviewAssessment:
    verdict: Literal["APPROVED", "CHANGES_REQUESTED"]
    findings: tuple[str, ...]
    policy_conflicts: tuple[str, ...]


@dataclass(frozen=True)
class PlannerDecision:
    complete: bool
    policy_conflicts: tuple[str, ...] = ()
    skipped: tuple[PlannerSkip, ...] = ()
    rationale: str | None = None
    changes: tuple[str, ...] = ()


@dataclass(frozen=True)
class IssueHandoffResult:
    issue: int | str
    label: str
    pr_number: int
    pr_url: str
    outcome: str
    reviews: int
    warnings: tuple[str, ...] = ()


@dataclass
class RepositoryDelivery:
    config: Config
    work_items: tuple[Issue, ...]
    client: PurpleMuxCLIClient
    repo: GitRepository
    github: GitHubRepository
    pr: PullRequestState | None
    review: ReviewDelivery
    issue_results: tuple[IssueHandoffResult, ...]
    warnings: tuple[str, ...]


ISSUE_HANDOFF_RESULTS: list[IssueHandoffResult] = []
COMPLETED_ISSUE_PRS: dict[int | str, PullRequestState] = {}


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


def parse_repository_configs():
    yield parse_args()


def issue_driven_repository_declarations():
    return ()


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
    warning_scope: int | str | None = None,
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
    terminal_progress("START", name, iteration=iteration)

    def warn_busy_timeout(warning: str) -> None:
        contextual = AgentTurnTimeoutWarning(
            warning_scope, name, f"{name}: {warning}"
        )
        if contextual not in AGENT_TURN_TIMEOUT_WARNINGS:
            AGENT_TURN_TIMEOUT_WARNINGS.append(contextual)
        print(f"WARN: {contextual.message}", flush=True)
        emit_finding("runtime", contextual.message, status="warning")

    try:
        client.wait_until_ready(tab, READY_TIMEOUT)
        client.send_input(tab, prompt)
        client.wait_for_turn_completion(
            tab, TURN_TIMEOUT, on_busy_timeout=warn_busy_timeout
        )
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
        terminal_progress("FAILED", name, iteration=iteration, detail=short_error(exc))
        raise
    emit_step(
        name,
        "completed",
        iteration=iteration,
        workspace=client.workspace_id,
        tab=tab,
        **navigation,
    )
    terminal_progress("DONE", name, iteration=iteration)
    return result


ValidatedOutput = TypeVar("ValidatedOutput")


def run_validated_turn(
    client: PurpleMuxCLIClient,
    tab: str,
    name: str,
    prompt: str,
    validator: Callable[[str], ValidatedOutput],
    *,
    iteration: int | None = None,
    pr: PullRequestState | None = None,
    warning_scope: int | str | None = None,
) -> tuple[str, ValidatedOutput]:
    """Retry an invalid machine-readable response in the same agent session."""
    result = run_turn(
        client,
        tab,
        name,
        prompt,
        iteration=iteration,
        pr=pr,
        warning_scope=warning_scope,
    )
    for correction in range(MAX_MACHINE_OUTPUT_CORRECTIONS + 1):
        try:
            return result, validator(result)
        except WorkerFailure as exc:
            if correction == MAX_MACHINE_OUTPUT_CORRECTIONS:
                raise WorkerFailure(
                    f"{name} returned invalid output after "
                    f"{MAX_MACHINE_OUTPUT_CORRECTIONS} correction attempts: "
                    f"{short_error(exc)}"
                ) from exc
            validation_error = short_error(exc)
            result = run_turn(
                client,
                tab,
                f"{name} output correction",
                "The previous response violated its machine-readable output "
                f"contract: {validation_error}\n\n"
                "Return the complete corrected response only, following the "
                "original response contract. Correct the output in this same "
                "session; do not repeat the underlying task or mutate any state.",
                iteration=correction + 1,
                pr=pr,
                warning_scope=warning_scope,
            )
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class RecoveryReport:
    repaired: bool
    retry_safe: bool
    summary: str
    evidence: str


def parse_recovery_report(source: str) -> RecoveryReport:
    """Accept only a bounded account with evidence for a safe retry."""
    try:
        source_bytes = source.encode("utf-8")
    except UnicodeError as exc:
        raise WorkerFailure("recovery report is not valid UTF-8") from exc
    if len(source_bytes) > MAX_RECOVERY_REPORT_BYTES:
        raise WorkerFailure("recovery report exceeds its size limit")
    try:
        value = json.loads(source)
    except (UnicodeError, ValueError) as exc:
        raise WorkerFailure("recovery report must be one JSON object") from exc
    if not isinstance(value, dict) or set(value) != {
        "repaired",
        "retry_safe",
        "summary",
        "evidence",
    }:
        raise WorkerFailure("recovery report has invalid fields")
    if type(value["repaired"]) is not bool or type(value["retry_safe"]) is not bool:
        raise WorkerFailure("recovery report decisions must be booleans")
    for field_name in ("summary", "evidence"):
        field_value = value[field_name]
        if (
            not isinstance(field_value, str)
            or not field_value
            or field_value != field_value.strip()
            or any(
                character in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
                for character in field_value
            )
            or "\0" in field_value
        ):
            raise WorkerFailure(f"recovery report {field_name} is invalid")
        try:
            field_bytes = field_value.encode("utf-8")
        except UnicodeError as exc:
            raise WorkerFailure(f"recovery report {field_name} is invalid UTF-8") from exc
        if len(field_bytes) > 500:
            raise WorkerFailure(f"recovery report {field_name} is too long")
    if value["retry_safe"] and not value["repaired"]:
        raise WorkerFailure("recovery cannot recommend retry without a repair")
    return RecoveryReport(**value)


def recover_error(
    client: PurpleMuxCLIClient,
    config: Config,
    error: BaseException,
    authoritative_state: str,
) -> RecoveryReport:
    """Start a dedicated recovery Agent for this error, never a resident agent."""
    if not isinstance(authoritative_state, str) or not authoritative_state.strip():
        raise WorkerFailure("recovery requires current authoritative state")
    if len(authoritative_state.encode("utf-8")) > MAX_RECOVERY_STATE_BYTES:
        raise WorkerFailure("recovery authoritative state exceeds its size limit")
    agent = create_agent(
        client, config, agent_type=IMPLEMENTER_AGENT, name="Recovery agent"
    )
    try:
        _, report = run_validated_turn(
            client,
            agent,
            "Recovery assessment",
            implementer_prompt(
                "Investigate this workflow error using the current authoritative "
                "state below. Make only a safe, necessary repair, then re-inspect "
                "the affected state. If the outcome is uncertain, report retry_safe "
                "as false. Do not reset, rebase, stash, force-push, merge a work-item "
                "PR, create unrelated PRs, discard ambiguous work, or edit "
                "agent-workflow-manager fingerprint markers. Return exactly one JSON "
                "object with boolean repaired and retry_safe fields and concise "
                "single-line summary and evidence strings (at most 500 UTF-8 bytes "
                "each). Include evidence from the state after repair when "
                "recommending retry. No other fields or prose.\n\n"
                f"Error: {short_error(error)}\n\n"
                f"Current authoritative state:\n{authoritative_state}",
                process="recovery",
            ),
            parse_recovery_report,
        )
        return report
    finally:
        client.close_session(agent)


def implementer_prompt(prompt: str, *, process: str = "implementation") -> str:
    """Add the shared change-boundary policy to an implementation turn."""
    if process not in {"implementation", "reviewer-fix", "cleanup", "recovery"}:
        raise ValueError(f"unsupported implementation process: {process!r}")
    coauthor = agent_commit_coauthor(IMPLEMENTER_AGENT)
    return (
        f"{prompt.rstrip()}\n\n{IMPLEMENTATION_PRINCIPLE}\n\n"
        "Do not create, remove, or edit agent-workflow-manager fingerprint markers; "
        "the workflow owns and reconciles those markers from its persisted "
        "work-item plan.\n\n"
        "Every commit you create must end with these exact Git trailers, preserving "
        "any additional trailers the agent adds:\n"
        f"Co-authored-by: {coauthor}\n"
        f"AWM-Agent: {IMPLEMENTER_AGENT}\n"
        f"AWM-Process: {process}"
    )


def run_outline_step(name: str, action):
    """Run one concrete outline unit while retaining detailed nested progress."""
    emit_step(name, "started")
    terminal_progress("START", name)
    try:
        result = action()
    except BaseException as exc:
        emit_step(name, "failed", error=short_error(exc))
        terminal_progress("FAILED", name, detail=short_error(exc))
        raise
    navigation = (
        {"pr_number": result.number, "pr_url": result.url}
        if isinstance(result, PullRequestState)
        else {}
    )
    emit_step(name, "completed", **navigation)
    terminal_progress("DONE", name)
    return result


_REVIEW_VERDICTS = {"APPROVED", "CHANGES_REQUESTED"}
_REVIEW_FIX_DISPOSITIONS = {
    "pending",
    "not_required",
    "fixed",
    "no_change_after_re_evaluation",
    "review_limit_reached",
    "review_limit_reached_after_head_change",
    "reviewer_changed_head",
    "head_changed_before_disposition",
}


def decision(result: str) -> str:
    return review_assessment(result).verdict


_SECRET_LABEL = (
    r"(?:password|passwd|passphrase|credential|secret|token|client[_ -]?secret|"
    r"api[_ -]?key|access[_ -]?key)"
)
_SECRET_LITERAL = (
    r"(?:\"[^\"\s]{4,}\"|'[^'\s]{4,}'|"
    r"(?=[A-Za-z0-9_./+-]{4,}\b)(?=[A-Za-z0-9_./+-]*[A-Za-z])"
    r"(?=[A-Za-z0-9_./+-]*\d)[A-Za-z0-9_./+-]+\b)"
)
_SENSITIVE_REVIEW_TEXT = re.compile(
    rf"(?i)(\b{_SECRET_LABEL}\b\s*(?:=|:)\s*\S+|"
    rf"\b{_SECRET_LABEL}\b(?:(?![.!?]).){{0,96}}?{_SECRET_LITERAL}|"
    rf"{_SECRET_LITERAL}(?:(?![.!?]).){{0,96}}?\b{_SECRET_LABEL}\b|"
    rf"\b{_SECRET_LABEL}\b\s+(?!(?:is|was)\b)\S+\s+"
    r"(?:(?:is|was)\s+)?(?:exposed|leaked|logged|printed)\b|"
    r"\bbearer\s+(?=[A-Za-z0-9._~+/=-]{8,}\b)"
    r"(?=\S*[0-9._~+/=-])[A-Za-z0-9._~+/=-]+|"
    r"\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@[^\s/]+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:AKIA|ASIA)[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{35}|github_pat_|gh[pousr]_|glpat-|"
    r"xox[baprs]-|sk_(?:live|test)_|sk-[a-z0-9]|"
    r"eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)"
)
_RAW_REVIEW_OUTPUT = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:\$\s|Traceback \(most recent call last\):|"
    r"\d{4}-\d\d-\d\d[ T]"
    r"\d\d:\d\d|FAILED(?:\s|:)|ERROR(?:\s|:)|npm ERR!\s|"
    r"\[(?:DEBUG|ERROR|FATAL|INFO|TRACE|WARN|WARNING)\])"
)
_OPAQUE_SECRET_LIKE_VALUE = re.compile(r"(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_./+=-]{32,}")
_PERSISTENCE_SAFE_REVIEW_TEXT = re.compile(r"^[^\x00-\x1f\x7f<>{}`=]+$")
_PLANNER_LOW_LEVEL_TEXT = re.compile(
    r"(?i)(?:^|[ \t])(?:user|assistant|system|developer|tool|stdout|stderr)\s*:"
)


def _safe_review_text(value: object, *, max_bytes: int) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\n" in value
        or "\0" in value
        or "```" in value
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        return False
    return (
        len(value.encode()) <= max_bytes
        and _PERSISTENCE_SAFE_REVIEW_TEXT.fullmatch(value) is not None
        and _RAW_REVIEW_OUTPUT.search(value) is None
        and _SENSITIVE_REVIEW_TEXT.search(value) is None
        and _OPAQUE_SECRET_LIKE_VALUE.search(value) is None
    )


def review_assessment(result: str) -> ReviewAssessment:
    """Validate the complete bounded review result before durable persistence."""
    try:
        value = json.loads(result)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkerFailure("reviewer response must be one JSON object") from exc
    if not isinstance(value, dict) or set(value) != {
        "verdict",
        "findings",
        "policy_conflicts",
    }:
        raise WorkerFailure(
            "reviewer response must contain only verdict, findings, and "
            "policy_conflicts"
        )
    verdict = value["verdict"]
    findings = value["findings"]
    policy_conflicts = value["policy_conflicts"]
    if not isinstance(verdict, str) or verdict not in _REVIEW_VERDICTS:
        raise WorkerFailure("reviewer verdict must be APPROVED or CHANGES_REQUESTED")
    if not isinstance(findings, list) or len(findings) > MAX_REVIEW_FINDINGS:
        raise WorkerFailure(
            f"reviewer findings must be an array of at most {MAX_REVIEW_FINDINGS} items"
        )
    for finding in findings:
        if not _safe_review_text(finding, max_bytes=MAX_REVIEW_FINDING_BYTES):
            raise WorkerFailure(
                "reviewer findings must be concise single-line actionable text "
                "without logs or secret-like values"
            )
    if (verdict == "APPROVED") != (not findings):
        raise WorkerFailure(
            "APPROVED reviews must have no findings and CHANGES_REQUESTED "
            "reviews must have at least one finding"
        )
    if (
        not isinstance(policy_conflicts, list)
        or len(policy_conflicts) > MAX_PLANNER_POLICY_CONFLICTS
        or any(
            not _safe_review_text(
                conflict, max_bytes=MAX_POLICY_CONFLICT_DETAIL_CHARS
            )
            for conflict in policy_conflicts
        )
    ):
        raise WorkerFailure("reviewer policy_conflicts array is invalid or unsafe")
    return ReviewAssessment(verdict, tuple(findings), tuple(policy_conflicts))


def _review_audit_payload(records: tuple[ReviewAuditRecord, ...]) -> str:
    source = json.dumps(
        [
            {
                "audit_id": record.audit_id,
                "role": record.role,
                "round": record.round,
                "verdict": record.verdict,
                "reviewed_sha": record.reviewed_sha,
                "findings": list(record.findings),
                "fix_disposition": record.fix_disposition,
                "fix_sha": record.fix_sha,
            }
            for record in records
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.b64encode(source.encode()).decode("ascii")


def review_audit_from_body(body: str) -> tuple[ReviewAuditRecord, ...]:
    prefix = f"<!-- {REVIEW_AUDIT_MARKER}"
    starts = [match.start() for match in re.finditer(re.escape(REVIEW_AUDIT_START), body)]
    ends = [match.start() for match in re.finditer(re.escape(REVIEW_AUDIT_END), body)]
    payloads = [match.start() for match in re.finditer(re.escape(prefix), body)]
    marker_matches = list(
        re.finditer(rf"(?m)^[ \t]*{re.escape(prefix)}.*? -->[ \t]*$", body)
    )
    if not starts and not ends and not payloads:
        return ()
    if (
        len(starts) != 1
        or len(ends) != 1
        or len(payloads) != 1
        or len(marker_matches) != 1
    ):
        raise WorkerFailure("PR has ambiguous review audit markers")
    marker_match = marker_matches[0]
    if not starts[0] < marker_match.start() < marker_match.end() <= ends[0]:
        raise WorkerFailure("PR has invalid review audit section markers")
    marker = marker_match.group().strip()
    encoded = marker[len(prefix) : -len(" -->")]
    try:
        source = base64.b64decode(encoded, validate=True).decode("utf-8")
        values = json.loads(source)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkerFailure("PR review audit marker is invalid") from exc
    if not isinstance(values, list) or len(values) > MAX_REVIEW_AUDIT_RECORDS:
        raise WorkerFailure("PR review audit exceeds its record bound")
    records: list[ReviewAuditRecord] = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {
            "audit_id",
            "role",
            "round",
            "verdict",
            "reviewed_sha",
            "findings",
            "fix_disposition",
            "fix_sha",
        }:
            raise WorkerFailure("PR review audit record has an invalid schema")
        findings = value["findings"]
        if (
            not isinstance(value["audit_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", value["audit_id"])
            or not isinstance(value["role"], str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", value["role"])
            or not isinstance(value["round"], int)
            or isinstance(value["round"], bool)
            or value["round"] < 1
            or not isinstance(value["verdict"], str)
            or value["verdict"] not in _REVIEW_VERDICTS
            or not isinstance(value["reviewed_sha"], str)
            or not 1 <= len(value["reviewed_sha"]) <= 128
            or re.fullmatch(r"[A-Za-z0-9._-]+", value["reviewed_sha"]) is None
            or not isinstance(findings, list)
            or len(findings) > MAX_REVIEW_FINDINGS
            or any(
                not _safe_review_text(
                    finding, max_bytes=MAX_REVIEW_FINDING_BYTES
                )
                for finding in findings
            )
            or (value["verdict"] == "APPROVED") != (not findings)
            or not isinstance(value["fix_disposition"], str)
            or value["fix_disposition"] not in _REVIEW_FIX_DISPOSITIONS
            or (
                value["fix_sha"] is not None
                and (
                    not isinstance(value["fix_sha"], str)
                    or not 1 <= len(value["fix_sha"]) <= 128
                    or re.fullmatch(r"[A-Za-z0-9._-]+", value["fix_sha"]) is None
                )
            )
        ):
            raise WorkerFailure("PR review audit record contains invalid values")
        records.append(
            ReviewAuditRecord(
                value["audit_id"],
                value["role"],
                value["round"],
                value["verdict"],
                value["reviewed_sha"],
                tuple(findings),
                value["fix_disposition"],
                value["fix_sha"],
            )
        )
    if len({record.audit_id for record in records}) != len(records):
        raise WorkerFailure("PR review audit contains duplicate record IDs")
    if _review_audit_payload(tuple(records)) != encoded:
        raise WorkerFailure("PR review audit marker is not canonical")
    return tuple(records)


def new_review_audit(
    role: str, round_number: int, verdict: str, reviewed_sha: str, result: str
) -> ReviewAuditRecord:
    assessment = review_assessment(result)
    if (
        verdict != assessment.verdict
        or not isinstance(role, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,39}", role) is None
        or not isinstance(round_number, int)
        or isinstance(round_number, bool)
        or round_number < 1
        or not isinstance(reviewed_sha, str)
        or not 1 <= len(reviewed_sha) <= 128
        or re.fullmatch(r"[A-Za-z0-9._-]+", reviewed_sha) is None
    ):
        raise WorkerFailure("review audit identity differs from validated review data")
    findings = assessment.findings
    identity = json.dumps(
        [role, round_number, verdict, reviewed_sha, findings],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return ReviewAuditRecord(
        hashlib.sha256(identity.encode()).hexdigest()[:32],
        role,
        round_number,
        verdict,  # type: ignore[arg-type]
        reviewed_sha,
        findings,
        "not_required" if verdict == "APPROVED" else "pending",
    )


def whole_continuation_audit(
    round_number: int, head_sha: str, disposition: str,
) -> ReviewAuditRecord:
    result = json.dumps({
        "verdict": "CHANGES_REQUESTED",
        "findings": ["Whole-version review continued with a warning."],
        "policy_conflicts": [],
    })
    audit = new_review_audit(
        "whole_version_continuation", round_number,
        "CHANGES_REQUESTED", head_sha, result,
    )
    return ReviewAuditRecord(
        audit.audit_id, audit.role, audit.round, audit.verdict,
        audit.reviewed_sha, audit.findings, disposition,
    )


def whole_continuation_warning(disposition: str, limit: int) -> str:
    if disposition == "review_limit_reached_after_head_change":
        return (
            f"Whole-version review limit {limit} was already reached "
            "before the current head could complete review; keeping the Base PR "
            "Draft and continuing without reviewer approval."
        )
    if disposition == "review_limit_reached":
        return (
            f"Whole-version review limit {limit} reached with "
            "CHANGES_REQUESTED; keeping the Base PR Draft and continuing "
            "without reviewer approval."
        )
    if disposition == "no_change_after_re_evaluation":
        return (
            "Whole-version reviewer requested changes, but the fixer "
            "re-evaluated the findings and produced no code changes; "
            "keeping the Base PR Draft and continuing without reviewer approval."
        )
    raise WorkerFailure("whole-version continuation disposition is invalid")


def persist_whole_limit_head_change(
    github: GitHubRepository,
    pr: PullRequestState,
    *,
    round_number: int,
    reviewed_sha: str,
    head: str,
    base: str,
) -> PullRequestState:
    """Keep the exhausted loop count when an individual role changed the head."""
    result = json.dumps({
        "verdict": "CHANGES_REQUESTED",
        "findings": ["Review head changed after the whole-version review limit."],
        "policy_conflicts": [],
    })
    return persist_review_audit(
        github, pr,
        new_review_audit(
            "whole_version_limit", round_number,
            "CHANGES_REQUESTED", reviewed_sha, result,
        ),
        head=head, base=base,
    )


def allocate_review_audit(
    body: str, role: str, verdict: str, reviewed_sha: str, result: str
) -> ReviewAuditRecord:
    """Allocate a durable role round, reusing only an exact pending review."""
    assessment = review_assessment(result)
    records = review_audit_from_body(body)
    interrupted = [
        record
        for record in records
        if record.role == role
        and record.verdict == verdict
        and record.reviewed_sha == reviewed_sha
        and record.findings == assessment.findings
        and record.fix_disposition == "pending"
    ]
    if interrupted:
        round_number = max(record.round for record in interrupted)
    else:
        round_number = max(
            (record.round for record in records if record.role == role), default=0
        ) + 1
    return new_review_audit(role, round_number, verdict, reviewed_sha, result)


def with_review_audit(body: str, record: ReviewAuditRecord) -> str:
    records = list(review_audit_from_body(body))
    unchanged_replay = False
    matching = [existing for existing in records if existing.audit_id == record.audit_id]
    if matching:
        existing = matching[0]
        if (
            existing.role,
            existing.round,
            existing.verdict,
            existing.reviewed_sha,
            existing.findings,
        ) != (
            record.role,
            record.round,
            record.verdict,
            record.reviewed_sha,
            record.findings,
        ):
            raise WorkerFailure("review audit ID has conflicting immutable fields")
        if record == existing:
            unchanged_replay = True
        initial = {"pending", "not_required"}
        if existing.fix_disposition not in initial:
            if record.fix_disposition in initial:
                unchanged_replay = True
            elif (
                record.fix_disposition != existing.fix_disposition
                or record.fix_sha != existing.fix_sha
            ):
                raise WorkerFailure("completed review audit disposition cannot change")
    records = [existing for existing in records if existing.audit_id != record.audit_id]
    records.append(record)
    while len(records) > MAX_REVIEW_AUDIT_RECORDS:
        removable = _removable_review_audit_index(records)
        if removable is None:
            raise WorkerFailure(
                "PR review audit cannot retain every role within its record bound"
            )
        records.pop(removable)
    starts = [match.start() for match in re.finditer(re.escape(REVIEW_AUDIT_START), body)]
    ends = [match.end() for match in re.finditer(re.escape(REVIEW_AUDIT_END), body)]
    if len(starts) > 1 or len(ends) > 1 or bool(starts) != bool(ends):
        raise WorkerFailure("PR has ambiguous review audit section markers")
    if starts:
        if starts[0] >= ends[0]:
            raise WorkerFailure("PR has invalid review audit section markers")
        if unchanged_replay:
            return body
        prefix, suffix = body[: starts[0]], body[ends[0] :]
    else:
        prefix = f"{body.rstrip()}\n\n" if body.strip() else ""
        suffix = ""
    while records:
        marker = (
            f"<!-- {REVIEW_AUDIT_MARKER}"
            f"{_review_audit_payload(tuple(records))} -->"
        )
        lines = [REVIEW_AUDIT_START, "### Review audit"]
        for entry in records:
            disposition = re.sub("_", " ", entry.fix_disposition)
            if entry.fix_sha is not None:
                disposition += f" at `{entry.fix_sha}`"
            lines.append(
                f"- **{re.sub('_', ' ', entry.role)} round {entry.round}:** "
                f"{entry.verdict} at `{entry.reviewed_sha}`; fix: {disposition}."
            )
            lines.extend(
                f"  - {finding.translate(str.maketrans({'<': '&lt;', '>': '&gt;'}))}"
                for finding in entry.findings
            )
        lines.extend((marker, REVIEW_AUDIT_END))
        managed = "\n".join(lines)
        result = f"{prefix}{managed}{suffix}"
        if (
            len(managed.encode()) <= MAX_REVIEW_AUDIT_BYTES
            and len(result.encode()) <= MAX_BASE_PR_BODY_BYTES
        ):
            return result
        removable = _removable_review_audit_index(records)
        if removable is None:
            break
        records.pop(removable)
    raise WorkerFailure(
        "PR review audit cannot retain the latest record for every role within "
        "the shared PR-body byte budget"
    )


def _removable_review_audit_index(records: list[ReviewAuditRecord]) -> int | None:
    """Retain the last role result and the evidence for a changed-head continuation."""
    protected = {max(i for i, entry in enumerate(records) if entry.role == role)
                 for role in {entry.role for entry in records}}
    protected.update(
        i for i, entry in enumerate(records) if entry.fix_disposition == "pending"
    )
    whole_roles = {
        "scenario_gate", "design_principles", "whole_version", "version_readme",
        "whole_version_limit",
    }
    for dispositions in (
        {"review_limit_reached"},
        {"reviewer_changed_head", "head_changed_before_disposition"},
    ):
        candidates = [
            i for i, entry in enumerate(records)
            if entry.role in whole_roles
            and entry.fix_disposition in dispositions
        ]
        if candidates:
            protected.add(candidates[-1])
    return next((i for i in range(len(records)) if i not in protected), None)


def persist_review_audit(
    github: GitHubRepository,
    pr: PullRequestState,
    record: ReviewAuditRecord,
    *,
    head: str,
    base: str,
) -> PullRequestState:
    body = with_review_audit(pr.body, record)
    require_base_pr_body_size(body)
    if body == pr.body:
        return pr
    return github.update_pr_body(
        pr.number,
        body=body,
        expected_head=head,
        expected_head_sha=pr.head_sha,
        expected_base=base,
        expected_base_sha=pr.base_sha,
        draft=True,
    )


def review_audit_disposition(
    github: GitHubRepository,
    pr: PullRequestState,
    audit_id: str,
    disposition: str,
    *,
    head: str,
    base: str,
    fix_sha: str | None = None,
) -> PullRequestState:
    return review_audit_dispositions(
        github,
        pr,
        (audit_id,),
        disposition,
        head=head,
        base=base,
        fix_sha=fix_sha,
    )


def review_audit_dispositions(
    github: GitHubRepository,
    pr: PullRequestState,
    audit_ids: tuple[str, ...],
    disposition: str,
    *,
    head: str,
    base: str,
    fix_sha: str | None = None,
) -> PullRequestState:
    """Apply one transition to a set of records in one durable body update."""
    if disposition not in _REVIEW_FIX_DISPOSITIONS or (
        fix_sha is not None
        and (
            not 1 <= len(fix_sha) <= 128
            or re.fullmatch(r"[A-Za-z0-9._-]+", fix_sha) is None
        )
    ):
        raise WorkerFailure("review audit fix disposition is invalid")
    body = pr.body
    for audit_id in dict.fromkeys(audit_ids):
        records = review_audit_from_body(body)
        matching = [record for record in records if record.audit_id == audit_id]
        if len(matching) != 1:
            raise MissingReviewAudit("review audit record disappeared before fix disposition")
        record = matching[0]
        body = with_review_audit(
            body,
            ReviewAuditRecord(
                record.audit_id,
                record.role,
                record.round,
                record.verdict,
                record.reviewed_sha,
                record.findings,
                disposition,
                fix_sha,
            ),
        )
    require_base_pr_body_size(body)
    if body == pr.body:
        return pr
    return github.update_pr_body(
        pr.number,
        body=body,
        expected_head=head,
        expected_head_sha=pr.head_sha,
        expected_base=base,
        expected_base_sha=pr.base_sha,
        draft=True,
    )


def reconcile_review_audits_after_head_change(
    github: GitHubRepository,
    pr: PullRequestState,
    *,
    head: str,
    base: str,
) -> PullRequestState:
    """Recover audit transitions interrupted after delivery changed the PR head."""
    pending = tuple(
        record.audit_id
        for record in review_audit_from_body(pr.body)
        if record.fix_disposition == "pending" and record.reviewed_sha != pr.head_sha
    )
    return review_audit_dispositions(
        github,
        pr,
        pending,
        "head_changed_before_disposition",
        head=head,
        base=base,
        fix_sha=pr.head_sha,
    )


def policy_context(
    config: Config, *, scope: str, structured_conflicts: bool = False
) -> str:
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
    conflict_instruction = (
        "If you find a clear conflict, continue by following the implementation "
        "Issue and include a concise description in the policy_conflicts array "
        "of the required JSON response. Otherwise return an empty array."
        if structured_conflicts
        else f"""If you find a clear conflict, continue by following the implementation
Issue and include a line starting with {POLICY_CONFLICT_MARKER} that truthfully
describes the conflict."""
    )
    return f"""Before doing anything else, run `gh issue view {config.policy_issue}
--repo {config.slug}` and read policy Issue #{config.policy_issue}. Treat it as
the version-wide design context for {scope},
not as a workflow DSL or a source of ordering, retry, or merge behavior. The
implementation work item remains the primary requirement. {conflict_instruction}
{known_conflicts}

"""


def record_policy_conflict(issue_number: int | str | None, warning: str) -> None:
    for index, (warning_issue, existing) in enumerate(POLICY_CONFLICT_WARNINGS):
        if existing != warning:
            continue
        if warning_issue is None and issue_number is not None:
            POLICY_CONFLICT_WARNINGS[index] = (issue_number, warning)
        return
    if len(POLICY_CONFLICT_WARNINGS) >= MAX_POLICY_CONFLICT_WARNINGS:
        raise WorkerFailure(
            f"policy conflict warning limit {MAX_POLICY_CONFLICT_WARNINGS} exceeded"
        )
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
    try:
        details = review_assessment(result).policy_conflicts
    except WorkerFailure:
        legacy_details: list[str] = []
        for line in result.splitlines():
            marker, separator, detail = line.strip().partition(
                POLICY_CONFLICT_MARKER
            )
            if separator and not marker and detail.strip():
                legacy_details.append(detail.strip())
        details = tuple(legacy_details)
    for detail in details:
        warning = (
            f"Policy Issue #{config.policy_issue} conflicts with {scope}: "
            f"{detail[:MAX_POLICY_CONFLICT_DETAIL_CHARS]}; continuing with the "
            "implementation work item as the primary requirement."
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
        warning.message
        for warning in AGENT_TURN_TIMEOUT_WARNINGS
        if warning.result_scope == issue_number
    )
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
    COMPLETED_ISSUE_PRS[issue] = pr


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
        body = f"{prefix}\n\n{managed}" if prefix else managed
        require_base_pr_body_size(body)
        return body
    start = existing_body.index(HUMAN_HANDOFF_START)
    end = existing_body.index(HUMAN_HANDOFF_END, start) + len(HUMAN_HANDOFF_END)
    body = f"{existing_body[:start]}{managed}{existing_body[end:]}"
    require_base_pr_body_size(body)
    return body


def warn_human_handoff(message: str) -> None:
    warning = f"Base PR human handoff was not updated: {message}"
    print(f"WARN: {warning}", flush=True)
    emit_finding("github", warning, status="warning")


def human_handoff_warnings(delivery: ReviewDelivery) -> tuple[str, ...]:
    warnings = [warning.message for warning in AGENT_TURN_TIMEOUT_WARNINGS]
    warnings.extend(delivery.warnings)
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


def multi_repository_handoff_prompt(
    deliveries: tuple[RepositoryDelivery, ...],
) -> str:
    repositories = []
    for delivery in deliveries:
        config = delivery.config
        base_pr = (
            f"#{delivery.pr.number} {delivery.pr.url}; "
            f"state={'Draft' if delivery.pr.is_draft else delivery.pr.state}"
            if delivery.pr is not None
            else "not created (no implementation changes)"
        )
        issues = "\n".join(
            f"  - {item.label}: PR #{item.pr_number} ({item.pr_url}), "
            f"outcome={item.outcome}, reviews={item.reviews}, "
            f"warning_count={len(item.warnings)}"
            for item in delivery.issue_results
        ) or "  - No implementation Issue result was recorded."
        policy = (
            f"https://github.com/{config.slug}/issues/{config.policy_issue}"
            if config.policy_issue is not None
            else "none"
        )
        one_shot = (
            f"https://github.com/{config.slug}/issues/{config.one_shot_issue}"
            if config.one_shot_issue is not None
            else "none"
        )
        repositories.append(
            f"- repository: {config.slug}\n"
            f"  integration/final: {config.integration_branch} -> {config.main_branch}\n"
            f"  Base PR: {base_pr}\n"
            f"  whole review: outcome={delivery.review.outcome}, "
            f"reviews={delivery.review.reviews}\n"
            f"  one-shot source Issue: {one_shot}\n"
            f"  Policy Issue: {policy}\n"
            f"  implementation results:\n{issues}"
        )
    warning_lines = "\n".join(
        f"- {warning}"
        for warning in dict.fromkeys(
            warning
            for delivery in deliveries
            for warning in delivery.warnings
        )
    ) or "- none"
    return f"""Create one final human handoff Markdown for this multi-repository Run.
You are the Reviewer role Agent selected by reviewer_agent. This turn generates
prose only and does not change any review verdict. Do not edit files, run GitHub
mutations, or change Git/PR state.

Read referenced GitHub Issue bodies and inspect the listed PR diffs when useful.
Do not include raw logs, environment values, credentials, tokens, or secrets.
The handoff must summarize every repository separately and retain every Base PR
and implementation PR URL below.

Authoritative handoff context:
{chr(10).join(repositories)}
- automated verification: each repository's configured final checks passed on
  its exact reviewed head
- warnings:
{warning_lines}

Return only Japanese Markdown, with these headings exactly once and in order:
## 概要
## 主な変更
## 人間による確認
## 自動検証
Add `## 注意事項` only when warnings are listed above. Under 主な変更, group
results by repository. Under 人間による確認, use 1 to 12 unchecked `- [ ]`
items. Each item must describe one concrete, quickly answerable Yes/No
observation, primarily in a browser or real environment. Do not ask a human to
rerun checks already covered by automation and do not require terminal commands.
Keep the entire response concise and under {MAX_HUMAN_HANDOFF_CHARS} characters.
Do not emit HTML comments, code fences, prefaces, or extra headings."""


def update_multi_repository_human_handoffs(
    deliveries: list[RepositoryDelivery],
) -> None:
    open_deliveries = [
        delivery
        for delivery in deliveries
        if delivery.pr is not None and delivery.pr.state == "OPEN"
    ]
    if not open_deliveries:
        return
    warnings = tuple(
        dict.fromkeys(
            warning
            for delivery in deliveries
            for warning in delivery.warnings
        )
    )[:12]
    writer_delivery = open_deliveries[-1]
    assert writer_delivery.pr is not None
    try:
        writer = create_agent(
            writer_delivery.client,
            writer_delivery.config,
            agent_type=REVIEWER_AGENT,
            name="Multi-repository human handoff writer",
        )
        result = run_turn(
            writer_delivery.client,
            writer,
            "Multi-repository human handoff",
            multi_repository_handoff_prompt(tuple(deliveries)),
            pr=writer_delivery.pr,
        )
        handoff = validate_human_handoff(
            result,
            writer_delivery.config,
            has_warnings=bool(warnings),
        )
        required_context = {
            item.config.slug for item in deliveries
        } | {
            item.pr.url
            for item in deliveries
            if item.pr is not None
        } | {
            result.pr_url
            for item in deliveries
            for result in item.issue_results
        }
        if any(value not in handoff for value in required_context):
            raise WorkerFailure(
                "multi-repository handoff lacks a repository or relevant PR URL"
            )
    except Exception as exc:
        warn_human_handoff(short_error(exc))
        return

    for delivery in open_deliveries:
        assert delivery.pr is not None
        try:
            body = with_human_handoff(delivery.pr.body, handoff)
            delivery.pr = delivery.github.update_pr_body(
                delivery.pr.number,
                body=body,
                expected_head=delivery.config.integration_branch,
                expected_head_sha=delivery.pr.head_sha,
                expected_base=delivery.config.main_branch,
                expected_base_sha=delivery.pr.base_sha,
            )
        except MutationOutcomeUnknown:
            raise
        except WorkerFailure as exc:
            warn_human_handoff(
                f"{delivery.config.slug}: {short_error(exc)}"
            )


def finalize_multi_repository_deliveries(
    deliveries: list[RepositoryDelivery],
) -> None:
    update_multi_repository_human_handoffs(deliveries)
    if not MERGE_FINAL:
        return
    for delivery in deliveries:
        pr = delivery.pr
        if pr is None or pr.state != "OPEN" or delivery.review.outcome != "approved":
            continue
        merged = merge_pr_and_advance(
            delivery.repo,
            delivery.github,
            number=pr.number,
            head=delivery.config.integration_branch,
            head_sha=delivery.review.head_sha,
            base=delivery.config.main_branch,
            base_sha=delivery.review.base_sha,
        )
        delivery.pr = merged.pr


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


def ensure_issue_pr_metadata(
    github: GitHubRepository,
    pr: PullRequestState,
    issue: Issue,
    config: Config,
) -> PullRequestState:
    body = (
        pr.body
        if issue.task_fingerprint is None
        else reconcile_inline_task_pr_body(pr, issue.task_fingerprint)
    )
    if config.policy_issue is not None:
        for issue_number, warning in POLICY_CONFLICT_WARNINGS:
            if issue_number != issue.result_id:
                continue
            marker = encoded_policy_conflict_marker(warning)
            if marker not in body:
                body = (
                    f"{body.rstrip()}\n\nPolicy conflict warning: {warning}\n{marker}"
                )
    if body == pr.body:
        return pr
    current = github.update_pr_body(
        pr.number,
        body=body,
        expected_head=issue.branch,
        expected_head_sha=pr.head_sha,
        expected_base=config.integration_branch,
        expected_base_sha=pr.base_sha,
    )
    require_inline_task_pr_fingerprint(current, issue.task_fingerprint)
    return current


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


def require_base_pr_body_size(body: str) -> None:
    if len(body.encode()) > MAX_BASE_PR_BODY_BYTES:
        raise WorkerFailure(
            f"Base PR body exceeds its {MAX_BASE_PR_BODY_BYTES}-byte limit"
        )
    starts = [match.start() for match in re.finditer(re.escape(REVIEW_AUDIT_START), body)]
    ends = [match.end() for match in re.finditer(re.escape(REVIEW_AUDIT_END), body)]
    if len(starts) > 1 or len(ends) > 1 or bool(starts) != bool(ends):
        raise WorkerFailure("PR has ambiguous review audit section markers")
    non_audit_body = (
        f"{body[: starts[0]]}{body[ends[0] :]}" if starts else body
    )
    non_audit_limit = MAX_BASE_PR_BODY_BYTES - MIN_REVIEW_AUDIT_RESERVE_BYTES
    if len(non_audit_body.encode()) > non_audit_limit:
        raise WorkerFailure(
            "PR body leaves less than the reserved "
            f"{MIN_REVIEW_AUDIT_RESERVE_BYTES}-byte review audit budget"
        )


def with_base_pr_policy_notes(body: str, config: Config) -> str:
    if config.policy_issue is None:
        require_base_pr_body_size(body)
        return body
    reference = f"https://github.com/{config.slug}/issues/{config.policy_issue}"
    if reference not in body:
        body = f"{body.rstrip()}{policy_pr_notes(config)}"
    else:
        for _, warning in POLICY_CONFLICT_WARNINGS:
            marker = encoded_policy_conflict_marker(warning)
            if marker not in body:
                body = (
                    f"{body.rstrip()}\n\nPolicy conflict warning: {warning}\n{marker}"
                )
    require_base_pr_body_size(body)
    return body


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
    body = f"{pr.body.rstrip()}{one_shot_pr_notes(config)}"
    require_base_pr_body_size(body)
    return github.update_pr_body(
        pr.number,
        body=body,
        expected_head=config.integration_branch,
        expected_head_sha=pr.head_sha,
        expected_base=config.main_branch,
        expected_base_sha=pr.base_sha,
    )


def ensure_base_pr_policy_notes(
    github: GitHubRepository, pr: PullRequestState, config: Config
) -> PullRequestState:
    body = with_base_pr_policy_notes(pr.body, config)
    if body == pr.body:
        return pr
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
    warning_scope: int | str | None = None,
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
ignored, removed, or could not resolve.""", process="cleanup"),
        iteration=iteration,
        warning_scope=warning_scope,
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
    expected_process: str,
    iteration: int | None = None,
    warning_scope: int | str | None = None,
) -> tuple[str, bool]:
    repo.require_current_branch(branch)
    require_clean_worktree(
        repo,
        client,
        tab,
        context=f"verifying the coding result on {branch!r}",
        iteration=iteration,
        warning_scope=warning_scope,
    )
    result = repo.require_committed_result(
        branch,
        previous_sha=previous_sha,
        allow_unchanged=allow_unchanged,
        expected_agent=IMPLEMENTER_AGENT,
        expected_process=expected_process,
    )
    assert result.local_sha is not None
    emit_finding("git", f"{branch} is clean at {result.local_sha}")
    return result.local_sha, result.local_sha != previous_sha


def issue_prompts(issue: Issue, config: Config) -> tuple[str, str, str]:
    context = policy_context(config, scope=issue.label)
    review_context = policy_context(
        config, scope=issue.label, structured_conflicts=True
    )
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
    scope_review = review_context + f"""Perform only the Scope / Design Review for
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
phase. Do not mutate files or PR state. {REVIEWER_CHECKOUT_GUARD}
{REVIEWER_AUDIT_GUARD}"""
    correctness_review = review_context + f"""Perform only the Correctness Review for
{issue.label} and its PR from {issue.branch} to {config.integration_branch}.
{issue.requirement}
The change scope has already completed Scope / Design Review. Concentrate on
whether that implementation is correct and safe: functional behavior, edge
cases, state and lifecycle consistency, error handling, races or stale state,
Git/GitHub topology, regressions, missing tests, cleanup and resource ownership,
and security or secret handling. Do not reopen scope preferences unless they
cause a concrete correctness problem. Do not mutate files or PR state. Return
only the structured review response described below.
{REVIEWER_CHECKOUT_GUARD}
{REVIEWER_AUDIT_GUARD}"""
    return implementation, scope_review, correctness_review


def prepare_issue(
    repo: GitRepository,
    github: GitHubRepository,
    issue: Issue,
    config: Config,
) -> tuple[PullRequestState | None, str, bool] | PullRequestState:
    open_pr = inspect_pr(github, head=issue.branch, base=config.integration_branch)
    if open_pr is not None:
        require_inline_task_pr_fingerprint(open_pr, issue.task_fingerprint)
    merged = github.find_pr(
        head=issue.branch, base=config.integration_branch, state="MERGED"
    )
    if merged is not None:
        require_inline_task_pr_fingerprint(merged, issue.task_fingerprint)
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
    reconcile_plan_owned_inline_identity: bool = False,
) -> PullRequestState:
    local = repo.require_current_branch(issue.branch)
    assert local.local_sha is not None
    feature = repo.ensure_pushed(issue.branch, expected_local_sha=local.local_sha)
    assert feature.remote_sha is not None
    reconciled_pr_number: int | None = None
    if reconcile_plan_owned_inline_identity and issue.task_fingerprint is not None:
        assert issue.task_id is not None
        reconciled = recover_issue_driven_work_item_topology(
            repo=str(config.repo),
            integration_branch=config.integration_branch,
            issue=(issue.label, issue.branch, issue.task_fingerprint),
            command_timeout_seconds=COMMAND_TIMEOUT,
        )
        if (
            reconciled.classification != "recoverable"
            or reconciled.feature_sha != feature.remote_sha
            or reconciled.integration_sha != expected_base_sha
        ):
            raise WorkerFailure(
                f"{issue.label} topology changed while reconciling its "
                "plan-owned PR identity"
            )
        reconciled_pr_number = reconciled.open_pr_number
    pr = inspect_pr(github, head=issue.branch, base=config.integration_branch)
    if (
        reconcile_plan_owned_inline_identity
        and issue.task_fingerprint is not None
        and (pr.number if pr is not None else None) != reconciled_pr_number
    ):
        raise WorkerFailure(
            f"{issue.label} PR identity changed after fingerprint reconciliation"
        )
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
    require_inline_task_pr_fingerprint(current, issue.task_fingerprint)
    return current


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


def _review_issue_phase(
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
    pr = reconcile_review_audits_after_head_change(
        github,
        pr,
        head=issue.branch,
        base=config.integration_branch,
    )
    role = re.sub("/", "_", phase)
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
        same_head_audits = tuple(
            record.audit_id
            for record in review_audit_from_body(current.body)
            if record.fix_disposition == "pending"
            and record.reviewed_sha == current.head_sha
            and record.role == role
        )
        current = review_audit_dispositions(
            github,
            current,
            same_head_audits,
            "review_limit_reached",
            head=issue.branch,
            base=config.integration_branch,
        )
        print(f"WARN: {warning}", flush=True)
        terminal_progress("WARN CONTINUATION", f"{issue.label} {phase} review")
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
        resumed_audits = tuple(
            record.audit_id
            for record in review_audit_from_body(pr.body)
            if record.fix_disposition == "pending"
            and record.reviewed_sha == pr.head_sha
            and record.role == role
        )
        result, verdict = run_validated_turn(
            client,
            reviewer,
            f"{issue.label} {phase} review",
            f"{prompt}\nReview exact head {pr.head_sha} against base {pr.base_sha}.",
            decision,
            iteration=review_number,
            pr=pr,
            warning_scope=issue.result_id,
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
        audit = allocate_review_audit(
            current.body,
            role,
            verdict,
            current.head_sha,
            result,
        )
        current = persist_review_audit(
            github,
            current,
            audit,
            head=issue.branch,
            base=config.integration_branch,
        )
        reviewed_sha, reviewer_changed = require_agent_result(
            repo,
            client,
            implementer,
            issue.branch,
            current.head_sha,
            allow_unchanged=True,
            expected_process="cleanup",
            iteration=review_number,
            warning_scope=issue.result_id,
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
            pr = ensure_issue_pr_metadata(github, pr, issue, config)
            pending_audits = tuple(
                record.audit_id
                for record in review_audit_from_body(pr.body)
                if record.fix_disposition == "pending"
            )
            pr = review_audit_dispositions(
                github,
                pr,
                pending_audits + (audit.audit_id,),
                "reviewer_changed_head",
                head=issue.branch,
                base=config.integration_branch,
                fix_sha=reviewed_sha,
            )
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
        current = ensure_issue_pr_metadata(github, current, issue, config)
        if verdict == "APPROVED":
            current = review_audit_dispositions(
                github,
                current,
                resumed_audits,
                "no_change_after_re_evaluation",
                head=issue.branch,
                base=config.integration_branch,
            )
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
            current = review_audit_dispositions(
                github,
                current,
                resumed_audits + (audit.audit_id,),
                "review_limit_reached",
                head=issue.branch,
                base=config.integration_branch,
            )
            print(f"WARN: {warning}", flush=True)
            terminal_progress("WARN CONTINUATION", f"{issue.label} {phase} review")
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
leave it clean and explain why; do not create an empty commit.\n\n{result}""",
                process="reviewer-fix",
            ),
            iteration=review_number,
            pr=pr,
            warning_scope=issue.result_id,
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
            expected_process="reviewer-fix",
            iteration=review_number,
            warning_scope=issue.result_id,
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
            current = review_audit_dispositions(
                github,
                current,
                resumed_audits + (audit.audit_id,),
                "no_change_after_re_evaluation",
                head=issue.branch,
                base=config.integration_branch,
            )
            print(f"WARN: {warning}", flush=True)
            terminal_progress("WARN CONTINUATION", f"{issue.label} {phase} review")
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
        pr = ensure_issue_pr_metadata(github, pr, issue, config)
        pr = review_audit_dispositions(
            github,
            pr,
            resumed_audits + (audit.audit_id,),
            "fixed",
            head=issue.branch,
            base=config.integration_branch,
            fix_sha=fixed_sha,
        )
        if restart_scope_on_change:
            return IssueReviewPhaseResult(
                pr, "head_changed", pr.head_sha, pr.base_sha, review_number
            )
    raise WorkerFailure(f"{issue.label} {phase} review ended unexpectedly")


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
    """Retry a lost audit only after verifying the current delivery topology."""
    reviewed_head_sha = pr.head_sha
    for attempt in range(MAX_REPOSITORY_RECOVERIES + 1):
        try:
            return _review_issue_phase(
                issue, config, client, repo, github, implementer, reviewer, pr,
                phase=phase, prompt=prompt, max_reviews=max_reviews,
                review_offset=review_offset,
                restart_scope_on_change=restart_scope_on_change,
            )
        except MissingReviewAudit:
            if attempt == MAX_REPOSITORY_RECOVERIES:
                raise
            repo.require_clean()
            pushed = repo.require_pushed(issue.branch)
            if pushed.local_sha is None or pushed.remote_sha != pushed.local_sha:
                raise WorkerFailure("review audit recovery requires a pushed head")
            pr = github.require_pr(
                number=pr.number,
                head=issue.branch,
                base=config.integration_branch,
                state="OPEN",
                expected_head_sha=pushed.local_sha,
                expected_base_sha=pr.base_sha,
                draft=True,
            )
            if pr.auto_merge_enabled or pr.merge_queue_entry is not None:
                raise WorkerFailure("review audit recovery requires a safe Draft PR")
            require_inline_task_pr_fingerprint(pr, issue.task_fingerprint)
            review_offset = 0
            if restart_scope_on_change and pr.head_sha != reviewed_head_sha:
                return IssueReviewPhaseResult(
                    pr, "head_changed", pr.head_sha, pr.base_sha, review_offset
                )
            emit_finding(
                "github",
                f"{issue.label} {phase} audit is missing; reviewing current head "
                f"{pr.head_sha} again",
                status="warning",
            )
    raise WorkerFailure("review audit recovery retry limit exceeded")


def process_issue(
    issue: Issue,
    config: Config,
    client: PurpleMuxCLIClient,
    repo: GitRepository,
    github: GitHubRepository,
) -> PullRequestState:
    terminal_progress("WORK ITEM", issue.label, detail=issue.branch)
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
            warning_scope=issue.result_id,
        )
    prepared = prepare_issue(repo, github, issue, config)
    previous_result = next(
        (result for result in ISSUE_HANDOFF_RESULTS if result.issue == issue.result_id),
        None,
    )
    previous_pr = COMPLETED_ISSUE_PRS.get(issue.result_id)
    if isinstance(prepared, PullRequestState):
        rehydrate_policy_conflicts(prepared.body, config, issue_number=issue.result_id)
        if (
            previous_result is not None
            and previous_pr is not None
            and previous_result.outcome != "skipped"
            and (
                previous_result.pr_number != prepared.number
                or previous_pr.head_sha != prepared.head_sha
            )
        ):
            raise WorkerFailure(f"completed {issue.label} PR changed during recovery")
        completed_here = (
            previous_result is not None
            and previous_pr is not None
            and previous_result.outcome != "skipped"
            and previous_result.pr_number == prepared.number
            and previous_pr.head_sha == prepared.head_sha
        )
        outcome = previous_result.outcome if completed_here else "skipped"
        reviews = previous_result.reviews if completed_here else 0
        warnings = (
            tuple(dict.fromkeys((*previous_result.warnings, *summary_warnings(issue.result_id))))[:3]
            if completed_here
            else summary_warnings(issue.result_id)
        )
        print(f"Already merged {issue.label}", flush=True)
        record_issue_handoff_result(
            issue.result_id, issue.label, prepared, outcome, reviews, warnings
        )
        emit_issue_result(
            issue.result_id,
            outcome,
            reviews,
            prepared.number,
            prepared.url,
            warnings=warnings,
            label=issue.label,
        )
        return prepared
    existing_pr, start_sha, reused_existing_work = prepared
    if (
        not MERGE_TO_INTEGRATION
        and previous_result is not None
        and previous_pr is not None
        and previous_result.outcome in ("approved", "continued_with_warning")
        and not previous_pr.is_draft
    ):
        if (
            existing_pr is None
            or previous_result.pr_number != existing_pr.number
        ):
            raise WorkerFailure(f"reviewed {issue.label} PR changed during recovery")
        pushed = repo.require_pushed(issue.branch)
        if pushed.local_sha != previous_pr.head_sha:
            raise WorkerFailure(f"reviewed {issue.label} head changed during recovery")
        ready = github.require_pr(
            number=existing_pr.number,
            head=issue.branch,
            base=config.integration_branch,
            state="OPEN",
            expected_head_sha=previous_pr.head_sha,
            expected_base_sha=previous_pr.base_sha,
            draft=False,
        )
        require_inline_task_pr_fingerprint(ready, issue.task_fingerprint)
        audits = review_audit_from_body(ready.body)
        for role, phase, limit in (
            ("scope_design", "scope/design", MAX_SCOPE_REVIEWS),
            ("correctness", "correctness", MAX_REVIEWS),
        ):
            role_audits = [
                record for record in audits
                if record.role == role and record.reviewed_sha == ready.head_sha
            ]
            reviewed_current_head = any(
                (record.verdict == "APPROVED" and record.fix_disposition == "not_required")
                or (
                    previous_result.outcome == "continued_with_warning"
                    and record.verdict == "CHANGES_REQUESTED"
                    and record.fix_disposition in (
                        "review_limit_reached",
                        "no_change_after_re_evaluation",
                    )
                )
                for record in role_audits
            )
            limit_warning = (
                f"{issue.label} {phase} review limit {limit} was already "
                "reached before the current head could complete this phase; continuing "
                "without reviewer approval."
            )
            limit_before_current_head = (
                previous_result.outcome == "continued_with_warning"
                and limit_warning in previous_result.warnings
                and any(
                    record.role == role
                    and record.round >= limit
                    and record.reviewed_sha != ready.head_sha
                    and record.fix_disposition != "pending"
                    for record in audits
                )
                and any(
                    record.fix_sha == ready.head_sha
                    and record.fix_disposition in (
                        "fixed",
                        "reviewer_changed_head",
                        "head_changed_before_disposition",
                    )
                    for record in audits
                )
            )
            if not (reviewed_current_head or limit_before_current_head):
                raise WorkerFailure(
                    f"reviewed {issue.label} lacks persisted {role} review evidence"
                )
        record_issue_handoff_result(
            issue.result_id,
            issue.label,
            ready,
            previous_result.outcome,
            previous_result.reviews,
            previous_result.warnings,
        )
        emit_issue_result(
            issue.result_id,
            previous_result.outcome,
            previous_result.reviews,
            ready.number,
            ready.url,
            warnings=previous_result.warnings,
            label=issue.label,
        )
        return ready
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
    if existing_pr is not None:
        emit_issue_navigation(
            issue.result_id,
            existing_pr.number,
            existing_pr.url,
            label=issue.label,
            workspace_id=client.workspace_id,
            implementation_tab_id=implementer,
            scope_review_tab_id=scope_reviewer,
            correctness_review_tab_id=correctness_reviewer,
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
        warning_scope=issue.result_id,
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
        expected_process="implementation",
        warning_scope=issue.result_id,
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
        reconcile_plan_owned_inline_identity=existing_pr is None,
    )
    emit_issue_navigation(
        issue.result_id,
        pr.number,
        pr.url,
        label=issue.label,
        workspace_id=client.workspace_id,
        implementation_tab_id=implementer,
        scope_review_tab_id=scope_reviewer,
        correctness_review_tab_id=correctness_reviewer,
    )
    pr = ensure_issue_pr_metadata(github, pr, issue, config)
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
            workspace_id=client.workspace_id,
            implementation_tab_id=implementer,
            scope_review_tab_id=scope_reviewer,
            correctness_review_tab_id=correctness_reviewer,
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
        workspace_id=client.workspace_id,
        implementation_tab_id=implementer,
        scope_review_tab_id=scope_reviewer,
        correctness_review_tab_id=correctness_reviewer,
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


def planner_work_item_json(issue: Issue) -> dict[str, int | str]:
    if issue.number is not None:
        return {"issue": issue.number, "branch": issue.branch}
    assert issue.task_id is not None and issue.task is not None
    return {"id": issue.task_id, "task": issue.task}


def planner_prompt(plan: WorkItemPlan, config: Config) -> str:
    processed = [planner_work_item_json(issue) for issue in plan.items[: plan.position]]
    remaining = [planner_work_item_json(issue) for issue in plan.remaining]
    one_shot_context = ""
    if config.one_shot_issue is not None:
        one_shot_context = f"""This is a one-shot run sourced from GitHub Issue
#{config.one_shot_issue}. Before deciding, read it with `gh issue view
{config.one_shot_issue} --repo {config.slug}` and read
`docs/design-principles.md` from the current integration branch with `git show
{config.integration_branch}:docs/design-principles.md`. Use that document as the
canonical source when decomposing or refining work. Manage delivery by
decomposing the remaining work into short inline mini tasks. Each task must state
its purpose and any non-negotiable design decision, while leaving implementation
detail to the implementer. Do not create GitHub Issues or implement the source
Issue as one undivided work item. Numeric Issue additions are invalid in one-shot
mode. Do not include stdout or agent conversation logs in tasks or rationale.
Tasks, rationale, and skip reasons that will be published must be concise
single-line summaries, never copied stdout, stderr, or conversation transcripts.

"""
    decision_keys = (
        '"actions", "complete", "policy_conflicts", and\n"rationale"'
        if config.one_shot_issue is not None
        else '"actions", "complete", and\n"policy_conflicts"'
    )
    rationale_contract = (
        f" rationale must be a concise single-line explanation of why the current "
        f"decomposition is appropriate, at most {MAX_PLANNER_RATIONALE_BYTES} UTF-8 "
        "bytes, without logs or secrets."
        if config.one_shot_issue is not None
        else ""
    )
    return f"""Review the workflow-owned work-item plan before its next dispatch.
You are the planning role only: do not edit files, implement work, or mutate Git
or GitHub. Inspect repository and GitHub state read-only when useful. Preserve
the current plan unless progress provides a concrete reason to add necessary
work, refine a pending inline mini task, or skip obsolete/redundant pending work.
Never update or skip a processed item. GitHub Issue work uses its positive number;
inline work uses a stable lowercase kebab-case ID and a concise authoritative task.

Before skipping a pending GitHub Issue as already implemented, read its current
Issue body and directly compare every requirement with the code on the current
integration branch. Treat earlier Issues and pull requests only as supporting
context; the existence of a related pull request is not sufficient evidence for
a skip. The skip reason must briefly identify evidence in the current integration
branch, such as the files, symbols, or tests that satisfy the Issue requirements.

{one_shot_context}Repository: {config.slug}
Integration branch: {config.integration_branch}
Processed work items: {json.dumps(processed, ensure_ascii=False)}
Pending work items: {json.dumps(remaining, ensure_ascii=False)}

Return exactly one JSON object with keys {decision_keys}.{rationale_contract}
policy_conflicts must be an array containing at most
{MAX_PLANNER_POLICY_CONFLICTS} concise strings of at most
{MAX_POLICY_CONFLICT_DETAIL_CHARS} characters each, and must be empty when no
conflict exists. Actions run in order and have one of these exact shapes:
- {{"action":"add","item":123}}
- {{"action":"add","item":{{"id":"task-id","task":"instruction"}}}}
- {{"action":"update","key":"task-id","task":"revised instruction"}}
- {{"action":"skip","key":123,"reason":"integration branch path/to/file.py and its tests satisfy the requirements"}}
- {{"action":"skip","key":"task-id","reason":"concise reason"}}
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


def planner_text_is_publishable(value: str) -> bool:
    return (
        value.splitlines() == [value]
        and all(
            ord(character) >= 0x20 and ord(character) != 0x7F for character in value
        )
        and "```" not in value
        and _PLANNER_LOW_LEVEL_TEXT.search(value) is None
        and _RAW_REVIEW_OUTPUT.search(value) is None
        and _SENSITIVE_REVIEW_TEXT.search(value) is None
        and _OPAQUE_SECRET_LIKE_VALUE.search(value) is None
    )


def require_publishable_planner_task(issue: Issue) -> None:
    assert issue.task is not None
    if not planner_text_is_publishable(issue.task):
        raise WorkerFailure(
            "one-shot planner task must not contain logs or secret-like values"
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


def persisted_work_item(value: object) -> Issue:
    if isinstance(value, dict) and set(value) == {"issue", "branch"}:
        number = value["issue"]
        branch = value["branch"]
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or not isinstance(branch, str)
            or not branch
            or branch != branch.strip()
            or "\0" in branch
        ):
            raise WorkerFailure("persisted GitHub Issue work item is invalid")
        return Issue(number, branch)
    return planner_added_issue(value)


def apply_planner_decision(plan: WorkItemPlan, source: str) -> PlannerDecision:
    try:
        decision = json.loads(source)
    except json.JSONDecodeError as exc:
        raise WorkerFailure(f"planner returned invalid JSON: {exc.msg}") from exc
    expected_fields = {"actions", "complete", "policy_conflicts"}
    if plan.config.one_shot_issue is not None:
        expected_fields.add("rationale")
    if not isinstance(decision, dict) or set(decision) != expected_fields:
        raise WorkerFailure(
            "planner decision must contain only actions, complete, and "
            "policy_conflicts"
        )
    actions = decision["actions"]
    complete = decision["complete"]
    policy_conflicts = decision["policy_conflicts"]
    rationale = decision.get("rationale")
    if (
        not isinstance(actions, list)
        or len(actions) > MAX_PLANNER_ACTIONS
        or not isinstance(complete, bool)
        or not isinstance(policy_conflicts, list)
        or len(policy_conflicts) > MAX_PLANNER_POLICY_CONFLICTS
    ):
        raise WorkerFailure("planner decision has invalid bounded values")
    if plan.config.one_shot_issue is not None:
        if not _safe_review_text(
            rationale, max_bytes=MAX_PLANNER_RATIONALE_BYTES
        ) or not planner_text_is_publishable(rationale):
            raise WorkerFailure("planner rationale is invalid or unsafe")
    for conflict in policy_conflicts:
        conflict_has_surrogate = isinstance(conflict, str) and any(
            0xD800 <= ord(character) <= 0xDFFF for character in conflict
        )
        if (
            not isinstance(conflict, str)
            or not conflict
            or conflict != conflict.strip()
            or "\0" in conflict
            or len(conflict) > MAX_POLICY_CONFLICT_DETAIL_CHARS
            or conflict_has_surrogate
        ):
            raise WorkerFailure("planner policy conflict is invalid")
    if policy_conflicts and plan.config.policy_issue is None:
        raise WorkerFailure("planner reported a policy conflict without a policy Issue")

    candidate = WorkItemPlan(plan.config)
    candidate.items = list(plan.items)
    candidate.position = plan.position
    candidate.skipped = list(plan.skipped)
    skipped_before = len(candidate.skipped)
    changes: list[str] = []
    try:
        for action in actions:
            if not isinstance(action, dict) or not isinstance(
                action.get("action"), str
            ):
                raise WorkerFailure("planner action is invalid")
            kind = action["action"]
            if kind == "add" and set(action) == {"action", "item"}:
                added = planner_added_issue(action["item"])
                candidate.add(added)
                changes.append(f"Added {added.label}.")
            elif kind == "update" and set(action) == {"action", "key", "task"}:
                key = planner_key(action["key"])
                index = candidate._remaining_index(key)
                current = candidate.items[index]
                if current.task_id is None:
                    raise WorkerFailure("planner can update only an inline mini task")
                updated = planner_inline_issue(current.task_id, action["task"])
                candidate.update(key, updated)
                changes.append(f"Updated {current.label}.")
            elif kind == "skip" and set(action) == {"action", "key", "reason"}:
                reason = action["reason"]
                reason_has_surrogate = isinstance(reason, str) and any(
                    0xD800 <= ord(character) <= 0xDFFF for character in reason
                )
                if (
                    not isinstance(reason, str)
                    or not reason
                    or reason != reason.strip()
                    or "\0" in reason
                    or len(reason) > 500
                    or reason_has_surrogate
                ):
                    raise WorkerFailure("planner skip reason is invalid")
                if (
                    plan.config.one_shot_issue is not None
                    and not planner_text_is_publishable(reason)
                ):
                    raise WorkerFailure(
                        "one-shot planner skip reason must not contain logs or "
                        "secret-like values"
                    )
                candidate.skip(planner_key(action["key"]), reason)
                summary = reason if len(reason) <= 160 else f"{reason[:159]}…"
                changes.append(
                    f"Skipped {candidate.skipped[-1].issue.label}: {summary}"
                )
            else:
                raise WorkerFailure("planner action has an unsupported shape")
    except ValueError as exc:
        raise WorkerFailure(f"planner decision is invalid: {exc}") from exc

    if plan.config.one_shot_issue is not None:
        for issue in candidate.items:
            require_publishable_planner_task(issue)

    if complete and candidate.remaining:
        raise WorkerFailure("planner cannot complete while work items remain")
    if not complete and not candidate.remaining:
        raise WorkerFailure("planner must add work or complete an empty plan")
    candidate.finalized = complete
    validate_work_item_plan_capacity(candidate)
    plan.items = candidate.items
    plan.skipped = candidate.skipped
    plan.finalized = complete
    return PlannerDecision(
        complete,
        tuple(policy_conflicts),
        tuple(candidate.skipped[skipped_before:]),
        rationale,
        tuple(changes),
    )


def one_shot_planning_comment(
    plan: WorkItemPlan, decision: PlannerDecision
) -> str:
    if plan.config.one_shot_issue is None or decision.rationale is None:
        raise ValueError("planning comments require a one-shot planner decision")
    items = []
    for index, issue in enumerate(plan.items):
        status = "processed" if index < plan.position else "pending"
        assert issue.task_id is not None and issue.task is not None
        items.append(f"{index + 1}. `{issue.task_id}` ({status}) — {issue.task}")
    decomposition = "\n".join(items) if items else "No work items remain."
    changes = (
        "\n".join(f"- {change}" for change in decision.changes)
        if decision.changes
        else "- No changes from the previous planning result."
    )
    return (
        "## One-Shot Planning\n\n"
        "### Work item decomposition\n\n"
        f"{decomposition}\n\n"
        "### Decomposition rationale\n\n"
        f"{decision.rationale}\n\n"
        "### Changes from previous planning\n\n"
        f"{changes}"
    )


def plan_seed_fingerprint(config: Config) -> str:
    seed_value = {
        "items": [planner_work_item_json(issue) for issue in config.issues],
        "one_shot_issue": config.one_shot_issue,
        "policy_issue": config.policy_issue,
    }
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
        plan.items = [persisted_work_item(item) for item in items]
        plan._validate(plan.items)
    except ValueError as exc:
        raise WorkerFailure(f"Base PR work-item plan is invalid: {exc}") from exc
    plan.position = position
    plan.finalized = finalized
    if finalized and position != len(plan.items):
        raise WorkerFailure("Base PR work-item plan completion state is inconsistent")
    plan.persisted_source = serialized_work_item_plan(plan)
    return plan


def deferred_work_item_plan_ref(config: Config) -> str:
    identity = json.dumps(
        {
            "repository": config.slug.lower(),
            "integration_branch": config.integration_branch,
            "final_branch": config.main_branch,
            "seed": plan_seed_fingerprint(config),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"refs/notes/agent-workflow-manager/work-item-plan-{digest}"


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
        result = "\n".join(lines)
    else:
        result = f"{body.rstrip()}\n\n{marker}" if body.strip() else marker
    require_base_pr_body_size(result)
    return result


def prepare_work_item_plan_pr(
    config: Config,
    repo: GitRepository,
    github: GitHubRepository,
) -> tuple[PullRequestState | None, WorkItemPlan]:
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
        rehydrate_policy_conflicts(merged.body, config, issue_number=None)
        return merged, plan
    if pr is None:
        recovery_body = repo.inspect_remote_note(
            deferred_work_item_plan_ref(config), final.remote_sha
        )
        if recovery_body is not None:
            initial_plan = work_item_plan_from_body(recovery_body, config)
            initial_plan.persisted_source = recovery_body
            rehydrate_policy_conflicts(recovery_body, config, issue_number=None)
        else:
            initial_plan = WorkItemPlan(config)
        if integration.remote_sha == final.remote_sha:
            emit_finding(
                "github",
                "Base PR creation deferred until the integration branch "
                "differs from the final branch",
                status="info",
            )
            return None, initial_plan
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
    rehydrate_policy_conflicts(pr.body, config, issue_number=None)
    emit_run_pr(pr.number, pr.url)
    return pr, plan


def persist_work_item_plan(
    plan: WorkItemPlan,
    config: Config,
    repo: GitRepository,
    github: GitHubRepository,
    pr: PullRequestState | None,
) -> PullRequestState | None:
    if pr is None:
        pr, _initial_plan = prepare_work_item_plan_pr(config, repo, github)
        if pr is None:
            final = repo.inspect_branch(config.main_branch)
            if final.remote_sha is None:
                raise WorkerFailure("final remote branch is missing")
            source = with_base_pr_policy_notes(
                serialized_work_item_plan(plan), config
            )
            repo.update_remote_note(
                deferred_work_item_plan_ref(config),
                final.remote_sha,
                source,
                expected_body=plan.persisted_source,
            )
            plan.persisted_source = source
            return None
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
    body = with_base_pr_policy_notes(
        with_work_item_plan(current.body, plan), config
    )
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


def inspect_dynamic_work_item_topology(
    issue: Issue, config: Config, *, recover_missing_inline_identity: bool = False
) -> None:
    """Validate the plan-owned identity before recording its dispatch."""
    if issue.number is not None and issue in config.issues:
        return
    declaration: tuple[int | str, str] | tuple[int | str, str, str]
    if issue.number is not None:
        declaration = (issue.number, issue.branch)
    else:
        assert issue.task_id is not None and issue.task_fingerprint is not None
        declaration = (
            f"Mini task {issue.task_id}",
            issue.branch,
            issue.task_fingerprint,
        )
    inspect_topology = (
        recover_issue_driven_work_item_topology
        if recover_missing_inline_identity and issue.number is None
        else inspect_issue_driven_work_item_topology
    )
    inspect_topology(
        repo=str(config.repo),
        integration_branch=config.integration_branch,
        issue=declaration,
        command_timeout_seconds=COMMAND_TIMEOUT,
    )


def process_work_items(
    config: Config,
    client: PurpleMuxCLIClient,
    repo: GitRepository,
    github: GitHubRepository,
    plan_pr: PullRequestState | None,
    plan: WorkItemPlan,
) -> tuple[Issue, ...]:
    for recovered_issue in plan.items[: plan.position]:
        plan.active = recovered_issue
        inspect_dynamic_work_item_topology(
            recovered_issue, config, recover_missing_inline_identity=True
        )
        run_outline_step(
            recovered_issue.label,
            lambda issue=recovered_issue: process_issue(
                issue, config, client, repo, github
            ),
        )
        plan.active = None
    if plan.finalized:
        return plan.snapshot
    planner = create_agent(
        client,
        config,
        agent_type=REVIEWER_AGENT,
        name="Work-item planner",
    )
    for planner_turn in range(1, MAX_PLANNER_TURNS + 1):
        _, planner_decision = run_validated_turn(
            client,
            planner,
            "Work-item planning",
            policy_context(
                config, scope="work-item planning", structured_conflicts=True
            )
            + planner_prompt(plan, config),
            lambda source: apply_planner_decision(plan, source),
            iteration=planner_turn,
        )
        for conflict in planner_decision.policy_conflicts:
            record_policy_conflict(
                None,
                f"Policy Issue #{config.policy_issue} conflicts with work-item "
                f"planning: {conflict}; continuing with the implementation work "
                "item as the primary requirement.",
            )
        for skipped in planner_decision.skipped:
            emit_planner_skip(
                skipped.issue.result_id,
                skipped.reason,
                label=skipped.issue.label,
            )
        if config.one_shot_issue is not None:
            comment = one_shot_planning_comment(plan, planner_decision)
            github.create_issue_comment(
                config.one_shot_issue,
                body=comment,
                correlation_id=run_correlation(
                    "one-shot-planning-comment-"
                    + hashlib.sha256(comment.encode()).hexdigest()[:16]
                ),
            )
        plan_pr = persist_work_item_plan(plan, config, repo, github, plan_pr)
        if planner_decision.complete:
            return plan.snapshot
        issue = plan.take_next()
        assert issue is not None
        plan.active = issue
        inspect_dynamic_work_item_topology(issue, config)
        plan_pr = persist_work_item_plan(plan, config, repo, github, plan_pr)
        run_outline_step(
            issue.label,
            lambda issue=issue: process_issue(issue, config, client, repo, github),
        )
        plan.active = None
        if plan_pr is None:
            plan_pr = persist_work_item_plan(plan, config, repo, github, plan_pr)
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


def final_work_item_context(config: Config, work_items: tuple[Issue, ...]) -> str:
    items = "\n".join(
        (
            f"- GitHub Issue #{item.number}, branch {item.branch}"
            if item.number is not None
            else f"- {item.label}, branch {item.branch}: {item.task}"
        )
        for item in work_items
    ) or "- none"
    one_shot = (
        f"One-shot source: GitHub Issue #{config.one_shot_issue}."
        if config.one_shot_issue is not None
        else "One-shot source: none."
    )
    return f"""Authoritative final work-item plan:
{items}
{one_shot}
Read each listed GitHub Issue and the one-shot source, when present, with
`gh issue view NUMBER --repo {config.slug}` before judging its requirements.
Inline mini-task text above is authoritative."""


def scenario_gate_prompt(
    pr: PullRequestState, config: Config, work_items: tuple[Issue, ...]
) -> str:
    """Build the AI-judged Before/After gate from human-authored scenarios."""
    scenario_list = "\n".join(
        f"{index}. {scenario}" for index, scenario in enumerate(SCENARIOS, 1)
    )
    return f"""Run the Scenario Gate for exact Before commit {pr.base_sha} and exact
After commit {pr.head_sha}. The human-authored scenario list is below.

{scenario_list}

{final_work_item_context(config, work_items)}

Select a small, risk-relevant subset; executing every scenario is not required.
The subset may cover existing behavior, new behavior, and failure behavior. For
each selected scenario, observe or inspect both Before and After, report the
material behavioral difference and evidence, and judge whether that difference
is appropriate for the integrated work items. Do not treat this as a fixed
expected-output test: use the Issue and policy context to judge the difference.
Use read-only inspection or disposable temporary directories and leave the
repository worktree unchanged. Put only actionable problems in findings; omit
supporting raw evidence from the response. Do not mutate files or PR state.
{REVIEWER_CHECKOUT_GUARD}
{REVIEWER_AUDIT_GUARD}"""


def whole_version_review_prompt(
    pr: PullRequestState, config: Config, work_items: tuple[Issue, ...]
) -> str:
    return (
        f"Review the whole version at exact head {pr.head_sha} against final "
        f"base {pr.base_sha}. Examine integration consistency across Issues, "
        "duplication between their implementations, cross-feature interactions "
        "and regressions, and whether shared versus feature-specific "
        "responsibilities are placed at the right boundaries. Also review the "
        "combined version for correctness, safety, and missing integration "
        "coverage. Do not mutate anything.\n\n"
        + REVIEWER_CHECKOUT_GUARD
        + "\n\n"
        + REVIEWER_AUDIT_GUARD
        + "\n\n"
        + final_work_item_context(config, work_items)
    )


def design_principles_review_prompt(
    pr: PullRequestState, config: Config, work_items: tuple[Issue, ...]
) -> str:
    return (
        f"Review exact integration head {pr.head_sha} solely for conformance with "
        "the repository's design principles. Before judging, read the authoritative "
        "document from that exact head with `git show "
        f"{pr.head_sha}:docs/design-principles.md`. Inspect the integrated change "
        f"against final base {pr.base_sha} and report only deviations from an "
        "applicable principle in that document. Do not perform Scenario Gate, "
        "general whole-version, correctness, version, or README review in this "
        "turn. Do not mutate anything.\n\n"
        + REVIEWER_CHECKOUT_GUARD
        + "\n\n"
        + REVIEWER_AUDIT_GUARD
        + "\n\n"
        + final_work_item_context(config, work_items)
    )


def version_readme_review_prompt(
    pr: PullRequestState, config: Config, work_items: tuple[Issue, ...]
) -> str:
    return (
        f"Review version and README consistency at exact head {pr.head_sha} "
        f"against final base {pr.base_sha}. Check that version declarations and "
        "version references agree with the integrated implementation and intended "
        "release, and that README documentation agrees with the current behavior "
        "and specification. Look specifically for remnants of removed features, "
        "obsolete CLI or API usage, and outdated configuration examples. Keep this "
        "as an independent documentation/version review; do not repeat the general "
        "whole-version review. Do not mutate anything.\n\n"
        + REVIEWER_CHECKOUT_GUARD
        + "\n\n"
        + REVIEWER_AUDIT_GUARD
        + "\n\n"
        + final_work_item_context(config, work_items)
    )


def _review_whole_version(
    config: Config,
    client: PurpleMuxCLIClient,
    repo: GitRepository,
    github: GitHubRepository,
    pr: PullRequestState,
    work_items: tuple[Issue, ...],
) -> tuple[PullRequestState, ReviewDelivery]:
    """Review, fix, and check the whole version as one outline-level phase."""
    pr = reconcile_review_audits_after_head_change(
        github,
        pr,
        head=config.integration_branch,
        base=config.main_branch,
    )
    audits = review_audit_from_body(pr.body)
    latest_continuation = next(
        (record for record in reversed(audits)
         if record.role == "whole_version_continuation"),
        None,
    )
    continuation = (
        latest_continuation
        if latest_continuation is not None
        and latest_continuation.reviewed_sha == pr.head_sha
        and latest_continuation.fix_disposition in (
            "review_limit_reached", "review_limit_reached_after_head_change",
            "no_change_after_re_evaluation",
        )
        else None
    )
    if continuation is not None:
        warning = whole_continuation_warning(
            continuation.fix_disposition, continuation.round
        )
        pr = require_warning_delivery(
            repo, github, pr, head=config.integration_branch,
            base=config.main_branch, expected_head_sha=pr.head_sha,
            expected_base_sha=pr.base_sha,
        )
        emit_finding("github", warning, status="warning")
        return pr, ReviewDelivery(
            "continued_with_warning", pr.head_sha, pr.base_sha,
            continuation.round, (warning,),
        )
    whole_roles = {
        "scenario_gate", "design_principles", "whole_version", "version_readme",
        "whole_version_limit",
    }
    latest_changed_head = next(
        (record for record in reversed(audits)
         if record.role in whole_roles
         and record.fix_disposition in (
             "reviewer_changed_head", "head_changed_before_disposition"
         )),
        None,
    )
    prior_limit = (
        latest_changed_head
        if latest_changed_head is not None
        and latest_changed_head.round >= MAX_REVIEWS
        and latest_changed_head.fix_sha == pr.head_sha
        else None
    )
    required_roles = {"design_principles", "whole_version", "version_readme"}
    if SCENARIOS:
        required_roles.add("scenario_gate")
    latest_by_role = {
        role: next(
            (record for record in reversed(audits) if record.role == role), None
        )
        for role in required_roles
    }
    complete_current_head = all(
        record is not None
        and record.reviewed_sha == pr.head_sha
        and record.fix_disposition != "pending"
        for record in latest_by_role.values()
    )
    completed_warning = next(
        (record for record in latest_by_role.values()
         if record is not None
         and record.fix_disposition in (
             "review_limit_reached", "no_change_after_re_evaluation"
         )),
        None,
    ) if complete_current_head else None
    if completed_warning is not None:
        prior_limit = None
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
    design_principles_reviewer = create_agent(
        client,
        config,
        agent_type=REVIEWER_AGENT,
        name="Design Principles reviewer",
    )
    version_readme_reviewer = create_agent(
        client,
        config,
        agent_type=REVIEWER_AGENT,
        name="Version / README reviewer",
    )
    scenario_reviewer = (
        create_agent(
            client,
            config,
            agent_type=REVIEWER_AGENT,
            name="Scenario Gate reviewer",
        )
        if SCENARIOS
        else None
    )
    delivery: ReviewDelivery | None = None
    for review_number in (
        () if prior_limit is not None or completed_warning is not None
        else range(1, MAX_REVIEWS + 1)
    ):
        result: str
        resumed_records = tuple(
            record
            for record in review_audit_from_body(pr.body)
            if record.fix_disposition == "pending"
            and record.reviewed_sha == pr.head_sha
        )
        review_results = [
            json.dumps(
                {
                    "verdict": "CHANGES_REQUESTED",
                    "findings": list(record.findings),
                    "policy_conflicts": [],
                }
            )
            for record in resumed_records
        ]
        requested_change_audits = [record.audit_id for record in resumed_records]
        whole_audit: ReviewAuditRecord | None = None
        changes_requested = bool(resumed_records)
        if scenario_reviewer is not None:
            result, verdict = run_validated_turn(
                client,
                scenario_reviewer,
                "Scenario Gate reviewer turn",
                policy_context(
                    config,
                    scope="the whole-version Scenario Gate",
                    structured_conflicts=True,
                )
                + scenario_gate_prompt(pr, config, work_items),
                decision,
                iteration=review_number,
            )
            emit_policy_conflicts(result, config, scope="the integrated version")
            scenario_audit = allocate_review_audit(
                pr.body, "scenario_gate", verdict, pr.head_sha, result
            )
            pr = persist_review_audit(
                github,
                pr,
                scenario_audit,
                head=config.integration_branch,
                base=config.main_branch,
            )
            if verdict == "CHANGES_REQUESTED":
                requested_change_audits.append(scenario_audit.audit_id)
            scenario_sha, scenario_reviewer_changed = require_agent_result(
                repo,
                client,
                fixer,
                config.integration_branch,
                pr.head_sha,
                allow_unchanged=True,
                expected_process="cleanup",
                iteration=review_number,
            )
            if scenario_reviewer_changed:
                pushed = repo.ensure_pushed(
                    config.integration_branch, expected_local_sha=scenario_sha
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
                pending_audits = tuple(
                    record.audit_id
                    for record in review_audit_from_body(pr.body)
                    if record.fix_disposition == "pending"
                )
                pr = review_audit_dispositions(
                    github,
                    pr,
                    tuple(requested_change_audits)
                    + pending_audits
                    + (scenario_audit.audit_id,),
                    "reviewer_changed_head",
                    head=config.integration_branch,
                    base=config.main_branch,
                    fix_sha=scenario_sha,
                )
                if review_number == MAX_REVIEWS:
                    pr = persist_whole_limit_head_change(
                        github, pr, round_number=review_number,
                        reviewed_sha=scenario_audit.reviewed_sha,
                        head=config.integration_branch, base=config.main_branch,
                    )
                emit_finding(
                    "git",
                    "Scenario Gate review changed the integration branch; "
                    f"approval invalidated at {scenario_sha}",
                )
                continue
            review_results.append(result)
            changes_requested = changes_requested or verdict == "CHANGES_REQUESTED"
        else:
            result = "APPROVED\nScenario Gate not configured."
            verdict = "APPROVED"
        principles_result, principles_verdict = run_validated_turn(
            client,
            design_principles_reviewer,
            "Design Principles reviewer turn",
            policy_context(
                config,
                scope="the design-principles conformance review",
                structured_conflicts=True,
            )
            + design_principles_review_prompt(pr, config, work_items),
            decision,
            iteration=review_number,
        )
        review_results.append(principles_result)
        changes_requested = (
            changes_requested or principles_verdict == "CHANGES_REQUESTED"
        )
        emit_policy_conflicts(
            principles_result, config, scope="the integrated version"
        )
        principles_audit = allocate_review_audit(
            pr.body,
            "design_principles",
            principles_verdict,
            pr.head_sha,
            principles_result,
        )
        pr = persist_review_audit(
            github,
            pr,
            principles_audit,
            head=config.integration_branch,
            base=config.main_branch,
        )
        if principles_verdict == "CHANGES_REQUESTED":
            requested_change_audits.append(principles_audit.audit_id)
        principles_sha, principles_reviewer_changed = require_agent_result(
            repo,
            client,
            fixer,
            config.integration_branch,
            pr.head_sha,
            allow_unchanged=True,
            expected_process="cleanup",
            iteration=review_number,
        )
        if principles_reviewer_changed:
            pushed = repo.ensure_pushed(
                config.integration_branch, expected_local_sha=principles_sha
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
            pending_audits = tuple(
                record.audit_id
                for record in review_audit_from_body(pr.body)
                if record.fix_disposition == "pending"
            )
            pr = review_audit_dispositions(
                github,
                pr,
                tuple(requested_change_audits)
                + pending_audits
                + (principles_audit.audit_id,),
                "reviewer_changed_head",
                head=config.integration_branch,
                base=config.main_branch,
                fix_sha=principles_sha,
            )
            if review_number == MAX_REVIEWS:
                pr = persist_whole_limit_head_change(
                    github, pr, round_number=review_number,
                    reviewed_sha=principles_audit.reviewed_sha,
                    head=config.integration_branch, base=config.main_branch,
                )
            emit_finding(
                "git",
                "design-principles review changed the integration branch; "
                f"approval invalidated at {principles_sha}",
            )
            continue
        result, verdict = run_validated_turn(
            client,
            reviewer,
            "Whole-version reviewer turn",
            policy_context(
                config,
                scope="the whole-version review",
                structured_conflicts=True,
            )
            + whole_version_review_prompt(pr, config, work_items),
            decision,
            iteration=review_number,
        )
        review_results.append(result)
        changes_requested = changes_requested or verdict == "CHANGES_REQUESTED"
        whole_audit = allocate_review_audit(
            pr.body, "whole_version", verdict, pr.head_sha, result
        )
        pr = persist_review_audit(
            github,
            pr,
            whole_audit,
            head=config.integration_branch,
            base=config.main_branch,
        )
        if verdict == "CHANGES_REQUESTED":
            requested_change_audits.append(whole_audit.audit_id)
        emit_policy_conflicts(result, config, scope="the integrated version")
        reviewed_sha, reviewer_changed = require_agent_result(
            repo,
            client,
            fixer,
            config.integration_branch,
            pr.head_sha,
            allow_unchanged=True,
            expected_process="cleanup",
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
            pending_audits = tuple(
                record.audit_id
                for record in review_audit_from_body(pr.body)
                if record.fix_disposition == "pending"
            )
            current_audits = (whole_audit.audit_id,) if whole_audit is not None else ()
            pr = review_audit_dispositions(
                github,
                pr,
                tuple(requested_change_audits) + pending_audits + current_audits,
                "reviewer_changed_head",
                head=config.integration_branch,
                base=config.main_branch,
                fix_sha=reviewed_sha,
            )
            if review_number == MAX_REVIEWS:
                pr = persist_whole_limit_head_change(
                    github, pr, round_number=review_number,
                    reviewed_sha=whole_audit.reviewed_sha,
                    head=config.integration_branch, base=config.main_branch,
                )
            emit_finding(
                "git",
                "whole-version review changed the integration branch; "
                f"approval invalidated at {reviewed_sha}",
            )
            continue
        version_result, version_verdict = run_validated_turn(
            client,
            version_readme_reviewer,
            "Version / README reviewer turn",
            policy_context(
                config,
                scope="the version and README review",
                structured_conflicts=True,
            )
            + version_readme_review_prompt(pr, config, work_items),
            decision,
            iteration=review_number,
        )
        review_results.append(version_result)
        changes_requested = changes_requested or version_verdict == "CHANGES_REQUESTED"
        emit_policy_conflicts(version_result, config, scope="the integrated version")
        version_audit = allocate_review_audit(
            pr.body,
            "version_readme",
            version_verdict,
            pr.head_sha,
            version_result,
        )
        pr = persist_review_audit(
            github,
            pr,
            version_audit,
            head=config.integration_branch,
            base=config.main_branch,
        )
        if version_verdict == "CHANGES_REQUESTED":
            requested_change_audits.append(version_audit.audit_id)
        reviewed_sha, reviewer_changed = require_agent_result(
            repo,
            client,
            fixer,
            config.integration_branch,
            pr.head_sha,
            allow_unchanged=True,
            expected_process="cleanup",
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
            pending_audits = tuple(
                record.audit_id
                for record in review_audit_from_body(pr.body)
                if record.fix_disposition == "pending"
            )
            pr = review_audit_dispositions(
                github,
                pr,
                tuple(requested_change_audits)
                + pending_audits
                + (version_audit.audit_id,),
                "reviewer_changed_head",
                head=config.integration_branch,
                base=config.main_branch,
                fix_sha=reviewed_sha,
            )
            if review_number == MAX_REVIEWS:
                pr = persist_whole_limit_head_change(
                    github, pr, round_number=review_number,
                    reviewed_sha=version_audit.reviewed_sha,
                    head=config.integration_branch, base=config.main_branch,
                )
            emit_finding(
                "git",
                "version and README review changed the integration branch; "
                f"approval invalidated at {reviewed_sha}",
            )
            continue
        result = "\n\n".join(
            review_result
            for review_result in review_results
            if decision(review_result) == "CHANGES_REQUESTED"
        )
        verdict = "CHANGES_REQUESTED" if changes_requested else "APPROVED"
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
        warning: str | None = None
        if verdict == "CHANGES_REQUESTED":
            if review_number == MAX_REVIEWS:
                warning = (
                    f"Whole-version review limit {MAX_REVIEWS} reached with "
                    "CHANGES_REQUESTED; keeping the Base PR Draft and continuing "
                    "without reviewer approval."
                )
                current = review_audit_dispositions(
                    github,
                    current,
                    tuple(requested_change_audits),
                    "review_limit_reached",
                    head=config.integration_branch,
                    base=config.main_branch,
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
                        process="reviewer-fix",
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
                    expected_process="reviewer-fix",
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
                    pr = review_audit_dispositions(
                        github,
                        pr,
                        tuple(requested_change_audits),
                        "fixed",
                        head=config.integration_branch,
                        base=config.main_branch,
                        fix_sha=fixed_sha,
                    )
                    continue
                current = ensure_base_pr_policy_notes(github, current, config)
                current = review_audit_dispositions(
                    github,
                    current,
                    tuple(requested_change_audits),
                    "no_change_after_re_evaluation",
                    head=config.integration_branch,
                    base=config.main_branch,
                )
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
            expected_process="cleanup",
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
            pr = persist_review_audit(
                github, pr,
                whole_continuation_audit(
                    review_number, current.head_sha,
                    "review_limit_reached" if review_number == MAX_REVIEWS
                    else "no_change_after_re_evaluation",
                ),
                head=config.integration_branch, base=config.main_branch,
            )
            print(f"WARN: {warning}", flush=True)
            terminal_progress("WARN CONTINUATION", "Whole-version review")
            emit_finding("github", warning, status="warning")
            delivery = ReviewDelivery(
                "continued_with_warning",
                current.head_sha,
                current.base_sha,
                review_number,
                (warning,),
            )
        break
    if prior_limit is not None or completed_warning is not None:
        continuation_round = (
            MAX_REVIEWS if prior_limit is not None else completed_warning.round
        )
        disposition = (
            "review_limit_reached_after_head_change" if prior_limit is not None
            else completed_warning.fix_disposition
        )
        warning = whole_continuation_warning(disposition, continuation_round)
        run_final_checks(client, config)
        checked_sha, changed = require_agent_result(
            repo, client, fixer, config.integration_branch, pr.head_sha,
            allow_unchanged=True, expected_process="cleanup",
            iteration=continuation_round,
        )
        if changed or checked_sha != pr.head_sha:
            raise WorkerFailure(
                "final checks changed the integration branch at the review limit"
            )
        pr = require_warning_delivery(
            repo, github, pr, head=config.integration_branch,
            base=config.main_branch, expected_head_sha=pr.head_sha,
            expected_base_sha=pr.base_sha,
        )
        pr = persist_review_audit(
            github, pr,
            whole_continuation_audit(
                continuation_round, pr.head_sha, disposition,
            ),
            head=config.integration_branch, base=config.main_branch,
        )
        emit_finding("github", warning, status="warning")
        delivery = ReviewDelivery(
            "continued_with_warning", pr.head_sha, pr.base_sha,
            continuation_round, (warning,),
        )
    if delivery is None:
        raise WorkerFailure("whole-version review ended without a review outcome")
    return pr, delivery


def review_whole_version(
    config: Config,
    client: PurpleMuxCLIClient,
    repo: GitRepository,
    github: GitHubRepository,
    pr: PullRequestState,
    work_items: tuple[Issue, ...],
) -> tuple[PullRequestState, ReviewDelivery]:
    """Repeat a whole review when its audit vanished after safe reinspection."""
    for attempt in range(MAX_REPOSITORY_RECOVERIES + 1):
        try:
            return _review_whole_version(config, client, repo, github, pr, work_items)
        except MissingReviewAudit:
            if attempt == MAX_REPOSITORY_RECOVERIES:
                raise
            repo.require_clean()
            pushed = repo.require_pushed(config.integration_branch)
            if pushed.local_sha is None or pushed.remote_sha != pushed.local_sha:
                raise WorkerFailure("review audit recovery requires a pushed head")
            pr = github.require_pr(
                number=pr.number,
                head=config.integration_branch,
                base=config.main_branch,
                state="OPEN",
                expected_head_sha=pushed.local_sha,
                expected_base_sha=pr.base_sha,
                draft=True,
            )
            if pr.auto_merge_enabled or pr.merge_queue_entry is not None:
                raise WorkerFailure("review audit recovery requires a safe Draft PR")
            emit_finding(
                "github",
                f"Whole-version audit is missing; reviewing current head {pr.head_sha} again",
                status="warning",
            )
    raise WorkerFailure("review audit recovery retry limit exceeded")


def integration_delivery(
    config: Config,
    work_items: tuple[Issue, ...],
    client: PurpleMuxCLIClient,
    repo: GitRepository,
    github: GitHubRepository,
    deferred_deliveries: list[RepositoryDelivery] | None = None,
) -> PullRequestState | None:
    terminal_progress(
        "PREPARE",
        "Final integration PR",
        detail=f"{config.integration_branch} -> {config.main_branch}",
    )
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
        delivery = ReviewDelivery(
            "skipped", merged_pr.head_sha, merged_pr.base_sha, 0
        )
        emit_whole_review_result("skipped", 0, warnings=summary_warnings(None))
        if FINAL_REVIEW:
            emit_step(
                "Whole-version review",
                "completed",
                message=f"delivery already merged as PR #{merged_pr.number}",
            )
            terminal_progress(
                "DONE",
                "Whole-version review",
                detail=f"delivery already merged as PR #{merged_pr.number}",
            )
        emit_step(
            "Final integration PR",
            "completed",
            message=f"already merged as PR #{merged_pr.number}",
        )
        terminal_progress(
            "DONE",
            "Final integration PR",
            detail=f"already merged as PR #{merged_pr.number}",
        )
        if deferred_deliveries is not None:
            deferred_deliveries.append(
                RepositoryDelivery(
                    config,
                    work_items,
                    client,
                    repo,
                    github,
                    merged_pr,
                    delivery,
                    tuple(ISSUE_HANDOFF_RESULTS),
                    human_handoff_warnings(delivery),
                )
            )
        return merged_pr
    if pr is None:
        pr, finalized_plan = prepare_work_item_plan_pr(config, repo, github)
        if not finalized_plan.finalized or finalized_plan.snapshot != work_items:
            raise WorkerFailure(
                "final delivery work items do not match the finalized persisted plan"
            )
        if pr is None:
            delivery = ReviewDelivery(
                "skipped", integration.remote_sha, main.remote_sha, 0
            )
            emit_whole_review_result("skipped", 0, warnings=summary_warnings(None))
            emit_step(
                "Final integration PR",
                "completed",
                message="no implementation changes; no PR required",
            )
            terminal_progress(
                "DONE",
                "Final integration PR",
                detail="no implementation changes; no PR required",
            )
            if deferred_deliveries is not None:
                deferred_deliveries.append(
                    RepositoryDelivery(
                        config,
                        work_items,
                        client,
                        repo,
                        github,
                        None,
                        delivery,
                        tuple(ISSUE_HANDOFF_RESULTS),
                        human_handoff_warnings(delivery),
                    )
                )
            return None
        if pr.state == "MERGED":
            raise WorkerFailure(
                "final delivery was merged while its deferred PR was being acquired"
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
    terminal_progress(
        "IDENTIFIED", "Final integration PR", detail=f"PR #{pr.number} {pr.url}"
    )
    if FINAL_REVIEW:
        pr, delivery = run_outline_step(
            "Whole-version review",
            lambda: review_whole_version(
                config, client, repo, github, pr, work_items
            ),
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
                    expected_agent=IMPLEMENTER_AGENT,
                    expected_process="cleanup",
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
                    expected_process="cleanup",
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
        if deferred_deliveries is not None:
            deferred_deliveries.append(
                RepositoryDelivery(
                    config,
                    work_items,
                    client,
                    repo,
                    github,
                    delivered,
                    delivery,
                    tuple(ISSUE_HANDOFF_RESULTS),
                    human_handoff_warnings(delivery),
                )
            )
            return delivered
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


def report_repository_delivery(config: Config, ready: PullRequestState | None) -> None:
    if ready is None:
        print("No implementation changes; no whole-version PR is required.", flush=True)
        return
    if ready.state == "MERGED":
        outcome = "Merged"
    elif ready.is_draft:
        outcome = "Draft (warning continuation)"
    else:
        outcome = "Ready (not merged)"
    print(f"Whole-version PR is {outcome}: {ready.url}", flush=True)
    if config.policy_issue is not None:
        print(f"Policy Issue: {config.slug}#{config.policy_issue}", flush=True)


def recovery_authoritative_state(
    config: Config,
    repo: GitRepository,
    github: GitHubRepository,
    plan: WorkItemPlan | None = None,
) -> str:
    """Inspect current Git and PR state after a workflow failure."""
    state: dict[str, object] = {
        "repository": str(config.repo),
        "integration_branch": config.integration_branch,
        "final_branch": config.main_branch,
    }
    if plan is not None:
        try:
            base_pr = github.find_pr(
                head=config.integration_branch, base=config.main_branch, state="OPEN"
            )
            if base_pr is not None:
                persisted = work_item_plan_from_body(base_pr.body, config)
            else:
                merged = github.find_pr(
                    head=config.integration_branch,
                    base=config.main_branch,
                    state="MERGED",
                )
                if merged is not None:
                    persisted = work_item_plan_from_body(merged.body, config)
                else:
                    final = repo.inspect_branch(config.main_branch)
                    if final.remote_sha is None:
                        raise WorkerFailure("final remote branch is missing")
                    body = repo.inspect_remote_note(
                        deferred_work_item_plan_ref(config), final.remote_sha
                    )
                    persisted = (
                        WorkItemPlan(config)
                        if body is None
                        else work_item_plan_from_body(body, config)
                    )
            if serialized_work_item_plan(persisted) != serialized_work_item_plan(plan):
                state["process_plan_differs_from_persisted"] = True
            if plan.active is not None:
                if plan.active not in persisted.items:
                    raise WorkerFailure("active work item differs from persisted plan")
                persisted.active = plan.active
            plan = persisted
        except Exception as exc:
            state["work_item_plan_inspection_error"] = short_error(exc)
            plan = None
    if plan is None:
        state["work_item_plan"] = {
            "status": "unavailable before plan preparation completed",
            "seed_count": len(config.issues),
        }
    else:
        active = plan.active
        next_item = plan.remaining[0] if plan.remaining else None
        state["work_item_plan"] = {
            "position": plan.position,
            "total": len(plan.items),
            "finalized": plan.finalized,
            "active": (
                None
                if active is None
                else {
                    **planner_work_item_json(active),
                    "branch": active.branch,
                    "task_fingerprint": active.task_fingerprint,
                }
            ),
            "next_item": (
                None
                if next_item is None
                else {**planner_work_item_json(next_item), "branch": next_item.branch}
            ),
        }
    try:
        worktree = repo.inspect_worktree()
        state["worktree"] = {
            "current_branch": worktree.current_branch,
            "dirty": worktree.dirty,
            "status": [entry[:200] for entry in worktree.status[:20]],
        }
    except Exception as exc:
        state["worktree_inspection_error"] = short_error(exc)
    try:
        state["remote_heads"] = repo.inspect_remote_branches(
            (config.integration_branch, config.main_branch)
        )
    except Exception as exc:
        state["remote_heads_inspection_error"] = short_error(exc)
    if plan is not None and plan.active is not None:
        active = plan.active
        try:
            branch = repo.inspect_branch(active.branch)
            state["active_branch"] = {
                "name": branch.name,
                "local_sha": branch.local_sha,
                "remote_sha": branch.remote_sha,
                "current": branch.current,
            }
        except Exception as exc:
            state["active_branch_inspection_error"] = short_error(exc)
        active_prs = []
        for status in ("OPEN", "MERGED", "CLOSED"):
            try:
                pr = github.find_pr(
                    head=active.branch, base=config.integration_branch, state=status
                )
                if pr is not None:
                    active_prs.append(
                        {
                            "number": pr.number,
                            "state": pr.state,
                            "draft": pr.is_draft,
                            "head_sha": pr.head_sha,
                            "base_sha": pr.base_sha,
                            "merge_commit_sha": pr.merge_commit_sha,
                        }
                    )
            except Exception as exc:
                state[f"active_pr_{status.lower()}_inspection_error"] = short_error(exc)
        state["active_prs"] = active_prs
    try:
        pr = github.find_pr(
            head=config.integration_branch, base=config.main_branch, state="OPEN"
        )
        state["base_pr"] = (
            None
            if pr is None
            else {
                "number": pr.number,
                "head_sha": pr.head_sha,
                "base_sha": pr.base_sha,
                "draft": pr.is_draft,
            }
        )
    except Exception as exc:
        state["base_pr_inspection_error"] = short_error(exc)
    return json.dumps(state, ensure_ascii=True)


def require_recovery_retry_state(source: str, expected_source: str | None = None) -> None:
    """Require the topology needed to safely start a fresh repository pass."""
    state = json.loads(source)
    if any(key.endswith("_inspection_error") for key in state):
        raise WorkerFailure("recovery outcome is uncertain: topology inspection failed")
    worktree = state.get("worktree")
    remote_heads = state.get("remote_heads")
    if (
        not isinstance(worktree, dict)
        or worktree.get("dirty") is not False
        or not isinstance(remote_heads, dict)
        or not remote_heads.get(state["integration_branch"])
        or not remote_heads.get(state["final_branch"])
        or "base_pr" not in state
    ):
        raise WorkerFailure("recovery outcome is uncertain: repository state is incomplete")
    if state["work_item_plan"].get("active") is not None and (
        "active_branch" not in state or "active_prs" not in state
    ):
        raise WorkerFailure("recovery outcome is uncertain: active work item is incomplete")
    active = state["work_item_plan"].get("active")
    if active is not None:
        branch = state["active_branch"]
        prs = state["active_prs"]
        if (
            branch.get("name") != active["branch"]
            or branch.get("current") != (worktree.get("current_branch") == active["branch"])
            or not branch.get("local_sha")
            or branch.get("remote_sha") != branch.get("local_sha")
            or len(prs) > 1
            or any(
                not isinstance(pr.get("number"), int)
                or pr.get("state") not in ("OPEN", "MERGED", "CLOSED")
                or (pr.get("state") == "OPEN" and (
                    pr.get("head_sha") != branch["remote_sha"]
                    or pr.get("base_sha") != remote_heads[state["integration_branch"]]
                ))
                for pr in prs
            )
        ):
            raise WorkerFailure("recovery outcome is uncertain: active branch or PR changed")
    base_pr = state["base_pr"]
    if base_pr is not None and (
        base_pr.get("head_sha") != remote_heads[state["integration_branch"]]
        or base_pr.get("base_sha") != remote_heads[state["final_branch"]]
        or not isinstance(base_pr.get("number"), int)
    ):
        raise WorkerFailure("recovery outcome is uncertain: Base PR changed")
    if expected_source is not None:
        expected = json.loads(expected_source)
        prior_prs = expected.get("active_prs")
        if isinstance(prior_prs, list) and prior_prs:
            prior_numbers = {pr.get("number") for pr in prior_prs}
            current_prs = state.get("active_prs")
            if not isinstance(current_prs, list) or any(
                pr.get("number") not in prior_numbers for pr in current_prs
            ):
                raise WorkerFailure("recovery outcome is uncertain: active PR identity changed")
        prior_base = expected.get("base_pr")
        if prior_base is not None and base_pr is not None and (
            prior_base.get("number") != base_pr.get("number")
        ):
            raise WorkerFailure("recovery outcome is uncertain: Base PR identity changed")


def run_repository(
    config: Config,
    deferred_deliveries: list[RepositoryDelivery] | None = None,
) -> PullRequestState | None:
    POLICY_CONFLICT_WARNINGS.clear()
    AGENT_TURN_TIMEOUT_WARNINGS.clear()
    ISSUE_HANDOFF_RESULTS.clear()
    COMPLETED_ISSUE_PRS.clear()
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
    for recovery_attempt in range(MAX_REPOSITORY_RECOVERIES + 1):
        plan: WorkItemPlan | None = None
        delivery_count = len(deferred_deliveries) if deferred_deliveries is not None else 0
        try:
            plan_pr, plan = prepare_work_item_plan_pr(config, repo, github)
            work_items = run_outline_step(
                "Work items",
                lambda: process_work_items(config, client, repo, github, plan_pr, plan),
            )
            ready = integration_delivery(
                config, work_items, client, repo, github, deferred_deliveries
            )
        except Exception as exc:
            if isinstance(exc, (MutationOutcomeUnknown, WorkerInterrupted)) or (
                deferred_deliveries is not None
                and len(deferred_deliveries) != delivery_count
            ):
                raise
            if recovery_attempt == MAX_REPOSITORY_RECOVERIES:
                raise WorkerFailure("repository recovery retry limit exceeded") from exc
            recovery_worktree = repo.inspect_worktree()
            recovery_branch = recovery_worktree.current_branch
            if recovery_branch is None:
                raise WorkerFailure("repository recovery requires a current branch") from exc
            recovery_start = repo.inspect_branch(recovery_branch)
            if recovery_start.local_sha is None:
                raise WorkerFailure(
                    "repository recovery requires a local branch commit"
                ) from exc
            state = recovery_authoritative_state(config, repo, github, plan)
            report = recover_error(client, config, exc, state)
            print(
                f"Recovery: {report.summary} Retry safe: {report.retry_safe}. "
                f"Evidence: {report.evidence}",
                flush=True,
            )
            if not report.repaired or not report.retry_safe:
                raise
            repo.require_committed_result(
                recovery_branch,
                previous_sha=recovery_start.local_sha,
                allow_unchanged=True,
                expected_agent=IMPLEMENTER_AGENT,
                expected_process="recovery",
            )
            require_recovery_retry_state(
                recovery_authoritative_state(config, repo, github, plan), state
            )
            emit_finding(
                "runtime",
                f"Recovered workflow error: {short_error(exc)}",
                status="warning",
            )
            emit_finding(
                "runtime",
                f"Recovery repair: {report.summary} Evidence: {report.evidence}",
                status="warning",
            )
            continue
        if deferred_deliveries is None:
            report_repository_delivery(config, ready)
        return ready
    raise AssertionError("unreachable")


def main() -> None:
    declarations = issue_driven_repository_declarations()
    deliveries: list[RepositoryDelivery] | None = None
    if declarations:
        emit_issue_driven_repositories(declarations)
        deliveries = []
    for config in parse_repository_configs():
        run_repository(config, deliveries)
    if deliveries is not None:
        finalize_multi_repository_deliveries(deliveries)
        for delivery in deliveries:
            report_repository_delivery(delivery.config, delivery.pr)


if __name__ == "__main__":
    main()
