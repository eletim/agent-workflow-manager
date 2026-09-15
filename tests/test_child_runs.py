from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from purplemux_client.runner import PythonRunner


@pytest.mark.parametrize("outcome", ["success", "failed", "stopped"])
def test_running_parent_awaits_child(outcome: str, tmp_path: Path) -> None:
    history = tmp_path / "history.json"
    child = 'from purplemux_client import emit_step\nemit_step("child", "completed")\nprint("child output")\n'
    if outcome == "failed":
        child += 'raise RuntimeError("child failure")\n'
    if outcome == "stopped":
        child += "import time\ntime.sleep(60)\n"
    code = f"""
from purplemux_client import start_child_run, wait_child_run, get_child_run_result
import json
child_id = start_child_run({child!r})
print(child_id, flush=True)
result = wait_child_run(child_id, timeout=10)
assert result == get_child_run_result(child_id)
print(json.dumps({{"state": result.state, "stdout": result.stdout}}), flush=True)
"""
    runner = PythonRunner(
        managed_workflows=False, run_history_file=history, stop_timeout=0.2
    )
    try:
        parent_id = runner.start(code)
        deadline = time.monotonic() + 10
        while not runner.snapshot(parent_id).child_runs:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        parent = runner.snapshot(parent_id)
        child_id = int(parent.child_runs[0].rsplit("-", 1)[1])
        child_snapshot = runner.snapshot(child_id)
        assert child_snapshot.parent_run is not None
        persisted = json.loads(history.read_text())
        assert str(child_id) in json.dumps(persisted)
        if outcome == "stopped":
            assert parent.state == "running"
            runner.stop(child_id)
        while runner.snapshot(parent_id).state == "running":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        parent = runner.snapshot(parent_id)
        assert parent.state == "success", parent.stderr
        assert json.loads(parent.stdout.splitlines()[-1])["state"] == outcome
        child_snapshot = runner.snapshot(child_id)
        assert child_snapshot.state == outcome
        if outcome != "stopped":
            assert "child output" in child_snapshot.stdout
            assert child_snapshot.progress[0].name == "child"
        if outcome == "failed":
            assert "child failure" in child_snapshot.stderr
    finally:
        runner.close()
    restored = PythonRunner(managed_workflows=False, run_history_file=history)
    try:
        assert restored.snapshot(parent_id).child_runs == parent.child_runs
        assert restored.snapshot(child_id).state == outcome
    finally:
        restored.close()


def test_family_is_persisted_before_child_execution(
    tmp_path: Path, monkeypatch
) -> None:
    from purplemux_client.correlation import RUN_IDENTITY_ENV

    history = tmp_path / "history.json"
    runner = PythonRunner(managed_workflows=False, run_history_file=history)
    original = runner._spawn_process
    observed = []

    def spawn(code, **kwargs):
        if code == 'print("child")':
            records = json.loads(history.read_text())
            identity = kwargs["child_env"][RUN_IDENTITY_ENV]
            assert records["runFamilyLinks"][runner._run_identity(1)] == [identity]
            assert runner._runs[2].parent_run == runner._run_identity(1)
            assert runner._runs[1].child_runs == (identity,)
            observed.append(identity)
        return original(code, **kwargs)

    monkeypatch.setattr(runner, "_spawn_process", spawn)
    try:
        parent_id = runner.start("""
from purplemux_client import start_child_run, wait_child_run
wait_child_run(start_child_run('print("child")'), timeout=5)
""")
        deadline = time.monotonic() + 10
        while runner.snapshot(parent_id).state == "running":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert runner.snapshot(parent_id).state == "success"
        assert len(observed) == 1
    finally:
        runner.close()


def test_control_rejects_unrelated_runs_and_wait_timeout() -> None:
    runner = PythonRunner(managed_workflows=False)
    try:
        unrelated_id = runner.start('print("unrelated")')
        parent_id = runner.start("""
from purplemux_client import start_child_run, wait_child_run, get_child_run_result
child_id = start_child_run('import time; time.sleep(60)')
assert get_child_run_result(child_id) is None
try:
    wait_child_run(child_id, timeout=0)
except TimeoutError:
    print("timeout", flush=True)
import time
time.sleep(60)
""")
        deadline = time.monotonic() + 10
        while "timeout" not in runner.snapshot(parent_id).stdout:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        token = runner._runs[parent_id].control_token
        with pytest.raises(PermissionError):
            runner._workflow_control("invalid", {"operation": "start", "code": "pass"})
        with pytest.raises(PermissionError):
            runner._workflow_control(
                token, {"operation": "result", "run_id": unrelated_id}
            )
        child_id = int(runner.snapshot(parent_id).child_runs[0].rsplit("-", 1)[1])
        assert runner.snapshot(child_id).state == "running"
        runner.stop(parent_id)
        with pytest.raises(PermissionError):
            runner._workflow_control(token, {"operation": "start", "code": "pass"})
    finally:
        runner.close()


def test_child_does_not_execute_when_family_write_fails(
    tmp_path: Path, monkeypatch
) -> None:
    from purplemux_client.runner import RunHistoryError

    runner = PythonRunner(
        managed_workflows=False, run_history_file=tmp_path / "history.json"
    )
    try:
        parent_id = runner.start("import time; time.sleep(60)")
        original_write = runner._write_run_history_locked
        spawned = []
        original_spawn = runner._spawn_process

        def write():
            if runner._runs[parent_id].child_runs:
                raise RunHistoryError("family write failed")
            original_write()

        def spawn(*args, **kwargs):
            spawned.append(True)
            return original_spawn(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(runner, "_write_run_history_locked", write)
            patch.setattr(runner, "_spawn_process", spawn)
            with pytest.raises(RunHistoryError):
                runner.start("pass", parent_run_id=parent_id)
            assert not spawned
    finally:
        runner.close()
