from __future__ import annotations

import ast
import importlib.util
import sys
from dataclasses import replace
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


def test_direct_sample_is_plain_python_and_dry_run_eligible() -> None:
    source = SAMPLE.read_text(encoding="utf-8")
    compile(source, str(SAMPLE), "exec")
    result = WorkflowValidator().validate(source)

    assert result.valid
    assert not result.dry_run_issues
    assert SAMPLE_MODULE.WORKFLOW_DRY_RUN == 1


def test_direct_sample_has_no_raw_topology_or_workspace_subprocess_layer() -> None:
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


def test_clean_issue_resume_validates_checkpointed_preparation_without_mutation(
    tmp_path: Path,
) -> None:
    base_sha = "b" * 40
    branch = config(tmp_path).issues[0].branch
    events: list[str] = []

    class GitHub:
        def find_pr(self, *, state: str, **_kwargs: object):
            events.append(f"pr:{state}")
            return None

    class Repo:
        def inspect_worktree(self):
            events.append("worktree")
            return purplemux_client.WorktreeState(tmp_path, branch, False, ())

        def require_current_branch(self, requested: str):
            events.append(f"current:{requested}")

        def inspect_feature_preparation(
            self, requested: str, *, base: str, expected_base_sha: str
        ):
            events.append(f"prepared:{requested}:{base}:{expected_base_sha}")
            feature = purplemux_client.BranchState(requested, "c" * 40, "c" * 40, True)
            base_state = purplemux_client.BranchState(base, base_sha, base_sha, False)
            return purplemux_client.FeaturePreparationState(
                feature, base_state, expected_base_sha, True, "switch"
            )

        def synchronize_branch(self, _branch: str):
            pytest.fail("clean resume synchronized integration again")

        def prepare_feature_branch(self, *_args: object, **_kwargs: object):
            pytest.fail("clean resume prepared the feature again")

    state = SAMPLE_MODULE.Recovery(
        phase="issue_implementation_done",
        issue_number=1,
        prepared_base_sha=base_sha,
    )

    assert (
        SAMPLE_MODULE.prepare_issue(
            Repo(), GitHub(), config(tmp_path).issues[0], config(tmp_path), state
        )
        is None
    )
    assert state.prepared_base_sha == base_sha
    assert events == [
        "pr:OPEN",
        "pr:MERGED",
        "pr:OPEN",
        "worktree",
        f"current:{branch}",
        f"prepared:{branch}:dev/v1.2.3:{base_sha}",
    ]


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


def test_issue_delivery_pushes_and_creates_exact_draft_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = SAMPLE_MODULE.Recovery(prepared_base_sha="b" * 40)
    expected_pr = pr()
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(SAMPLE_MODULE, "save_checkpoint", lambda *_args: None)

    class Repo:
        def require_current_branch(self, branch: str):
            events.append(("current", branch))
            return purplemux_client.BranchState(branch, "a" * 40, None, True)

        def ensure_pushed(self, branch: str, *, expected_local_sha: str):
            events.append(("push", (branch, expected_local_sha)))
            return purplemux_client.BranchState(
                branch, expected_local_sha, expected_local_sha, True
            )

    class GitHub:
        def find_pr(self, **kwargs: object):
            events.append(("find", kwargs))
            return None

        def create_draft_pr(self, **kwargs: object):
            events.append(("create", kwargs))
            return expected_pr

        def require_pr(self, **kwargs: object):
            events.append(("require", kwargs))
            return expected_pr

    delivered = SAMPLE_MODULE.ensure_issue_pr(
        Repo(), GitHub(), config(tmp_path).issues[0], config(tmp_path), state
    )

    assert delivered == expected_pr
    create = next(value for name, value in events if name == "create")
    assert isinstance(create, dict)
    assert create["head"] == "feature/issue-1"
    assert create["base"] == "dev/v1.2.3"
    assert create["expected_head_sha"] == "a" * 40
    assert create["expected_base_sha"] == "b" * 40
    required = next(value for name, value in events if name == "require")
    assert isinstance(required, dict)
    assert required["draft"] is True
    assert state.phase == "issue_delivery_done"


def test_no_change_policy_is_explicit_and_not_reviewer_approval(
    tmp_path: Path,
) -> None:
    state = SAMPLE_MODULE.Recovery(
        approved_sha="a" * 40,
        approved_base_sha="b" * 40,
        review_outcome="no-change-policy",
    )
    observed: list[dict[str, object]] = []

    class GitHub:
        def require_pr(self, **kwargs: object):
            observed.append(kwargs)
            return pr()

    assert SAMPLE_MODULE.require_reviewed_topology(GitHub(), pr(), state) == pr()
    assert observed[0]["expected_head_sha"] == "a" * 40
    assert state.review_outcome != "approved"

    state.review_outcome = None
    with pytest.raises(purplemux_client.WorkerFailure, match="accepted exact"):
        SAMPLE_MODULE.require_reviewed_topology(GitHub(), pr(), state)


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


def test_ready_integration_pr_without_exact_approval_stops_before_mutation(
    tmp_path: Path,
) -> None:
    existing = replace(
        pr(),
        is_draft=False,
        head_branch="dev/v1.2.3",
        base_branch="main",
    )
    events: list[str] = []

    class GitHub:
        def find_pr(self, **_kwargs: object):
            events.append("inspect-open")
            return existing

    class Repo:
        def synchronize_branch(self, _branch: str):
            events.append("git-mutation")
            pytest.fail("Git mutation followed an unapproved Ready integration PR")

    class Runtime:
        def list_workspaces(self):
            events.append("runtime")
            pytest.fail("runtime mutation followed an unapproved Ready integration PR")

    with pytest.raises(purplemux_client.WorkerFailure, match="checkpointed approval"):
        SAMPLE_MODULE.integration_review(
            config(tmp_path),
            Runtime(),
            Repo(),
            GitHub(),
            SAMPLE_MODULE.Recovery(),
        )

    assert events == ["inspect-open"]


@pytest.mark.parametrize(
    "phase", ["integration_review_turn_pending", "integration_fix_turn_pending"]
)
def test_pending_integration_turn_is_never_resent(tmp_path: Path, phase: str) -> None:
    class Unexpected:
        def __getattr__(self, name: str):
            pytest.fail(f"pending turn performed {name}")

    with pytest.raises(purplemux_client.MutationOutcomeUnknown, match="do not resend"):
        SAMPLE_MODULE.integration_review(
            config(tmp_path),
            Unexpected(),
            Unexpected(),
            Unexpected(),
            SAMPLE_MODULE.Recovery(phase=phase),
        )


def test_final_check_send_uncertainty_checkpoints_exact_shell_before_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoints: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "save_checkpoint",
        lambda name, data: checkpoints.append((name, data)),
    )
    state = SAMPLE_MODULE.Recovery(phase="integration_approved")

    class Client:
        def start_shell(self, _request: object, *, on_created: object):
            on_created("tab-checks", "/tmp/awm-shell-checks/result.json")
            raise purplemux_client.MutationOutcomeUnknown("send outcome unknown")

    with pytest.raises(purplemux_client.MutationOutcomeUnknown):
        SAMPLE_MODULE.run_final_checks(Client(), config(tmp_path), state)

    assert state.phase == "integration_checks_running"
    assert state.check_shell == "tab-checks"
    assert state.check_result_path == "/tmp/awm-shell-checks/result.json"
    assert checkpoints[-1][0] == "integration_checks_running"
    assert checkpoints[-1][1]["check_shell"] == "tab-checks"


def test_running_final_check_reattaches_without_new_shell_or_agent_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SAMPLE_MODULE, "save_checkpoint", lambda *_args: None)
    state = SAMPLE_MODULE.Recovery(
        phase="integration_checks_running",
        check_shell="tab-checks",
        check_result_path="/tmp/awm-shell-checks/result.json",
    )
    events: list[str] = []

    class Client:
        def start_shell(self, *_args: object, **_kwargs: object):
            pytest.fail("resumed final checks created a new shell")

        def send_input(self, *_args: object, **_kwargs: object):
            pytest.fail("resumed final checks sent an agent turn")

        def resume_shell(self, tab: str, result_path: str, *, cwd: str) -> None:
            events.append(f"resume:{tab}:{result_path}")

        def wait_for_shell_completion(self, tab: str, _timeout: int) -> None:
            events.append(f"wait:{tab}")

        def read_shell_result(self, tab: str):
            events.append(f"read:{tab}")
            return purplemux_client.ShellResult(0)

        def close_session(self, tab: str) -> None:
            events.append(f"close:{tab}")

    SAMPLE_MODULE.run_final_checks(Client(), config(tmp_path), state)

    assert state.phase == "integration_checks_complete"
    assert events == [
        "resume:tab-checks:/tmp/awm-shell-checks/result.json",
        "wait:tab-checks",
        "read:tab-checks",
    ]


def test_completed_final_check_reattaches_without_automatic_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SAMPLE_MODULE, "save_checkpoint", lambda *_args: None)
    state = SAMPLE_MODULE.Recovery(
        phase="integration_checks_complete",
        check_shell="tab-checks",
        check_result_path="/tmp/awm-shell-checks/result.json",
    )
    events: list[str] = []

    class Client:
        def resume_shell(self, tab: str, result_path: str, *, cwd: str) -> None:
            events.append(f"resume:{tab}:{result_path}")

        def close_session(self, tab: str) -> None:
            events.append(f"close:{tab}")

    SAMPLE_MODULE.run_final_checks(Client(), config(tmp_path), state)

    assert state.phase == "integration_checks_complete"
    assert events == [
        "resume:tab-checks:/tmp/awm-shell-checks/result.json",
    ]


def test_failed_final_check_emits_runner_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SAMPLE_MODULE, "save_checkpoint", lambda *_args: None)
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "emit_step",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    state = SAMPLE_MODULE.Recovery(
        phase="integration_checks_running",
        check_shell="tab-checks",
        check_result_path="/tmp/awm-shell-checks/result.json",
    )

    class Client:
        workspace_id = "ws-test"

        def resume_shell(self, _tab: str, _result_path: str, *, cwd: str) -> None:
            assert cwd == str(tmp_path)

        def wait_for_shell_completion(self, _tab: str, _timeout: int) -> None:
            pass

        def read_shell_result(self, _tab: str):
            return purplemux_client.ShellResult(
                2,
                diagnostic_output="expected branch main, got: feature/work",
                cwd=str(tmp_path),
                workspace_id="ws-test",
                tab_id="tab-checks",
            )

    with pytest.raises(purplemux_client.WorkerFailure) as failure:
        SAMPLE_MODULE.run_final_checks(Client(), config(tmp_path), state)

    message = str(failure.value)
    assert "final whole-version checks failed (exit code 2)" in message
    assert "expected branch main, got: feature/work" in message
    assert f"cwd: {tmp_path}" in message
    assert "workspace/tab: ws-test / tab-checks" in message
    assert emitted == [
        (
            ("final whole-version checks", "failed"),
            {
                "error": message,
                "workspace": "ws-test",
                "tab": "tab-checks",
            },
        )
    ]
