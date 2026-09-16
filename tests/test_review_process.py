from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from purplemux_client.review_process import ReviewProcessScope


def test_review_scope_resolves_only_the_declared_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import purplemux_client.review_process as process_module

    commands: list[list[str]] = []

    def run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(args)
        if args[1:3] == ["tab", "list"]:
            return SimpleNamespace(
                stdout='{"tabs":[{"tabId":"other","sessionName":"pt-other"},'
                '{"tabId":"agent","sessionName":"pt-review-agent"}]}',
            )
        return SimpleNamespace(stdout="12345\n")

    monkeypatch.setattr(process_module.subprocess, "run", run)
    assert ReviewProcessScope._pane_pid("/bin/purplemux", "ws-review", "agent") == 12345
    assert commands[-1] == [
        "tmux",
        "-L",
        "purple",
        "display-message",
        "-p",
        "-t",
        "pt-review-agent",
        "#{pane_pid}",
    ]


def test_review_scope_kills_detached_child_before_returning(tmp_path: Path) -> None:
    try:
        scope = ReviewProcessScope()
    except (OSError, RuntimeError) as exc:
        pytest.skip(f"cgroup v2 delegation unavailable: {exc}")

    marker = tmp_path / "late-write"
    earlier_marker = tmp_path / "existing-child-write"
    child_code = (
        "import pathlib,sys,time; time.sleep(0.8); "
        "pathlib.Path(sys.argv[1]).write_text('late')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        "first=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[3]], "
        "start_new_session=True, stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(first.pid,flush=True); sys.stdin.readline(); "
        "second=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]], "
        "start_new_session=True, stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(second.pid,flush=True); time.sleep(10)"
    )
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            parent_code,
            child_code,
            str(marker),
            str(earlier_marker),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pids: list[int] = []
    try:
        assert parent.stdin is not None and parent.stdout is not None
        child_pids.append(int(parent.stdout.readline().strip()))
        scope._attach_root(parent.pid)
        parent.stdin.write("\n")
        parent.stdin.flush()
        child_pids.append(int(parent.stdout.readline().strip()))
        scope.stop()
        parent.wait(timeout=5)
        time.sleep(0.9)
        assert not marker.exists()
        assert not earlier_marker.exists()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        for child_pid in child_pids:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
        if scope.path.exists():
            scope.stop()
