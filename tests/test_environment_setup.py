from __future__ import annotations

import ast
import json
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
        return SimpleNamespace(source_repository=Path(repo)), "branch"

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
