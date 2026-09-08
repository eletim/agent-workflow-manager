from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from purplemux_client.errors import WorkerFailure
from purplemux_client.execution_context import _inspect_repository_declaration
from purplemux_client.git import BranchState, GitRepository
from purplemux_client.github import GitHubRepository, PullRequestState
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


@dataclass(frozen=True)
class IssueTopologyState:
    issue: int
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
    def find_pr(
        self, *, head: str, base: str, state: Literal["OPEN", "MERGED", "CLOSED"]
    ) -> PullRequestState | None: ...

    def require_pr(self, **kwargs: object) -> PullRequestState: ...

    def compare_commits(self, *, base_sha: str, head_sha: str) -> str: ...


class _CachedIssueGitHub:
    """Reuse one bounded PR enumeration across every Issue classification."""

    def __init__(
        self, github: GitHubRepository, pull_requests: tuple[PullRequestState, ...]
    ) -> None:
        self._github = github
        self._pull_requests = pull_requests

    def find_pr(
        self, *, head: str, base: str, state: Literal["OPEN", "MERGED", "CLOSED"]
    ) -> PullRequestState | None:
        candidates = tuple(
            pr
            for pr in self._pull_requests
            if pr.head_repository.lower() == self._github.slug.lower()
            and pr.head_branch == head
            and pr.state == state
        )
        if state == "OPEN":
            wrong = tuple(pr for pr in candidates if pr.base_branch != base)
            if wrong:
                descriptions = ", ".join(
                    f"#{pr.number}->{pr.base_branch}" for pr in wrong
                )
                raise WorkerFailure(
                    f"open PR(s) from {head!r} target the wrong base: "
                    f"{descriptions}; expected {base!r}"
                )
        exact = tuple(pr for pr in candidates if pr.base_branch == base)
        if len(exact) > 1:
            numbers = ", ".join(f"#{pr.number}" for pr in exact)
            raise WorkerFailure(
                f"ambiguous {state.lower()} PRs from {head!r} to {base!r}: {numbers}"
            )
        return exact[0] if exact else None

    def require_pr(self, **kwargs: object) -> PullRequestState:
        head = str(kwargs["head"])
        base = str(kwargs["base"])
        state_value = kwargs.get("state", "OPEN")
        if state_value not in {"OPEN", "MERGED", "CLOSED"}:
            raise ValueError(f"unsupported PR state: {state_value!r}")
        state = cast(Literal["OPEN", "MERGED", "CLOSED"], state_value)
        pr = self.find_pr(head=head, base=base, state=state)
        if pr is None:
            raise WorkerFailure(f"no {str(state).lower()} PR from {head!r} to {base!r}")
        if kwargs.get("number") is not None and pr.number != kwargs["number"]:
            raise WorkerFailure(
                f"PR identity changed: expected #{kwargs['number']}, found #{pr.number}"
            )
        expected_head_sha = kwargs.get("expected_head_sha")
        expected_base_sha = kwargs.get("expected_base_sha")
        if expected_head_sha is not None and pr.head_sha != expected_head_sha:
            raise WorkerFailure(
                f"PR head changed: expected {expected_head_sha}, found {pr.head_sha}"
            )
        if expected_base_sha is not None and pr.base_sha != expected_base_sha:
            raise WorkerFailure(
                f"PR base changed: expected {expected_base_sha}, found {pr.base_sha}"
            )
        expected = self._github.slug.lower()
        if (
            pr.head_repository.lower() != expected
            or pr.base_repository.lower() != expected
        ):
            raise WorkerFailure("PR crosses an unexpected repository")
        return pr

    def compare_commits(self, *, base_sha: str, head_sha: str) -> str:
        return self._github.compare_commits(base_sha=base_sha, head_sha=head_sha)


def classify_issue_topology(
    repository: _IssueGitRepository,
    github: _IssueGitHubRepository,
    *,
    issue: int,
    branch: str,
    integration_branch: str,
    integration_sha: str,
) -> IssueTopologyState:
    """Classify one Issue from authoritative remote Git and GitHub state."""
    try:
        feature = repository.inspect_branch(branch)
        open_pr = github.find_pr(head=branch, base=integration_branch, state="OPEN")
        merged_pr = github.find_pr(head=branch, base=integration_branch, state="MERGED")
        closed_pr = github.find_pr(head=branch, base=integration_branch, state="CLOSED")
        matching = tuple(pr for pr in (open_pr, merged_pr, closed_pr) if pr is not None)
        if len(matching) > 1:
            numbers = ", ".join(f"#{pr.number}" for pr in matching)
            raise WorkerFailure(
                f"ambiguous PR states from {branch} to {integration_branch}: {numbers}"
            )
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
            merged = github.require_pr(
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
            github.require_pr(
                number=open_pr.number,
                head=branch,
                base=integration_branch,
                state="OPEN",
                expected_head_sha=feature_sha,
                expected_base_sha=integration_sha,
            )
        if merged_pr is not None:
            github.require_pr(
                number=merged_pr.number,
                head=branch,
                base=integration_branch,
                state="MERGED",
                expected_head_sha=feature_sha,
            )

        already_integrated = _commit_is_contained(github, feature_sha, integration_sha)
        contains_base = _commit_is_contained(github, integration_sha, feature_sha)
        if already_integrated:
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
        if not contains_base:
            raise WorkerFailure(
                f"existing feature branch {branch} does not contain current "
                f"integration base {integration_sha} and is not already integrated"
            )
        return IssueTopologyState(
            issue, branch, "recoverable", feature_sha, integration_sha
        )
    except WorkerFailure as exc:
        if str(exc).startswith(f"Issue #{issue}:"):
            raise
        raise WorkerFailure(f"Issue #{issue}: {exc}") from exc


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
    issues: tuple[tuple[int, str], ...],
    prospective_base_branch: str | None = None,
    remote: str = "origin",
    command_timeout_seconds: float = 30.0,
    _cwd: Path | None = None,
) -> tuple[IssueTopologyState, ...]:
    """Inspect all Issue branches and PRs before any workflow mutation."""
    if not issues or any(
        isinstance(number, bool)
        or not isinstance(number, int)
        or number < 1
        or not isinstance(branch, str)
        or not branch
        for number, branch in issues
    ):
        raise ValueError("issues must contain positive Issue numbers and branches")
    numbers = tuple(number for number, _branch in issues)
    branches = tuple(branch for _number, branch in issues)
    if len(set(numbers)) != len(numbers) or len(set(branches)) != len(branches):
        raise ValueError("Issue numbers and feature branches must be unique")
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
    pull_requests = github.list_prs()
    cached_github = _CachedIssueGitHub(github, pull_requests)
    remote_shas = repository.inspect_remote_branches((integration_branch, *branches))
    cached_repository = _CachedIssueGit(remote_shas)
    integration_sha = remote_shas[integration_branch]
    if integration_sha is None:
        if prospective_base_branch is None:
            raise WorkerFailure(
                f"remote integration branch {integration_branch!r} does not exist"
            )
        integration_sha = preparation.base_sha
    elif prospective_base_branch is not None and not _commit_is_contained(
        cached_github, preparation.base_sha, integration_sha
    ):
        raise WorkerFailure(
            f"existing integration branch {integration_branch!r} does not contain "
            f"prospective base {preparation.base_sha}"
        )

    states = tuple(
        classify_issue_topology(
            cached_repository,
            cached_github,
            issue=number,
            branch=branch,
            integration_branch=integration_branch,
            integration_sha=integration_sha,
        )
        for number, branch in issues
    )
    if (
        repository.inspect_remote_branches((integration_branch, *branches))
        != remote_shas
    ):
        raise WorkerFailure("remote branch topology changed during inspection")
    current_pull_requests = github.list_prs()
    if _pr_topology(current_pull_requests) != _pr_topology(pull_requests):
        raise WorkerFailure("GitHub PR topology changed during inspection")
    for state in states:
        if state.classification == "new":
            message = (
                f"Issue #{state.issue}: no existing feature branch; new run is safe"
            )
        elif state.classification == "recoverable":
            message = (
                f"Issue #{state.issue}: existing feature branch / PR topology "
                "is recoverable"
            )
        else:
            message = f"Issue #{state.issue}: already integrated; execution may skip this Issue"
        emit_finding("github", message, status="info")
    return states


def _pr_topology(
    pull_requests: tuple[PullRequestState, ...],
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
            )
            for pr in pull_requests
        )
    )


@dataclass(frozen=True)
class IssueDrivenConfig:
    repository: str
    integration_branch: str
    final_branch: str
    issues: tuple[int, ...]
    max_reviews: int
    merge_to_integration: bool
    final_review: bool
    merge_final: bool
    implementer_agent: str = "codex"
    reviewer_agent: str = "codex"
    policy_issue: int | None = None
    make_integration_branch: bool = False

    def as_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "mode": "issue-driven",
            "repository": self.repository,
            "integration_branch": self.integration_branch,
            "final_branch": self.final_branch,
            "make_integration_branch": self.make_integration_branch,
            "issues": list(self.issues),
            "max_reviews": self.max_reviews,
            "merge_to_integration": self.merge_to_integration,
            "final_review": self.final_review,
            "merge_final": self.merge_final,
            "implementer_agent": self.implementer_agent,
            "reviewer_agent": self.reviewer_agent,
        }
        if self.policy_issue is not None:
            result["policy_issue"] = self.policy_issue
        return result


_REQUIRED_FIELDS = {
    "repository",
    "integration_branch",
    "final_branch",
    "issues",
    "max_reviews",
    "merge_to_integration",
    "final_review",
    "merge_final",
}
_OPTIONAL_FIELDS = {
    "mode",
    "make_integration_branch",
    "implementer_agent",
    "reviewer_agent",
    "policy_issue",
}
_ALLOWED_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS
_SUPPORTED_AGENTS = {"codex", "claude"}
# Kept in lockstep with preflight.MAX_OUTLINE_ITEMS by boundary tests.
_MAX_WORKFLOW_OUTLINE_ITEMS = 100


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


def parse_issue_driven_json(source: str) -> IssueDrivenConfig:
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
    issues = value.get("issues")
    if not isinstance(issues, list) or not issues:
        findings.append(IssueDrivenFinding("$.issues", "must be a non-empty array"))
    else:
        reserved_outline_items = 2 if value.get("final_review") is True else 1
        max_issues = _MAX_WORKFLOW_OUTLINE_ITEMS - reserved_outline_items
        if len(issues) > max_issues:
            findings.append(
                IssueDrivenFinding(
                    "$.issues",
                    f"must contain at most {max_issues} items when "
                    f"final_review is {value.get('final_review')!r}",
                )
            )
        seen: set[int] = set()
        for index, issue in enumerate(issues):
            if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
                findings.append(
                    IssueDrivenFinding(
                        f"$.issues[{index}]", "must be a positive integer"
                    )
                )
            elif issue in seen:
                findings.append(
                    IssueDrivenFinding(f"$.issues[{index}]", "must be unique")
                )
            else:
                seen.add(issue)
        generated_branches = {f"feature/issue-{issue}" for issue in seen}
        for key, branch in (
            ("integration_branch", integration),
            ("final_branch", final),
        ):
            if isinstance(branch, str) and branch in generated_branches:
                findings.append(
                    IssueDrivenFinding(
                        f"$.{key}", "must differ from every generated Issue branch"
                    )
                )
    policy_issue = value.get("policy_issue")
    if "policy_issue" in value:
        if (
            isinstance(policy_issue, bool)
            or not isinstance(policy_issue, int)
            or policy_issue < 1
        ):
            findings.append(
                IssueDrivenFinding("$.policy_issue", "must be a positive integer")
            )
        elif isinstance(issues, list) and policy_issue in issues:
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
    if findings:
        raise IssueDrivenValidationError(findings)
    return IssueDrivenConfig(
        repository=value["repository"],
        integration_branch=value["integration_branch"],
        final_branch=value["final_branch"],
        make_integration_branch=value.get("make_integration_branch", False),
        issues=tuple(value["issues"]),
        max_reviews=value["max_reviews"],
        merge_to_integration=value["merge_to_integration"],
        final_review=value["final_review"],
        merge_final=value["merge_final"],
        implementer_agent=value.get("implementer_agent", "codex"),
        reviewer_agent=value.get("reviewer_agent", "codex"),
        policy_issue=value.get("policy_issue"),
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


def _fixed_config_function(config: IssueDrivenConfig) -> str:
    issues = ",\n        ".join(
        f"Issue({number}, 'feature/issue-{number}')" for number in config.issues
    )
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
        f"({number}, 'feature/issue-{number}')" for number in config.issues
    )
    prospective = config.final_branch if config.make_integration_branch else None
    return f"""def parse_args() -> Config:
    inspect_issue_driven_topology(
        repo={config.repository!r},
        integration_branch={config.integration_branch!r},
        issues=(
        {topology_issues},
        ),
        prospective_base_branch={prospective!r},
    )
    context = prepare_run_repository(
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
        (
        {issues},
        ),
        "git diff --check",
        WORKFLOW_POLICY_ISSUE,
    )


"""


def _workflow_outline(config: IssueDrivenConfig) -> str:
    labels = [f"Issue #{number}" for number in config.issues]
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
    outline_start = source.index("WORKFLOW_OUTLINE = [\n")
    outline_end = source.index("\n]", outline_start) + len("\n]")
    source = source[:outline_start] + _workflow_outline(config) + source[outline_end:]
    source = source.replace("MAX_REVIEWS = 5", f"MAX_REVIEWS = {config.max_reviews}", 1)
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
    return source[:start] + _fixed_config_function(config) + source[end:]
