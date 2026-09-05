from __future__ import annotations

import ast
import runpy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

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


def open_pr(*, head: str, base: str, draft: bool) -> PullRequestState:
    return PullRequestState(
        18,
        "https://example.test/pull/18",
        "OPEN",
        draft,
        "acme/project",
        head,
        "review-head",
        "acme/project",
        base,
        "review-base",
        None,
        False,
        None,
        "node-18",
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


def test_clean_worktree_does_not_invoke_cleanup_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    require_clean_worktree = workflow["require_clean_worktree"]

    class Repository:
        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False, current_branch="feature/issue-116")

    monkeypatch.setitem(
        require_clean_worktree.__globals__,
        "run_turn",
        lambda *args, **kwargs: pytest.fail("clean worktree invoked cleanup turn"),
    )

    require_clean_worktree(
        Repository(), object(), "cleanup-tab", context="testing the clean path"
    )


def test_dirty_worktree_gets_focused_cleanup_and_is_rechecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    require_clean_worktree = workflow["require_clean_worktree"]
    states = iter(
        (
            SimpleNamespace(
                dirty=True,
                current_branch="feature/issue-116",
                status=(" M src/feature.py", "?? build/output.js"),
            ),
            SimpleNamespace(
                dirty=False, current_branch="feature/issue-116", status=()
            ),
        )
    )
    prompts: list[str] = []

    class Repository:
        def inspect_worktree(self) -> SimpleNamespace:
            return next(states)

    monkeypatch.setitem(
        require_clean_worktree.__globals__,
        "run_turn",
        lambda *args, **kwargs: prompts.append(str(args[3])) or "cleaned",
    )
    monkeypatch.setitem(
        require_clean_worktree.__globals__, "emit_finding", lambda *args, **kwargs: None
    )

    require_clean_worktree(
        Repository(), object(), "cleanup-tab", context="verifying Issue #116"
    )

    assert len(prompts) == 1
    assert "Preserve and commit all intended source, test" in prompts[0]
    assert ".gitignore" in prompts[0]
    assert "clearly disposable generated" in prompts[0]
    assert "discard uncertain work" in prompts[0]


def test_ambiguous_dirty_worktree_fails_with_remaining_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    require_clean_worktree = workflow["require_clean_worktree"]
    dirty = SimpleNamespace(
        dirty=True,
        current_branch="feature/issue-116",
        status=(" M src/feature.py", "?? uncertain.txt"),
    )

    class Repository:
        def inspect_worktree(self) -> SimpleNamespace:
            return dirty

    monkeypatch.setitem(
        require_clean_worktree.__globals__,
        "run_turn",
        lambda *args, **kwargs: "uncertain work preserved",
    )

    with pytest.raises(WorkerFailure, match=r"src/feature.py.*uncertain.txt"):
        require_clean_worktree(
            Repository(), object(), "cleanup-tab", context="verifying Issue #116"
        )


def test_example_revalidates_ready_prs_and_preserves_terminal_delivery() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    assert "already_approved" not in source
    assert "draft=False" in source
    assert "return_to_draft_for_review(" in source
    assert "Ready without review provenance" in source
    assert "final delivery already merged" in source
    assert 'ready.state == "MERGED"' in source
    assert "base branch {base!r} changed before approved merge" in source
    assert source.count("merge_pr_and_advance(") == 3


def test_ready_issue_pr_is_redrafted_and_independently_reviewed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["process_issue"].__globals__
    issue = workflow["Issue"](90, "feature/issue-90")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    ready = open_pr(head=issue.branch, base=config.integration_branch, draft=False)
    draft = replace(ready, is_draft=True)
    events: list[str] = []

    class Repository:
        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False)

        def inspect_branch(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, ready.base_sha, ready.base_sha, True)

    class GitHub:
        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            assert number == ready.number
            events.append(f"set_draft:{kwargs['draft']}")
            return draft if kwargs["draft"] else ready

        def require_pr(self, **kwargs: object) -> PullRequestState:
            events.append("require_review_head")
            assert kwargs["draft"] is True
            return draft

    def run_turn(*args: object, **kwargs: object) -> str:
        name = str(args[2])
        events.append(name)
        return "APPROVED" if name.endswith("review") else "implemented"

    monkeypatch.setitem(
        workflow_globals, "prepare_issue", lambda *args: (ready, ready.head_sha, True)
    )
    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: (ready.head_sha, False),
    )
    monkeypatch.setitem(
        workflow_globals, "ensure_issue_pr", lambda *args, **kwargs: draft
    )
    monkeypatch.setitem(
        workflow_globals,
        "merge_pr_and_advance",
        lambda *args, **kwargs: SimpleNamespace(pr=ready),
    )

    workflow["process_issue"](issue, config, object(), Repository(), GitHub())

    assert events[0] == "set_draft:True"
    assert "Issue #90 review" in events
    assert events[-1] == "set_draft:False"


def test_ready_final_pr_repeats_review_and_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["integration_delivery"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    ready = open_pr(
        head=config.integration_branch, base=config.main_branch, draft=False
    )
    current = ready
    events: list[str] = []

    class Repository:
        def synchronize_branch(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, ready.head_sha, ready.head_sha, True)

        def inspect_branch(self, branch: str) -> BranchState:
            assert branch == config.main_branch
            return BranchState(branch, ready.base_sha, ready.base_sha, True)

    class GitHub:
        def find_pr(self, *, head: str, base: str, state: str):  # type: ignore[no-untyped-def]
            assert (head, base) == (config.integration_branch, config.main_branch)
            return ready if state == "OPEN" else None

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            nonlocal current
            assert number == ready.number
            events.append(f"set_draft:{kwargs['draft']}")
            current = replace(ready, is_draft=bool(kwargs["draft"]))
            return current

        def require_pr(self, **kwargs: object) -> PullRequestState:
            assert kwargs["draft"] is True
            events.append("require_review_head")
            return current

    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(
        workflow_globals,
        "run_turn",
        lambda *args, **kwargs: events.append(str(args[2])) or "APPROVED",
    )
    monkeypatch.setitem(
        workflow_globals,
        "run_final_checks",
        lambda *args: events.append("final checks"),
    )

    result = workflow["integration_delivery"](config, object(), Repository(), GitHub())

    assert result.is_draft is False
    assert events == [
        "set_draft:True",
        "require_review_head",
        "Whole-version review",
        "require_review_head",
        "final checks",
        "set_draft:False",
    ]


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
