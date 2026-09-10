from __future__ import annotations

import ast
import hashlib
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
    assert "except MutationOutcomeUnknown:" in source
    assert "existing_pr is not None or reused_existing_work" in source
    assert '"Deliver the exact Issue topology"' in source
    assert "Deliver the exact approved Issue topology" not in source


def test_outline_step_logs_terminal_progress_without_replacing_events(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    globals_ = workflow["run_outline_step"].__globals__
    events: list[tuple[str, str]] = []
    monkeypatch.setitem(
        globals_,
        "emit_step",
        lambda name, status, **kwargs: events.append((name, status)),
    )

    result = workflow["run_outline_step"]("Issue #190", lambda: "delivered")

    assert result == "delivered"
    assert events == [("Issue #190", "started"), ("Issue #190", "completed")]
    assert capsys.readouterr().out.splitlines() == [
        "[workflow] START Issue #190",
        "[workflow] DONE Issue #190",
    ]


def test_terminal_progress_formats_iteration_and_detail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    terminal_progress = runpy.run_path(str(EXAMPLE))["terminal_progress"]

    terminal_progress("START", "Issue #190 correctness review", iteration=2)
    terminal_progress(
        "IDENTIFIED",
        "Final integration PR",
        detail="PR #201 https://example.test/pull/201",
    )

    assert capsys.readouterr().out.splitlines() == [
        "[workflow] START Issue #190 correctness review (iteration 2)",
        "[workflow] IDENTIFIED Final integration PR: "
        "PR #201 https://example.test/pull/201",
    ]


def test_canonical_workflow_logs_major_issue_driven_boundaries() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    assert 'terminal_progress("WORK ITEM", issue.label, detail=issue.branch)' in source
    assert (
        'terminal_progress("WARN CONTINUATION", f"{issue.label} {phase} review")'
        in source
    )
    assert 'terminal_progress("WARN CONTINUATION", "Whole-version review")' in source
    assert '"PREPARE",\n        "Final integration PR"' in source
    assert '"IDENTIFIED", "Final integration PR"' in source


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("APPROVED", "APPROVED"),
        ("## APPROVED", "APPROVED"),
        ("**APPROVED**", "APPROVED"),
        ("Verdict: APPROVED", "APPROVED"),
        ("**Verdict: APPROVED**", "APPROVED"),
        ("Review result:\nAPPROVED\n\n- no findings", "APPROVED"),
        ("CHANGES_REQUESTED", "CHANGES_REQUESTED"),
        ("### CHANGES_REQUESTED", "CHANGES_REQUESTED"),
        ("**CHANGES_REQUESTED**", "CHANGES_REQUESTED"),
        ("Verdict: CHANGES_REQUESTED", "CHANGES_REQUESTED"),
        ("`Verdict: CHANGES_REQUESTED`", "CHANGES_REQUESTED"),
        ("Review result:\nCHANGES_REQUESTED\n\n- finding", "CHANGES_REQUESTED"),
        ("`approved`", "APPROVED"),
        ("Verdict:   changes_requested", "CHANGES_REQUESTED"),
    ],
)
def test_decision_accepts_bounded_reviewer_verdict_variations(
    result: str, expected: str
) -> None:
    decision = runpy.run_path(str(EXAMPLE))["decision"]

    assert decision(result) == expected


@pytest.mark.parametrize(
    "result",
    [
        "APPROVED and CHANGES_REQUESTED",
        "APPROVED\nCHANGES_REQUESTED",
        "Review result:\nAPPROVED\nCHANGES_REQUESTED",
        "Verdict: CHANGES_REQUESTED\n## APPROVED",
        "I initially considered APPROVED,\nbut the final verdict is CHANGES_REQUESTED.",
        "Review result:\nNothing conclusive\nPlease retry",
        "Introduction\nDetails\nMore details\nAPPROVED",
        "NOT APPROVED",
        "This review is APPROVED",
        "Looks approved",
        "APPROVE",
        "No changes requested",
        "UNAPPROVED",
    ],
)
def test_decision_fails_closed_for_ambiguous_or_invalid_results(result: str) -> None:
    decision = runpy.run_path(str(EXAMPLE))["decision"]

    with pytest.raises(WorkerFailure):
        decision(result)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            "CHANGES_REQUESTED\nScope was not APPROVED because coverage is missing.",
            "CHANGES_REQUESTED",
        ),
        (
            "APPROVED\nThe prior CHANGES_REQUESTED findings have been resolved.",
            "APPROVED",
        ),
    ],
)
def test_decision_ignores_verdict_words_in_finding_prose(
    result: str, expected: str
) -> None:
    decision = runpy.run_path(str(EXAMPLE))["decision"]

    assert decision(result) == expected


def test_decision_accepts_repeated_equivalent_standalone_verdicts() -> None:
    decision = runpy.run_path(str(EXAMPLE))["decision"]

    assert decision("APPROVED\nVerdict: APPROVED") == "APPROVED"


def test_whole_version_review_prompt_covers_cross_issue_responsibilities() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    assert "integration consistency across Issues" in source
    assert "duplication between their implementations" in source
    assert "cross-feature interactions" in source
    assert "shared versus feature-specific" in source
    assert "right boundaries" in source


def test_scenario_gate_prompt_selects_and_compares_human_scenarios() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    prompt_globals = workflow["scenario_gate_prompt"].__globals__
    prompt_globals["SCENARIOS"] = (
        "Existing: prompt execution still succeeds.",
        "New: scenario configuration is accepted.",
        "Failure: malformed scenarios are rejected.",
    )
    pr = open_pr(head="dev/v1", base="main", draft=True)
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )

    prompt = workflow["scenario_gate_prompt"](pr, config, config.issues)

    assert pr.base_sha in prompt
    assert pr.head_sha in prompt
    assert "Select a small, risk-relevant subset" in prompt
    assert "executing every scenario is not required" in prompt
    assert "observe or inspect both Before and After" in prompt
    assert "judge whether that difference\nis appropriate" in prompt
    assert "1. Existing:" in prompt
    assert "2. New:" in prompt
    assert "3. Failure:" in prompt


def test_final_review_prompts_include_authoritative_dynamic_plan() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    prompt_globals = workflow["scenario_gate_prompt"].__globals__
    prompt_globals["SCENARIOS"] = ("New: dynamically planned behavior works.",)
    issue_type = workflow["Issue"]
    revised_task = "Publish the revised dynamically planned release notes."
    work_items = (
        issue_type(91, "feature/custom-91"),
        workflow["planner_inline_issue"]("release-notes", revised_task),
    )
    config = workflow["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        work_items,
        "true",
    )
    one_shot_items = (work_items[1],)
    one_shot_config = replace(config, issues=one_shot_items, one_shot_issue=169)
    pr = open_pr(head="dev/v1", base="main", draft=True)

    dynamic_prompts = (
        workflow["scenario_gate_prompt"](pr, config, work_items),
        workflow["whole_version_review_prompt"](pr, config, work_items),
    )

    for prompt in dynamic_prompts:
        assert "GitHub Issue #91, branch feature/custom-91" in prompt
        assert "Mini task release-notes" in prompt
        assert revised_task in prompt

    one_shot_prompts = (
        workflow["scenario_gate_prompt"](pr, one_shot_config, one_shot_items),
        workflow["whole_version_review_prompt"](pr, one_shot_config, one_shot_items),
    )
    for prompt in one_shot_prompts:
        assert revised_task in prompt
        assert "One-shot source: GitHub Issue #169" in prompt


def test_all_review_phases_share_decision_parser() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    assert source.count("def decision(result: str) -> str:") == 1
    assert "decision(result)" not in source
    assert source.count("run_validated_turn(") == 5


def test_machine_output_recovery_corrects_in_the_same_session() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    responses = iter(("Looks approved", "## APPROVED"))
    turns: list[tuple[str, str, str]] = []

    def run_turn(_client, tab, name, prompt, **_kwargs):
        turns.append((tab, name, prompt))
        return next(responses)

    workflow["run_validated_turn"].__globals__["run_turn"] = run_turn

    result, verdict = workflow["run_validated_turn"](
        object(), "reviewer-tab", "Scope review", "Review this.", workflow["decision"]
    )

    assert result == "## APPROVED"
    assert verdict == "APPROVED"
    assert [turn[0] for turn in turns] == ["reviewer-tab", "reviewer-tab"]
    assert turns[1][1] == "Scope review output correction"
    assert "reviewer must provide APPROVED or CHANGES_REQUESTED" in turns[1][2]
    assert "complete corrected response only" in turns[1][2]


def test_machine_output_recovery_fails_only_after_bounded_corrections() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    turns: list[str] = []

    def run_turn(_client, _tab, name, _prompt, **_kwargs):
        turns.append(name)
        return "APPROVE"

    workflow["run_validated_turn"].__globals__["run_turn"] = run_turn

    with pytest.raises(WorkerFailure, match="after 2 correction attempts"):
        workflow["run_validated_turn"](
            object(),
            "reviewer-tab",
            "Scenario Gate",
            "Review this.",
            workflow["decision"],
        )

    assert turns == [
        "Scenario Gate",
        "Scenario Gate output correction",
        "Scenario Gate output correction",
    ]


def test_shared_implementation_principle_is_only_added_to_implementer_prompt() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    principle = workflow["IMPLEMENTATION_PRINCIPLE"]
    issue_type = workflow["Issue"]
    config_type = workflow["Config"]
    config = config_type(
        Path("/tmp/project"),
        "acme/project",
        "dev/v1",
        "main",
        (issue_type(149, "feature/issue-149"),),
        "git diff --check",
    )

    implementation, scope_review, correctness_review = workflow["issue_prompts"](
        config.issues[0], config
    )

    assert principle in implementation
    assert principle not in scope_review
    assert principle not in correctness_review
    assert "reuse" in scope_review.lower()
    assert "responsibilities" in scope_review
    assert "over-generalization" in scope_review
    assert "policy Issue" in scope_review
    assert "functional behavior" in correctness_review
    assert "Do not reopen scope preferences" in correctness_review
    assert "Reuse the existing implementation where appropriate" in principle
    assert "minimum required for this Issue" in principle
    assert "mixing responsibilities unnaturally" in principle
    assert "over-generalizing distinct behavior" in principle


def test_scope_and_correctness_reviews_have_separate_limits_and_results() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    assert "MAX_SCOPE_REVIEWS = 6" in source
    assert "max_reviews=MAX_SCOPE_REVIEWS" in source
    assert "max_reviews=MAX_REVIEWS" in source
    for field in (
        "scope_reviews=",
        "correctness_reviews=",
        "scope_outcome=",
        "correctness_outcome=",
    ):
        assert field in source


def test_every_implementer_turn_uses_shared_implementation_principle() -> None:
    tree = ast.parse(EXAMPLE.read_text(encoding="utf-8"))
    prompts: dict[str, ast.expr] = {}
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not isinstance(call.func, ast.Name) or call.func.id not in {
            "run_turn",
            "run_validated_turn",
        }:
            continue
        name = call.args[2]
        if isinstance(name, ast.Constant):
            label = str(name.value)
        elif isinstance(name, ast.JoinedStr):
            label = "".join(
                str(value.value)
                for value in name.values
                if isinstance(value, ast.Constant)
            )
        else:
            continue
        prompts[label] = call.args[3]

    # Initial implementation, cleanup/remediation, phase fixes, and whole-version
    # fixes are the four prompt-producing implementer paths.
    assert isinstance(prompts[" implementation"], ast.Name)
    for label in ("Clean worktree", "  fixes", "Whole-version fixes"):
        prompt = prompts[label]
        assert isinstance(prompt, ast.Call)
        assert isinstance(prompt.func, ast.Name)
        assert prompt.func.id == "implementer_prompt"
    for label in ("  review", "Whole-version reviewer turn"):
        prompt = prompts[label]
        assert not (
            isinstance(prompt, ast.Call)
            and isinstance(prompt.func, ast.Name)
            and prompt.func.id == "implementer_prompt"
        )


def test_scope_review_fix_is_re_reviewed_with_an_independent_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    review_issue_phase = workflow["review_issue_phase"]
    globals_ = review_issue_phase.__globals__
    issue = workflow["Issue"](150, "feature/issue-150")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    original = open_pr(head=issue.branch, base=config.integration_branch, draft=True)
    fixed_sha = "fixed-head"
    current = original
    agent_results = iter(
        ((original.head_sha, False), (fixed_sha, True), (fixed_sha, False))
    )
    turns: list[str] = []

    class Repository:
        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            assert (branch, expected_local_sha) == (issue.branch, fixed_sha)
            return BranchState(branch, fixed_sha, fixed_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current
            current = replace(current, head_sha=str(kwargs["expected_head_sha"]))
            return current

    results = iter(("CHANGES_REQUESTED\nreduce the scope", "APPROVED"))

    def run_turn(*args: object, **kwargs: object) -> str:
        name = str(args[2])
        turns.append(name)
        return "fixed" if name.endswith("fixes") else next(results)

    monkeypatch.setitem(globals_, "run_turn", run_turn)
    monkeypatch.setitem(
        globals_, "require_agent_result", lambda *args, **kwargs: next(agent_results)
    )
    monkeypatch.setitem(globals_, "emit_finding", lambda *args, **kwargs: None)

    result = review_issue_phase(
        issue,
        config,
        object(),
        Repository(),
        GitHub(),
        "implementer",
        "reviewer",
        original,
        phase="scope/design",
        prompt="scope prompt",
        max_reviews=3,
    )

    assert result.outcome == "approved"
    assert result.reviews == 2
    assert result.head_sha == fixed_sha
    assert turns == [
        "Issue #150 scope/design review",
        "Issue #150 scope/design fixes",
        "Issue #150 scope/design review",
    ]


def test_scope_review_limit_continues_without_faking_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    review_issue_phase = workflow["review_issue_phase"]
    globals_ = review_issue_phase.__globals__
    issue = workflow["Issue"](150, "feature/issue-150")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    pr = open_pr(head=issue.branch, base=config.integration_branch, draft=True)
    findings: list[tuple[str, str, str]] = []

    class Repository:
        def require_pushed(self, branch: str) -> BranchState:
            assert branch == issue.branch
            return BranchState(branch, pr.head_sha, pr.head_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            assert kwargs["expected_head_sha"] == pr.head_sha
            assert kwargs["expected_base_sha"] == pr.base_sha
            return pr

    monkeypatch.setitem(
        globals_,
        "run_turn",
        lambda *args, **kwargs: "CHANGES_REQUESTED\nstill too broad",
    )
    monkeypatch.setitem(
        globals_,
        "require_agent_result",
        lambda *args, **kwargs: (pr.head_sha, False),
    )
    monkeypatch.setitem(
        globals_,
        "emit_finding",
        lambda category, message, status="passed": findings.append(
            (category, message, status)
        ),
    )

    result = review_issue_phase(
        issue,
        config,
        object(),
        Repository(),
        GitHub(),
        "implementer",
        "reviewer",
        pr,
        phase="scope/design",
        prompt="scope prompt",
        max_reviews=1,
    )

    assert result.outcome == "continued_with_warning"
    assert result.reviews == 1
    assert any(
        status == "warning"
        and "CHANGES_REQUESTED" in message
        and "without reviewer approval" in message
        for _, message, status in findings
    )


def test_correctness_fix_restarts_scope_before_final_correctness_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    globals_ = workflow["process_issue"].__globals__
    issue = workflow["Issue"](150, "feature/issue-150")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    head_a = "scope-approved-head"
    head_b = "correctness-fixed-head"
    base_sha = "integration-head"
    current_pr = replace(
        open_pr(head=issue.branch, base=config.integration_branch, draft=True),
        head_sha=head_a,
        base_sha=base_sha,
    )
    events: list[tuple[str, str]] = []
    findings: list[str] = []
    agent_results = iter(
        (
            (head_a, False),
            (head_a, False),
            (head_a, False),
            (head_b, True),
            (head_b, False),
            (head_b, False),
        )
    )

    class Repository:
        local_sha = head_a

        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False)

        def inspect_branch(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, base_sha, base_sha, False)

        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            assert (branch, expected_local_sha) == (issue.branch, head_b)
            self.local_sha = head_b
            return BranchState(branch, head_b, head_b, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            current_pr = replace(current_pr, head_sha=str(kwargs["expected_head_sha"]))
            return current_pr

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            assert number == current_pr.number
            assert kwargs["expected_head_sha"] == head_b
            events.append(("ready", head_b))
            return replace(current_pr, is_draft=False)

    correctness_reviews = 0

    def run_turn(*args: object, **kwargs: object) -> str:
        nonlocal correctness_reviews
        name = str(args[2])
        prompt = str(args[3])
        events.append((name, prompt))
        if name.endswith("implementation"):
            return "implementation already committed"
        if name.endswith("correctness review"):
            correctness_reviews += 1
            return (
                "CHANGES_REQUESTED\nfix correctness"
                if correctness_reviews == 1
                else "APPROVED"
            )
        if name.endswith("scope/design review"):
            return "APPROVED"
        if name.endswith("correctness fixes"):
            return "fixed and committed"
        raise AssertionError(f"unexpected turn {name}")

    monkeypatch.setitem(
        globals_, "prepare_issue", lambda *args: (current_pr, head_a, True)
    )
    monkeypatch.setitem(
        globals_, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(globals_, "run_turn", run_turn)
    monkeypatch.setitem(
        globals_, "require_agent_result", lambda *args, **kwargs: next(agent_results)
    )
    monkeypatch.setitem(globals_, "ensure_issue_pr", lambda *args, **kwargs: current_pr)
    monkeypatch.setitem(
        globals_,
        "emit_finding",
        lambda category, message, **kwargs: findings.append(message),
    )
    monkeypatch.setitem(globals_, "MERGE_TO_INTEGRATION", False)

    result = workflow["process_issue"](
        issue, config, SimpleNamespace(workspace_id="ws-test"), Repository(), GitHub()
    )

    review_events = [name for name, _ in events if name.endswith("review")]
    assert review_events == [
        "Issue #150 scope/design review",
        "Issue #150 correctness review",
        "Issue #150 scope/design review",
        "Issue #150 correctness review",
    ]
    scope_prompts = [
        prompt for name, prompt in events if name.endswith("scope/design review")
    ]
    assert head_a in scope_prompts[0]
    assert head_b in scope_prompts[1]
    assert result.head_sha == head_b
    assert any(
        "scope_reviews=2" in finding
        and "correctness_reviews=2" in finding
        and "scope_outcome=approved" in finding
        and "correctness_outcome=approved" in finding
        for finding in findings
    )


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
            SimpleNamespace(dirty=False, current_branch="feature/issue-116", status=()),
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
    assert "Draft (warning continuation)" in source
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

    workflow["process_issue"](
        issue, config, SimpleNamespace(workspace_id="ws-test"), Repository(), GitHub()
    )

    assert events[0] == "set_draft:True"
    assert events.index("Issue #90 scope/design review") < events.index(
        "Issue #90 correctness review"
    )
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

        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
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

    workflow["process_issue"](
        issue, config, SimpleNamespace(workspace_id="ws-test"), repository, GitHub()
    )

    assert review_count == 3
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
    issue_results: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Repository:
        def __init__(self) -> None:
            self.local_sha = start_sha

        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False, current_branch=issue.branch, status=())

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

        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
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
    monkeypatch.setitem(
        workflow_globals,
        "emit_issue_result",
        lambda *args, **kwargs: issue_results.append((args, kwargs)),
    )

    workflow["process_issue"](
        issue, config, SimpleNamespace(workspace_id="ws-test"), repository, GitHub()
    )

    commit_index = events.index(f"commit:{implementation_sha}")
    push_index = events.index(f"push:{implementation_sha}")
    draft_index = events.index(f"draft:{implementation_sha}")
    verify_index = events.index(f"require_pr:{implementation_sha}")
    assert commit_index < push_index < draft_index < verify_index
    assert events[-1] == "ready"
    assert issue_results[0][1]["workspace_id"] == "ws-test"
    assert issue_results[0][1]["implementation_tab_id"] == (
        f"{issue.label} implementer"
    )
    assert issue_results[0][1]["scope_review_tab_id"] == (
        f"{issue.label} scope reviewer"
    )
    assert issue_results[0][1]["correctness_review_tab_id"] == (
        f"{issue.label} correctness reviewer"
    )


def test_mini_task_adopts_agent_created_draft_pr_with_recovery_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["process_issue"].__globals__
    task = "Refresh the New Run help."
    fingerprint = hashlib.sha256(task.encode()).hexdigest()
    branch = "feature/work-item-refresh-run-help"
    issue = workflow["Issue"](None, branch, "refresh-run-help", task, fingerprint)
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    implementation_sha = "implementation-head"
    base_sha = "integration-head"
    marker = f"<!-- agent-workflow-manager:inline-task-sha256:{fingerprint} -->"
    events: list[str] = []

    class Repository:
        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False)

        def inspect_branch(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, base_sha, base_sha, False)

        def require_current_branch(self, current: str) -> BranchState:
            assert current == branch
            return BranchState(current, implementation_sha, None, True)

        def ensure_pushed(
            self, current: str, *, expected_local_sha: str
        ) -> BranchState:
            assert (current, expected_local_sha) == (branch, implementation_sha)
            return BranchState(current, implementation_sha, implementation_sha, True)

    class GitHub:
        def __init__(self) -> None:
            self.pr: PullRequestState | None = None

        def find_pr(self, *, head: str, base: str, state: str):
            assert (head, base, state) == (branch, config.integration_branch, "OPEN")
            return self.pr

        def require_pr(self, **kwargs: object) -> PullRequestState:
            assert self.pr is not None
            assert kwargs["expected_head_sha"] == implementation_sha
            assert kwargs["expected_base_sha"] == base_sha
            return self.pr

        def update_pr_body(
            self, number: int, *, body: str, **kwargs: object
        ) -> PullRequestState:
            assert self.pr is not None and number == self.pr.number
            assert body == f"{marker}\n\nAgent-created PR body"
            events.append("identity")
            self.pr = replace(self.pr, body=body)
            return self.pr

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            assert self.pr is not None and number == self.pr.number
            events.append("ready")
            self.pr = replace(self.pr, is_draft=False)
            return self.pr

    github = GitHub()
    agent_pr = replace(
        open_pr(head=branch, base=config.integration_branch, draft=True),
        head_sha=implementation_sha,
        base_sha=base_sha,
        body="Agent-created PR body",
    )

    def run_turn(*args: object, **kwargs: object) -> str:
        github.pr = agent_pr
        events.append("agent-created")
        return "implemented, committed, pushed, and opened Draft PR"

    def review_issue_phase(*args: object, **kwargs: object):
        assert github.pr is not None and github.pr.body.startswith(marker)
        return workflow["IssueReviewPhaseResult"](
            github.pr, "approved", implementation_sha, base_sha, 1
        )

    monkeypatch.setitem(
        workflow_globals, "prepare_issue", lambda *args: (None, "start-head", False)
    )
    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: (implementation_sha, True),
    )
    monkeypatch.setitem(workflow_globals, "review_issue_phase", review_issue_phase)
    monkeypatch.setitem(workflow_globals, "MERGE_TO_INTEGRATION", False)

    result = workflow["process_issue"](
        issue, config, SimpleNamespace(workspace_id="ws-test"), Repository(), github
    )

    assert result.body == f"{marker}\n\nAgent-created PR body"
    assert events == ["agent-created", "identity", "ready"]


def test_issue_review_limit_warns_without_starting_an_extra_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["process_issue"].__globals__
    issue = workflow["Issue"](134, "feature/issue-134")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    base_sha = "integration-head"
    initial_sha = "implementation-head"
    fixed_sha = "fixed-head"
    current_pr = replace(
        open_pr(head=issue.branch, base=config.integration_branch, draft=True),
        head_sha=initial_sha,
        base_sha=base_sha,
    )
    events: list[str] = []
    findings: list[tuple[str, str, str]] = []

    class Repository:
        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False)

        def inspect_branch(self, branch: str) -> BranchState:
            return BranchState(branch, base_sha, base_sha, False)

        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            events.append(f"push:{expected_local_sha}")
            return BranchState(branch, expected_local_sha, expected_local_sha, True)

        def require_pushed(self, branch: str) -> BranchState:
            events.append("require_pushed")
            return BranchState(branch, fixed_sha, fixed_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            current_pr = replace(current_pr, head_sha=str(kwargs["expected_head_sha"]))
            return current_pr

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            events.append(f"ready:{kwargs['expected_head_sha']}")
            return replace(current_pr, is_draft=False)

    result_calls = iter(
        (
            (initial_sha, False),
            (initial_sha, False),
            (fixed_sha, True),
            (fixed_sha, False),
            (fixed_sha, False),
        )
    )

    def run_turn(*args: object, **kwargs: object) -> str:
        name = str(args[2])
        events.append(name)
        if "scope/design review" in name:
            return "CHANGES_REQUESTED\nstill needs work"
        if "correctness review" in name:
            return "APPROVED"
        return "done"

    monkeypatch.setitem(
        workflow_globals,
        "prepare_issue",
        lambda *args: (current_pr, initial_sha, True),
    )
    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: next(result_calls),
    )
    monkeypatch.setitem(
        workflow_globals, "ensure_issue_pr", lambda *args, **kwargs: current_pr
    )
    monkeypatch.setitem(workflow_globals, "MAX_REVIEWS", 2)
    monkeypatch.setitem(workflow_globals, "MAX_SCOPE_REVIEWS", 2)
    monkeypatch.setitem(workflow_globals, "MERGE_TO_INTEGRATION", False)
    monkeypatch.setitem(
        workflow_globals,
        "emit_finding",
        lambda category, message, *, status="passed": findings.append(
            (category, status, message)
        ),
    )

    result = workflow["process_issue"](
        issue, config, SimpleNamespace(workspace_id="ws-test"), Repository(), GitHub()
    )

    assert result.is_draft is False
    assert events.count("Issue #134 scope/design review") == 2
    assert events.count("Issue #134 scope/design fixes") == 1
    assert events.count("Issue #134 correctness review") == 1
    assert "require_pushed" in events
    assert events[-1] == f"ready:{fixed_sha}"
    assert any(
        status == "warning"
        and "review limit 2 reached" in message
        and "without reviewer approval" in message
        for _, status, message in findings
    )


def test_policy_conflict_from_fixer_is_persisted_after_pushed_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["process_issue"].__globals__
    issue = workflow["Issue"](137, "feature/issue-137")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true", 200
    )
    base_sha = "integration-head"
    initial_sha = "implementation-head"
    fixed_sha = "fixed-head"
    current_pr = replace(
        open_pr(head=issue.branch, base=config.integration_branch, draft=True),
        head_sha=initial_sha,
        base_sha=base_sha,
    )
    persisted_heads: list[str] = []

    class Repository:
        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False)

        def inspect_branch(self, branch: str) -> BranchState:
            return BranchState(branch, base_sha, base_sha, False)

        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            assert expected_local_sha == fixed_sha
            return BranchState(branch, fixed_sha, fixed_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            current_pr = replace(current_pr, head_sha=str(kwargs["expected_head_sha"]))
            return current_pr

        def update_pr_body(self, number: int, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            assert number == current_pr.number
            assert kwargs["expected_head_sha"] == current_pr.head_sha
            body = str(kwargs["body"])
            if "agent-workflow-manager:policy-conflict:" in body:
                persisted_heads.append(current_pr.head_sha)
            current_pr = replace(current_pr, body=body)
            return current_pr

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            return replace(current_pr, is_draft=False)

    results = iter(
        (
            (initial_sha, False),
            (initial_sha, False),
            (fixed_sha, True),
            (fixed_sha, False),
            (fixed_sha, False),
        )
    )
    review_count = 0

    def run_turn(*args: object, **kwargs: object) -> str:
        nonlocal review_count
        name = str(args[2])
        if name.endswith("review"):
            review_count += 1
            return "CHANGES_REQUESTED\nfix it" if review_count == 1 else "APPROVED"
        if name.endswith("fixes"):
            return "POLICY_CONFLICT: the child requires the legacy API"
        return "implemented"

    monkeypatch.setitem(
        workflow_globals,
        "prepare_issue",
        lambda *args: (current_pr, initial_sha, True),
    )
    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: next(results),
    )
    monkeypatch.setitem(
        workflow_globals, "ensure_issue_pr", lambda *args, **kwargs: current_pr
    )
    monkeypatch.setitem(workflow_globals, "MERGE_TO_INTEGRATION", False)
    monkeypatch.setitem(workflow_globals, "emit_finding", lambda *args, **kwargs: None)

    workflow["process_issue"](
        issue, config, SimpleNamespace(workspace_id="ws-test"), Repository(), GitHub()
    )

    assert persisted_heads
    assert set(persisted_heads) == {fixed_sha}


def test_policy_conflict_from_changed_reviewer_uses_reacquired_child_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["process_issue"].__globals__
    issue = workflow["Issue"](137, "feature/issue-137")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true", 200
    )
    initial_sha = "implementation-head"
    reviewed_sha = "reviewer-head"
    base_sha = "integration-head"
    current_pr = replace(
        open_pr(head=issue.branch, base=config.integration_branch, draft=True),
        head_sha=initial_sha,
        base_sha=base_sha,
    )
    persisted_heads: list[str] = []

    class Repository:
        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False)

        def inspect_branch(self, branch: str) -> BranchState:
            return BranchState(branch, base_sha, base_sha, False)

        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            assert expected_local_sha == reviewed_sha
            return BranchState(branch, reviewed_sha, reviewed_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            current_pr = replace(current_pr, head_sha=str(kwargs["expected_head_sha"]))
            return current_pr

        def update_pr_body(self, number: int, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            assert kwargs["expected_head_sha"] == current_pr.head_sha
            body = str(kwargs["body"])
            if "agent-workflow-manager:policy-conflict:" in body:
                persisted_heads.append(current_pr.head_sha)
            current_pr = replace(current_pr, body=body)
            return current_pr

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            return replace(current_pr, is_draft=False)

    results = iter(
        (
            (initial_sha, False),
            (reviewed_sha, True),
            (reviewed_sha, False),
            (reviewed_sha, False),
        )
    )
    review_count = 0

    def run_turn(*args: object, **kwargs: object) -> str:
        nonlocal review_count
        if str(args[2]).endswith("review"):
            review_count += 1
            if review_count == 1:
                return "APPROVED\nPOLICY_CONFLICT: reviewer found a conflict"
            return "APPROVED"
        return "implemented"

    monkeypatch.setitem(
        workflow_globals,
        "prepare_issue",
        lambda *args: (current_pr, initial_sha, True),
    )
    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: next(results),
    )
    monkeypatch.setitem(
        workflow_globals, "ensure_issue_pr", lambda *args, **kwargs: current_pr
    )
    monkeypatch.setitem(workflow_globals, "MERGE_TO_INTEGRATION", False)
    monkeypatch.setitem(workflow_globals, "emit_finding", lambda *args, **kwargs: None)

    workflow["process_issue"](
        issue, config, SimpleNamespace(workspace_id="ws-test"), Repository(), GitHub()
    )

    assert review_count == 3
    assert persisted_heads
    assert set(persisted_heads) == {reviewed_sha}


def test_warning_delivery_fails_closed_when_exact_head_is_not_pushed() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    draft = open_pr(head="feature/issue-134", base="dev/v1", draft=True)

    class Repository:
        def require_pushed(self, branch: str) -> BranchState:
            return BranchState(branch, "different-head", "different-head", True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            pytest.fail("mismatched Git topology must fail before PR delivery")

    with pytest.raises(WorkerFailure, match="warning delivery head changed"):
        workflow["require_warning_delivery"](
            Repository(),
            GitHub(),
            draft,
            head=draft.head_branch,
            base=draft.base_branch,
            expected_head_sha=draft.head_sha,
            expected_base_sha=draft.base_sha,
        )


@pytest.mark.parametrize(
    ("deferred_state", "error"),
    [
        ({"auto_merge_enabled": True}, "auto-merge enabled"),
        ({"merge_queue_entry": "queue-entry"}, "merge queue entry"),
    ],
)
def test_warning_delivery_rejects_deferred_pr_mutation_state(
    deferred_state: dict[str, object], error: str
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    draft = replace(
        open_pr(head="feature/issue-134", base="dev/v1", draft=True),
        **deferred_state,
    )

    class Repository:
        def require_pushed(self, branch: str) -> BranchState:
            return BranchState(branch, draft.head_sha, draft.head_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            return draft

    with pytest.raises(WorkerFailure, match=error):
        workflow["require_warning_delivery"](
            Repository(),
            GitHub(),
            draft,
            head=draft.head_branch,
            base=draft.base_branch,
            expected_head_sha=draft.head_sha,
            expected_base_sha=draft.base_sha,
        )


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

    result = workflow["integration_delivery"](
        config, config.issues, object(), Repository(), GitHub()
    )

    assert result.is_draft is False
    assert events == [
        "set_draft:True",
        "require_review_head",
        "Whole-version reviewer turn",
        "require_review_head",
        "final checks",
        "set_draft:False",
        "Base PR human handoff",
    ]


def test_policy_conflict_from_changed_whole_reviewer_uses_reacquired_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["review_whole_version"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true", 200
    )
    initial_sha = "integration-head"
    reviewed_sha = "reviewer-head"
    current_pr = replace(
        open_pr(head=config.integration_branch, base=config.main_branch, draft=True),
        head_sha=initial_sha,
        base_sha="main-head",
    )
    persisted_heads: list[str] = []

    class Repository:
        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            assert (branch, expected_local_sha) == (
                config.integration_branch,
                reviewed_sha,
            )
            return BranchState(branch, reviewed_sha, reviewed_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            current_pr = replace(current_pr, head_sha=str(kwargs["expected_head_sha"]))
            return current_pr

        def update_pr_body(self, number: int, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            assert kwargs["expected_head_sha"] == current_pr.head_sha
            body = str(kwargs["body"])
            if "agent-workflow-manager:policy-conflict:" in body:
                persisted_heads.append(current_pr.head_sha)
            current_pr = replace(current_pr, body=body)
            return current_pr

    results = iter(((reviewed_sha, True), (reviewed_sha, False), (reviewed_sha, False)))
    review_count = 0

    def run_turn(*args: object, **kwargs: object) -> str:
        nonlocal review_count
        review_count += 1
        if review_count == 1:
            return "APPROVED\nPOLICY_CONFLICT: whole reviewer found a conflict"
        return "APPROVED"

    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: next(results),
    )
    monkeypatch.setitem(workflow_globals, "run_final_checks", lambda *args: None)
    monkeypatch.setitem(workflow_globals, "emit_finding", lambda *args, **kwargs: None)

    _, delivery = workflow["review_whole_version"](
        config, object(), Repository(), GitHub(), current_pr, config.issues
    )

    assert delivery.outcome == "approved"
    assert review_count == 2
    assert persisted_heads
    assert set(persisted_heads) == {reviewed_sha}


def test_unchanged_whole_version_fixer_warns_and_keeps_base_pr_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["integration_delivery"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    draft = open_pr(head=config.integration_branch, base=config.main_branch, draft=True)
    events: list[str] = []

    class Repository:
        def synchronize_branch(self, branch: str) -> BranchState:
            return BranchState(branch, draft.head_sha, draft.head_sha, True)

        def inspect_branch(self, branch: str) -> BranchState:
            return BranchState(branch, draft.base_sha, draft.base_sha, False)

        def require_pushed(self, branch: str) -> BranchState:
            events.append("require_pushed")
            return BranchState(branch, draft.head_sha, draft.head_sha, True)

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
        config, config.issues, object(), Repository(), GitHub()
    )

    assert result.is_draft is True
    assert events.count("Whole-version reviewer turn") == 1
    assert events.count("Whole-version fixes") == 1
    assert events.count("final checks") == 1
    assert events.index("Whole-version fixes") < events.index("final checks")
    assert events.count("require_pushed") == 2
    assert "ready" not in events


def test_whole_version_review_limit_warns_without_an_extra_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["review_whole_version"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    initial_sha = "integration-head"
    fixed_sha = "fixed-integration-head"
    current_pr = replace(
        open_pr(head=config.integration_branch, base=config.main_branch, draft=True),
        head_sha=initial_sha,
    )
    events: list[str] = []
    findings: list[tuple[str, str, str]] = []

    class Repository:
        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            events.append(f"push:{expected_local_sha}")
            return BranchState(branch, expected_local_sha, expected_local_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            current_pr = replace(current_pr, head_sha=str(kwargs["expected_head_sha"]))
            return current_pr

    result_calls = iter(
        (
            (initial_sha, False),
            (fixed_sha, True),
            (fixed_sha, False),
            (fixed_sha, False),
        )
    )

    def run_turn(*args: object, **kwargs: object) -> str:
        name = str(args[2])
        events.append(name)
        return "CHANGES_REQUESTED\nstill needs work"

    def warning_delivery(*args: object, **kwargs: object) -> PullRequestState:
        events.append(f"safe:{kwargs['expected_head_sha']}")
        assert kwargs["expected_head_sha"] == fixed_sha
        return current_pr

    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: next(result_calls),
    )
    monkeypatch.setitem(
        workflow_globals,
        "run_final_checks",
        lambda *args: events.append("final checks"),
    )
    monkeypatch.setitem(workflow_globals, "require_warning_delivery", warning_delivery)
    monkeypatch.setitem(workflow_globals, "MAX_REVIEWS", 2)
    monkeypatch.setitem(
        workflow_globals,
        "emit_finding",
        lambda category, message, *, status="passed": findings.append(
            (category, status, message)
        ),
    )

    pr, delivery = workflow["review_whole_version"](
        config, object(), Repository(), GitHub(), current_pr, config.issues
    )

    assert pr.is_draft is True
    assert delivery.outcome == "continued_with_warning"
    assert delivery.head_sha == fixed_sha
    assert events.count("Whole-version reviewer turn") == 2
    assert events.count("Whole-version fixes") == 1
    assert events[-2:] == ["final checks", f"safe:{fixed_sha}"]
    assert any(
        status == "warning"
        and "review limit 2 reached" in message
        and "without reviewer approval" in message
        for _, status, message in findings
    )


def test_skipped_final_review_is_ready_without_being_recorded_as_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["integration_delivery"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    draft = open_pr(head=config.integration_branch, base=config.main_branch, draft=True)
    outcomes: list[str] = []
    events: list[str] = []
    review_delivery = workflow["ReviewDelivery"]

    class Repository:
        def synchronize_branch(self, branch: str) -> BranchState:
            return BranchState(branch, draft.head_sha, draft.head_sha, True)

        def inspect_branch(self, branch: str) -> BranchState:
            return BranchState(branch, draft.base_sha, draft.base_sha, False)

        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False)

        def require_committed_result(
            self, branch: str, *, previous_sha: str, allow_unchanged: bool
        ) -> BranchState:
            return BranchState(branch, draft.head_sha, draft.head_sha, True)

    class GitHub:
        def find_pr(
            self, *, head: str, base: str, state: str
        ) -> PullRequestState | None:
            return draft if state == "OPEN" else None

        def require_pr(self, **kwargs: object) -> PullRequestState:
            return draft

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            events.append(f"ready:{kwargs['expected_head_sha']}")
            return replace(draft, is_draft=False)

    def record_delivery(outcome: str, head_sha: str, base_sha: str, reviews: int = 0):
        outcomes.append(outcome)
        return review_delivery(outcome, head_sha, base_sha, reviews)

    monkeypatch.setitem(workflow_globals, "FINAL_REVIEW", False)
    monkeypatch.setitem(workflow_globals, "ReviewDelivery", record_delivery)
    monkeypatch.setitem(
        workflow_globals,
        "run_final_checks",
        lambda *args: events.append("final checks"),
    )
    monkeypatch.setitem(
        workflow_globals,
        "review_whole_version",
        lambda *args: pytest.fail("disabled final review must not run"),
    )

    result = workflow["integration_delivery"](
        config, config.issues, object(), Repository(), GitHub()
    )

    assert result.is_draft is False
    assert outcomes == ["skipped"]
    assert events == ["final checks", f"ready:{draft.head_sha}"]


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
        open_pr(head=config.integration_branch, base=config.main_branch, draft=True),
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

        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
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
    outline_events: list[tuple[str, str]] = []

    def run_turn(*args: object, **kwargs: object) -> str:
        nonlocal review_count
        name = str(args[2])
        events.append(name)
        if name == "Whole-version reviewer turn":
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
    monkeypatch.setitem(
        workflow_globals,
        "emit_step",
        lambda name, status, **kwargs: outline_events.append((name, status)),
    )

    ready = workflow["integration_delivery"](
        config, config.issues, object(), repository, GitHub()
    )

    assert ready.is_draft is False
    assert review_count == 2
    assert check_count == 2
    assert outline_events == [
        ("Whole-version review", "started"),
        ("Whole-version review", "completed"),
        ("Final integration PR", "started"),
        ("Final integration PR", "completed"),
    ]
    assert f"push:{cleanup_sha}" in events
    assert events.index("Clean worktree") < events.index(f"ready:{cleanup_sha}")


def test_whole_version_outline_fails_when_final_checks_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["integration_delivery"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    draft = open_pr(head=config.integration_branch, base=config.main_branch, draft=True)
    outline_events: list[tuple[str, str]] = []

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
            return draft

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            pytest.fail("failed review must not ready the final PR")

    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(
        workflow_globals, "run_turn", lambda *args, **kwargs: "APPROVED"
    )
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: (draft.head_sha, False),
    )
    monkeypatch.setitem(
        workflow_globals,
        "run_final_checks",
        lambda *args: (_ for _ in ()).throw(WorkerFailure("checks failed")),
    )
    monkeypatch.setitem(
        workflow_globals,
        "emit_step",
        lambda name, status, **kwargs: outline_events.append((name, status)),
    )

    with pytest.raises(WorkerFailure, match="checks failed"):
        workflow["integration_delivery"](
            config, config.issues, object(), Repository(), GitHub()
        )

    assert outline_events == [
        ("Whole-version review", "started"),
        ("Whole-version review", "failed"),
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
        integration_delivery(config, config.issues, object(), repository, GitHub())

    assert repository.synchronized == ["dev/v1"]


def test_exact_merged_final_pr_rehydrates_policy_conflict_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["integration_delivery"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true", 200
    )
    warning = (
        "Policy Issue #200 conflicts with the integrated version: ownership "
        "differs; continuing with the implementation work item as the primary requirement."
    )
    marker = workflow["encoded_policy_conflict_marker"](warning)
    merged = replace(merged_final_pr("new-head"), body=f"Base PR.\n\n{marker}")
    findings: list[tuple[str, str, str]] = []
    terminal_events: list[tuple[str, str, str | None]] = []

    class Repository:
        def synchronize_branch(self, branch: str) -> BranchState:
            sha = "new-head" if branch == config.integration_branch else "merge-head"
            return BranchState(branch, sha, sha, True)

        def inspect_branch(self, branch: str) -> BranchState:
            return BranchState(branch, "merge-head", "merge-head", True)

        def require_contains(self, branch: str, commit_sha: str) -> None:
            assert (branch, commit_sha) == (config.main_branch, "new-head")

    class GitHub:
        def find_pr(
            self, *, head: str, base: str, state: str
        ) -> PullRequestState | None:
            return None if state == "OPEN" else merged

        def require_pr(self, **kwargs: object) -> PullRequestState:
            assert kwargs["state"] == "MERGED"
            return merged

    monkeypatch.setitem(
        workflow_globals,
        "emit_finding",
        lambda category, message, status="completed": findings.append(
            (category, message, status)
        ),
    )
    monkeypatch.setitem(
        workflow_globals,
        "terminal_progress",
        lambda event, subject, **kwargs: terminal_events.append(
            (event, subject, kwargs.get("detail"))
        ),
    )

    delivered = workflow["integration_delivery"](
        config, config.issues, object(), Repository(), GitHub()
    )

    assert delivered is merged
    assert ("policy_issue", warning, "warning") in findings
    assert terminal_events == [
        ("PREPARE", "Final integration PR", "dev/v1 -> main"),
        (
            "DONE",
            "Whole-version review",
            "delivery already merged as PR #17",
        ),
        ("DONE", "Final integration PR", "already merged as PR #17"),
    ]


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
        integration_delivery(config, config.issues, object(), Repository(), GitHub())


def test_example_passes_static_validation() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    result = WorkflowValidator(check_timeout=10).validate(source)

    assert result.valid, result.issues
    assert result.dry_run_issues == ()
