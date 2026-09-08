from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from purplemux_client.claude_trust import ensure_claude_project_trust
from purplemux_client.errors import WorkerFailure


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
