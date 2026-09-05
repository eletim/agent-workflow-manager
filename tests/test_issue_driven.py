from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from purplemux_client import BranchState, GitHubRepository, GitRepository
from purplemux_client.issue_driven import (
    IssueDrivenValidationError,
    generate_issue_driven_workflow,
    parse_issue_driven_json,
)
from purplemux_client.preflight import WorkflowValidator


def payload(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "mode": "issue-driven",
        "repository": str(Path(__file__).parents[1]),
        "integration_branch": "dev/v0.2.0",
        "final_branch": "main",
        "issues": [90, 89, 91],
        "max_reviews": 5,
        "merge_to_integration": True,
        "final_review": True,
        "merge_final": False,
    }
    value.update(overrides)
    return value


def parse(value: dict[str, object]):
    return parse_issue_driven_json(json.dumps(value))


def test_valid_json_preserves_issue_order() -> None:
    config = parse(payload(issues=[90, 89, 91]))

    assert config.issues == (90, 89, 91)
    assert config.merge_final is False


@pytest.mark.parametrize(
    ("source", "path"),
    [
        ("not json", "$"),
        ("[]", "$"),
        (json.dumps({}), "$.repository"),
        (json.dumps(payload(issues=[])), "$.issues"),
        (json.dumps(payload(issues=[90, 90])), "$.issues[1]"),
        (json.dumps(payload(max_reviews=0)), "$.max_reviews"),
        (json.dumps(payload(integration_branch="bad..branch")), "$.integration_branch"),
        (
            json.dumps(payload(integration_branch="foo.lock/bar")),
            "$.integration_branch",
        ),
        (
            json.dumps(payload(integration_branch="bad\u007fbranch")),
            "$.integration_branch",
        ),
        (json.dumps(payload(merge_final="false")), "$.merge_final"),
        (json.dumps(payload(extra=True)), "$.extra"),
        ('{"repository":"a","repository":"b"}', "$.repository"),
    ],
)
def test_invalid_json_and_schema_fields_are_reported(source: str, path: str) -> None:
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse_issue_driven_json(source)

    assert path in {finding.path for finding in caught.value.findings}


def test_generation_is_deterministic_parseable_and_uses_ordered_issues() -> None:
    config = parse(payload(issues=[91, 90, 89]))

    first = generate_issue_driven_workflow(config)
    second = generate_issue_driven_workflow(parse(config.as_json()))

    assert first == second
    ast.parse(first)
    positions = [
        first.index(f"Issue({number}, 'feature/issue-{number}')")
        for number in config.issues
    ]
    assert positions == sorted(positions)
    assert "MAX_REVIEWS = 5" in first


def test_generated_workflow_passes_supported_static_validation() -> None:
    code = generate_issue_driven_workflow(parse(payload()))

    result = WorkflowValidator(check_timeout=10).validate(code)

    assert result.valid, result.issues
    assert result.dry_run_issues == ()


def test_generated_workflow_uses_coding_agent_delivery_contract() -> None:
    code = generate_issue_driven_workflow(parse(payload()))

    assert "require_committed_result(" in code
    assert "repo.ensure_pushed(" in code
    assert "github.create_draft_pr(" in code
    assert "reviewer requested changes, but implementer re-evaluated" in code
    assert "leave the working tree clean" in code


def test_generated_post_merge_path_starts_next_issue() -> None:
    code = generate_issue_driven_workflow(parse(payload(issues=[107, 108])))
    module_name = "generated_issue_workflow"
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(code, "<generated-issue-workflow>", "exec"), module.__dict__)
    finally:
        del sys.modules[module_name]
    workflow = module.__dict__
    issue_type = workflow["Issue"]
    config_type = workflow["Config"]
    merge_pr_and_advance = workflow["merge_pr_and_advance"]
    prepare_issue = workflow["prepare_issue"]
    assert callable(issue_type)
    assert callable(config_type)
    assert callable(merge_pr_and_advance)
    assert callable(prepare_issue)

    base_sha = "1" * 40
    reviewed_head = "2" * 40
    merge_sha = "3" * 40
    integration_branch = "dev/v0.2.1"
    first_branch = "feature/issue-107"
    next_branch = "feature/issue-108"
    events: list[str] = []

    class Repository:
        def __init__(self) -> None:
            self.current = first_branch
            self.integration_sha = base_sha

        def synchronize_branch(self, branch: str) -> BranchState:
            assert branch == integration_branch
            events.append(f"synchronize:{self.current}->{branch}")
            self.current = branch
            return BranchState(branch, self.integration_sha, self.integration_sha, True)

        def advance_after_merge(
            self,
            branch: str,
            *,
            previous_sha: str,
            merge_commit_sha: str,
            required_commit_sha: str,
        ) -> BranchState:
            events.append("advance")
            assert self.current == branch == integration_branch
            assert self.integration_sha == previous_sha == base_sha
            assert merge_commit_sha == merge_sha
            assert required_commit_sha == reviewed_head
            self.integration_sha = merge_sha
            return BranchState(branch, merge_sha, merge_sha, True)

        def require_clean(self) -> None:
            events.append("require_clean")

        def recover_feature_branch(
            self, branch: str, *, base: str, expected_base_sha: str
        ) -> SimpleNamespace:
            events.append(f"recover:{branch}")
            assert self.current == base == integration_branch
            assert expected_base_sha == self.integration_sha == merge_sha
            self.current = branch
            return SimpleNamespace(
                branch=BranchState(branch, merge_sha, None, True),
                reused_existing_work=False,
            )

    repository = Repository()

    class GitHub:
        def merge_pr(self, number: int, **kwargs: object) -> SimpleNamespace:
            events.append("merge")
            assert repository.current == integration_branch
            assert number == 107
            assert kwargs == {
                "expected_head": first_branch,
                "expected_head_sha": reviewed_head,
                "expected_base": integration_branch,
                "expected_base_sha": base_sha,
            }
            return SimpleNamespace(
                merge_commit_sha=merge_sha,
                pr=SimpleNamespace(url="https://example.test/pull/107"),
            )

        def find_pr(self, *, head: str, base: str, state: str) -> None:
            assert (head, base) == (next_branch, integration_branch)
            assert state in {"OPEN", "MERGED"}
            return None

    github = GitHub()
    merge_pr_and_advance(
        repository,
        github,
        number=107,
        head=first_branch,
        head_sha=reviewed_head,
        base=integration_branch,
        base_sha=base_sha,
    )
    config = config_type(
        Path("/repo"),
        "eletim/agent-workflow-manager",
        integration_branch,
        "main",
        (),
        "true",
    )
    prepared = prepare_issue(repository, github, issue_type(108, next_branch), config)

    assert prepared is not None
    assert repository.current == next_branch
    assert events[:3] == [
        f"synchronize:{first_branch}->{integration_branch}",
        "merge",
        "advance",
    ]
    assert f"synchronize:{integration_branch}->{integration_branch}" in events
    assert f"recover:{next_branch}" in events
    for unsafe in ("git reset", "git rebase", "git stash", "--force", "-f HEAD:"):
        assert unsafe not in code


def test_generated_repository_calls_match_public_helper_signatures() -> None:
    tree = ast.parse(generate_issue_driven_workflow(parse(payload())))
    contracts = {"repo": GitRepository, "github": GitHubRepository}

    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not isinstance(call.func, ast.Attribute):
            continue
        receiver = call.func.value
        if not isinstance(receiver, ast.Name) or receiver.id not in contracts:
            continue
        method = getattr(contracts[receiver.id], call.func.attr)
        assert all(keyword.arg is not None for keyword in call.keywords)
        inspect.signature(method).bind(
            None,
            *(None for _ in call.args),
            **{keyword.arg: None for keyword in call.keywords if keyword.arg},
        )


def test_generated_workflow_uses_run_scoped_correlation_without_ad_hoc_tokens() -> None:
    code = generate_issue_driven_workflow(parse(payload()))

    assert "run_correlation(" in code
    assert "uuid" not in code
    assert "RUN_TOKEN" not in code
    assert "[awm:" not in code
    assert "CreateSessionRequest(" in code
    assert 'name=f"Issue {issue.number} implementer"' in code


def test_generated_workflow_has_no_in_place_recovery_state() -> None:
    code = generate_issue_driven_workflow(parse(payload()))

    assert "save_checkpoint" not in code
    assert "resume_checkpoint" not in code
    assert "resume_shell" not in code
    assert "_pending" not in code
    assert "inspect_feature_preparation(" in code


def test_merge_to_integration_policy_changes_only_issue_merge_path() -> None:
    merging = generate_issue_driven_workflow(parse(payload(merge_to_integration=True)))
    ready_only = generate_issue_driven_workflow(
        parse(payload(merge_to_integration=False))
    )

    assert "MERGE_TO_INTEGRATION = True" in merging
    assert "MERGE_TO_INTEGRATION = False" in ready_only
    assert "Approved Issue #{issue.number} PR is Ready" in ready_only


def test_final_review_policy_selects_the_generated_control_flow() -> None:
    reviewed = generate_issue_driven_workflow(parse(payload(final_review=True)))
    skipped = generate_issue_driven_workflow(parse(payload(final_review=False)))

    assert "FINAL_REVIEW = True" in reviewed
    assert "FINAL_REVIEW = False" in skipped
    assert "if FINAL_REVIEW:" in reviewed


def test_merge_final_false_has_no_final_merge_path() -> None:
    ready_only = generate_issue_driven_workflow(parse(payload(merge_final=False)))
    merging = generate_issue_driven_workflow(parse(payload(merge_final=True)))

    assert "MERGE_FINAL = False" in ready_only
    assert "MERGE_FINAL = True" in merging
    assert "if not MERGE_FINAL:" in ready_only
