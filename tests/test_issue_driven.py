from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from purplemux_client import MutationOutcomeUnknown
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
        "max_reviews": 8,
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
    assert "MAX_REVIEWS = 8" in first


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


def test_generated_workflow_uses_run_scoped_correlation_without_ad_hoc_tokens() -> None:
    code = generate_issue_driven_workflow(parse(payload()))

    assert "run_correlation(" in code
    assert "uuid" not in code
    assert "RUN_TOKEN" not in code
    assert "[awm:" not in code
    assert "CreateSessionRequest(" in code
    assert 'name=f"Issue {issue.number} implementer"' in code


@pytest.mark.parametrize("visible_pr", [False, True])
def test_review_disabled_resume_never_retries_pending_pr_creation(
    visible_pr: bool,
) -> None:
    code = generate_issue_driven_workflow(parse(payload(final_review=False)))
    module = ModuleType("generated_issue_driven_test")
    sys.modules[module.__name__] = module
    try:
        exec(compile(code, "<generated>", "exec"), module.__dict__)
    finally:
        sys.modules.pop(module.__name__, None)

    class UnexpectedGitHub:
        called = False

        def find_pr(self, **_kwargs: object) -> object | None:
            self.called = True
            return object() if visible_pr else None

    recovery = SimpleNamespace(phase="integration_pr_create_pending")
    github = UnexpectedGitHub()
    with pytest.raises(MutationOutcomeUnknown):
        module.integration_delivery_without_review(
            object(), object(), object(), github, recovery
        )
    assert github.called is False


def test_merge_to_integration_policy_changes_only_issue_merge_path() -> None:
    merging = generate_issue_driven_workflow(parse(payload(merge_to_integration=True)))
    ready_only = generate_issue_driven_workflow(
        parse(payload(merge_to_integration=False))
    )

    assert merging.count("github.merge_pr(") == 1
    assert ready_only.count("github.merge_pr(") == 0
    assert "Approved Issue #{issue.number} PR is Ready" in ready_only


def test_final_review_policy_selects_the_generated_control_flow() -> None:
    reviewed = generate_issue_driven_workflow(parse(payload(final_review=True)))
    skipped = generate_issue_driven_workflow(parse(payload(final_review=False)))

    assert (
        "ready = integration_review(config, runtime, repo, github, recovery)"
        in reviewed
    )
    assert "def integration_review(" in reviewed
    assert "def integration_review(" not in skipped
    assert "review-disabled-policy" in skipped
    assert "ready = integration_delivery_without_review(" in skipped


def test_merge_final_false_has_no_final_merge_path() -> None:
    ready_only = generate_issue_driven_workflow(parse(payload(merge_final=False)))
    merging = generate_issue_driven_workflow(parse(payload(merge_final=True)))

    ready_main = ready_only[ready_only.index("def main() -> None:") :]
    merging_main = merging[merging.index("def main() -> None:") :]
    assert "github.merge_pr(" not in ready_main
    assert "not merged" in ready_main
    assert "github.merge_pr(" in merging_main
    assert "Merged final integration PR" in merging_main
