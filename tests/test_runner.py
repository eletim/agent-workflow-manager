from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest

from purplemux_client.notification_settings import NotificationSettings
from purplemux_client.notifier import NotificationResult
from purplemux_client.runner import (
    InvalidExecutionContextError,
    PythonRunner,
    RunnerClosedError,
    RunnerSnapshot,
    RunNotResumableError,
)
from purplemux_client.web import RunnerHTTPServer, build_parser


@pytest.fixture
def runner() -> Iterator[PythonRunner]:
    instance = PythonRunner(stop_timeout=0.5)
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
    assert "".join(entry.text for entry in result.stdout_entries) == result.stdout
    assert result.stderr_entries == ()
    observed_at = datetime.fromisoformat(result.stdout_entries[0].observed_at)
    assert observed_at.tzinfo is not None
    assert result.exit_code == 0


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


def test_run_uses_and_records_explicit_cwd_and_args(
    runner: PythonRunner, tmp_path: Path
) -> None:
    runner.start(
        "import json, os, sys; print(json.dumps([os.getcwd(), sys.argv[1:]]))",
        cwd=tmp_path,
        args=("--repo", "path with spaces"),
    )

    result = wait_until_finished(runner)

    assert json.loads(result.stdout) == [
        str(tmp_path),
        ["--repo", "path with spaces"],
    ]
    assert result.cwd == str(tmp_path)
    assert result.args == ("--repo", "path with spaces")


def test_explicit_cwd_removes_runner_virtualenv_from_child_environment(
    runner: PythonRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager_venv = tmp_path / "manager-venv"
    target_bin = tmp_path / "target-bin"
    monkeypatch.setenv("VIRTUAL_ENV", str(manager_venv))
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(manager_venv / "bin"), str(target_bin)))
    )

    runner.start(
        "import json, os; print(json.dumps([os.environ.get('VIRTUAL_ENV'), "
        "os.environ.get('PATH')]))",
        cwd=tmp_path,
    )
    result = wait_until_finished(runner)

    assert json.loads(result.stdout) == [None, str(target_bin)]


def test_implicit_cwd_preserves_existing_environment(
    runner: PythonRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager_venv = tmp_path / "manager-venv"
    monkeypatch.setenv("VIRTUAL_ENV", str(manager_venv))

    runner.start("import os; print(os.environ.get('VIRTUAL_ENV'))")
    result = wait_until_finished(runner)

    assert result.stdout == f"{manager_venv}\n"


def test_execution_context_rejects_non_directory(
    runner: PythonRunner, tmp_path: Path
) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(InvalidExecutionContextError, match="not a directory"):
        runner.start("", cwd=missing)


def test_preflight_resolves_relative_paths_from_explicit_cwd(
    runner: PythonRunner, tmp_path: Path
) -> None:
    (tmp_path / "input.txt").write_text("input", encoding="utf-8")

    result = runner.validate(
        "WORKFLOW_PREFLIGHT = {'paths': ['input.txt']}", cwd=tmp_path
    )

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
from purplemux_client import emit_step, save_checkpoint

def wait_for(name):
    while not Path(name).exists():
        time.sleep(0.01)

wait_for("output")
print("visible", flush=True)
wait_for("progress")
emit_step("observed", "completed")
wait_for("checkpoint")
save_checkpoint("safe", {"workspace": "ws-1"})
wait_for("finish")
"""
    run_id = runner.start(code, cwd=tmp_path)
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
    assert runner.snapshot(run_id).progress[-1].name == "observed"

    (tmp_path / "checkpoint").touch()
    changed = runner.wait_for_change(revision, timeout=2)
    assert changed is not None
    revision = changed
    checkpoint = runner.snapshot(run_id).checkpoint
    assert checkpoint is not None
    assert checkpoint.name == "safe"
    assert checkpoint.data == {"workspace": "ws-1"}

    (tmp_path / "finish").touch()
    changed = runner.wait_for_change(revision, timeout=2)
    assert changed is not None
    assert runner.snapshot(run_id).state == "success"


def test_output_is_bounded_and_reports_truncation() -> None:
    runner = PythonRunner(max_output_chars=20)
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
        PythonRunner()


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
    runner = PythonRunner(stop_timeout=0.5)
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


def test_resume_reuses_same_run_from_explicit_checkpoint_without_replaying_side_effect(
    runner: PythonRunner, tmp_path: Path
) -> None:
    code = """\
WORKFLOW_OUTLINE = ["repair", "continue"]

from pathlib import Path
from purplemux_client import resume_checkpoint, save_checkpoint

side_effect = Path("side-effect.txt")
checkpoint = resume_checkpoint()
if checkpoint is None:
    side_effect.write_text("created once")
    save_checkpoint("resource created", {"resource": str(side_effect.resolve())})
else:
    assert checkpoint.name == "resource created"
    assert Path(checkpoint.data["resource"]).read_text() == "manually repaired"
if not Path("repair.complete").exists():
    raise RuntimeError("manual repair required")
print("continued safely")
"""
    run_id = runner.start(code, cwd=tmp_path)
    first = wait_until_finished(runner)
    assert first.state == "failed"
    assert first.checkpoint is not None
    assert first.checkpoint.name == "resource created"
    assert first.outline == ("repair", "continue")
    assert first.attempts[0].state == "failed"
    first_stdout_entries = first.stdout_entries
    first_observed_at = [entry.observed_at for entry in first_stdout_entries]

    (tmp_path / "side-effect.txt").write_text("manually repaired", encoding="utf-8")
    (tmp_path / "repair.complete").touch()
    runner.resume(run_id)
    resumed = wait_until_finished(runner)

    assert resumed.run_id == run_id
    assert resumed.state == "success"
    assert resumed.outline == first.outline
    assert (tmp_path / "side-effect.txt").read_text() == "manually repaired"
    assert "[resume attempt 2 from checkpoint 'resource created']" in resumed.stdout
    assert resumed.stdout.endswith("continued safely\n")
    assert resumed.stdout_entries[: len(first_stdout_entries)] == first_stdout_entries
    assert [
        entry.observed_at
        for entry in resumed.stdout_entries[: len(first_stdout_entries)]
    ] == first_observed_at
    assert len(resumed.stdout_entries) > len(first_stdout_entries)
    assert [(attempt.state, attempt.resumed_from) for attempt in resumed.attempts] == [
        ("failed", None),
        ("success", "resource created"),
    ]


def test_default_cwd_resume_preserves_environment_and_preflight(
    runner: PythonRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager_venv = tmp_path / "manager-venv"
    manager_bin = manager_venv / "bin"
    manager_bin.mkdir(parents=True)
    required_command = manager_bin / "legacy-tool"
    required_command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    required_command.chmod(0o755)
    inherited_path = os.pathsep.join((str(manager_bin), os.environ.get("PATH", "")))
    monkeypatch.setenv("VIRTUAL_ENV", str(manager_venv))
    monkeypatch.setenv("PATH", inherited_path)
    monkeypatch.chdir(tmp_path)
    code = """\
WORKFLOW_PREFLIGHT = {
    "commands": ["legacy-tool"],
    "environment": ["VIRTUAL_ENV"],
}
import json
import os
from purplemux_client import resume_checkpoint, save_checkpoint

checkpoint = resume_checkpoint()
print(json.dumps({"virtualEnv": os.environ.get("VIRTUAL_ENV"), "path": os.environ.get("PATH")}))
if checkpoint is None:
    save_checkpoint("legacy boundary", {"ready": "yes"})
    raise RuntimeError("manual repair required")
assert checkpoint.name == "legacy boundary"
"""

    run_id = runner.start(code)
    first = wait_until_finished(runner)
    assert first.state == "failed"
    assert first.checkpoint is not None

    runner.resume(run_id)
    resumed = wait_until_finished(runner)

    environments = [
        json.loads(line) for line in resumed.stdout.splitlines() if line.startswith("{")
    ]
    assert resumed.state == "success"
    assert environments == [
        {"virtualEnv": str(manager_venv), "path": inherited_path},
        {"virtualEnv": str(manager_venv), "path": inherited_path},
    ]


def test_resume_rejects_failure_without_safe_checkpoint(runner: PythonRunner) -> None:
    run_id = runner.start("raise RuntimeError('unsafe to replay')")
    wait_until_finished(runner)

    with pytest.raises(RunNotResumableError, match="no safe checkpoint"):
        runner.resume(run_id)


def test_suspended_run_is_distinct_and_resumable(runner: PythonRunner) -> None:
    code = """\
from purplemux_client import resume_checkpoint, save_checkpoint, suspend_run
checkpoint = resume_checkpoint()
if checkpoint is None:
    save_checkpoint("agent waiting", {"tab": "tab-1"})
    suspend_run("reply in tab-1 before continuing")
print("continued after input")
"""
    run_id = runner.start(code)
    suspended = wait_until_finished(runner)

    assert suspended.state == "suspended"
    assert suspended.suspension_reason == "reply in tab-1 before continuing"
    runner.resume(run_id)
    resumed = wait_until_finished(runner)
    assert resumed.state == "success"
    assert [attempt.state for attempt in resumed.attempts] == ["suspended", "success"]


def test_repeated_manual_recovery_uses_latest_safe_checkpoint(
    runner: PythonRunner, tmp_path: Path
) -> None:
    code = """\
from pathlib import Path
from purplemux_client import resume_checkpoint, save_checkpoint
checkpoint = resume_checkpoint()
if checkpoint is None:
    save_checkpoint("phase one", {"workspace": "ws-1"})
    raise RuntimeError("first repair")
if checkpoint.name == "phase one":
    assert Path("first-fixed").exists()
    save_checkpoint("phase two", {"workspace": checkpoint.data["workspace"], "tab": "tab-2"})
    raise RuntimeError("second repair")
assert checkpoint.name == "phase two"
assert Path("second-fixed").exists()
print(checkpoint.data["workspace"], checkpoint.data["tab"])
"""
    run_id = runner.start(code, cwd=tmp_path)
    wait_until_finished(runner)
    (tmp_path / "first-fixed").touch()
    runner.resume(run_id)
    second = wait_until_finished(runner)
    assert second.state == "failed"
    assert second.checkpoint is not None
    assert second.checkpoint.name == "phase two"

    (tmp_path / "second-fixed").touch()
    runner.resume(run_id)
    final = wait_until_finished(runner)
    assert final.state == "success"
    assert final.stdout.endswith("ws-1 tab-2\n")
    assert [attempt.resumed_from for attempt in final.attempts] == [
        None,
        "phase one",
        "phase two",
    ]


def test_start_after_close_is_rejected() -> None:
    runner = PythonRunner()
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
        if kwargs.get("start_new_session") is True:
            popen_entered.set()
            assert allow_popen.wait(timeout=3)
        return real_popen(*args, **kwargs)  # type: ignore[call-overload,return-value]

    monkeypatch.setattr(subprocess, "Popen", blocking_popen)
    runner = PythonRunner(stop_timeout=0.5)
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
    runner = PythonRunner(stop_timeout=0.5)
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
    runner = PythonRunner(stop_timeout=0.5)
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
    runner = PythonRunner(stop_timeout=0.2)
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
    runner = PythonRunner(stop_timeout=0.5)
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
        if options.get("start_new_session") is True
    )
    assert isinstance(command, list)
    assert command[0] == sys.executable
    assert options["shell"] is False
    assert options["start_new_session"] is True
    assert options["cwd"] == Path.cwd()


@pytest.fixture
def web_server() -> Iterator[tuple[tuple[str, int], str]]:
    server = RunnerHTTPServer(("127.0.0.1", 0), PythonRunner(stop_timeout=0.5))
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
    runner = PythonRunner(stop_timeout=0.5)
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
    assert [entry["text"] for entry in stdout_entries] == ["HTTP_OK\n"]
    assert datetime.fromisoformat(stdout_entries[0]["observedAt"]).tzinfo is not None
    assert stderr_entries == []
    assert result == {
        "state": "success",
        "stdout": "HTTP_OK\n",
        "stderr": "",
        "outline": [],
        "progress": [],
        "validation": [],
        "exitCode": 0,
        "runId": 1,
        "cwd": str(Path.cwd()),
        "args": [],
        "checkpoint": None,
        "attempts": [
            {
                "number": 1,
                "state": "success",
                "exitCode": 0,
                "resumedFrom": None,
            }
        ],
        "code": 'print("HTTP_OK")',
        "suspensionReason": None,
        "resumable": False,
    }


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
    web_server: tuple[tuple[str, int], str], tmp_path: Path
) -> None:
    address, token = web_server
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()

    status, first = request(
        address,
        "POST",
        "/api/run",
        json.dumps(
            {
                "code": "import time; print('first', flush=True); time.sleep(60)",
                "cwd": str(first_cwd),
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
                "cwd": str(second_cwd),
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
        (first_id, str(first_cwd), ["--first"]),
        (second_id, str(second_cwd), ["--second"]),
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


def test_run_api_resumes_same_run_and_rejects_unsafe_replay(
    web_server: tuple[tuple[str, int], str], tmp_path: Path
) -> None:
    address, token = web_server
    code = """\
from pathlib import Path
from purplemux_client import resume_checkpoint, save_checkpoint
if resume_checkpoint() is None:
    save_checkpoint("before repair", {"workspace": "ws-1", "tab": "tab-1"})
if not Path("fixed").exists():
    raise RuntimeError("fix required")
print("resumed")
"""
    status, started = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": code, "cwd": str(tmp_path)}),
        token=token,
    )
    assert status == 202
    run_id = int(started["runId"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        _, failed = request(address, "GET", f"/api/runs/{run_id}")
        if failed["state"] != "running":
            break
        time.sleep(0.02)
    assert failed["state"] == "failed"
    assert failed["resumable"] is True
    assert failed["checkpoint"] == {
        "name": "before repair",
        "data": {"workspace": "ws-1", "tab": "tab-1"},
    }

    (tmp_path / "fixed").touch()
    status, resumed = request(
        address, "POST", f"/api/runs/{run_id}/resume", token=token
    )
    assert status == 202
    assert resumed["runId"] == run_id

    unsafe_status, unsafe = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": "raise RuntimeError('no checkpoint')"}),
        token=token,
    )
    assert unsafe_status == 202
    unsafe_id = int(unsafe["runId"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        _, unsafe = request(address, "GET", f"/api/runs/{unsafe_id}")
        if unsafe["state"] != "running":
            break
        time.sleep(0.02)
    status, refusal = request(
        address, "POST", f"/api/runs/{unsafe_id}/resume", token=token
    )
    assert status == 409
    assert "no safe checkpoint" in str(refusal["error"])


def test_resume_api_reports_removed_working_directory_without_mutating_run(
    web_server: tuple[tuple[str, int], str], tmp_path: Path
) -> None:
    address, token = web_server
    run_cwd = tmp_path / "removed-after-failure"
    run_cwd.mkdir()
    code = """\
from purplemux_client import save_checkpoint
save_checkpoint("safe boundary", {"workspace": "ws-1"})
raise RuntimeError("manual repair required")
"""
    status, started = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": code, "cwd": str(run_cwd)}),
        token=token,
    )
    assert status == 202
    run_id = int(started["runId"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        _, before = request(address, "GET", f"/api/runs/{run_id}")
        if before["state"] != "running":
            break
        time.sleep(0.02)
    assert before["state"] == "failed"
    run_cwd.rmdir()

    status, refusal = request(
        address, "POST", f"/api/runs/{run_id}/resume", token=token
    )

    assert status == 400
    assert refusal == {"error": f"working directory is not a directory: {run_cwd}"}
    _, after = request(address, "GET", f"/api/runs/{run_id}")
    assert after == before


def test_run_api_passes_run_scoped_execution_context(
    web_server: tuple[tuple[str, int], str], tmp_path: Path
) -> None:
    address, token = web_server
    status, started = request(
        address,
        "POST",
        "/api/run",
        json.dumps(
            {
                "code": "import json, os, sys; print(json.dumps([os.getcwd(), sys.argv[1:]]))",
                "cwd": str(tmp_path),
                "args": ["--repo", "target repo"],
            }
        ),
        token=token,
    )

    assert status == 202
    assert started["cwd"] == str(tmp_path)
    assert started["args"] == ["--repo", "target repo"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        _, result = request(address, "GET", "/api/status")
        if result["state"] != "running":
            break
        time.sleep(0.02)
    assert json.loads(str(result["stdout"])) == [
        str(tmp_path),
        ["--repo", "target repo"],
    ]


def test_run_api_exposes_submitted_code_in_detail_but_not_in_list_summary(
    web_server: tuple[tuple[str, int], str], tmp_path: Path
) -> None:
    """Issue #45: /api/runs/{id} must return each run's own submitted code,
    unaffected by other runs starting, while /api/runs list summaries stay
    lightweight (no full source)."""
    address, token = web_server
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    first_code = "print('RUN=A')"
    second_code = "print('RUN=B')"

    status, first = request(
        address,
        "POST",
        "/api/run",
        json.dumps({"code": first_code, "cwd": str(first_cwd), "args": ["A-ARG"]}),
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
        json.dumps({"code": second_code, "cwd": str(second_cwd), "args": ["B-ARG"]}),
        token=token,
    )
    assert status == 202
    second_id = int(second["runId"])
    assert second["code"] == second_code

    # The first run's own snapshot must be unaffected by the second run
    # starting and executing with a different cwd/args/code.
    status, after = request(address, "GET", f"/api/runs/{first_id}")
    assert status == 200
    assert after["code"] == before["code"] == first_code
    assert after["cwd"] == before["cwd"] == str(first_cwd)
    assert after["args"] == before["args"] == ["A-ARG"]

    status, second_detail = request(address, "GET", f"/api/runs/{second_id}")
    assert status == 200
    assert second_detail["code"] == second_code
    assert second_detail["cwd"] == str(second_cwd)

    # The list summary intentionally omits the full source.
    status, listed = request(address, "GET", "/api/runs")
    assert status == 200
    assert len(listed["runs"]) == 2
    assert all("code" not in run for run in listed["runs"])


@pytest.mark.parametrize(
    ("context", "error"),
    [
        ({"cwd": 42}, "cwd must be a string or null"),
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


def test_run_api_rejects_missing_working_directory(
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
    assert "working directory is not a directory" in str(payload["error"])


def test_runner_page_exposes_execution_context_inputs(
    web_server: tuple[tuple[str, int], str],
) -> None:
    address, _ = web_server
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.request("GET", "/")
    response = connection.getresponse()
    page = response.read().decode()
    connection.close()

    assert response.status == 200
    assert 'id="working-directory"' in page
    assert 'id="run-arguments"' in page
    assert 'id="run-list"' in page


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

    assert '<link id="favicon" rel="icon" href="/favicon.svg"' in documents["/"][1]
    assert documents["/favicon.svg"][0] == "image/svg+xml"
    assert documents["/favicon.svg"][1].startswith("<svg ")


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
    assert '<script src="/log-display.js"></script>' in index
    assert "formatOutputEntries" in log_display
    assert "writeText" in helper
    assert 'execCommand("copy")' in helper
    assert "runnerOutputClipboard.writeText" in script
    assert 'guideCopy.textContent = "Copy manually"' in script
    assert 'outputCopy.textContent = "Copy manually"' in script
    assert 'id="manual-copy-dialog"' in index


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


@pytest.mark.parametrize("path", ["/api/validate", "/api/run"])
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
    runner = PythonRunner(stop_timeout=0.5)
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
        for path in ("/api/run", "/api/validate")
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
    runner = PythonRunner(stop_timeout=0.5)
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
    runner = PythonRunner(stop_timeout=0.5)
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


def test_server_allows_explicitly_requested_hostname() -> None:
    server = RunnerHTTPServer(("localhost", 0))
    try:
        _, port = server.server_address
        assert server.is_allowed_host(f"localhost:{port}")
    finally:
        server.server_close()


def test_web_server_close_stops_running_process() -> None:
    runner = PythonRunner(stop_timeout=0.5)
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
