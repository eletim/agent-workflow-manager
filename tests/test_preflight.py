from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from purplemux_client.preflight import (
    MAX_OUTLINE_ITEMS,
    MAX_OUTLINE_LABEL_CHARS,
    ValidationResult,
    WorkflowValidator,
)
from purplemux_client.runner import (
    PythonRunner,
    RunnerClosedError,
    WorkflowValidationError,
)


def validator(tmp_path: Path, **environment: str) -> WorkflowValidator:
    return WorkflowValidator(
        environment=environment,
        cwd=tmp_path,
        module_search_path=[],
    )


def stalling_worker_command(pid_log: Path) -> list[str]:
    code = (
        "import os, time\n"
        f"with open({str(pid_log)!r}, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(str(os.getpid()) + '\\n')\n"
        "    stream.flush()\n"
        "time.sleep(60)\n"
    )
    return [sys.executable, "-c", code]


def stalled_remote_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "remote", "add", "origin", "stall::remote"],
        check=True,
    )
    helper_directory = tmp_path / "bin"
    helper_directory.mkdir()
    helper = helper_directory / "git-remote-stall"
    helper.write_text(
        "#!/bin/sh\n"
        "sleep 60 &\n"
        "child=$!\n"
        f'printf \'%s\\n%s\\n\' "$$" "$child" > {str(tmp_path / "remote-pids")!r}\n'
        'wait "$child"\n',
        encoding="utf-8",
    )
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{helper_directory}:{os.environ['PATH']}")
    return repository


def wait_for_pids(pid_log: Path, count: int) -> list[int]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if pid_log.exists():
            pids = [
                int(line) for line in pid_log.read_text(encoding="utf-8").splitlines()
            ]
            if len(pids) >= count:
                return pids
        time.sleep(0.01)
    raise AssertionError(f"worker PID log did not reach {count} entries")


def process_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for_processes_to_exit(pids: list[int]) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if all(not process_is_live(pid) for pid in pids):
            return
        time.sleep(0.01)
    live = [pid for pid in pids if process_is_live(pid)]
    raise AssertionError(f"processes did not exit: {live}")


def test_valid_script_passes_preflight(tmp_path: Path) -> None:
    required = tmp_path / "input.json"
    required.write_text("{}", encoding="utf-8")
    result = validator(tmp_path, PATH="/usr/bin", API_TOKEN="set").validate(
        "import os\n"
        "WORKFLOW_PREFLIGHT = {\n"
        "    'commands': ['sh'],\n"
        "    'environment': ['API_TOKEN'],\n"
        "    'paths': ['input.json'],\n"
        "}\n"
        "print(os.environ['API_TOKEN'])\n"
    )

    assert result.valid
    assert result.issues == ()


def test_static_workflow_outline_is_returned_without_execution(tmp_path: Path) -> None:
    marker = tmp_path / "outline-side-effect"
    result = validator(tmp_path).validate(
        "WORKFLOW_OUTLINE = ['prepare', 'implement', 'review']\n"
        f"open({str(marker)!r}, 'w').write('executed')\n"
    )

    assert result.valid
    assert result.outline == ("prepare", "implement", "review")
    assert not marker.exists()


def test_workflow_outline_is_optional(tmp_path: Path) -> None:
    result = validator(tmp_path).validate("print('no outline')")

    assert result.valid
    assert result.outline == ()


@pytest.mark.parametrize(
    "declaration",
    [
        "WORKFLOW_OUTLINE = make_outline()",
        "WORKFLOW_OUTLINE = ('prepare', 'review')",
        "WORKFLOW_OUTLINE = ['prepare', 2]",
        "WORKFLOW_OUTLINE = ['   ']",
        "WORKFLOW_OUTLINE = ['line\\nbreak']",
        f"WORKFLOW_OUTLINE = [{('x' * (MAX_OUTLINE_LABEL_CHARS + 1))!r}]",
        f"WORKFLOW_OUTLINE = {['step'] * (MAX_OUTLINE_ITEMS + 1)!r}",
    ],
)
def test_malformed_workflow_outline_is_a_validation_error(
    tmp_path: Path, declaration: str
) -> None:
    result = validator(tmp_path).validate(declaration)

    assert not result.valid
    assert result.outline == ()
    assert result.issues[0].kind == "outline"
    assert "WORKFLOW_OUTLINE" in result.issues[0].message


def test_syntax_error_has_source_location(tmp_path: Path) -> None:
    result = validator(tmp_path).validate("def broken(\n")

    assert not result.valid
    assert result.issues[0].kind == "syntax"
    assert result.issues[0].line == 1
    assert result.issues[0].column is not None


def test_missing_import_and_configuration_are_actionable(tmp_path: Path) -> None:
    result = validator(tmp_path).validate(
        "import dependency_that_does_not_exist\n"
        "import os\n"
        "token = os.environ['REQUIRED_TOKEN']\n"
    )

    assert [(issue.kind, issue.line) for issue in result.issues] == [
        ("import", 1),
        ("environment", 3),
    ]
    assert "dependency_that_does_not_exist" in result.issues[0].message
    assert "REQUIRED_TOKEN" in result.issues[1].message


@pytest.mark.parametrize(
    "code",
    [
        "import json.nonexistent",
        "WORKFLOW_PREFLIGHT = {'imports': ['json.nonexistent']}",
    ],
)
def test_nested_missing_module_is_rejected(tmp_path: Path, code: str) -> None:
    result = WorkflowValidator(cwd=tmp_path).validate(code)

    assert not result.valid
    assert result.issues[0].kind == "import"
    assert "json.nonexistent" in result.issues[0].message


def test_nested_existing_module_is_accepted(tmp_path: Path) -> None:
    result = WorkflowValidator(cwd=tmp_path).validate("import json.decoder")

    assert result.valid


def test_manager_cwd_only_module_is_not_visible_to_workflow_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "manager_only.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(tmp_path)

    result = WorkflowValidator(cwd=tmp_path).validate("import manager_only")

    assert not result.valid
    assert result.issues[0].kind == "import"
    assert "manager_only" in result.issues[0].message


def test_guarded_import_and_environment_access_are_not_mandatory(
    tmp_path: Path,
) -> None:
    result = validator(tmp_path).validate(
        "import os\n"
        "try:\n"
        "    import optional_dependency_that_is_missing\n"
        "except ImportError:\n"
        "    optional_dependency_that_is_missing = None\n"
        "if 'OPTIONAL_TOKEN' in os.environ:\n"
        "    token = os.environ['OPTIONAL_TOKEN']\n"
    )

    assert result.valid


def test_declared_requirements_report_missing_command_import_and_path(
    tmp_path: Path,
) -> None:
    result = validator(tmp_path, PATH="").validate(
        "WORKFLOW_PREFLIGHT = {\n"
        "  'commands': ['missing-command'],\n"
        "  'imports': ['missing_package.module'],\n"
        "  'paths': ['missing.txt'],\n"
        "}\n"
    )

    assert {issue.kind for issue in result.issues} == {"command", "import", "path"}


@pytest.mark.parametrize(
    "declaration",
    [
        "WORKFLOW_PREFLIGHT = make_requirements()",
        "WORKFLOW_PREFLIGHT = []",
        "WORKFLOW_PREFLIGHT = {'unknown': []}",
        "WORKFLOW_PREFLIGHT = {1: []}",
        "WORKFLOW_PREFLIGHT = {'commands': ['git', 1]}",
    ],
)
def test_malformed_metadata_is_rejected(tmp_path: Path, declaration: str) -> None:
    result = validator(tmp_path).validate(declaration)

    assert not result.valid
    assert result.issues[0].kind == "metadata"
    assert "WORKFLOW_PREFLIGHT" in result.issues[0].message


def test_unknown_public_api_is_rejected(tmp_path: Path) -> None:
    result = WorkflowValidator(cwd=tmp_path).validate(
        "from purplemux_client import WorkflowGraph\n"
    )

    assert not result.valid
    assert result.issues[0].kind == "api"
    assert "WorkflowGraph" in result.issues[0].message


def test_validation_never_executes_top_level_side_effects(tmp_path: Path) -> None:
    marker = tmp_path / "side-effect"
    code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('created')\n"

    result = WorkflowValidator(cwd=tmp_path).validate(code)

    assert result.valid
    assert not marker.exists()


def test_runner_does_not_start_process_when_validation_fails(tmp_path: Path) -> None:
    runner = PythonRunner(validator=validator(tmp_path))
    try:
        with pytest.raises(WorkflowValidationError):
            runner.start("import dependency_that_does_not_exist")

        snapshot = runner.snapshot()
        assert snapshot.state == "validation_failed"
        assert snapshot.run_id is None
        assert snapshot.exit_code is None
        assert snapshot.validation[0].kind == "import"

        assert runner.validate("print('fixed')").valid
        assert runner.snapshot().state == "idle"
    finally:
        runner.close()


def test_repeated_timeouts_reap_validation_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_log = tmp_path / "worker-pids"
    validator = WorkflowValidator(cwd=tmp_path, check_timeout=0.05)
    monkeypatch.setattr(
        validator, "_worker_command", lambda: stalling_worker_command(pid_log)
    )

    results = [validator.validate("import json") for _ in range(3)]
    pids = wait_for_pids(pid_log, 3)
    validator.close()

    assert all(not result.valid for result in results)
    assert all(result.issues[0].kind == "timeout" for result in results)
    assert all("exceeded 0.05s" in result.issues[0].message for result in results)
    assert all(not process_is_live(pid) for pid in pids)
    assert not any(
        thread.name == "workflow-preflight-checks" for thread in threading.enumerate()
    )


def test_validation_timeout_kills_stalled_remote_helper_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = stalled_remote_repository(tmp_path, monkeypatch)
    pid_log = tmp_path / "remote-pids"
    validator = WorkflowValidator(cwd=tmp_path, check_timeout=0.2)
    code = (
        "from purplemux_client import prepare_run_repository\n"
        f"prepare_run_repository(repo={str(repository)!r}, base_branch='main')\n"
    )

    result = validator.validate(code)
    pids = wait_for_pids(pid_log, 2)
    validator.close()

    assert not result.valid
    assert result.issues[0].kind == "timeout"
    wait_for_processes_to_exit(pids)


def test_validator_close_kills_stalled_remote_helper_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = stalled_remote_repository(tmp_path, monkeypatch)
    pid_log = tmp_path / "remote-pids"
    validator = WorkflowValidator(cwd=tmp_path, check_timeout=10)
    code = (
        "from purplemux_client import prepare_run_repository\n"
        f"prepare_run_repository(repo={str(repository)!r}, base_branch='main')\n"
    )
    results: list[ValidationResult] = []
    validation_thread = threading.Thread(
        target=lambda: results.append(validator.validate(code))
    )
    validation_thread.start()
    pids = wait_for_pids(pid_log, 2)

    validator.close()
    validation_thread.join(timeout=1)

    assert not validation_thread.is_alive()
    assert len(results) == 1
    assert not results[0].valid
    wait_for_processes_to_exit(pids)


def test_stalled_checks_do_not_block_runner_observation_or_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_log = tmp_path / "worker-pid"
    validator = WorkflowValidator(cwd=tmp_path, check_timeout=10)
    monkeypatch.setattr(
        validator, "_worker_command", lambda: stalling_worker_command(pid_log)
    )
    runner = PythonRunner(validator=validator)
    errors: list[BaseException] = []

    def start() -> None:
        try:
            runner.start("import json")
        except BaseException as exc:
            errors.append(exc)

    start_thread = threading.Thread(target=start)
    start_thread.start()
    worker_pid = wait_for_pids(pid_log, 1)[0]

    assert runner.snapshot().state == "idle"
    assert runner.stop() is False
    runner.close()
    start_thread.join(timeout=1)

    assert not start_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RunnerClosedError)
    assert not process_is_live(worker_pid)
    assert validator._workers == set()


def test_close_prevents_helper_registration_that_has_not_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_log = tmp_path / "worker-pid"
    validation_entered = threading.Event()
    allow_registration = threading.Event()
    validator = WorkflowValidator(cwd=tmp_path, check_timeout=10)
    original_run_worker = validator._run_worker

    def delayed_run_worker(
        code: str, *, environment: Mapping[str, str] | None, cwd: Path | None
    ) -> ValidationResult:
        validation_entered.set()
        assert allow_registration.wait(timeout=2)
        return original_run_worker(code, environment=environment, cwd=cwd)

    monkeypatch.setattr(validator, "_run_worker", delayed_run_worker)
    monkeypatch.setattr(
        validator, "_worker_command", lambda: stalling_worker_command(pid_log)
    )
    runner = PythonRunner(validator=validator)
    errors: list[BaseException] = []

    def start() -> None:
        try:
            runner.start("import json")
        except BaseException as exc:
            errors.append(exc)

    start_thread = threading.Thread(target=start)
    start_thread.start()
    assert validation_entered.wait(timeout=1)

    runner.close()
    allow_registration.set()
    start_thread.join(timeout=1)

    assert not start_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RunnerClosedError)
    assert not pid_log.exists()
    assert validator._workers == set()
