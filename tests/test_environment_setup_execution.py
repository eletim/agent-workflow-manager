from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from purplemux_client.environment_setup_execution import (
    execute_environment_setup_commands,
)


def test_runs_supplied_commands_in_order_and_observes_usability(tmp_path: Path) -> None:
    checks, verification = execute_environment_setup_commands(
        build="printf 'build\\n' >> order; touch built",
        start="printf 'start\\n' >> order; touch started",
        ready_check="printf 'ready\\n' >> order; test -f built && test -f started",
        verification_command=None,
        cwd=str(tmp_path),
        remaining=lambda: 5,
    )
    assert (tmp_path / "order").read_text().splitlines() == ["build", "start", "ready"]
    assert list(checks) == ["build", "start", "ready_check"]
    assert all(check["exit_code"] == 0 for check in checks.values())
    assert verification == checks["ready_check"]


def test_omitted_commands_still_require_observed_usability(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="needs a usability check"):
        execute_environment_setup_commands(
            build=None,
            start=None,
            ready_check=None,
            verification_command=None,
            cwd=str(tmp_path),
            remaining=lambda: 5,
        )
    checks, verification = execute_environment_setup_commands(
        build=None,
        start=None,
        ready_check=None,
        verification_command="printf usable; test -d .",
        cwd=str(tmp_path),
        remaining=lambda: 5,
    )
    assert checks == {}
    assert verification == {
        "command": "printf usable; test -d .",
        "exit_code": 0,
        "output": "usable",
    }


def test_failed_supplied_command_blocks_later_commands(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="build failed"):
        execute_environment_setup_commands(
            build="exit 3",
            start="touch started",
            ready_check="touch checked",
            verification_command=None,
            cwd=str(tmp_path),
            remaining=lambda: 5,
        )
    assert not (tmp_path / "started").exists()
    assert not (tmp_path / "checked").exists()


def test_start_can_keep_running_while_ready_check_succeeds(tmp_path: Path) -> None:
    checks, verification = execute_environment_setup_commands(
        build=None,
        start="touch started; sleep 5",
        ready_check="test -f started",
        verification_command=None,
        cwd=str(tmp_path),
        remaining=lambda: 5,
    )
    try:
        assert checks["start"]["running"] is True
        assert verification["exit_code"] == 0
    finally:
        os.killpg(int(checks["start"]["pid"]), signal.SIGKILL)


def test_command_timeout_is_bounded(tmp_path: Path) -> None:
    deadline = time.monotonic() + 0.1
    with pytest.raises(TimeoutError):
        execute_environment_setup_commands(
            build="sleep 5",
            start=None,
            ready_check="true",
            verification_command=None,
            cwd=str(tmp_path),
            remaining=lambda: max(0.01, deadline - time.monotonic()),
        )
    assert time.monotonic() - deadline < 1
