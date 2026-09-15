from __future__ import annotations

import json
import time
from pathlib import Path
from urllib import request

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
        assert restored.snapshot(child_id).parent_run == child_snapshot.parent_run
        assert restored.snapshot(child_id).stdout == child_snapshot.stdout
        assert restored.snapshot(child_id).stderr == child_snapshot.stderr
        assert restored.snapshot(child_id).progress == child_snapshot.progress
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


def test_failed_child_launch_is_persisted_before_another_history_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from purplemux_client.runner import RunAttempt

    history = tmp_path / "history.json"
    runner = PythonRunner(managed_workflows=False, run_history_file=history)
    try:
        parent_id = runner.start("import time; time.sleep(60)")

        def fail_spawn(*args, **kwargs):
            raise OSError("child process could not be spawned")

        monkeypatch.setattr(runner, "_spawn_process", fail_spawn)
        with pytest.raises(OSError, match="child process could not be spawned"):
            runner.start("pass", parent_run_id=parent_id)
        child_id = int(runner.snapshot(parent_id).child_runs[0].rsplit("-", 1)[1])
        failed = runner.snapshot(child_id)
        assert failed.state == "failed"
        assert failed.exit_code == 1
        assert (
            failed.stderr
            == "Workflow launch failed: child process could not be spawned\n"
        )
        assert failed.attempts == (RunAttempt(1, "failed", 1),)
        assert runner.snapshot(parent_id).state == "running"

        # Freeze the immediate failure history before Stop/close can write it again.
        recovery_history = tmp_path / "recovery-history.json"
        recovery_history.write_bytes(history.read_bytes())
        restored = PythonRunner(
            managed_workflows=False, run_history_file=recovery_history
        )
        try:
            result = restored.snapshot(child_id)
            assert result.state == failed.state
            assert result.exit_code == failed.exit_code
            assert result.stderr_entries == failed.stderr_entries
            assert result.attempts == failed.attempts
            assert result.parent_run == failed.parent_run
        finally:
            restored.close()
    finally:
        runner.close()


@pytest.mark.parametrize(
    "outcome", ["success", "failed", "stopped", "timeout", "unknown"]
)
def test_external_child_workflow(outcome, tmp_path, monkeypatch):
    import threading
    from contextlib import ExitStack

    from purplemux_client.external_runs import ExternalRunError
    from purplemux_client.external_targets import ExternalTargetSettings
    from purplemux_client.web import RunnerHTTPServer

    with ExitStack() as stack:
        remote = PythonRunner(
            managed_workflows=False,
            run_history_file=tmp_path / "remote.json",
            stop_timeout=0.2,
        )
        local = PythonRunner(
            managed_workflows=False, run_history_file=tmp_path / "local.json"
        )
        stack.callback(remote.close)
        stack.callback(local.close)
        settings = ExternalTargetSettings(tmp_path / "targets.json", environment={})
        server = RunnerHTTPServer(("127.0.0.1", 0), remote)
        stack.callback(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join)
        stack.callback(server.shutdown)
        settings._environment = {"REMOTE_TOKEN": server.request_token}
        source = RunnerHTTPServer(
            ("127.0.0.1", 0), local, external_target_settings=settings
        )
        stack.callback(source.server_close)
        source_thread = threading.Thread(target=source.serve_forever, daemon=True)
        source_thread.start()
        stack.callback(source_thread.join)
        stack.callback(source.shutdown)
        registration = {
            "targets": [
                {
                    "id": "remote",
                    "destination": f"http://127.0.0.1:{server.server_address[1]}",
                    "tokenEnv": "REMOTE_TOKEN",
                }
            ]
        }
        message = request.Request(
            f"http://127.0.0.1:{source.server_address[1]}/api/settings/external-targets",
            data=json.dumps(registration).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Python-Runner-Token": source.request_token,
            },
        )
        with request.urlopen(message, timeout=5) as response:
            registered = json.load(response)
        assert registered["targets"][0]["credentialStatus"] == "configured"
        assert server.request_token not in json.dumps(registered)
        assert (
            ExternalTargetSettings(settings.path, environment={}).read()["targets"][0][
                "id"
            ]
            == "remote"
        )
        child_code = 'from purplemux_client import emit_step\nemit_step("remote", "completed")\nprint("remote output", flush=True)\n'
        if outcome == "failed":
            child_code += 'raise RuntimeError("remote failure")\n'
        elif outcome in {"stopped", "timeout", "unknown"}:
            child_code += "import time; time.sleep(60)\n"
        if outcome == "unknown":

            def unavailable(*args, **kwargs):
                raise ExternalRunError("observation unavailable; outcome unknown")

            monkeypatch.setattr(
                local._external_child_client, "get_run_result", unavailable
            )
        workflow = f"""
from purplemux_client import start_child_run, wait_child_run, get_child_run_result, ExternalRunError
child_id = start_child_run({child_code!r}, target_id="remote")
print(child_id, flush=True)
try:
    result = wait_child_run(child_id, target_id="remote", timeout={0.05 if outcome == "timeout" else 10})
    assert result == get_child_run_result(child_id, target_id="remote")
    print(result.state, result.stdout, flush=True)
except (TimeoutError, ExternalRunError) as exc:
    print(type(exc).__name__, flush=True)
"""
        original_spawn = remote._spawn_process
        received_parents = []

        def spawn_remote(code, **kwargs):
            child = remote._runs[max(remote._runs)]
            records = json.loads((tmp_path / "remote.json").read_text())
            assert records["runFamilyLinks"][child.parent_run] == [
                remote._run_identity(child.run_id)
            ]
            received_parents.append(child.parent_run)
            return original_spawn(code, **kwargs)

        monkeypatch.setattr(remote, "_spawn_process", spawn_remote)
        parent_id = local.start(workflow)
        deadline = time.monotonic() + 15
        while not local.snapshot(parent_id).child_runs:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        parent_identity = local._run_identity(parent_id)
        child_identity = local.snapshot(parent_id).child_runs[0]
        child_id = int(child_identity.rsplit("-", 1)[1])
        assert child_identity == remote._run_identity(child_id)
        assert remote.snapshot(child_id).parent_run == parent_identity
        assert received_parents == [parent_identity]
        if outcome == "stopped":
            remote.stop(child_id)
        while local.snapshot(parent_id).state == "running":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        parent = local.snapshot(parent_id)
        assert parent.state == "success", parent.stderr
        expected = {"timeout": "TimeoutError", "unknown": "ExternalRunError"}.get(
            outcome, outcome
        )
        assert expected in parent.stdout
        if outcome in {"success", "failed"}:
            assert "remote output" in parent.stdout
        if outcome in {"timeout", "unknown"}:
            assert remote.snapshot(child_id).state == "running"
        assert json.loads((tmp_path / "local.json").read_text())["runFamilyLinks"][
            parent_identity
        ] == [child_identity]
        assert json.loads((tmp_path / "remote.json").read_text())["runFamilyLinks"][
            parent_identity
        ] == [child_identity]
        if outcome in {"success", "failed"}:
            assert remote.snapshot(child_id).state == outcome
            assert remote.snapshot(child_id).progress[0].name == "remote"
            child_result = local._external_child_client.get_run_result(
                "remote", child_id
            )
            assert child_result is not None
            assert child_result.exit_code == remote.snapshot(child_id).exit_code
            assert child_result.stdout == remote.snapshot(child_id).stdout
            assert child_result.stderr == remote.snapshot(child_id).stderr
            if outcome == "failed":
                assert child_result.exit_code != 0
                assert "remote failure" in child_result.stderr
        with pytest.raises(PermissionError):
            local._workflow_control(
                local._runs[parent_id].control_token,
                {"operation": "result", "target_id": "remote", "run_id": child_id},
            )
        local.close()
        remote.close()
        # Reload independent histories after both instances have shut down.
        for runner, name, run_id in (
            (local, "local", parent_id),
            (remote, "remote", child_id),
        ):
            before = runner.snapshot(run_id)
            restored = PythonRunner(
                managed_workflows=False, run_history_file=tmp_path / f"{name}.json"
            )
            try:
                after = restored.snapshot(run_id)
                assert after.parent_run == before.parent_run
                assert after.child_runs == before.child_runs
                assert after.state == before.state
                assert after.stdout == before.stdout
                assert after.stderr == before.stderr
                assert after.progress == before.progress
            finally:
                restored.close()


@pytest.mark.parametrize("received_identity", [True, False])
def test_external_launch_unknown_retains_received_identity(
    tmp_path, monkeypatch, received_identity
):
    from purplemux_client.external_runs import (
        ExternalRunClient,
        ExternalRunLaunchUnknown,
    )

    runner = PythonRunner(
        managed_workflows=False, run_history_file=tmp_path / "history.json"
    )
    try:
        parent_id = runner.start("import time; time.sleep(60)")
        client = ExternalRunClient()
        runner._external_child_client = client
        identity = "a" * 32 + "-42"
        calls = []

        def launch(target_id, path, payload):
            calls.append(payload)
            return {
                "runId": 42,
                "identity": identity if received_identity else None,
                "state": "unknown",
            }

        monkeypatch.setattr(client, "_request", launch)
        with pytest.raises(ExternalRunLaunchUnknown):
            runner._workflow_control(
                runner._runs[parent_id].control_token,
                {"operation": "start", "code": "pass", "target_id": "remote"},
            )
        assert len(calls) == 1
        assert calls[0]["parentRun"] == runner._run_identity(parent_id)
        expected = (identity,) if received_identity else ()
        assert runner.snapshot(parent_id).child_runs == expected
        assert json.loads((tmp_path / "history.json").read_text())[
            "runFamilyLinks"
        ].get(runner._run_identity(parent_id), []) == list(expected)
    finally:
        runner.close()


@pytest.mark.parametrize("observed_identity", [None, "b" * 32 + "-42"])
def test_external_child_result_requires_known_identity(observed_identity, monkeypatch):
    from purplemux_client.external_runs import ExternalRunClient, ExternalRunError

    client = ExternalRunClient()
    identity = "a" * 32 + "-42"
    client.run_identities["remote", 42] = identity
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: {
            "runId": 42,
            "identity": observed_identity,
            "state": "success",
            "result": {"exitCode": 0, "stdout": "wrong child", "stderr": ""},
        },
    )
    with pytest.raises(ExternalRunError):
        client.get_run_result("remote", 42)
    assert client.run_identities["remote", 42] == identity


def test_concurrent_external_collision_keeps_authorized_identity(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from purplemux_client.external_runs import (
        ExternalRunClient,
        ExternalRunError,
        ExternalRunLaunchUnknown,
    )

    runner = PythonRunner(
        managed_workflows=False, run_history_file=tmp_path / "history.json"
    )
    request_started = threading.Event()
    replacement_launched = threading.Event()
    try:
        parent_id = runner.start("import time; time.sleep(60)")
        other_parent_id = runner.start("import time; time.sleep(60)")
        client = ExternalRunClient()
        runner._external_child_client = client
        original = "a" * 32 + "-42"
        replacement = "b" * 32 + "-42"
        client.run_identities["remote", 42] = original
        runner.link_runs(runner._run_identity(parent_id), original)
        token = runner._runs[parent_id].control_token
        other_token = runner._runs[other_parent_id].control_token

        def exchange(target_id, path, payload=None, **kwargs):
            if payload is not None:
                return {"runId": 42, "identity": replacement, "state": "running"}
            request_started.set()
            assert replacement_launched.wait(5)
            return {
                "runId": 42,
                "identity": replacement,
                "state": "success",
                "result": {"exitCode": 0, "stdout": "replacement output", "stderr": ""},
            }

        monkeypatch.setattr(client, "_request", exchange)
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = workers.submit(
                runner._workflow_control,
                token,
                {"operation": "result", "target_id": "remote", "run_id": 42},
            )
            try:
                assert request_started.wait(5)
                with pytest.raises(ExternalRunLaunchUnknown):
                    runner._workflow_control(
                        other_token,
                        {"operation": "start", "target_id": "remote", "code": "pass"},
                    )
            finally:
                replacement_launched.set()
            with pytest.raises(ExternalRunError, match="identity changed"):
                pending.result(timeout=5)
        assert client.run_identities["remote", 42] == original
        assert runner.snapshot(parent_id).child_runs == (original,)
        assert runner.snapshot(other_parent_id).child_runs == (replacement,)
        with pytest.raises(PermissionError):
            runner._workflow_control(
                other_token,
                {"operation": "result", "target_id": "remote", "run_id": 42},
            )
        links = json.loads((tmp_path / "history.json").read_text())["runFamilyLinks"]
        assert links[runner._run_identity(parent_id)] == [original]
        assert links[runner._run_identity(other_parent_id)] == [replacement]
    finally:
        replacement_launched.set()
        runner.close()


def test_concurrent_external_client_first_use(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from purplemux_client import external_runs

    runner = PythonRunner(
        managed_workflows=False, run_history_file=tmp_path / "history.json"
    )
    client_type = external_runs.ExternalRunClient
    constructors = []
    start_together = threading.Barrier(2)
    constructors_together = threading.Barrier(2)
    try:
        parent_ids = [runner.start("import time; time.sleep(60)") for _ in range(2)]

        def make_client():
            client = client_type()
            constructors.append(client)
            # Allow competing constructors to overlap in the unsynchronized implementation.
            # A single synchronized constructor proceeds when the barrier times out.
            try:
                constructors_together.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass
            return client

        def exchange(self, target_id, path, payload=None, **kwargs):
            if payload is not None:
                run_id = int(payload["parentRun"].rsplit("-", 1)[1])
                return {
                    "runId": run_id,
                    "identity": "a" * 32 + f"-{run_id}",
                    "state": "running",
                }
            run_id = int(path.split("/")[-2])
            return {
                "runId": run_id,
                "identity": "a" * 32 + f"-{run_id}",
                "state": "success",
                "result": {"exitCode": 0, "stdout": str(run_id), "stderr": ""},
            }

        monkeypatch.setattr(external_runs, "ExternalRunClient", make_client)
        monkeypatch.setattr(client_type, "_request", exchange)

        def start(parent_id):
            start_together.wait(timeout=5)
            return runner._workflow_control(
                runner._runs[parent_id].control_token,
                {"operation": "start", "target_id": "remote", "code": "pass"},
            )["run_id"]

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(start, parent_id) for parent_id in parent_ids]
            child_ids = [future.result(timeout=5) for future in futures]
        assert len(constructors) == 1
        assert runner._external_child_client is constructors[0]
        for parent_id, child_id in zip(parent_ids, child_ids):
            result = runner._workflow_control(
                runner._runs[parent_id].control_token,
                {"operation": "result", "target_id": "remote", "run_id": child_id},
            )
            assert result["state"] == "success"
            assert result["stdout"] == str(child_id)
            assert runner.snapshot(parent_id).child_runs == ("a" * 32 + f"-{child_id}",)
    finally:
        runner.close()


@pytest.mark.parametrize("response_matches", [True, False])
def test_external_result_uses_authorized_identity(response_matches, monkeypatch):
    from purplemux_client.external_runs import ExternalRunClient, ExternalRunError

    client = ExternalRunClient()
    authorized = "a" * 32 + "-42"
    replacement = "b" * 32 + "-42"
    client.run_identities["remote", 42] = replacement
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: {
            "runId": 42,
            "identity": authorized if response_matches else replacement,
            "state": "success",
            "result": {"exitCode": 0, "stdout": "child output", "stderr": ""},
        },
    )
    if response_matches:
        result = client.get_run_result("remote", 42, _identity=authorized)
        assert result is not None and result.stdout == "child output"
    else:
        with pytest.raises(ExternalRunError):
            client.get_run_result("remote", 42, _identity=authorized)


@pytest.mark.parametrize("target_id", [None, "remote"])
def test_runnable_child_example(target_id, tmp_path):
    import threading
    from contextlib import ExitStack

    from purplemux_client.external_targets import ExternalTargetSettings
    from purplemux_client.web import RunnerHTTPServer

    with ExitStack() as stack:
        remote = PythonRunner(managed_workflows=False)
        local = PythonRunner(managed_workflows=False)
        stack.callback(remote.close)
        stack.callback(local.close)
        server = RunnerHTTPServer(("127.0.0.1", 0), remote)
        stack.callback(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join)
        stack.callback(server.shutdown)
        settings = ExternalTargetSettings(
            tmp_path / "targets.json", environment={"TOKEN": server.request_token}
        )
        settings.update(
            {
                "targets": [
                    {
                        "id": "remote",
                        "destination": f"http://127.0.0.1:{server.server_address[1]}",
                        "tokenEnv": "TOKEN",
                    }
                ]
            }
        )
        from purplemux_client.external_runs import ExternalRunClient

        local._external_child_client = ExternalRunClient(settings)
        code = (Path(__file__).parents[1] / "examples" / "child-runs.py").read_text()
        parent_id = local.start(code, args=[] if target_id is None else [target_id])
        deadline = time.monotonic() + 15
        while local.snapshot(parent_id).state == "running":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        parent = local.snapshot(parent_id)
        assert parent.state == "success", parent.stderr
        assert "hello AWM" in parent.stdout
        assert len(parent.child_runs) == 1
