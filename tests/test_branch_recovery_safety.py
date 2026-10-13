from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_issue_driven import load_generated_workflow, topology_pr

import purplemux_client.issue_driven as issue_driven
from purplemux_client import BranchOwnershipError, BranchState, WorkerFailure
from purplemux_client.github import PullRequestSnapshot
from purplemux_client.issue_driven import classify_issue_topology


@pytest.mark.parametrize("status", ["OPEN", "MERGED", "CLOSED"])
@pytest.mark.parametrize("relationship", ["ahead", "diverged", "behind"])
def test_inline_branch_rejects_foreign_pr_history_before_ancestry(
    status: str, relationship: str
) -> None:
    branch = "feature/work-item-admin-app-role-ui"
    foreign_pr = replace(
        topology_pr(state=status, head_branch=branch), base_branch="dev/old"
    )
    with pytest.raises(BranchOwnershipError, match="belongs to another integration"):
        classify_issue_topology(
            SimpleNamespace(
                inspect_branch=lambda name: BranchState(name, "f" * 40, "f" * 40, False)
            ),
            PullRequestSnapshot("acme/project", (foreign_pr,)),
            SimpleNamespace(
                compare_commits=lambda **kwargs: pytest.fail(
                    f"ancestry {relationship} must not authorize a foreign branch"
                )
            ),
            issue="Mini task admin-app-role-ui",
            branch=branch,
            integration_branch="dev/current",
            integration_sha="b" * 40,
            inline_task_fingerprint="a" * 64,
        )


def test_inline_branch_only_resume_remains_supported() -> None:
    branch = "feature/work-item-resume"
    state = classify_issue_topology(
        SimpleNamespace(
            inspect_branch=lambda name: BranchState(name, "f" * 40, "f" * 40, False)
        ),
        PullRequestSnapshot("acme/project", ()),
        SimpleNamespace(compare_commits=lambda **kwargs: "ahead"),
        issue="Mini task resume",
        branch=branch,
        integration_branch="dev/current",
        integration_sha="b" * 40,
        inline_task_fingerprint="a" * 64,
    )
    assert state.classification == "recoverable"
    assert state.open_pr_number is None


def test_public_inspector_checks_ownership_before_comparison_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch = "feature/work-item-collision"
    foreign = replace(
        topology_pr(state="MERGED", head_branch=branch), base_branch="dev/old"
    )
    snapshot = PullRequestSnapshot("acme/project", (foreign,))
    git = SimpleNamespace(
        expected_github_slug="acme/project",
        inspect_remote_branches=lambda branches: {name: "b" * 40 for name in branches},
    )
    inspection = SimpleNamespace(
        inspect_pr_snapshot=lambda branches: snapshot,
        inspect_comparisons=lambda pairs: pytest.fail(
            "known collision must stop before comparison API"
        ),
    )
    monkeypatch.setattr(
        issue_driven,
        "_inspect_repository_declaration",
        lambda **kwargs: SimpleNamespace(
            source_repository=Path("/repo"), base_sha="b" * 40
        ),
    )
    monkeypatch.setattr(issue_driven.GitRepository, "open", lambda *args, **kwargs: git)
    monkeypatch.setattr(
        issue_driven.GitHubRepository,
        "open",
        lambda *args, **kwargs: SimpleNamespace(topology_inspection=lambda: inspection),
    )
    with pytest.raises(BranchOwnershipError, match="belongs to another integration"):
        issue_driven.inspect_issue_driven_work_item_topology(
            repo="acme/project",
            integration_branch="dev/current",
            issue=("Mini task collision", branch, "a" * 64),
        )


def recovery_workflow(*, error: Exception | None = None):
    workflow = load_generated_workflow(issues=[90])
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    heads = {"dev/v1": "b" * 40, "main": "a" * 40, "feature/foreign": "c" * 40}
    current = {"branch": "dev/v1", "dirty": False}
    remote = dict(heads)
    calls = []

    def restore(branch, *, expected_sha):
        calls.append("restore")
        if current["dirty"]:
            raise WorkerFailure("worktree must be clean")
        assert heads[branch] == expected_sha
        current["branch"] = branch

    def committed(branch, **kwargs):
        calls.append("committed")
        assert current["branch"] == branch
        assert not current["dirty"]
        return BranchState(branch, heads[branch], remote[branch], True)

    repo = SimpleNamespace(
        inspect_worktree=lambda: SimpleNamespace(
            current_branch=current["branch"], dirty=current["dirty"], status=()
        ),
        inspect_branch=lambda branch: BranchState(
            branch, heads.get(branch), remote.get(branch), current["branch"] == branch
        ),
        inspect_local_branch_heads=lambda: dict(heads),
        inspect_remote_branch_heads=lambda: dict(remote),
        inspect_remote_branches=lambda branches: {
            branch: remote.get(branch) for branch in branches
        },
        restore_checkout=restore,
        require_committed_result=committed,
    )
    workflow["GitRepository"] = SimpleNamespace(open=lambda *args, **kwargs: repo)
    workflow["GitHubRepository"] = SimpleNamespace(
        open=lambda *args, **kwargs: SimpleNamespace(find_pr=lambda **kwargs: None)
    )
    workflow["create_runtime"] = lambda config: object()
    workflow["emit_issue_driven_context"] = lambda *args, **kwargs: None
    workflow["run_outline_step"] = lambda name, action: action()
    attempts = []

    def prepare(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise error or WorkerFailure("planning failed")
        return None, workflow["WorkItemPlan"](config)

    workflow["prepare_work_item_plan_pr"] = prepare
    workflow["process_work_items"] = lambda *args: ()
    workflow["integration_delivery"] = lambda *args: "delivered"
    workflow["report_repository_delivery"] = lambda *args: None
    return workflow, config, repo, heads, remote, current, calls, attempts


def test_foreign_branch_collision_never_starts_recovery() -> None:
    workflow, config, *_rest = recovery_workflow(
        error=BranchOwnershipError("foreign branch")
    )
    workflow["recover_error"] = lambda *args, **kwargs: pytest.fail(
        "must not offer a foreign branch to Recovery"
    )
    with pytest.raises(BranchOwnershipError):
        workflow["run_repository"](config)


@pytest.mark.parametrize("location", ["local", "remote"])
@pytest.mark.parametrize("mutation", ["advance", "create", "delete"])
def test_recovery_cannot_hide_foreign_mutation_by_restoring_checkout(
    location: str, mutation: str, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow, config, _repo, heads, remote, current, calls, attempts = (
        recovery_workflow()
    )

    def recover(*args, **kwargs):
        assert "'dev/v1' only" in kwargs["recovery_scope"]
        affected = heads if location == "local" else remote
        if mutation == "advance":
            affected["feature/foreign"] = "d" * 40
        elif mutation == "delete":
            del affected["feature/foreign"]
        else:
            affected["feature/unplanned"] = "d" * 40
        current["branch"] = "feature/foreign"
        return workflow["RecoveryReport"](True, True, "Repaired.", "Claimed safe.")

    workflow["recover_error"] = recover
    with pytest.raises(WorkerFailure, match="out-of-scope"):
        workflow["run_repository"](config)
    assert calls == []
    assert len(attempts) == 1
    assert "Retry safe: True" not in capsys.readouterr().out


@pytest.mark.parametrize("dirty", [False, True])
def test_recovery_restores_only_clean_unchanged_checkout(
    dirty: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow, config, _repo, _heads, _remote, current, calls, attempts = (
        recovery_workflow()
    )

    def recover(*args, **kwargs):
        current.update(branch="feature/foreign", dirty=dirty)
        return workflow["RecoveryReport"](
            True, True, "Checked state.", "Branches unchanged."
        )

    workflow["recover_error"] = recover
    if dirty:
        with pytest.raises(WorkerFailure, match="clean"):
            workflow["run_repository"](config)
        assert len(attempts) == 1
        assert "Retry safe: True" not in capsys.readouterr().out
    else:
        assert workflow["run_repository"](config) == "delivered"
        assert current["branch"] == "dev/v1"
        assert calls == ["restore", "committed"]
        assert "Retry safe: True" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["provenance", "retry_state", "unpublished"])
def test_retry_safe_is_not_reported_before_verification(
    failure: str, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow, config, repo, heads, _remote, _current, _calls, attempts = (
        recovery_workflow()
    )

    def reject(*args, **kwargs):
        raise WorkerFailure("verification failed")

    if failure == "provenance":
        repo.require_committed_result = reject
    elif failure == "retry_state":
        workflow["require_recovery_retry_state"] = reject

    def recover(*args, **kwargs):
        if failure == "unpublished":
            heads["dev/v1"] = "d" * 40
        return workflow["RecoveryReport"](True, True, "Repaired.", "Claimed safe.")

    workflow["recover_error"] = recover
    with pytest.raises(WorkerFailure, match="verification failed|not fully pushed"):
        workflow["run_repository"](config)
    assert len(attempts) == 1
    assert "Retry safe: True" not in capsys.readouterr().out


def test_retry_state_rejects_wrong_ending_branch() -> None:
    workflow = load_generated_workflow(issues=[90])
    state = {
        "integration_branch": "dev/v1",
        "final_branch": "main",
        "work_item_plan": {"active": None},
        "worktree": {"dirty": False, "current_branch": "dev/v1"},
        "remote_heads": {"dev/v1": "b" * 40, "main": "a" * 40},
        "base_pr": None,
    }
    before = json.dumps(state)
    state["worktree"]["current_branch"] = "feature/foreign"
    with pytest.raises(WorkerFailure, match="checkout branch changed"):
        workflow["require_recovery_retry_state"](json.dumps(state), before)


def test_retry_state_accepts_logical_active_branch_on_private_checkout() -> None:
    workflow = load_generated_workflow(issues=[90])
    physical = "awm-run/0123456789ab/feature/issue-90"
    state = {
        "integration_branch": "dev/v1",
        "final_branch": "main",
        "work_item_plan": {"active": {"branch": "feature/issue-90"}},
        "worktree": {"dirty": False, "current_branch": physical},
        "remote_heads": {"dev/v1": "b" * 40, "main": "a" * 40},
        "active_branch": {
            "name": "feature/issue-90",
            "local_sha": "c" * 40,
            "remote_sha": "c" * 40,
            "current": True,
            "checkout_branch": physical,
        },
        "active_prs": [],
        "base_pr": None,
    }
    source = json.dumps(state)
    workflow["require_recovery_retry_state"](source, source)
    state["active_branch"]["checkout_branch"] = "awm-run/other/feature/issue-90"
    with pytest.raises(WorkerFailure, match="active branch or PR changed"):
        workflow["require_recovery_retry_state"](json.dumps(state), source)


def test_recovery_rechecks_preexisting_remote_ancestry_before_retry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow, config, repo, heads, remote, _current, _calls, attempts = (
        recovery_workflow()
    )
    prior_remote = "e" * 40
    remote["dev/v1"] = prior_remote
    checked = []

    def require_contains(branch, sha):
        checked.append((branch, sha))
        raise WorkerFailure("repair would discard published history")

    repo.require_contains = require_contains

    def recover(*args, **kwargs):
        heads["dev/v1"] = remote["dev/v1"] = "d" * 40
        return workflow["RecoveryReport"](True, True, "Repaired.", "Claimed safe.")

    workflow["recover_error"] = recover
    with pytest.raises(WorkerFailure, match="discard published history"):
        workflow["run_repository"](config)
    assert checked == [("dev/v1", prior_remote)]
    assert len(attempts) == 1
    assert "Retry safe: True" not in capsys.readouterr().out
