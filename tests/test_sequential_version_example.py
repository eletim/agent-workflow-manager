from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

import purplemux_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SAMPLE = REPOSITORY_ROOT / "examples" / "sequential-version-development.py"
GUIDE = (
    REPOSITORY_ROOT
    / "src"
    / "purplemux_client"
    / "web_static"
    / "python-workflow-guide.md"
)


def load_sample_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "canonical_sequential_version_sample", SAMPLE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SAMPLE_MODULE = load_sample_module()


def sample_tree() -> ast.Module:
    source = SAMPLE.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(SAMPLE))


def test_canonical_sample_compiles_and_uses_only_public_client_api() -> None:
    tree = sample_tree()
    compile(tree, str(SAMPLE), "exec")
    public_api = set(purplemux_client.__all__)
    imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "purplemux_client"
        for alias in node.names
    }

    assert imports
    assert imports <= public_api


def test_workflow_guide_prominently_links_to_existing_canonical_sample() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    first_section = guide.split("## Architecture and responsibility", maxsplit=1)[0]
    match = re.search(
        r"\[[^]]+\]\(([^)]+sequential-version-development\.py)\)", first_section
    )

    assert match is not None
    assert (GUIDE.parent / match.group(1)).resolve() == SAMPLE.resolve()
    assert SAMPLE.is_file()


def test_sample_has_separate_issue_and_whole_version_review_phases() -> None:
    source = SAMPLE.read_text(encoding="utf-8")

    assert "def process_issue(" in source
    assert "def integration_review(" in source
    assert "whole-version integration review" in source
    assert "MAX_REVIEWS = 4" in source
    assert "reviewer=create_agent" not in source  # no hidden/delegated orchestration
    assert "issue_reviewer_create_pending" in source
    assert "integration_reviewer_create_pending" in source
    assert "integration_checks_start_pending" in source
    assert "do not replay creation" in source
    assert "issue_implementation_turn_done" in source
    assert "issue_review_turn_done" in source
    assert "issue_fix_turn_done" in source
    assert "integration_review_turn_done" in source
    assert "integration_fix_turn_done" in source


def test_issue_prs_can_only_target_integration_and_main_is_never_merged() -> None:
    tree = sample_tree()
    source = SAMPLE.read_text(encoding="utf-8")
    functions = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    merge_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        segment = ast.get_source_segment(source, node) or ""
        if '"merge"' in segment and '"gh"' in segment:
            merge_calls.append(segment)

    assert "config.integration_branch" in functions["issue_prs"]
    assert "config.main_branch" not in functions["issue_prs"]
    assert merge_calls
    assert all("main_branch" not in call for call in merge_calls)
    assert all('"--auto"' not in call for call in merge_calls)
    assert '"--match-head-commit"' in functions["merge_issue_pr"]
    assert "--delete-branch=false" not in source
    assert "merge_issue_pr(issue, config, recovery)" in source
    assert "never merged automatically" in source


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/OWNER/REPOSITORY.git",
        "git@github.com:OWNER/REPOSITORY.git",
        "ssh://git@github.com/OWNER/REPOSITORY.git",
    ],
)
def test_origin_parser_accepts_exact_github_forms(origin: str) -> None:
    assert SAMPLE_MODULE.github_origin_slug(origin) == "OWNER/REPOSITORY"


@pytest.mark.parametrize(
    "origin",
    [
        "https://evilgithub.com/OWNER/REPOSITORY.git",
        "https://github.com/prefix/OWNER/REPOSITORY.git",
        "https://github.com:evil/OWNER/REPOSITORY.git",
        "git@github.com:prefix/OWNER/REPOSITORY.git",
    ],
)
def test_origin_parser_rejects_lookalike_hosts_and_path_prefixes(origin: str) -> None:
    with pytest.raises(purplemux_client.WorkerFailure):
        SAMPLE_MODULE.github_origin_slug(origin)


def test_approved_head_change_is_rejected_after_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    published: dict[str, object] = {}

    def record_checkpoint(name: str, data: dict[str, str]) -> None:
        published.update(name=name, data=data)

    monkeypatch.setattr(SAMPLE_MODULE, "save_checkpoint", record_checkpoint)
    original = SAMPLE_MODULE.Recovery(
        workspace="ws-1",
        phase="issue_approved",
        issue_number=1,
        implementer="tab-1",
        reviewer="tab-2",
        reviews_used=1,
        approved_sha="a" * 40,
    )
    original.checkpoint(config)
    checkpoint = purplemux_client.ResumeCheckpoint(
        name=str(published["name"]), data=published["data"]
    )

    resumed = SAMPLE_MODULE.load_recovery(config, checkpoint)

    assert resumed.approved_sha == "a" * 40
    with pytest.raises(purplemux_client.WorkerFailure, match="changed after approval"):
        SAMPLE_MODULE.require_approved_head("b" * 40, resumed, "Issue PR")


def test_issue_approved_resume_with_new_head_runs_a_new_bounded_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    recovery = SAMPLE_MODULE.Recovery(
        workspace="ws-1",
        phase="issue_approved",
        issue_number=1,
        implementer="tab-1",
        reviewer="tab-2",
        reviews_used=1,
        approved_sha="a" * 40,
    )
    new_sha = "b" * 40
    reviews: list[int | None] = []
    monkeypatch.setattr(SAMPLE_MODULE, "merged_issue_pr", lambda *_args: None)
    monkeypatch.setattr(SAMPLE_MODULE, "worktree_is_dirty", lambda _config: False)
    monkeypatch.setattr(SAMPLE_MODULE, "prepare_issue_branch", lambda *_args: None)
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "require_issue_head",
        lambda *_args: (new_sha, {"headRefOid": new_sha}),
    )
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "run_turn",
        lambda *_args, iteration=None, **_kwargs: (
            reviews.append(iteration) or "APPROVED"
        ),
    )
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "merge_issue_pr",
        lambda _issue, _config, state: {
            "url": "https://github.com/OWNER/REPOSITORY/pull/1",
            "headRefOid": state.approved_sha,
        },
    )
    monkeypatch.setattr(SAMPLE_MODULE, "close_tabs", lambda *_args: None)

    SAMPLE_MODULE.process_issue(config.issues[0], config, object(), recovery)

    assert reviews == [2]
    assert recovery.phase == "workspace_ready"


def test_integration_approved_resume_with_new_head_rereviews_and_rechecks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    recovery = SAMPLE_MODULE.Recovery(
        workspace="ws-1",
        phase="integration_checks_done",
        implementer="tab-1",
        reviewer="tab-2",
        reviews_used=1,
        approved_sha="a" * 40,
    )
    new_sha = "b" * 40
    pr = {
        "number": 10,
        "url": "https://github.com/OWNER/REPOSITORY/pull/10",
        "state": "OPEN",
        "isDraft": True,
        "headRefName": config.integration_branch,
        "headRefOid": new_sha,
        "baseRefName": config.main_branch,
    }
    reviews: list[int | None] = []
    checks: list[str] = []
    monkeypatch.setattr(SAMPLE_MODULE, "worktree_is_dirty", lambda _config: False)
    monkeypatch.setattr(SAMPLE_MODULE, "switch_to_integration", lambda _config: None)
    monkeypatch.setattr(SAMPLE_MODULE, "integration_prs", lambda *_args: [pr])
    monkeypatch.setattr(SAMPLE_MODULE, "ensure_draft_integration_pr", lambda *_args: pr)
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "require_integration_head",
        lambda *_args: (new_sha, pr),
    )
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "run_turn",
        lambda *_args, iteration=None, **_kwargs: (
            reviews.append(iteration) or "APPROVED"
        ),
    )

    def run_checks(_client: object, _config: object, state: object, name: str) -> None:
        checks.append(name)
        state.phase = "integration_checks_done"

    def mutate(args: list[str], _config: object) -> str:
        if args[:3] == ["gh", "pr", "ready"]:
            pr["isDraft"] = False
        return ""

    monkeypatch.setattr(SAMPLE_MODULE, "run_checks_in_purplemux", run_checks)
    monkeypatch.setattr(SAMPLE_MODULE, "mutate", mutate)
    monkeypatch.setattr(SAMPLE_MODULE, "close_tabs", lambda *_args: None)

    ready = SAMPLE_MODULE.integration_review(config, object(), recovery)

    assert reviews == [2]
    assert checks == ["final whole-version checks"]
    assert ready["isDraft"] is False
    assert recovery.phase == "integration_ready"


def test_issue_approval_drift_after_fourth_review_exhausts_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    recovery = SAMPLE_MODULE.Recovery(
        workspace="ws-1",
        phase="issue_approved",
        issue_number=1,
        implementer="tab-1",
        reviewer="tab-2",
        reviews_used=SAMPLE_MODULE.MAX_REVIEWS,
        approved_sha="a" * 40,
    )
    new_sha = "b" * 40
    monkeypatch.setattr(SAMPLE_MODULE, "merged_issue_pr", lambda *_args: None)
    monkeypatch.setattr(SAMPLE_MODULE, "worktree_is_dirty", lambda _config: False)
    monkeypatch.setattr(SAMPLE_MODULE, "prepare_issue_branch", lambda *_args: None)
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "require_issue_head",
        lambda *_args: (new_sha, {"headRefOid": new_sha}),
    )
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "run_turn",
        lambda *_args, **_kwargs: pytest.fail("review limit was bypassed"),
    )

    with pytest.raises(purplemux_client.WorkerFailure, match="ended without approval"):
        SAMPLE_MODULE.process_issue(config.issues[0], config, object(), recovery)

    assert recovery.phase == "issue_fix_done"
    assert recovery.reviews_used == SAMPLE_MODULE.MAX_REVIEWS
    assert recovery.approved_sha is None


def test_integration_approval_drift_after_fourth_review_exhausts_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    recovery = SAMPLE_MODULE.Recovery(
        workspace="ws-1",
        phase="integration_approved",
        implementer="tab-1",
        reviewer="tab-2",
        reviews_used=SAMPLE_MODULE.MAX_REVIEWS,
        approved_sha="a" * 40,
    )
    new_sha = "b" * 40
    pr = {
        "number": 10,
        "url": "https://github.com/OWNER/REPOSITORY/pull/10",
        "state": "OPEN",
        "isDraft": True,
        "headRefName": config.integration_branch,
        "headRefOid": new_sha,
        "baseRefName": config.main_branch,
    }
    monkeypatch.setattr(SAMPLE_MODULE, "worktree_is_dirty", lambda _config: False)
    monkeypatch.setattr(SAMPLE_MODULE, "switch_to_integration", lambda _config: None)
    monkeypatch.setattr(SAMPLE_MODULE, "integration_prs", lambda *_args: [pr])
    monkeypatch.setattr(SAMPLE_MODULE, "ensure_draft_integration_pr", lambda *_args: pr)
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "require_integration_head",
        lambda *_args: (new_sha, pr),
    )
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "run_turn",
        lambda *_args, **_kwargs: pytest.fail("review limit was bypassed"),
    )

    with pytest.raises(purplemux_client.WorkerFailure, match="ended without approval"):
        SAMPLE_MODULE.integration_review(config, object(), recovery)

    assert recovery.phase == "integration_fix_done"
    assert recovery.reviews_used == SAMPLE_MODULE.MAX_REVIEWS
    assert recovery.approved_sha is None


def test_dirty_integration_resume_on_wrong_branch_stops_before_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    recovery = SAMPLE_MODULE.Recovery(
        workspace="ws-1",
        phase="integration_fix_done",
        implementer="tab-1",
        reviewer="tab-2",
    )
    monkeypatch.setattr(SAMPLE_MODULE, "worktree_is_dirty", lambda _config: True)
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "read_text",
        lambda args, _config: "feature/wrong-branch",
    )

    with pytest.raises(purplemux_client.WorkerFailure, match="expected 'dev/v1.2.3'"):
        SAMPLE_MODULE.integration_review(config, object(), recovery)


def test_ready_integration_pr_with_changed_head_is_returned_to_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    recovery = SAMPLE_MODULE.Recovery(
        workspace="ws-1",
        phase="integration_ready",
        approved_sha="a" * 40,
    )
    mutations: list[list[str]] = []
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "mutate",
        lambda args, _config: mutations.append(list(args)) or "",
    )

    with pytest.raises(
        purplemux_client.WorkerFailure, match="Draft status was restored"
    ):
        SAMPLE_MODULE.restore_draft_if_ready_head_is_unapproved(
            {
                "number": 10,
                "isDraft": False,
                "headRefOid": "b" * 40,
            },
            recovery,
            config,
        )

    assert mutations == [
        ["gh", "pr", "ready", "10", "--undo", "--repo", "OWNER/REPOSITORY"]
    ]
    assert recovery.phase == "integration_fix_done"
    assert recovery.approved_sha is None


@pytest.mark.parametrize(
    ("existing_ref", "other_ref"),
    [
        ("refs/heads/feature/issue-1", "refs/remotes/origin/feature/issue-1"),
        ("refs/remotes/origin/feature/issue-1", "refs/heads/feature/issue-1"),
    ],
    ids=("stale-local", "unrelated-remote"),
)
def test_existing_issue_branch_must_contain_latest_integration_tip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_ref: str,
    other_ref: str,
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    monkeypatch.setattr(SAMPLE_MODULE, "switch_to_integration", lambda _config: None)
    monkeypatch.setattr(SAMPLE_MODULE, "mutate", lambda args, _config: "")
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "branch_exists",
        lambda ref, _config: ref == existing_ref,
    )

    def reject_ancestry(ancestor: str, descendant: str, _config: object) -> None:
        assert ancestor == "origin/dev/v1.2.3"
        assert descendant == "HEAD"
        raise purplemux_client.WorkerFailure("not based on latest integration")

    monkeypatch.setattr(SAMPLE_MODULE, "require_ancestor", reject_ancestry)

    with pytest.raises(purplemux_client.WorkerFailure, match="not based"):
        SAMPLE_MODULE.prepare_issue_branch(config.issues[0], config)
    assert other_ref != existing_ref


@pytest.mark.parametrize(
    "local_behind", [True, False], ids=("local-behind", "remote-behind")
)
def test_existing_issue_branch_reconciles_before_ancestry_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_behind: bool
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    local_sha = "a" * 40
    remote_sha = "b" * 40
    merge_base = local_sha if local_behind else remote_sha
    events: list[tuple[str, ...]] = []
    monkeypatch.setattr(SAMPLE_MODULE, "switch_to_integration", lambda _config: None)
    monkeypatch.setattr(SAMPLE_MODULE, "branch_exists", lambda *_args: True)

    def mutate(args: list[str], _config: object) -> str:
        events.append(tuple(args))
        return ""

    def read_text(args: list[str], _config: object) -> str:
        if args[:3] == ["git", "merge-base", "HEAD"]:
            return merge_base
        if args[-1] == "HEAD":
            return local_sha
        return remote_sha

    def require_ancestor(ancestor: str, descendant: str, _config: object) -> None:
        events.append(("ancestry", ancestor, descendant))

    monkeypatch.setattr(SAMPLE_MODULE, "mutate", mutate)
    monkeypatch.setattr(SAMPLE_MODULE, "read_text", read_text)
    monkeypatch.setattr(SAMPLE_MODULE, "require_ancestor", require_ancestor)

    SAMPLE_MODULE.prepare_issue_branch(config.issues[0], config)

    ancestry_event = ("ancestry", "origin/dev/v1.2.3", "HEAD")
    assert events[-1] == ancestry_event
    merge_command = ("git", "merge", "--ff-only", "refs/remotes/origin/feature/issue-1")
    assert (merge_command in events) is local_behind


def test_git_ancestry_rejects_stale_or_unrelated_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", ""),
    )

    with pytest.raises(purplemux_client.WorkerFailure, match="not based on the latest"):
        SAMPLE_MODULE.require_ancestor(
            "origin/dev/v1.2.3", "refs/heads/feature/issue-1", config
        )


@pytest.mark.parametrize("failure_point", ["send", "wait"])
def test_issue_turn_timeout_resume_does_not_resend_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    recovery = SAMPLE_MODULE.Recovery(
        workspace="ws-1",
        phase="issue_sessions_ready",
        issue_number=1,
        implementer="tab-1",
        reviewer="tab-2",
    )
    checkpoints: list[str] = []

    class TimeoutClient:
        workspace_id = "ws-1"

        def __init__(self) -> None:
            self.send_attempts = 0

        def wait_until_ready(self, _tab: str, _timeout: int) -> None:
            return None

        def send_input(self, _tab: str, _prompt: str) -> None:
            assert recovery.phase == "issue_implementation_turn_pending"
            assert checkpoints == ["issue_implementation_turn_pending"]
            self.send_attempts += 1
            if failure_point == "send":
                raise purplemux_client.MutationOutcomeUnknown("send timed out")

        def wait_for_turn_completion(self, _tab: str, _timeout: int) -> None:
            raise purplemux_client.WorkerFailure("turn timed out")

    client = TimeoutClient()
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "save_checkpoint",
        lambda name, _data: checkpoints.append(name),
    )
    monkeypatch.setattr(SAMPLE_MODULE, "merged_issue_pr", lambda *_args: None)
    monkeypatch.setattr(SAMPLE_MODULE, "worktree_is_dirty", lambda _config: False)
    monkeypatch.setattr(SAMPLE_MODULE, "prepare_issue_branch", lambda *_args: None)

    expected_error = (
        purplemux_client.MutationOutcomeUnknown
        if failure_point == "send"
        else purplemux_client.WorkerFailure
    )
    with pytest.raises(expected_error):
        SAMPLE_MODULE.process_issue(config.issues[0], config, client, recovery)

    assert checkpoints == ["issue_implementation_turn_pending"]
    assert recovery.phase == "issue_implementation_turn_pending"
    assert client.send_attempts == 1

    with pytest.raises(
        purplemux_client.MutationOutcomeUnknown, match="do not resend the prompt"
    ):
        SAMPLE_MODULE.process_issue(config.issues[0], config, client, recovery)

    assert client.send_attempts == 1


@pytest.mark.parametrize(
    "phase",
    ["issue_review_turn_pending", "issue_fix_turn_pending"],
)
def test_issue_pending_review_or_fix_resume_stops_before_resend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    recovery = SAMPLE_MODULE.Recovery(
        workspace="ws-1",
        phase=phase,
        issue_number=1,
        implementer="tab-1",
        reviewer="tab-2",
    )
    monkeypatch.setattr(SAMPLE_MODULE, "merged_issue_pr", lambda *_args: None)
    monkeypatch.setattr(SAMPLE_MODULE, "worktree_is_dirty", lambda _config: False)
    monkeypatch.setattr(SAMPLE_MODULE, "prepare_issue_branch", lambda *_args: None)
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "run_turn",
        lambda *_args, **_kwargs: pytest.fail("pending turn was resent"),
    )

    with pytest.raises(
        purplemux_client.MutationOutcomeUnknown, match="do not resend the prompt"
    ):
        SAMPLE_MODULE.process_issue(config.issues[0], config, object(), recovery)


@pytest.mark.parametrize(
    "phase",
    ["integration_review_turn_pending", "integration_fix_turn_pending"],
)
def test_integration_pending_turn_resume_stops_before_resend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    recovery = SAMPLE_MODULE.Recovery(
        workspace="ws-1",
        phase=phase,
        implementer="tab-1",
        reviewer="tab-2",
    )
    pr = {
        "number": 10,
        "url": "https://github.com/OWNER/REPOSITORY/pull/10",
        "state": "OPEN",
        "isDraft": True,
        "headRefName": config.integration_branch,
        "headRefOid": "a" * 40,
        "baseRefName": config.main_branch,
    }
    monkeypatch.setattr(SAMPLE_MODULE, "worktree_is_dirty", lambda _config: False)
    monkeypatch.setattr(SAMPLE_MODULE, "switch_to_integration", lambda _config: None)
    monkeypatch.setattr(SAMPLE_MODULE, "integration_prs", lambda *_args: [pr])
    monkeypatch.setattr(SAMPLE_MODULE, "ensure_draft_integration_pr", lambda *_args: pr)
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "run_turn",
        lambda *_args, **_kwargs: pytest.fail("pending turn was resent"),
    )

    with pytest.raises(
        purplemux_client.MutationOutcomeUnknown, match="do not resend the prompt"
    ):
        SAMPLE_MODULE.integration_review(config, object(), recovery)


def test_confirmed_review_resumes_post_result_verification_without_resend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SAMPLE_MODULE.Config(
        repo=tmp_path,
        slug="OWNER/REPOSITORY",
        integration_branch="dev/v1.2.3",
        main_branch="main",
        issues=(SAMPLE_MODULE.Issue(1, "feature/issue-1"),),
        check_command="make test",
    )
    recovery = SAMPLE_MODULE.Recovery(
        workspace="ws-1",
        phase="issue_implementation_done",
        issue_number=1,
        implementer="tab-1",
        reviewer="tab-2",
    )
    reviewed_sha = "a" * 40
    checkpoints: list[tuple[str, dict[str, str]]] = []

    class CompletedTurnClient:
        workspace_id = "ws-1"

        def __init__(self) -> None:
            self.send_attempts = 0
            self.result_reads = 0

        def wait_until_ready(self, _tab: str, _timeout: int) -> None:
            return None

        def send_input(self, _tab: str, _prompt: str) -> None:
            self.send_attempts += 1

        def wait_for_turn_completion(self, _tab: str, _timeout: int) -> None:
            return None

        def read_result(self, _tab: str) -> str:
            self.result_reads += 1
            return "APPROVED\n"

    client = CompletedTurnClient()
    head_checks = 0

    def require_issue_head(
        _issue: object, _config: object
    ) -> tuple[str, dict[str, str]]:
        nonlocal head_checks
        head_checks += 1
        if head_checks == 2:
            raise purplemux_client.WorkerFailure("post-result verification timed out")
        return reviewed_sha, {"headRefOid": reviewed_sha}

    monkeypatch.setattr(
        SAMPLE_MODULE,
        "save_checkpoint",
        lambda name, data: checkpoints.append((name, dict(data))),
    )
    monkeypatch.setattr(SAMPLE_MODULE, "merged_issue_pr", lambda *_args: None)
    monkeypatch.setattr(SAMPLE_MODULE, "worktree_is_dirty", lambda _config: False)
    monkeypatch.setattr(SAMPLE_MODULE, "prepare_issue_branch", lambda *_args: None)
    monkeypatch.setattr(SAMPLE_MODULE, "require_issue_head", require_issue_head)
    monkeypatch.setattr(
        SAMPLE_MODULE,
        "merge_issue_pr",
        lambda _issue, _config, state: {
            "url": "https://github.com/OWNER/REPOSITORY/pull/1",
            "headRefOid": state.approved_sha,
        },
    )
    monkeypatch.setattr(SAMPLE_MODULE, "close_tabs", lambda *_args: None)

    with pytest.raises(
        purplemux_client.WorkerFailure, match="post-result verification timed out"
    ):
        SAMPLE_MODULE.process_issue(config.issues[0], config, client, recovery)

    assert recovery.phase == "issue_review_turn_done"
    assert recovery.turn_sha == reviewed_sha
    assert [name for name, _data in checkpoints[-2:]] == [
        "issue_review_turn_pending",
        "issue_review_turn_done",
    ]
    assert client.send_attempts == 1
    assert client.result_reads == 1

    confirmed_name, confirmed_data = checkpoints[-1]
    recovery = SAMPLE_MODULE.load_recovery(
        config,
        purplemux_client.ResumeCheckpoint(
            name=confirmed_name,
            data=confirmed_data,
        ),
    )
    SAMPLE_MODULE.process_issue(config.issues[0], config, client, recovery)

    assert recovery.phase == "workspace_ready"
    assert client.send_attempts == 1
    assert client.result_reads == 2
