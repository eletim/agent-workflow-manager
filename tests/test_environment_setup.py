from __future__ import annotations

import ast
import json
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import purplemux_client.environment_setup as setup


def declaration(**overrides: object) -> str:
    value: dict[str, object] = {
        "mode": "environment-setup",
        "repository": "/source/repo",
        "revision": "dev/v0.4.1",
        "environment_agent": "codex",
        "timeout": 120,
    }
    value.update(overrides)
    return json.dumps(value)


@pytest.fixture(autouse=True)
def repository_lookup(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def inspect(*, repo: str, revision: str) -> tuple[SimpleNamespace, str]:
        calls.append((repo, revision))
        return SimpleNamespace(
            source_repository=Path(repo), revision_validation="verified"
        ), "branch"

    monkeypatch.setattr(setup, "inspect_run_revision", inspect)
    return calls


def test_generates_python_from_declarative_inputs(
    repository_lookup: list[tuple[str, str]],
) -> None:
    config = setup.parse_environment_setup_json(
        declaration(
            build="python -m build",
            start="python app.py",
            ready_check="curl http://localhost:8000/health",
            environment_agent="claude-code",
        )
    )
    code = setup.generate_environment_setup_workflow(config)
    ast.parse(code)
    assert repository_lookup == [("/source/repo", "dev/v0.4.1")]
    assert "prepare_run_revision(" in code
    assert "revision='dev/v0.4.1'" in code
    assert "PurpleMuxRuntime(owned_by_run=True)" in code
    assert "client.command_timeout_seconds" not in code
    assert "worker='claude-code'" in code
    assert "deadline = time.monotonic() + 120" in code
    assert "Build command: python -m build" in code
    assert "Start command: python app.py" in code
    assert "Ready check command: curl http://localhost:8000/health" in code
    assert "exactly as given" in code
    assert "before considering any alternative" in code
    assert "temporary setup changes in the execution directory" in code
    assert "Report BLOCKED if readiness requires a permanent product fix" in code
    assert "Execution failed or readiness was not reached" in code
    assert "Inspect repository files and managed terminal logs" in code
    assert "Do not change product code to conceal a product failure" in code
    assert "execute_environment_setup_commands(" in code
    assert "steps" not in config.as_json()


@pytest.mark.parametrize(
    "source,expected",
    [
        ('{"mode":"environment-setup","mode":"environment-setup"}', "duplicate"),
        (declaration(steps=[]), "unknown fields"),
        (declaration(environment_agent="terminal"), "environment_agent"),
        (declaration(timeout=True), "timeout"),
        (declaration(timeout=0), "timeout"),
        (declaration(build=" "), "build"),
        (declaration(revision=""), "revision"),
        (declaration(repository="bad\0path"), "repository"),
    ],
)
def test_rejects_invalid_inputs(source: str, expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        setup.parse_environment_setup_json(source)


def test_omitted_commands_are_not_in_generated_prompt() -> None:
    config = setup.parse_environment_setup_json(declaration())
    code = setup.generate_environment_setup_workflow(config)
    assert "Build command:" not in code
    assert "Start command:" not in code
    assert "Ready check command:" not in code
    assert "Skip instructions that were omitted" in code
    assert "even if all were omitted" in code
    assert set(config.as_json()) == {
        "mode",
        "repository",
        "revision",
        "environment_agent",
        "timeout",
    }


@pytest.mark.parametrize(
    ("report", "busy_timeout", "late_completion", "expected_error"),
    [
        (
            {
                "status": "BLOCKED",
                "summary": "build failed",
                "checks": {"build": "failed"},
            },
            False,
            False,
            "agent blocked: build failed",
        ),
        (
            {
                "status": "READY",
                "summary": "ready",
                "checks": {"build": "passed"},
                "verification": "ok",
            },
            False,
            False,
            "agent blocked: no usable check",
        ),
        (None, True, False, "timed out while the agent was busy"),
        (
            {
                "status": "READY",
                "summary": "ready",
                "checks": {"build": "passed"},
                "verification": "service responded successfully",
                "verification_command": "printf usable; test -d .",
                "endpoint": "http://127.0.0.1:8000/health",
                "resolved_revision": "unverified",
                "working_path": "/wrong/path",
            },
            False,
            False,
            None,
        ),
        (
            {
                "status": "READY",
                "summary": "ready",
                "checks": {"build": "passed"},
                "verification": "service responded successfully",
                "verification_command": "printf usable; test -d .",
            },
            False,
            True,
            "Environment Setup timed out",
        ),
        (
            {
                "status": "READY",
                "summary": "recover",
                "verification_command": "printf usable; test -d .",
            },
            False,
            False,
            None,
        ),
        (
            {
                "status": "READY",
                "summary": "start interrupted",
                "verification_command": "printf usable; test -d .",
            },
            False,
            False,
            "start observation interrupted",
        ),
        (
            {
                "status": "READY",
                "summary": "final blocked",
                "verification_command": "printf usable; test -d .",
            },
            False,
            False,
            "agent blocked: endpoint unavailable",
        ),
    ],
)
def test_generated_workflow_fails_on_failed_command_or_busy_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    report: dict[str, object] | None,
    busy_timeout: bool,
    late_completion: bool,
    expected_error: str | None,
) -> None:
    import purplemux_client

    events: list[tuple[str, str]] = []
    interrupted: list[str] = []

    class Client:
        workspace_id = "ws-1"

        def __init__(self) -> None:
            self.reads = 0
            self.shells = 0
            self.prompts: list[str] = []

        def create_session(self, _request: object) -> str:
            return "tab-1"

        def wait_until_ready(self, _tab: str, _timeout: float) -> None:
            pass

        def send_input(self, _tab: str, prompt: str) -> None:
            self.prompts.append(prompt)

        def wait_for_turn_completion(
            self, _tab: str, _timeout: float, *, on_busy_timeout: object
        ) -> None:
            if busy_timeout:
                on_busy_timeout("still busy")  # type: ignore[operator]
            if late_completion:
                time.sleep(1.05)

        def read_result(self, _tab: str) -> str:
            self.reads += 1
            if (
                self.reads > 1
                and report is not None
                and report.get("summary") == "final blocked"
            ):
                return json.dumps(
                    {"status": "BLOCKED", "summary": "endpoint unavailable"}
                )
            if (
                self.reads > 1
                and report is not None
                and not report.get("verification_command")
            ):
                return json.dumps({"status": "BLOCKED", "summary": "no usable check"})
            return json.dumps(report)

        def start_shell(self, _request: object, *, on_created=None) -> str:
            self.shells += 1
            tab = f"shell-{self.shells}"
            if on_created is not None:
                on_created(tab, "/managed/result.json")
            return tab

        def wait_for_shell_completion(self, _tab: str, _timeout: float) -> None:
            pass

        def read_shell_result(self, tab: str) -> SimpleNamespace:
            if (
                report is not None
                and report.get("summary") == "start interrupted"
                and tab == "shell-2"
            ):
                raise TimeoutError("start observation interrupted")
            return SimpleNamespace(
                exit_code=3
                if report is not None
                and report.get("summary") == "recover"
                and tab == "shell-1"
                else 0
            )

        def capture_screen(self, tab: str) -> str:
            return f"observed {tab}"

        def read_status(self, _tab: str) -> dict[str, object]:
            return {"alive": True}

        def interrupt(self, tab: str) -> None:
            interrupted.append(tab)

    client = Client()

    class Runtime:
        def __init__(self, *, owned_by_run: bool) -> None:
            assert owned_by_run

        def create_workspace(self, _request: object) -> SimpleNamespace:
            return SimpleNamespace(id="ws-1")

        def workspace(self, _workspace_id: str) -> Client:
            return client

    monkeypatch.setattr(purplemux_client, "PurpleMuxRuntime", Runtime)
    monkeypatch.setattr(
        purplemux_client,
        "prepare_run_revision",
        lambda **_kwargs: SimpleNamespace(execution_root=tmp_path, base_sha="a" * 40),
    )
    monkeypatch.setattr(
        purplemux_client,
        "emit_step",
        lambda _name, state, **_kwargs: events.append(("Environment Setup", state)),
    )
    code = setup.generate_environment_setup_workflow(
        setup.EnvironmentSetupInput(
            "/source/repo",
            "main",
            "codex",
            1 if late_completion else 120,
            build="printf built",
            start="serve"
            if report is not None and report.get("summary") == "start interrupted"
            else None,
        )
    )
    output = StringIO()
    if expected_error is None:
        with redirect_stdout(output):
            exec(compile(code, "<environment-setup>", "exec"), {})
        result = json.loads(output.getvalue())
        assert result["status"] == "READY"
        assert result["resolved_revision"] == "a" * 40
        assert result["working_path"] == str(tmp_path)
        expected_connection = {"workspace_id": "ws-1", "agent_tab_id": "tab-1"}
        if report is not None and "endpoint" in report:
            expected_connection["endpoint"] = report["endpoint"]
        assert result["connection"] == expected_connection
        assert result["process"] is None
        assert result["execution_summary"]
        assert result["readiness_summary"]
        offset = 1 if report is not None and report.get("summary") == "recover" else 0
        assert result["checks"]["build"]["output"] == f"observed shell-{1 + offset}"
        assert result["verification"]["output"] == f"observed shell-{2 + offset}"
        assert len(result["attempts"]) == 1 + offset
        if offset:
            assert client.reads == 3
            assert "Environment Setup build failed" in client.prompts[1]
            assert "temporary environment or setup changes" in client.prompts[1]
        assert "observed a usable connection address" in client.prompts[-1]
    else:
        with redirect_stdout(output):
            exec(compile(code, "<environment-setup>", "exec"), {})
        result = json.loads(output.getvalue())
        assert result["status"] == "BLOCKED"
        assert expected_error in result["summary"]
        assert result["observed_facts"]["error"] == result["summary"]
        assert result["resolved_revision"] == "a" * 40
        assert result["working_path"] == str(tmp_path)
        assert result["connection"] == {"workspace_id": "ws-1", "agent_tab_id": "tab-1"}
        if (
            report is not None
            and report.get("summary") == "ready"
            and not report.get("verification_command")
        ):
            assert result["attempts"][0]["failed_stage"] == "ready_check"
        if report is not None and report.get("summary") == "start interrupted":
            assert result["checks"]["build"]["exit_code"] == 0
            assert result["process"]["tab_id"] == "shell-2"
            assert result["service_tab"] == "shell-2"
            assert result["attempts"][0]["failed_stage"] == "start"
        if report is not None and report.get("summary") == "final blocked":
            assert result["attempts"][0]["failure"] is None
    assert events == [
        ("Environment Setup", "started"),
        ("Environment Setup", "completed" if expected_error is None else "failed"),
    ]
    assert interrupted == (["tab-1"] if busy_timeout else [])


@pytest.mark.parametrize("phase", ["preparation", "workspace", "session"])
def test_generated_workflow_stops_creating_resources_after_deadline(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    import purplemux_client

    created: list[str] = []
    events: list[str] = []

    def prepare(**kwargs: object) -> SimpleNamespace:
        if phase == "preparation":
            time.sleep(1.05)
        kwargs["deadline_check"]()  # type: ignore[operator]
        created.append("worktree")
        return SimpleNamespace(
            execution_root=Path("/tmp/environment-setup"), base_sha="b" * 40
        )

    class Client:
        command_timeout_seconds = 30.0

        def create_session(self, request: object) -> str:
            if phase == "session":
                time.sleep(1.05)
            request.deadline_check()  # type: ignore[attr-defined]
            created.append("session")
            return "tab-1"

    class Runtime:
        def __init__(self, *, owned_by_run: bool) -> None:
            assert owned_by_run

        def create_workspace(self, request: object) -> SimpleNamespace:
            if phase == "workspace":
                time.sleep(1.05)
            request.deadline_check()  # type: ignore[attr-defined]
            created.append("workspace")
            return SimpleNamespace(id="ws-1")

        def workspace(self, _workspace_id: str) -> Client:
            return Client()

    monkeypatch.setattr(purplemux_client, "prepare_run_revision", prepare)
    monkeypatch.setattr(purplemux_client, "PurpleMuxRuntime", Runtime)
    monkeypatch.setattr(
        purplemux_client,
        "emit_step",
        lambda _name, state, **_kwargs: events.append(state),
    )
    code = setup.generate_environment_setup_workflow(
        setup.EnvironmentSetupInput("/source/repo", "main", "codex", 1)
    )
    output = StringIO()
    with redirect_stdout(output):
        exec(compile(code, "<environment-setup>", "exec"), {})
    result = json.loads(output.getvalue())
    assert result["status"] == "BLOCKED"
    assert "Environment Setup timed out" in result["summary"]
    assert result["resolved_revision"] == (None if phase == "preparation" else "b" * 40)
    assert result["attempts"] == []
    assert events == ["started", "failed"]
    assert (
        created
        == {
            "preparation": [],
            "workspace": ["worktree"],
            "session": ["worktree", "workspace"],
        }[phase]
    )


def test_large_result_remains_parseable_and_keeps_latest_observation() -> None:
    history = [
        {"checks": {"build": {"output": "x" * 4096}}, "failure": f"attempt {i}"}
        for i in range(1000)
    ]
    result = {
        "status": "BLOCKED",
        "summary": "retry limit reached",
        "resolved_revision": "a" * 40,
        "working_path": "/tmp/setup",
        "connection": {"workspace_id": "ws-1"},
        "attempts": history,
        "observed_facts": {"agent_reports": [{"summary": "y" * 4096}] * 1000},
    }
    payload = setup.serialize_environment_setup_result(result)
    assert len(payload) < 100_000
    decoded = json.loads(payload)
    assert decoded["status"] == "BLOCKED"
    assert decoded["attempts"][-1]["failure"] == "attempt 999"
    assert decoded["history_truncated"] == {
        "attempts": 992,
        "agent_reports": 992,
    }
    assert decoded["resolved_revision"] == "a" * 40
