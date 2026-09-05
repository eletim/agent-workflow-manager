from __future__ import annotations

import ast
from pathlib import Path

from purplemux_client.preflight import WorkflowValidator

EXAMPLE = Path(__file__).parents[1] / "examples" / "sequential-version-development.py"


def test_example_is_plain_python_without_in_place_recovery_contract() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    ast.parse(source)
    assert "save_checkpoint" not in source
    assert "resume_checkpoint" not in source
    assert "ResumeCheckpoint" not in source
    assert "resume_shell" not in source
    assert "_pending" not in source


def test_example_preserves_authoritative_inspection_and_mutation_safety() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    assert "inspect_feature_preparation(" in source
    assert "repo.recover_feature_branch(" in source
    assert "github.require_pr(" in source
    assert "repo.require_committed_result(" in source
    assert "repo.ensure_pushed(" in source
    assert "run_correlation(" in source
    assert "MutationOutcomeUnknown" not in source  # helpers raise it internally
    assert "existing_pr is not None or reused_existing_work" in source


def test_example_passes_static_validation() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    result = WorkflowValidator(check_timeout=10).validate(source)

    assert result.valid, result.issues
    assert result.dry_run_issues == ()
