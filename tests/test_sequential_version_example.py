from __future__ import annotations

import ast
import runpy
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from purplemux_client import (
    BranchState,
    GitRepository,
    PullRequestState,
    WorkerFailure,
)
from purplemux_client.preflight import WorkflowValidator

EXAMPLE = Path(__file__).parents[1] / "examples" / "sequential-version-development.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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


def test_cleanup_commits_intended_mixed_work_and_ignores_generated_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    require_clean_worktree = workflow["require_clean_worktree"]
    repository_path = tmp_path / "repo"
    repository_path.mkdir()
    git(repository_path, "init", "-b", "feature/issue-116")
    git(repository_path, "config", "user.email", "test@example.com")
    git(repository_path, "config", "user.name", "Test User")
    git(
        repository_path,
        "remote",
        "add",
        "origin",
        "https://github.com/acme/project.git",
    )
    tracked = repository_path / "tracked.py"
    tracked.write_text("before\n", encoding="utf-8")
    git(repository_path, "add", "tracked.py")
    git(repository_path, "commit", "-m", "initial")

    tracked.write_text("after\n", encoding="utf-8")
    intended = repository_path / "new_test.py"
    intended.write_text("def test_feature():\n    assert True\n", encoding="utf-8")
    generated = repository_path / "build" / "cache.bin"
    generated.parent.mkdir()
    generated.write_text("generated", encoding="utf-8")
    repo = GitRepository.open(repository_path, expected_github_slug="acme/project")

    def cleanup_turn(*args: object, **kwargs: object) -> str:
        (repository_path / ".gitignore").write_text("/build/\n", encoding="utf-8")
        git(repository_path, "add", ".gitignore", "tracked.py", "new_test.py")
        git(repository_path, "commit", "-m", "preserve intended recovery work")
        return "committed intended work and ignored build output"

    monkeypatch.setitem(require_clean_worktree.__globals__, "run_turn", cleanup_turn)
    monkeypatch.setitem(
        require_clean_worktree.__globals__, "emit_finding", lambda *args, **kwargs: None
    )

    require_clean_worktree(
        repo, object(), "cleanup-tab", context="verifying mixed recovery"
    )

    assert git(repository_path, "status", "--porcelain=v1") == ""
    assert git(repository_path, "show", "HEAD:tracked.py") == "after"
    assert "test_feature" in git(repository_path, "show", "HEAD:new_test.py")
    assert git(repository_path, "show", "HEAD:.gitignore") == "/build/"
    assert generated.read_text(encoding="utf-8") == "generated"
    assert git(repository_path, "check-ignore", "build/cache.bin") == "build/cache.bin"


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


def test_reviewer_dirty_state_is_committed_delivered_and_re_reviewed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["process_issue"].__globals__
    issue = workflow["Issue"](116, "feature/issue-116")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    base_sha = "base-head"
    start_sha = "start-head"
    implementation_sha = "implementation-head"
    cleanup_sha = "review-cleanup-head"
    initial_pr = replace(
        open_pr(head=issue.branch, base=config.integration_branch, draft=True),
        head_sha=implementation_sha,
        base_sha=base_sha,
    )
    events: list[str] = []

    class Repository:
        def __init__(self) -> None:
            self.local_sha = implementation_sha
            self.dirty = False

        def inspect_worktree(self) -> SimpleNamespace:
            status = (" M reviewer-created.py",) if self.dirty else ()
            return SimpleNamespace(
                dirty=self.dirty, current_branch=issue.branch, status=status
            )

        def require_current_branch(self, branch: str) -> BranchState:
            assert branch == issue.branch
            return BranchState(branch, self.local_sha, self.local_sha, True)

        def require_committed_result(
            self, branch: str, *, previous_sha: str, allow_unchanged: bool
        ) -> BranchState:
            assert not self.dirty
            return BranchState(branch, self.local_sha, self.local_sha, True)

        def inspect_branch(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, base_sha, base_sha, False)

        def ensure_pushed(
            self, branch: str, *, expected_local_sha: str
        ) -> BranchState:
            events.append(f"push:{expected_local_sha}")
            assert branch == issue.branch
            assert expected_local_sha == self.local_sha
            return BranchState(branch, self.local_sha, self.local_sha, True)

    repository = Repository()
    current_pr = initial_pr

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            expected = str(kwargs["expected_head_sha"])
            events.append(f"require_pr:{expected}")
            current_pr = replace(current_pr, head_sha=expected)
            return current_pr

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            events.append(f"ready:{kwargs['expected_head_sha']}")
            assert kwargs["expected_head_sha"] == cleanup_sha
            return replace(current_pr, is_draft=False)

    review_count = 0

    def run_turn(*args: object, **kwargs: object) -> str:
        nonlocal review_count
        name = str(args[2])
        events.append(name)
        if name.endswith("review"):
            review_count += 1
            if review_count == 1:
                repository.dirty = True
            return "APPROVED"
        if name == "Clean worktree":
            assert repository.dirty
            repository.dirty = False
            repository.local_sha = cleanup_sha
            return "committed reviewer-created work"
        return "implemented"

    monkeypatch.setitem(
        workflow_globals,
        "prepare_issue",
        lambda *args: (initial_pr, start_sha, True),
    )
    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals, "ensure_issue_pr", lambda *args, **kwargs: initial_pr
    )
    monkeypatch.setitem(
        workflow_globals,
        "merge_pr_and_advance",
        lambda *args, **kwargs: SimpleNamespace(pr=replace(current_pr, state="MERGED")),
    )
    monkeypatch.setitem(workflow_globals, "emit_finding", lambda *args, **kwargs: None)

    workflow["process_issue"](issue, config, object(), repository, GitHub())

    assert review_count == 2
    assert f"push:{cleanup_sha}" in events
    assert f"require_pr:{cleanup_sha}" in events
    assert events.index("Clean worktree") < events.index(f"ready:{cleanup_sha}")


def test_normal_issue_path_commits_pushes_and_creates_exact_draft_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["process_issue"].__globals__
    issue = workflow["Issue"](116, "feature/issue-116")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    start_sha = "start-head"
    implementation_sha = "implementation-head"
    base_sha = "integration-head"
    events: list[str] = []

    class Repository:
        def __init__(self) -> None:
            self.local_sha = start_sha

        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(
                dirty=False, current_branch=issue.branch, status=()
            )

        def require_current_branch(self, branch: str) -> BranchState:
            assert branch == issue.branch
            return BranchState(branch, self.local_sha, None, True)

        def require_committed_result(
            self, branch: str, *, previous_sha: str, allow_unchanged: bool
        ) -> BranchState:
            events.append(f"commit:{self.local_sha}")
            assert branch == issue.branch
            assert self.local_sha != previous_sha or allow_unchanged
            return BranchState(branch, self.local_sha, None, True)

        def inspect_branch(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, base_sha, base_sha, False)

        def ensure_pushed(
            self, branch: str, *, expected_local_sha: str
        ) -> BranchState:
            events.append(f"push:{expected_local_sha}")
            assert (branch, expected_local_sha) == (
                issue.branch,
                implementation_sha,
            )
            return BranchState(branch, expected_local_sha, expected_local_sha, True)

    repository = Repository()
    draft = replace(
        open_pr(head=issue.branch, base=config.integration_branch, draft=True),
        head_sha=implementation_sha,
        base_sha=base_sha,
    )

    class GitHub:
        def find_pr(
            self, *, head: str, base: str, state: str
        ) -> PullRequestState | None:
            assert (head, base, state) == (
                issue.branch,
                config.integration_branch,
                "OPEN",
            )
            return None

        def create_draft_pr(self, **kwargs: object) -> PullRequestState:
            events.append(f"draft:{kwargs['expected_head_sha']}")
            assert kwargs["head"] == issue.branch
            assert kwargs["base"] == config.integration_branch
            assert kwargs["expected_head_sha"] == implementation_sha
            assert kwargs["expected_base_sha"] == base_sha
            return draft

        def require_pr(self, **kwargs: object) -> PullRequestState:
            events.append(f"require_pr:{kwargs['expected_head_sha']}")
            assert kwargs["draft"] is True
            assert kwargs["expected_head_sha"] == implementation_sha
            assert kwargs["expected_base_sha"] == base_sha
            return draft

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            events.append("ready")
            return replace(draft, is_draft=False)

    def run_turn(*args: object, **kwargs: object) -> str:
        name = str(args[2])
        if name.endswith("implementation"):
            repository.local_sha = implementation_sha
            return "implemented and committed"
        if name.endswith("review"):
            return "APPROVED"
        raise AssertionError(f"unexpected turn {name}")

    monkeypatch.setitem(
        workflow_globals,
        "prepare_issue",
        lambda *args: (None, start_sha, False),
    )
    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(workflow_globals, "MERGE_TO_INTEGRATION", False)

    workflow["process_issue"](issue, config, object(), repository, GitHub())

    commit_index = events.index(f"commit:{implementation_sha}")
    push_index = events.index(f"push:{implementation_sha}")
    draft_index = events.index(f"draft:{implementation_sha}")
    verify_index = events.index(f"require_pr:{implementation_sha}")
    assert commit_index < push_index < draft_index < verify_index
    assert events[-1] == "ready"


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
        "require_agent_result",
        lambda *args, **kwargs: (ready.head_sha, False),
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


def test_unchanged_whole_version_fixer_still_runs_checks_before_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["integration_delivery"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    draft = open_pr(
        head=config.integration_branch, base=config.main_branch, draft=True
    )
    events: list[str] = []

    class Repository:
        def synchronize_branch(self, branch: str) -> BranchState:
            return BranchState(branch, draft.head_sha, draft.head_sha, True)

        def inspect_branch(self, branch: str) -> BranchState:
            return BranchState(branch, draft.base_sha, draft.base_sha, False)

    class GitHub:
        def find_pr(
            self, *, head: str, base: str, state: str
        ) -> PullRequestState | None:
            return draft if state == "OPEN" else None

        def require_pr(self, **kwargs: object) -> PullRequestState:
            events.append("require_pr")
            return draft

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            events.append("ready")
            return replace(draft, is_draft=False)

    def run_turn(*args: object, **kwargs: object) -> str:
        name = str(args[2])
        events.append(name)
        return "CHANGES_REQUESTED\nNo source change is actually warranted."

    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: (draft.head_sha, False),
    )
    monkeypatch.setitem(
        workflow_globals,
        "run_final_checks",
        lambda *args: events.append("final checks"),
    )

    result = workflow["integration_delivery"](
        config, object(), Repository(), GitHub()
    )

    assert result.is_draft is False
    assert events.count("Whole-version review") == 1
    assert events.count("Whole-version fixes") == 1
    assert events.count("final checks") == 1
    assert events.index("Whole-version fixes") < events.index("final checks")
    assert events.index("final checks") < events.index("ready")


def test_final_check_dirty_state_invalidates_approval_and_repeats_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["integration_delivery"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    initial_sha = "integration-head"
    cleanup_sha = "check-cleanup-head"
    base_sha = "main-head"
    current_pr = replace(
        open_pr(
            head=config.integration_branch, base=config.main_branch, draft=True
        ),
        head_sha=initial_sha,
        base_sha=base_sha,
    )
    events: list[str] = []

    class Repository:
        def __init__(self) -> None:
            self.local_sha = initial_sha
            self.dirty = False

        def synchronize_branch(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, self.local_sha, self.local_sha, True)

        def inspect_branch(self, branch: str) -> BranchState:
            assert branch == config.main_branch
            return BranchState(branch, base_sha, base_sha, False)

        def inspect_worktree(self) -> SimpleNamespace:
            status = ("?? generated-report.txt",) if self.dirty else ()
            return SimpleNamespace(
                dirty=self.dirty,
                current_branch=config.integration_branch,
                status=status,
            )

        def require_current_branch(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, self.local_sha, self.local_sha, True)

        def require_committed_result(
            self, branch: str, *, previous_sha: str, allow_unchanged: bool
        ) -> BranchState:
            assert not self.dirty
            return BranchState(branch, self.local_sha, self.local_sha, True)

        def ensure_pushed(
            self, branch: str, *, expected_local_sha: str
        ) -> BranchState:
            events.append(f"push:{expected_local_sha}")
            return BranchState(branch, expected_local_sha, expected_local_sha, True)

    repository = Repository()

    class GitHub:
        def find_pr(
            self, *, head: str, base: str, state: str
        ) -> PullRequestState | None:
            assert (head, base) == (
                config.integration_branch,
                config.main_branch,
            )
            return current_pr if state == "OPEN" else None

        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            expected = str(kwargs["expected_head_sha"])
            events.append(f"require_pr:{expected}")
            current_pr = replace(current_pr, head_sha=expected)
            return current_pr

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            events.append(f"ready:{kwargs['expected_head_sha']}")
            assert kwargs["expected_head_sha"] == cleanup_sha
            return replace(current_pr, is_draft=False)

    review_count = 0
    check_count = 0

    def run_turn(*args: object, **kwargs: object) -> str:
        nonlocal review_count
        name = str(args[2])
        events.append(name)
        if name == "Whole-version review":
            review_count += 1
            return "APPROVED"
        if name == "Clean worktree":
            assert repository.dirty
            repository.dirty = False
            repository.local_sha = cleanup_sha
            return "committed final-check artifacts"
        raise AssertionError(f"unexpected turn {name}")

    def final_checks(*args: object) -> None:
        nonlocal check_count
        check_count += 1
        events.append("final checks")
        if check_count == 1:
            repository.dirty = True

    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(workflow_globals, "run_final_checks", final_checks)
    monkeypatch.setitem(workflow_globals, "emit_finding", lambda *args, **kwargs: None)

    ready = workflow["integration_delivery"](
        config, object(), repository, GitHub()
    )

    assert ready.is_draft is False
    assert review_count == 2
    assert check_count == 2
    assert f"push:{cleanup_sha}" in events
    assert events.index("Clean worktree") < events.index(f"ready:{cleanup_sha}")


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
