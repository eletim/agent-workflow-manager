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
            "did not report verified READY",
        ),
        (
            {
                "status": "READY",
                "summary": "build failed",
                "checks": {"build": "failed"},
            },
            False,
            False,
            "did not report verified READY",
        ),
        (None, True, False, "timed out while the agent was busy"),
        (
            {
                "status": "READY",
                "summary": "ready",
                "checks": {"build": "passed"},
                "resolved_revision": "unverified",
                "working_path": "/wrong/path",
            },
            False,
            False,
            None,
        ),
        (
            {"status": "READY", "summary": "ready", "checks": {"build": "passed"}},
            False,
            True,
            "Environment Setup timed out",
        ),
    ],
)
def test_generated_workflow_fails_on_failed_command_or_busy_timeout(
    monkeypatch: pytest.MonkeyPatch,
    report: dict[str, object] | None,
    busy_timeout: bool,
    late_completion: bool,
    expected_error: str | None,
) -> None:
    import purplemux_client

    events: list[tuple[str, str]] = []
    interrupted: list[str] = []

    class Client:
        def create_session(self, _request: object) -> str:
            return "tab-1"

        def wait_until_ready(self, _tab: str, _timeout: float) -> None:
            pass

        def send_input(self, _tab: str, _prompt: str) -> None:
            pass

        def wait_for_turn_completion(
            self, _tab: str, _timeout: float, *, on_busy_timeout: object
        ) -> None:
            if busy_timeout:
                on_busy_timeout("still busy")  # type: ignore[operator]
            if late_completion:
                time.sleep(1.05)

        def read_result(self, _tab: str) -> str:
            return json.dumps(report)

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
        lambda **_kwargs: SimpleNamespace(
            execution_root=Path("/tmp/environment-setup"), base_sha="a" * 40
        ),
    )
    monkeypatch.setattr(
        purplemux_client,
        "emit_step",
        lambda _name, state, **_kwargs: events.append(("Environment Setup", state)),
    )
    code = setup.generate_environment_setup_workflow(
        setup.EnvironmentSetupInput(
            "/source/repo", "main", "codex", 1 if late_completion else 120, build="make"
        )
    )
    output = StringIO()
    if expected_error is None:
        with redirect_stdout(output):
            exec(compile(code, "<environment-setup>", "exec"), {})
        result = json.loads(output.getvalue())
        assert result["resolved_revision"] == "a" * 40
        assert result["working_path"] == "/tmp/environment-setup"
    else:
        with pytest.raises((RuntimeError, TimeoutError), match=expected_error):
            with redirect_stdout(output):
                exec(compile(code, "<environment-setup>", "exec"), {})
        assert not output.getvalue()
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
        return SimpleNamespace(execution_root=Path("/tmp/environment-setup"))

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
    with pytest.raises(TimeoutError, match="Environment Setup timed out"):
        exec(compile(code, "<environment-setup>", "exec"), {})
    assert events == ["started", "failed"]
    assert (
        created
        == {
            "preparation": [],
            "workspace": ["worktree"],
            "session": ["worktree", "workspace"],
        }[phase]
    )
