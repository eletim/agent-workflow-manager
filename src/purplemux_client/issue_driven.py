from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Protocol

from purplemux_client.errors import WorkerFailure
from purplemux_client.execution_context import _inspect_repository_declaration
from purplemux_client.git import BranchState, GitRepository
from purplemux_client.github import (
    GitHubRepository,
    PullRequestSnapshot,
    PullRequestState,
)
from purplemux_client.progress import emit_finding


@dataclass(frozen=True)
class IssueDrivenFinding:
    path: str
    message: str

    def as_json(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


class IssueDrivenValidationError(ValueError):
    def __init__(self, findings: list[IssueDrivenFinding]) -> None:
        super().__init__("issue-driven JSON validation failed")
        self.findings = tuple(findings)


IssueTopologyClassification = Literal["new", "recoverable", "already_integrated"]
INLINE_TASK_FINGERPRINT_MARKER = "agent-workflow-manager:inline-task-sha256:"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class IssueTopologyState:
    issue: int | str
    branch: str
    classification: IssueTopologyClassification
    feature_sha: str | None
    integration_sha: str


class _IssueGitRepository(Protocol):
    def inspect_branch(self, branch: str) -> Any: ...


class _CachedIssueGit:
    def __init__(self, remote_shas: dict[str, str | None]) -> None:
        self._remote_shas = remote_shas

    def inspect_branch(self, branch: str) -> BranchState:
        return BranchState(branch, None, self._remote_shas[branch], False)


class _IssueGitHubRepository(Protocol):
    def compare_commits(self, *, base_sha: str, head_sha: str) -> str: ...


class _IssuePullRequests(Protocol):
    def find_pr(
        self, *, head: str, base: str, state: Literal["OPEN", "MERGED", "CLOSED"]
    ) -> PullRequestState | None: ...

    def require_pr(
        self,
        *,
        head: str,
        base: str,
        number: int | None = None,
        state: Literal["OPEN", "MERGED", "CLOSED"] = "OPEN",
        expected_head_sha: str | None = None,
        expected_base_sha: str | None = None,
        draft: bool | None = None,
    ) -> PullRequestState: ...


def classify_issue_topology(
    repository: _IssueGitRepository,
    pull_requests: _IssuePullRequests,
    github: _IssueGitHubRepository,
    *,
    issue: int | str,
    branch: str,
    integration_branch: str,
    integration_sha: str,
    inline_task_fingerprint: str | None = None,
) -> IssueTopologyState:
    """Classify one Issue from authoritative remote Git and GitHub state."""
    try:
        feature = repository.inspect_branch(branch)
        open_pr = pull_requests.find_pr(
            head=branch, base=integration_branch, state="OPEN"
        )
        merged_pr = pull_requests.find_pr(
            head=branch, base=integration_branch, state="MERGED"
        )
        closed_pr = pull_requests.find_pr(
            head=branch, base=integration_branch, state="CLOSED"
        )
        matching = tuple(pr for pr in (open_pr, merged_pr, closed_pr) if pr is not None)
        if len(matching) > 1:
            numbers = ", ".join(f"#{pr.number}" for pr in matching)
            raise WorkerFailure(
                f"ambiguous PR states from {branch} to {integration_branch}: {numbers}"
            )
        for pr in matching:
            _require_inline_task_fingerprint(pr, inline_task_fingerprint)
        if closed_pr is not None:
            raise WorkerFailure(
                f"closed unmerged PR #{closed_pr.number} exists from {branch} "
                f"to {integration_branch}"
            )

        feature_sha = feature.remote_sha
        if feature_sha is None:
            if open_pr is not None:
                raise WorkerFailure(
                    f"open PR #{open_pr.number} exists but remote feature branch "
                    f"{branch} does not"
                )
            if merged_pr is None:
                return IssueTopologyState(issue, branch, "new", None, integration_sha)
            merged = pull_requests.require_pr(
                number=merged_pr.number,
                head=branch,
                base=integration_branch,
                state="MERGED",
            )
            if not _commit_is_contained(github, merged.head_sha, integration_sha):
                raise WorkerFailure(
                    f"merged PR #{merged.number} head {merged.head_sha} is not "
                    f"contained by current integration {integration_sha}"
                )
            return IssueTopologyState(
                issue, branch, "already_integrated", merged.head_sha, integration_sha
            )

        if open_pr is not None:
            pull_requests.require_pr(
                number=open_pr.number,
                head=branch,
                base=integration_branch,
                state="OPEN",
                expected_head_sha=feature_sha,
                expected_base_sha=integration_sha,
            )
        if merged_pr is not None:
            pull_requests.require_pr(
                number=merged_pr.number,
                head=branch,
                base=integration_branch,
                state="MERGED",
                expected_head_sha=feature_sha,
            )

        relationship = github.compare_commits(
            base_sha=integration_sha, head_sha=feature_sha
        )
        if relationship in {"behind", "identical"}:
            if open_pr is not None:
                raise WorkerFailure(
                    f"open PR #{open_pr.number} remains although {branch} is "
                    "already integrated"
                )
            return IssueTopologyState(
                issue, branch, "already_integrated", feature_sha, integration_sha
            )
        if merged_pr is not None:
            raise WorkerFailure(
                f"merged PR #{merged_pr.number} exists but current feature head "
                f"{feature_sha} is not integrated"
            )
        if relationship != "ahead":
            raise WorkerFailure(
                f"existing feature branch {branch} does not contain current "
                f"integration base {integration_sha} and is not already integrated"
            )
        return IssueTopologyState(
            issue, branch, "recoverable", feature_sha, integration_sha
        )
    except WorkerFailure as exc:
        label = _work_item_label(issue)
        if str(exc).startswith(f"{label}:"):
            raise
        raise WorkerFailure(f"{label}: {exc}") from exc


def _work_item_label(issue: int | str) -> str:
    return f"Issue #{issue}" if isinstance(issue, int) else issue


def _inline_task_fingerprints(body: str) -> tuple[str, ...]:
    prefix = f"<!-- {INLINE_TASK_FINGERPRINT_MARKER}"
    suffix = " -->"
    lines = body.splitlines()
    if not lines:
        return ()
    marker = lines[0].strip()
    if not marker.startswith(prefix) or not marker.endswith(suffix):
        return ()
    return (marker[len(prefix) : -len(suffix)],)


def _require_inline_task_fingerprint(
    pr: PullRequestState, expected: str | None
) -> None:
    if expected is None:
        return
    if _inline_task_fingerprints(pr.body) != (expected,):
        raise WorkerFailure(
            f"PR #{pr.number} inline task fingerprint is missing or does not match "
            "the declared task"
        )


def _commit_is_contained(
    github: _IssueGitHubRepository, commit_sha: str, branch_sha: str
) -> bool:
    return github.compare_commits(base_sha=commit_sha, head_sha=branch_sha) in {
        "ahead",
        "identical",
    }


def inspect_issue_driven_topology(
    *,
    repo: str,
    integration_branch: str,
    issues: tuple[tuple[int | str, str] | tuple[int | str, str, str], ...],
    prospective_base_branch: str | None = None,
    remote: str = "origin",
    command_timeout_seconds: float = 30.0,
    _cwd: Path | None = None,
) -> tuple[IssueTopologyState, ...]:
    """Inspect all Issue branches and PRs before any workflow mutation."""
    normalized: list[tuple[int | str, str, str | None]] = []
    for declaration in issues:
        if not isinstance(declaration, tuple) or len(declaration) not in (2, 3):
            raise ValueError("issues must contain work-item declarations")
        number, branch = declaration[:2]
        fingerprint = declaration[2] if len(declaration) == 3 else None
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, str))
            or (isinstance(number, int) and number < 1)
            or (isinstance(number, str) and not number.strip())
            or not isinstance(branch, str)
            or not branch
            or (
                fingerprint is not None
                and (
                    not isinstance(fingerprint, str)
                    or _SHA256.fullmatch(fingerprint) is None
                )
            )
            or (isinstance(number, str) and fingerprint is None)
            or (isinstance(number, int) and fingerprint is not None)
        ):
            raise ValueError("issues must contain valid work-item declarations")
        normalized.append((number, branch, fingerprint))
    if not normalized:
        raise ValueError("issues must contain work-item identifiers and branches")
    numbers = tuple(number for number, _branch, _fingerprint in normalized)
    branches = tuple(branch for _number, branch, _fingerprint in normalized)
    if len(set(numbers)) != len(numbers) or len(set(branches)) != len(branches):
        raise ValueError("work-item identifiers and feature branches must be unique")
    if integration_branch in branches:
        raise ValueError("integration and feature branches must differ")
    inspection_base = prospective_base_branch or integration_branch
    preparation = _inspect_repository_declaration(
        repo=repo,
        base_branch=inspection_base,
        remote=remote,
        command_timeout_seconds=command_timeout_seconds,
        cwd=_cwd,
    )
    repository = GitRepository.open(
        preparation.source_repository,
        remote=remote,
        command_timeout_seconds=command_timeout_seconds,
    )
    github = GitHubRepository.open(
        repository.expected_github_slug,
        command_timeout_seconds=command_timeout_seconds,
    )
    github_inspection = github.topology_inspection()
    remote_shas = repository.inspect_remote_branches((integration_branch, *branches))
    cached_repository = _CachedIssueGit(remote_shas)
    integration_sha = remote_shas[integration_branch]
    if integration_sha is None:
        if prospective_base_branch is None:
            raise WorkerFailure(
                f"remote integration branch {integration_branch!r} does not exist"
            )
        integration_sha = preparation.base_sha
    pull_requests = github_inspection.inspect_pr_snapshot(branches)
    comparison_pairs: list[tuple[str, str]] = []
    if prospective_base_branch is not None:
        comparison_pairs.append((preparation.base_sha, integration_sha))
    for branch in branches:
        feature_sha = remote_shas[branch]
        if feature_sha is not None:
            comparison_pairs.append((integration_sha, feature_sha))
            continue
        merged_pr = pull_requests.find_pr(
            head=branch, base=integration_branch, state="MERGED"
        )
        if merged_pr is not None:
            comparison_pairs.append((merged_pr.head_sha, integration_sha))
    comparisons = github_inspection.inspect_comparisons(comparison_pairs)
    if prospective_base_branch is not None and not _commit_is_contained(
        comparisons, preparation.base_sha, integration_sha
    ):
        raise WorkerFailure(
            f"existing integration branch {integration_branch!r} does not contain "
            f"prospective base {preparation.base_sha}"
        )

    states = tuple(
        classify_issue_topology(
            cached_repository,
            pull_requests,
            comparisons,
            issue=number,
            branch=branch,
            integration_branch=integration_branch,
            integration_sha=integration_sha,
            inline_task_fingerprint=fingerprint,
        )
        for number, branch, fingerprint in normalized
    )
    if (
        repository.inspect_remote_branches((integration_branch, *branches))
        != remote_shas
    ):
        raise WorkerFailure("remote branch topology changed during inspection")
    current_pull_requests = github_inspection.inspect_pr_snapshot(branches)
    if _pr_topology(current_pull_requests) != _pr_topology(pull_requests):
        raise WorkerFailure("GitHub PR topology changed during inspection")
    for state in states:
        label = _work_item_label(state.issue)
        if state.classification == "new":
            message = f"{label}: no existing feature branch; new run is safe"
        elif state.classification == "recoverable":
            message = f"{label}: existing feature branch / PR topology is recoverable"
        else:
            message = f"{label}: already integrated; execution may skip this work item"
        emit_finding("github", message, status="info")
    return states


def inspect_issue_driven_work_item_topology(
    *,
    repo: str,
    integration_branch: str,
    issue: tuple[int | str, str] | tuple[int | str, str, str],
    remote: str = "origin",
    command_timeout_seconds: float = 30.0,
) -> IssueTopologyState:
    """Authoritatively inspect one runtime-planned work item before dispatch."""
    return inspect_issue_driven_topology(
        repo=repo,
        integration_branch=integration_branch,
        issues=(issue,),
        remote=remote,
        command_timeout_seconds=command_timeout_seconds,
    )[0]


def _pr_topology(
    snapshot: PullRequestSnapshot,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                pr.number,
                pr.state,
                pr.head_repository.lower(),
                pr.head_branch,
                pr.head_sha,
                pr.base_repository.lower(),
                pr.base_branch,
                pr.base_sha,
                _inline_task_fingerprints(pr.body),
            )
            for pr in snapshot.pull_requests
        )
    )


@dataclass(frozen=True)
class WorkItem:
    issue: int | None = None
    id: str | None = None
    task: str | None = None

    @property
    def task_fingerprint(self) -> str | None:
        if self.task is None:
            return None
        return hashlib.sha256(self.task.encode()).hexdigest()

    @property
    def branch(self) -> str:
        if self.issue is not None:
            return f"feature/issue-{self.issue}"
        assert self.id is not None
        return f"feature/work-item-{self.id}"

    def as_json(self) -> int | dict[str, str]:
        if self.issue is not None:
            return self.issue
        assert self.id is not None and self.task is not None
        return {"id": self.id, "task": self.task}


@dataclass(frozen=True)
class IssueDrivenRepositoryConfig:
    repository: str
    integration_branch: str
    final_branch: str
    work_items: tuple[WorkItem, ...]

    @property
    def issues(self) -> tuple[int, ...]:
        return tuple(item.issue for item in self.work_items if item.issue is not None)

    def as_json(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "integration_branch": self.integration_branch,
            "final_branch": self.final_branch,
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class IssueDrivenConfig:
    repositories: tuple[IssueDrivenRepositoryConfig, ...]
    max_reviews: int
    merge_to_integration: bool
    final_review: bool
    merge_final: bool
    implementer_agent: str = "codex"
    reviewer_agent: str = "codex"
    policy_issue: int | None = None
    make_integration_branch: bool = False
    one_shot_issue: int | None = None
    scenarios: tuple[str, ...] = ()
    scope_max_reviews: int = 3

    @property
    def repository(self) -> str:
        return self.repositories[0].repository

    @property
    def integration_branch(self) -> str:
        return self.repositories[0].integration_branch

    @property
    def final_branch(self) -> str:
        return self.repositories[0].final_branch

    @property
    def work_items(self) -> tuple[WorkItem, ...]:
        return self.repositories[0].work_items

    @property
    def issues(self) -> tuple[int, ...]:
        """Return GitHub Issue numbers for compatibility with existing callers."""
        return tuple(item.issue for item in self.work_items if item.issue is not None)

    def as_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "mode": "issue-driven",
            "make_integration_branch": self.make_integration_branch,
            "max_reviews": self.max_reviews,
            "scope_max_reviews": self.scope_max_reviews,
            "merge_to_integration": self.merge_to_integration,
            "final_review": self.final_review,
            "merge_final": self.merge_final,
            "implementer_agent": self.implementer_agent,
            "reviewer_agent": self.reviewer_agent,
        }
        if len(self.repositories) > 1:
            result["repositories"] = [item.as_json() for item in self.repositories]
        else:
            result.update(
                {
                    "repository": self.repository,
                    "integration_branch": self.integration_branch,
                    "final_branch": self.final_branch,
                }
            )
        if self.policy_issue is not None:
            result["policy_issue"] = self.policy_issue
        if self.scenarios:
            result["scenarios"] = list(self.scenarios)
        if len(self.repositories) > 1:
            pass
        elif self.one_shot_issue is not None:
            result["one_shot_issue"] = self.one_shot_issue
        elif all(item.issue is not None for item in self.work_items):
            result["issues"] = [item.issue for item in self.work_items]
        else:
            result["work_items"] = [item.as_json() for item in self.work_items]
        return result


_REQUIRED_FIELDS = {
    "repository",
    "integration_branch",
    "final_branch",
    "max_reviews",
    "merge_to_integration",
    "final_review",
    "merge_final",
}
_OPTIONAL_FIELDS = {
    "mode",
    "make_integration_branch",
    "scope_max_reviews",
    "implementer_agent",
    "reviewer_agent",
    "policy_issue",
    "issues",
    "work_items",
    "one_shot_issue",
    "scenarios",
    "repositories",
}
_ALLOWED_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS
_SUPPORTED_AGENTS = {"codex", "claude"}
_MAX_INITIAL_WORK_ITEMS = 100
_MAX_WORK_ITEM_PLAN_STATE_BYTES = 32_000
_MAX_SCENARIOS = 100
_MAX_SCENARIO_CHARS = 4_000
_MAX_SCENARIO_LIST_BYTES = 64_000
_WORK_ITEM_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _work_item_plan_state_size(
    work_items: list[WorkItem], policy_issue: object, one_shot_issue: object
) -> int:
    items = [
        {"issue": item.issue, "branch": item.branch}
        if item.issue is not None
        else item.as_json()
        for item in work_items
    ]
    seed_value = {
        "items": items,
        "one_shot_issue": one_shot_issue,
        "policy_issue": policy_issue,
    }
    seed = json.dumps(seed_value, ensure_ascii=False, separators=(",", ":"))
    payload = {
        "version": 1,
        "seed_sha256": hashlib.sha256(seed.encode()).hexdigest(),
        "items": items,
        "position": len(items),
        "finalized": False,
    }
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def _scenario_list_size(scenarios: list[str]) -> int:
    numbered = "\n".join(
        f"{index}. {scenario}" for index, scenario in enumerate(scenarios, 1)
    )
    return len(numbered.encode())


def _valid_branch_name(value: str) -> bool:
    forbidden = set(" ~^:?*[\\")
    components = value.split("/")
    return not (
        value == "@"
        or value.startswith(("-", ".", "/"))
        or value.endswith((".", "/", ".lock"))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(
            character in forbidden or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in components
        )
    )


def _parse_single_issue_driven_json(source: str) -> IssueDrivenConfig:
    """Parse the intentionally small Issue Driven configuration."""
    if not isinstance(source, str):
        raise TypeError("source must be a string")
    duplicate_keys: list[str] = []

    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicate_keys.append(key)
            result[key] = value
        return result

    try:
        value = json.loads(source, object_pairs_hook=object_from_pairs)
    except json.JSONDecodeError as exc:
        raise IssueDrivenValidationError(
            [IssueDrivenFinding("$", f"invalid JSON at line {exc.lineno}: {exc.msg}")]
        ) from exc
    if not isinstance(value, dict):
        raise IssueDrivenValidationError(
            [IssueDrivenFinding("$", "top-level value must be an object")]
        )
    findings: list[IssueDrivenFinding] = []
    for key in sorted(set(duplicate_keys)):
        findings.append(IssueDrivenFinding(f"$.{key}", "field is duplicated"))
    for key in sorted(_REQUIRED_FIELDS - set(value)):
        findings.append(IssueDrivenFinding(f"$.{key}", "required field is missing"))
    for key in sorted(set(value) - _ALLOWED_FIELDS):
        findings.append(IssueDrivenFinding(f"$.{key}", "unknown field is not allowed"))
    if "mode" in value and value["mode"] != "issue-driven":
        findings.append(IssueDrivenFinding("$.mode", "must be exactly 'issue-driven'"))
    work_item_fields = {"issues", "work_items", "one_shot_issue"} & set(value)
    if not work_item_fields:
        findings.append(
            IssueDrivenFinding(
                "$.work_items",
                "one_shot_issue, work_items, or the legacy issues field is required",
            )
        )
    if "issues" in value and "work_items" in value:
        findings.append(
            IssueDrivenFinding("$.work_items", "must not be combined with issues")
        )
    if "one_shot_issue" in value and ({"issues", "work_items"} & set(value)):
        findings.append(
            IssueDrivenFinding(
                "$.one_shot_issue",
                "must not be combined with issues or work_items",
            )
        )
    raw_policy_issue = value.get("policy_issue")
    policy_issue = (
        raw_policy_issue
        if isinstance(raw_policy_issue, int)
        and not isinstance(raw_policy_issue, bool)
        and raw_policy_issue > 0
        else None
    )
    if "policy_issue" in value and policy_issue is None:
        findings.append(
            IssueDrivenFinding("$.policy_issue", "must be a positive integer")
        )
    raw_one_shot_issue = value.get("one_shot_issue")
    one_shot_issue = (
        raw_one_shot_issue
        if isinstance(raw_one_shot_issue, int)
        and not isinstance(raw_one_shot_issue, bool)
        and raw_one_shot_issue > 0
        else None
    )
    if "one_shot_issue" in value and one_shot_issue is None:
        findings.append(
            IssueDrivenFinding("$.one_shot_issue", "must be a positive integer")
        )
    for key in ("repository", "integration_branch", "final_branch"):
        item = value.get(key)
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or "\0" in item
        ):
            findings.append(
                IssueDrivenFinding(f"$.{key}", "must be a non-empty trimmed string")
            )
    integration = value.get("integration_branch")
    final = value.get("final_branch")
    for key, branch in (("integration_branch", integration), ("final_branch", final)):
        if isinstance(branch, str) and branch and not _valid_branch_name(branch):
            findings.append(
                IssueDrivenFinding(f"$.{key}", "must be a valid Git branch name")
            )
    if isinstance(integration, str) and integration == final:
        findings.append(
            IssueDrivenFinding("$.final_branch", "must differ from integration_branch")
        )
    items_key = "work_items" if "work_items" in value else "issues"
    raw_items = value.get(items_key, [])
    work_items: list[WorkItem] = []
    if "one_shot_issue" not in value and (
        not isinstance(raw_items, list) or not raw_items
    ):
        findings.append(
            IssueDrivenFinding(f"$.{items_key}", "must be a non-empty array")
        )
    elif "one_shot_issue" not in value:
        if len(raw_items) > _MAX_INITIAL_WORK_ITEMS:
            findings.append(
                IssueDrivenFinding(
                    f"$.{items_key}",
                    f"must contain at most {_MAX_INITIAL_WORK_ITEMS} items",
                )
            )
        seen_issues: set[int] = set()
        seen_ids: set[str] = set()
        for index, item in enumerate(raw_items):
            path = f"$.{items_key}[{index}]"
            if (
                isinstance(item, bool)
                or not isinstance(item, (int, dict))
                or (items_key == "issues" and not isinstance(item, int))
            ):
                findings.append(
                    IssueDrivenFinding(
                        path,
                        (
                            "must be a positive integer"
                            if items_key == "issues"
                            else "must be a positive Issue number or a mini-task object"
                        ),
                    )
                )
            elif isinstance(item, int):
                if item < 1:
                    findings.append(
                        IssueDrivenFinding(path, "must be a positive Issue number")
                    )
                elif item in seen_issues:
                    findings.append(IssueDrivenFinding(path, "must be unique"))
                else:
                    seen_issues.add(item)
                    work_items.append(WorkItem(issue=item))
            else:
                unknown = sorted(set(item) - {"id", "task"})
                for key in unknown:
                    findings.append(
                        IssueDrivenFinding(
                            f"{path}.{key}", "unknown field is not allowed"
                        )
                    )
                for key in sorted({"id", "task"} - set(item)):
                    findings.append(
                        IssueDrivenFinding(f"{path}.{key}", "required field is missing")
                    )
                item_id = item.get("id")
                task = item.get("task")
                id_valid = (
                    isinstance(item_id, str)
                    and _WORK_ITEM_ID.fullmatch(item_id) is not None
                    and len(item_id) <= 50
                )
                duplicate_id = id_valid and item_id in seen_ids
                if not id_valid:
                    findings.append(
                        IssueDrivenFinding(
                            f"{path}.id",
                            "must be a lowercase kebab-case identifier of at most 50 characters",
                        )
                    )
                elif duplicate_id:
                    findings.append(IssueDrivenFinding(f"{path}.id", "must be unique"))
                else:
                    assert isinstance(item_id, str)
                    seen_ids.add(item_id)
                task_has_surrogate = isinstance(task, str) and any(
                    0xD800 <= ord(character) <= 0xDFFF for character in task
                )
                task_valid = (
                    isinstance(task, str)
                    and bool(task)
                    and task == task.strip()
                    and "\0" not in task
                    and len(task) <= 4000
                    and not task_has_surrogate
                )
                if task_has_surrogate:
                    findings.append(
                        IssueDrivenFinding(
                            f"{path}.task", "must contain only Unicode scalar values"
                        )
                    )
                elif not task_valid:
                    findings.append(
                        IssueDrivenFinding(
                            f"{path}.task",
                            "must be a non-empty trimmed string of at most 4000 characters",
                        )
                    )
                if id_valid and not duplicate_id and task_valid and not unknown:
                    assert isinstance(item_id, str) and isinstance(task, str)
                    work_items.append(WorkItem(id=item_id, task=task))
        if (
            len(work_items) == len(raw_items)
            and _work_item_plan_state_size(work_items, policy_issue, one_shot_issue)
            > _MAX_WORK_ITEM_PLAN_STATE_BYTES
        ):
            findings.append(
                IssueDrivenFinding(
                    f"$.{items_key}",
                    "serialized recovery state must not exceed 32000 bytes",
                )
            )
        generated_branches = {item.branch for item in work_items}
        if len(generated_branches) != len(work_items):
            findings.append(
                IssueDrivenFinding(
                    f"$.{items_key}", "generated branches must be unique"
                )
            )
        for key, branch in (
            ("integration_branch", integration),
            ("final_branch", final),
        ):
            if isinstance(branch, str) and branch in generated_branches:
                findings.append(
                    IssueDrivenFinding(
                        f"$.{key}",
                        (
                            "must differ from every generated Issue branch"
                            if items_key == "issues"
                            else "must differ from every generated work-item branch"
                        ),
                    )
                )
    if policy_issue is not None:
        if policy_issue in {
            item.issue for item in work_items if item.issue is not None
        }:
            findings.append(
                IssueDrivenFinding(
                    "$.policy_issue", "must differ from every implementation Issue"
                )
            )
    max_reviews = value.get("max_reviews")
    if (
        isinstance(max_reviews, bool)
        or not isinstance(max_reviews, int)
        or not 1 <= max_reviews <= 100
    ):
        findings.append(
            IssueDrivenFinding("$.max_reviews", "must be an integer from 1 to 100")
        )
    scope_max_reviews = value.get("scope_max_reviews", 3)
    if (
        isinstance(scope_max_reviews, bool)
        or not isinstance(scope_max_reviews, int)
        or not 1 <= scope_max_reviews <= 100
    ):
        findings.append(
            IssueDrivenFinding(
                "$.scope_max_reviews", "must be an integer from 1 to 100"
            )
        )
    for key in (
        "make_integration_branch",
        "merge_to_integration",
        "final_review",
        "merge_final",
    ):
        if key == "make_integration_branch" and key not in value:
            continue
        if not isinstance(value.get(key), bool):
            findings.append(IssueDrivenFinding(f"$.{key}", "must be a boolean"))
    for key in ("implementer_agent", "reviewer_agent"):
        agent = value.get(key, "codex")
        if not isinstance(agent, str):
            findings.append(IssueDrivenFinding(f"$.{key}", "must be a string"))
        elif agent not in _SUPPORTED_AGENTS:
            findings.append(
                IssueDrivenFinding(f"$.{key}", "must be one of: codex, claude")
            )
    raw_scenarios = value.get("scenarios", [])
    scenarios: list[str] = []
    if not isinstance(raw_scenarios, list):
        findings.append(IssueDrivenFinding("$.scenarios", "must be an array"))
    else:
        if len(raw_scenarios) > _MAX_SCENARIOS:
            findings.append(
                IssueDrivenFinding(
                    "$.scenarios", f"must contain at most {_MAX_SCENARIOS} items"
                )
            )
        seen_scenarios: set[str] = set()
        for index, scenario in enumerate(raw_scenarios):
            path = f"$.scenarios[{index}]"
            has_surrogate = isinstance(scenario, str) and any(
                0xD800 <= ord(character) <= 0xDFFF for character in scenario
            )
            if has_surrogate:
                findings.append(
                    IssueDrivenFinding(path, "must contain only Unicode scalar values")
                )
            elif (
                not isinstance(scenario, str)
                or not scenario
                or scenario != scenario.strip()
                or "\0" in scenario
                or len(scenario) > _MAX_SCENARIO_CHARS
            ):
                findings.append(
                    IssueDrivenFinding(
                        path,
                        "must be a non-empty trimmed string of at most 4000 characters",
                    )
                )
            elif scenario in seen_scenarios:
                findings.append(IssueDrivenFinding(path, "must be unique"))
            else:
                seen_scenarios.add(scenario)
                scenarios.append(scenario)
        if (
            len(scenarios) == len(raw_scenarios)
            and _scenario_list_size(scenarios) > _MAX_SCENARIO_LIST_BYTES
        ):
            findings.append(
                IssueDrivenFinding(
                    "$.scenarios",
                    "numbered Scenario List must encode to at most 64000 UTF-8 bytes",
                )
            )
    if scenarios and value.get("final_review") is False:
        findings.append(
            IssueDrivenFinding("$.scenarios", "requires final_review to be true")
        )
    if findings:
        raise IssueDrivenValidationError(findings)
    return IssueDrivenConfig(
        repositories=(
            IssueDrivenRepositoryConfig(
                value["repository"],
                value["integration_branch"],
                value["final_branch"],
                tuple(work_items),
            ),
        ),
        make_integration_branch=value.get("make_integration_branch", False),
        max_reviews=value["max_reviews"],
        merge_to_integration=value["merge_to_integration"],
        final_review=value["final_review"],
        merge_final=value["merge_final"],
        scope_max_reviews=scope_max_reviews,
        implementer_agent=value.get("implementer_agent", "codex"),
        reviewer_agent=value.get("reviewer_agent", "codex"),
        policy_issue=policy_issue,
        one_shot_issue=one_shot_issue,
        scenarios=tuple(scenarios),
    )


_REPOSITORY_FIELDS = {
    "repository",
    "integration_branch",
    "final_branch",
    "issues",
}
_MULTI_REQUIRED_FIELDS = _REQUIRED_FIELDS - _REPOSITORY_FIELDS | {"repositories"}
_MULTI_ALLOWED_FIELDS = (
    _ALLOWED_FIELDS - _REPOSITORY_FIELDS - {"work_items", "one_shot_issue"}
) | {"repositories"}


def parse_issue_driven_json(source: str) -> IssueDrivenConfig:
    """Parse single- or multi-repository Issue Driven configuration."""
    if not isinstance(source, str):
        raise TypeError("source must be a string")

    class ParsedObject(dict[str, Any]):
        def __init__(self, pairs: list[tuple[str, Any]]) -> None:
            super().__init__()
            duplicates: list[str] = []
            for key, item in pairs:
                if key in self:
                    duplicates.append(key)
                self[key] = item
            self.duplicates = tuple(duplicates)

    try:
        value = json.loads(source, object_pairs_hook=ParsedObject)
    except json.JSONDecodeError:
        return _parse_single_issue_driven_json(source)
    if not isinstance(value, dict) or "repositories" not in value:
        return _parse_single_issue_driven_json(source)

    findings: list[IssueDrivenFinding] = []
    for key in sorted(set(value.duplicates)):
        findings.append(IssueDrivenFinding(f"$.{key}", "field is duplicated"))
    for key in sorted(_MULTI_REQUIRED_FIELDS - set(value)):
        findings.append(IssueDrivenFinding(f"$.{key}", "required field is missing"))
    for key in sorted(set(value) - _MULTI_ALLOWED_FIELDS):
        findings.append(IssueDrivenFinding(f"$.{key}", "unknown field is not allowed"))
    raw_repositories = value.get("repositories")
    if not isinstance(raw_repositories, list) or len(raw_repositories) < 2:
        findings.append(
            IssueDrivenFinding(
                "$.repositories", "must contain at least two repositories"
            )
        )
        raw_repositories = []

    shared = {key: item for key, item in value.items() if key != "repositories"}
    parsed: list[tuple[int, IssueDrivenConfig]] = []
    for index, declaration in enumerate(raw_repositories):
        path = f"$.repositories[{index}]"
        if not isinstance(declaration, dict):
            findings.append(IssueDrivenFinding(path, "must be an object"))
            continue
        if isinstance(declaration, ParsedObject):
            for key in sorted(set(declaration.duplicates)):
                findings.append(
                    IssueDrivenFinding(f"{path}.{key}", "field is duplicated")
                )
        for key in sorted(_REPOSITORY_FIELDS - set(declaration)):
            findings.append(
                IssueDrivenFinding(f"{path}.{key}", "required field is missing")
            )
        for key in sorted(set(declaration) - _REPOSITORY_FIELDS):
            findings.append(
                IssueDrivenFinding(f"{path}.{key}", "unknown field is not allowed")
            )
        if set(declaration) != _REPOSITORY_FIELDS:
            continue
        candidate = {**shared, **declaration}
        try:
            parsed.append(
                (
                    index,
                    _parse_single_issue_driven_json(
                        json.dumps(candidate, ensure_ascii=False)
                    ),
                )
            )
        except IssueDrivenValidationError as exc:
            for finding in exc.findings:
                field = finding.path.removeprefix("$.")
                finding_path = (
                    f"{path}.{field}"
                    if any(
                        field == repository_field
                        or field.startswith(f"{repository_field}[")
                        for repository_field in _REPOSITORY_FIELDS
                    )
                    else finding.path
                )
                findings.append(IssueDrivenFinding(finding_path, finding.message))

    indexed_repositories = [(index, item.repositories[0]) for index, item in parsed]
    seen_repositories: set[str] = set()
    for index, repository in indexed_repositories:
        if repository.repository in seen_repositories:
            findings.append(
                IssueDrivenFinding(
                    f"$.repositories[{index}].repository", "must be unique"
                )
            )
        seen_repositories.add(repository.repository)
    if findings:
        raise IssueDrivenValidationError(findings)

    repositories = tuple(repository for _index, repository in indexed_repositories)
    first = parsed[0][1]
    return IssueDrivenConfig(
        repositories=repositories,
        make_integration_branch=first.make_integration_branch,
        max_reviews=first.max_reviews,
        merge_to_integration=first.merge_to_integration,
        final_review=first.final_review,
        merge_final=first.merge_final,
        scope_max_reviews=first.scope_max_reviews,
        implementer_agent=first.implementer_agent,
        reviewer_agent=first.reviewer_agent,
        policy_issue=first.policy_issue,
        scenarios=first.scenarios,
    )


def _canonical_source() -> str:
    development_copy = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "sequential-version-development.py"
    )
    if development_copy.is_file():
        return development_copy.read_text(encoding="utf-8")
    packaged = resources.files("purplemux_client").joinpath(
        "_issue_driven_sequential_template.py"
    )
    return packaged.read_text(encoding="utf-8")


def _fixed_config_function(
    config: IssueDrivenConfig, *, function_name: str = "parse_args"
) -> str:
    issues = ",\n        ".join(
        (
            f"Issue({item.issue}, {item.branch!r})"
            if item.issue is not None
            else (
                f"Issue(None, {item.branch!r}, {item.id!r}, {item.task!r}, "
                f"{item.task_fingerprint!r})"
            )
        )
        for item in config.work_items
    )
    issue_tuple = f"(\n        {issues},\n        )" if issues else "()"
    base_branch = (
        config.final_branch
        if config.make_integration_branch
        else config.integration_branch
    )
    prepare_integration = ""
    if config.make_integration_branch:
        prepare_integration = f"""    integration = repository.prepare_feature_branch(
        {config.integration_branch!r},
        base={config.final_branch!r},
        expected_base_sha=context.base_sha,
    )
    assert integration.local_sha is not None
    repository.ensure_pushed(
        {config.integration_branch!r},
        expected_local_sha=integration.local_sha,
    )
"""
    topology_issues = ",\n        ".join(
        (
            f"({item.issue!r}, {item.branch!r})"
            if item.issue is not None
            else (
                f"({f'Mini task {item.id}'!r}, {item.branch!r}, "
                f"{item.task_fingerprint!r})"
            )
        )
        for item in config.work_items
    )
    prospective = config.final_branch if config.make_integration_branch else None
    topology_inspection = ""
    if config.work_items:
        topology_inspection = f"""    inspect_issue_driven_topology(
        repo={config.repository!r},
        integration_branch={config.integration_branch!r},
        issues=(
        {topology_issues},
        ),
        prospective_base_branch={prospective!r},
    )
"""
    return f"""def {function_name}() -> Config:
{topology_inspection}    context = prepare_run_repository(
        repo={config.repository!r},
        base_branch={base_branch!r},
    )
    repository = GitRepository.open(
        context.execution_root,
        command_timeout_seconds=COMMAND_TIMEOUT,
    )
{prepare_integration}    return Config(
        context.execution_root,
        repository.expected_github_slug,
        {config.integration_branch!r},
        {config.final_branch!r},
        {issue_tuple},
        "git diff --check",
        WORKFLOW_POLICY_ISSUE,
        {config.one_shot_issue!r},
    )


"""


def _fixed_config_functions(config: IssueDrivenConfig) -> str:
    if len(config.repositories) == 1:
        return (
            _fixed_config_function(config)
            + "def parse_repository_configs():\n"
            + "    yield parse_args()\n\n\n"
            + "def issue_driven_repository_declarations():\n"
            + "    return ()\n\n\n"
        )
    declarations: list[str] = []
    functions: list[str] = []
    function_names: list[str] = []
    for index, repository in enumerate(config.repositories, 1):
        issues = ", ".join(
            f"Issue({item.issue}, {item.branch!r})" for item in repository.work_items
        )
        issue_tuple = f"({issues},)" if issues else "()"
        declarations.append(
            "    {\n"
            f'        "repository": {repository.repository!r},\n'
            f'        "integration_branch": {repository.integration_branch!r},\n'
            f'        "final_branch": {repository.final_branch!r},\n'
            f'        "policy_issue": {config.policy_issue!r},\n'
            f'        "issues": {issue_tuple},\n'
            "    },"
        )
        repository_config = replace(config, repositories=(repository,))
        function_name = f"parse_repository_{index}"
        function_names.append(function_name)
        functions.append(
            _fixed_config_function(repository_config, function_name=function_name)
        )
    repository_tuple = "\n".join(declarations)
    preparation_functions = "".join(functions)
    repository_yields = "".join(
        "    emit_issue_driven_repository(\n"
        f'        {index}, "started"\n'
        "    )\n"
        f"    yield {'parse_args' if index == 1 else function_name}()\n"
        "    emit_issue_driven_repository(\n"
        f'        {index}, "completed"\n'
        "    )\n"
        for index, function_name in enumerate(function_names, 1)
    )
    return (
        "ISSUE_DRIVEN_REPOSITORIES = (\n"
        f"{repository_tuple}\n"
        ")\n\n\n"
        f"{preparation_functions}"
        "def parse_args() -> Config:\n"
        f"    return {function_names[0]}()\n\n\n"
        "def parse_repository_configs():\n"
        f"{repository_yields}\n\n"
        "def issue_driven_repository_declarations():\n"
        "    return tuple(\n"
        "        (\n"
        "            item['repository'],\n"
        "            item['integration_branch'],\n"
        "            item['final_branch'],\n"
        "            item['policy_issue'],\n"
        "        )\n"
        "        for item in ISSUE_DRIVEN_REPOSITORIES\n"
        "    )\n\n\n"
    )


def _workflow_outline(config: IssueDrivenConfig) -> str:
    labels = ["Work items"]
    if config.final_review:
        labels.append("Whole-version review")
    labels.append("Final integration PR")
    entries = "\n".join(f"    {label!r}," for label in labels)
    return f"WORKFLOW_OUTLINE = [\n{entries}\n]"


def generate_issue_driven_workflow(config: IssueDrivenConfig) -> str:
    """Generate a deterministic plain-Python workflow using new-run recovery."""
    source = _canonical_source()
    source = source.replace(
        "    PurpleMuxRuntime,\n",
        "    PurpleMuxRuntime,\n    inspect_issue_driven_topology,\n"
        "    prepare_run_repository,\n",
        1,
    )
    if len(config.repositories) > 1:
        source = source.replace(
            "    emit_issue_driven_repositories,\n",
            "    emit_issue_driven_repositories,\n    emit_issue_driven_repository,\n",
            1,
        )
    outline_start = source.index("WORKFLOW_OUTLINE = [\n")
    outline_end = source.index("\n]", outline_start) + len("\n]")
    source = source[:outline_start] + _workflow_outline(config) + source[outline_end:]
    source = source.replace("MAX_REVIEWS = 4", f"MAX_REVIEWS = {config.max_reviews}", 1)
    source = source.replace(
        "MAX_SCOPE_REVIEWS = 6",
        f"MAX_SCOPE_REVIEWS = {config.scope_max_reviews}",
        1,
    )
    source = source.replace(
        'IMPLEMENTER_AGENT = "codex"',
        f"IMPLEMENTER_AGENT = {config.implementer_agent!r}",
        1,
    )
    source = source.replace(
        'REVIEWER_AGENT = "codex"',
        f"REVIEWER_AGENT = {config.reviewer_agent!r}",
        1,
    )
    source = source.replace(
        "WORKFLOW_POLICY_ISSUE = None",
        f"WORKFLOW_POLICY_ISSUE = {config.policy_issue!r}",
        1,
    )
    source = source.replace(
        "SCENARIOS: tuple[str, ...] = ()",
        f"SCENARIOS: tuple[str, ...] = {config.scenarios!r}",
        1,
    )
    source = source.replace(
        "MERGE_TO_INTEGRATION = True",
        f"MERGE_TO_INTEGRATION = {config.merge_to_integration}",
        1,
    )
    source = source.replace(
        "FINAL_REVIEW = True", f"FINAL_REVIEW = {config.final_review}", 1
    )
    source = source.replace(
        "MERGE_FINAL = False", f"MERGE_FINAL = {config.merge_final}", 1
    )
    start = source.index("def parse_args() -> Config:\n")
    end = source.index("def short_error(", start)
    return source[:start] + _fixed_config_functions(config) + source[end:]
