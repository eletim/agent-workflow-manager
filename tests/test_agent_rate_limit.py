from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_purplemux_client import FakeRunner, baseline, client, completed

from purplemux_client.agent_rate_limit import AgentRateLimited, detect_agent_rate_limit
from purplemux_client.errors import MutationOutcomeUnknown, WorkerFailure

NOTICE = "exceeded retry limit, last status: 429 Too Many Requests"
STATUS = {
    "cliState": "busy",
    "alive": True,
    "eventSeq": 2,
    "agentProviderId": "codex",
    "agentSessionId": "session-original",
}
EXAMPLE = Path(__file__).parents[1] / "examples/sequential-version-development.py"


def failure():
    evidence = detect_agent_rate_limit(NOTICE)
    assert evidence is not None
    return AgentRateLimited("tab-1", STATUS, evidence)


@pytest.mark.parametrize(
    "notice",
    [
        NOTICE,
        "■ " + NOTICE,
        'API Error: 429 {"type":"rate_limit_error"}',
        "You've hit your usage limit. Try again later.",
        "Rate limit exceeded",
        "error: HTTP 429 Too Many Requests; Retry-After: 120",
    ],
)
def test_recognizes_provider_errors_without_retaining_logs(notice):
    evidence = detect_agent_rate_limit(notice)
    assert evidence is not None
    assert notice not in repr(evidence)
    assert len(evidence.fingerprint) == 64


@pytest.mark.parametrize(
    "text",
    [
        "429",
        "Review error handling for HTTP 429",
        json.dumps({"verdict": "CHANGES_REQUESTED", "findings": [NOTICE]}),
        json.dumps({"summary": "You've hit your usage limit"}),
        "You've hit the target",
        "You have reached the destination",
        NOTICE + "\nA new successful response",
        "HTTP 401 Unauthorized",
        "HTTP 503 Service Unavailable",
        "› " + NOTICE,
    ],
)
def test_does_not_treat_task_results_or_other_errors_as_rate_limits(text):
    assert detect_agent_rate_limit(text) is None


def test_retry_after_is_respected():
    evidence = detect_agent_rate_limit("HTTP 429 Too Many Requests; Retry-After: 3600")
    assert evidence.retry_after_seconds == 3600


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_busy_provider_429_triggers_recovery_without_waiting_turn_timeout(provider):
    status = {**STATUS, "agentProviderId": provider}
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            *([completed(status)] * 30),
            completed(status),
            completed({"content": NOTICE}),
        ]
    )
    cli = client(runner)
    cli.send_input("tab-1", "work")
    with pytest.raises(AgentRateLimited) as exc:
        cli.wait_for_turn_completion("tab-1", 7200)
    assert exc.value.agent_session_id == "session-original"
    assert exc.value.provider == provider
    assert "tab-1" in cli._turn_baselines
    assert sum(call[2] == "send" for call in runner.calls) == 1


def test_fresh_structured_provider_error_never_becomes_a_review_result():
    ready = {**STATUS, "cliState": "ready-for-review", "eventSeq": 3}
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed(ready),
            completed(
                {"status": "completed", "completionTimestamp": 2, "text": NOTICE}
            ),
            completed(ready),
        ]
    )
    cli = client(runner)
    cli.send_input("tab-1", "review exact SHA")
    with pytest.raises(AgentRateLimited):
        cli.wait_for_turn_completion("tab-1", 900)
    assert "tab-1" not in cli._completed_turns
    assert "tab-1" in cli._turn_baselines


def test_resume_requires_acknowledged_interrupt_before_sending_to_stuck_busy_agent():
    idle = {
        **STATUS,
        "cliState": "idle",
        "eventSeq": 3,
        "lastEvent": {"name": "interrupt", "seq": 3},
    }
    runner = FakeRunner(
        [
            completed(STATUS),
            completed(STATUS),
            completed({"content": NOTICE}),
            completed(STATUS),
            completed({"status": "interrupted"}),
            completed(idle),
            completed(idle),
            completed(idle),
            completed({"status": "interrupted"}),
            completed({"status": "sent"}),
        ]
    )
    cli = client(runner)
    assert (
        cli.resume_rate_limited_turn("tab-1", failure(), "continue partial work")
        is True
    )
    mutations = [
        call[2]
        for call in runner.calls
        if call[2] in {"send", "interrupt", "close", "create"}
    ]
    assert mutations == ["interrupt", "send"]


@pytest.mark.parametrize(
    "change", [{"agentSessionId": "another-session"}, {"agentProviderId": "claude"}]
)
def test_identity_change_cannot_dispatch_continuation(change):
    runner = FakeRunner([completed({**STATUS, **change})])
    with pytest.raises(MutationOutcomeUnknown):
        client(runner).resume_rate_limited_turn("tab-1", failure(), "continue")
    assert len(runner.calls) == 1


def test_busy_agent_that_has_progressed_does_not_receive_input():
    runner = FakeRunner(
        [
            completed(STATUS),
            completed({**STATUS, "eventSeq": 3}),
            completed({"content": "working normally"}),
        ]
    )
    assert (
        client(runner).resume_rate_limited_turn("tab-1", failure(), "continue") is False
    )
    assert all(call[2] not in {"send", "interrupt"} for call in runner.calls)


def test_completed_original_turn_is_observed_without_resending():
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({**STATUS, "cliState": "ready-for-review"}),
            completed(
                {"status": "completed", "completionTimestamp": 2, "text": "done"}
            ),
        ]
    )
    cli = client(runner)
    cli.send_input("tab-1", "work")
    assert cli.resume_rate_limited_turn("tab-1", failure(), "continue") is False
    assert sum(call[2] == "send" for call in runner.calls) == 1


@pytest.mark.parametrize(
    "phase",
    ["implementation", "scope-review", "correctness-review", "work-item-planning"],
)
def test_cooldown_then_recovery_ai_resumes_same_phase_without_consuming_iteration(
    monkeypatch, phase
):
    workflow = runpy.run_path(str(EXAMPLE))
    fn = workflow["wait_for_agent_result"]
    g = fn.__globals__
    calls = []
    findings = []

    class Client:
        workspace_id = "ws-1"
        waits = 0

        def workspace_directory(self):
            return "/repo"

        def wait_for_turn_completion(self, tab, timeout, **kw):
            self.waits += 1
            if self.waits == 1:
                raise failure()

        def read_result(self, tab):
            return "done"

        def resume_rate_limited_turn(self, tab, error, prompt):
            calls.append(("resume", tab, prompt))
            return True

    monkeypatch.setitem(
        g, "rate_limit_wait", lambda seconds: calls.append(("wait", seconds))
    )
    monkeypatch.setitem(g, "snapshot_review_repositories", lambda paths: ("unchanged",))
    monkeypatch.setitem(g, "emit_finding", lambda *a, **k: findings.append(a[1]))
    monkeypatch.setitem(g, "emit_step", lambda *a, **k: None)

    def recover(cli, tab, context, **kw):
        calls.append(("recovery", context))
        return {"action": "resume", "wait_seconds": 60}

    monkeypatch.setitem(g, "recover_agent_rate_limit", recover)
    pr = SimpleNamespace(
        number=21,
        url="https://github.com/owner/repo/pull/21",
        head_sha="a" * 40,
        base_sha="b" * 40,
    )
    assert (
        fn(
            Client(),
            "tab-1",
            name="original phase",
            prompt="original task",
            repository_identity="owner/repo",
            role="reviewer" if "review" in phase else "agent",
            phase=phase,
            iteration=3,
            pr=pr,
            work_item_id="mini-task:test",
            verify_resume=lambda: calls.append(("verify",)),
            on_busy_timeout=lambda w: None,
        )
        == "done"
    )
    assert [c[0] for c in calls] == ["wait", "verify", "recovery", "verify", "resume"]
    context = next(c[1] for c in calls if c[0] == "recovery")
    assert context["phase"] == phase and context["iteration"] == 3
    assert context["head_sha"] == "a" * 40 and context["base_sha"] == "b" * 40
    assert context["agent_session"] == "session-original"
    assert NOTICE not in "\n".join(findings)
    assert "original task" in calls[-1][2]


def test_recovery_agent_rate_limit_waits_without_spawning_recursive_recovery(
    monkeypatch,
):
    workflow = runpy.run_path(str(EXAMPLE))
    fn = workflow["wait_for_agent_result"]
    g = fn.__globals__
    waits = []

    class Client:
        workspace_id = "ws"
        count = 0

        def wait_for_turn_completion(self, *a, **kw):
            self.count += 1
            if self.count <= 3:
                raise failure()

        def read_result(self, tab):
            return "recovery decision"

        def resume_rate_limited_turn(self, *a):
            return True

    monkeypatch.setitem(g, "rate_limit_wait", waits.append)
    monkeypatch.setitem(g, "emit_finding", lambda *a, **kw: None)
    monkeypatch.setitem(g, "emit_step", lambda *a, **kw: None)
    monkeypatch.setitem(
        g,
        "recover_agent_rate_limit",
        lambda *a, **kw: pytest.fail("recursive Recovery Agent"),
    )
    assert (
        fn(
            Client(),
            "tab-1",
            name="Recovery",
            prompt="inspect",
            repository_identity="owner/repo",
            role="rate-limit-recovery",
            phase="rate-limit-recovery",
            iteration=1,
            pr=None,
            work_item_id=None,
            verify_resume=lambda: None,
            on_busy_timeout=lambda w: None,
        )
        == "recovery decision"
    )
    assert waits == [60, 120, 240]


@pytest.mark.parametrize(
    "source",
    [
        '{"action":"approved","wait_seconds":60}',
        '{"action":"resume","wait_seconds":true}',
        '{"action":"resume","wait_seconds":0}',
        '{"action":"resume","wait_seconds":9999}',
        "{}",
    ],
)
def test_recovery_decision_cannot_approve_or_skip_wait_bounds(source):
    with pytest.raises(WorkerFailure):
        runpy.run_path(str(EXAMPLE))["parse_rate_limit_recovery"](source)


def test_recovery_ai_is_launched_once_and_reused_in_original_workspace(monkeypatch):
    workflow = runpy.run_path(str(EXAMPLE))
    fn = workflow["recover_agent_rate_limit"]
    g = fn.__globals__
    created = []
    assessments = []

    class Client:
        workspace_id = "ws-original"

        def workspace_directory(self):
            return "/original/worktree"

        def create_session(self, request):
            created.append(request)
            return "recovery-tab"

    def validated(cli, tab, name, prompt, validator, **kw):
        assessments.append((tab, prompt, kw))
        return "result", validator('{"action":"resume","wait_seconds":60}')

    monkeypatch.setitem(g, "run_validated_turn", validated)
    cli = Client()
    for _ in range(2):
        assert (
            fn(
                cli,
                "original-tab",
                {"head_sha": "a" * 40},
                repository_identity="owner/repo",
            )["action"]
            == "resume"
        )
    assert len(created) == 1
    assert created[0].cwd == "/original/worktree"
    assert created[0].worker == g["IMPLEMENTER_AGENT"]
    assert all(
        a[0] == "recovery-tab" and a[2]["role"] == "rate-limit-recovery"
        for a in assessments
    )
    assert all(
        "read-only" in a[1] and "Do not switch providers" in a[1] for a in assessments
    )


def test_changed_review_head_blocks_continuation_before_recovery_or_dispatch(
    monkeypatch,
):
    workflow = runpy.run_path(str(EXAMPLE))
    fn = workflow["_execute_turn"]
    g = fn.__globals__
    checks = []

    class Client:
        workspace_id = "ws"

        def wait_until_ready(self, *a):
            pass

        def send_input(self, *a):
            pass

        def wait_for_turn_completion(self, *a, **kw):
            raise failure()

        def resume_rate_limited_turn(self, *a):
            pytest.fail("continuation after changed head")

    class GitHub:
        @classmethod
        def open(cls, *a, **kw):
            return cls()

        def require_pr(self, **kw):
            checks.append(kw)
            raise WorkerFailure("PR head changed")

    monkeypatch.setitem(g, "GitHubRepository", GitHub)
    monkeypatch.setitem(g, "rate_limit_wait", lambda seconds: None)
    monkeypatch.setitem(g, "emit_finding", lambda *a, **kw: None)
    monkeypatch.setitem(g, "emit_step", lambda *a, **kw: None)
    monkeypatch.setitem(g, "emit_agent_turn", lambda *a, **kw: None)
    monkeypatch.setitem(
        g,
        "recover_agent_rate_limit",
        lambda *a, **kw: pytest.fail("recovery after failed target check"),
    )
    pr = SimpleNamespace(
        number=21,
        url="https://github.com/owner/repo/pull/21",
        head_sha="a" * 40,
        base_sha="b" * 40,
        head_branch="feature/task",
        base_branch="dev/v0.4.0",
    )
    with pytest.raises(WorkerFailure, match="PR head changed"):
        fn(
            Client(),
            "tab-1",
            "Correctness review",
            "Review exact head",
            repository_identity="owner/repo",
            role="reviewer",
            phase="correctness-review",
            iteration=3,
            pr=pr,
        )
    assert checks[0]["expected_head_sha"] == "a" * 40
    assert checks[0]["expected_base_sha"] == "b" * 40


def test_provider_retry_after_delays_recovery_attempt(monkeypatch):
    workflow = runpy.run_path(str(EXAMPLE))
    fn = workflow["wait_for_agent_result"]
    g = fn.__globals__
    waits = []
    evidence = detect_agent_rate_limit("HTTP 429 Too Many Requests; Retry-After: 3600")

    class Client:
        workspace_id = "ws"
        count = 0

        def wait_for_turn_completion(self, *a, **kw):
            self.count += 1
            if self.count == 1:
                raise AgentRateLimited("tab-1", STATUS, evidence)

        def read_result(self, tab):
            return "done"

        def resume_rate_limited_turn(self, *a):
            return True

    monkeypatch.setitem(g, "rate_limit_wait", waits.append)
    monkeypatch.setitem(g, "emit_finding", lambda *a, **kw: None)
    monkeypatch.setitem(g, "emit_step", lambda *a, **kw: None)
    assert (
        fn(
            Client(),
            "tab-1",
            name="Recovery",
            prompt="inspect",
            repository_identity="owner/repo",
            role="rate-limit-recovery",
            phase="rate-limit-recovery",
            iteration=1,
            pr=None,
            work_item_id=None,
            verify_resume=lambda: None,
            on_busy_timeout=lambda w: None,
        )
        == "done"
    )
    assert waits == [3600]


def test_retry_after_http_date_and_multiline_hint(monkeypatch):
    import purplemux_client.agent_rate_limit as module

    monkeypatch.setattr(module.time, "time", lambda: 0)
    evidence = detect_agent_rate_limit(
        NOTICE + "\nRetry-After: Thu, 01 Jan 1970 01:00:00 GMT"
    )
    assert evidence is not None and evidence.retry_after_seconds == 3600


def test_diagnostic_502_does_not_end_an_otherwise_running_turn():
    ready = {**STATUS, "cliState": "ready-for-review", "eventSeq": 3}
    error = completed("", returncode=1, stderr="HTTP 502")
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            *([completed(STATUS)] * 30),
            completed(STATUS),
            error,
            error,
            completed(ready),
            completed(
                {"status": "completed", "completionTimestamp": 2, "text": "done"}
            ),
        ]
    )
    cli = client(runner)
    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 900)
    assert cli.read_result("tab-1") == "done"


def test_manual_interrupt_during_cooldown_is_not_overridden():
    from purplemux_client import WorkerInterrupted

    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed(
                {
                    **STATUS,
                    "cliState": "idle",
                    "eventSeq": 3,
                    "lastEvent": {"name": "interrupt", "seq": 3},
                }
            ),
        ]
    )
    cli = client(runner)
    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerInterrupted, match="during rate-limit wait"):
        cli.resume_rate_limited_turn("tab-1", failure(), "continue")
    assert sum(call[2] == "send" for call in runner.calls) == 1
