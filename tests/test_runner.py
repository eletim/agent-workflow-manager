from __future__ import annotations

import ast
import http.client
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest

import purplemux_client.runner as runner_module
import purplemux_client.web as web_module
from purplemux_client import WorkspaceState
from purplemux_client.errors import MutationOutcomeUnknown
from purplemux_client.notification_settings import NotificationSettings
from purplemux_client.notifier import NotificationResult
from purplemux_client.preflight import WorkflowValidator
from purplemux_client.prompt import (
    PromptExecution,
    build_prompt_workflow,
    prepare_prompt_execution,
)
from purplemux_client.readiness import (
    AgentReadinessStatus,
    ReadinessReconciliationRequired,
)
from purplemux_client.runner import (
    InvalidExecutionContextError,
    PythonRunner,
    RunCleanupNotAllowedError,
    RunnerClosedError,
    RunnerSnapshot,
    RunResource,
    TopologyFinding,
)
from purplemux_client.web import (
    RunnerHTTPServer,
    build_parser,
    list_directory,
    mobile_connection_url,
)


@pytest.fixture
def runner() -> Iterator[PythonRunner]:
    instance = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    yield instance
    instance.close()


def wait_for(
    runner: PythonRunner,
    predicate: Callable[[RunnerSnapshot], bool],
    *,
    timeout: float = 5,
    run_id: int | None = None,
) -> RunnerSnapshot:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = runner.snapshot(run_id)
        if predicate(snapshot):
            return snapshot
        time.sleep(0.02)
    raise AssertionError(
        f"runner did not reach expected state: {runner.snapshot(run_id)}"
    )


def wait_until_finished(runner: PythonRunner) -> RunnerSnapshot:
    return wait_for(runner, lambda snapshot: snapshot.state != "running")


def test_simple_stdout(runner: PythonRunner) -> None:
    runner.start('print("HELLO_RUNNER")')

    result = wait_until_finished(runner)

    assert result.state == "success"
    assert result.stdout == "HELLO_RUNNER\n"
    assert result.stderr == ""


def test_obsolete_recovery_metadata_is_ignored(
    runner: PythonRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "PURPLEMUX_RUNNER_RESUME_CHECKPOINT",
        '{"name":"old","data":{"workspace":"ws-old"}}',
    )
    code = """\
import os
print(os.environ.get("PURPLEMUX_RUNNER_RESUME_CHECKPOINT", "ignored"))
"""

    run_id = runner.start(code)
    result = wait_until_finished(runner)

    assert result.run_id == run_id
    assert result.state == "success"
    assert result.stdout == "ignored\n"
    assert not hasattr(runner, "resume")
    assert (
        PythonRunner._parse_runner_event('{"type":"checkpoint","name":"old","data":{}}')
        is None
    )
    assert "".join(entry.text for entry in result.stdout_entries) == result.stdout
    assert result.stderr_entries == ()
    observed_at = datetime.fromisoformat(result.stdout_entries[0].observed_at)
    assert observed_at.tzinfo is not None
    assert result.exit_code == 0


def test_runner_correlation_is_stable_within_run_and_unique_across_runs(
    runner: PythonRunner,
) -> None:
    code = """
from purplemux_client import run_correlation
print(run_correlation("workspace"))
print(run_correlation("workspace"))
"""
    first_id = runner.start(code)
    first = wait_for(
        runner, lambda snapshot: snapshot.state != "running", run_id=first_id
    )
    second_id = runner.start(code)
    second = wait_for(
        runner, lambda snapshot: snapshot.state != "running", run_id=second_id
    )

    first_values = first.stdout.splitlines()
    second_values = second.stdout.splitlines()
    assert first_values[0] == first_values[1]
    assert second_values[0] == second_values[1]
    assert first_values[0] != second_values[0]


def test_terminal_run_checked_metadata_is_reversible_and_run_scoped(
    runner: PythonRunner,
) -> None:
    first_id = runner.start("print('first')")
    first = wait_for(runner, lambda item: item.state == "success", run_id=first_id)
    assert first.checked is False
    assert first.as_json()["checked"] is False

    checked = runner.set_checked(first_id, True)
    assert checked.checked is True
    assert checked.state == "success"

    second_id = runner.start("print('second')")
    second = wait_for(runner, lambda item: item.state == "success", run_id=second_id)
    assert second.checked is False
    assert runner.snapshot(first_id).checked is True
    assert [item.as_summary_json()["checked"] for item in runner.snapshots()] == [
        True,
        False,
    ]

    unchecked = runner.set_checked(first_id, False)
    assert unchecked.checked is False
    assert unchecked.state == "success"


def test_delete_checked_runs_removes_only_checked_terminal_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history_file = tmp_path / "run-history.json"
    runner = PythonRunner(
        managed_workflows=False,
        stop_timeout=0.5,
        run_history_file=history_file,
    )
    try:
        checked_id = runner.start("print('checked')")
        wait_for(runner, lambda item: item.state == "success", run_id=checked_id)
        runner.set_checked(checked_id, True)

        unchecked_id = runner.start("print('unchecked')")
        wait_for(runner, lambda item: item.state == "success", run_id=unchecked_id)

        active_id = runner.start("import time; time.sleep(60)")
        with runner._lock:
            # Exercise the backend's final state check even for inconsistent
            # metadata that cannot be produced through the public API.
            runner._runs[active_id].checked = True

        def unexpected_cleanup(_run_id: int) -> None:
            raise AssertionError("history deletion must not clean run resources")

        monkeypatch.setattr(runner, "cleanup", unexpected_cleanup)
        with pytest.raises(
            runner_module.RunDeletionNotAllowedError, match="refresh and confirm"
        ):
            runner.delete_checked_runs((checked_id, unchecked_id))
        assert runner.snapshot(checked_id).checked is True
        assert runner.delete_checked_runs((checked_id,)) == (checked_id,)
        assert [item.run_id for item in runner.snapshots()] == [unchecked_id, active_id]
        assert runner.snapshot(unchecked_id).checked is False
        assert runner.snapshot(active_id).state == "running"
        assert checked_id not in {
            run["runId"]
            for run in json.loads(history_file.read_text(encoding="utf-8"))[
                "runs"
            ].values()
        }
        with runner._lock:
            runner._runs[active_id].checked = False
    finally:
        runner.close()


def test_delete_checked_runs_rolls_back_when_history_cannot_be_saved(
    runner: PythonRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = runner.start("print('keep me')")
    wait_for(runner, lambda item: item.state == "success", run_id=run_id)
    runner.set_checked(run_id, True)

    def fail_write() -> None:
        raise runner_module.RunHistoryError("save failed")

    monkeypatch.setattr(runner, "_write_run_history_locked", fail_write)
    with pytest.raises(runner_module.RunHistoryError, match="save failed"):
        runner.delete_checked_runs((run_id,))

    assert runner.snapshot(run_id).checked is True


def test_delete_checked_runs_rejects_concurrent_cleanup(
    runner: PythonRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = runner.start(
        """
from purplemux_client import register_run_resource
register_run_resource("purplemux_tab", "tab-1", {"workspace_id": "ws-1"})
"""
    )
    wait_for(runner, lambda item: item.state == "success", run_id=run_id)
    runner.set_checked(run_id, True)
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    cleanup_results: list[RunnerSnapshot] = []
    cleanup_errors: list[BaseException] = []

    def blocking_cleanup(_resource: RunResource) -> None:
        cleanup_entered.set()
        if not allow_cleanup.wait(timeout=5):
            raise AssertionError("cleanup test was not released")

    def cleanup() -> None:
        try:
            cleanup_results.append(runner.cleanup(run_id))
        except BaseException as exc:
            cleanup_errors.append(exc)

    monkeypatch.setattr(runner, "_cleanup_resource", blocking_cleanup)
    cleanup_thread = threading.Thread(target=cleanup)
    cleanup_thread.start()
    try:
        assert cleanup_entered.wait(timeout=5)
        with pytest.raises(
            runner_module.RunDeletionNotAllowedError, match="cleanup is active"
        ):
            runner.delete_checked_runs((run_id,))
        during_cleanup = runner.snapshot(run_id)
        assert during_cleanup.resources[0].cleanup_state == "cleanup_pending"
    finally:
        allow_cleanup.set()
        cleanup_thread.join(timeout=5)

    assert not cleanup_thread.is_alive()
    assert cleanup_errors == []
    assert cleanup_results[0].resources[0].cleanup_state == "cleaned"
    assert runner.delete_checked_runs((run_id,)) == (run_id,)


def test_delete_checked_runs_retains_resource_ownership_until_fully_cleaned(
    runner: PythonRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = runner.start(
        """
from purplemux_client import register_run_resource
register_run_resource("purplemux_tab", "tab-1", {"workspace_id": "ws-1"})
"""
    )
    wait_for(runner, lambda item: item.state == "success", run_id=run_id)
    runner.set_checked(run_id, True)

    with pytest.raises(runner_module.RunDeletionNotAllowedError, match="fully cleaned"):
        runner.delete_checked_runs((run_id,))
    assert runner.snapshot(run_id).resources[0].cleanup_state == "retained"

    monkeypatch.setattr(runner, "_cleanup_resource", lambda _resource: None)
    cleaned = runner.cleanup(run_id)
    assert cleaned.resources[0].cleanup_state == "cleaned"
    assert runner.delete_checked_runs((run_id,)) == (run_id,)


def test_checked_terminal_run_is_restored_after_runner_reconstruction(
    tmp_path: Path,
) -> None:
    history_file = tmp_path / "state" / "run-history.json"
    first_runner = PythonRunner(
        managed_workflows=False,
        stop_timeout=0.5,
        run_history_file=history_file,
    )
    try:
        first_id = first_runner.start("print('persisted output')")
        finished = wait_for(
            first_runner, lambda item: item.state == "success", run_id=first_id
        )
        stable_identity = first_runner._run_identity(first_id)
        assert finished.identity == stable_identity
        assert finished.checked is False
        first_runner.set_checked(first_id, True)
        saved = json.loads(history_file.read_text(encoding="utf-8"))
        assert list(saved["runs"]) == [stable_identity]
    finally:
        first_runner.close()

    second_runner = PythonRunner(
        managed_workflows=False,
        stop_timeout=0.5,
        run_history_file=history_file,
    )
    try:
        restored = second_runner.snapshot(first_id)
        assert second_runner._run_identity(first_id) == stable_identity
        assert restored.identity == stable_identity
        assert restored.as_summary_json()["identity"] == stable_identity
        assert restored.state == "success"
        assert restored.checked is True
        assert restored.stdout == "persisted output\n"
        assert [item.run_id for item in second_runner.snapshots()] == [first_id]

        second_id = second_runner.start("print('new run')")
        assert second_id == first_id + 1
        second = wait_for(
            second_runner, lambda item: item.state == "success", run_id=second_id
        )
        assert second.checked is False
        assert second_runner.snapshot(first_id).checked is True
    finally:
        second_runner.close()

    assert stat.S_IMODE(history_file.stat().st_mode) == 0o600


def test_run_history_lock_prevents_two_runners_from_overwriting_shared_state(
    tmp_path: Path,
) -> None:
    history_file = tmp_path / "run-history.json"
    owner = PythonRunner(
        managed_workflows=False,
        stop_timeout=0.5,
        run_history_file=history_file,
    )
    try:
        run_id = owner.start("print('owner')")
        wait_for(owner, lambda item: item.state == "success", run_id=run_id)
        owner.set_checked(run_id, True)

        with pytest.raises(
            runner_module.RunHistoryError,
            match="another AWM server owns",
        ):
            PythonRunner(
                managed_workflows=False,
                stop_timeout=0.5,
                run_history_file=history_file,
            )

        assert owner.snapshot(run_id).checked is True
    finally:
        owner.close()

    successor = PythonRunner(
        managed_workflows=False,
        stop_timeout=0.5,
        run_history_file=history_file,
    )
    try:
        restored = successor.snapshot(run_id)
        assert restored.stdout == "owner\n"
        assert restored.checked is True
        assert successor.start("print('successor')") == run_id + 1
    finally:
        successor.close()


def test_run_id_is_durably_reserved_before_launch_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history_file = tmp_path / "run-history.json"
    crashed_identity: str | None = None
    crashing_runner = PythonRunner(
        managed_workflows=False,
        stop_timeout=0.5,
        run_history_file=history_file,
    )

    class SimulatedCrash(BaseException):
        pass

    def crash_before_process(*_args: object, **_kwargs: object) -> None:
        raise SimulatedCrash

    monkeypatch.setattr(crashing_runner, "_spawn_process", crash_before_process)
    try:
        crashed_identity = crashing_runner._run_identity(1)
        with pytest.raises(SimulatedCrash):
            crashing_runner.start("print('never launched')")
        saved = json.loads(history_file.read_text(encoding="utf-8"))
        assert saved["nextRunId"] == 2
        assert saved["runs"] == {}
    finally:
        crashing_runner.close()

    reconstructed = PythonRunner(
        managed_workflows=False,
        stop_timeout=0.5,
        run_history_file=history_file,
    )
    try:
        run_id = reconstructed.start("print('after crash')")
        assert run_id == 2
        assert reconstructed._run_identity(run_id) != crashed_identity
        wait_for(reconstructed, lambda item: item.state == "success", run_id=run_id)
    finally:
        reconstructed.close()


def test_failed_run_id_reservation_prevents_launch_and_consumes_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = PythonRunner(
        managed_workflows=False,
        stop_timeout=0.5,
        run_history_file=tmp_path / "run-history.json",
    )
    original_write = runner._write_run_history_locked
    original_spawn = runner._spawn_process
    spawn_calls = 0

    def fail_reservation() -> None:
        raise runner_module.RunHistoryError("reservation failed")

    def track_spawn(*args: object, **kwargs: object) -> object:
        nonlocal spawn_calls
        spawn_calls += 1
        return original_spawn(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_write_run_history_locked", fail_reservation)
    monkeypatch.setattr(runner, "_spawn_process", track_spawn)
    try:
        with pytest.raises(runner_module.RunHistoryError, match="reservation failed"):
            runner.start("print('not launched')")
        assert spawn_calls == 0

        monkeypatch.setattr(runner, "_write_run_history_locked", original_write)
        run_id = runner.start("print('launched')")
        assert run_id == 2
        assert spawn_calls == 1
        wait_for(runner, lambda item: item.state == "success", run_id=run_id)
    finally:
        runner.close()


@pytest.mark.parametrize("terminal_state", ["success", "failed", "stopped"])
def test_checked_metadata_does_not_change_terminal_state(
    runner: PythonRunner, terminal_state: str
) -> None:
    code = {
        "success": "print('done')",
        "failed": "raise RuntimeError('failed')",
        "stopped": "import time; time.sleep(60)",
    }[terminal_state]
    run_id = runner.start(code)
    if terminal_state == "stopped":
        assert runner.stop(run_id) is True
    terminal = wait_for(
        runner, lambda item: item.state == terminal_state, run_id=run_id
    )

    checked = runner.set_checked(run_id, True)

    assert checked.state == terminal.state
    assert checked.exit_code == terminal.exit_code
    assert checked.checked is True


def test_new_runner_instance_does_not_reuse_run_correlation() -> None:
    first_runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    second_runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    code = 'from purplemux_client import run_correlation; print(run_correlation("workspace"))'
    try:
        first_id = first_runner.start(code)
        second_id = second_runner.start(code)
        first = wait_for(
            first_runner,
            lambda snapshot: snapshot.state != "running",
            run_id=first_id,
        )
        second = wait_for(
            second_runner,
            lambda snapshot: snapshot.state != "running",
            run_id=second_id,
        )
    finally:
        first_runner.close()
        second_runner.close()

    assert first.stdout != second.stdout


def test_prompt_workflow_uses_direct_unowned_structured_runtime_path(
    tmp_path: Path,
) -> None:
    execution = prepare_prompt_execution(
        agent="claude-code", cwd=str(tmp_path), prompt='Review "this".\nBe concise.'
    )

    code = build_prompt_workflow(execution)

    assert "runtime = PurpleMuxRuntime()" in code
    assert "owned_by_run" not in code
    assert "runtime.create_workspace(" in code
    assert "client = runtime.workspace(workspace.id)" in code
    assert "client.create_session(" in code
    assert "client.wait_for_turn_completion(tab, 3600)" in code
    assert "result = client.read_result(tab)" in code
    assert "client.interrupt(tab)" in code
    assert "capture_screen" not in code
    assert "close_session" not in code
    assert "delete_workspace" not in code
    assert json.dumps(execution.cwd) in code
    assert json.dumps(execution.prompt) in code
    validation = WorkflowValidator().validate(code)
    assert validation.valid
    assert validation.outline == ("Prompt",)


def test_prepare_prompt_execution_retains_validated_github_identity(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://github.com/acme/widgets.git",
        ],
        check=True,
    )

    execution = prepare_prompt_execution(
        agent="codex", cwd=str(tmp_path), prompt="Work"
    )

    assert execution.repository_json() == {
        "slug": "acme/widgets",
        "url": "https://github.com/acme/widgets",
    }


@pytest.mark.parametrize(
    ("agent", "cwd", "prompt", "message"),
    [
        ("other", ".", "work", "agent must be"),
        ("codex", "", "work", "cwd must be"),
        ("codex", ".", "  ", "prompt must not be empty"),
    ],
)
def test_prompt_input_validation(
    agent: str, cwd: str, prompt: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        prepare_prompt_execution(agent=agent, cwd=cwd, prompt=prompt)


def test_prompt_run_hides_generated_code_and_rejects_workflow_cleanup(
    runner: PythonRunner, tmp_path: Path
) -> None:
    execution = PromptExecution("codex", str(tmp_path), "answer")
    run_id = runner.start("print('structured answer')", prompt=execution)
    result = wait_for(
        runner, lambda snapshot: snapshot.state == "success", run_id=run_id
    )

    payload = result.as_json()
    assert payload["mode"] == "prompt"
    assert payload["prompt"] == execution.as_json()
    assert payload["code"] is None
    assert payload["cwd"] == str(tmp_path)
    assert payload["resources"] == []
    assert payload["cleanupAvailable"] is False
    summary = result.as_summary_json()
    assert summary["mode"] == "prompt"
    assert summary["prompt"] == {"agent": "codex", "cwd": str(tmp_path)}
    with pytest.raises(RunCleanupNotAllowedError, match="Prompt run"):
        runner.cleanup(run_id)


def test_prompt_repository_navigation_survives_run_history(
    tmp_path: Path,
) -> None:
    history_file = tmp_path / "run-history.json"
    execution = PromptExecution(
        "codex",
        str(tmp_path),
        "answer",
        repository_slug="acme/widgets",
        repository_url="https://github.com/acme/widgets",
    )
    first = PythonRunner(
        managed_workflows=False,
        stop_timeout=0.5,
        run_history_file=history_file,
    )
    try:
        run_id = first.start("print('done')", prompt=execution)
        result = wait_for(first, lambda item: item.state == "success", run_id=run_id)
        assert result.as_json()["repository"] == {
            "slug": "acme/widgets",
            "url": "https://github.com/acme/widgets",
        }
    finally:
        first.close()

    restored = PythonRunner(
        managed_workflows=False,
        stop_timeout=0.5,
        run_history_file=history_file,
    )
    try:
        payload = restored.snapshot(run_id).as_json()
        assert payload["repository"] == {
            "slug": "acme/widgets",
            "url": "https://github.com/acme/widgets",
        }
    finally:
        restored.close()


def test_run_owned_resources_are_retained_structurally_after_success(
    runner: PythonRunner,
) -> None:
    runner.start(
        """
from purplemux_client import register_run_resource
register_run_resource("purplemux_workspace", "ws-1", {"name": "Owned"})
register_run_resource("purplemux_tab", "tab-1", {
    "workspace_id": "ws-1", "name": "Agent", "panel_type": "codex-cli"
})
register_run_resource("purplemux_tab", "tab-1", {
    "workspace_id": "ws-1", "name": "Agent", "panel_type": "codex-cli"
})
"""
    )

    result = wait_until_finished(runner)

    assert result.state == "success"
    assert [(item.kind, item.identity) for item in result.resources] == [
        ("purplemux_workspace", "ws-1"),
        ("purplemux_tab", "tab-1"),
    ]
    assert result.resources[1].metadata["workspace_id"] == "ws-1"
    assert result.resources[1].cleanup_state == "retained"
    assert result.as_json()["resourceCleanupStatus"] == "retained"


def test_explicit_cleanup_uses_dependency_order_and_keeps_run_history(
    runner: PythonRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.start(
        """
from purplemux_client import register_run_resource
register_run_resource("purplemux_workspace", "ws-1", {"name": "Owned"})
register_run_resource("purplemux_tab", "tab-1", {"workspace_id": "ws-1"})
register_run_resource("purplemux_tab", "tab-2", {"workspace_id": "ws-1"})
register_run_resource("git_worktree", "/tmp/worktree", {"repository": "/tmp/repo"})
"""
    )
    result = wait_until_finished(runner)
    cleaned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner,
        "_cleanup_resource",
        lambda resource: cleaned.append((resource.kind, resource.identity)),
    )

    after = runner.cleanup(result.run_id or 0)

    assert cleaned == [
        ("purplemux_tab", "tab-2"),
        ("purplemux_tab", "tab-1"),
        ("purplemux_workspace", "ws-1"),
        ("git_worktree", "/tmp/worktree"),
    ]
    assert all(item.cleanup_state == "cleaned" for item in after.resources)
    assert after.as_json()["resourceCleanupStatus"] == "cleaned"
    assert runner.snapshot(result.run_id).state == "success"


def test_cleanup_aggregates_sibling_failures_and_blocks_parent_resources(
    runner: PythonRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.start(
        """
from purplemux_client import register_run_resource
register_run_resource("purplemux_workspace", "ws-1", {"name": "Owned"})
register_run_resource("purplemux_tab", "tab-1", {"workspace_id": "ws-1"})
register_run_resource("purplemux_tab", "tab-2", {"workspace_id": "ws-1"})
"""
    )
    result = wait_until_finished(runner)
    attempted: list[str] = []

    def cleanup(resource: object) -> None:
        identity = resource.identity  # type: ignore[attr-defined]
        attempted.append(identity)
        if identity == "tab-1":
            raise MutationOutcomeUnknown("close outcome could not be confirmed")

    monkeypatch.setattr(runner, "_cleanup_resource", cleanup)

    after = runner.cleanup(result.run_id or 0)

    assert attempted == ["tab-2", "tab-1"]
    states = {item.identity: item.cleanup_state for item in after.resources}
    assert states == {"ws-1": "retained", "tab-1": "blocked", "tab-2": "cleaned"}
    assert after.as_json()["resourceCleanupStatus"] == "blocked"
    assert "could not be confirmed" in str(after.resources[1].cleanup_error)
    monkeypatch.setattr(runner, "_resource_is_absent", lambda _resource: False)
    reconciled = runner.cleanup(result.run_id or 0)
    assert reconciled.as_json()["resourceCleanupStatus"] == "blocked"
    assert attempted == ["tab-2", "tab-1"]


def test_cleanup_retries_precondition_failure_after_remediation(
    runner: PythonRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.start(
        """
from purplemux_client import register_run_resource
register_run_resource("purplemux_workspace", "ws-1", {"name": "Owned"})
register_run_resource("purplemux_tab", "tab-1", {"workspace_id": "ws-1"})
"""
    )
    result = wait_until_finished(runner)
    precondition_satisfied = False
    attempted: list[str] = []

    def cleanup(resource: RunResource) -> None:
        attempted.append(resource.identity)
        if resource.identity == "tab-1" and not precondition_satisfied:
            raise OSError("tab identity changed; refusing cleanup")

    monkeypatch.setattr(runner, "_cleanup_resource", cleanup)

    blocked = runner.cleanup(result.run_id or 0)

    assert attempted == ["tab-1"]
    assert [item.cleanup_state for item in blocked.resources] == [
        "retained",
        "cleanup_retryable",
    ]
    assert blocked.as_json()["resourceCleanupStatus"] == "blocked"
    assert "identity changed" in str(blocked.resources[1].cleanup_error)

    precondition_satisfied = True
    cleaned = runner.cleanup(result.run_id or 0)

    assert attempted == ["tab-1", "tab-1", "ws-1"]
    assert all(item.cleanup_state == "cleaned" for item in cleaned.resources)
    assert cleaned.as_json()["resourceCleanupStatus"] == "cleaned"


def test_cleanup_rejects_running_workflow(runner: PythonRunner) -> None:
    run_id = runner.start("import time; time.sleep(60)")

    with pytest.raises(RunCleanupNotAllowedError, match="non-running"):
        runner.cleanup(run_id)

    assert runner.stop(run_id)


def _capture_exception(
    action: Callable[[], object], errors: list[BaseException]
) -> None:
    try:
        action()
    except BaseException as exc:
        errors.append(exc)


def test_managed_shell_result_cleanup_removes_only_expected_temp_directory(
    runner: PythonRunner,
) -> None:
    directory = Path(tempfile.mkdtemp(prefix="awm-shell-"))
    result = directory / "result.json"
    result.write_text('{"exitCode":0}', encoding="utf-8")
    resource = RunResource(
        "managed_shell_result",
        str(directory),
        {
            "result_path": str(result),
            "tab_id": "tab-1",
            "directory_identity": _test_path_identity(directory),
        },
    )

    runner._cleanup_resource(resource)

    assert not directory.exists()


def test_managed_shell_result_cleanup_removes_wrapper_owned_pending_sidecar(
    runner: PythonRunner,
) -> None:
    directory = Path(tempfile.mkdtemp(prefix="awm-shell-"))
    result = directory / "result.json"
    (directory / "result.json.pending").write_text('{"exitCode":0}', encoding="utf-8")
    resource = RunResource(
        "managed_shell_result",
        str(directory),
        {
            "result_path": str(result),
            "tab_id": "tab-1",
            "directory_identity": _test_path_identity(directory),
        },
    )

    runner._cleanup_resource(resource)

    assert not directory.exists()


def test_managed_shell_result_cleanup_rejects_non_regular_pending_sidecar(
    runner: PythonRunner,
) -> None:
    directory = Path(tempfile.mkdtemp(prefix="awm-shell-"))
    result = directory / "result.json"
    pending = directory / "result.json.pending"
    pending.mkdir()
    resource = RunResource(
        "managed_shell_result",
        str(directory),
        {
            "result_path": str(result),
            "tab_id": "tab-1",
            "directory_identity": _test_path_identity(directory),
        },
    )

    with pytest.raises(OSError, match="not a regular file"):
        runner._cleanup_resource(resource)

    assert pending.is_dir()


@pytest.mark.parametrize("replacement_kind", ["directory", "symlink"])
def test_managed_shell_result_cleanup_rejects_replaced_directory_without_unlinking(
    runner: PythonRunner, tmp_path: Path, replacement_kind: str
) -> None:
    directory = Path(tempfile.mkdtemp(prefix="awm-shell-"))
    result = directory / "result.json"
    result.write_text('{"exitCode":0}', encoding="utf-8")
    resource = RunResource(
        "managed_shell_result",
        str(directory),
        {
            "result_path": str(result),
            "tab_id": "tab-1",
            "directory_identity": _test_path_identity(directory),
        },
    )
    result.unlink()
    directory.rmdir()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    replacement_result = replacement / "result.json"
    replacement_result.write_text("replacement", encoding="utf-8")
    if replacement_kind == "directory":
        directory.mkdir()
        (directory / "result.json").write_text("replacement", encoding="utf-8")
        protected_result = directory / "result.json"
    else:
        directory.symlink_to(replacement, target_is_directory=True)
        protected_result = replacement_result

    with pytest.raises(OSError, match="owned directory|identity changed"):
        runner._cleanup_resource(resource)

    assert protected_result.read_text(encoding="utf-8") == "replacement"


def test_workspace_cleanup_releases_verified_empty_workspace(
    runner: PythonRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = WorkspaceState("ws-owned", "Owned", ("/repo",))
    deleted: list[tuple[str, WorkspaceState]] = []

    class Runtime:
        def list_workspaces(self) -> tuple[WorkspaceState, ...]:
            return (workspace,)

        def delete_workspace(
            self, workspace_id: str, *, expected_state: WorkspaceState
        ) -> None:
            deleted.append((workspace_id, expected_state))

    class Client:
        def list_sessions(self) -> tuple[object, ...]:
            return ()

    monkeypatch.setattr(runner_module, "PurpleMuxRuntime", Runtime)
    monkeypatch.setattr(
        runner_module, "PurpleMuxCLIClient", lambda _workspace: Client()
    )

    runner._cleanup_resource(
        RunResource(
            "purplemux_workspace",
            "ws-owned",
            {"name": "Owned", "directories": "/repo"},
        )
    )

    assert deleted == [("ws-owned", workspace)]


def test_git_worktree_cleanup_rejects_replacement_at_owned_path(
    runner: PythonRunner, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    worktree = tmp_path / "owned-worktree"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    (repository / "tracked").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "tracked"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "initial"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-qb",
            "feature",
            str(worktree),
        ],
        check=True,
    )

    def output(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    metadata = {
        "repository": str(repository),
        "path_identity": _test_path_identity(worktree),
        "git_file_identity": _test_path_identity(worktree / ".git", ctime=True),
        "git_dir": output("rev-parse", "--absolute-git-dir"),
        "head": output("rev-parse", "HEAD"),
        "branch": output("rev-parse", "--symbolic-full-name", "HEAD"),
    }
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "remove", str(worktree)],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-q",
            str(worktree),
            "feature",
        ],
        check=True,
    )

    with pytest.raises(OSError, match="identity changed"):
        runner._cleanup_resource(RunResource("git_worktree", str(worktree), metadata))

    assert worktree.is_dir()


def test_git_worktree_cleanup_allows_branch_and_head_to_change_after_registration(
    runner: PythonRunner, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    worktree = tmp_path / "owned-worktree"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    (repository / "tracked").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "tracked"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "initial"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "--detach", str(worktree)],
        check=True,
        capture_output=True,
    )

    def output(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    metadata = {
        "repository": str(repository),
        "path_identity": _test_path_identity(worktree),
        "git_file_identity": _test_path_identity(worktree / ".git", ctime=True),
        "git_dir": output("rev-parse", "--absolute-git-dir"),
        "head": output("rev-parse", "HEAD"),
        "branch": output("rev-parse", "--symbolic-full-name", "HEAD"),
    }
    subprocess.run(["git", "-C", str(worktree), "switch", "-c", "feature"], check=True)
    (worktree / "tracked").write_text("two", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "tracked"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-qm", "implementation"],
        check=True,
    )

    runner._cleanup_resource(RunResource("git_worktree", str(worktree), metadata))

    assert not worktree.exists()


def _test_path_identity(path: Path, *, ctime: bool = False) -> str:
    state = path.stat(follow_symlinks=False)
    suffix = f":{state.st_ctime_ns}" if ctime else ""
    return f"{state.st_dev}:{state.st_ino}{suffix}"


def test_stderr(runner: PythonRunner) -> None:
    runner.start('import sys; print("BAD", file=sys.stderr)')

    result = wait_until_finished(runner)

    assert result.state == "success"
    assert result.stderr == "BAD\n"
    assert "".join(entry.text for entry in result.stderr_entries) == result.stderr
    assert result.stdout_entries == ()


def test_sequential_output_has_chronological_observation_timestamps(
    runner: PythonRunner,
) -> None:
    runner.start(
        'import time; print("first", flush=True); time.sleep(0.2); print("second")'
    )

    result = wait_until_finished(runner)

    assert result.stdout == "first\nsecond\n"
    assert len(result.stdout_entries) >= 2
    observed_at = [
        datetime.fromisoformat(entry.observed_at) for entry in result.stdout_entries
    ]
    assert observed_at == sorted(observed_at)


def test_standard_library_alias_import_passes_and_runs(runner: PythonRunner) -> None:
    runner.start("import os.path; print(os.path.basename('/tmp/example'))")

    result = wait_until_finished(runner)

    assert result.state == "success"
    assert result.stdout == "example\n"


def test_nonzero_exit(runner: PythonRunner) -> None:
    runner.start("raise SystemExit(3)")

    result = wait_until_finished(runner)

    assert result.state == "failed"
    assert result.exit_code == 3


def test_runtime_python_error_is_reported(runner: PythonRunner) -> None:
    runner.start("raise RuntimeError('boom')")

    result = wait_until_finished(runner)

    assert result.state == "failed"
    assert result.exit_code != 0
    assert result.stderr


def test_empty_code(runner: PythonRunner) -> None:
    runner.start("")

    result = wait_until_finished(runner)

    assert result.state == "success"
    assert result.exit_code == 0


def test_run_uses_runner_controlled_cwd_and_records_args(tmp_path: Path) -> None:
    runner = PythonRunner(managed_workflows=False, workflow_cwd=tmp_path)
    runner.start(
        "import json, os, sys; print(json.dumps([os.getcwd(), sys.argv[1:]]))",
        args=("--repo", "path with spaces"),
    )

    result = wait_until_finished(runner)
    runner.close()

    assert json.loads(result.stdout) == [
        str(tmp_path),
        ["--repo", "path with spaces"],
    ]
    assert result.cwd == str(tmp_path)
    assert result.args == ("--repo", "path with spaces")


def test_runner_controlled_cwd_preserves_runner_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager_venv = tmp_path / "manager-venv"
    target_bin = tmp_path / "target-bin"
    monkeypatch.setenv("VIRTUAL_ENV", str(manager_venv))
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(manager_venv / "bin"), str(target_bin)))
    )

    runner = PythonRunner(managed_workflows=False, workflow_cwd=tmp_path)
    runner.start(
        "import json, os; print(json.dumps([os.environ.get('VIRTUAL_ENV'), "
        "os.environ.get('PATH')]))"
    )
    result = wait_until_finished(runner)
    runner.close()

    assert json.loads(result.stdout) == [
        str(manager_venv),
        os.pathsep.join((str(manager_venv / "bin"), str(target_bin))),
    ]


def test_implicit_cwd_preserves_existing_environment(
    runner: PythonRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager_venv = tmp_path / "manager-venv"
    monkeypatch.setenv("VIRTUAL_ENV", str(manager_venv))

    runner.start("import os; print(os.environ.get('VIRTUAL_ENV'))")
    result = wait_until_finished(runner)

    assert result.stdout == f"{manager_venv}\n"


def test_runner_rejects_non_directory_workflow_cwd(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(InvalidExecutionContextError, match="not a directory"):
        PythonRunner(managed_workflows=False, workflow_cwd=missing)


def test_preflight_resolves_relative_paths_from_runner_cwd(tmp_path: Path) -> None:
    (tmp_path / "input.txt").write_text("input", encoding="utf-8")

    runner = PythonRunner(managed_workflows=False, workflow_cwd=tmp_path)
    result = runner.validate("WORKFLOW_PREFLIGHT = {'paths': ['input.txt']}")
    runner.close()

    assert result.valid
    assert runner.snapshot().cwd == str(tmp_path)


def test_stop_long_running_process(runner: PythonRunner) -> None:
    runner.start('import time; print("START", flush=True); time.sleep(60)')
    wait_for(runner, lambda snapshot: snapshot.stdout == "START\n")

    assert runner.stop() is True
    result = wait_until_finished(runner)

    assert result.state == "stopped"
    assert result.exit_code is not None


def test_streams_flushed_output_without_newline(runner: PythonRunner) -> None:
    runner.start('import time; print("PARTIAL", end="", flush=True); time.sleep(60)')

    result = wait_for(runner, lambda snapshot: snapshot.stdout == "PARTIAL")

    assert result.state == "running"


def test_change_revision_tracks_authoritative_observable_state(
    runner: PythonRunner, tmp_path: Path
) -> None:
    code = """\
import time
from pathlib import Path
from purplemux_client import emit_step

def wait_for(name):
    while not Path(name).exists():
        time.sleep(0.01)

wait_for("output")
print("visible", flush=True)
wait_for("progress")
emit_step("observed", "completed")
wait_for("finish")
"""
    runner._workflow_cwd = tmp_path
    run_id = runner.start(code)
    revision = runner.change_revision()

    (tmp_path / "output").touch()
    changed = runner.wait_for_change(revision, timeout=2)
    assert changed is not None
    revision = changed
    assert runner.snapshot(run_id).stdout == "visible\n"

    (tmp_path / "progress").touch()
    changed = runner.wait_for_change(revision, timeout=2)
    assert changed is not None
    revision = changed
    progress_snapshot = runner.snapshot(run_id)
    progress_event = progress_snapshot.progress[-1]
    assert progress_event.name == "observed"
    assert progress_event.observed_at is not None
    assert datetime.fromisoformat(progress_event.observed_at).tzinfo is not None
    serialized_progress = progress_snapshot.as_json()["progress"]
    assert isinstance(serialized_progress, list)
    assert serialized_progress[-1]["observedAt"] == progress_event.observed_at

    (tmp_path / "finish").touch()
    changed = runner.wait_for_change(revision, timeout=2)
    assert changed is not None
    historical_snapshot = runner.snapshot(run_id)
    assert historical_snapshot.state == "success"
    assert historical_snapshot.progress[-1].observed_at == progress_event.observed_at


def test_pr_navigation_is_scoped_to_and_retained_by_each_run(
    runner: PythonRunner,
) -> None:
    first = runner.start(
        """\
from purplemux_client import emit_run_pr, emit_step
emit_step(
    "Issue #127 review", "completed", pr_number=130,
    pr_url="https://github.com/eletim/agent-workflow-manager/pull/130",
)
emit_run_pr(131, "https://github.com/eletim/agent-workflow-manager/pull/131")
"""
    )
    wait_until_finished(runner)
    second = runner.start('print("no PR")')
    wait_until_finished(runner)

    first_snapshot = runner.snapshot(first)
    assert first_snapshot.progress[0].pr_number == 130
    progress_json = first_snapshot.as_json()["progress"]
    assert isinstance(progress_json, list)
    assert progress_json[0]["pr_url"].endswith("/130")
    assert first_snapshot.as_json()["integrationPr"] == {
        "number": 131,
        "url": "https://github.com/eletim/agent-workflow-manager/pull/131",
    }
    assert runner.snapshot(second).integration_pr is None
    assert runner.snapshot(second).as_json()["integrationPr"] is None


def test_progress_pr_navigation_survives_bounded_event_eviction() -> None:
    runner = PythonRunner(managed_workflows=False, max_progress_events=3)
    try:
        run_id = runner.start(
            """\
from purplemux_client import emit_step
emit_step(
    "Issue #1", "completed", pr_number=101,
    pr_url="https://github.com/example/repo/pull/101",
)
for number in range(1, 6):
    emit_step(f"later event {number}", "completed")
"""
        )
        snapshot = wait_until_finished(runner)
    finally:
        runner.close()

    assert snapshot.run_id == run_id
    assert [event.name for event in snapshot.progress] == [
        "Issue #1",
        "later event 3",
        "later event 4",
        "later event 5",
    ]
    issue_event = snapshot.progress[0]
    assert issue_event.pr_number == 101
    assert issue_event.pr_url == "https://github.com/example/repo/pull/101"
    assert snapshot.as_json()["progress"][0]["pr_number"] == 101


def test_shell_failure_diagnostic_survives_real_progress_event_encoding(
    runner: PythonRunner, tmp_path: Path
) -> None:
    diagnostic = '"\\' * 1_250
    code = f"""\
from purplemux_client import ShellResult, emit_step

result = ShellResult(
    2,
    diagnostic_output={diagnostic!r},
    cwd={str(tmp_path)!r},
    workspace_id="ws-test",
    tab_id="tab-test",
)
if result.exit_code != 0:
    failure = result.failure_message("sync and verify main")
    emit_step(
        "sync and verify main",
        "failed",
        error=failure,
        workspace=result.workspace_id,
        tab=result.tab_id,
    )
    raise SystemExit(result.exit_code)
"""

    run_id = runner.start(code)
    snapshot = wait_until_finished(runner)

    assert snapshot.run_id == run_id
    assert snapshot.state == "failed"
    assert snapshot.exit_code == 2
    assert len(snapshot.progress) == 1
    event = snapshot.progress[0]
    assert event.name == "sync and verify main"
    assert event.status == "failed"
    assert event.workspace == "ws-test"
    assert event.tab == "tab-test"
    assert event.observed_at is not None
    assert datetime.fromisoformat(event.observed_at).tzinfo is not None
    assert event.error is not None
    assert event.error.startswith(
        "sync and verify main failed (exit code 2)\n"
        f"cwd: {tmp_path}\n"
        "workspace/tab: ws-test / tab-test\n"
    )
    assert event.error.endswith("\n[error truncated]")


def test_runner_parses_structured_warning_finding() -> None:
    parsed = PythonRunner._parse_runner_event(
        json.dumps(
            {
                "type": "finding",
                "category": "github",
                "status": "warning",
                "message": "review limit reached",
            }
        )
    )

    assert parsed is not None
    kind, finding = parsed
    assert kind == "finding"
    assert finding == TopologyFinding("github", "warning", "review limit reached")


def test_successful_run_aggregates_only_structured_warning_findings() -> None:
    runner = PythonRunner(managed_workflows=False)
    try:
        warning_id = runner.start(
            """from purplemux_client import emit_finding
emit_finding("github", "human review required", status="warning")
print("done")
"""
        )
        warning_result = wait_until_finished(runner)
        info_id = runner.start(
            """from purplemux_client import emit_finding
emit_finding("runtime", "WARN: informational text", status="info")
print("WARN: stdout text")
"""
        )
        info_result = wait_until_finished(runner)
    finally:
        runner.close()

    assert warning_result.run_id == warning_id
    assert warning_result.state == "success"
    assert warning_result.exit_code == 0
    assert warning_result.has_warnings is True
    assert warning_result.as_json()["hasWarnings"] is True
    assert warning_result.as_summary_json()["hasWarnings"] is True
    assert info_result.run_id == info_id
    assert info_result.state == "success"
    assert info_result.has_warnings is False
    assert info_result.as_json()["hasWarnings"] is False
    assert info_result.as_summary_json()["hasWarnings"] is False


def test_warning_aggregate_survives_finding_eviction_and_run_switching() -> None:
    runner = PythonRunner(managed_workflows=False, max_progress_events=1)
    try:
        warning_id = runner.start(
            """from purplemux_client import emit_finding
emit_finding("git", "review required", status="warning")
emit_finding("git", "later fact", status="passed")
"""
        )
        wait_until_finished(runner)
        clean_id = runner.start('print("clean")')
        wait_until_finished(runner)

        warning_result = runner.snapshot(warning_id)
        clean_result = runner.snapshot(clean_id)
    finally:
        runner.close()

    assert [finding.status for finding in warning_result.findings] == ["passed"]
    assert warning_result.has_warnings is True
    assert clean_result.has_warnings is False


def test_issue_driven_summary_uses_durable_structured_results_not_progress() -> None:
    runner = PythonRunner(managed_workflows=False, max_progress_events=1)
    try:
        run_id = runner.start(
            """from purplemux_client import (
    emit_finding, emit_issue_driven_context, emit_issue_result, emit_run_pr,
    emit_step, emit_whole_review_result,
)
emit_issue_driven_context("acme/project", "dev/v1", "main", policy_issue=9)
emit_issue_result(10, "approved", 2, 40, "https://github.com/acme/project/pull/40")
emit_finding("github", "review limit reached", status="warning")
emit_issue_result(11, "continued_with_warning", 5, 41, "https://github.com/acme/project/pull/41", warnings=("review limit reached",))
emit_issue_result("mini-task:refresh-help", "approved", 2, 42, "https://github.com/acme/project/pull/42", label="Mini task refresh-help")
emit_whole_review_result("skipped", 0)
emit_run_pr(50, "https://github.com/acme/project/pull/50")
for number in range(3):
    emit_step(f"noise {number}", "completed")
"""
        )
        result = wait_until_finished(runner)
        runner.start('print("another run")')
        wait_until_finished(runner)
        historical = runner.snapshot(run_id).as_json()
    finally:
        runner.close()

    assert [event.name for event in result.progress] == ["noise 2"]
    assert historical["warningCount"] == 1
    assert historical["issueDrivenSummary"] == {
        "repository": "acme/project",
        "integrationBranch": "dev/v1",
        "finalBranch": "main",
        "terminalResult": "success",
        "warningCount": 1,
        "policyIssue": 9,
        "issues": [
            {
                "issue": 10,
                "outcome": "approved",
                "reviews": 2,
                "pr": {
                    "number": 40,
                    "url": "https://github.com/acme/project/pull/40",
                },
                "warnings": [],
            },
            {
                "issue": 11,
                "outcome": "continued_with_warning",
                "reviews": 5,
                "pr": {
                    "number": 41,
                    "url": "https://github.com/acme/project/pull/41",
                },
                "warnings": ["review limit reached"],
            },
            {
                "issue": "mini-task:refresh-help",
                "label": "Mini task refresh-help",
                "outcome": "approved",
                "reviews": 2,
                "pr": {
                    "number": 42,
                    "url": "https://github.com/acme/project/pull/42",
                },
                "warnings": [],
            },
        ],
        "wholeReview": {"outcome": "skipped", "reviews": 0, "warnings": []},
        "basePr": {
            "number": 50,
            "url": "https://github.com/acme/project/pull/50",
        },
    }


def test_issue_driven_summary_is_hidden_while_running_and_scoped_to_run() -> None:
    runner = PythonRunner(managed_workflows=False)
    try:
        first = runner.start(
            """from purplemux_client import emit_issue_driven_context
import time
emit_issue_driven_context("acme/project", "dev/v1", "main")
time.sleep(60)
"""
        )
        running = wait_for(
            runner, lambda item: item.issue_driven_context is not None, run_id=first
        )
        runner.stop(first)
        wait_for(runner, lambda item: item.state == "stopped", run_id=first)
        second = runner.start('print("clean")')
        wait_until_finished(runner)
    finally:
        runner.close()

    assert running.as_json()["issueDrivenSummary"] is None
    assert runner.snapshot(first).as_json()["issueDrivenSummary"] is not None
    assert runner.snapshot(second).as_json()["issueDrivenSummary"] is None


def test_output_is_bounded_and_reports_truncation() -> None:
    runner = PythonRunner(managed_workflows=False, max_output_chars=20)
    try:
        runner.start('print("x" * 50, end="")')
        result = wait_until_finished(runner)
    finally:
        runner.close()

    assert result.stdout == "[output truncated; showing tail]\n" + "x" * 20
    assert "".join(entry.text for entry in result.stdout_entries) == result.stdout


def test_runner_rejects_non_posix_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")

    with pytest.raises(RuntimeError, match="requires a POSIX"):
        PythonRunner(
            managed_workflows=False,
        )


def test_runs_execute_concurrently_with_independent_output(
    runner: PythonRunner,
) -> None:
    first_id = runner.start(
        'WORKFLOW_OUTLINE = ["first plan"]\n'
        'import time; print("first-start", flush=True); time.sleep(60)'
    )
    first_running = wait_for(
        runner,
        lambda snapshot: snapshot.stdout == "first-start\n",
        run_id=first_id,
    )

    second_id = runner.start('WORKFLOW_OUTLINE = ["second plan"]\nprint("second")')
    second = wait_for(
        runner,
        lambda snapshot: snapshot.state != "running",
        run_id=second_id,
    )

    assert first_running.state == "running"
    assert runner.snapshot(first_id).state == "running"
    assert runner.snapshot(first_id).stdout == "first-start\n"
    assert runner.snapshot(first_id).outline == ("first plan",)
    assert [entry.text for entry in runner.snapshot(first_id).stdout_entries] == [
        "first-start\n"
    ]
    assert second.state == "success"
    assert second.stdout == "second\n"
    assert second.outline == ("second plan",)
    assert [entry.text for entry in second.stdout_entries] == ["second\n"]
    assert runner.stop(first_id) is True


def test_stopping_one_run_does_not_affect_another(runner: PythonRunner) -> None:
    first_id = runner.start("import time; time.sleep(60)")
    second_id = runner.start("import time; time.sleep(60)")

    assert runner.stop(first_id) is True
    first = wait_for(
        runner,
        lambda snapshot: snapshot.state != "running",
        run_id=first_id,
    )

    assert first.state == "stopped"
    assert runner.snapshot(second_id).state == "running"
    assert runner.stop(second_id) is True


def test_concurrent_targeted_stops_have_independent_grace_periods() -> None:
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    stubborn_code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n"
    )
    try:
        run_ids = [runner.start(stubborn_code) for _ in range(2)]
        for run_id in run_ids:
            wait_for(
                runner,
                lambda snapshot: snapshot.stdout == "READY\n",
                run_id=run_id,
            )

        stop_results: list[bool] = []
        stop_threads = [
            threading.Thread(
                target=lambda target=run_id: stop_results.append(runner.stop(target))
            )
            for run_id in run_ids
        ]
        started = time.monotonic()
        for thread in stop_threads:
            thread.start()
        for thread in stop_threads:
            thread.join(timeout=2)
        elapsed = time.monotonic() - started

        assert all(not thread.is_alive() for thread in stop_threads)
        assert stop_results == [True, True]
        assert elapsed < 0.85
        for run_id in run_ids:
            assert (
                wait_for(
                    runner,
                    lambda snapshot: snapshot.state != "running",
                    run_id=run_id,
                ).state
                == "stopped"
            )
    finally:
        runner.close()


def test_validation_does_not_overwrite_an_active_run(runner: PythonRunner) -> None:
    run_id = runner.start("import time; time.sleep(60)")

    validation = runner.validate("def broken(")

    assert not validation.valid
    assert runner.snapshot(run_id).state == "running"
    assert [snapshot.run_id for snapshot in runner.snapshots()] == [run_id]
    assert runner.stop(run_id) is True


def test_can_run_again_after_finish(runner: PythonRunner) -> None:
    first_id = runner.start('print("first")')
    wait_until_finished(runner)

    second_id = runner.start('print("second")')
    result = wait_until_finished(runner)

    assert second_id > first_id
    assert result.stdout == "second\n"
    assert result.state == "success"


def test_can_run_again_after_stop(runner: PythonRunner) -> None:
    runner.start("import time; time.sleep(60)")
    assert runner.stop() is True
    wait_until_finished(runner)

    runner.start('print("after stop")')
    result = wait_until_finished(runner)

    assert result.stdout == "after stop\n"
    assert result.state == "success"


def test_start_after_close_is_rejected() -> None:
    runner = PythonRunner(
        managed_workflows=False,
    )
    runner.close()

    with pytest.raises(RunnerClosedError, match="Runner is closed"):
        runner.start('print("must not start")')


def test_close_cannot_miss_concurrent_start_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    popen_entered = threading.Event()
    allow_popen = threading.Event()

    def blocking_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        if kwargs.get("start_new_session") is True and "cwd" in kwargs:
            popen_entered.set()
            assert allow_popen.wait(timeout=3)
        return real_popen(*args, **kwargs)  # type: ignore[call-overload,return-value]

    monkeypatch.setattr(subprocess, "Popen", blocking_popen)
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    start_errors: list[BaseException] = []

    def start_run() -> None:
        try:
            runner.start("import time; time.sleep(60)")
        except BaseException as exc:
            start_errors.append(exc)

    start_thread = threading.Thread(target=start_run)
    start_thread.start()
    assert popen_entered.wait(timeout=3)
    close_thread = threading.Thread(target=runner.close)
    close_thread.start()
    allow_popen.set()
    start_thread.join(timeout=5)
    close_thread.join(timeout=5)

    assert not start_thread.is_alive()
    assert not close_thread.is_alive()
    assert start_errors == []
    assert runner.snapshot().state == "stopped"
    with pytest.raises(RunnerClosedError):
        runner.start("")


def test_close_stops_process_group() -> None:
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    runner.start(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    result = wait_for(runner, lambda snapshot: bool(snapshot.stdout))
    child_pid = int(result.stdout.strip())

    runner.close()
    wait_until_finished(runner)

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _process_is_live(child_pid):
        time.sleep(0.05)
    assert not _process_is_live(child_pid)


def test_close_cleans_up_stubborn_runs_with_one_bounded_grace_period() -> None:
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    stubborn_code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n"
    )
    run_ids = [runner.start(stubborn_code) for _ in range(3)]
    for run_id in run_ids:
        wait_for(
            runner,
            lambda snapshot: snapshot.stdout == "READY\n",
            run_id=run_id,
        )

    started = time.monotonic()
    runner.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.85
    assert [runner.snapshot(run_id).state for run_id in run_ids] == [
        "stopped",
        "stopped",
        "stopped",
    ]


def test_stop_kills_child_that_ignores_sigterm(runner: PythonRunner) -> None:
    runner.start(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        "'import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        'print(f"CHILD {os.getpid()}", flush=True); time.sleep(60)\'])\n'
        "time.sleep(60)\n"
    )
    result = wait_for(runner, lambda snapshot: "CHILD " in snapshot.stdout)
    child_pid = int(result.stdout.split()[1])

    assert runner.stop() is True
    wait_until_finished(runner)

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _process_is_live(child_pid):
        time.sleep(0.05)
    assert not _process_is_live(child_pid)


def test_stop_and_close_are_bounded_when_detached_child_keeps_output_open() -> None:
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.2)
    child_pid: int | None = None
    try:
        run_id = runner.start(
            "import subprocess, sys, time\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "    start_new_session=True,\n"
            ")\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        result = wait_for(runner, lambda snapshot: snapshot.stdout.strip() != "")
        child_pid = int(result.stdout.strip())

        started = time.monotonic()
        assert runner.stop(run_id) is True
        result = wait_for(
            runner,
            lambda snapshot: snapshot.state == "stopped",
            timeout=2,
            run_id=run_id,
        )
        runner.close()
        elapsed = time.monotonic() - started

        assert result.state == "stopped"
        assert elapsed < 1.5
        assert _process_is_live(child_pid)
    finally:
        runner.close()
        if child_pid is not None and _process_is_live(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_stop_rejects_exited_main_with_detached_child_output() -> None:
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    child_pid: int | None = None
    try:
        run_id = runner.start(
            "import subprocess, sys\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "    start_new_session=True,\n"
            ")\n"
            "print(child.pid, flush=True)\n"
        )
        result = wait_for(
            runner,
            lambda snapshot: (
                bool(snapshot.stdout.strip())
                and runner._runs[run_id].process.poll() == 0
            ),
            run_id=run_id,
        )
        child_pid = int(result.stdout.strip())
        assert result.state == "running"

        started = time.monotonic()
        assert runner.stop(run_id) is False
        runner.close()
        elapsed = time.monotonic() - started
        result = runner.snapshot(run_id)

        assert elapsed < 1.5
        assert result.state == "success"
        assert result.exit_code == 0
        assert result.attempts[-1].state == "success"
        assert _process_is_live(child_pid)
    finally:
        runner.close()
        if child_pid is not None and _process_is_live(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def _process_is_live(pid: int) -> bool:
    try:
        output = subprocess.check_output(
            ["ps", "-o", "stat=", "-p", str(pid)], text=True
        ).strip()
    except subprocess.CalledProcessError:
        return False
    return bool(output) and not output.startswith("Z")


def test_popen_uses_current_interpreter_without_shell(
    runner: PythonRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_popen = subprocess.Popen
    calls: list[tuple[object, dict[str, object]]] = []

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        calls.append((args[0], kwargs))
        return real_popen(*args, **kwargs)  # type: ignore[call-overload,return-value]

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    runner.start("")
    wait_until_finished(runner)

    command, options = next(
        (command, options)
        for command, options in calls
        if options.get("start_new_session") is True and "cwd" in options
    )
    assert isinstance(command, list)
    assert command[0] == sys.executable
    assert options["shell"] is False
    assert options["start_new_session"] is True
    assert options["cwd"] == Path.cwd()


@pytest.fixture
def web_server() -> Iterator[tuple[tuple[str, int], str]]:
    server = RunnerHTTPServer(
        ("127.0.0.1", 0), PythonRunner(managed_workflows=False, stop_timeout=0.5)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield (str(host), int(port)), server.request_token
    server.shutdown()
    server.server_close()
    thread.join()


class SettingsAPINotifier:
    def __init__(self) -> None:
        self.current_policy = (True, True, True, False)
        self.test_result = NotificationResult(True, True, "notification sent")
        self.test_calls = 0
        self.transport_calls: list[tuple[str, str, str | None]] = []

    def policy(self) -> tuple[bool, bool, bool, bool]:
        return self.current_policy

    def configure_policy(
        self,
        *,
        enabled: bool,
        notify_success: bool,
        notify_failure: bool,
        notify_stopped: bool,
    ) -> None:
        self.current_policy = (
            enabled,
            notify_success,
            notify_failure,
            notify_stopped,
        )

    def send_test(self) -> NotificationResult:
        self.test_calls += 1
        return self.test_result

    def configure_transport(
        self, *, server: str, topic: str, replacement_token: str | None
    ) -> None:
        self.transport_calls.append((server, topic, replacement_token))


@pytest.fixture
def settings_web_server(
    tmp_path: Path,
) -> Iterator[
    tuple[tuple[str, int], str, Path, Path, SettingsAPINotifier, PythonRunner]
]:
    runtime_config = tmp_path / "config.sh"
    notify_config = tmp_path / "notify/config"
    runtime_config.write_text(
        'AGENT_WORKFLOW_MANAGER_HOST="127.0.0.1"\n', encoding="utf-8"
    )
    notify_config.parent.mkdir()
    notify_config.write_text(
        "NOTIFY_SERVER=https://notify.example\n"
        "NOTIFY_TOPIC=agents\n"
        "NOTIFY_TOKEN=tk_api_secret\n",
        encoding="utf-8",
    )
    notifier = SettingsAPINotifier()
    settings = NotificationSettings(
        runtime_config=runtime_config,
        notify_config=notify_config,
        notifier=notifier,
        environment={},
    )
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    server = RunnerHTTPServer(("127.0.0.1", 0), runner, notification_settings=settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield (
        (str(host), int(port)),
        server.request_token,
        runtime_config,
        notify_config,
        notifier,
        runner,
    )
    server.shutdown()
    server.server_close()
    thread.join()


def request(
    server_address: tuple[str, int],
    method: str,
    path: str,
    body: str | None = None,
    *,
    token: str | None = None,
    origin: str | None = None,
    host: str | None = None,
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection(*server_address, timeout=3)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token is not None:
        headers["X-Python-Runner-Token"] = token
    if origin is not None:
        headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    return response.status, payload


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("not json", "invalid JSON"),
        ("[]", "JSON object required"),
        ('{"code": 42}', "code must be a string"),
    ],
)
def test_malformed_run_request(
    web_server: tuple[tuple[str, int], str], body: str, error: str
) -> None:
    address, token = web_server
    status, payload = request(address, "POST", "/api/run", body, token=token)

    assert status == 400
    assert payload == {"error": error}


def test_runner_http_lifecycle(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    status, started = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": 'print("HTTP_OK")'}),
        token=token,
    )
    assert status == 202
    assert started["runId"] == 1

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, result = request(address, "GET", "/api/status")
        if result["state"] != "running":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("HTTP run did not finish")

    assert status == 200
    stdout_entries = result.pop("stdoutEntries")
    stderr_entries = result.pop("stderrEntries")
    identity = result.pop("identity")
    assert [entry["text"] for entry in stdout_entries] == ["HTTP_OK\n"]
    assert datetime.fromisoformat(stdout_entries[0]["observedAt"]).tzinfo is not None
    assert stderr_entries == []
    assert re.fullmatch(r"[0-9a-f]{32}-1", identity)
    assert result == {
        "mode": "workflow",
        "state": "success",
        "stdout": "HTTP_OK\n",
        "stderr": "",
        "outline": [],
        "progress": [],
        "validation": [],
        "dryRun": None,
        "dryRunEligible": True,
        "dryRunIssues": [],
        "executionContext": None,
        "findings": [],
        "hasWarnings": False,
        "warningCount": 0,
        "exitCode": 0,
        "runId": 1,
        "integrationPr": None,
        "issueDrivenSummary": None,
        "cwd": str(Path.cwd()),
        "args": [],
        "attempts": [
            {
                "number": 1,
                "state": "success",
                "exitCode": 0,
            }
        ],
        "code": 'print("HTTP_OK")',
        "resources": [],
        "resourceCleanupStatus": "cleaned",
        "cleanupAvailable": True,
        "checked": False,
    }


def test_prompt_api_starts_shared_run_with_authoritative_cwd(
    web_server: tuple[tuple[str, int], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address, token = web_server
    monkeypatch.setattr(
        web_module,
        "build_prompt_workflow",
        lambda _execution: "print('PROMPT_RESULT')",
    )

    status, started = request(
        address,
        "POST",
        "/api/prompt",
        json.dumps({"agent": "codex", "cwd": str(tmp_path), "prompt": "Do the work"}),
        token=token,
    )

    assert status == 202
    assert started["mode"] == "prompt"
    assert started["prompt"] == {
        "agent": "codex",
        "cwd": str(tmp_path.resolve()),
        "prompt": "Do the work",
    }
    assert started["cwd"] == str(tmp_path.resolve())
    assert started["code"] is None
    assert started["resources"] == []
    assert started["cleanupAvailable"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"agent": "codex", "cwd": ".", "prompt": 1},
        {"agent": "unknown", "cwd": ".", "prompt": "work"},
        {"agent": "codex", "cwd": "/definitely/missing", "prompt": "work"},
    ],
)
def test_prompt_api_rejects_invalid_inputs(
    web_server: tuple[tuple[str, int], str], payload: dict[str, object]
) -> None:
    address, token = web_server

    status, result = request(
        address,
        "POST",
        "/api/prompt",
        json.dumps(payload),
        token=token,
    )

    assert status == 400
    assert isinstance(result["error"], str)


def test_list_directory_returns_only_sorted_directories(tmp_path: Path) -> None:
    (tmp_path / "zeta").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / "notes.txt").write_text("not exposed", encoding="utf-8")

    listing = list_directory(str(tmp_path))

    assert listing == {
        "path": str(tmp_path.resolve()),
        "parent": str(tmp_path.resolve().parent),
        "directories": [
            {"name": "Alpha", "path": str((tmp_path / "Alpha").resolve())},
            {"name": "zeta", "path": str((tmp_path / "zeta").resolve())},
        ],
    }


def test_directory_api_navigates_and_validates_paths(
    web_server: tuple[tuple[str, int], str], tmp_path: Path
) -> None:
    address, token = web_server
    child = tmp_path / "child"
    child.mkdir()
    file_path = tmp_path / "file.txt"
    file_path.write_text("secret", encoding="utf-8")

    status, listing = request(
        address,
        "POST",
        "/api/directories",
        json.dumps({"path": str(tmp_path)}),
        token=token,
    )

    assert status == 200
    assert listing["path"] == str(tmp_path.resolve())
    assert listing["parent"] == str(tmp_path.resolve().parent)
    assert listing["directories"] == [{"name": "child", "path": str(child.resolve())}]
    assert "file.txt" not in json.dumps(listing)

    for invalid_path in (str(file_path), str(tmp_path / "missing"), ""):
        status, payload = request(
            address,
            "POST",
            "/api/directories",
            json.dumps({"path": invalid_path}),
            token=token,
        )
        assert status == 400
        assert isinstance(payload["error"], str)


def test_events_endpoint_streams_runner_change_notifications(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    events = http.client.HTTPConnection(*address, timeout=3)
    events.request("GET", "/api/events")
    response = events.getresponse()

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-cache"
    assert response.readline() == b"retry: 1000\n"
    assert response.readline() == b"\n"

    status, started = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": "import time; time.sleep(60)"}),
        token=token,
    )
    assert status == 202
    assert started["state"] == "running"
    assert response.readline() == b"event: runner-change\n"
    revision_line = response.readline()
    assert revision_line.startswith(b"data: ")
    assert int(revision_line.removeprefix(b"data: ")) > 0
    assert response.readline() == b"\n"

    events.close()


def test_events_endpoint_rejects_untrusted_host(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, _ = web_server
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.request("GET", "/api/events", headers={"Host": "attacker.example"})
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()

    assert response.status == 403
    assert payload == {"error": "untrusted host"}


def test_run_api_lists_selects_and_stops_concurrent_runs_independently(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server

    status, first = request(
        address,
        "POST",
        "/api/run",
        json.dumps(
            {
                "code": "import time; print('first', flush=True); time.sleep(60)",
                "args": ["--first"],
            }
        ),
        token=token,
    )
    assert status == 202
    status, second = request(
        address,
        "POST",
        "/api/run",
        json.dumps(
            {
                "code": "import time; print('second', flush=True); time.sleep(60)",
                "args": ["--second"],
            }
        ),
        token=token,
    )
    assert status == 202
    first_id = int(first["runId"])
    second_id = int(second["runId"])

    status, listed = request(address, "GET", "/api/runs")
    assert status == 200
    assert [(run["runId"], run["cwd"], run["args"]) for run in listed["runs"]] == [
        (first_id, str(Path.cwd()), ["--first"]),
        (second_id, str(Path.cwd()), ["--second"]),
    ]

    status, stopped = request(
        address, "POST", f"/api/runs/{first_id}/stop", token=token
    )
    assert status == 202
    assert stopped["stopped"] is True
    assert stopped["runId"] == first_id

    status, still_running = request(address, "GET", f"/api/runs/{second_id}")
    assert status == 200
    assert still_running["state"] == "running"

    status, _ = request(address, "POST", f"/api/runs/{second_id}/stop", token=token)
    assert status == 202


def test_run_api_returns_not_found_for_unknown_run(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server

    assert request(address, "GET", "/api/runs/999")[0] == 404
    assert request(address, "POST", "/api/runs/999/stop")[0] == 403
    assert request(address, "POST", "/api/runs/999/stop", token=token)[0] == 404
    assert request(address, "POST", "/api/runs/999/cleanup", token=token)[0] == 404
    assert request(address, "POST", "/api/runs/999/resume", token=token)[0] == 404


def test_run_api_updates_checked_metadata_only_for_terminal_run(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    status, started = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": "import time; time.sleep(0.1)"}),
        token=token,
    )
    assert status == 202
    run_id = int(started["runId"])
    assert started["checked"] is False

    status, rejected = request(
        address,
        "POST",
        f"/api/runs/{run_id}/checked",
        json.dumps({"checked": True}),
        token=token,
    )
    assert status == 409
    assert "only terminal runs" in str(rejected["error"])

    deadline = time.monotonic() + 5
    while request(address, "GET", f"/api/runs/{run_id}")[1]["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)

    status, checked = request(
        address,
        "POST",
        f"/api/runs/{run_id}/checked",
        json.dumps({"checked": True}),
        token=token,
    )
    assert status == 200
    assert checked["checked"] is True
    assert checked["state"] == "success"
    assert request(address, "GET", f"/api/runs/{run_id}")[1]["checked"] is True
    assert request(address, "GET", "/api/runs")[1]["runs"][0]["checked"] is True

    status, unchecked = request(
        address,
        "POST",
        f"/api/runs/{run_id}/checked",
        json.dumps({"checked": False}),
        token=token,
    )
    assert status == 200
    assert unchecked["checked"] is False
    assert unchecked["state"] == "success"


def test_run_api_deletes_only_checked_terminal_history(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    run_ids = []
    for source in ("print('checked')", "print('unchecked')"):
        status, started = request(
            address, "POST", "/api/run", json.dumps({"code": source}), token=token
        )
        assert status == 202
        run_id = int(started["runId"])
        run_ids.append(run_id)
        deadline = time.monotonic() + 5
        while request(address, "GET", f"/api/runs/{run_id}")[1]["state"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.02)

    status, _ = request(
        address,
        "POST",
        f"/api/runs/{run_ids[0]}/checked",
        json.dumps({"checked": True}),
        token=token,
    )
    assert status == 200

    status, changed = request(
        address,
        "POST",
        "/api/runs/delete-checked",
        json.dumps({"runIds": run_ids}),
        token=token,
    )
    assert status == 409
    assert "refresh and confirm" in str(changed["error"])
    assert request(address, "GET", f"/api/runs/{run_ids[0]}")[0] == 200

    deletion_body = json.dumps({"runIds": [run_ids[0]]})
    assert request(address, "POST", "/api/runs/delete-checked", deletion_body)[0] == 403
    status, deleted = request(
        address,
        "POST",
        "/api/runs/delete-checked",
        deletion_body,
        token=token,
    )
    assert status == 200
    assert deleted == {"deletedCount": 1, "deletedRunIds": [run_ids[0]]}
    assert request(address, "GET", f"/api/runs/{run_ids[0]}")[0] == 404
    assert request(address, "GET", f"/api/runs/{run_ids[1]}")[0] == 200


def test_run_api_exposes_explicit_cleanup_without_deleting_history(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    status, started = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": "print('done')"}),
        token=token,
    )
    assert status == 202
    run_id = int(started["runId"])
    deadline = time.monotonic() + 5
    while request(address, "GET", f"/api/runs/{run_id}")[1]["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)

    status, cleaned = request(
        address, "POST", f"/api/runs/{run_id}/cleanup", token=token
    )

    assert status == 200
    assert cleaned["state"] == "success"
    assert cleaned["resourceCleanupStatus"] == "cleaned"
    assert request(address, "GET", f"/api/runs/{run_id}")[0] == 200


def test_run_api_uses_runner_cwd_and_passes_arguments(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    status, started = request(
        address,
        "POST",
        "/api/run",
        json.dumps(
            {
                "code": "import json, os, sys; print(json.dumps([os.getcwd(), sys.argv[1:]]))",
                "args": ["--repo", "target repo"],
            }
        ),
        token=token,
    )

    assert status == 202
    assert started["cwd"] == str(Path.cwd())
    assert started["args"] == ["--repo", "target repo"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        _, result = request(address, "GET", "/api/status")
        if result["state"] != "running":
            break
        time.sleep(0.02)
    assert json.loads(str(result["stdout"])) == [
        str(Path.cwd()),
        ["--repo", "target repo"],
    ]


def test_run_api_exposes_submitted_code_in_detail_but_not_in_list_summary(
    web_server: tuple[tuple[str, int], str],
) -> None:
    """Issue #45: /api/runs/{id} must return each run's own submitted code,
    unaffected by other runs starting, while /api/runs list summaries stay
    lightweight (no full source)."""
    address, token = web_server
    first_code = "print('RUN=A')"
    second_code = "print('RUN=B')"

    status, first = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": first_code, "args": ["A-ARG"]}),
        token=token,
    )
    assert status == 202
    first_id = int(first["runId"])
    assert first["code"] == first_code

    status, before = request(address, "GET", f"/api/runs/{first_id}")
    assert status == 200
    assert before["code"] == first_code

    status, second = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": second_code, "args": ["B-ARG"]}),
        token=token,
    )
    assert status == 202
    second_id = int(second["runId"])
    assert second["code"] == second_code

    # The first run's own snapshot must be unaffected by the second run
    # starting and executing with different args/code.
    status, after = request(address, "GET", f"/api/runs/{first_id}")
    assert status == 200
    assert after["code"] == before["code"] == first_code
    assert after["cwd"] == before["cwd"] == str(Path.cwd())
    assert after["args"] == before["args"] == ["A-ARG"]

    status, second_detail = request(address, "GET", f"/api/runs/{second_id}")
    assert status == 200
    assert second_detail["code"] == second_code
    assert second_detail["cwd"] == str(Path.cwd())

    # The list summary intentionally omits the full source.
    status, listed = request(address, "GET", "/api/runs")
    assert status == 200
    assert len(listed["runs"]) == 2
    assert all("code" not in run for run in listed["runs"])


@pytest.mark.parametrize(
    ("context", "error"),
    [
        (
            {"cwd": 42},
            "cwd is not a Workflow input; declare repository context with prepare_run_repository()",
        ),
        ({"args": "--repo target"}, "args must be an array of strings"),
        ({"args": ["--repo", 42]}, "args must be an array of strings"),
    ],
)
def test_run_api_rejects_invalid_execution_context_shape(
    web_server: tuple[tuple[str, int], str],
    context: dict[str, object],
    error: str,
) -> None:
    address, token = web_server

    status, payload = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": "", **context}),
        token=token,
    )

    assert status == 400
    assert payload == {"error": error}


def test_run_api_rejects_workflow_cwd_input(
    web_server: tuple[tuple[str, int], str], tmp_path: Path
) -> None:
    address, token = web_server

    status, payload = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": "", "cwd": str(tmp_path / "missing")}),
        token=token,
    )

    assert status == 400
    assert "cwd is not a Workflow input" in str(payload["error"])


def test_runner_page_keeps_repository_context_in_python(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, _ = web_server
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.request("GET", "/")
    response = connection.getresponse()
    page = response.read().decode()
    connection.close()

    assert response.status == 200
    assert 'id="working-directory"' not in page
    assert 'id="run-arguments"' in page
    assert 'id="run-list"' in page


def test_runner_page_exposes_prompt_and_workflow_modes(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, _ = web_server
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.request("GET", "/")
    response = connection.getresponse()
    page = response.read().decode()
    connection.close()

    assert response.status == 200
    for element_id in (
        "prompt-mode",
        "issue-driven-mode",
        "workflow-mode",
        "prompt-agent",
        "prompt-cwd",
        "prompt-text",
        "directory-picker-open",
        "directory-picker-dialog",
        "directory-picker-path",
        "directory-picker-parent",
        "directory-picker-select",
        "issue-driven-fields",
        "issue-driven-json",
        "issue-driven-python",
        "issue-driven-generate",
    ):
        assert f'id="{element_id}"' in page
    assert 'id="resume"' not in page


def test_issue_driven_generation_api_is_distinct_from_python_validation(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    source = json.dumps(
        {
            "repository": "/tmp/example",
            "integration_branch": "dev/v0.2.0",
            "final_branch": "main",
            "issues": [90, 89],
            "max_reviews": 5,
            "implementer_agent": "claude",
            "reviewer_agent": "codex",
            "merge_to_integration": True,
            "final_review": True,
            "merge_final": False,
        }
    )

    status, generated = request(
        address,
        "POST",
        "/api/issue-driven/generate",
        json.dumps({"json": source}),
        token=token,
    )

    assert status == 200
    assert generated["issueDrivenValidation"] == []
    assert generated["config"]["mode"] == "issue-driven"
    assert generated["config"]["implementer_agent"] == "claude"
    assert generated["config"]["reviewer_agent"] == "codex"
    ast.parse(generated["generatedCode"])

    status, rejected = request(
        address,
        "POST",
        "/api/issue-driven/generate",
        json.dumps({"json": "{}"}),
        token=token,
    )
    assert status == 422
    assert rejected["error"] == "issue-driven JSON validation failed"
    assert rejected["issueDrivenValidation"]


def test_issue_driven_generation_api_rejects_unpaired_task_surrogate(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    source = json.dumps(
        {
            "repository": "/tmp/example",
            "integration_branch": "dev/v0.2.0",
            "final_branch": "main",
            "work_items": [{"id": "invalid-unicode", "task": "Do it \ud800"}],
            "max_reviews": 5,
            "merge_to_integration": True,
            "final_review": True,
            "merge_final": False,
        }
    )

    status, rejected = request(
        address,
        "POST",
        "/api/issue-driven/generate",
        json.dumps({"json": source}),
        token=token,
    )

    assert status == 422
    assert rejected["issueDrivenValidation"] == [
        {
            "path": "$.work_items[0].task",
            "message": "must contain only Unicode scalar values",
        }
    ]


@pytest.mark.parametrize("policy_issue", ["200", "\ud800"])
def test_issue_driven_generation_api_rejects_invalid_policy_identity(
    web_server: tuple[tuple[str, int], str], policy_issue: str
) -> None:
    address, token = web_server
    source = json.dumps(
        {
            "repository": "/tmp/example",
            "integration_branch": "dev/v0.2.0",
            "final_branch": "main",
            "issues": [90],
            "policy_issue": policy_issue,
            "max_reviews": 5,
            "merge_to_integration": True,
            "final_review": True,
            "merge_final": False,
        }
    )

    status, rejected = request(
        address,
        "POST",
        "/api/issue-driven/generate",
        json.dumps({"json": source}),
        token=token,
    )

    assert status == 422
    assert rejected["issueDrivenValidation"] == [
        {"path": "$.policy_issue", "message": "must be a positive integer"}
    ]


def test_runner_page_exposes_agent_workflow_manager_favicon(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, _ = web_server
    documents = {}
    for path in ["/", "/favicon.svg"]:
        connection = http.client.HTTPConnection(*address, timeout=3)
        connection.request("GET", path)
        response = connection.getresponse()
        documents[path] = (response.getheader("Content-Type"), response.read().decode())
        connection.close()
        assert response.status == 200

    assert (
        '<link id="favicon" rel="icon" href="/favicon.svg" '
        'type="image/svg+xml" sizes="any">'
    ) in documents["/"][1]
    assert documents["/favicon.svg"][0] == "image/svg+xml"
    assert documents["/favicon.svg"][1].startswith(
        '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" '
        'viewBox="0 0 32 32">'
    )


def test_runner_page_exposes_copy_actions_and_shared_helper(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, _ = web_server
    documents = {}
    for path in ["/", "/log-display.js", "/output-copy.js", "/app.js"]:
        connection = http.client.HTTPConnection(*address, timeout=3)
        connection.request("GET", path)
        response = connection.getresponse()
        documents[path] = response.read().decode()
        connection.close()
        assert response.status == 200

    index = documents["/"]
    log_display = documents["/log-display.js"]
    helper = documents["/output-copy.js"]
    script = documents["/app.js"]
    assert 'id="output-copy"' in index
    assert 'id="guide-copy"' in index
    assert 'id="guide-raw"' in index
    assert '<script src="/log-display.js"></script>' in index
    assert "formatOutputEntries" in log_display
    assert "writeText" in helper
    assert 'execCommand("copy")' in helper
    assert "runnerOutputClipboard.writeText" in script
    assert 'guideCopy.textContent = "Copy manually"' in script
    assert 'outputCopy.textContent = "Copy manually"' in script
    assert 'id="manual-copy-dialog"' in index


def test_runner_serves_issue_driven_guide(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, _ = web_server
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.request("GET", "/issue-driven-guide.md")
    response = connection.getresponse()
    guide = response.read().decode()
    connection.close()

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/markdown; charset=utf-8"
    assert guide.startswith("# Issue Driven Guide")


def test_copy_browser_logic() -> None:
    test_file = Path(__file__).with_name("test_output_copy.js")

    subprocess.run(["node", "--test", str(test_file)], check=True)


def test_runner_browser_logic() -> None:
    test_file = Path(__file__).with_name("test_app.js")

    subprocess.run(["node", "--test", str(test_file)], check=True)


def test_validation_api_and_run_preflight_report_distinct_state(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    body = json.dumps({"code": "def broken("})

    status, validated = request(address, "POST", "/api/validate", body, token=token)
    assert status == 422
    assert validated["state"] == "validation_failed"
    assert validated["runId"] is None
    assert validated["validation"][0]["kind"] == "syntax"
    assert validated["validation"][0]["line"] == 1

    status, rejected = request(address, "POST", "/api/run", body, token=token)
    assert status == 422
    assert rejected["error"] == "workflow validation failed"
    assert rejected["state"] == "validation_failed"
    assert rejected["runId"] is None


def test_workflow_api_validates_and_snapshots_static_outline(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    code = "WORKFLOW_OUTLINE = ['prepare', 'execute']\nprint('done')"
    body = json.dumps({"code": code})

    status, validated = request(address, "POST", "/api/validate", body, token=token)
    assert status == 200
    assert validated["outline"] == ["prepare", "execute"]

    status, started = request(address, "POST", "/api/run", body, token=token)
    assert status == 202
    run_id = int(started["runId"])
    assert started["outline"] == ["prepare", "execute"]
    assert request(address, "GET", f"/api/runs/{run_id}")[1]["outline"] == [
        "prepare",
        "execute",
    ]

    malformed = json.dumps({"code": "WORKFLOW_OUTLINE = build_outline()"})
    status, rejected = request(address, "POST", "/api/validate", malformed, token=token)
    assert status == 422
    assert rejected["outline"] == []
    assert rejected["validation"][0]["kind"] == "outline"


@pytest.mark.parametrize("path", ["/api/validate", "/api/dry-run", "/api/run"])
def test_workflow_api_reports_unresolvable_preflight_path(
    web_server: tuple[tuple[str, int], str], path: str
) -> None:
    address, token = web_server
    status, result = request(
        address,
        "POST",
        path,
        json.dumps({"code": "WORKFLOW_PREFLIGHT = {'paths': ['~unknown-user/input']}"}),
        token=token,
    )

    assert status == 422
    assert result["state"] == "validation_failed"
    assert result["validation"][0]["kind"] == "path"
    assert "could not check required path" in result["validation"][0]["message"]


def test_dry_run_api_reports_first_mutation_and_findings(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, token = web_server
    code = """
WORKFLOW_DRY_RUN = 1
from purplemux_client import emit_finding
from purplemux_client.operations import execute_mutation, Reconciliation, MutationResolution
emit_finding("git", "base ref verified")
execute_mutation(
    operation="switch branch", target="feature/example", pre_state="main",
    dispatch=lambda: None,
    reconcile=lambda _: Reconciliation(MutationResolution.UNKNOWN),
    plan={"kind": "switch", "branch": "feature/example"},
)
"""
    status, payload = request(
        address, "POST", "/api/dry-run", json.dumps({"code": code}), token=token
    )

    assert status == 200
    assert payload["dryRun"]["status"] == "frontier"
    assert payload["dryRun"]["nextMutation"]["operation"] == "switch branch"
    assert payload["dryRun"]["findings"] == [
        {"category": "git", "status": "passed", "message": "base ref verified"}
    ]


class ReadinessAPIService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def snapshot(self) -> dict[str, object]:
        return {
            "workspaces": [
                {"id": "ws-1", "name": "Existing", "directories": ["/repo"]}
            ],
            "running": False,
            "probe": None,
        }

    def probe(self, *, workspace_id: str, provider: str) -> AgentReadinessStatus:
        self.calls.append((workspace_id, provider))
        return AgentReadinessStatus(
            "succeeded",
            workspace_id,
            "Existing",
            provider,
            "awm-readiness-codex-api123",
            "api123",
            "tab-probe",
            "ready",
            "confirmed",
        )

    def reconcile(self) -> AgentReadinessStatus:
        raise ValueError("there is no unresolved Agent readiness probe")


class BlockedReadinessAPIService(ReadinessAPIService):
    def __init__(self) -> None:
        super().__init__()
        self.blocked = False

    def probe(self, *, workspace_id: str, provider: str) -> AgentReadinessStatus:
        if self.blocked:
            raise ReadinessReconciliationRequired("probe must be reconciled")
        self.calls.append((workspace_id, provider))
        self.blocked = True
        return AgentReadinessStatus(
            "unknown",
            workspace_id,
            "Existing",
            provider,
            "awm-readiness-codex-api123",
            "api123",
            None,
            "not-observed",
            "not-attempted",
            "create outcome unknown",
        )

    def reconcile(self) -> AgentReadinessStatus:
        self.blocked = False
        return AgentReadinessStatus(
            "reconciled",
            "ws-1",
            "Existing",
            "codex",
            "awm-readiness-codex-api123",
            "api123",
            None,
            "not-observed",
            "confirmed-absent",
        )


def test_agent_readiness_api_is_explicit_and_reports_separate_cleanup() -> None:
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    readiness = ReadinessAPIService()
    server = RunnerHTTPServer(
        ("127.0.0.1", 0),
        runner,
        readiness_service=readiness,  # type: ignore[arg-type]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = (str(server.server_address[0]), int(server.server_address[1]))
    try:
        status, available = request(address, "GET", "/api/readiness")
        assert status == 200
        assert available["workspaces"][0]["id"] == "ws-1"
        assert readiness.calls == []

        for path in ("/api/validate", "/api/dry-run"):
            status, _ = request(
                address,
                "POST",
                path,
                json.dumps({"code": "WORKFLOW_DRY_RUN = 1"}),
                token=server.request_token,
            )
            assert status == 200
        assert readiness.calls == []

        status, result = request(
            address,
            "POST",
            "/api/readiness/probe",
            json.dumps({"workspaceId": "ws-1", "provider": "codex"}),
            token=server.request_token,
        )
        assert status == 200
        assert readiness.calls == [("ws-1", "codex")]
        assert result["probe"]["readiness"] == "ready"
        assert result["probe"]["cleanup"] == "confirmed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_agent_readiness_api_blocks_retry_until_explicit_reconciliation() -> None:
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    readiness = BlockedReadinessAPIService()
    server = RunnerHTTPServer(
        ("127.0.0.1", 0),
        runner,
        readiness_service=readiness,  # type: ignore[arg-type]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = (str(server.server_address[0]), int(server.server_address[1]))
    body = json.dumps({"workspaceId": "ws-1", "provider": "codex"})
    try:
        assert (
            request(
                address,
                "POST",
                "/api/readiness/probe",
                body,
                token=server.request_token,
            )[0]
            == 200
        )
        status, blocked = request(
            address,
            "POST",
            "/api/readiness/probe",
            body,
            token=server.request_token,
        )
        assert status == 409
        assert "reconciled" in str(blocked["error"])
        assert readiness.calls == [("ws-1", "codex")]

        status, reconciled = request(
            address,
            "POST",
            "/api/readiness/reconcile",
            "{}",
            token=server.request_token,
        )
        assert status == 200
        assert reconciled["probe"]["status"] == "reconciled"
        assert (
            request(
                address,
                "POST",
                "/api/readiness/probe",
                body,
                token=server.request_token,
            )[0]
            == 200
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_notification_settings_api_read_never_returns_token(
    settings_web_server: tuple[
        tuple[str, int], str, Path, Path, SettingsAPINotifier, PythonRunner
    ],
) -> None:
    address, _, _, _, _, _ = settings_web_server

    status, payload = request(address, "GET", "/api/settings/notifications")

    assert status == 200
    assert payload["credentialStatus"] == "configured"
    assert payload["server"] == "https://notify.example"
    assert "tk_api_secret" not in json.dumps(payload)
    assert "token" not in json.dumps(payload).lower()


def test_notification_settings_api_honors_environment_only_credential(
    tmp_path: Path,
) -> None:
    runtime_config = tmp_path / "config.sh"
    notify_config = tmp_path / "notify/config"
    runtime_config.write_text("AGENT_WORKFLOW_MANAGER_NOTIFICATIONS=enabled\n")
    notify_config.parent.mkdir()
    notify_config.write_text(
        "NOTIFY_SERVER=https://file.example\nNOTIFY_TOPIC=file-topic\n",
        encoding="utf-8",
    )
    notifier = SettingsAPINotifier()
    settings = NotificationSettings(
        runtime_config=runtime_config,
        notify_config=notify_config,
        notifier=notifier,
        environment={
            "NOTIFY_SERVER": "https://environment.example",
            "NOTIFY_TOPIC": "environment-topic",
            "NOTIFY_TOKEN": "tk_environment_api_secret",
        },
    )
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    server = RunnerHTTPServer(("127.0.0.1", 0), runner, notification_settings=settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    address = (str(host), int(port))
    trusted_host = f"{host}:{port}"
    try:
        status, read_payload = request(address, "GET", "/api/settings/notifications")
        test_status, test_payload = request(
            address,
            "POST",
            "/api/settings/notifications/test",
            "{}",
            token=server.request_token,
            origin=f"http://{trusted_host}",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert status == 200
    assert read_payload["credentialStatus"] == "configured"
    assert read_payload["server"] == "https://environment.example"
    assert "tk_environment_api_secret" not in json.dumps(read_payload)
    assert test_status == 200
    assert test_payload["delivered"] is True
    assert notifier.transport_calls == [
        (
            "https://environment.example",
            "environment-topic",
            "tk_environment_api_secret",
        )
    ]


def test_notification_settings_api_write_applies_immediately(
    settings_web_server: tuple[
        tuple[str, int], str, Path, Path, SettingsAPINotifier, PythonRunner
    ],
) -> None:
    address, token, runtime_config, notify_config, notifier, _ = settings_web_server
    host = f"{address[0]}:{address[1]}"
    secret = "tk_replaced_api_secret"

    status, payload = request(
        address,
        "POST",
        "/api/settings/notifications",
        json.dumps(
            {
                "enabled": False,
                "onSuccess": False,
                "onFailure": True,
                "onStopped": True,
                "server": "https://new-notify.example",
                "topic": "runner_team",
                "replacementToken": secret,
            }
        ),
        token=token,
        origin=f"http://{host}",
    )

    assert status == 200
    assert payload["enabled"] is False
    assert payload["credentialStatus"] == "configured"
    assert payload["restartRequired"] is False
    assert secret not in json.dumps(payload)
    assert notifier.current_policy == (False, False, True, True)
    assert secret not in runtime_config.read_text(encoding="utf-8")
    assert secret in notify_config.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "invalid_server",
    [
        "https://[",
        "https://notify.example\r\nNOTIFY_TOKEN=injected",
        "https://x\t",
        "https://notify.example\u0085NOTIFY_TOKEN=injected",
        "https://notify.example\u2028NOTIFY_TOKEN=injected",
        "https://notify.example\u2029NOTIFY_TOKEN=injected",
    ],
)
def test_notification_settings_api_rejects_malformed_server_without_writes(
    settings_web_server: tuple[
        tuple[str, int], str, Path, Path, SettingsAPINotifier, PythonRunner
    ],
    invalid_server: str,
) -> None:
    address, token, runtime_config, notify_config, _, _ = settings_web_server
    host = f"{address[0]}:{address[1]}"
    original_runtime = runtime_config.read_bytes()
    original_notify = notify_config.read_bytes()

    status, payload = request(
        address,
        "POST",
        "/api/settings/notifications",
        json.dumps(
            {
                "enabled": True,
                "onSuccess": True,
                "onFailure": True,
                "onStopped": False,
                "server": invalid_server,
                "topic": "agents",
            }
        ),
        token=token,
        origin=f"http://{host}",
    )

    assert status == 400
    assert payload == {"error": "Notify server must be a valid HTTP(S) URL."}
    assert runtime_config.read_bytes() == original_runtime
    assert notify_config.read_bytes() == original_notify


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_notification_settings_api_rejects_unicode_separator_token_without_writes(
    settings_web_server: tuple[
        tuple[str, int], str, Path, Path, SettingsAPINotifier, PythonRunner
    ],
    separator: str,
) -> None:
    address, token, runtime_config, notify_config, _, _ = settings_web_server
    host = f"{address[0]}:{address[1]}"
    original_runtime = runtime_config.read_bytes()
    original_notify = notify_config.read_bytes()

    status, payload = request(
        address,
        "POST",
        "/api/settings/notifications",
        json.dumps(
            {
                "enabled": True,
                "onSuccess": True,
                "onFailure": True,
                "onStopped": False,
                "server": "https://notify.example",
                "topic": "agents",
                "replacementToken": f"tk_before{separator}NOTIFY_TOKEN=injected",
            }
        ),
        token=token,
        origin=f"http://{host}",
    )

    assert status == 400
    assert payload == {"error": "Replacement token is invalid."}
    assert runtime_config.read_bytes() == original_runtime
    assert notify_config.read_bytes() == original_notify


def test_notification_test_api_success_does_not_change_runner_state(
    settings_web_server: tuple[
        tuple[str, int], str, Path, Path, SettingsAPINotifier, PythonRunner
    ],
) -> None:
    address, token, _, _, notifier, runner = settings_web_server
    host = f"{address[0]}:{address[1]}"
    before = runner.snapshot()

    status, payload = request(
        address,
        "POST",
        "/api/settings/notifications/test",
        "{}",
        token=token,
        origin=f"http://{host}",
    )

    assert status == 200
    assert payload == {"delivered": True, "message": "Test notification sent."}
    assert notifier.test_calls == 1
    assert runner.snapshot() == before


def test_notification_test_api_cli_failure_is_sanitized(
    settings_web_server: tuple[
        tuple[str, int], str, Path, Path, SettingsAPINotifier, PythonRunner
    ],
) -> None:
    address, token, _, _, notifier, _ = settings_web_server
    host = f"{address[0]}:{address[1]}"
    notifier.test_result = NotificationResult(
        True, False, "notify stderr contained tk_api_secret"
    )

    status, payload = request(
        address,
        "POST",
        "/api/settings/notifications/test",
        "{}",
        token=token,
        origin=f"http://{host}",
    )

    assert status == 502
    assert payload == {
        "error": "Notify failed; check the server, topic, token, and network."
    }
    assert "tk_api_secret" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("token_kind", "origin_kind", "host_kind"),
    [
        ("missing", "trusted", "trusted"),
        ("wrong", "trusted", "trusted"),
        ("valid", "untrusted", "trusted"),
        ("valid", "trusted", "untrusted"),
    ],
)
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/settings/notifications/test", "{}"),
        (
            "/api/settings/notifications",
            json.dumps(
                {
                    "enabled": True,
                    "onSuccess": True,
                    "onFailure": True,
                    "onStopped": False,
                    "server": "https://notify.example",
                    "topic": "agents",
                }
            ),
        ),
    ],
)
def test_notification_settings_mutation_rejects_untrusted_request(
    settings_web_server: tuple[
        tuple[str, int], str, Path, Path, SettingsAPINotifier, PythonRunner
    ],
    token_kind: str,
    origin_kind: str,
    host_kind: str,
    path: str,
    body: str,
) -> None:
    address, actual_token, runtime_config, notify_config, notifier, _ = (
        settings_web_server
    )
    original_runtime = runtime_config.read_bytes()
    original_notify = notify_config.read_bytes()
    trusted_host = f"{address[0]}:{address[1]}"
    supplied_token = (
        actual_token
        if token_kind == "valid"
        else "wrong"
        if token_kind == "wrong"
        else None
    )
    origin = (
        f"http://{trusted_host}"
        if origin_kind == "trusted"
        else "https://attacker.example"
    )
    host = trusted_host if host_kind == "trusted" else "attacker.example"

    status, payload = request(
        address,
        "POST",
        path,
        body,
        token=supplied_token,
        origin=origin,
        host=host,
    )

    assert status == 403
    assert payload == {"error": "untrusted request"}
    assert notifier.test_calls == 0
    assert runtime_config.read_bytes() == original_runtime
    assert notify_config.read_bytes() == original_notify


@pytest.mark.parametrize(
    ("path", "token", "origin"),
    [
        (path, token, origin)
        for path in (
            "/api/directories",
            "/api/prompt",
            "/api/run",
            "/api/validate",
            "/api/dry-run",
            "/api/readiness/probe",
            "/api/readiness/reconcile",
        )
        for token, origin in (
            (None, None),
            ("wrong", None),
            ("valid", "https://attacker.example"),
            ("valid", "http://["),
        )
    ],
)
def test_workflow_action_rejects_untrusted_request(
    web_server: tuple[tuple[str, int], str],
    path: str,
    token: str | None,
    origin: str | None,
) -> None:
    address, actual_token = web_server
    status, payload = request(
        address,
        "POST",
        path,
        json.dumps({"code": 'print("must not run")'}),
        token=actual_token if token == "valid" else token,
        origin=origin,
    )

    assert status == 403
    assert payload == {"error": "untrusted request"}


def test_token_rejects_untrusted_host(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, _ = web_server
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.request("GET", "/api/token", headers={"Host": "attacker.example"})
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()

    assert response.status == 403
    assert payload == {"error": "untrusted host"}


@pytest.mark.parametrize("delimiter", ["?", "#", "?#"])
def test_run_rejects_matching_origin_with_empty_delimiter(
    web_server: tuple[tuple[str, int], str], delimiter: str
) -> None:
    address, token = web_server
    host, port = address
    status, payload = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": ""}),
        token=token,
        origin=f"http://{host}:{port}{delimiter}",
    )

    assert status == 403
    assert payload == {"error": "untrusted request"}


def test_explicit_remote_bind_accepts_only_matching_host_origin_and_token() -> None:
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    server = RunnerHTTPServer(("127.0.0.2", 0), runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    address = (str(host), int(port))
    trusted_host = f"127.0.0.2:{port}"
    trusted_origin = f"http://{trusted_host}"
    try:
        connection = http.client.HTTPConnection(*address, timeout=3)
        connection.request("GET", "/")
        response = connection.getresponse()
        page = response.read()
        connection.close()
        assert response.status == 200
        assert b"Python Runner" in page

        status, token_payload = request(address, "GET", "/api/token")
        assert status == 200
        token = str(token_payload["token"])
        status, connection = request(address, "GET", "/api/settings/mobile-connection")
        assert status == 200
        assert connection == {"url": None}
        status, _ = request(address, "GET", "/api/status")
        assert status == 200

        status, _ = request(
            address,
            "POST",
            "/api/run",
            json.dumps({"code": ""}),
            origin=trusted_origin,
        )
        assert status == 403
        status, _ = request(
            address,
            "POST",
            "/api/run",
            json.dumps({"code": ""}),
            token=token,
            origin="http://127.0.0.3:8765",
        )
        assert status == 403
        status, _ = request(
            address,
            "POST",
            "/api/run",
            json.dumps({"code": ""}),
            token=token,
            origin=trusted_origin,
            host="127.0.0.3:8765",
        )
        assert status == 403

        status, started = request(
            address,
            "POST",
            "/api/run",
            json.dumps({"code": "import time; time.sleep(60)"}),
            token=token,
            origin=trusted_origin,
        )
        assert status == 202
        assert started["state"] == "running"
        status, stopped = request(
            address,
            "POST",
            "/api/stop",
            token=token,
            origin=trusted_origin,
        )
        assert status == 202
        assert stopped["stopped"] is True
        assert wait_until_finished(runner).state == "stopped"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "not-an-ip"])
def test_web_cli_rejects_non_explicit_ipv4_bind(host: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--host", host])


def test_web_cli_accepts_explicit_remote_ipv4_bind() -> None:
    args = build_parser().parse_args(["--host", "100.64.10.20"])

    assert args.host == "100.64.10.20"


def test_mobile_connection_uses_remote_browser_origin_only() -> None:
    alias_origin = "http://runner.example.ts.net:8765"

    assert mobile_connection_url("100.64.10.20", alias_origin) == alias_origin
    assert mobile_connection_url("192.168.50.20", alias_origin) == alias_origin
    assert mobile_connection_url("127.0.0.1", alias_origin) is None
    assert mobile_connection_url("localhost", alias_origin) is None
    assert mobile_connection_url("0.0.0.0", alias_origin) is None


def test_mobile_connection_qr_is_generated_as_svg() -> None:
    url = "http://runner.example.ts.net:8765"

    qr = RunnerHTTPServer._make_qr_svg(url)

    assert b"<svg" in qr
    assert b"<path" in qr


@pytest.mark.parametrize(
    "aliases",
    [
        "*.ts.net",
        "https://runner.ts.net",
        "runner.ts.net/path",
        "runner.ts.net?query",
        "user@runner.ts.net",
        "runner.ts.net\rforged",
        "runner..ts.net",
        "127.0.0.1",
        "127.1",
        "2130706433",
        "0x7f000001",
        "0x",
        "0X",
        "0x.1",
        "0x000000001",
        "0000000000000001",
    ],
)
def test_web_cli_rejects_invalid_hostname_aliases(aliases: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--host-aliases", aliases])


def test_configured_hostname_alias_accepts_get_and_protected_post() -> None:
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    server = RunnerHTTPServer(
        ("127.0.0.1", 0),
        runner,
        host_aliases=("E-Ryzen.tail6bc726.ts.net.",),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bound_host, bound_port = server.server_address
    address = (str(bound_host), int(bound_port))
    alias_host = f"e-ryzen.tail6bc726.ts.net:{bound_port}"
    try:
        status, _ = request(address, "GET", "/api/status", host=alias_host)
        assert status == 200

        status, token_payload = request(address, "GET", "/api/token", host=alias_host)
        assert status == 200
        token = str(token_payload["token"])

        status, result = request(
            address,
            "POST",
            "/api/run",
            json.dumps({"code": 'print("ALIAS_OK")'}),
            token=token,
            host=alias_host,
            origin=f"http://{alias_host}",
        )
        assert status == 202
        assert result["state"] == "running"
        assert wait_until_finished(runner).state == "success"

        status, _ = request(
            address,
            "POST",
            "/api/run",
            json.dumps({"code": ""}),
            host=alias_host,
            origin=f"http://{alias_host}",
        )
        assert status == 403
        status, _ = request(
            address,
            "POST",
            "/api/run",
            json.dumps({"code": ""}),
            token=token,
            host=alias_host,
            origin=f"http://unknown.tail6bc726.ts.net:{bound_port}",
        )
        assert status == 403
        status, _ = request(
            address,
            "GET",
            "/api/status",
            host=f"unknown.tail6bc726.ts.net:{bound_port}",
        )
        assert status == 403

        status, _ = request(address, "GET", "/api/status")
        assert status == 200
        status, _ = request(
            address, "GET", "/api/status", host=f"localhost:{bound_port}"
        )
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_local_only_server_does_not_offer_a_mobile_connection_qr() -> None:
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    server = RunnerHTTPServer(("127.0.0.1", 0), runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    address = (str(host), int(port))
    try:
        status, connection = request(address, "GET", "/api/settings/mobile-connection")
        qr_status, qr_error = request(
            address, "GET", "/api/settings/mobile-connection/qr.svg"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert status == 200
    assert connection == {"url": None}
    assert qr_status == 404
    assert "local-only" in str(qr_error["error"])


def test_server_allows_explicitly_requested_hostname() -> None:
    server = RunnerHTTPServer(("localhost", 0))
    try:
        _, port = server.server_address
        assert server.is_allowed_host(f"localhost:{port}")
    finally:
        server.server_close()


def test_web_server_close_stops_running_process() -> None:
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    server = RunnerHTTPServer(("127.0.0.1", 0), runner)
    runner.start("import time; time.sleep(60)")

    server.server_close()

    assert wait_until_finished(runner).state == "stopped"


def test_web_server_bind_failure_preserves_address_in_use_error() -> None:
    first = RunnerHTTPServer(("127.0.0.1", 0))
    address = first.server_address
    try:
        with pytest.raises(OSError, match="Address already in use"):
            RunnerHTTPServer(address)
    finally:
        first.server_close()
