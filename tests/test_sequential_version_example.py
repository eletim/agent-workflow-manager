from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

import purplemux_client
from purplemux_client.preflight import WorkflowValidator

SAMPLE = Path(__file__).parents[1] / "examples" / "sequential-version-development.py"


def load_sample() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sequential_version_sample", SAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SAMPLE_MODULE = load_sample()


def config(tmp_path: Path):
    return SAMPLE_MODULE.Config(
        tmp_path,
        "OWNER/REPOSITORY",
        "dev/v1.2.3",
        "main",
        (SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        "make test",
    )


def pr(*, head: str = "a" * 40, base: str = "b" * 40):
    return purplemux_client.PullRequestState(
        number=10,
        url="https://github.com/OWNER/REPOSITORY/pull/10",
        state="OPEN",
        is_draft=True,
        head_repository="OWNER/REPOSITORY",
        head_branch="feature/issue-1",
        head_sha=head,
        base_repository="OWNER/REPOSITORY",
        base_branch="dev/v1.2.3",
        base_sha=base,
        merge_commit_sha=None,
        auto_merge_enabled=False,
        merge_queue_entry=None,
        node_id="PR_10",
        body="",
    )


def test_canonical_sample_is_plain_python_and_dry_run_eligible() -> None:
    source = SAMPLE.read_text(encoding="utf-8")
    compile(source, str(SAMPLE), "exec")
    result = WorkflowValidator().validate(source)

    assert result.valid
    assert not result.dry_run_issues
    assert SAMPLE_MODULE.WORKFLOW_DRY_RUN == 1


def test_canonical_sample_has_no_raw_topology_or_workspace_subprocess_layer() -> None:
    source = SAMPLE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}

    assert "subprocess" not in imports
    assert (
        not {
            "run_command",
            "read_text",
            "mutate",
            "gh_json",
            "branch_exists",
            "switch_to_integration",
            "require_ancestor",
        }
        & functions
    )
    assert "PurpleMuxRuntime" in source
    assert "GitRepository.open" in source
    assert "GitHubRepository.open" in source


def test_topology_gate_rejects_before_git_or_runtime_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class GitHub:
        def find_pr(self, **_kwargs: object):
            events.append("inspect-open")
            raise purplemux_client.PullRequestTopologyError("wrong base")

    class Repo:
        def inspect_worktree(self):
            events.append("git")
            pytest.fail("Git inspection followed rejected PR topology")

    with pytest.raises(purplemux_client.WorkerFailure, match="wrong base"):
        SAMPLE_MODULE.prepare_issue(
            Repo(),
            GitHub(),
            config(tmp_path).issues[0],
            config(tmp_path),
            SAMPLE_MODULE.Recovery(),
        )
    assert events == ["inspect-open"]


def test_each_issue_session_creation_has_an_immediate_open_pr_gate() -> None:
    source = SAMPLE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    process = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "process_issue"
    )
    calls = [
        node.func.id
        for node in ast.walk(process)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    # One gate precedes workspace creation and one immediately precedes each
    # of the two session creations; prepare_issue owns the pre-Git gate.
    assert calls.count("inspect_issue_pr_topology") == 3
    assert calls.count("create_agent") == 2


def test_review_approval_tracks_and_invalidates_both_shas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = SAMPLE_MODULE.Recovery(
        phase="issue_approved",
        approved_sha="a" * 40,
        approved_base_sha="b" * 40,
    )
    monkeypatch.setattr(SAMPLE_MODULE, "save_checkpoint", lambda *_args: None)

    assert not SAMPLE_MODULE.reopen_if_topology_drifted(
        pr(), state, "issue_fix_done", config(tmp_path)
    )
    assert SAMPLE_MODULE.reopen_if_topology_drifted(
        pr(base="c" * 40), state, "issue_fix_done", config(tmp_path)
    )
    assert state.approved_sha is None
    assert state.approved_base_sha is None
    assert state.phase == "issue_fix_done"


def test_final_path_only_makes_exact_integration_topology_ready() -> None:
    source = SAMPLE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    integration = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "integration_review"
    )
    attributes = [
        node.func.attr
        for node in ast.walk(integration)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "set_draft" in attributes
    assert "merge_pr" not in attributes
    assert "expected_head_sha" in source
    assert "expected_base_sha" in source
