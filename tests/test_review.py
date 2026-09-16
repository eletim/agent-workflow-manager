from __future__ import annotations

import ast
import json
import stat
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
    assert "if START is not None:" in code
    assert "if FINISH is not None:" in code
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


@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize("finish_unavailable", [False, True])
def test_generated_review_sequences_optional_turns_and_reports_result(
    repositories: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    oversized: bool,
    finish_unavailable: bool,
) -> None:
    import purplemux_client
    import purplemux_client.review as review_module

    messages: list[str] = []
    steps: list[tuple[str, str]] = []
    closed: list[str] = []

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
                        "observability_gaps": ["Production logs unavailable"],
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
    assert result["verdict"] == "FAIL"
    assert result["observed_facts"] == ["Request returned 500"]
    assert result["evidence"] == ["Browser response"]
    assert result["hypotheses"] == ["Handler omitted"]
    expected_gaps = ["Production logs unavailable"]
    if finish_unavailable:
        expected_gaps.append("Finish could not be confirmed: finish timed out")
    assert result["observability_gaps"] == expected_gaps
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
    assert closed == ["agent-tab"]


@pytest.mark.parametrize("unavailable", ["timeout", "result unavailable"])
def test_generated_review_unavailable_returns_blocked_and_runs_finish(
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
                raise WorkerFailure("result unavailable")

        def read_result(self, _tab: str) -> str:
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
        else "result unavailable"
    )
    assert expected in result["summary"]
    assert result["observability_gaps"] == [expected]
    assert "Close the observation" in messages[1]
    assert closed == ["agent-tab"]


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


def test_generated_review_reports_repository_change(
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
