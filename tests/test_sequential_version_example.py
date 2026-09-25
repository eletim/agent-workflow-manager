from __future__ import annotations

import ast
import hashlib
import inspect
import json
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
    WorkerInterrupted,
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


def keep_review_audit_in_memory(
    workflow: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Let review-loop unit fakes stay focused on Git and verdict behavior."""
    globals_ = workflow["review_issue_phase"].__globals__  # type: ignore[attr-defined]
    monkeypatch.setitem(
        globals_,
        "persist_review_audit",
        lambda _github, pr, _record, **_kwargs: pr,
    )
    monkeypatch.setitem(
        globals_,
        "review_audit_disposition",
        lambda _github, pr, _audit_id, _disposition, **_kwargs: pr,
    )
    monkeypatch.setitem(
        globals_,
        "review_audit_dispositions",
        lambda _github, pr, _audit_ids, _disposition, **_kwargs: pr,
    )
    monkeypatch.setitem(
        globals_,
        "reconcile_review_audits_after_head_change",
        lambda _github, pr, **_kwargs: pr,
    )
    monkeypatch.setitem(
        globals_,
        "new_review_audit",
        lambda role, round_number, verdict, reviewed_sha, _result: workflow[
            "ReviewAuditRecord"
        ](
            "0" * 32,
            role,
            round_number,
            verdict,
            reviewed_sha,
            (),
            "not_required" if verdict == "APPROVED" else "pending",
        ),
    )
    monkeypatch.setitem(
        globals_,
        "allocate_review_audit",
        lambda _body, role, verdict, reviewed_sha, _result: workflow[
            "ReviewAuditRecord"
        ](
            "0" * 32,
            role,
            1,
            verdict,
            reviewed_sha,
            (),
            "not_required" if verdict == "APPROVED" else "pending",
        ),
    )
    monkeypatch.setitem(
        globals_,
        "decision",
        lambda result: (
            "CHANGES_REQUESTED" if "CHANGES_REQUESTED" in result else "APPROVED"
        ),
    )


def review_result(
    verdict: str = "APPROVED",
    findings: tuple[str, ...] = (),
    policy_conflicts: tuple[str, ...] = (),
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "findings": list(findings),
            "policy_conflicts": list(policy_conflicts),
        }
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


@pytest.mark.parametrize(
    ("configured", "branches", "expected"),
    [
        (
            "dev/v0.4.7",
            {
                "dev/v0.4.7": "a" * 40,
                "dev/v0.4.8": "b" * 40,
                "dev/v0.5.0": "c" * 40,
                "release/v0.4.9": "d" * 40,
            },
            ("dev/v0.4.8",),
        ),
        (
            "dev/v0.4.7",
            {"dev/v0.4.8": "a" * 40, "dev/v0.4.9": "b" * 40},
            ("dev/v0.4.9", "dev/v0.4.8"),
        ),
        ("dev/v0.4.8", {"dev/v0.4.7": "a" * 40}, ()),
        ("dev/v1", {"dev/v1.0.1": "a" * 40}, ()),
    ],
)
def test_newer_development_branches_are_limited_to_the_same_remote_patch_series(
    configured: str, branches: dict[str, str], expected: tuple[str, ...]
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))

    assert workflow["newer_development_branches"](configured, branches) == expected


def test_stale_integration_branch_warning_uses_remote_state_without_rewriting(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v0.4.7", "main", (), "true"
    )
    repo = SimpleNamespace(
        inspect_remote_branch_heads=lambda: {
            "dev/v0.4.7": "a" * 40,
            "dev/v0.4.8": "b" * 40,
            "dev/v0.4.9": "c" * 40,
        }
    )
    comparisons: list[tuple[str, str]] = []
    github = SimpleNamespace(
        compare_commits=lambda *, base_sha, head_sha: (
            comparisons.append((base_sha, head_sha))
            or ("ahead" if head_sha == "b" * 40 else "diverged")
        )
    )
    findings: list[tuple[str, str, str]] = []
    monkeypatch.setitem(
        workflow["warn_if_stale_integration_branch"].__globals__,
        "emit_finding",
        lambda category, message, *, status: findings.append(
            (category, message, status)
        ),
    )

    workflow["warn_if_stale_integration_branch"](config, repo, github)

    assert comparisons == [("a" * 40, "c" * 40), ("a" * 40, "b" * 40)]
    assert len(findings) == 1
    assert findings[0][0::2] == ("git", "warning")
    assert "dev/v0.4.7" in findings[0][1]
    assert "dev/v0.4.8" in findings[0][1]
    assert "will not change it automatically" in findings[0][1]
    assert capsys.readouterr().out == f"WARN: {findings[0][1]}\n"


@pytest.mark.parametrize("comparison", ["identical", "behind", "diverged"])
def test_newer_named_branch_without_forward_progress_does_not_warn(
    comparison: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v0.4.7", "main", (), "true"
    )
    repo = SimpleNamespace(
        inspect_remote_branch_heads=lambda: {
            "dev/v0.4.7": "a" * 40,
            "dev/v0.4.8": "b" * 40,
        }
    )
    github = SimpleNamespace(compare_commits=lambda **_kwargs: comparison)
    monkeypatch.setitem(
        workflow["warn_if_stale_integration_branch"].__globals__,
        "emit_finding",
        lambda *args, **kwargs: pytest.fail("non-ahead branch emitted a warning"),
    )

    workflow["warn_if_stale_integration_branch"](config, repo, github)

    assert capsys.readouterr().out == ""


def test_missing_integration_branch_warns_when_newer_branch_is_ahead_of_base(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    config = workflow["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v0.4.7",
        "main",
        (),
        "true",
        make_integration_branch=True,
    )
    repo = SimpleNamespace(
        inspect_remote_branch_heads=lambda: {
            "main": "a" * 40,
            "dev/v0.4.8": "b" * 40,
        }
    )
    comparisons: list[tuple[str, str]] = []
    github = SimpleNamespace(
        compare_commits=lambda *, base_sha, head_sha: (
            comparisons.append((base_sha, head_sha)) or "ahead"
        )
    )
    findings: list[str] = []
    monkeypatch.setitem(
        workflow["warn_if_stale_integration_branch"].__globals__,
        "emit_finding",
        lambda _category, message, *, status: findings.append(message),
    )

    workflow["warn_if_stale_integration_branch"](config, repo, github)

    assert comparisons == [("a" * 40, "b" * 40)]
    assert len(findings) == 1
    assert "dev/v0.4.7 is absent" in findings[0]
    assert "would be created from main" in findings[0]
    assert capsys.readouterr().out == f"WARN: {findings[0]}\n"


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


def test_run_turn_retains_busy_timeout_warning_for_summary_and_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    globals_ = workflow["run_turn"].__globals__
    findings: list[tuple[str, str, str]] = []

    class Client:
        workspace_id = "ws-1"

        def wait_until_ready(self, tab: str, timeout: int) -> None:
            assert (tab, timeout) == ("tab-1", globals_["READY_TIMEOUT"])

        def send_input(self, tab: str, prompt: str) -> None:
            assert (tab, prompt) == ("tab-1", "work")

        def wait_for_turn_completion(
            self, tab: str, timeout: int, *, on_busy_timeout
        ) -> None:
            assert (tab, timeout) == ("tab-1", globals_["TURN_TIMEOUT"])
            on_busy_timeout("turn timeout warning")

        def read_result(self, tab: str) -> str:
            assert tab == "tab-1"
            return "done"

    monkeypatch.setitem(globals_, "emit_step", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        globals_,
        "emit_finding",
        lambda category, message, *, status: findings.append(
            (category, message, status)
        ),
    )

    assert (
        workflow["run_turn"](
            Client(),
            "tab-1",
            "Issue #240 implementation",
            "work",
            repository_identity="acme/project",
            warning_scope=240,
        )
        == "done"
    )
    contextual_warning = "Issue #240 implementation: turn timeout warning"
    assert findings == [("runtime", contextual_warning, "warning")]
    records = workflow["AGENT_TURN_TIMEOUT_WARNINGS"]
    assert records == [
        workflow["AgentTurnTimeoutWarning"](
            240, "Issue #240 implementation", contextual_warning
        )
    ]
    other_warning = "Issue #241 correctness review: another timeout"
    records.append(
        workflow["AgentTurnTimeoutWarning"](
            241, "Issue #241 correctness review", other_warning
        )
    )

    assert workflow["summary_warnings"](240, ("review warning",)) == (
        "review warning",
        contextual_warning,
    )
    assert workflow["summary_warnings"](241) == (other_warning,)
    assert workflow["summary_warnings"](None) == ()
    delivery = workflow["ReviewDelivery"]("approved", "head", "base", 1)
    assert workflow["human_handoff_warnings"](delivery) == (
        contextual_warning,
        other_warning,
    )


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


@pytest.mark.parametrize("verdict", ["APPROVED", "CHANGES_REQUESTED"])
def test_decision_accepts_exact_structured_review_response(verdict: str) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    findings = () if verdict == "APPROVED" else ("Actionable finding",)

    assert workflow["decision"](review_result(verdict, findings)) == verdict
    assessment = workflow["review_assessment"](
        review_result(verdict, findings, ("Policy mismatch",))
    )
    assert assessment.findings == findings
    assert assessment.policy_conflicts == ("Policy mismatch",)


@pytest.mark.parametrize(
    "result",
    [
        review_result("APPROVED", ("Fix the boundary check.",)),
        review_result("CHANGES_REQUESTED"),
    ],
)
def test_decision_rejects_verdicts_inconsistent_with_findings(result: str) -> None:
    workflow = runpy.run_path(str(EXAMPLE))

    with pytest.raises(WorkerFailure, match="APPROVED reviews must have no findings"):
        workflow["decision"](result)


@pytest.mark.parametrize(
    "finding",
    [
        "Add an authorization check before updating the record.",
        "Do not log credentials or tokens when authentication fails.",
        "Store secrets through the configured credential provider.",
        "Use bearer authentication only after validating the authorization header.",
        "The password was logged without redaction.",
        "Credential validation appeared incomplete.",
    ],
)
def test_review_findings_allow_security_vocabulary_without_values(
    finding: str,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))

    assessment = workflow["review_assessment"](
        review_result("CHANGES_REQUESTED", (finding,))
    )

    assert assessment.findings == (finding,)


@pytest.mark.parametrize(
    "finding",
    [
        "Password hunter2 was printed in the output.",
        "Password hunter was printed in the output.",
        "Token abc123 was logged in the output.",
        "Token abc123 must be redacted.",
        "A leaked password value of hunter2 was printed.",
        "A password value copied from the production response was hunter2.",
        "Remove hunter2 from the password example.",
        "Remove hunter2 from the outdated generated password example.",
        "Observed failure: FAILED tests/test_api.py::test_auth",
    ],
)
def test_review_findings_reject_secret_values_and_embedded_raw_logs(
    finding: str,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))

    with pytest.raises(WorkerFailure, match="without logs or secret-like values"):
        workflow["review_assessment"](review_result("CHANGES_REQUESTED", (finding,)))


@pytest.mark.parametrize(
    "result",
    [
        "APPROVED",
        '{"verdict":"APPROVED","findings":[]}',
        '{"verdict":"APPROVE","findings":[],"policy_conflicts":[]}',
        '{"verdict":"APPROVED","findings":[],"policy_conflicts":[],"extra":1}',
        '{"verdict":"APPROVED","findings":"none","policy_conflicts":[]}',
        review_result("CHANGES_REQUESTED", ("$ printenv",)),
        review_result("CHANGES_REQUESTED", ("token=secret-value",)),
        review_result(
            "CHANGES_REQUESTED",
            ("AWS credential AKIAIOSFODNN7EXAMPLE was printed.",),
        ),
        review_result("CHANGES_REQUESTED", ("The password is hunter2; rotate it.",)),
        review_result(
            "CHANGES_REQUESTED", ("client_secret=abc123 was logged; rotate it.",)
        ),
        review_result(
            "CHANGES_REQUESTED",
            ("FAILED tests/test_api.py::test_auth - AssertionError: expected 401",),
        ),
        review_result(
            "CHANGES_REQUESTED", ("npm ERR! code ERESOLVE while installing",)
        ),
        review_result(
            "CHANGES_REQUESTED",
            ("https://alice:hunter2@example.com was emitted in the error.",),
        ),
        review_result("CHANGES_REQUESTED", ("Credential hunter2 appeared in output.",)),
        review_result(
            "CHANGES_REQUESTED", ("FAILED: test_auth expected 401 but got 200",)
        ),
        review_result("CHANGES_REQUESTED", ("[ERROR] request headers were emitted",)),
        review_result("CHANGES_REQUESTED", ("a\nraw log",)),
        review_result("CHANGES_REQUESTED", ("A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6",)),
        review_result("CHANGES_REQUESTED", ("界" * 100,)),
    ],
)
def test_decision_fails_closed_for_invalid_or_unsafe_results(result: str) -> None:
    decision = runpy.run_path(str(EXAMPLE))["decision"]

    with pytest.raises(WorkerFailure):
        decision(result)


def test_whole_version_review_prompt_covers_cross_issue_responsibilities() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    assert "integration consistency across Issues" in source
    assert "duplication between their implementations" in source
    assert "cross-feature interactions" in source
    assert "shared versus feature-specific" in source
    assert "right boundaries" in source


def test_design_principles_review_prompt_has_an_independent_conformance_scope() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    pr = open_pr(head="dev/v1", base="main", draft=True)
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )

    prompt = workflow["design_principles_review_prompt"](pr, config, config.issues)

    assert pr.base_sha in prompt
    assert pr.head_sha in prompt
    assert f"git show {pr.head_sha}:docs/design-principles.md" in prompt
    assert "solely for conformance" in prompt
    assert "authoritative document" in prompt
    assert "Do not perform Scenario Gate" in prompt
    assert "general whole-version" in prompt
    assert "version, or README review" in prompt


def test_version_readme_review_prompt_has_an_independent_documentation_scope() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    pr = open_pr(head="dev/v1", base="main", draft=True)
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )

    prompt = workflow["version_readme_review_prompt"](pr, config, config.issues)

    assert pr.base_sha in prompt
    assert pr.head_sha in prompt
    assert "version declarations and version references" in prompt
    assert "README documentation agrees with the current behavior" in prompt
    assert "removed features" in prompt
    assert "obsolete CLI or API usage" in prompt
    assert "outdated configuration examples" in prompt
    assert "independent documentation/version review" in prompt


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
        workflow["design_principles_review_prompt"](pr, config, work_items),
        workflow["whole_version_review_prompt"](pr, config, work_items),
        workflow["version_readme_review_prompt"](pr, config, work_items),
    )

    for prompt in dynamic_prompts:
        assert "GitHub Issue #91, branch feature/custom-91" in prompt
        assert "Mini task release-notes" in prompt
        assert revised_task in prompt

    one_shot_prompts = (
        workflow["scenario_gate_prompt"](pr, one_shot_config, one_shot_items),
        workflow["design_principles_review_prompt"](
            pr, one_shot_config, one_shot_items
        ),
        workflow["whole_version_review_prompt"](pr, one_shot_config, one_shot_items),
        workflow["version_readme_review_prompt"](pr, one_shot_config, one_shot_items),
    )
    for prompt in one_shot_prompts:
        assert revised_task in prompt
        assert "One-shot source: GitHub Issue #169" in prompt


def test_all_review_prompts_preserve_the_active_checkout() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    issue = workflow["Issue"](91, "feature/issue-91")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    pr = open_pr(head="dev/v1", base="main", draft=True)
    _, scope_review, correctness_review = workflow["issue_prompts"](issue, config)
    prompts = (
        scope_review,
        correctness_review,
        workflow["scenario_gate_prompt"](pr, config, config.issues),
        workflow["design_principles_review_prompt"](pr, config, config.issues),
        workflow["whole_version_review_prompt"](pr, config, config.issues),
        workflow["version_readme_review_prompt"](pr, config, config.issues),
    )

    guard = workflow["REVIEWER_CHECKOUT_GUARD"]
    assert all(guard in prompt for prompt in prompts)
    audit_guard = workflow["REVIEWER_AUDIT_GUARD"]
    assert all(audit_guard in prompt for prompt in prompts)
    for command in (
        "git checkout",
        "git switch",
        "git restore",
        "gh pr checkout",
        "git rebase",
        "git bisect",
    ):
        assert command in guard


def test_all_review_phases_share_decision_parser() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")

    assert source.count("def decision(result: str) -> str:") == 1
    assert "decision(result)" not in source
    assert source.count("run_validated_turn(") == 8


def test_machine_output_recovery_corrects_in_the_same_session() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    corrected = review_result("APPROVED")
    responses = iter(("Looks approved", corrected))
    turns: list[tuple[str, str, str]] = []

    outcomes: list[str | None] = []

    def execute_turn(_client, tab, name, prompt, **kwargs):
        turns.append((tab, name, prompt))
        return workflow["_AgentTurnExecution"](
            next(responses),
            len(turns),
            name,
            kwargs.get("role", "agent"),
            kwargs.get("iteration") or 1,
            kwargs.get("phase"),
            kwargs.get("work_item_id"),
            kwargs.get("work_item_label"),
            kwargs["repository_identity"],
        )

    workflow["run_validated_turn"].__globals__["run_turn"] = execute_turn
    workflow["run_validated_turn"].__globals__["_emit_completed_turn"] = (
        lambda _execution, outcome: outcomes.append(outcome)
    )

    result, verdict = workflow["run_validated_turn"](
        object(),
        "reviewer-tab",
        "Scope review",
        "Review this.",
        workflow["decision"],
        repository_identity="acme/project",
    )

    assert result == corrected
    assert verdict == "APPROVED"
    assert [turn[0] for turn in turns] == ["reviewer-tab", "reviewer-tab"]
    assert turns[1][1] == "Scope review output correction"
    assert "reviewer response must be one JSON object" in turns[1][2]
    assert "complete corrected response only" in turns[1][2]
    assert outcomes == ["correct_output", None]


def test_validated_transition_traces_the_control_path_decision_once() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    result = review_result("APPROVED")
    validation_calls: list[str] = []
    outcomes: list[str | None] = []

    workflow["run_validated_turn"].__globals__["run_turn"] = (
        lambda _client, _tab, name, _prompt, **kwargs: workflow[
            "_AgentTurnExecution"
        ](
            result,
            1,
            name,
            kwargs.get("role", "agent"),
            1,
            kwargs.get("phase"),
            kwargs.get("work_item_id"),
            kwargs.get("work_item_label"),
            kwargs["repository_identity"],
        )
    )
    workflow["run_validated_turn"].__globals__["_emit_completed_turn"] = (
        lambda _execution, outcome: outcomes.append(outcome)
    )

    def validate(source: str) -> str:
        validation_calls.append(source)
        return workflow["decision"](source)

    assert workflow["run_validated_turn"](
        object(),
        "reviewer-tab",
        "Correctness review",
        "Review this.",
        validate,
        repository_identity="acme/project",
        transition_outcome=lambda verdict: verdict.lower(),
    ) == (result, "APPROVED")
    assert validation_calls == [result]
    assert outcomes == ["approved"]
    assert (
        inspect.signature(workflow["run_turn"])
        .parameters["transition_outcome"]
        .default
        is None
    )


def test_machine_output_recovery_fails_only_after_bounded_corrections() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    turns: list[str] = []

    def execute_turn(_client, _tab, name, _prompt, **kwargs):
        turns.append(name)
        return workflow["_AgentTurnExecution"](
            "APPROVE",
            len(turns),
            name,
            kwargs.get("role", "agent"),
            kwargs.get("iteration") or 1,
            kwargs.get("phase"),
            kwargs.get("work_item_id"),
            kwargs.get("work_item_label"),
            kwargs["repository_identity"],
        )

    workflow["run_validated_turn"].__globals__["run_turn"] = execute_turn

    with pytest.raises(WorkerFailure, match="after 2 correction attempts"):
        workflow["run_validated_turn"](
            object(),
            "reviewer-tab",
            "Scenario Gate",
            "Review this.",
            workflow["decision"],
            repository_identity="acme/project",
        )

    assert turns == [
        "Scenario Gate",
        "Scenario Gate output correction",
        "Scenario Gate output correction",
    ]


def test_review_audit_is_bounded_idempotent_and_records_fix_disposition() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    body = "Delivery context."
    records = []
    for round_number in range(1, 36):
        record = workflow["new_review_audit"](
            "correctness",
            round_number,
            "CHANGES_REQUESTED",
            f"head-{round_number}",
            review_result("CHANGES_REQUESTED", (f"Fix behavior {round_number}",)),
        )
        records.append(record)
        body = workflow["with_review_audit"](
            body,
            record
            if round_number == 35
            else replace(
                record, fix_disposition="fixed", fix_sha=f"head-{round_number + 1}"
            ),
        )

    recovered = workflow["review_audit_from_body"](body)
    assert len(recovered) == workflow["MAX_REVIEW_AUDIT_RECORDS"] == 16
    assert recovered[0].round == 20
    assert recovered[-1].findings == ("Fix behavior 35",)

    unchanged = workflow["with_review_audit"](body, records[-1])
    assert unchanged == body
    updated = replace(records[-1], fix_disposition="fixed", fix_sha="fixed-head")
    revised = workflow["with_review_audit"](body, updated)
    revised_records = workflow["review_audit_from_body"](revised)
    assert len(revised_records) == 16
    assert revised_records[-1].fix_disposition == "fixed"
    assert revised_records[-1].fix_sha == "fixed-head"
    assert "fix: fixed at `fixed-head`" in revised


@pytest.mark.parametrize(
    ("disposition", "fix_sha"),
    [
        ("fixed", "fixed-head"),
        ("no_change_after_re_evaluation", None),
        ("review_limit_reached", None),
        ("reviewer_changed_head", "reviewer-head"),
        ("head_changed_before_disposition", "recovered-head"),
    ],
)
def test_review_audit_replay_cannot_downgrade_terminal_disposition(
    disposition: str, fix_sha: str | None
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    pending = workflow["new_review_audit"](
        "correctness",
        1,
        "CHANGES_REQUESTED",
        "reviewed-head",
        review_result("CHANGES_REQUESTED", ("Handle the failure path.",)),
    )
    completed = replace(pending, fix_disposition=disposition, fix_sha=fix_sha)
    body = workflow["with_review_audit"]("Child PR.", completed)

    assert workflow["with_review_audit"](body, pending) == body
    assert workflow["review_audit_from_body"](body) == (completed,)


def test_review_audit_round_reuses_only_an_exact_pending_record() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    result = review_result("CHANGES_REQUESTED", ("Handle the failure path.",))
    pending = workflow["new_review_audit"](
        "correctness", 3, "CHANGES_REQUESTED", "reviewed-head", result
    )
    pending_body = workflow["with_review_audit"]("Child PR.", pending)

    resumed = workflow["allocate_review_audit"](
        pending_body, "correctness", "CHANGES_REQUESTED", "reviewed-head", result
    )
    completed_body = workflow["with_review_audit"](
        "Child PR.",
        replace(
            pending,
            fix_disposition="no_change_after_re_evaluation",
        ),
    )
    next_run = workflow["allocate_review_audit"](
        completed_body, "correctness", "CHANGES_REQUESTED", "reviewed-head", result
    )

    assert resumed == pending
    assert next_run.round == 4
    assert next_run.audit_id != pending.audit_id


def test_review_audit_byte_eviction_retains_latest_record_for_every_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    globals_ = workflow["with_review_audit"].__globals__
    monkeypatch.setitem(globals_, "MAX_REVIEW_AUDIT_BYTES", 7_000)
    body = "B" * 55_000
    roles = ("scenario_gate", "design_principles", "whole_version", "version_readme")
    findings = tuple(f"Finding {index}: " + "x" * 170 for index in range(3))

    for round_number in (1, 2):
        for role in roles:
            record = workflow["new_review_audit"](
                role,
                round_number,
                "CHANGES_REQUESTED",
                f"head-{round_number}",
                review_result("CHANGES_REQUESTED", findings),
            )
            body = workflow["with_review_audit"](
                body, replace(record, fix_disposition="fixed", fix_sha="fixed-head")
            )

    recovered = workflow["review_audit_from_body"](body)
    assert len(body.encode()) <= workflow["MAX_BASE_PR_BODY_BYTES"]
    assert len(body[body.index(workflow["REVIEW_AUDIT_START"]) :].encode()) <= 7_000
    assert {record.role for record in recovered} == set(roles)
    assert len(recovered) < 8
    assert all(
        max(record.round for record in recovered if record.role == role) == 2
        for role in roles
    )
    with pytest.raises(WorkerFailure, match="reserved 8000-byte review audit budget"):
        workflow["require_base_pr_body_size"](
            "B"
            * (
                workflow["MAX_BASE_PR_BODY_BYTES"]
                - workflow["MIN_REVIEW_AUDIT_RESERVE_BYTES"]
                + 1
            )
        )


def test_review_audit_rejects_raw_logs_and_secret_like_finding_text() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    secret = "github_pat_not-for-github"
    unsafe_results = (
        review_result("CHANGES_REQUESTED", ("$ env",)),
        review_result("CHANGES_REQUESTED", ("2026-09-15 10:00 build log",)),
        review_result("CHANGES_REQUESTED", (f"token={secret}",)),
        review_result("CHANGES_REQUESTED", ("```raw output```",)),
    )

    for result in unsafe_results:
        with pytest.raises(WorkerFailure, match="without logs or secret-like"):
            workflow["new_review_audit"](
                "scope_design", 1, "CHANGES_REQUESTED", "reviewed-head", result
            )


def test_review_audit_persists_on_exact_draft_pr_and_updates_same_record() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    pr = open_pr(head="feature/issue-1", base="dev/v1", draft=True)
    bodies: list[str] = []

    class GitHub:
        def update_pr_body(self, number: int, *, body: str, **kwargs: object):
            nonlocal pr
            assert number == pr.number
            assert kwargs == {
                "expected_head": "feature/issue-1",
                "expected_head_sha": pr.head_sha,
                "expected_base": "dev/v1",
                "expected_base_sha": pr.base_sha,
                "draft": True,
            }
            bodies.append(body)
            pr = replace(pr, body=body)
            return pr

    github = GitHub()
    record = workflow["new_review_audit"](
        "correctness",
        1,
        "CHANGES_REQUESTED",
        pr.head_sha,
        review_result("CHANGES_REQUESTED", ("Add the missing boundary check.",)),
    )
    pr = workflow["persist_review_audit"](
        github, pr, record, head="feature/issue-1", base="dev/v1"
    )
    pr = workflow["review_audit_disposition"](
        github,
        pr,
        record.audit_id,
        "fixed",
        head="feature/issue-1",
        base="dev/v1",
        fix_sha="fixed-head",
    )

    assert len(bodies) == 2
    recovered = workflow["review_audit_from_body"](pr.body)
    assert recovered == (
        replace(record, fix_disposition="fixed", fix_sha="fixed-head"),
    )


def test_later_reviewer_head_change_invalidates_every_earlier_pending_finding() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    pr = open_pr(head="dev/v1", base="main", draft=True)
    scenario = workflow["new_review_audit"](
        "scenario_gate",
        1,
        "CHANGES_REQUESTED",
        pr.head_sha,
        review_result("CHANGES_REQUESTED", ("Correct the scenario behavior.",)),
    )
    principles = workflow["new_review_audit"](
        "design_principles", 1, "APPROVED", pr.head_sha, review_result()
    )
    pr = replace(
        pr,
        head_sha="principles-reviewer-head",
        body=workflow["with_review_audit"](
            workflow["with_review_audit"]("Base PR.", scenario), principles
        ),
    )

    class GitHub:
        def update_pr_body(self, number: int, *, body: str, **kwargs: object):
            nonlocal pr
            assert number == pr.number
            assert kwargs["expected_head_sha"] == "principles-reviewer-head"
            pr = replace(pr, body=body)
            return pr

    pr = workflow["review_audit_dispositions"](
        GitHub(),
        pr,
        (scenario.audit_id, principles.audit_id),
        "reviewer_changed_head",
        head="dev/v1",
        base="main",
        fix_sha=pr.head_sha,
    )

    records = workflow["review_audit_from_body"](pr.body)
    assert [record.fix_disposition for record in records] == [
        "reviewer_changed_head",
        "reviewer_changed_head",
    ]
    assert all(record.fix_sha == "principles-reviewer-head" for record in records)


def test_pending_audits_recover_after_push_before_disposition_update() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    pr = open_pr(head="feature/issue-1", base="dev/v1", draft=True)
    first = workflow["new_review_audit"](
        "scope_design",
        1,
        "CHANGES_REQUESTED",
        "reviewed-head",
        review_result("CHANGES_REQUESTED", ("Narrow the implementation scope.",)),
    )
    second = workflow["new_review_audit"](
        "correctness",
        1,
        "CHANGES_REQUESTED",
        "reviewed-head",
        review_result("CHANGES_REQUESTED", ("Handle the missing failure case.",)),
    )
    body = workflow["with_review_audit"]("Child PR.", first)
    body = workflow["with_review_audit"](body, second)
    body = workflow["with_review_audit"](
        body, replace(first, fix_disposition="fixed", fix_sha="pushed-fix")
    )
    pr = replace(pr, head_sha="pushed-fix", body=body)
    updates = 0

    class GitHub:
        def update_pr_body(self, number: int, *, body: str, **kwargs: object):
            nonlocal pr, updates
            assert number == pr.number
            assert kwargs["expected_head_sha"] == "pushed-fix"
            updates += 1
            pr = replace(pr, body=body)
            return pr

    github = GitHub()
    pr = workflow["reconcile_review_audits_after_head_change"](
        github, pr, head="feature/issue-1", base="dev/v1"
    )
    pr = workflow["reconcile_review_audits_after_head_change"](
        github, pr, head="feature/issue-1", base="dev/v1"
    )

    records = workflow["review_audit_from_body"](pr.body)
    assert updates == 1
    assert records[0].fix_disposition == "fixed"
    assert records[1].fix_disposition == "head_changed_before_disposition"
    assert records[1].fix_sha == "pushed-fix"


def test_review_audit_rejects_ambiguous_managed_markers() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    record = workflow["new_review_audit"](
        "whole_version", 1, "APPROVED", "head", review_result()
    )
    body = workflow["with_review_audit"]("Base PR.", record)

    with pytest.raises(WorkerFailure, match="ambiguous review audit"):
        workflow["with_review_audit"](
            f"{body}\n{workflow['REVIEW_AUDIT_START']}", record
        )


@pytest.mark.parametrize("placement", ["orphaned", "before", "after"])
def test_review_audit_rejects_payload_outside_managed_section(
    placement: str,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    record = workflow["new_review_audit"](
        "whole_version", 1, "APPROVED", "head", review_result()
    )
    marker = (
        f"<!-- {workflow['REVIEW_AUDIT_MARKER']}"
        f"{workflow['_review_audit_payload']((record,))} -->"
    )
    if placement == "orphaned":
        body = f"Base PR.\n\n{marker}"
    elif placement == "before":
        body = (
            f"{marker}\n{workflow['REVIEW_AUDIT_START']}\n"
            f"{workflow['REVIEW_AUDIT_END']}"
        )
    else:
        body = (
            f"{workflow['REVIEW_AUDIT_START']}\n{workflow['REVIEW_AUDIT_END']}\n"
            f"{marker}"
        )

    with pytest.raises(WorkerFailure, match="review audit"):
        workflow["with_review_audit"](body, record)


@pytest.mark.parametrize(
    ("verdict", "findings"),
    [
        ("APPROVED", ("Fix the boundary check.",)),
        ("CHANGES_REQUESTED", ()),
    ],
)
def test_review_audit_recovery_rejects_verdicts_inconsistent_with_findings(
    verdict: str, findings: tuple[str, ...]
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    record_type = workflow["ReviewAuditRecord"]
    record = record_type(
        "0" * 32,
        "whole_version",
        1,
        verdict,
        "head",
        findings,
        "not_required" if verdict == "APPROVED" else "pending",
    )
    marker = (
        f"<!-- {workflow['REVIEW_AUDIT_MARKER']}"
        f"{workflow['_review_audit_payload']((record,))} -->"
    )
    body = (
        f"{workflow['REVIEW_AUDIT_START']}\n### Review audit\n{marker}\n"
        f"{workflow['REVIEW_AUDIT_END']}"
    )

    with pytest.raises(WorkerFailure, match="invalid values"):
        workflow["review_audit_from_body"](body)


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
    for label in (
        "  review",
        "Design Principles reviewer turn",
        "Whole-version reviewer turn",
        "Version / README reviewer turn",
    ):
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

        def update_pr_body(
            self, number: int, *, body: str, **_kwargs: object
        ) -> PullRequestState:
            nonlocal current
            assert number == current.number
            current = replace(current, body=body)
            return current

    results = iter(
        (
            review_result("CHANGES_REQUESTED", ("Reduce the scope.",)),
            review_result(),
        )
    )

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
    audit = workflow["review_audit_from_body"](result.pr.body)
    assert [(record.verdict, record.fix_disposition) for record in audit] == [
        ("CHANGES_REQUESTED", "fixed"),
        ("APPROVED", "not_required"),
    ]
    assert result.head_sha == fixed_sha
    assert turns == [
        "Issue #150 scope/design review",
        "Issue #150 scope/design fixes",
        "Issue #150 scope/design review",
    ]


def test_same_head_no_change_recovery_disposes_prior_pending_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    review_issue_phase = workflow["review_issue_phase"]
    globals_ = review_issue_phase.__globals__
    issue = workflow["Issue"](150, "feature/issue-150")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    current = open_pr(head=issue.branch, base=config.integration_branch, draft=True)
    interrupted = workflow["new_review_audit"](
        "scope_design",
        7,
        "CHANGES_REQUESTED",
        current.head_sha,
        review_result("CHANGES_REQUESTED", ("Remove the stale compatibility path.",)),
    )
    current = replace(
        current, body=workflow["with_review_audit"]("Child PR.", interrupted)
    )

    class Repository:
        def require_pushed(self, branch: str) -> BranchState:
            assert branch == issue.branch
            return BranchState(branch, current.head_sha, current.head_sha, True)

    class GitHub:
        def require_pr(self, **_kwargs: object) -> PullRequestState:
            return current

        def update_pr_body(
            self, number: int, *, body: str, **_kwargs: object
        ) -> PullRequestState:
            nonlocal current
            assert number == current.number
            current = replace(current, body=body)
            return current

    turns = iter(
        (
            review_result("CHANGES_REQUESTED", ("Re-check the compatibility path.",)),
            "No change is warranted after re-evaluation.",
        )
    )
    agent_results = iter(((current.head_sha, False), (current.head_sha, False)))
    monkeypatch.setitem(globals_, "run_turn", lambda *args, **kwargs: next(turns))
    monkeypatch.setitem(
        globals_,
        "require_agent_result",
        lambda *args, **kwargs: next(agent_results),
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
        current,
        phase="scope/design",
        prompt="scope prompt",
        max_reviews=2,
    )

    records = workflow["review_audit_from_body"](result.pr.body)
    assert len(records) == 2
    assert all(
        record.fix_disposition == "no_change_after_re_evaluation" for record in records
    )


def test_later_rerun_can_fix_a_previously_terminal_same_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    review_issue_phase = workflow["review_issue_phase"]
    globals_ = review_issue_phase.__globals__
    issue = workflow["Issue"](150, "feature/issue-150")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    current = open_pr(head=issue.branch, base=config.integration_branch, draft=True)
    finding = "Correct the retry behavior."
    prior = workflow["new_review_audit"](
        "correctness",
        1,
        "CHANGES_REQUESTED",
        current.head_sha,
        review_result("CHANGES_REQUESTED", (finding,)),
    )
    current = replace(
        current,
        body=workflow["with_review_audit"](
            "Child PR.",
            replace(prior, fix_disposition="no_change_after_re_evaluation"),
        ),
    )
    fixed_sha = "fixed-on-later-run"

    class Repository:
        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            assert (branch, expected_local_sha) == (issue.branch, fixed_sha)
            return BranchState(branch, fixed_sha, fixed_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current
            current = replace(current, head_sha=str(kwargs["expected_head_sha"]))
            return current

        def update_pr_body(
            self, number: int, *, body: str, **_kwargs: object
        ) -> PullRequestState:
            nonlocal current
            assert number == current.number
            current = replace(current, body=body)
            return current

    turns = iter(
        (
            review_result("CHANGES_REQUESTED", (finding,)),
            "Fixed the retry behavior.",
        )
    )
    agent_results = iter(((current.head_sha, False), (fixed_sha, True)))
    monkeypatch.setitem(globals_, "run_turn", lambda *args, **kwargs: next(turns))
    monkeypatch.setitem(
        globals_,
        "require_agent_result",
        lambda *args, **kwargs: next(agent_results),
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
        current,
        phase="correctness",
        prompt="correctness prompt",
        max_reviews=4,
        restart_scope_on_change=True,
    )

    records = workflow["review_audit_from_body"](result.pr.body)
    assert result.outcome == "head_changed"
    assert [(record.round, record.fix_disposition) for record in records] == [
        (1, "no_change_after_re_evaluation"),
        (2, "fixed"),
    ]


def test_scope_review_limit_continues_without_faking_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    keep_review_audit_in_memory(workflow, monkeypatch)
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
    keep_review_audit_in_memory(workflow, monkeypatch)
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
        expected_github_slug = "acme/project"

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
        expected_github_slug = "acme/project"

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
        expected_github_slug = "acme/project"

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
    assert source.count("merge_pr_and_advance(") == 4


def test_ready_issue_pr_is_redrafted_and_independently_reviewed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    keep_review_audit_in_memory(workflow, monkeypatch)
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
    keep_review_audit_in_memory(workflow, monkeypatch)
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
        expected_github_slug = "acme/project"

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
            self,
            branch: str,
            *,
            previous_sha: str,
            allow_unchanged: bool,
        ) -> BranchState:
            assert not self.dirty
            return BranchState(branch, self.local_sha, self.local_sha, True)

        def require_agent_commit_provenance(
            self, start: str, end: str, **kwargs: object
        ) -> None:
            assert kwargs["expected_agent"] == "codex"
            assert kwargs["expected_process"] in {"implementation", "cleanup"}

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


@pytest.mark.parametrize(
    ("primary_sha", "allow_unchanged", "expected_process"),
    [
        ("primary", True, "reviewer-fix"),
        ("previous", False, "implementation"),
    ],
)
def test_agent_result_preserves_primary_and_cleanup_turn_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    primary_sha: str,
    allow_unchanged: bool,
    expected_process: str,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    globals_ = workflow["require_agent_result"].__globals__
    branch = "feature/process-boundaries"
    previous_sha = "previous"
    cleanup_sha = "cleanup"
    provenance: list[tuple[str, str, dict[str, object]]] = []

    class Repository:
        expected_github_slug = "acme/project"

        def __init__(self) -> None:
            self.local_sha = primary_sha
            self.dirty = True

        def require_current_branch(self, current: str) -> BranchState:
            assert current == branch
            return BranchState(current, self.local_sha, None, True)

        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(
                dirty=self.dirty,
                current_branch=branch,
                status=(" M pending.py",) if self.dirty else (),
            )

        def require_committed_result(
            self, current: str, *, previous_sha: str, allow_unchanged: bool
        ) -> BranchState:
            assert (current, previous_sha, allow_unchanged) == (
                branch,
                "previous",
                True,
            )
            return BranchState(current, self.local_sha, None, True)

        def require_agent_commit_provenance(
            self, start: str, end: str, **kwargs: object
        ) -> None:
            provenance.append((start, end, kwargs))

    repository = Repository()

    def clean(*args: object, **kwargs: object) -> str:
        repository.local_sha = cleanup_sha
        repository.dirty = False
        return "cleaned"

    monkeypatch.setitem(globals_, "run_turn", clean)
    monkeypatch.setitem(globals_, "emit_finding", lambda *args, **kwargs: None)

    assert workflow["require_agent_result"](
        repository,
        object(),
        "tab",
        branch,
        previous_sha,
        allow_unchanged=allow_unchanged,
        expected_process=expected_process,
    ) == (cleanup_sha, True)
    assert provenance == [
        (
            previous_sha,
            primary_sha,
            {
                "expected_agent": "codex",
                "expected_process": expected_process,
                "allow_unchanged": True,
            },
        ),
        (
            primary_sha,
            cleanup_sha,
            {"expected_agent": "codex", "expected_process": "cleanup"},
        ),
    ]


def test_failed_mutating_turn_validates_commits_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    branch = "feature/failed-turn"
    provenance: list[tuple[str, str, dict[str, object]]] = []

    class Repository:
        local_sha = "before"

        def require_current_branch(self, current: str) -> BranchState:
            assert current == branch
            return BranchState(current, self.local_sha, None, True)

        def require_agent_commit_provenance(
            self, start: str, end: str, **kwargs: object
        ) -> None:
            provenance.append((start, end, kwargs))

    repository = Repository()

    class Client:
        workspace_id = "ws-test"

        def wait_until_ready(self, tab: str, timeout: int) -> None:
            pass

        def send_input(self, tab: str, prompt: str) -> None:
            pass

        def wait_for_turn_completion(self, *args: object, **kwargs: object) -> None:
            repository.local_sha = "failed-turn-commit"
            raise WorkerFailure("turn failed")

    globals_ = workflow["run_turn"].__globals__
    monkeypatch.setitem(globals_, "emit_step", lambda *args, **kwargs: None)
    monkeypatch.setitem(globals_, "terminal_progress", lambda *args, **kwargs: None)

    with pytest.raises(WorkerFailure, match="turn failed"):
        workflow["run_turn"](
            Client(),
            "tab",
            "Implementation",
            "prompt",
            repository_identity="acme/project",
            repository=repository,
            branch=branch,
            expected_process="implementation",
        )
    assert provenance == [
        (
            "before",
            "failed-turn-commit",
            {
                "expected_agent": "codex",
                "expected_process": "implementation",
                "allow_unchanged": True,
            },
        )
    ]


def test_interrupted_turn_is_not_masked_by_provenance_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    branch = "feature/interrupted-turn"

    class Repository:
        def require_current_branch(self, current: str) -> BranchState:
            return BranchState(current, "head", None, True)

        def require_agent_commit_provenance(
            self, *args: object, **kwargs: object
        ) -> None:
            raise WorkerFailure("invalid provenance")

    class Client:
        workspace_id = "ws-test"

        def wait_until_ready(self, tab: str, timeout: int) -> None:
            raise WorkerInterrupted("turn interrupted")

    globals_ = workflow["run_turn"].__globals__
    monkeypatch.setitem(globals_, "emit_step", lambda *args, **kwargs: None)
    monkeypatch.setitem(globals_, "terminal_progress", lambda *args, **kwargs: None)

    with pytest.raises(WorkerInterrupted, match="turn interrupted"):
        workflow["run_turn"](
            Client(),
            "tab",
            "Implementation",
            "prompt",
            repository_identity="acme/project",
            repository=Repository(),
            branch=branch,
            expected_process="implementation",
        )


def test_normal_issue_path_commits_pushes_and_creates_exact_draft_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    keep_review_audit_in_memory(workflow, monkeypatch)
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
        expected_github_slug = "acme/project"

        def __init__(self) -> None:
            self.local_sha = start_sha

        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False, current_branch=issue.branch, status=())

        def require_current_branch(self, branch: str) -> BranchState:
            assert branch == issue.branch
            return BranchState(branch, self.local_sha, None, True)

        def require_committed_result(
            self,
            branch: str,
            *,
            previous_sha: str,
            allow_unchanged: bool,
        ) -> BranchState:
            events.append(f"commit:{self.local_sha}")
            assert branch == issue.branch
            assert self.local_sha != previous_sha or allow_unchanged
            return BranchState(branch, self.local_sha, None, True)

        def require_agent_commit_provenance(
            self, start: str, end: str, **kwargs: object
        ) -> None:
            assert kwargs["expected_agent"] == "codex"
            assert kwargs["expected_process"] in {"implementation", "cleanup"}

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


@pytest.mark.parametrize(
    "failure_phase", ["after_pr_creation", "scope/design", "correctness"]
)
def test_issue_navigation_precedes_post_pr_and_review_failures(
    monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    workflow_globals = workflow["process_issue"].__globals__
    issue = workflow["Issue"](178, "feature/issue-178")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    head_sha = "implementation-head"
    base_sha = "integration-head"
    pr = replace(
        open_pr(head=issue.branch, base=config.integration_branch, draft=True),
        head_sha=head_sha,
        base_sha=base_sha,
    )
    navigations: list[tuple[tuple[object, ...], dict[str, object]]] = []
    issue_results: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Repository:
        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False)

        def inspect_branch(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, base_sha, base_sha, False)

    def ensure_metadata(*args: object, **kwargs: object) -> PullRequestState:
        if failure_phase == "after_pr_creation":
            raise RuntimeError("metadata failure")
        return pr

    def review_phase(*args: object, **kwargs: object) -> object:
        phase = str(kwargs["phase"])
        if phase == failure_phase:
            raise RuntimeError(f"{phase} failure")
        assert phase == "scope/design"
        return workflow["IssueReviewPhaseResult"](
            pr, "approved", head_sha, base_sha, 1, ()
        )

    monkeypatch.setitem(
        workflow_globals, "prepare_issue", lambda *args: (None, "start-head", False)
    )
    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", lambda *args, **kwargs: "done")
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: (head_sha, False),
    )
    monkeypatch.setitem(workflow_globals, "ensure_issue_pr", lambda *args, **kwargs: pr)
    monkeypatch.setitem(workflow_globals, "ensure_issue_pr_metadata", ensure_metadata)
    monkeypatch.setitem(workflow_globals, "review_issue_phase", review_phase)
    monkeypatch.setitem(
        workflow_globals,
        "emit_issue_navigation",
        lambda *args, **kwargs: navigations.append((args, kwargs)),
    )
    monkeypatch.setitem(
        workflow_globals,
        "emit_issue_result",
        lambda *args, **kwargs: issue_results.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="failure"):
        workflow["process_issue"](
            issue,
            config,
            SimpleNamespace(workspace_id="ws-test"),
            Repository(),
            object(),
        )

    assert issue_results == []
    assert navigations == [
        (
            (issue.result_id, pr.number, pr.url),
            {
                "label": issue.label,
                "workspace_id": "ws-test",
                "implementation_tab_id": f"{issue.label} implementer",
                "scope_review_tab_id": f"{issue.label} scope reviewer",
                "correctness_review_tab_id": f"{issue.label} correctness reviewer",
            },
        )
    ]


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

    def reconcile(**kwargs: object) -> SimpleNamespace:
        assert kwargs == {
            "repo": str(config.repo),
            "integration_branch": config.integration_branch,
            "issue": (issue.label, issue.branch, issue.task_fingerprint),
            "command_timeout_seconds": workflow["COMMAND_TIMEOUT"],
        }
        assert github.pr is not None
        github.update_pr_body(
            github.pr.number,
            body=f"{marker}\n\nAgent-created PR body",
            expected_head=branch,
            expected_head_sha=implementation_sha,
            expected_base=config.integration_branch,
            expected_base_sha=base_sha,
            draft=True,
        )
        return SimpleNamespace(
            classification="recoverable",
            feature_sha=implementation_sha,
            integration_sha=base_sha,
            open_pr_number=agent_pr.number,
        )

    monkeypatch.setitem(
        workflow_globals, "prepare_issue", lambda *args: (None, "start-head", False)
    )
    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals, "recover_issue_driven_work_item_topology", reconcile
    )
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


def test_one_shot_child_pr_creation_and_body_update_preserve_fingerprint() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    task = "Refresh the New Run help."
    fingerprint = hashlib.sha256(task.encode()).hexdigest()
    branch = "feature/work-item-refresh-run-help"
    issue = workflow["Issue"](None, branch, "refresh-run-help", task, fingerprint)
    config = workflow["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        (issue,),
        "true",
        one_shot_issue=204,
    )
    head_sha = "implementation-head"
    base_sha = "integration-head"
    marker = f"<!-- agent-workflow-manager:inline-task-sha256:{fingerprint} -->"
    bodies: list[str] = []

    class Repository:
        def require_current_branch(self, current: str) -> BranchState:
            assert current == branch
            return BranchState(current, head_sha, None, True)

        def ensure_pushed(
            self, current: str, *, expected_local_sha: str
        ) -> BranchState:
            assert (current, expected_local_sha) == (branch, head_sha)
            return BranchState(current, head_sha, head_sha, True)

    class GitHub:
        def __init__(self) -> None:
            self.pr: PullRequestState | None = None

        def find_pr(self, *, head: str, base: str, state: str):
            assert (head, base, state) == (branch, config.integration_branch, "OPEN")
            return self.pr

        def create_draft_pr(self, **kwargs: object) -> PullRequestState:
            body = str(kwargs["body"])
            bodies.append(body)
            self.pr = replace(
                open_pr(head=branch, base=config.integration_branch, draft=True),
                head_sha=head_sha,
                base_sha=base_sha,
                body=body,
            )
            return self.pr

        def require_pr(self, **kwargs: object) -> PullRequestState:
            assert self.pr is not None
            return self.pr

        def update_pr_body(
            self, number: int, *, body: str, **kwargs: object
        ) -> PullRequestState:
            assert self.pr is not None and number == self.pr.number
            bodies.append(body)
            self.pr = replace(self.pr, body=body)
            return self.pr

    github = GitHub()
    created = workflow["ensure_issue_pr"](
        Repository(), github, issue, config, expected_base_sha=base_sha
    )
    assert created.body.startswith(f"{marker}\n\n")

    rewritten = replace(created, body="Updated child PR description")
    github.pr = rewritten
    updated = workflow["ensure_issue_pr_metadata"](github, rewritten, issue, config)

    assert updated.body == f"{marker}\n\nUpdated child PR description"
    assert bodies == [issue.pr_body, updated.body]


@pytest.mark.parametrize(
    ("update_stage", "foreign_marker"),
    [("review", False), ("fix", True)],
    ids=["duplicate-during-review-update", "foreign-during-fix-update"],
)
def test_inline_review_updates_reject_ambiguous_pr_identity(
    monkeypatch: pytest.MonkeyPatch,
    update_stage: str,
    foreign_marker: bool,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    keep_review_audit_in_memory(workflow, monkeypatch)
    workflow_globals = workflow["review_issue_phase"].__globals__
    task = "Refresh the New Run help."
    fingerprint = hashlib.sha256(task.encode()).hexdigest()
    issue = workflow["Issue"](
        None,
        "feature/work-item-refresh-run-help",
        "refresh-run-help",
        task,
        fingerprint,
    )
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    initial_sha = "implementation-head"
    updated_sha = f"{update_stage}-head"
    base_sha = "integration-head"
    marker = f"<!-- agent-workflow-manager:inline-task-sha256:{fingerprint} -->"
    other = "b" * 64 if foreign_marker else fingerprint
    added_marker = f"<!-- agent-workflow-manager:inline-task-sha256:{other} -->"
    current_pr = replace(
        open_pr(head=issue.branch, base=config.integration_branch, draft=True),
        head_sha=initial_sha,
        base_sha=base_sha,
        body=f"{marker}\n\nImplementation summary",
    )

    class Repository:
        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            assert (branch, expected_local_sha) == (issue.branch, updated_sha)
            return BranchState(branch, updated_sha, updated_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            if kwargs["expected_head_sha"] == initial_sha:
                return current_pr
            return replace(
                current_pr,
                head_sha=updated_sha,
                body=f"{current_pr.body}\n\n{added_marker}",
            )

        def update_pr_body(self, *args: object, **kwargs: object) -> PullRequestState:
            pytest.fail("ambiguous identity must fail before a PR body mutation")

    agent_results = (
        iter(((updated_sha, True),))
        if update_stage == "review"
        else iter(((initial_sha, False), (updated_sha, True)))
    )
    monkeypatch.setitem(
        workflow_globals,
        "run_validated_turn",
        lambda *args, **kwargs: (
            ("APPROVED", "APPROVED")
            if update_stage == "review"
            else ("CHANGES_REQUESTED\nfix it", "CHANGES_REQUESTED")
        ),
    )
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: next(agent_results),
    )
    monkeypatch.setitem(workflow_globals, "run_turn", lambda *args, **kwargs: "fixed")

    with pytest.raises(WorkerFailure, match="inline task fingerprint"):
        workflow["review_issue_phase"](
            issue,
            config,
            object(),
            Repository(),
            GitHub(),
            "implementer",
            "reviewer",
            current_pr,
            phase="scope/design",
            prompt="review",
            max_reviews=2,
        )


def test_regular_issue_pr_body_metadata_maintenance_is_a_noop() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    issue = workflow["Issue"](204, "feature/issue-204")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    pr = open_pr(head=issue.branch, base=config.integration_branch, draft=True)

    class GitHub:
        def update_pr_body(self, *args: object, **kwargs: object) -> PullRequestState:
            raise AssertionError("regular Issue PR body must not be updated")

    assert workflow["ensure_issue_pr_metadata"](GitHub(), pr, issue, config) is pr


def test_issue_review_limit_warns_without_starting_an_extra_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    keep_review_audit_in_memory(workflow, monkeypatch)
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
    keep_review_audit_in_memory(workflow, monkeypatch)
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
    keep_review_audit_in_memory(workflow, monkeypatch)
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
    keep_review_audit_in_memory(workflow, monkeypatch)
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
        "Design Principles reviewer turn",
        "Whole-version reviewer turn",
        "Version / README reviewer turn",
        "require_review_head",
        "final checks",
        "set_draft:False",
        "Base PR human handoff",
    ]


def test_policy_conflict_from_changed_whole_reviewer_uses_reacquired_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    keep_review_audit_in_memory(workflow, monkeypatch)
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

    results = iter(
        (
            (initial_sha, False),
            (reviewed_sha, True),
            (reviewed_sha, False),
            (reviewed_sha, False),
            (reviewed_sha, False),
            (reviewed_sha, False),
        )
    )
    whole_review_count = 0

    def run_turn(*args: object, **kwargs: object) -> str:
        nonlocal whole_review_count
        if str(args[2]) == "Whole-version reviewer turn":
            whole_review_count += 1
        if whole_review_count == 1 and str(args[2]) == "Whole-version reviewer turn":
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
    assert whole_review_count == 2
    assert persisted_heads
    assert set(persisted_heads) == {reviewed_sha}


def test_changed_design_principles_reviewer_invalidates_and_repeats_whole_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    keep_review_audit_in_memory(workflow, monkeypatch)
    workflow_globals = workflow["review_whole_version"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    initial_sha = "integration-head"
    changed_sha = "principles-reviewer-head"
    current_pr = replace(
        open_pr(head=config.integration_branch, base=config.main_branch, draft=True),
        head_sha=initial_sha,
    )
    events: list[str] = []

    class Repository:
        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            events.append(f"push:{expected_local_sha}")
            return BranchState(branch, expected_local_sha, expected_local_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            current_pr = replace(current_pr, head_sha=str(kwargs["expected_head_sha"]))
            return current_pr

    results = iter(
        (
            (changed_sha, True),
            (changed_sha, False),
            (changed_sha, False),
            (changed_sha, False),
            (changed_sha, False),
        )
    )
    prompts: list[str] = []

    def run_turn(*args: object, **kwargs: object) -> str:
        events.append(str(args[2]))
        prompts.append(str(args[3]))
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
    assert delivery.head_sha == changed_sha
    assert events == [
        "Design Principles reviewer turn",
        f"push:{changed_sha}",
        "Design Principles reviewer turn",
        "Whole-version reviewer turn",
        "Version / README reviewer turn",
    ]
    assert initial_sha in prompts[0]
    assert changed_sha in prompts[1]


def test_changed_later_reviewer_disposes_earlier_role_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    make_audit = workflow["new_review_audit"]
    keep_review_audit_in_memory(workflow, monkeypatch)
    workflow_globals = workflow["review_whole_version"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    initial_sha = "integration-head"
    changed_sha = "principles-reviewer-head"
    current_pr = replace(
        open_pr(head=config.integration_branch, base=config.main_branch, draft=True),
        head_sha=initial_sha,
    )
    audit_roles: dict[str, str] = {}
    invalidated_roles: list[tuple[str, ...]] = []

    def tracked_audit(
        _body: str, role: str, verdict: str, reviewed_sha: str, result: str
    ):
        record = make_audit(role, 1, verdict, reviewed_sha, result)
        audit_roles[record.audit_id] = record.role
        return record

    def dispositions(
        _github, pr, audit_ids: tuple[str, ...], disposition: str, **_kwargs
    ):
        assert disposition == "reviewer_changed_head"
        invalidated_roles.append(tuple(audit_roles[audit_id] for audit_id in audit_ids))
        return pr

    class Repository:
        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            return BranchState(branch, expected_local_sha, expected_local_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            current_pr = replace(current_pr, head_sha=str(kwargs["expected_head_sha"]))
            return current_pr

    scenario_reviews = 0

    def run_turn(*args: object, **_kwargs: object) -> str:
        nonlocal scenario_reviews
        if str(args[2]) == "Scenario Gate reviewer turn":
            scenario_reviews += 1
            if scenario_reviews == 1:
                return review_result(
                    "CHANGES_REQUESTED", ("Correct the scenario behavior.",)
                )
        return review_result()

    agent_results = iter(
        (
            (initial_sha, False),
            (changed_sha, True),
            (changed_sha, False),
            (changed_sha, False),
            (changed_sha, False),
            (changed_sha, False),
            (changed_sha, False),
        )
    )
    monkeypatch.setitem(workflow_globals, "SCENARIOS", ("scenario",))
    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "allocate_review_audit", tracked_audit)
    monkeypatch.setitem(workflow_globals, "review_audit_dispositions", dispositions)
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: next(agent_results),
    )
    monkeypatch.setitem(workflow_globals, "run_final_checks", lambda *args: None)
    monkeypatch.setitem(workflow_globals, "emit_finding", lambda *args, **kwargs: None)

    _, delivery = workflow["review_whole_version"](
        config, object(), Repository(), GitHub(), current_pr, config.issues
    )

    assert delivery.outcome == "approved"
    assert invalidated_roles == [("scenario_gate", "design_principles")]


def test_same_head_partial_review_limit_recovery_finishes_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    apply_dispositions = workflow["review_audit_dispositions"]
    workflow_globals = workflow["review_whole_version"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    current = replace(
        open_pr(head=config.integration_branch, base=config.main_branch, draft=True),
        head_sha="integration-head",
    )
    completed = workflow["new_review_audit"](
        "whole_version",
        6,
        "CHANGES_REQUESTED",
        current.head_sha,
        review_result("CHANGES_REQUESTED", ("Correct the first integration issue.",)),
    )
    interrupted = workflow["new_review_audit"](
        "version_readme",
        6,
        "CHANGES_REQUESTED",
        current.head_sha,
        review_result("CHANGES_REQUESTED", ("Correct the release documentation.",)),
    )
    body = workflow["with_review_audit"](
        "Base PR.", replace(completed, fix_disposition="review_limit_reached")
    )
    current = replace(current, body=workflow["with_review_audit"](body, interrupted))
    keep_review_audit_in_memory(workflow, monkeypatch)
    updates = 0

    class Repository:
        def require_pushed(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, current.head_sha, current.head_sha, True)

    class GitHub:
        def require_pr(self, **_kwargs: object) -> PullRequestState:
            return current

        def update_pr_body(
            self, number: int, *, body: str, **_kwargs: object
        ) -> PullRequestState:
            nonlocal current, updates
            assert number == current.number
            updates += 1
            current = replace(current, body=body)
            return current

    agent_results = iter((current.head_sha, False) for _ in range(4))
    monkeypatch.setitem(workflow_globals, "MAX_REVIEWS", 1)
    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(
        workflow_globals, "review_audit_dispositions", apply_dispositions
    )
    monkeypatch.setitem(
        workflow_globals, "run_turn", lambda *args, **kwargs: "APPROVED"
    )
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: next(agent_results),
    )
    monkeypatch.setitem(workflow_globals, "run_final_checks", lambda *args: None)
    monkeypatch.setitem(workflow_globals, "emit_finding", lambda *args, **kwargs: None)

    pr, delivery = workflow["review_whole_version"](
        config, object(), Repository(), GitHub(), current, config.issues
    )

    records = workflow["review_audit_from_body"](pr.body)
    assert delivery.outcome == "continued_with_warning"
    assert updates == 1
    assert [record.fix_disposition for record in records] == [
        "review_limit_reached",
        "review_limit_reached",
    ]


def test_unchanged_whole_version_fixer_warns_and_keeps_base_pr_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    keep_review_audit_in_memory(workflow, monkeypatch)
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


def test_whole_failures_do_not_consume_version_readme_review_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    keep_review_audit_in_memory(workflow, monkeypatch)
    workflow_globals = workflow["review_whole_version"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    initial_sha = "integration-head"
    fixed_sha = "documented-head"
    current_pr = replace(
        open_pr(head=config.integration_branch, base=config.main_branch, draft=True),
        head_sha=initial_sha,
    )
    events: list[str] = []

    class Repository:
        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            assert (branch, expected_local_sha) == (
                config.integration_branch,
                fixed_sha,
            )
            return BranchState(branch, fixed_sha, fixed_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            nonlocal current_pr
            current_pr = replace(current_pr, head_sha=str(kwargs["expected_head_sha"]))
            return current_pr

    agent_results = iter(
        (
            (initial_sha, False),
            (initial_sha, False),
            (initial_sha, False),
            (fixed_sha, True),
            (fixed_sha, False),
            (fixed_sha, False),
            (fixed_sha, False),
            (fixed_sha, False),
        )
    )
    version_reviews = 0
    whole_reviews = 0
    fix_prompts: list[str] = []

    def run_turn(*args: object, **kwargs: object) -> str:
        nonlocal version_reviews, whole_reviews
        name = str(args[2])
        events.append(name)
        if name == "Whole-version reviewer turn":
            whole_reviews += 1
            if whole_reviews == 1:
                return "CHANGES_REQUESTED\nRepair the cross-Issue integration."
        if name == "Version / README reviewer turn":
            version_reviews += 1
            if version_reviews == 1:
                return "CHANGES_REQUESTED\nRemove the obsolete CLI example."
        if name == "Whole-version fixes":
            fix_prompts.append(str(args[3]))
        return "APPROVED"

    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: next(agent_results),
    )
    monkeypatch.setitem(workflow_globals, "run_final_checks", lambda *args: None)
    monkeypatch.setitem(workflow_globals, "emit_finding", lambda *args, **kwargs: None)

    _, delivery = workflow["review_whole_version"](
        config, object(), Repository(), GitHub(), current_pr, config.issues
    )

    assert delivery.outcome == "approved"
    assert delivery.reviews == 2
    assert delivery.head_sha == fixed_sha
    assert len(fix_prompts) == 1
    assert "Repair the cross-Issue integration." in fix_prompts[0]
    assert "Remove the obsolete CLI example." in fix_prompts[0]
    assert events == [
        "Design Principles reviewer turn",
        "Whole-version reviewer turn",
        "Version / README reviewer turn",
        "Whole-version fixes",
        "Design Principles reviewer turn",
        "Whole-version reviewer turn",
        "Version / README reviewer turn",
    ]


def test_scenario_gate_failure_does_not_skip_independent_reviews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    keep_review_audit_in_memory(workflow, monkeypatch)
    workflow_globals = workflow["review_whole_version"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    current_pr = replace(
        open_pr(head=config.integration_branch, base=config.main_branch, draft=True),
        head_sha="integration-head",
    )
    events: list[str] = []

    class Repository:
        def require_pushed(self, branch: str) -> BranchState:
            return BranchState(branch, current_pr.head_sha, current_pr.head_sha, True)

    class GitHub:
        def require_pr(self, **_kwargs: object) -> PullRequestState:
            return current_pr

    def run_turn(*args: object, **_kwargs: object) -> str:
        name = str(args[2])
        events.append(name)
        if name == "Scenario Gate reviewer turn":
            return "CHANGES_REQUESTED\nCorrect the scenario behavior."
        return "APPROVED"

    monkeypatch.setitem(workflow_globals, "SCENARIOS", ("scenario",))
    monkeypatch.setitem(workflow_globals, "MAX_REVIEWS", 1)
    monkeypatch.setitem(
        workflow_globals, "create_agent", lambda *args, **kwargs: kwargs["name"]
    )
    monkeypatch.setitem(workflow_globals, "run_turn", run_turn)
    monkeypatch.setitem(
        workflow_globals,
        "require_agent_result",
        lambda *args, **kwargs: (current_pr.head_sha, False),
    )
    monkeypatch.setitem(workflow_globals, "run_final_checks", lambda *args: None)
    monkeypatch.setitem(workflow_globals, "emit_finding", lambda *args, **kwargs: None)

    _, delivery = workflow["review_whole_version"](
        config, object(), Repository(), GitHub(), current_pr, config.issues
    )

    assert delivery.outcome == "continued_with_warning"
    assert events == [
        "Scenario Gate reviewer turn",
        "Design Principles reviewer turn",
        "Whole-version reviewer turn",
        "Version / README reviewer turn",
    ]


def test_whole_version_review_limit_warns_without_an_extra_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    keep_review_audit_in_memory(workflow, monkeypatch)
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
            (initial_sha, False),
            (initial_sha, False),
            (fixed_sha, True),
            (fixed_sha, False),
            (fixed_sha, False),
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
    assert events.count("Design Principles reviewer turn") == 2
    assert events.count("Whole-version reviewer turn") == 2
    assert events.count("Version / README reviewer turn") == 2
    assert events.count("Whole-version fixes") == 1
    assert events[-2:] == ["final checks", f"safe:{fixed_sha}"]
    assert any(
        status == "warning"
        and "review limit 2 reached" in message
        and "without reviewer approval" in message
        for _, status, message in findings
    )


def test_whole_warning_retries_from_durable_audit_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = runpy.run_path(str(EXAMPLE))
    config = first["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    current = open_pr(head=config.integration_branch, base=config.main_branch, draft=True)
    marker = first["whole_continuation_audit"](
        2, current.head_sha, "review_limit_reached"
    )
    current = replace(current, body=first["with_review_audit"](current.body, marker))
    second = runpy.run_path(str(EXAMPLE))
    globals_ = second["review_whole_version"].__globals__

    class Repository:
        def require_pushed(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, current.head_sha, current.head_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            assert kwargs["expected_head_sha"] == current.head_sha
            return current

    monkeypatch.setitem(globals_, "MAX_REVIEWS", 2)
    monkeypatch.setitem(
        globals_, "create_agent", lambda *args, **kwargs: pytest.fail("review restarted")
    )
    monkeypatch.setitem(globals_, "emit_finding", lambda *args, **kwargs: None)
    pr, delivery = second["review_whole_version"](
        config, object(), Repository(), GitHub(), current, config.issues
    )

    assert pr == current
    assert delivery.outcome == "continued_with_warning"
    assert delivery.reviews == 2
    assert "review limit 2 reached" in delivery.warnings[0]


@pytest.mark.parametrize(
    "changed_role",
    (
        "scenario_gate", "design_principles", "whole_version",
        "version_readme", "whole_version_limit",
    ),
)
def test_whole_limit_after_head_change_persists_retry_decision(
    monkeypatch: pytest.MonkeyPatch, changed_role: str,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    globals_ = workflow["review_whole_version"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    current = replace(
        open_pr(head=config.integration_branch, base=config.main_branch, draft=True),
        head_sha="new-head",
    )
    previous = workflow["new_review_audit"](
        changed_role, 2, "CHANGES_REQUESTED", "old-head",
        review_result("CHANGES_REQUESTED", ("Correct the integration issue.",)),
    )
    current = replace(
        current,
        body=workflow["with_review_audit"](
            current.body,
            replace(previous, fix_disposition="reviewer_changed_head", fix_sha="new-head"),
        ),
    )

    class Repository:
        def require_pushed(self, branch: str) -> BranchState:
            return BranchState(branch, current.head_sha, current.head_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            return current

        def update_pr_body(self, number: int, *, body: str, **kwargs: object):
            nonlocal current
            current = replace(current, body=body)
            return current

    monkeypatch.setitem(globals_, "MAX_REVIEWS", 2)
    monkeypatch.setitem(globals_, "create_agent", lambda *args, **kwargs: kwargs["name"])
    monkeypatch.setitem(
        globals_, "run_turn", lambda *args, **kwargs: pytest.fail("review restarted")
    )
    monkeypatch.setitem(globals_, "run_final_checks", lambda *args: None)
    monkeypatch.setitem(
        globals_, "require_agent_result",
        lambda *args, **kwargs: (current.head_sha, False),
    )
    monkeypatch.setitem(globals_, "emit_finding", lambda *args, **kwargs: None)

    pr, delivery = workflow["review_whole_version"](
        config, object(), Repository(), GitHub(), current, config.issues
    )
    assert delivery.outcome == "continued_with_warning"
    assert "already reached" in delivery.warnings[0]
    marker = workflow["review_audit_from_body"](pr.body)[-1]
    assert marker.role == "whole_version_continuation"
    assert marker.fix_disposition == "review_limit_reached_after_head_change"

    recovered = runpy.run_path(str(EXAMPLE))
    recovered_globals = recovered["review_whole_version"].__globals__
    monkeypatch.setitem(recovered_globals, "MAX_REVIEWS", 2)
    monkeypatch.setitem(
        recovered_globals, "create_agent",
        lambda *args, **kwargs: pytest.fail("recovery restarted review"),
    )
    monkeypatch.setitem(recovered_globals, "emit_finding", lambda *args, **kwargs: None)
    _, retry = recovered["review_whole_version"](
        config, object(), Repository(), GitHub(), pr, config.issues
    )
    assert (
        retry.outcome, retry.head_sha, retry.base_sha, retry.reviews, retry.warnings
    ) == (
        delivery.outcome, delivery.head_sha, delivery.base_sha,
        delivery.reviews, delivery.warnings,
    )


def test_limit_head_change_marker_survives_recovery_transition() -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    current = replace(
        open_pr(head="dev/v1", base="main", draft=True), head_sha="new-head"
    )

    class GitHub:
        def update_pr_body(self, number: int, *, body: str, **kwargs: object):
            nonlocal current
            current = replace(current, body=body)
            return current

    github = GitHub()
    current = workflow["persist_whole_limit_head_change"](
        github, current, round_number=2, reviewed_sha="old-head",
        head="dev/v1", base="main",
    )
    assert workflow["review_audit_from_body"](current.body)[0].fix_disposition == (
        "pending"
    )
    current = workflow["reconcile_review_audits_after_head_change"](
        github, current, head="dev/v1", base="main"
    )
    recovered = workflow["review_audit_from_body"](current.body)[0]
    assert recovered.role == "whole_version_limit"
    assert recovered.round == 2
    assert recovered.fix_disposition == "head_changed_before_disposition"
    assert recovered.fix_sha == "new-head"


def test_whole_retry_finishes_warning_after_dispositions_but_before_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    globals_ = workflow["review_whole_version"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    current = open_pr(head=config.integration_branch, base=config.main_branch, draft=True)
    body = current.body
    for role, verdict in (
        ("design_principles", "APPROVED"),
        ("whole_version", "CHANGES_REQUESTED"),
        ("version_readme", "APPROVED"),
    ):
        result = review_result(
            verdict, ("Correct the integration issue.",)
            if verdict == "CHANGES_REQUESTED" else (),
        )
        record = workflow["new_review_audit"](
            role, 2, verdict, current.head_sha, result
        )
        if verdict == "CHANGES_REQUESTED":
            record = replace(record, fix_disposition="review_limit_reached")
        body = workflow["with_review_audit"](body, record)
    current = replace(current, body=body)

    class Repository:
        def require_pushed(self, branch: str) -> BranchState:
            return BranchState(branch, current.head_sha, current.head_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            return current

        def update_pr_body(self, number: int, *, body: str, **kwargs: object):
            nonlocal current
            current = replace(current, body=body)
            return current

    monkeypatch.setitem(globals_, "MAX_REVIEWS", 2)
    monkeypatch.setitem(globals_, "create_agent", lambda *args, **kwargs: kwargs["name"])
    monkeypatch.setitem(
        globals_, "run_turn", lambda *args, **kwargs: pytest.fail("review restarted")
    )
    monkeypatch.setitem(globals_, "run_final_checks", lambda *args: None)
    monkeypatch.setitem(
        globals_, "require_agent_result",
        lambda *args, **kwargs: (current.head_sha, False),
    )
    monkeypatch.setitem(globals_, "emit_finding", lambda *args, **kwargs: None)
    pr, delivery = workflow["review_whole_version"](
        config, object(), Repository(), GitHub(), current, config.issues
    )
    assert delivery.outcome == "continued_with_warning"
    assert workflow["review_audit_from_body"](pr.body)[-1].role == (
        "whole_version_continuation"
    )


@pytest.mark.parametrize(
    "changed_role",
    ("scenario_gate", "design_principles", "whole_version", "version_readme"),
)
def test_review_audit_pruning_keeps_head_change_continuation_evidence(
    monkeypatch: pytest.MonkeyPatch, changed_role: str,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    monkeypatch.setitem(
        workflow["with_review_audit"].__globals__, "MAX_REVIEW_AUDIT_BYTES", 1_200
    )
    prior = workflow["new_review_audit"](
        changed_role, 2, "CHANGES_REQUESTED", "old-head",
        review_result("CHANGES_REQUESTED", ("Correct the integration issue.",)),
    )
    prior = replace(
        prior, fix_disposition="reviewer_changed_head", fix_sha="new-head"
    )
    body = workflow["with_review_audit"]("Base PR.", prior)
    for round_number in range(3, 22):
        record = workflow["new_review_audit"](
            changed_role, round_number, "APPROVED", f"head-{round_number}",
            review_result(),
        )
        body = workflow["with_review_audit"](body, record)
    assert prior in workflow["review_audit_from_body"](body)


def test_review_audit_pruning_keeps_pending_fix_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    globals_ = workflow["with_review_audit"].__globals__
    monkeypatch.setitem(globals_, "MAX_REVIEW_AUDIT_RECORDS", 3)
    pending = workflow["new_review_audit"](
        "correctness", 1, "CHANGES_REQUESTED", "old-head",
        review_result("CHANGES_REQUESTED", ("Correct the issue.",)),
    )
    body = workflow["with_review_audit"]("Child PR.", pending)
    for round_number in range(2, 5):
        approved = workflow["new_review_audit"](
            "correctness", round_number, "APPROVED", f"head-{round_number}",
            review_result(),
        )
        body = workflow["with_review_audit"](body, approved)
    assert pending in workflow["review_audit_from_body"](body)

    current = replace(open_pr(head="feature/issue-311", base="dev/v1", draft=True), body=body)

    class GitHub:
        def update_pr_body(self, number: int, *, body: str, **kwargs: object):
            return replace(current, body=body)

    disposed = workflow["review_audit_disposition"](
        GitHub(), current, pending.audit_id, "fixed",
        head="feature/issue-311", base="dev/v1", fix_sha="head-4",
    )
    assert any(
        record.audit_id == pending.audit_id and record.fix_disposition == "fixed"
        for record in workflow["review_audit_from_body"](disposed.body)
    )


@pytest.mark.parametrize("resumed", (False, True))
@pytest.mark.parametrize("lost_phase", ("scope/design", "correctness"))
def test_missing_audit_after_fixes_restarts_review_and_completes_work_item(
    monkeypatch: pytest.MonkeyPatch, resumed: bool, lost_phase: str,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    globals_ = workflow["process_issue"].__globals__
    issue = workflow["Issue"](311, "feature/issue-311")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    current = open_pr(head=issue.branch, base=config.integration_branch, draft=True)
    original_body = "Child PR."
    prior = workflow["new_review_audit"](
        "correctness", 1, "APPROVED", "prior-head", review_result()
    )
    if resumed:
        original_body = workflow["with_review_audit"](original_body, prior)
    current = replace(current, body=original_body)
    review_heads: list[str] = []
    turns: list[str] = []
    persisted: list[tuple] = []
    lost_audit_id: str | None = None
    fix_delivered = False

    class Repository:
        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False)

        def inspect_branch(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, current.base_sha, current.base_sha, False)

        def require_clean(self) -> None:
            assert current.head_sha == "fixed-head"

        def require_pushed(self, branch: str) -> BranchState:
            assert branch == issue.branch
            return BranchState(branch, current.head_sha, current.head_sha, True)

        def ensure_pushed(self, branch: str, *, expected_local_sha: str) -> BranchState:
            nonlocal current, fix_delivered, lost_audit_id
            assert branch == issue.branch
            assert expected_local_sha == "fixed-head"
            assert not fix_delivered
            records = workflow["review_audit_from_body"](current.body)
            lost_audit_id = records[-1].audit_id
            assert records[-1].fix_disposition == "pending"
            current = replace(current, head_sha="fixed-head", body=original_body)
            fix_delivered = True
            return BranchState(branch, "fixed-head", "fixed-head", True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            assert kwargs["expected_head_sha"] == current.head_sha
            assert kwargs["expected_base_sha"] == current.base_sha
            assert kwargs["draft"] is True
            return current

        def update_pr_body(self, number: int, *, body: str, **kwargs: object):
            nonlocal current
            assert number == current.number
            current = replace(current, body=body)
            persisted.append(workflow["review_audit_from_body"](body))
            return current

        def set_draft(self, number: int, *, draft: bool, **kwargs: object):
            nonlocal current
            assert number == current.number
            assert draft is False
            current = replace(current, is_draft=False)
            return current

    def run_turn(*args: object, **kwargs: object) -> str:
        name = str(args[2])
        turns.append(name)
        if name.endswith("implementation"):
            return "implemented"
        if name.endswith(f"{lost_phase} fixes"):
            return "fixed"
        if name.endswith("scope/design review"):
            review_heads.append(current.head_sha)
            if lost_phase == "scope/design" and len(review_heads) == 1:
                return review_result("CHANGES_REQUESTED", ("Correct the scope.",))
            return review_result()
        if name.endswith("correctness review"):
            if lost_phase == "correctness" and current.head_sha == "review-head":
                return review_result("CHANGES_REQUESTED", ("Correct the behavior.",))
            return review_result()
        pytest.fail(f"unexpected agent turn: {name}")

    agent_results_by_turn = [
        ("review-head", True),
        ("review-head", False),
        ("fixed-head", True),
        ("fixed-head", False),
        ("fixed-head", False),
    ]
    if lost_phase == "correctness":
        agent_results_by_turn.insert(2, ("review-head", False))
    agent_results = iter(agent_results_by_turn)
    monkeypatch.setitem(globals_, "MERGE_TO_INTEGRATION", False)
    monkeypatch.setitem(globals_, "prepare_issue", lambda *args: (
        current if resumed else None, "review-head", resumed
    ))
    monkeypatch.setitem(globals_, "ensure_issue_pr", lambda *args, **kwargs: current)
    monkeypatch.setitem(globals_, "create_agent", lambda *args, **kwargs: kwargs["name"])
    monkeypatch.setitem(globals_, "run_turn", run_turn)
    monkeypatch.setitem(
        globals_, "require_agent_result", lambda *args, **kwargs: next(agent_results)
    )
    monkeypatch.setitem(globals_, "emit_issue_navigation", lambda *args, **kwargs: None)
    monkeypatch.setitem(globals_, "emit_step", lambda *args, **kwargs: None)
    monkeypatch.setitem(globals_, "emit_issue_result", lambda *args, **kwargs: None)
    monkeypatch.setitem(globals_, "emit_finding", lambda *args, **kwargs: None)
    ready = workflow["process_issue"](
        issue, config, SimpleNamespace(workspace_id="workspace"), Repository(), GitHub()
    )
    records = workflow["review_audit_from_body"](ready.body)
    assert fix_delivered
    assert review_heads == ["review-head", "fixed-head"]
    expected_turns = ["Issue #311 implementation", "Issue #311 scope/design review"]
    if lost_phase == "correctness":
        expected_turns.append("Issue #311 correctness review")
    expected_turns.extend(
        (
            f"Issue #311 {lost_phase} fixes",
            "Issue #311 scope/design review",
            "Issue #311 correctness review",
        )
    )
    assert turns == expected_turns
    assert ready.is_draft is False
    assert any(
        lost_audit_id in {record.audit_id for record in update}
        for update in persisted
    )
    assert lost_audit_id not in {record.audit_id for record in records}
    assert any(
        record.role == lost_phase.replace("/", "_")
        and record.reviewed_sha == "fixed-head"
        and record.verdict == "APPROVED"
        for record in records
    )
    assert any(
        record.role == "correctness"
        and record.reviewed_sha == "fixed-head"
        and record.verdict == "APPROVED"
        for record in records
    )
    assert any(
        record.role == "scope_design" and record.reviewed_sha == "fixed-head"
        for update in persisted for record in update
    )
    if resumed:
        assert prior in records


def test_missing_audit_does_not_retry_with_dirty_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    globals_ = workflow["review_issue_phase"].__globals__
    issue = workflow["Issue"](311, "feature/issue-311")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    current = open_pr(head=issue.branch, base=config.integration_branch, draft=True)

    class Repository:
        def require_clean(self) -> None:
            raise WorkerFailure("worktree is dirty")

    monkeypatch.setitem(
        globals_, "_review_issue_phase",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            workflow["MissingReviewAudit"]("record missing")
        ),
    )
    with pytest.raises(WorkerFailure, match="worktree is dirty"):
        workflow["review_issue_phase"](
            issue, config, object(), Repository(), object(), "implementer",
            "reviewer", current, phase="correctness", prompt="review", max_reviews=4,
        )


def test_missing_whole_review_audit_restarts_at_verified_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = runpy.run_path(str(EXAMPLE))
    globals_ = workflow["review_whole_version"].__globals__
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true"
    )
    current = open_pr(head=config.integration_branch, base=config.main_branch, draft=True)
    reviewed: list[str] = []

    class Repository:
        def require_clean(self) -> None:
            pass

        def require_pushed(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, current.head_sha, current.head_sha, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            assert kwargs["draft"] is True
            assert kwargs["expected_head_sha"] == current.head_sha
            return current

    def review(_config, _client, _repo, _github, pr, _work_items):
        reviewed.append(pr.head_sha)
        if len(reviewed) == 1:
            raise workflow["MissingReviewAudit"]("record missing")
        return pr, workflow["ReviewDelivery"]("approved", pr.head_sha, pr.base_sha, 1)

    monkeypatch.setitem(globals_, "_review_whole_version", review)
    monkeypatch.setitem(globals_, "emit_finding", lambda *args, **kwargs: None)
    pr, delivery = workflow["review_whole_version"](
        config, object(), Repository(), GitHub(), current, ()
    )
    assert pr == current
    assert delivery.outcome == "approved"
    assert reviewed == [current.head_sha, current.head_sha]


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
            self,
            branch: str,
            *,
            previous_sha: str,
            allow_unchanged: bool,
            expected_agent: str | None = None,
            expected_process: str | None = None,
        ) -> BranchState:
            assert expected_agent == "codex"
            assert expected_process == "cleanup"
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
    keep_review_audit_in_memory(workflow, monkeypatch)
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
        expected_github_slug = "acme/project"

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
            self,
            branch: str,
            *,
            previous_sha: str,
            allow_unchanged: bool,
        ) -> BranchState:
            assert not self.dirty
            return BranchState(branch, self.local_sha, self.local_sha, True)

        def require_agent_commit_provenance(
            self, start: str, end: str, **kwargs: object
        ) -> None:
            assert kwargs["expected_agent"] == "codex"
            assert kwargs["expected_process"] == "cleanup"

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
        if name == "Design Principles reviewer turn":
            return "APPROVED"
        if name == "Whole-version reviewer turn":
            review_count += 1
            return "APPROVED"
        if name == "Version / README reviewer turn":
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
    keep_review_audit_in_memory(workflow, monkeypatch)
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
