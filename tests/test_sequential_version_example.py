from __future__ import annotations

import ast
import runpy
from pathlib import Path

import pytest

from purplemux_client import BranchState, PullRequestState, WorkerFailure
from purplemux_client.preflight import WorkflowValidator

EXAMPLE = Path(__file__).parents[1] / "examples" / "sequential-version-development.py"


def merged_final_pr(head_sha: str) -> PullRequestState:
    return PullRequestState(
        17,
        "https://example.test/pull/17",
        "MERGED",
        False,
        "acme/project",
        "dev/v1",
        head_sha,
        "acme/project",
        "main",
        "old-base",
        "merge-commit",
        False,
        None,
        "node-17",
        "",
    )


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


def test_example_preserves_completed_approval_and_terminal_delivery() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    assert "already_approved" in source
    assert "draft=False" in source
    assert "Skipping already-Ready Issue" in source
    assert "final delivery already merged" in source
    assert "if not MERGE_FINAL:\n            return ready" in source
    assert 'ready.state == "MERGED"' in source
    assert "base branch {base!r} changed before approved merge" in source
    assert source.count("merge_pr_and_advance(") == 5


def test_historical_merged_final_pr_cannot_complete_newer_delivery() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    config_type = workflow["Config"]
    integration_delivery = workflow["integration_delivery"]
    config = config_type(
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        (),
        "true",
    )

    class Repository:
        def __init__(self) -> None:
            self.synchronized: list[str] = []

        def synchronize_branch(self, branch: str) -> BranchState:
            self.synchronized.append(branch)
            assert branch == "dev/v1"
            return BranchState(branch, "new-head", "new-head", True)

        def inspect_branch(self, branch: str) -> BranchState:
            assert branch == "main"
            return BranchState(branch, "final-head", "final-head", False)

    class GitHub:
        def find_pr(self, *, head: str, base: str, state: str):  # type: ignore[no-untyped-def]
            assert (head, base) == ("dev/v1", "main")
            if state == "OPEN":
                return None
            assert state == "MERGED"
            return merged_final_pr("old-head")

    repository = Repository()
    with pytest.raises(WorkerFailure, match="historical merged final PR #17"):
        integration_delivery(config, object(), repository, GitHub())

    assert repository.synchronized == ["dev/v1"]


def test_exact_merged_final_pr_requires_final_branch_containment() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    integration_delivery = workflow["integration_delivery"]

    class Repository:
        def synchronize_branch(self, branch: str) -> BranchState:
            sha = "new-head" if branch == "dev/v1" else "final-head"
            return BranchState(branch, sha, sha, True)

        def inspect_branch(self, branch: str) -> BranchState:
            return BranchState(branch, "final-head", "final-head", False)

        def require_contains(self, branch: str, commit_sha: str) -> None:
            assert (branch, commit_sha) == ("main", "new-head")
            raise WorkerFailure("main does not contain new-head")

    class GitHub:
        def find_pr(self, *, head: str, base: str, state: str):  # type: ignore[no-untyped-def]
            return None if state == "OPEN" else merged_final_pr("new-head")

        def require_pr(self, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs["state"] == "MERGED"
            assert kwargs["expected_head_sha"] == "new-head"
            return merged_final_pr("new-head")

    with pytest.raises(WorkerFailure, match="main does not contain new-head"):
        integration_delivery(config, object(), Repository(), GitHub())


def test_example_passes_static_validation() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    result = WorkflowValidator(check_timeout=10).validate(source)

    assert result.valid, result.issues
    assert result.dry_run_issues == ()
