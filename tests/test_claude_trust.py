from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import purplemux_client.claude_trust as claude_trust
from purplemux_client.claude_trust import ensure_claude_project_trust
from purplemux_client.errors import WorkerFailure


@pytest.fixture(autouse=True)
def _clear_custom_oauth_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", raising=False)


def test_trusts_only_exact_canonical_project_and_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "claude-config"
    config.mkdir()
    state_path = config / ".claude.json"
    state_path.write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "projects": {"/unrelated": {"hasTrustDialogAccepted": False}},
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    assert ensure_claude_project_trust(str(alias)) == str(project)
    assert ensure_claude_project_trust(str(project)) == str(project)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state == {
        "hasCompletedOnboarding": True,
        "projects": {
            "/unrelated": {"hasTrustDialogAccepted": False},
            str(project): {"hasTrustDialogAccepted": True},
        },
    }


def test_default_state_file_is_in_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    ensure_claude_project_trust(str(project))

    state = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert state["projects"] == {str(project): {"hasTrustDialogAccepted": True}}


def test_config_json_takes_precedence_in_default_config_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config = home / ".claude"
    config.mkdir(parents=True)
    current_state = config / ".config.json"
    legacy_state = home / ".claude.json"
    current_state.write_text('{"projects":{},"current":true}\n', encoding="utf-8")
    legacy_state.write_text('{"projects":{},"legacy":true}\n', encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    ensure_claude_project_trust(str(project))

    current = json.loads(current_state.read_text(encoding="utf-8"))
    assert current["projects"] == {str(project): {"hasTrustDialogAccepted": True}}
    assert current["current"] is True
    assert json.loads(legacy_state.read_text(encoding="utf-8")) == {
        "projects": {},
        "legacy": True,
    }


def test_config_json_takes_precedence_in_custom_config_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "claude-config"
    config.mkdir()
    current_state = config / ".config.json"
    legacy_state = config / ".claude.json"
    current_state.write_text('{"projects":{},"current":true}\n', encoding="utf-8")
    legacy_state.write_text('{"projects":{},"legacy":true}\n', encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    ensure_claude_project_trust(str(project))

    current = json.loads(current_state.read_text(encoding="utf-8"))
    assert current["projects"] == {str(project): {"hasTrustDialogAccepted": True}}
    assert current["current"] is True
    assert json.loads(legacy_state.read_text(encoding="utf-8")) == {
        "projects": {},
        "legacy": True,
    }


@pytest.mark.parametrize("custom_config", [False, True])
def test_custom_oauth_state_suffix_is_used_without_config_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_config: bool,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_directory = tmp_path / "claude-config" if custom_config else home
    if custom_config:
        state_directory.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(state_directory))
    else:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    custom_state = state_directory / ".claude-custom-oauth.json"
    legacy_state = state_directory / ".claude.json"
    custom_state.write_text('{"projects":{},"custom":true}\n', encoding="utf-8")
    legacy_state.write_text('{"projects":{},"legacy":true}\n', encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", "https://oauth.example")

    ensure_claude_project_trust(str(project))

    custom = json.loads(custom_state.read_text(encoding="utf-8"))
    assert custom["projects"] == {str(project): {"hasTrustDialogAccepted": True}}
    assert custom["custom"] is True
    assert json.loads(legacy_state.read_text(encoding="utf-8")) == {
        "projects": {},
        "legacy": True,
    }


def test_relative_config_directory_fails_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative-config")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(WorkerFailure, match="must be an absolute path"):
        ensure_claude_project_trust(str(project))

    assert not (tmp_path / "relative-config").exists()


def test_home_directory_fails_before_changing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "claude-config"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    with pytest.raises(WorkerFailure, match="does not persist.*home directory"):
        ensure_claude_project_trust(str(home))

    assert not config.exists()


def test_invalid_or_unsafe_state_fails_without_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "claude-config"
    config.mkdir()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    state_path = config / ".claude.json"
    state_path.symlink_to(target)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    with pytest.raises(WorkerFailure, match="not a safe user file"):
        ensure_claude_project_trust(str(tmp_path))

    assert state_path.is_symlink()
    assert target.read_text(encoding="utf-8") == "{}"


def test_concurrent_processes_preserve_both_project_entries(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "claude-config"
    projects = [tmp_path / "project-one", tmp_path / "project-two"]
    for project in projects:
        project.mkdir()
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["CLAUDE_CONFIG_DIR"] = str(config)
    program = (
        "from purplemux_client.claude_trust import ensure_claude_project_trust; "
        "import sys; ensure_claude_project_trust(sys.argv[1])"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", program, str(project)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for project in projects
    ]
    results = [process.communicate(timeout=10) for process in processes]

    assert [
        (process.returncode, stderr) for process, (_, stderr) in zip(processes, results)
    ] == [(0, ""), (0, "")]
    state = json.loads((config / ".claude.json").read_text(encoding="utf-8"))
    assert state["projects"] == {
        str(project): {"hasTrustDialogAccepted": True} for project in projects
    }


def test_waits_for_claude_state_lock_and_preserves_external_update(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "claude-config"
    config.mkdir()
    state_path = config / ".config.json"
    state_path.write_text('{"projects":{}}\n', encoding="utf-8")
    lock_path = config / ".config.json.lock"
    lock_path.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["CLAUDE_CONFIG_DIR"] = str(config)
    program = (
        "from purplemux_client.claude_trust import ensure_claude_project_trust; "
        "import sys; ensure_claude_project_trust(sys.argv[1])"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(project)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.1)
        assert process.poll() is None
        state_path.write_text(
            '{"projects":{},"externalClaudeUpdate":true}\n', encoding="utf-8"
        )
    finally:
        lock_path.rmdir()
    stdout, stderr = process.communicate(timeout=10)

    assert (process.returncode, stdout, stderr) == (0, "", "")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["externalClaudeUpdate"] is True
    assert state["projects"] == {str(project): {"hasTrustDialogAccepted": True}}


def test_reclaims_abandoned_claude_state_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "claude-config"
    config.mkdir()
    state_path = config / ".config.json"
    state_path.write_text('{"projects":{},"preserved":true}\n', encoding="utf-8")
    lock_path = config / ".config.json.lock"
    lock_path.mkdir()
    abandoned = time.time() - claude_trust._LOCK_STALE_SECONDS - 1
    os.utime(lock_path, (abandoned, abandoned))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    ensure_claude_project_trust(str(project))

    assert not lock_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["preserved"] is True
    assert state["projects"] == {str(project): {"hasTrustDialogAccepted": True}}


def test_refreshes_claude_state_lock_during_slow_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "claude-config"
    config.mkdir()
    state_path = config / ".config.json"
    state_path.write_text('{"projects":{}}\n', encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setattr(claude_trust, "_LOCK_UPDATE_SECONDS", 0.02)
    original_write = claude_trust._write_state

    def slow_write(path: Path, state: object, mode: int) -> None:
        lock_path = Path(f"{path}.lock")
        created_mtime = lock_path.stat().st_mtime_ns
        time.sleep(0.08)
        assert lock_path.stat().st_mtime_ns > created_mtime
        original_write(path, state, mode)  # type: ignore[arg-type]

    monkeypatch.setattr(claude_trust, "_write_state", slow_write)

    ensure_claude_project_trust(str(project))

    assert not Path(f"{state_path}.lock").exists()
