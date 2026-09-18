from __future__ import annotations

import ast
import json
import os
import stat
import struct
import subprocess
import threading
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runner import request, wait_for

from purplemux_client.review import (
    ReviewWriteMonitor,
    generate_review_workflow,
    parse_review_json,
    require_ext_review_contract,
    serialize_review_result,
    snapshot_review_repositories,
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


def test_review_generates_valid_ordinary_workflow(
    repositories: tuple[Path, Path],
) -> None:
    config = parse_review_json(
        declaration(
            repositories,
            start="Read their README files.",
            finish="Summarize risks.",
            agent="claude-code",
            timeout=120,
        )
    )
    assert config.as_json() == {
        "mode": "review",
        "repositories": [str(path) for path in repositories],
        "check": "Inspect both repositories for missing error handling.",
        "start": "Read their README files.",
        "finish": "Summarize risks.",
        "agent": "claude-code",
        "timeout": 120,
    }
    code = generate_review_workflow(config)
    ast.parse(code)
    assert 'WORKFLOW_OUTLINE = ["Review"]' in code
    assert "PurpleMuxRuntime(owned_by_run=True)" in code
    assert "worker=AGENT" in code
    assert "if result is None and START is not None:" in code
    assert "if FINISH is not None and start_completed and check_completed:" in code
    assert "json.loads(report)" in code
    assert "any available browser tool" in code
    assert "PurpleMux CLI" in code
    assert "require_ext_review_contract" in code
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
    repositories: tuple[Path, Path],
    changes: dict[str, object],
    expected: str,
) -> None:
    with pytest.raises(ValueError, match=expected):
        parse_review_json(declaration(repositories, **changes))


def test_review_rejects_duplicate_or_nested_repository(
    repositories: tuple[Path, Path],
) -> None:
    with pytest.raises(ValueError, match="repeat"):
        parse_review_json(
            declaration(repositories, repositories=[str(repositories[0])] * 2)
        )
    nested = repositories[0] / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="repository root"):
        parse_review_json(declaration(repositories, repositories=[str(nested)]))
    source = declaration(repositories)
    with pytest.raises(ValueError, match="duplicate fields"):
        parse_review_json(
            source.replace('"mode": "review",', '"mode": "review", "mode": "review",')
        )


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
            address,
            "POST",
            "/api/review/generate",
            json.dumps({"json": source}),
            token=server.request_token,
        )
        assert status == 200
        assert generated["config"]["repositories"] == [
            str(path) for path in repositories
        ]
        assert runner.validate(generated["generatedCode"]).valid
        status, rejected = request(
            address,
            "POST",
            "/api/review/generate",
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


def test_generated_review_submits_with_identity_and_source_without_stubbed_generator(
    repositories: tuple[Path, Path], tmp_path: Path
) -> None:
    source = declaration(repositories)
    runner = PythonRunner(
        managed_workflows=False, run_history_file=tmp_path / "runs.json"
    )
    server = RunnerHTTPServer(("127.0.0.1", 0), runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = (str(server.server_address[0]), int(server.server_address[1]))
    try:
        status, generated = request(
            address,
            "POST",
            "/api/review/generate",
            json.dumps({"json": source}),
            token=server.request_token,
        )
        assert status == 200
        status, started = request(
            address,
            "POST",
            "/api/run",
            json.dumps({"code": generated["generatedCode"], "reviewJson": source}),
            token=server.request_token,
        )
        assert status == 202
        assert started["mode"] == "review"
        assert started["reviewJson"] == source
        assert started["code"] == generated["generatedCode"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        runner.close()


def test_review_run_binds_code_and_retains_source_in_history(
    repositories: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from purplemux_client import web

    source = declaration(repositories)
    code = 'print("review submitted")'
    monkeypatch.setattr(web, "generate_review_workflow", lambda _config: code)
    history = tmp_path / "runs.json"
    runner = PythonRunner(managed_workflows=False, run_history_file=history)
    server = RunnerHTTPServer(("127.0.0.1", 0), runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = (str(server.server_address[0]), int(server.server_address[1]))
    try:
        for payload, expected in (
            ({"code": code + "# changed", "reviewJson": source}, 400),
            ({"code": code, "reviewJson": declaration(repositories, check="")}, 422),
            ({"code": code, "reviewJson": None}, 400),
        ):
            status, _ = request(
                address,
                "POST",
                "/api/run",
                json.dumps(payload),
                token=server.request_token,
            )
            assert status == expected
        status, started = request(
            address,
            "POST",
            "/api/run",
            json.dumps({"code": code, "reviewJson": source}),
            token=server.request_token,
        )
        assert status == 202
        assert started["mode"] == "review"
        assert started["reviewJson"] == source
        run_id = started["runId"]
        wait_for(runner, lambda snapshot: snapshot.state == "success", run_id=run_id)
        status, detail = request(
            address, "GET", f"/api/runs/{run_id}", token=server.request_token
        )
        assert status == 200
        assert detail["mode"] == "review"
        assert detail["reviewJson"] == source
        assert detail["code"] == code
        status, listing = request(
            address, "GET", "/api/runs", token=server.request_token
        )
        assert status == 200
        assert listing["runs"][0]["mode"] == "review"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        runner.close()

    restored = PythonRunner(managed_workflows=False, run_history_file=history)
    try:
        snapshot = restored.snapshot(run_id).as_json()
        assert snapshot["mode"] == "review"
        assert snapshot["reviewJson"] == source
        assert snapshot["code"] == code
    finally:
        restored.close()


@pytest.mark.parametrize("verdict", ["PASS", "FAIL", "BLOCKED"])
def test_review_result_is_durable_and_independent_of_output(
    repositories: tuple[Path, Path], tmp_path: Path, verdict: str
) -> None:
    history = tmp_path / "runs.json"
    source = declaration(repositories)
    result = {
        "verdict": verdict,
        "summary": "Observed review decision",
        "repositories": [str(path) for path in repositories],
        "findings": ["Retained detail"],
    }
    code = (
        "import json, sys\n"
        "from purplemux_client.review import publish_review_result\n"
        f"publish_review_result({result!r})\n"
        "print('stdout is diagnostic, not the decision')\n"
        "print('stderr is diagnostic too', file=sys.stderr)\n"
    )
    runner = PythonRunner(
        managed_workflows=False, run_history_file=history, max_output_chars=16
    )
    try:
        run_id = runner.start(code, review_json=source)
        snapshot = wait_for(runner, lambda item: item.state == "success", run_id=run_id)
        assert snapshot.as_json()["reviewResult"] == result
        assert snapshot.stdout.endswith("the decision\n")
        assert snapshot.stderr.endswith("diagnostic too\n")
        assert json.dumps(result) not in snapshot.stdout + snapshot.stderr
    finally:
        runner.close()

    restored = PythonRunner(managed_workflows=False, run_history_file=history)
    try:
        restored_snapshot = restored.snapshot(run_id)
        assert restored_snapshot.as_json()["reviewResult"] == result
        assert restored_snapshot.stdout == snapshot.stdout
        assert restored_snapshot.stderr == snapshot.stderr
        assert (
            json.loads(history.read_text())["runs"][snapshot.identity]["reviewResult"]
            == result
        )
    finally:
        restored.close()


def test_review_result_requires_valid_report_and_successful_run(
    repositories: tuple[Path, Path], tmp_path: Path
) -> None:
    runner = PythonRunner(
        managed_workflows=False, run_history_file=tmp_path / "runs.json"
    )
    source = declaration(repositories)
    try:
        output_only = runner.start(
            'print("{\\"verdict\\": \\"PASS\\"}")', review_json=source
        )
        assert (
            wait_for(
                runner, lambda item: item.state == "success", run_id=output_only
            ).as_json()["reviewResult"]
            is None
        )

        invalid = runner.start(
            "from purplemux_client.review import publish_review_result\n"
            "publish_review_result({'verdict': 'PASS', 'summary': ''})\n",
            review_json=source,
        )
        assert (
            wait_for(
                runner, lambda item: item.state == "failed", run_id=invalid
            ).as_json()["reviewResult"]
            is None
        )

        failed = runner.start(
            "from purplemux_client.review import publish_review_result\n"
            "publish_review_result({'verdict': 'BLOCKED', 'summary': 'Unavailable', 'repositories': []})\n"
            "raise RuntimeError('after publish')\n",
            review_json=source,
        )
        assert (
            wait_for(
                runner, lambda item: item.state == "failed", run_id=failed
            ).as_json()["reviewResult"]
            is None
        )
    finally:
        runner.close()


@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize("finish_unavailable", [False, True])
@pytest.mark.parametrize("full_gaps", [False, True])
def test_generated_review_sequences_optional_turns_and_reports_result(
    repositories: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    oversized: bool,
    finish_unavailable: bool,
    full_gaps: bool,
) -> None:
    import purplemux_client
    import purplemux_client.review as review_module

    messages: list[str] = []
    steps: list[tuple[str, str]] = []
    closed: list[str] = []
    published: list[dict[str, object]] = []

    class Client:
        def create_session(self, request: object) -> str:
            assert request.worker == "codex"
            return "agent-tab"

        def wait_until_ready(self, _tab: str, _seconds: float) -> None:
            pass

        def send_input(self, _tab: str, message: str) -> None:
            messages.append(message)

        def wait_for_turn_completion(
            self, _tab: str, _seconds: float, **_kwargs: object
        ) -> None:
            if len(messages) == 3 and finish_unavailable:
                raise TimeoutError("finish timed out")

        def read_result(self, _tab: str) -> str:
            if len(messages) == 2:
                return json.dumps(
                    {
                        "verdict": "FAIL",
                        "summary": "Missing handling"
                        if not oversized
                        else "x" * 1_100_000,
                        "findings": ["A" if not oversized else "y" * 1_100_000],
                        "observed_facts": ["Request returned 500"],
                        "evidence": ["Browser response"],
                        "hypotheses": ["Handler omitted"],
                        "observability_gaps": (
                            [f"Existing gap {index}" for index in range(100)]
                            if full_gaps
                            else ["Production logs unavailable"]
                        ),
                    }
                )
            return "done"

        def close_session(self, tab: str) -> None:
            closed.append(tab)

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
        review_module,
        "require_ext_review_contract",
        lambda **_kwargs: "/usr/bin/purplemux",
    )
    monkeypatch.setattr(review_module, "publish_review_result", published.append)
    monkeypatch.setattr(
        purplemux_client,
        "emit_step",
        lambda name, state, **_kwargs: steps.append((name, state)),
    )
    config = parse_review_json(
        declaration(
            repositories,
            start="Survey layout",
            finish="Summarize findings",
        )
    )
    output = StringIO()
    with redirect_stdout(output):
        exec(compile(generate_review_workflow(config), "<review>", "exec"), {})
    assert len(messages) == 3
    assert "Survey layout" in messages[0]
    assert '"/usr/bin/purplemux" ext-review create' in messages[0]
    assert "Perform this check" in messages[1]
    assert "Summarize findings" in messages[2]
    retained = output.getvalue()
    assert len(retained) <= 1_000_000
    result = json.loads(retained)
    assert published == [result]
    assert result["verdict"] == "FAIL"
    assert result["observed_facts"] == ["Request returned 500"]
    assert result["evidence"] == ["Browser response"]
    assert result["hypotheses"] == ["Handler omitted"]
    expected_gaps = (
        [f"Existing gap {index}" for index in range(100)]
        if full_gaps
        else ["Production logs unavailable"]
    )
    if finish_unavailable:
        expected_gaps.insert(0, "Finish could not be confirmed: finish timed out")
    if full_gaps and finish_unavailable:
        expected_gaps.pop()
    assert result["observability_gaps"] == expected_gaps
    if full_gaps and finish_unavailable:
        assert result["truncated"]["observability_gaps"] == 1
    assert result["repositories"] == [str(path) for path in repositories]
    if oversized:
        expected_truncated = {
            "summary_chars": 1_100_000 - 16384,
            "finding_texts": 1,
        }
        if full_gaps and finish_unavailable:
            expected_truncated["observability_gaps"] = 1
        assert result["truncated"] == expected_truncated
        assert len(result["summary"]) == 16384
        assert len(result["findings"][0]) == 512
        assert len(messages[2]) < 1_000_000
    assert steps == [("Review", "started"), ("Review", "completed")]
    assert closed == ["agent-tab"]


@pytest.mark.parametrize(
    "unavailable", ["timeout", "busy timeout", "result unavailable"]
)
def test_generated_review_unavailable_returns_blocked_and_skips_finish(
    repositories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    import purplemux_client
    import purplemux_client.review as review_module
    from purplemux_client.errors import WorkerFailure

    messages: list[str] = []
    closed: list[str] = []

    class Client:
        def create_session(self, _request: object) -> str:
            return "agent-tab"

        def wait_until_ready(self, _tab: str, _seconds: float) -> None:
            pass

        def send_input(self, _tab: str, message: str) -> None:
            messages.append(message)

        def wait_for_turn_completion(
            self, _tab: str, _seconds: float, **_kwargs: object
        ) -> None:
            if len(messages) == 1:
                if unavailable == "timeout":
                    raise TimeoutError("observation deadline exceeded")
                if unavailable == "busy timeout":
                    _kwargs["on_busy_timeout"]("check tab remains busy")  # type: ignore[operator]
                    pytest.fail("Busy callback should stop the wait")
                if unavailable != "result unavailable":
                    raise WorkerFailure("result unavailable")

        def read_result(self, _tab: str) -> str:
            if len(messages) == 1 and unavailable == "result unavailable":
                raise WorkerFailure("result unavailable")
            return "finish complete"

        def close_session(self, tab: str) -> None:
            closed.append(tab)

    client = Client()

    class Runtime:
        def __init__(self, *, owned_by_run: bool) -> None:
            assert owned_by_run

        def create_workspace(self, _request: object) -> SimpleNamespace:
            return SimpleNamespace(id="review-workspace")

        def workspace(self, _workspace_id: str) -> Client:
            return client

    monkeypatch.setattr(purplemux_client, "PurpleMuxRuntime", Runtime)
    monkeypatch.setattr(
        review_module,
        "require_ext_review_contract",
        lambda **_kwargs: "/usr/bin/purplemux",
    )
    monkeypatch.setattr(purplemux_client, "emit_step", lambda *_args, **_kwargs: None)
    config = parse_review_json(
        declaration(repositories, finish="Close the observation")
    )
    output = StringIO()
    with redirect_stdout(output):
        exec(compile(generate_review_workflow(config), "<review>", "exec"), {})
    result = json.loads(output.getvalue())
    assert result["verdict"] == "BLOCKED"
    expected = (
        "observation deadline exceeded"
        if unavailable == "timeout"
        else "Review timed out while the agent was busy"
        if unavailable == "busy timeout"
        else "result unavailable"
    )
    assert expected in result["summary"]
    assert result["observability_gaps"] == (
        [expected]
        if unavailable == "result unavailable"
        else [
            "Finish could not run because check completion was not confirmed",
            expected,
        ]
    )
    assert len(messages) == (2 if unavailable == "result unavailable" else 1)
    if unavailable == "result unavailable":
        assert "Close the observation" in messages[1]
    assert closed == ["agent-tab"]


@pytest.mark.parametrize("unavailable", ["timeout", "result unavailable"])
@pytest.mark.parametrize("repository_changed", [False, True])
def test_generated_review_start_unavailable_blocks_without_check_or_finish(
    repositories: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    unavailable: str,
    repository_changed: bool,
) -> None:
    import purplemux_client
    import purplemux_client.review as review_module
    from purplemux_client.errors import WorkerFailure

    messages: list[str] = []
    closed: list[str] = []
    steps: list[tuple[str, str]] = []

    class Client:
        def create_session(self, _request: object) -> str:
            return "agent-tab"

        def wait_until_ready(self, _tab: str, _seconds: float) -> None:
            pass

        def send_input(self, _tab: str, message: str) -> None:
            messages.append(message)

        def wait_for_turn_completion(
            self, _tab: str, _seconds: float, **_kwargs: object
        ) -> None:
            if repository_changed:
                (repositories[1] / "changed.txt").write_text("changed during start")
            if unavailable == "timeout":
                raise TimeoutError("start observation timed out")
            raise WorkerFailure("start result unavailable")

        def close_session(self, tab: str) -> None:
            closed.append(tab)

    client = Client()

    class Runtime:
        def __init__(self, *, owned_by_run: bool) -> None:
            assert owned_by_run

        def create_workspace(self, _request: object) -> SimpleNamespace:
            return SimpleNamespace(id="review-workspace")

        def workspace(self, _workspace_id: str) -> Client:
            return client

    monkeypatch.setattr(purplemux_client, "PurpleMuxRuntime", Runtime)
    monkeypatch.setattr(
        review_module,
        "require_ext_review_contract",
        lambda **_kwargs: "/usr/bin/purplemux",
    )
    monkeypatch.setattr(
        purplemux_client,
        "emit_step",
        lambda name, state, **_kwargs: steps.append((name, state)),
    )
    config = parse_review_json(
        declaration(repositories, start="Start observation", finish="Close observation")
    )
    output = StringIO()
    with redirect_stdout(output):
        if repository_changed:
            with pytest.raises(RuntimeError, match="Review repository change detected"):
                exec(compile(generate_review_workflow(config), "<review>", "exec"), {})
        else:
            exec(compile(generate_review_workflow(config), "<review>", "exec"), {})
    if repository_changed:
        assert output.getvalue() == ""
        assert closed == ["agent-tab"]
        assert steps == [("Review", "started"), ("Review", "failed")]
        return
    result = json.loads(output.getvalue())
    assert result["verdict"] == "BLOCKED"
    assert "start observation" in result["summary"]
    assert result["observability_gaps"] == [
        "Start completion could not be confirmed: "
        + (
            "start observation timed out"
            if unavailable == "timeout"
            else "start result unavailable"
        )
    ]
    assert len(messages) == 1
    assert "Start observation" in messages[0]
    assert closed == ["agent-tab"]
    assert steps == [("Review", "started"), ("Review", "completed")]


@pytest.mark.parametrize("repository_changed", [False, True])
def test_generated_review_agent_readiness_timeout_returns_blocked(
    repositories: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    repository_changed: bool,
) -> None:
    import purplemux_client
    import purplemux_client.review as review_module
    from purplemux_client.errors import SessionReadyTimeout

    closed: list[str] = []
    steps: list[tuple[str, str]] = []

    class Client:
        def create_session(self, _request: object) -> str:
            return "agent-tab"

        def wait_until_ready(self, _tab: str, _seconds: float) -> None:
            if repository_changed:
                (repositories[1] / "changed.txt").write_text("changed during startup")
            raise SessionReadyTimeout("agent never became ready")

        def send_input(self, _tab: str, _message: str) -> None:
            pytest.fail("No turn should be sent to an unready agent")

        def close_session(self, tab: str) -> None:
            closed.append(tab)

    client = Client()

    class Runtime:
        def __init__(self, *, owned_by_run: bool) -> None:
            assert owned_by_run

        def create_workspace(self, _request: object) -> SimpleNamespace:
            return SimpleNamespace(id="review-workspace")

        def workspace(self, _workspace_id: str) -> Client:
            return client

    monkeypatch.setattr(purplemux_client, "PurpleMuxRuntime", Runtime)
    monkeypatch.setattr(
        review_module,
        "require_ext_review_contract",
        lambda **_kwargs: "/usr/bin/purplemux",
    )
    monkeypatch.setattr(
        purplemux_client,
        "emit_step",
        lambda name, state, **_kwargs: steps.append((name, state)),
    )
    config = parse_review_json(
        declaration(repositories, start="Start observation", finish="Close observation")
    )
    output = StringIO()
    with redirect_stdout(output):
        if repository_changed:
            with pytest.raises(RuntimeError, match="Review repository change detected"):
                exec(compile(generate_review_workflow(config), "<review>", "exec"), {})
        else:
            exec(compile(generate_review_workflow(config), "<review>", "exec"), {})
    if repository_changed:
        assert output.getvalue() == ""
        assert closed == ["agent-tab"]
        assert steps == [("Review", "started"), ("Review", "failed")]
        return
    result = json.loads(output.getvalue())
    assert result["verdict"] == "BLOCKED"
    assert "agent never became ready" in result["summary"]
    assert result["observability_gaps"] == [
        "Agent readiness could not be confirmed: agent never became ready"
    ]
    assert closed == ["agent-tab"]
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


def test_review_compacts_escaped_optional_arrays_without_losing_verdict() -> None:
    names = (
        "findings",
        "observed_facts",
        "evidence",
        "hypotheses",
        "observability_gaps",
    )
    report = {"verdict": "FAIL", "summary": "Observed a failure"}
    report.update({name: ["\0" * 512] * 100 for name in names})
    payload = serialize_review_result(report, ())
    result = json.loads(payload)
    assert len(payload) + 1 <= 1_000_000
    assert result["verdict"] == "FAIL"
    assert result["summary"] == "Observed a failure"
    assert sum(result["truncated"].get(name, 0) for name in names) > 0
    for name in names:
        assert result[name] == ["\0" * 512] * len(result[name])
        assert len(result[name]) + result["truncated"].get(name, 0) == 100


def test_review_compaction_preserves_finish_failure_with_escaped_arrays() -> None:
    other_entry = "\0" * 440 + "x" * 72
    report = {
        "verdict": "PASS",
        "summary": "The check passed",
        "findings": [other_entry] * 100,
        "observed_facts": [other_entry] * 100,
        "evidence": [other_entry] * 100,
        "hypotheses": [other_entry] * 100,
        "observability_gaps": ["\0" * 512] * 100,
    }
    failure = "Finish could not be confirmed: " + "\0" * 512
    payload = serialize_review_result(report, (), finish_failure=failure)
    result = json.loads(payload)
    assert len(payload) + 1 <= 1_000_000
    assert result["verdict"] == "PASS"
    assert len(result["observability_gaps"]) == 1
    assert result["observability_gaps"][0].startswith("Finish could not be confirmed: ")
    assert result["truncated"]["observability_gaps"] == 100


def test_review_snapshot_detects_changes_in_every_declared_repository(
    repositories: tuple[Path, Path],
) -> None:
    first, second = repositories
    (first / "tracked.txt").write_text("before")
    subprocess.run(["git", "-C", str(first), "add", "tracked.txt"], check=True)
    (second / ".gitignore").write_text("ignored.txt\n")
    (second / "ignored.txt").write_text("before")
    paths = tuple(map(str, repositories))
    baseline = snapshot_review_repositories(paths)
    (first / "tracked.txt").write_text("after")
    assert snapshot_review_repositories(paths)[0] != baseline[0]
    (first / "tracked.txt").write_text("before")
    (second / "ignored.txt").write_text("after")
    assert snapshot_review_repositories(paths)[1] != baseline[1]
    (second / "untracked.txt").write_text("new")
    assert snapshot_review_repositories(paths)[1] != baseline[1]


def test_review_snapshot_separates_file_records_and_covers_git_config(
    repositories: tuple[Path, Path],
) -> None:
    first = repositories[0]
    (first / "a").write_bytes(b"A")
    (first / "b").write_bytes(b"B")
    paths = tuple(map(str, repositories))
    baseline = snapshot_review_repositories(paths)
    mode = stat.S_IMODE((first / "b").stat().st_mode)
    (first / "a").write_bytes(b"Ab\0" + str(mode).encode() + b"fileB")
    (first / "b").unlink()
    assert snapshot_review_repositories(paths)[0] != baseline[0]
    (first / "a").write_bytes(b"A")
    (first / "b").write_bytes(b"B")
    subprocess.run(
        ["git", "-C", str(first), "config", "--local", "review.test", "enabled"],
        check=True,
    )
    assert snapshot_review_repositories(paths)[0] != baseline[0]


def test_review_snapshot_ignores_index_refresh_but_detects_index_state(
    repositories: tuple[Path, Path],
) -> None:
    repository = repositories[0]
    tracked = repository / "tracked.txt"
    tracked.write_text("original")
    subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
    os.utime(tracked, ns=(1_000_000_000, 1_000_000_000))
    baseline = snapshot_review_repositories((str(repository),))
    index = repository / ".git" / "index"
    before = index.read_bytes()
    subprocess.run(["git", "-C", str(repository), "status", "--short"], check=True)
    assert index.read_bytes() != before
    assert snapshot_review_repositories((str(repository),)) == baseline

    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "update-index",
            "--assume-unchanged",
            "tracked.txt",
        ],
        check=True,
    )
    assert snapshot_review_repositories((str(repository),)) != baseline


def test_review_snapshot_detects_intent_to_add_for_empty_file(
    repositories: tuple[Path, Path],
) -> None:
    repository = repositories[0]
    (repository / "empty.txt").touch()
    subprocess.run(["git", "-C", str(repository), "add", "empty.txt"], check=True)
    baseline = snapshot_review_repositories((str(repository),))
    subprocess.run(
        ["git", "-C", str(repository), "reset", "-q", "--", "empty.txt"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repository), "add", "-N", "empty.txt"], check=True)
    assert snapshot_review_repositories((str(repository),)) != baseline


def test_review_snapshot_covers_linked_worktree_git_config(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    linked = tmp_path / "linked"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Review Test",
            "-c",
            "user.email=review@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "--detach",
            "-q",
            str(linked),
        ],
        check=True,
    )
    baseline = snapshot_review_repositories((str(linked),))
    subprocess.run(
        ["git", "-C", str(linked), "config", "--local", "review.test", "enabled"],
        check=True,
    )
    assert snapshot_review_repositories((str(linked),)) != baseline


def test_review_snapshot_ignores_linked_worktree_index_refresh(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    linked = tmp_path / "linked"
    sibling = tmp_path / "sibling"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / "tracked.txt").write_text("original")
    subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Review Test",
            "-c",
            "user.email=review@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "--detach",
            "-q",
            str(sibling),
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "--detach",
            "-q",
            str(linked),
        ],
        check=True,
    )
    os.utime(linked / "tracked.txt", ns=(1_000_000_000, 1_000_000_000))
    os.utime(sibling / "tracked.txt", ns=(1_000_000_000, 1_000_000_000))
    baseline = snapshot_review_repositories((str(linked),))
    git_dir = Path(
        subprocess.run(
            ["git", "-C", str(linked), "rev-parse", "--absolute-git-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    before = (git_dir / "index").read_bytes()
    subprocess.run(["git", "-C", str(linked), "status", "--short"], check=True)
    assert (git_dir / "index").read_bytes() != before
    assert snapshot_review_repositories((str(linked),)) == baseline
    sibling_index = repository / ".git" / "worktrees" / sibling.name / "index"
    sibling_before = sibling_index.read_bytes()
    subprocess.run(["git", "-C", str(sibling), "status", "--short"], check=True)
    assert sibling_index.read_bytes() != sibling_before
    assert snapshot_review_repositories((str(linked),)) == baseline

    subprocess.run(
        ["git", "-C", str(sibling), "update-index", "--split-index"], check=True
    )
    assert list(sibling_index.parent.glob("sharedindex.*"))
    assert snapshot_review_repositories((str(linked),)) == baseline

    (linked / "tracked.txt").write_text("changed")
    assert snapshot_review_repositories((str(linked),)) != baseline
    (linked / "tracked.txt").write_text("original")
    subprocess.run(
        [
            "git",
            "-C",
            str(sibling),
            "update-index",
            "--assume-unchanged",
            "tracked.txt",
        ],
        check=True,
    )
    assert snapshot_review_repositories((str(linked),)) != baseline


def test_review_snapshot_detects_git_object_write(
    repositories: tuple[Path, Path],
) -> None:
    repository = repositories[0]
    baseline = snapshot_review_repositories((str(repository),))
    subprocess.run(
        ["git", "-C", str(repository), "hash-object", "-w", "--stdin"],
        input=b"new unreachable object",
        capture_output=True,
        check=True,
    )
    assert snapshot_review_repositories((str(repository),)) != baseline


def test_review_snapshot_detects_reflog_expiration(
    repositories: tuple[Path, Path],
) -> None:
    repository = repositories[0]
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Review Test",
            "-c",
            "user.email=review@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    baseline = snapshot_review_repositories((str(repository),))
    subprocess.run(
        ["git", "-C", str(repository), "reflog", "expire", "--expire=now", "--all"],
        check=True,
    )
    assert snapshot_review_repositories((str(repository),)) != baseline


def test_review_monitor_detects_restored_write_through_external_hard_link(
    repositories: tuple[Path, Path], tmp_path: Path
) -> None:
    repository = repositories[0]
    inside = repository / "existing.txt"
    inside.write_text("original")
    outside = tmp_path / "same-inode.txt"
    os.link(inside, outside)
    baseline = snapshot_review_repositories((str(repository),))
    monitor = ReviewWriteMonitor((str(repository),))
    try:
        outside.write_text("changed")
        outside.write_text("original")
        assert snapshot_review_repositories((str(repository),)) == baseline
        with pytest.raises(RuntimeError, match="Review repository change detected"):
            monitor.assert_unchanged()
    finally:
        monitor.close()


def test_review_monitor_ignores_attribute_only_event(
    repositories: tuple[Path, Path],
) -> None:
    path = repositories[0] / "existing.txt"
    path.write_text("original")
    monitor = ReviewWriteMonitor((str(repositories[0]),))
    try:
        os.utime(path, None)
        monitor.assert_unchanged()
    finally:
        monitor.close()


def test_review_monitor_ignores_transient_git_lock(
    repositories: tuple[Path, Path],
) -> None:
    repository = repositories[0]
    lock = repository / ".git" / "index.lock"
    monitor = ReviewWriteMonitor((str(repository),))
    try:
        lock.write_text("temporary Git inspection state")
        lock.unlink()
        monitor.assert_unchanged()
    finally:
        monitor.close()


def test_review_monitor_detects_restored_config_lock_write(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    monitor = object.__new__(ReviewWriteMonitor)
    monitor._fd = read_fd
    monitor._repositories = (str(tmp_path),)
    monitor._paths = {1: tmp_path}
    monitor._owners = {1: {str(tmp_path)}}
    monitor._git_dirs = {tmp_path}
    name = b"config.lock\0"
    try:
        os.write(
            write_fd,
            b"".join(
                struct.pack("iIII", 1, mask, 0, len(name)) + name
                for mask in (0x100, 0x002, 0x008, 0x200)
            ),
        )
        with pytest.raises(RuntimeError, match="Review repository change detected"):
            monitor.assert_unchanged()
    finally:
        monitor.close()
        os.close(write_fd)


def test_review_monitor_allows_index_metadata_refresh(
    repositories: tuple[Path, Path],
) -> None:
    repository = repositories[0]
    tracked = repository / "tracked.txt"
    tracked.write_text("original")
    subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
    os.utime(tracked, ns=(1_000_000_000, 1_000_000_000))
    baseline = snapshot_review_repositories((str(repository),))
    monitor = ReviewWriteMonitor((str(repository),))
    try:
        subprocess.run(["git", "-C", str(repository), "status", "--short"], check=True)
        monitor.assert_unchanged()
        assert snapshot_review_repositories((str(repository),)) == baseline
    finally:
        monitor.close()


@pytest.mark.parametrize("change", ["write", "create_delete", "move", "git_lock_move"])
def test_review_monitor_detects_real_changes_even_when_restored(
    repositories: tuple[Path, Path], change: str
) -> None:
    repository = repositories[0]
    original = repository / "existing.txt"
    original.write_text("original")
    monitor = ReviewWriteMonitor((str(repository),))
    try:
        if change == "write":
            original.write_text("changed")
            original.write_text("original")
        elif change == "create_delete":
            temporary = repository / "temporary.txt"
            temporary.write_text("new")
            temporary.unlink()
        elif change == "move":
            moved = repository / "moved.txt"
            original.rename(moved)
            moved.rename(original)
        else:
            lock = repository / ".git" / "config.lock"
            lock.write_text("new config")
            lock.rename(repository / ".git" / "config")
        with pytest.raises(RuntimeError, match="Review repository change detected"):
            monitor.assert_unchanged()
    finally:
        monitor.close()


@pytest.mark.parametrize(
    "mask,name",
    [
        (0x4000, b""),  # IN_Q_OVERFLOW
        (0x8000, b""),  # IN_IGNORED
        (0x100, b"index.lock"),  # Unpaired lock creation
        (0x002, b""),  # Content write
        (0x100, b"file.txt"),  # Ordinary creation
    ],
)
def test_review_monitor_fails_closed_on_unreliable_or_real_events(
    tmp_path: Path, mask: int, name: bytes
) -> None:
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    monitor = object.__new__(ReviewWriteMonitor)
    monitor._fd = read_fd
    monitor._repositories = (str(tmp_path),)
    monitor._paths = {1: tmp_path}
    monitor._owners = {1: {str(tmp_path)}}
    monitor._git_dirs = {tmp_path}
    try:
        os.write(write_fd, struct.pack("iIII", 1, mask, 0, len(name)) + name)
        with pytest.raises(RuntimeError, match="Review repository change detected"):
            monitor.assert_unchanged()
    finally:
        monitor.close()
        os.close(write_fd)


@pytest.mark.parametrize("sibling", [False, True])
def test_review_monitor_accepts_paired_git_lock_events(
    tmp_path: Path, sibling: bool
) -> None:
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    monitor = object.__new__(ReviewWriteMonitor)
    monitor._fd = read_fd
    monitor._repositories = (str(tmp_path),)
    git_dir = tmp_path / "worktrees" / "sibling" if sibling else tmp_path
    monitor._paths = {1: git_dir}
    monitor._owners = {1: {str(tmp_path)}}
    monitor._git_dirs = {tmp_path}
    name = b"index.lock\0"
    try:
        os.write(
            write_fd,
            b"".join(
                struct.pack("iIII", 1, mask, 0, len(name)) + name
                for mask in (0x100, 0x002, 0x008, 0x200)
            ),
        )
        monitor.assert_unchanged()
    finally:
        monitor.close()
        os.close(write_fd)


def test_review_monitor_rejects_sibling_config_lock_move(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    monitor = object.__new__(ReviewWriteMonitor)
    monitor._fd = read_fd
    monitor._repositories = (str(tmp_path),)
    monitor._paths = {1: tmp_path / "worktrees" / "sibling"}
    monitor._owners = {1: {str(tmp_path)}}
    monitor._git_dirs = {tmp_path}
    name = b"config.lock\0"
    try:
        os.write(
            write_fd,
            b"".join(
                struct.pack("iIII", 1, mask, 0, len(name)) + name
                for mask in (0x100, 0x002, 0x008, 0x040)
            ),
        )
        with pytest.raises(RuntimeError, match="Review repository change detected"):
            monitor.assert_unchanged()
    finally:
        monitor.close()
        os.close(write_fd)


@pytest.mark.parametrize("sibling", [False, True])
def test_review_monitor_accepts_index_replacement_events(
    tmp_path: Path, sibling: bool
) -> None:
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    monitor = object.__new__(ReviewWriteMonitor)
    monitor._fd = read_fd
    monitor._repositories = (str(tmp_path),)
    git_dir = tmp_path / "worktrees" / "sibling" if sibling else tmp_path
    monitor._paths = {1: git_dir, 2: git_dir / "index"}
    monitor._owners = {1: {str(tmp_path)}, 2: {str(tmp_path)}}
    monitor._git_dirs = {tmp_path}
    try:
        events = (
            (1, 0x100, b"index.lock\0"),
            (1, 0x008, b"index.lock\0"),
            (1, 0x040, b"index.lock\0"),
            (1, 0x080, b"index\0"),
            (2, 0x400, b""),
            (2, 0x8000, b""),
        )
        os.write(
            write_fd,
            b"".join(
                struct.pack("iIII", descriptor, mask, 0, len(name)) + name
                for descriptor, mask, name in events
            ),
        )
        monitor.assert_unchanged()
    finally:
        monitor.close()
        os.close(write_fd)


@pytest.mark.parametrize("location", ["common", "sibling", "worktree"])
def test_review_monitor_handles_sharedindex_housekeeping(
    tmp_path: Path, location: str
) -> None:
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    monitor = object.__new__(ReviewWriteMonitor)
    monitor._fd = read_fd
    monitor._repositories = (str(tmp_path),)
    parent = (
        tmp_path / "worktrees" / "sibling"
        if location == "sibling"
        else tmp_path / location
    )
    name = b"sharedindex.0123456789abcdef\0"
    monitor._paths = {1: parent, 2: parent / name[:-1].decode()}
    monitor._owners = {1: {str(tmp_path)}, 2: {str(tmp_path)}}
    monitor._git_dirs = {tmp_path / "common", tmp_path}
    try:
        events = (
            (1, 0x100, name),
            (1, 0x002, name),
            (1, 0x008, name),
            (1, 0x200, name),
            (2, 0x400, b""),
            (2, 0x8000, b""),
        )
        os.write(
            write_fd,
            b"".join(
                struct.pack("iIII", descriptor, mask, 0, len(event_name)) + event_name
                for descriptor, mask, event_name in events
            ),
        )
        if location == "worktree":
            with pytest.raises(RuntimeError, match="Review repository change detected"):
                monitor.assert_unchanged()
        else:
            monitor.assert_unchanged()
    finally:
        monitor.close()
        os.close(write_fd)


@pytest.mark.parametrize("restored", [False, True])
def test_generated_review_reports_repository_change(
    repositories: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    restored: bool,
) -> None:
    import purplemux_client
    import purplemux_client.review as review_module

    steps: list[tuple[str, str, str | None]] = []
    restored_path = repositories[1] / "existing.txt"
    if restored:
        restored_path.write_text("original")
    baseline = snapshot_review_repositories(tuple(map(str, repositories)))

    class Client:
        def create_session(self, _request: object) -> str:
            return "agent-tab"

        def wait_until_ready(self, _tab: str, _seconds: float) -> None:
            pass

        def send_input(self, _tab: str, _message: str) -> None:
            if restored:
                restored_path.write_text("changed")
                restored_path.write_text("original")
            else:
                (repositories[1] / "new.txt").write_text("changed")

        def wait_for_turn_completion(
            self, _tab: str, _seconds: float, **_kwargs: object
        ) -> None:
            pass

        def read_result(self, _tab: str) -> str:
            return json.dumps({"verdict": "PASS", "summary": "Looks good"})

        def close_session(self, _tab: str) -> None:
            pass

    class Runtime:
        def __init__(self, *, owned_by_run: bool) -> None:
            assert owned_by_run

        def create_workspace(self, _request: object) -> SimpleNamespace:
            return SimpleNamespace(id="review-workspace")

        def workspace(self, _workspace_id: str) -> Client:
            return Client()

    monkeypatch.setattr(purplemux_client, "PurpleMuxRuntime", Runtime)
    monkeypatch.setattr(
        review_module,
        "require_ext_review_contract",
        lambda **_kwargs: "/usr/bin/purplemux",
    )
    monkeypatch.setattr(
        purplemux_client,
        "emit_step",
        lambda name, state, **kwargs: steps.append((name, state, kwargs.get("error"))),
    )
    config = parse_review_json(declaration(repositories))
    with pytest.raises(RuntimeError, match="Review repository change detected"):
        exec(compile(generate_review_workflow(config), "<review>", "exec"), {})
    assert steps[0][:2] == ("Review", "started")
    assert steps[-1][:2] == ("Review", "failed")
    assert str(repositories[1]) in (steps[-1][2] or "")
    if restored:
        assert snapshot_review_repositories(tuple(map(str, repositories))) == baseline


def test_generated_review_verifies_after_agent_session_closes(
    repositories: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import purplemux_client
    import purplemux_client.review as review_module

    steps: list[tuple[str, str, str | None]] = []

    class Client:
        def create_session(self, _request: object) -> str:
            return "agent-tab"

        def wait_until_ready(self, _tab: str, _seconds: float) -> None:
            pass

        def send_input(self, _tab: str, _message: str) -> None:
            pass

        def wait_for_turn_completion(
            self, _tab: str, _seconds: float, **_kwargs: object
        ) -> None:
            pass

        def read_result(self, _tab: str) -> str:
            return json.dumps({"verdict": "PASS", "summary": "Looks good"})

        def close_session(self, _tab: str) -> None:
            (repositories[1] / "late.txt").write_text("changed during close")

    class Runtime:
        def __init__(self, *, owned_by_run: bool) -> None:
            assert owned_by_run

        def create_workspace(self, _request: object) -> SimpleNamespace:
            return SimpleNamespace(id="review-workspace")

        def workspace(self, _workspace_id: str) -> Client:
            return Client()

    monkeypatch.setattr(purplemux_client, "PurpleMuxRuntime", Runtime)
    monkeypatch.setattr(
        review_module,
        "require_ext_review_contract",
        lambda **_kwargs: "/usr/bin/purplemux",
    )
    monkeypatch.setattr(
        purplemux_client,
        "emit_step",
        lambda name, state, **kwargs: steps.append((name, state, kwargs.get("error"))),
    )
    code = generate_review_workflow(parse_review_json(declaration(repositories)))
    with pytest.raises(RuntimeError, match="Review repository change detected"):
        exec(compile(code, "<review>", "exec"), {})
    assert steps[-1][:2] == ("Review", "failed")
    assert str(repositories[1]) in (steps[-1][2] or "")


def test_review_requires_matching_cli_and_server_ext_review_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import purplemux_client.review as review_module

    monkeypatch.setattr(
        review_module.shutil, "which", lambda _name: "/usr/bin/purplemux"
    )
    calls: list[list[str]] = []

    def run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(args)
        output = (
            "ext-review create --socket PATH --session SESSION --window @ID"
            if args[-1] == "help"
            else "POST /api/cli/ext-reviews"
        )
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(review_module.subprocess, "run", run)
    assert require_ext_review_contract() == "/usr/bin/purplemux"
    assert calls == [
        ["/usr/bin/purplemux", "help"],
        ["/usr/bin/purplemux", "api-guide"],
    ]

    def old_server(args: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout="ext-review create --socket PATH"
            if args[-1] == "help"
            else "old API",
        )

    monkeypatch.setattr(review_module.subprocess, "run", old_server)
    with pytest.raises(RuntimeError, match="matching CLI"):
        require_ext_review_contract()


def test_generated_review_rejects_old_runtime_before_creating_workspace(
    repositories: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import purplemux_client
    import purplemux_client.review as review_module

    steps: list[tuple[str, str]] = []

    class Runtime:
        def __init__(self, *, owned_by_run: bool) -> None:
            assert owned_by_run

        def create_workspace(self, _request: object) -> None:
            pytest.fail("Review created a workspace without ext-review support")

    def unsupported(**_kwargs: object) -> str:
        raise RuntimeError("matching CLI with public ext-review support required")

    monkeypatch.setattr(purplemux_client, "PurpleMuxRuntime", Runtime)
    monkeypatch.setattr(review_module, "require_ext_review_contract", unsupported)
    monkeypatch.setattr(
        purplemux_client,
        "emit_step",
        lambda name, state, **_kwargs: steps.append((name, state)),
    )
    code = generate_review_workflow(parse_review_json(declaration(repositories)))
    with pytest.raises(RuntimeError, match="ext-review support required"):
        exec(compile(code, "<review>", "exec"), {})
    assert steps == [("Review", "started"), ("Review", "failed")]
