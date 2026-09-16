from __future__ import annotations

import ast
import json
import subprocess
import threading
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runner import request

from purplemux_client.review import (
    generate_review_workflow,
    parse_review_json,
    serialize_review_result,
)
from purplemux_client.runner import PythonRunner
from purplemux_client.web import RunnerHTTPServer


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[Path, Path]:
    paths = (tmp_path / "first", tmp_path / "second")
    for path in paths:
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True)
    return paths


def declaration(paths: tuple[Path, Path], **changes: object) -> str:
    value: dict[str, object] = {
        "mode": "review",
        "repositories": [str(path) for path in paths],
        "check": "Inspect both repositories for missing error handling.",
    }
    value.update(changes)
    return json.dumps(value)


def test_review_generates_valid_ordinary_workflow(repositories: tuple[Path, Path]) -> None:
    config = parse_review_json(
        declaration(repositories, start="Read their README files.", finish="Summarize risks.",
                    agent="claude-code", timeout=120)
    )
    assert config.as_json() == {
        "mode": "review", "repositories": [str(path) for path in repositories],
        "check": "Inspect both repositories for missing error handling.",
        "start": "Read their README files.", "finish": "Summarize risks.",
        "agent": "claude-code", "timeout": 120,
    }
    code = generate_review_workflow(config)
    ast.parse(code)
    assert "WORKFLOW_OUTLINE = [\"Review\"]" in code
    assert "PurpleMuxRuntime(owned_by_run=True)" in code
    assert "worker=AGENT" in code
    assert "if START is not None:" in code
    assert "if FINISH is not None:" in code
    assert "json.loads(report)" in code
    runner = PythonRunner(managed_workflows=False)
    try:
        assert runner.validate(code).valid
    finally:
        runner.close()


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"repositories": []}, "repositories must"),
        ({"repositories": "one"}, "repositories must"),
        ({"repositories": [None]}, r"repositories\[0\]"),
        ({"check": "  "}, "check"),
        ({"start": ""}, "start"),
        ({"finish": None}, "finish"),
        ({"agent": "shell"}, "agent"),
        ({"timeout": True}, "timeout"),
        ({"timeout": 86401}, "timeout"),
        ({"unexpected": "value"}, "unknown fields"),
        ({"mode": "workflow"}, "mode"),
    ],
)
def test_review_rejects_bad_inputs(
    repositories: tuple[Path, Path], changes: dict[str, object], expected: str,
) -> None:
    with pytest.raises(ValueError, match=expected):
        parse_review_json(declaration(repositories, **changes))


def test_review_rejects_duplicate_or_nested_repository(repositories: tuple[Path, Path]) -> None:
    with pytest.raises(ValueError, match="repeat"):
        parse_review_json(declaration(repositories, repositories=[str(repositories[0])] * 2))
    nested = repositories[0] / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="repository root"):
        parse_review_json(declaration(repositories, repositories=[str(nested)]))
    source = declaration(repositories)
    with pytest.raises(ValueError, match="duplicate fields"):
        parse_review_json(source.replace('"mode": "review",', '"mode": "review", "mode": "review",'))


def test_review_generation_api_feeds_ordinary_run(
    repositories: tuple[Path, Path],
) -> None:
    runner = PythonRunner(managed_workflows=False)
    server = RunnerHTTPServer(("127.0.0.1", 0), runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = (str(server.server_address[0]), int(server.server_address[1]))
    try:
        source = declaration(repositories)
        status, generated = request(
            address, "POST", "/api/review/generate",
            json.dumps({"json": source}), token=server.request_token,
        )
        assert status == 200
        assert generated["config"]["repositories"] == [str(path) for path in repositories]
        assert runner.validate(generated["generatedCode"]).valid
        status, rejected = request(
            address, "POST", "/api/review/generate",
            json.dumps({"json": declaration(repositories, check="")}),
            token=server.request_token,
        )
        assert status == 422
        assert "check" in rejected["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        runner.close()


@pytest.mark.parametrize("oversized", [False, True])
def test_generated_review_sequences_optional_turns_and_reports_result(
    repositories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, oversized: bool,
) -> None:
    import purplemux_client

    messages: list[str] = []
    steps: list[tuple[str, str]] = []

    class Client:
        def create_session(self, request: object) -> str:
            assert request.worker == "codex"
            return "agent-tab"

        def wait_until_ready(self, _tab: str, _seconds: float) -> None:
            pass

        def send_input(self, _tab: str, message: str) -> None:
            messages.append(message)

        def wait_for_turn_completion(self, _tab: str, _seconds: float, **_kwargs: object) -> None:
            pass

        def read_result(self, _tab: str) -> str:
            if len(messages) == 2:
                return json.dumps({
                    "verdict": "FAIL",
                    "summary": "Missing handling" if not oversized else "x" * 1_100_000,
                    "findings": ["A" if not oversized else "y" * 1_100_000],
                })
            return "done"

    client = Client()

    class Runtime:
        def __init__(self, *, owned_by_run: bool) -> None:
            assert owned_by_run

        def create_workspace(self, request: object) -> SimpleNamespace:
            assert request.cwd == str(repositories[0])
            return SimpleNamespace(id="review-workspace")

        def workspace(self, _workspace_id: str) -> Client:
            return client

    monkeypatch.setattr(purplemux_client, "PurpleMuxRuntime", Runtime)
    monkeypatch.setattr(
        purplemux_client, "emit_step",
        lambda name, state, **_kwargs: steps.append((name, state)),
    )
    config = parse_review_json(declaration(
        repositories, start="Survey layout", finish="Summarize findings",
    ))
    output = StringIO()
    with redirect_stdout(output):
        exec(compile(generate_review_workflow(config), "<review>", "exec"), {})
    assert len(messages) == 3
    assert "Survey layout" in messages[0]
    assert "Perform this check" in messages[1]
    assert "Summarize findings" in messages[2]
    retained = output.getvalue()
    assert len(retained) <= 1_000_000
    result = json.loads(retained)
    assert result["verdict"] == "FAIL"
    assert result["repositories"] == [str(path) for path in repositories]
    if oversized:
        assert result["truncated"] == {
            "summary_chars": 1_100_000 - 16384,
            "finding_texts": 1,
        }
        assert len(result["summary"]) == 16384
        assert len(result["findings"][0]) == 512
        assert len(messages[2]) < 1_000_000
    assert steps == [("Review", "started"), ("Review", "completed")]


def test_review_result_stays_complete_with_worst_case_json_escaping() -> None:
    report = {
        "verdict": "PASS",
        "summary": "😀" * 20_000,
        "findings": ["😀" * 1_000] * 120,
    }
    repositories = tuple("/" + "😀" * 2_000 + str(index) for index in range(32))
    payload = serialize_review_result(report, repositories)
    assert len(payload) + 1 <= 1_000_000
    result = json.loads(payload)
    assert result["verdict"] == "PASS"
    assert result["truncated"]["repositories"] > 0
    assert result["truncated"]["findings"] == 20
