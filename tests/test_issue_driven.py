from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

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
