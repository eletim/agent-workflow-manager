from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from purplemux_client import (
    PurpleMuxCLIClient,
    prepare_run_repository,
)

RUN_LIVE = os.environ.get("AGENT_WORKFLOW_MANAGER_RUN_LIVE_CODEX_LINKED_COMMIT") == "1"


def _git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="set AGENT_WORKFLOW_MANAGER_RUN_LIVE_CODEX_LINKED_COMMIT=1",
)
def test_real_codex_commits_cleanly_in_linked_run_worktree(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    _git("init", "--bare", str(remote))
    _git("init", "-b", "main", str(source))
    _git("config", "user.name", "AWM Live Test", cwd=source)
    _git("config", "user.email", "awm-live@example.com", cwd=source)
    (source / "proof.txt").write_text("base\n", encoding="utf-8")
    _git("add", "proof.txt", cwd=source)
    _git("commit", "-m", "base", cwd=source)
    _git("remote", "add", "origin", str(remote), cwd=source)
    _git("push", "origin", "main", cwd=source)

    context = prepare_run_repository(
        repo=source,
        base_branch="main",
        worktree_root=tmp_path / "worktrees",
    )
    worktree = context.execution_root
    branch = "feature/live-codex-linked-commit"
    _git("switch", "-c", branch, context.base_sha, cwd=worktree)

    try:
        result = subprocess.run(
            PurpleMuxCLIClient._publication_disabled_agent_command(
                "codex",
                "Append exactly one line containing 'codex' to proof.txt, then run "
                "git add proof.txt and git commit -m 'Verify Codex linked commit'. "
                "Do not change any other file, do not push, and finish with a clean "
                "worktree.",
            ),
            cwd=worktree,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert result.returncode == 0, result.stderr

        head = _git("rev-parse", "HEAD", cwd=worktree)
        assert head != context.base_sha
        assert (
            _git("merge-base", "--is-ancestor", context.base_sha, head, cwd=worktree)
            == ""
        )
        assert _git("branch", "--show-current", cwd=worktree) == branch
        assert _git("log", "-1", "--format=%s", cwd=worktree) == (
            "Verify Codex linked commit"
        )
        assert (worktree / "proof.txt").read_text(encoding="utf-8") == "base\ncodex\n"
        assert _git("status", "--porcelain", cwd=worktree) == ""
        assert _git("ls-remote", "--heads", "origin", branch, cwd=worktree) == ""
    finally:
        _git("worktree", "remove", str(worktree), cwd=source)
