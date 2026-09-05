from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any


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

    def as_json(self) -> dict[str, object]:
        return {
            "mode": "issue-driven",
            "repository": self.repository,
            "integration_branch": self.integration_branch,
            "final_branch": self.final_branch,
            "issues": list(self.issues),
            "max_reviews": self.max_reviews,
            "merge_to_integration": self.merge_to_integration,
            "final_review": self.final_review,
            "merge_final": self.merge_final,
        }


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
_ALLOWED_FIELDS = _REQUIRED_FIELDS | {"mode"}


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
    findings: list[IssueDrivenFinding] = []
    if not isinstance(value, dict):
        raise IssueDrivenValidationError(
            [IssueDrivenFinding("$", "top-level value must be an object")]
        )
    for key in sorted(set(duplicate_keys)):
        findings.append(IssueDrivenFinding(f"$.{key}", "field is duplicated"))
    for key in sorted(_REQUIRED_FIELDS - set(value)):
        findings.append(IssueDrivenFinding(f"$.{key}", "required field is missing"))
    for key in sorted(set(value) - _ALLOWED_FIELDS):
        findings.append(IssueDrivenFinding(f"$.{key}", "unknown field is not allowed"))
    if "mode" in value and value["mode"] != "issue-driven":
        findings.append(
            IssueDrivenFinding("$.mode", "must be exactly 'issue-driven'")
        )
    for key in ("repository", "integration_branch", "final_branch"):
        item = value.get(key)
        if not isinstance(item, str) or not item or item != item.strip() or "\0" in item:
            findings.append(
                IssueDrivenFinding(f"$.{key}", "must be a non-empty trimmed string")
            )
    integration = value.get("integration_branch")
    final = value.get("final_branch")
    for key, branch in (
        ("integration_branch", integration),
        ("final_branch", final),
    ):
        if isinstance(branch, str) and branch and not _valid_branch_name(branch):
            findings.append(
                IssueDrivenFinding(f"$.{key}", "must be a valid Git branch name")
            )
    if isinstance(integration, str) and integration == final:
        findings.append(
            IssueDrivenFinding(
                "$.final_branch", "must differ from integration_branch"
            )
        )
    issues = value.get("issues")
    if not isinstance(issues, list) or not issues:
        findings.append(IssueDrivenFinding("$.issues", "must be a non-empty array"))
    else:
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
    max_reviews = value.get("max_reviews")
    if (
        isinstance(max_reviews, bool)
        or not isinstance(max_reviews, int)
        or not 1 <= max_reviews <= 100
    ):
        findings.append(
            IssueDrivenFinding("$.max_reviews", "must be an integer from 1 to 100")
        )
    for key in ("merge_to_integration", "final_review", "merge_final"):
        if not isinstance(value.get(key), bool):
            findings.append(IssueDrivenFinding(f"$.{key}", "must be a boolean"))
    if findings:
        raise IssueDrivenValidationError(findings)
    return IssueDrivenConfig(
        repository=value["repository"],
        integration_branch=value["integration_branch"],
        final_branch=value["final_branch"],
        issues=tuple(value["issues"]),
        max_reviews=value["max_reviews"],
        merge_to_integration=value["merge_to_integration"],
        final_review=value["final_review"],
        merge_final=value["merge_final"],
    )


def _canonical_source() -> str:
    packaged = resources.files("purplemux_client").joinpath(
        "_issue_driven_sequential_template.py"
    )
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    development_copy = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "sequential-version-development.py"
    )
    return development_copy.read_text(encoding="utf-8")


def _replace_region(source: str, start: str, end: str, replacement: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[:start_index] + replacement + source[end_index:]


def _fixed_config_function(config: IssueDrivenConfig) -> str:
    issues = ",\n        ".join(
        f"Issue({number}, 'feature/issue-{number}')" for number in config.issues
    )
    return f'''def parse_args() -> Config:
    context = prepare_run_repository(
        repo={config.repository!r},
        base_branch={config.integration_branch!r},
    )
    repository = GitRepository.open(
        context.execution_root,
        command_timeout_seconds=COMMAND_TIMEOUT,
    )
    return Config(
        context.execution_root,
        repository.expected_github_slug,
        {config.integration_branch!r},
        {config.final_branch!r},
        (
        {issues},
        ),
        "git diff --check",
    )


'''


_NO_ISSUE_MERGE = '''    print(f"Approved Issue #{issue.number} PR is Ready: {pr.url}", flush=True)
    workspace = recovery.workspace
    recovery.__dict__.update(Recovery(workspace=workspace).__dict__)
    recovery.phase = "workspace_ready"
    recovery.checkpoint(config)
'''


_FINAL_WITHOUT_REVIEW = '''def integration_delivery_without_review(
    config: Config,
    runtime: PurpleMuxRuntime,
    repo: GitRepository,
    github: GitHubRepository,
    recovery: Recovery,
) -> PullRequestState:
    integration = repo.synchronize_branch(config.integration_branch)
    final = repo.inspect_branch(config.main_branch)
    if integration.remote_sha is None or final.remote_sha is None:
        raise WorkerFailure("integration or final remote branch is missing")
    pr = github.find_pr(
        head=config.integration_branch, base=config.main_branch, state="OPEN"
    )
    if pr is None:
        recovery.correlation_id = recovery.correlation_id or run_correlation(
            "integration-pr"
        )
        recovery.phase = "integration_pr_create_pending"
        recovery.checkpoint(config)
        pr = github.create_draft_pr(
            head=config.integration_branch,
            base=config.main_branch,
            expected_head_sha=integration.remote_sha,
            expected_base_sha=final.remote_sha,
            title=f"Integrate {config.integration_branch}",
            body="Issue Driven integration; whole-version review disabled by policy.",
            correlation_id=recovery.correlation_id,
        )
    accepted_ready = (
        recovery.phase in {"integration_ready_pending", "integration_ready"}
        and recovery.approved_sha == integration.remote_sha
        and recovery.approved_base_sha == final.remote_sha
        and recovery.review_outcome == "review-disabled-policy"
    )
    pr = github.require_pr(
        number=pr.number,
        head=config.integration_branch,
        base=config.main_branch,
        state="OPEN",
        expected_head_sha=integration.remote_sha,
        expected_base_sha=final.remote_sha,
        draft=False if accepted_ready else True,
    )
    client = ensure_workspace(runtime, config, recovery)
    run_final_checks(client, config, recovery)
    recovery.approved_sha, recovery.approved_base_sha = pr.head_sha, pr.base_sha
    recovery.review_outcome = "review-disabled-policy"
    recovery.phase = "integration_ready_pending"
    recovery.checkpoint(config)
    if pr.is_draft:
        pr = github.set_draft(
            pr.number,
            draft=False,
            expected_head=config.integration_branch,
            expected_head_sha=pr.head_sha,
            expected_base=config.main_branch,
            expected_base_sha=pr.base_sha,
        )
    recovery.phase = "integration_ready"
    recovery.checkpoint(config)
    return pr


'''


def _main_function(config: IssueDrivenConfig) -> str:
    final_call = (
        "integration_review(config, runtime, repo, github, recovery)"
        if config.final_review
        else "integration_delivery_without_review(config, runtime, repo, github, recovery)"
    )
    merge = ""
    conclusion = 'print(f"Final integration PR is Ready (not merged): {ready.url}", flush=True)'
    if config.merge_final:
        merge = '''
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
    )'''
        conclusion = 'print(f"Merged final integration PR: {merged.pr.url}", flush=True)'
    return f'''def main() -> None:
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
    ready = {final_call}{merge}
    {conclusion}


if __name__ == "__main__":
    main()
'''


def generate_issue_driven_workflow(config: IssueDrivenConfig) -> str:
    """Generate the complete plain-Python sequential workflow deterministically."""

    source = _canonical_source()
    source = source.replace(
        "    PurpleMuxRuntime,\n",
        "    PurpleMuxRuntime,\n    prepare_run_repository,\n",
        1,
    )
    source = source.replace("MAX_REVIEWS = 4", f"MAX_REVIEWS = {config.max_reviews}", 1)
    source = _replace_region(source, "def parse_args() -> Config:\n", "def load_recovery(", _fixed_config_function(config))
    source = source.replace(
        'or recovery.review_outcome not in {None, "approved", "no-change-policy"}',
        'or recovery.review_outcome not in {None, "approved", "no-change-policy", "review-disabled-policy"}',
        1,
    )
    if not config.merge_to_integration:
        source = _replace_region(
            source,
            "    merged = github.merge_pr(\n",
            "\n\ndef integration_review(\n",
            _NO_ISSUE_MERGE,
        )
    if not config.final_review:
        source = _replace_region(
            source,
            "def integration_review(\n",
            "def main() -> None:\n",
            _FINAL_WITHOUT_REVIEW,
        )
    source = source[: source.index("def main() -> None:\n")] + _main_function(config)
    return source
