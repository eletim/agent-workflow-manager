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
from test_runner import request, wait_for

from purplemux_client.errors import WorkerFailure
from purplemux_client.review_fix import (
    generate_review_fix_workflow,
    parse_review_fix_json,
    serialize_review_fix_result,
)
from purplemux_client.runner import PythonRunner
from purplemux_client.web import RunnerHTTPServer
from purplemux_client.workflow import ChildRunResult


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    path = tmp_path / "repository"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def declaration(path: Path, **changes: object) -> str:
    value: dict[str, object] = {
        "mode": "review-fix",
        "repository": str(path),
        "start": {
            "command": "./start-dev.sh",
            "ready_check": "http://127.0.0.1:3000/api/health",
        },
        "check": "Use the browser to verify the feature.",
        "max_iterations": 5,
    }
    value.update(changes)
    return json.dumps(value)


def test_review_fix_contract_generates_valid_plain_python(repository: Path) -> None:
    config = parse_review_fix_json(declaration(repository))
    assert config.as_json() == {
        "mode": "review-fix",
        "repository": str(repository),
        "start": {
            "command": "./start-dev.sh",
            "ready_check": "http://127.0.0.1:3000/api/health",
        },
        "check": "Use the browser to verify the feature.",
        "max_iterations": 5,
        "review_agent": "codex",
        "implementation_agent": "codex",
        "timeout": 3600,
    }
    code = generate_review_fix_workflow(config)
    ast.parse(code)
    assert 'WORKFLOW_OUTLINE = ["Start service", "Review Fix"]' in code
    assert "generate_review_workflow(ReviewInput(" in code
    assert "start=service_context" in code
    assert "start_child_run(review_code)" in code
    assert 'if report["verdict"] in ("PASS", "BLOCKED"):' in code
    assert "for iteration in range(1, MAX_ITERATIONS + 1):" in code
    assert "client.close_session(tab)" in code
    assert "repo.require_committed_result(" in code
    assert 'expected_process="implementation"' in code
    runner = PythonRunner(managed_workflows=False)
    try:
        assert runner.validate(code).valid
    finally:
        runner.close()


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"mode": "review"}, "mode"),
        ({"repository": "missing"}, "repository"),
        ({"start": "run"}, "start"),
        ({"start": {"command": "run"}}, "start"),
        (
            {"start": {"command": "run", "ready_check": "not-a-url"}},
            "ready_check",
        ),
        ({"check": ""}, "check"),
        ({"max_iterations": 0}, "max_iterations"),
        ({"max_iterations": True}, "max_iterations"),
        ({"review_agent": "shell"}, "review_agent"),
        ({"implementation_agent": "shell"}, "implementation_agent"),
        ({"timeout": 0}, "timeout"),
        ({"extra": True}, "unknown fields"),
    ],
)
def test_review_fix_rejects_invalid_declarations(
    repository: Path, changes: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        parse_review_fix_json(declaration(repository, **changes))


def test_review_fix_result_compacts_worst_case_history() -> None:
    text = '\\"\n' * 2048
    report = {
        "verdict": "FAIL",
        "summary": text,
        "findings": [text] * 5,
        "observed_facts": [text] * 5,
        "evidence": [text] * 5,
        "hypotheses": [text] * 5,
        "observability_gaps": [text] * 5,
    }
    result = {
        "verdict": "FAIL",
        "summary": "maximum iterations",
        "repository": "/repo",
        "readiness": {"url": "http://127.0.0.1:3000", "attempts": 1},
        "iterations": [
            {
                "iteration": iteration,
                "review_run_id": iteration,
                "review": report,
                "implementation": text,
                "implementation_sha": "a" * 40,
            }
            for iteration in range(1, 51)
        ],
    }

    payload = serialize_review_fix_result(result)
    restored = json.loads(payload)

    assert len(payload) <= 999_999
    assert restored["verdict"] == "FAIL"
    assert restored["iterations"][-1]["iteration"] == 50
    assert restored.get("iterations_omitted", 0) > 0


def test_review_fix_generation_and_run_binding(
    repository: Path, tmp_path: Path
) -> None:
    source = declaration(repository)
    history = tmp_path / "runs.json"
    runner = PythonRunner(managed_workflows=False, run_history_file=history)
    server = RunnerHTTPServer(("127.0.0.1", 0), runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = (str(server.server_address[0]), int(server.server_address[1]))
    try:
        status, generated = request(
            address,
            "POST",
            "/api/review-fix/generate",
            json.dumps({"json": source}),
            token=server.request_token,
        )
        assert status == 200
        assert generated["config"]["mode"] == "review-fix"
        code = generated["generatedCode"]
        assert runner.validate(code).valid

        status, rejected = request(
            address,
            "POST",
            "/api/run",
            json.dumps({"code": code + "\n", "reviewFixJson": source}),
            token=server.request_token,
        )
        assert status == 400

        harmless = 'print("bound")'
        from purplemux_client import web

        original = web.generate_review_fix_workflow
        web.generate_review_fix_workflow = lambda _config: harmless
        try:
            status, started = request(
                address,
                "POST",
                "/api/run",
                json.dumps({"code": harmless, "reviewFixJson": source}),
                token=server.request_token,
            )
        finally:
            web.generate_review_fix_workflow = original
        assert status == 202
        snapshot = wait_for(
            runner, lambda item: item.state == "success", run_id=started["runId"]
        )
        assert snapshot.as_json()["mode"] == "review-fix"
        assert snapshot.as_json()["reviewFixJson"] == source
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        runner.close()

    restored = PythonRunner(managed_workflows=False, run_history_file=history)
    try:
        snapshot = restored.snapshot(started["runId"]).as_json()
        assert snapshot["mode"] == "review-fix"
        assert snapshot["reviewFixJson"] == source
    finally:
        restored.close()


def test_generated_review_fix_separates_review_and_implementation_roles(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import purplemux_client
    import purplemux_client.workflow

    class ShellResult:
        exit_code = 0

        @staticmethod
        def failure_message(_name: str) -> str:
            return "failed"

    class Client:
        def __init__(self) -> None:
            self.shells = 0
            self.services = 0
            self.prompts: list[str] = []
            self.closed: list[str] = []

        def start_shell(self, request: object) -> str:
            self.shells += 1
            if "service" in request.name:
                self.services += 1
                return "service-" + str(self.services)
            return "readiness-" + str(self.shells)

        def wait_for_shell_completion(self, _tab: str, _timeout: float) -> None:
            pass

        def read_shell_result(self, _tab: str) -> ShellResult:
            return ShellResult()

        def close_session(self, tab: str) -> None:
            self.closed.append(tab)

        def create_session(self, _request: object) -> str:
            return "implementation"

        def wait_until_ready(self, _tab: str, _timeout: float) -> None:
            pass

        def send_input(self, _tab: str, prompt: str) -> None:
            self.prompts.append(prompt)

        def wait_for_turn_completion(self, _tab: str, _timeout: float) -> None:
            pass

        def read_result(self, _tab: str) -> str:
            return "implemented"

    client = Client()
    validations = 0

    class Repository:
        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(current_branch="feature/review-fix", dirty=False)

        def require_clean(self) -> None:
            pass

        def inspect_branch(self, branch: str) -> SimpleNamespace:
            assert branch == "feature/review-fix"
            return SimpleNamespace(local_sha="a" * 40)

        def require_committed_result(
            self, branch: str, **kwargs: object
        ) -> SimpleNamespace:
            nonlocal validations
            assert branch == "feature/review-fix"
            assert kwargs == {
                "previous_sha": "a" * 40,
                "expected_agent": "codex",
                "expected_process": "implementation",
            }
            validations += 1
            if validations == 1:
                raise WorkerFailure("worktree must be clean")
            return SimpleNamespace(local_sha="b" * 40)

    class RepositoryType:
        @staticmethod
        def open(path: str) -> Repository:
            assert path == str(repository)
            return Repository()

    class Runtime:
        def __init__(self, *, owned_by_run: bool) -> None:
            assert owned_by_run

        def create_workspace(self, _request: object) -> SimpleNamespace:
            return SimpleNamespace(id="workspace")

        def workspace(self, workspace_id: str) -> Client:
            assert workspace_id == "workspace"
            return client

    reports = iter(
        [
            {"verdict": "FAIL", "summary": "broken", "repositories": [str(repository)]},
            {"verdict": "PASS", "summary": "fixed", "repositories": [str(repository)]},
        ]
    )
    child_ids: list[int] = []
    review_codes: list[str] = []

    def start_child(code: str) -> int:
        review_codes.append(code)
        run_id = len(child_ids) + 1
        child_ids.append(run_id)
        return run_id

    def wait_child(run_id: int, *, timeout: float) -> ChildRunResult:
        assert timeout > 0
        return ChildRunResult(run_id, "success", 0, json.dumps(next(reports)), "")

    monkeypatch.setattr(purplemux_client, "PurpleMuxRuntime", Runtime)
    monkeypatch.setattr(purplemux_client, "GitRepository", RepositoryType)
    monkeypatch.setattr(purplemux_client, "emit_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(purplemux_client.workflow, "start_child_run", start_child)
    monkeypatch.setattr(purplemux_client.workflow, "wait_child_run", wait_child)

    config = parse_review_fix_json(
        declaration(repository, max_iterations=3, timeout=30)
    )
    output = StringIO()
    with redirect_stdout(output):
        exec(compile(generate_review_fix_workflow(config), "<review-fix>", "exec"), {})

    result = json.loads(output.getvalue())
    assert result["verdict"] == "PASS"
    assert child_ids == [1, 2]
    assert all("http://127.0.0.1:3000/api/health" in code for code in review_codes)
    assert all(
        expected in review_codes[index]
        for index, expected in enumerate(
            (
                "managed PurpleMux workspace workspace, tab service-1",
                "managed PurpleMux workspace workspace, tab service-2",
            )
        )
    )
    assert len(client.prompts) == 2
    assert '"verdict": "FAIL"' in client.prompts[0]
    assert "finish with a clean worktree" in client.prompts[0]
    assert "Co-authored-by: Codex <noreply@openai.com>" in client.prompts[0]
    assert "AWM-Agent: codex" in client.prompts[0]
    assert "AWM-Process: implementation" in client.prompts[0]
    assert "worktree must be clean" in client.prompts[1]
    assert result["iterations"][0]["implementation_sha"] == "b" * 40
    assert "implementation" in client.closed
    assert client.services == 2
    assert "service-1" in client.closed
    assert "service-2" in client.closed
