from __future__ import annotations

import json
import os
import pty
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

import purplemux_client.client as client_module
from purplemux_client import (
    CreateSessionRequest,
    MutationOutcomeUnknown,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    ResultNotReady,
    SessionReadyTimeout,
    ShellCommandRequest,
    TerminalSessionError,
    WorkerFailure,
    WorkerInterrupted,
    WorkerNeedsInput,
)
from purplemux_client.correlation import RUN_IDENTITY_ENV


def completed(data: object, *, returncode: int = 0, stderr: str = ""):
    stdout = data if isinstance(data, str) else json.dumps(data)
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeRunner:
    def __init__(
        self,
        outcomes: Sequence[
            subprocess.CompletedProcess[str] | subprocess.TimeoutExpired | OSError
        ],
        *,
        workspace_directories: Sequence[str] | None = ("/workspace/project",),
    ) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.tabs: dict[str, dict[str, object]] = {}
        self.workspace_directories = workspace_directories

    def __call__(
        self,
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output is True
        assert text is True
        assert check is False
        command = list(args)
        self.calls.append(command)
        self.timeouts.append(timeout)
        if command[1:3] == ["tab", "list"]:
            return completed({"tabs": list(self.tabs.values())})
        if command[1:] == ["workspaces"]:
            if self.workspace_directories is None:
                return completed({"workspaces": []})
            return completed(
                {
                    "workspaces": [
                        {
                            "id": "ws-test",
                            "name": "Test workspace",
                            "directories": list(self.workspace_directories),
                        }
                    ]
                }
            )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if command[1:3] == ["tab", "create"] and outcome.returncode == 0:
            try:
                data = json.loads(outcome.stdout)
            except json.JSONDecodeError:
                data = None
            tab_id = data.get("tabId") if isinstance(data, dict) else None
            if isinstance(tab_id, str):
                panel_type = command[command.index("-t") + 1]
                name = command[command.index("-n") + 1]
                self.tabs[tab_id] = {
                    "tabId": tab_id,
                    "workspaceId": "ws-test",
                    "name": name,
                    "panelType": panel_type,
                    "agentProviderId": {
                        "codex-cli": "codex",
                        "claude-code": "claude",
                    }.get(panel_type),
                }
        if command[1:3] == ["tab", "close"] and outcome.returncode == 0:
            self.tabs.pop(command[-1], None)
        return outcome


def client(runner: FakeRunner, **kwargs: object) -> PurpleMuxCLIClient:
    kwargs.setdefault("codex_project_truster", lambda path: path)
    kwargs.setdefault("claude_project_truster", lambda path: path)
    return PurpleMuxCLIClient(
        "ws-test",
        poll_interval_seconds=0,
        runner=runner,
        sleep=lambda _: None,
        **kwargs,
    )


def request(worker: str = "codex", command: str | None = None) -> CreateSessionRequest:
    return CreateSessionRequest(
        worker=worker,
        cwd="/workspace/project",
        command=command or worker,
    )


def baseline(
    *,
    state: str = "idle",
    event_seq: int = 1,
    result_status: str = "not-ready",
    text: str | None = None,
    completion_timestamp: int | None = None,
) -> list[subprocess.CompletedProcess[str]]:
    return [
        completed({"cliState": state, "alive": True, "eventSeq": event_seq}),
        completed(
            {
                "status": result_status,
                "text": text,
                "completionTimestamp": completion_timestamp,
            }
        ),
    ]


def initialize_test_repository(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "base"],
        check=True,
    )


@pytest.fixture
def linked_delivery_repository(
    tmp_path: Path,
) -> tuple[Path, Path, Path, str]:
    repository = tmp_path / "repository parent"
    checkout = tmp_path / "linked checkout"
    remote = tmp_path / "test remote.git"
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "test@example.com",
        ],
        check=True,
    )
    (repository / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    base = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-b",
            "feature/delivery",
            str(checkout),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "test-remote", str(remote)],
        check=True,
    )
    return repository, checkout, remote, base


def test_create_response_parsing_and_codex_panel_type() -> None:
    runner = FakeRunner([completed({"tabId": "tab-123"})])

    assert client(runner).create_session(request()) == "tab-123"
    create = next(call for call in runner.calls if call[1:3] == ["tab", "create"])
    assert create[-2:] == ["-t", "codex-cli"]
    assert create[create.index("-n") + 1].startswith("awm-codex-cli-")


@pytest.mark.parametrize("restriction", ["local-git-only", "publication-disabled"])
def test_restricted_session_uses_common_turn_interface(
    monkeypatch: pytest.MonkeyPatch,
    restriction: str,
) -> None:
    runner = FakeRunner([completed({"tabId": "tab-restricted"})])
    cli = client(runner)
    session = cli.create_session(
        CreateSessionRequest(
            worker="codex",
            cwd="/workspace/project",
            command="codex",
            restriction=restriction,  # type: ignore[arg-type]
        )
    )
    started: list[ShellCommandRequest] = []
    waited: list[tuple[str, float]] = []
    monkeypatch.setattr(
        cli,
        "_status",
        lambda tab: {"panelType": "terminal", "alive": True},
    )
    monkeypatch.setattr(
        cli,
        "_start_shell_run",
        lambda tab, request, cwd: started.append(request),
    )
    monkeypatch.setattr(
        cli,
        "wait_for_shell_completion",
        lambda tab, timeout: waited.append((tab, timeout)),
    )
    monkeypatch.setattr(
        cli,
        "read_shell_result",
        lambda tab: client_module.ShellResult(0, stdout="validated output\n"),
    )

    cli.wait_until_ready(session, 10)
    cli.send_input(session, "inspect and repair")
    cli.wait_for_turn_completion(session, 20)

    create = next(call for call in runner.calls if call[1:3] == ["tab", "create"])
    assert create[-1] == "terminal"
    assert len(started) == 1
    assert "inspect and repair" not in started[0].command
    assert waited == [(session, 20)]
    assert cli.read_result(session) == "validated output"


@pytest.mark.parametrize(
    ("worker", "required"),
    [
        (
            "codex",
            (
                "--sandbox workspace-write",
                "network_access=false",
                "exclude_tmpdir_env_var=true",
                "GIT_CONFIG_GLOBAL=/dev/null",
                "GIT_TERMINAL_PROMPT=0",
                "--search",
            ),
        ),
        (
            "claude",
            (
                "--restricted",
                "--permission-prompts none",
                "Bash(gh pr edit *)",
                "Bash(gh issue edit *)",
            ),
        ),
    ],
)
def test_restricted_session_preserves_safe_remote_capabilities(
    worker: str, required: tuple[str, ...]
) -> None:
    command = PurpleMuxCLIClient._restricted_agent_command(worker, "inspect safely")

    assert all(value in command for value in required)
    assert "GIT_SSH_COMMAND=false" in command
    if worker == "codex":
        assert "-u GH_TOKEN" in command
        assert "-u GITHUB_TOKEN" in command
        assert "GH_CONFIG_DIR=/dev/null" in command
        assert command.index("--ask-for-approval never") < command.index(" exec ")
    else:
        assert "-u GH_TOKEN" not in command
        assert "-u GITHUB_TOKEN" not in command


def test_restricted_claude_allows_only_bounded_local_git_mutations() -> None:
    command = PurpleMuxCLIClient._restricted_agent_command("claude", "repair locally")
    arguments = shlex.split(command)
    allowed_tools = arguments[arguments.index("--allowed-tools") + 1].split(",")

    assert "Bash(git add *)" in allowed_tools
    assert "Bash(git commit -m *)" in allowed_tools
    assert "Bash(git merge --ff-only *)" in allowed_tools
    assert not any("git push" in tool for tool in allowed_tools)
    assert not any("git reset" in tool for tool in allowed_tools)
    assert not any("git rebase" in tool for tool in allowed_tools)


def test_restricted_claude_does_not_allow_pr_close_delete_branch() -> None:
    command = PurpleMuxCLIClient._restricted_agent_command(
        "claude", "Run gh pr close 123 --delete-branch"
    )
    arguments = shlex.split(command)
    allowed_tools = arguments[arguments.index("--allowed-tools") + 1].split(",")

    assert "Bash(gh pr close *)" not in allowed_tools
    assert not any(tool.startswith("Bash(gh pr close") for tool in allowed_tools)
    assert "Bash(gh pr edit *)" in allowed_tools


@pytest.mark.parametrize(
    "attempt",
    [
        "gh api --method PATCH repos/acme/project/git/refs/heads/main",
        "git push --force https://x-access-token:${GH_TOKEN}@github.com/acme/project.git",
    ],
)
@pytest.mark.parametrize("worker", ["codex", "claude"])
def test_publication_disabled_agent_denies_authenticated_mutation_capabilities(
    attempt: str, worker: str, tmp_path: Path
) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "base"],
        check=True,
        capture_output=True,
    )
    fake_bin = tmp_path.parent / f"{tmp_path.name}-bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / worker
    fake_worker.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"${GH_TOKEN-unset}|${GITHUB_TOKEN-unset}|\""
        "\"${GH_CONFIG_DIR-unset}|$*\"\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    command = PurpleMuxCLIClient._publication_disabled_agent_command(worker, attempt)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "GH_TOKEN": "push-capable-gh-token",
            "GITHUB_TOKEN": "push-capable-github-token",
        }
    )

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert attempt not in command
    assert "-u GH_TOKEN" in command
    assert "-u GITHUB_TOKEN" in command
    assert 'GH_CONFIG_DIR="$awm_delivery_hooks/gh"' in command
    token, github_token, config_dir, arguments = result.stdout.strip().split("|", 3)
    assert token == github_token == "unset"
    assert config_dir.endswith("/gh")
    if worker == "codex":
        assert "sandbox_workspace_write.network_access=false" in arguments


@pytest.mark.parametrize("worker", ["codex", "claude"])
def test_publication_disabled_agent_retains_development_tools(worker: str) -> None:
    command = PurpleMuxCLIClient._publication_disabled_agent_command(
        worker, "run the project tests, lint, formatter, and build"
    )

    assert "GIT_SSH_COMMAND=false" in command
    assert "pre-push" in command
    if worker == "codex":
        assert "--sandbox workspace-write" in command
        assert "--ask-for-approval never" in command
    else:
        arguments = shlex.split(command)
        allowed_tools = arguments[arguments.index("--allowed-tools") + 1].split(",")
        assert "Bash" in allowed_tools
        assert not any(tool.startswith("Bash(") for tool in allowed_tools)


@pytest.mark.parametrize("worker", ["codex", "claude"])
def test_publication_disabled_launch_contract_delivers_linked_worktree_commit(
    worker: str,
    tmp_path: Path,
    linked_delivery_repository: tuple[Path, Path, Path, str],
) -> None:
    _, checkout, remote, base = linked_delivery_repository
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / worker
    fake_worker.write_text(
        "#!/bin/sh\n"
        "shadow=\n"
        "previous=\n"
        "for argument do\n"
        '    if [ "$previous" = --add-dir ]; then shadow=$argument; break; fi\n'
        "    previous=$argument\n"
        "done\n"
        "printf 'after\\n' > tracked.txt\n"
        "git add tracked.txt\n"
        "git commit -m linked-delivery >/dev/null 2>&1\n"
        "commit_status=$?\n"
        'git push "$AWM_TEST_REMOTE" HEAD:refs/heads/forbidden >/dev/null 2>&1\n'
        "push_status=$?\n"
        'git push --no-verify "$AWM_TEST_REMOTE" '
        "HEAD:refs/heads/forbidden-no-verify >/dev/null 2>&1\n"
        "no_verify_status=$?\n"
        'git -c core.hooksPath=/dev/null push "$AWM_TEST_REMOTE" '
        "HEAD:refs/heads/forbidden-hook-override >/dev/null 2>&1\n"
        "hook_override_status=$?\n"
        'git -c alias.ship=push ship --no-verify "$AWM_TEST_REMOTE" '
        "HEAD:refs/heads/forbidden-alias >/dev/null 2>&1\n"
        "alias_status=$?\n"
        'git --git-dir "$shadow" push --no-verify "$AWM_TEST_REMOTE" '
        "HEAD:refs/heads/forbidden-separated-git-dir >/dev/null 2>&1\n"
        "separated_git_dir_status=$?\n"
        '"$AWM_TEST_REAL_GIT" --git-dir="$shadow" push --no-verify '
        '"$AWM_TEST_REMOTE" HEAD:refs/heads/forbidden-real-git '
        ">/dev/null 2>&1\n"
        "real_git_status=$?\n"
        'GIT_EXEC_PATH="$AWM_TEST_GIT_EXEC_PATH" "$AWM_TEST_REAL_GIT" '
        '--git-dir="$shadow" push --no-verify "$AWM_TEST_REMOTE" '
        "HEAD:refs/heads/forbidden-exec-path >/dev/null 2>&1\n"
        "exec_path_status=$?\n"
        '"$AWM_TEST_REAL_GIT" --git-dir="$shadow" push --no-verify '
        '--receive-pack="$AWM_TEST_RECEIVE_PACK" "$AWM_TEST_REMOTE" '
        "HEAD:refs/heads/forbidden-receive-pack >/dev/null 2>&1\n"
        "receive_pack_status=$?\n"
        '"${AWM_DELIVERY_REAL_GIT:-false}" '
        '--git-dir="${AWM_DELIVERY_SHADOW_GIT_DIR:-}" push --no-verify '
        '"$AWM_TEST_REMOTE" HEAD:refs/heads/forbidden-direct >/dev/null 2>&1\n'
        "direct_git_status=$?\n"
        '(printf attack > "${GH_CONFIG_DIR%/gh}/reference-transaction") '
        "2>/dev/null\n"
        "hook_file_status=$?\n"
        "status=$(git status --porcelain)\n"
        "config_exposed=0\n"
        '[ ! -e "$GH_CONFIG_DIR/hosts.yml" ] || config_exposed=1\n'
        "printf '%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' "
        '"$commit_status" "$push_status" "$no_verify_status" '
        '"$hook_override_status" "$alias_status" "$separated_git_dir_status" '
        '"$real_git_status" "$exec_path_status" "$receive_pack_status" '
        '"$direct_git_status" '
        '"$hook_file_status" '
        '"${AWM_DELIVERY_REAL_GIT-unset}" '
        '"${AWM_DELIVERY_SHADOW_GIT_DIR-unset}" '
        '"${GH_TOKEN-unset}" "${GITHUB_TOKEN-unset}" "$config_exposed" '
        '"$status"\n',
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    publication_config = tmp_path / "publication-gh-config"
    publication_config.mkdir()
    (publication_config / "hosts.yml").write_text(
        "github.com:\n  oauth_token: publication-secret\n", encoding="utf-8"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "GH_CONFIG_DIR": str(publication_config),
            "GH_TOKEN": "push-capable-gh-token",
            "GITHUB_TOKEN": "push-capable-github-token",
            "AWM_TEST_REMOTE": str(remote),
            "AWM_TEST_REAL_GIT": shutil.which("git") or "git",
            "AWM_TEST_GIT_EXEC_PATH": subprocess.run(
                ["git", "--exec-path"], check=True, capture_output=True, text=True
            ).stdout.strip(),
            "AWM_TEST_RECEIVE_PACK": shutil.which("git-receive-pack")
            or "git-receive-pack",
            "AWM_DELIVERY_REAL_GIT": shutil.which("git") or "git",
            "AWM_DELIVERY_SHADOW_GIT_DIR": str(remote),
        }
    )

    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command(
            worker, "edit, stage, commit, and verify delivery"
        ),
        cwd=checkout,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    (
        commit_status,
        push_status,
        no_verify_status,
        hook_override_status,
        alias_status,
        separated_git_dir_status,
        real_git_status,
        exec_path_status,
        receive_pack_status,
        direct_git_status,
        hook_file_status,
        exposed_real_git,
        exposed_shadow_git_dir,
        token,
        github_token,
        config_exposed,
        status,
    ) = result.stdout.strip().split("|", 16)
    assert commit_status == "0", result.stderr
    assert push_status != "0"
    assert no_verify_status != "0"
    assert hook_override_status != "0"
    assert alias_status != "0"
    assert separated_git_dir_status != "0"
    assert real_git_status != "0"
    assert exec_path_status != "0"
    assert receive_pack_status != "0"
    assert direct_git_status != "0"
    assert hook_file_status != "0"
    assert exposed_real_git == exposed_shadow_git_dir == "unset"
    assert token == github_token == "unset"
    assert config_exposed == "0"
    assert status == ""
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head != base
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "merge-base", "--is-ancestor", base, head],
            check=False,
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "rev-list", "--count", f"{base}..{head}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "1"
    )
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "show", "HEAD:tracked.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == "after\n"
    )
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )
    for ref in (
        "refs/heads/forbidden",
        "refs/heads/forbidden-no-verify",
        "refs/heads/forbidden-hook-override",
        "refs/heads/forbidden-alias",
        "refs/heads/forbidden-separated-git-dir",
        "refs/heads/forbidden-real-git",
        "refs/heads/forbidden-exec-path",
        "refs/heads/forbidden-receive-pack",
        "refs/heads/forbidden-direct",
    ):
        assert (
            subprocess.run(
                ["git", "--git-dir", str(remote), "show-ref", "--verify", ref],
                check=False,
                capture_output=True,
            ).returncode
            != 0
        )


@pytest.mark.parametrize("worker", ["codex", "claude"])
def test_publication_disabled_retains_residual_changes_before_delivery(
    worker: str,
    tmp_path: Path,
    linked_delivery_repository: tuple[Path, Path, Path, str],
) -> None:
    repository, checkout, _, base = linked_delivery_repository
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / worker
    fake_worker.write_text(
        "#!/bin/sh\n"
        "printf 'committed\\n' > tracked.txt\n"
        "git add tracked.txt\n"
        "git commit -m committed-before-residual >/dev/null 2>&1\n"
        "printf 'staged\\n' > tracked.txt\n"
        "git add tracked.txt\n"
        "printf 'unstaged\\n' >> tracked.txt\n"
        "printf 'untracked\\n' > residual.txt\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command(worker, "commit once"),
        cwd=checkout,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    diagnostic = next(
        line
        for line in result.stderr.splitlines()
        if line.startswith("publication-disabled session left uncommitted changes")
    )
    assert diagnostic == "publication-disabled session left uncommitted changes"
    recovery_diagnostic = next(
        line
        for line in result.stderr.splitlines()
        if line.startswith("publication-disabled agent output recovery retained at ")
    )
    recovery = Path(recovery_diagnostic.rsplit(" retained at ", maxsplit=1)[1])
    try:
        assert recovery.is_dir()
        assert {path.name for path in recovery.iterdir()} == {
            ".complete",
            "metadata.json",
            "recovery.bundle",
            "status.porcelain",
        }
        metadata = json.loads((recovery / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["cleanliness"] == "residual"
        assert metadata["baseCommit"] == base
        assert metadata["shadowCommit"] != base
        assert metadata["stagedCommit"] != metadata["shadowCommit"]
        assert metadata["worktreeCommit"] != metadata["stagedCommit"]
        retained_status = (
            (recovery / "status.porcelain").read_text(encoding="utf-8").splitlines()
        )
        assert "MM tracked.txt" in retained_status
        assert "?? residual.txt" in retained_status

        recovered = tmp_path / "recovered"
        subprocess.run(["git", "init", str(recovered)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(recovered), "fetch", str(repository), base],
            check=True,
            capture_output=True,
        )
        for name in ("shadow", "staged", "worktree"):
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(recovered),
                    "fetch",
                    str(recovery / "recovery.bundle"),
                    f"refs/awm-delivery/{name}:refs/awm-delivery/{name}",
                ],
                check=True,
                capture_output=True,
            )
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(recovered),
                    "show",
                    "refs/awm-delivery/shadow:tracked.txt",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            == "committed\n"
        )
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(recovered),
                    "show",
                    "refs/awm-delivery/staged:tracked.txt",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            == "staged\n"
        )
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(recovered),
                    "show",
                    "refs/awm-delivery/worktree:tracked.txt",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            == "staged\nunstaged\n"
        )
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(recovered),
                    "show",
                    "refs/awm-delivery/worktree:residual.txt",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            == "untracked\n"
        )
        assert not list(tmp_path.glob("awm-delivery-shadow.*"))
        assert not list(tmp_path.glob("awm-delivery-worktree.*"))
        assert (
            subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == base
        )
        assert (checkout / "tracked.txt").read_text(encoding="utf-8") == "before\n"
        assert not (checkout / "residual.txt").exists()
        assert (
            subprocess.run(
                ["git", "-C", str(checkout), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            == ""
        )
    finally:
        shutil.rmtree(recovery, ignore_errors=True)


@pytest.mark.parametrize("topology", ["embedded", "submodule"])
def test_publication_disabled_recovers_nested_git_worktree_content(
    topology: str,
    tmp_path: Path,
    linked_delivery_repository: tuple[Path, Path, Path, str],
) -> None:
    _, checkout, _, _ = linked_delivery_repository
    nested = checkout / "nested"
    if topology == "submodule":
        source = tmp_path / "submodule-source"
        initialize_test_repository(source)
        (source / "tracked.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(source), "commit", "-m", "submodule base"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "protocol.file.allow=always",
                "-C",
                str(checkout),
                "submodule",
                "add",
                str(source),
                "nested",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "commit", "-am", "add submodule"],
            check=True,
            capture_output=True,
        )
        worker_body = (
            "printf 'unstaged\\n' > nested/tracked.txt\n"
            "printf 'untracked\\n' > nested/untracked.txt\n"
        )
        staged_text = "before\n"
    else:
        initialize_test_repository(nested)
        (nested / "tracked.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(nested), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(nested), "commit", "-m", "embedded base"],
            check=True,
            capture_output=True,
        )
        worker_body = (
            "printf 'staged\\n' > nested/tracked.txt\n"
            "git -C nested add tracked.txt\n"
            "printf 'unstaged\\n' >> nested/tracked.txt\n"
            "printf 'untracked\\n' > nested/untracked.txt\n"
        )
        staged_text = "staged\n"

    original_head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / "codex"
    fake_worker.write_text("#!/bin/sh\n" + worker_body, encoding="utf-8")
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command(
            "codex", "leave nested repository changes"
        ),
        cwd=checkout,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, result.stderr
    recovery_line = next(
        line
        for line in result.stderr.splitlines()
        if line.startswith("publication-disabled agent output recovery retained at ")
    )
    recovery = Path(recovery_line.rsplit(" retained at ", maxsplit=1)[1])
    try:
        metadata = json.loads((recovery / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["nestedRecoveryCount"] == 1
        nested_recovery = recovery / "nested" / "0000"
        nested_metadata = json.loads(
            (nested_recovery / "metadata.json").read_text(encoding="ascii")
        )
        assert nested_metadata["path"] == "nested"
        assert metadata["cleanliness"] == "residual"

        restored = tmp_path / "restored-nested"
        subprocess.run(["git", "init", str(restored)], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(restored),
                "fetch",
                str(nested_recovery / "staged.bundle"),
                "refs/awm-delivery/*:refs/awm-delivery/*",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(restored),
                "checkout",
                "--force",
                "refs/awm-delivery/staged",
            ],
            check=True,
            capture_output=True,
        )
        assert (restored / "tracked.txt").read_text(encoding="utf-8") == staged_text
        subprocess.run(
            [
                "git",
                "-C",
                str(restored),
                "checkout",
                "--force",
                "refs/awm-delivery/worktree",
            ],
            check=True,
            capture_output=True,
        )
        expected_worktree = (
            "staged\nunstaged\n" if topology == "embedded" else "unstaged\n"
        )
        assert (restored / "tracked.txt").read_text(encoding="utf-8") == expected_worktree
        assert (restored / "untracked.txt").read_text(encoding="utf-8") == (
            "untracked\n"
        )
        assert not list(tmp_path.glob("awm-delivery-shadow.*"))
        assert not list(tmp_path.glob("awm-delivery-worktree.*"))
        assert (
            subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == original_head
        )
        if topology == "submodule":
            assert (nested / "tracked.txt").read_text(encoding="utf-8") == "before\n"
            assert not (nested / "untracked.txt").exists()
    finally:
        shutil.rmtree(recovery, ignore_errors=True)


def test_publication_disabled_recovers_unmerged_index(
    tmp_path: Path,
    linked_delivery_repository: tuple[Path, Path, Path, str],
) -> None:
    repository, checkout, _, base = linked_delivery_repository
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / "codex"
    fake_worker.write_text(
        "#!/bin/sh\n"
        "base_blob=$(printf 'base stage\\n' | git hash-object -w --stdin)\n"
        "ours_blob=$(printf 'ours stage\\n' | git hash-object -w --stdin)\n"
        "theirs_blob=$(printf 'theirs stage\\n' | git hash-object -w --stdin)\n"
        "printf '100644 %s 1\\tconflicted.txt\\n"
        "100644 %s 2\\tconflicted.txt\\n"
        "100644 %s 3\\tconflicted.txt\\n' "
        '"$base_blob" "$ours_blob" "$theirs_blob" '
        "| git update-index --index-info\n"
        "git update-index --split-index\n"
        "printf 'worktree resolution\\n' > conflicted.txt\n"
        "printf 'untracked beside conflict\\n' > conflict-untracked.txt\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command(
            "codex", "leave an unresolved index"
        ),
        cwd=checkout,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, result.stderr
    recovery_line = next(
        line
        for line in result.stderr.splitlines()
        if line.startswith("publication-disabled agent output recovery retained at ")
    )
    recovery = Path(recovery_line.rsplit(" retained at ", maxsplit=1)[1])
    try:
        metadata = json.loads((recovery / "metadata.json").read_text(encoding="utf-8"))
        conflict_state = metadata["conflictState"]
        assert metadata["cleanliness"] == "residual"
        assert metadata["stagedCommit"] is None
        assert (recovery / conflict_state["indexFile"]).is_file()
        shared_index = recovery / conflict_state["sharedIndexFile"]
        assert shared_index.is_file()
        object_pack = recovery / conflict_state["objectPack"]
        assert object_pack.is_file()
        assert (recovery / conflict_state["objectIndex"]).is_file()
        assert not list(tmp_path.glob("awm-delivery-shadow.*"))
        assert not list(tmp_path.glob("awm-delivery-worktree.*"))

        recovered = tmp_path / "recovered-conflict"
        subprocess.run(["git", "init", str(recovered)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(recovered), "fetch", str(repository), base],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(recovered), "index-pack", "--stdin"],
            input=object_pack.read_bytes(),
            check=True,
            capture_output=True,
        )
        recovered_index = Path(
            subprocess.run(
                ["git", "-C", str(recovered), "rev-parse", "--absolute-git-dir"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        ) / "index"
        shutil.copyfile(recovery / conflict_state["indexFile"], recovered_index)
        shutil.copyfile(shared_index, recovered_index.parent / shared_index.name)
        stages = subprocess.run(
            ["git", "-C", str(recovered), "ls-files", "--unmerged"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert " 1\tconflicted.txt" in stages
        assert " 2\tconflicted.txt" in stages
        assert " 3\tconflicted.txt" in stages
        for stage, content in ((1, "base stage\n"), (2, "ours stage\n"), (3, "theirs stage\n")):
            assert (
                subprocess.run(
                    ["git", "-C", str(recovered), "show", f":{stage}:conflicted.txt"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                == content
            )
        subprocess.run(
            [
                "git",
                "-C",
                str(recovered),
                "fetch",
                str(recovery / "recovery.bundle"),
                "refs/awm-delivery/worktree:refs/awm-delivery/worktree",
            ],
            check=True,
            capture_output=True,
        )
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(recovered),
                    "show",
                    "refs/awm-delivery/worktree:conflicted.txt",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            == "worktree resolution\n"
        )
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(recovered),
                    "show",
                    "refs/awm-delivery/worktree:conflict-untracked.txt",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            == "untracked beside conflict\n"
        )
        assert subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout == ""
    finally:
        shutil.rmtree(recovery, ignore_errors=True)


def test_publication_disabled_recovers_non_utf8_and_resolve_undo_index(
    tmp_path: Path,
    linked_delivery_repository: tuple[Path, Path, Path, str],
) -> None:
    repository, checkout, _, base = linked_delivery_repository
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / "codex"
    fake_worker.write_text(
        "#!/usr/bin/python3\n"
        "import subprocess\n"
        "\n"
        "def git(*arguments, input_bytes=None):\n"
        "    return subprocess.run(\n"
        "        [b'git', *arguments], input=input_bytes, check=True,\n"
        "        stdout=subprocess.PIPE,\n"
        "    ).stdout.strip()\n"
        "\n"
        "def conflict(path, label):\n"
        "    objects = [\n"
        "        git(b'hash-object', b'-w', b'--stdin', "
        "input_bytes=label + suffix)\n"
        "        for suffix in (b' base\\n', b' ours\\n', b' theirs\\n')\n"
        "    ]\n"
        "    records = b''.join(\n"
        "        b'100644 ' + object_id + b' ' + str(stage).encode() "
        "+ b'\\t' + path + b'\\0'\n"
        "        for stage, object_id in enumerate(objects, 1)\n"
        "    )\n"
        "    git(b'update-index', b'-z', b'--index-info', "
        "input_bytes=records)\n"
        "\n"
        "unresolved = b'unresolved-\\xff.txt'\n"
        "conflict(unresolved, b'non-utf8')\n"
        "conflict(b'resolved.txt', b'resolve-undo')\n"
        "with open(unresolved, 'wb') as output:\n"
        "    output.write(b'non-utf8 worktree\\n')\n"
        "with open(b'resolved.txt', 'wb') as output:\n"
        "    output.write(b'resolved worktree\\n')\n"
        "git(b'add', b'--', b'resolved.txt')\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command(
            "codex", "leave non-UTF-8 and partially resolved conflicts"
        ),
        cwd=checkout,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, result.stderr
    recovery_line = next(
        line
        for line in result.stderr.splitlines()
        if line.startswith("publication-disabled agent output recovery retained at ")
    )
    recovery = Path(recovery_line.rsplit(" retained at ", maxsplit=1)[1])
    try:
        metadata = json.loads((recovery / "metadata.json").read_text(encoding="utf-8"))
        conflict_state = metadata["conflictState"]
        assert metadata["cleanliness"] == "residual"
        assert conflict_state["sharedIndexFile"] is None
        assert not list(tmp_path.glob("awm-delivery-shadow.*"))
        assert not list(tmp_path.glob("awm-delivery-worktree.*"))

        recovered = tmp_path / "recovered-index-extensions"
        subprocess.run(["git", "init", str(recovered)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(recovered), "fetch", str(repository), base],
            check=True,
            capture_output=True,
        )
        object_pack = recovery / conflict_state["objectPack"]
        subprocess.run(
            ["git", "-C", str(recovered), "index-pack", "--stdin"],
            input=object_pack.read_bytes(),
            check=True,
            capture_output=True,
        )
        recovered_git_dir = Path(
            subprocess.run(
                ["git", "-C", str(recovered), "rev-parse", "--absolute-git-dir"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        shutil.copyfile(
            recovery / conflict_state["indexFile"], recovered_git_dir / "index"
        )
        unmerged = subprocess.run(
            [b"git", b"ls-files", b"--unmerged", b"-z"],
            cwd=os.fsencode(recovered),
            check=True,
            capture_output=True,
        ).stdout
        assert b"unresolved-\xff.txt" in unmerged
        resolve_undo = subprocess.run(
            ["git", "-C", str(recovered), "ls-files", "--resolve-undo", "-z"],
            check=True,
            capture_output=True,
        ).stdout
        assert b"resolved.txt" in resolve_undo
        subprocess.run(
            ["git", "-C", str(recovered), "checkout", "-m", "--", "resolved.txt"],
            check=True,
            capture_output=True,
        )
        restored = (recovered / "resolved.txt").read_text(encoding="utf-8")
        assert "resolve-undo ours" in restored
        assert "resolve-undo theirs" in restored
    finally:
        shutil.rmtree(recovery, ignore_errors=True)


def test_publication_disabled_ignores_invalid_nested_git_marker(
    tmp_path: Path,
    linked_delivery_repository: tuple[Path, Path, Path, str],
) -> None:
    repository, checkout, _, base = linked_delivery_repository
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / "codex"
    fake_worker.write_text(
        "#!/bin/sh\n"
        "mkdir -p fixture\n"
        "printf 'not a gitdir\\n' > fixture/.git\n"
        "printf 'recoverable fixture data\\n' > fixture/data.txt\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command(
            "codex", "generate a fixture with an invalid .git marker"
        ),
        cwd=checkout,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, result.stderr
    recovery_line = next(
        line
        for line in result.stderr.splitlines()
        if line.startswith("publication-disabled agent output recovery retained at ")
    )
    recovery = Path(recovery_line.rsplit(" retained at ", maxsplit=1)[1])
    try:
        metadata = json.loads((recovery / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["cleanliness"] == "residual"
        assert metadata["nestedRecoveryCount"] == 0
        assert metadata["ordinaryGitMarkers"] == [
            {
                "path": "fixture/.git",
                "storedAt": "ordinary-git-markers/0000",
            }
        ]
        assert (recovery / "ordinary-git-markers" / "0000").read_text(
            encoding="utf-8"
        ) == "not a gitdir\n"
        recovered = tmp_path / "recovered-fixture"
        subprocess.run(["git", "init", str(recovered)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(recovered), "fetch", str(repository), base],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(recovered),
                "fetch",
                str(recovery / "recovery.bundle"),
                "refs/awm-delivery/worktree:refs/awm-delivery/worktree",
            ],
            check=True,
            capture_output=True,
        )
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(recovered),
                    "show",
                    "refs/awm-delivery/worktree:fixture/data.txt",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            == "recoverable fixture data\n"
        )
        assert subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout == ""
    finally:
        shutil.rmtree(recovery, ignore_errors=True)


def test_publication_disabled_recovers_when_cleanliness_cannot_be_verified(
    tmp_path: Path,
    linked_delivery_repository: tuple[Path, Path, Path, str],
) -> None:
    _, checkout, _, base = linked_delivery_repository
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    fake_git = fake_bin / "git"
    fake_git.write_text(
        f'#!/bin/sh\nif [ "$1" = status ]; then exit 71; fi\nexec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    fake_worker = fake_bin / "codex"
    fake_worker.write_text(
        "#!/bin/sh\n"
        "printf 'committed\\n' > tracked.txt\n"
        "git add tracked.txt\n"
        "git commit -m recover-before-status-failure >/dev/null 2>&1\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command(
            "codex", "commit before status failure"
        ),
        cwd=checkout,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert (
        "publication-disabled session cleanliness could not be verified"
        in result.stderr
    )
    recovery_line = next(
        line
        for line in result.stderr.splitlines()
        if line.startswith("publication-disabled agent output recovery retained at ")
    )
    recovery = Path(recovery_line.rsplit(" retained at ", maxsplit=1)[1])
    try:
        metadata = json.loads((recovery / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["cleanliness"] == "unverified"
        assert (
            subprocess.run(
                [
                    "git",
                    "bundle",
                    "list-heads",
                    str(recovery / "recovery.bundle"),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.count("refs/awm-delivery/")
            == 3
        )
        assert not list(tmp_path.glob("awm-delivery-shadow.*"))
        assert not list(tmp_path.glob("awm-delivery-worktree.*"))
        assert (
            subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == base
        )
    finally:
        shutil.rmtree(recovery, ignore_errors=True)


@pytest.mark.parametrize(
    "blocked_command", ["bundle", "status"], ids=("recovery", "cleanliness")
)
def test_publication_disabled_post_agent_signal_preserves_recovery_and_cleans_delivery(
    blocked_command: str,
    tmp_path: Path,
    linked_delivery_repository: tuple[Path, Path, Path, str],
) -> None:
    _, checkout, _, base = linked_delivery_repository
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "post-agent-phase"
    real_git = shutil.which("git")
    assert real_git is not None
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "$AWM_TEST_BLOCKED_GIT_COMMAND" ]; then\n'
        '    printf reached > "$AWM_TEST_POST_AGENT_MARKER"\n'
        "    sleep 0.5\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    fake_worker = fake_bin / "codex"
    fake_worker.write_text(
        "#!/bin/sh\n"
        "printf 'committed\\n' > tracked.txt\n"
        "git add tracked.txt\n"
        "git commit -m before-post-agent-signal >/dev/null 2>&1\n"
        "printf 'residual\\n' > residual.txt\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "AWM_TEST_BLOCKED_GIT_COMMAND": blocked_command,
            "AWM_TEST_POST_AGENT_MARKER": str(marker),
        }
    )
    process = subprocess.Popen(
        PurpleMuxCLIClient._publication_disabled_agent_command(
            "codex", "leave recoverable output"
        ),
        cwd=checkout,
        env=environment,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while process.poll() is None and not marker.exists():
        if time.monotonic() >= deadline:
            process.kill()
            pytest.fail("post-agent phase was not reached")
        time.sleep(0.01)
    if process.poll() is not None:
        pytest.fail("publication-disabled session exited before post-agent signal")

    os.killpg(process.pid, signal.SIGTERM)
    _, stderr = process.communicate(timeout=5)

    assert process.returncode == 143
    recovery_line = next(
        line
        for line in stderr.splitlines()
        if line.startswith("publication-disabled agent output recovery retained at ")
    )
    recovery = Path(recovery_line.rsplit(" retained at ", maxsplit=1)[1])
    try:
        assert (recovery / ".complete").is_file()
        assert (recovery / "recovery.bundle").is_file()
        assert not list(tmp_path.glob("awm-delivery-shadow.*"))
        assert not list(tmp_path.glob("awm-delivery-worktree.*"))
        assert (
            subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == base
        )
    finally:
        shutil.rmtree(recovery, ignore_errors=True)


@pytest.mark.parametrize("worker", ["codex", "claude"])
def test_publication_disabled_isolates_late_and_nested_local_remotes(
    worker: str,
    tmp_path: Path,
    linked_delivery_repository: tuple[Path, Path, Path, str],
) -> None:
    _, checkout, _, _ = linked_delivery_repository
    (checkout / ".gitignore").write_text(
        "late-remote.git/\nnested-remote/\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(checkout), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "commit", "-m", "ignore local remotes"],
        check=True,
        capture_output=True,
    )
    nested_remote = checkout / "nested-remote"
    subprocess.run(
        ["git", "init", "-b", "main", str(nested_remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(nested_remote), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(nested_remote), "config", "user.email", "test@example.com"],
        check=True,
    )
    (nested_remote / "nested.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(nested_remote), "add", "nested.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(nested_remote), "commit", "-m", "nested base"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(nested_remote),
            "config",
            "receive.denyCurrentBranch",
            "updateInstead",
        ],
        check=True,
    )
    nested_head = subprocess.run(
        ["git", "-C", str(nested_remote), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / worker
    fake_worker.write_text(
        "#!/bin/sh\n"
        "shadow=\n"
        "previous=\n"
        "for argument do\n"
        '    if [ "$previous" = --add-dir ]; then shadow=$argument; break; fi\n'
        "    previous=$argument\n"
        "done\n"
        "printf 'delivered\\n' > tracked.txt\n"
        "git add tracked.txt\n"
        "git commit -m isolated-delivery >/dev/null 2>&1\n"
        '"$AWM_TEST_REAL_GIT" --git-dir="$shadow" push --force --no-verify '
        '--receive-pack="$AWM_TEST_RECEIVE_PACK" nested-remote '
        "HEAD:main >/dev/null 2>&1\n"
        "nested_status=$?\n"
        '"$AWM_TEST_REAL_GIT" init --bare late-remote.git >/dev/null 2>&1\n'
        '"$AWM_TEST_REAL_GIT" --git-dir="$shadow" push --no-verify '
        '--receive-pack="$AWM_TEST_RECEIVE_PACK" late-remote.git '
        "HEAD:refs/heads/main >/dev/null 2>&1\n"
        "late_status=$?\n"
        'nested_after=$("$AWM_TEST_REAL_GIT" -C nested-remote rev-parse HEAD)\n'
        'late_after=$("$AWM_TEST_REAL_GIT" --git-dir=late-remote.git '
        "rev-parse refs/heads/main)\n"
        "printf 'isolated-remotes:%s|%s|%s|%s\\n' "
        '"$nested_status" "$late_status" "$nested_after" "$late_after"\n',
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "AWM_TEST_REAL_GIT": shutil.which("git") or "git",
            "AWM_TEST_RECEIVE_PACK": shutil.which("git-receive-pack")
            or "git-receive-pack",
        }
    )

    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command(worker, "commit once"),
        cwd=checkout,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    marker = next(
        line for line in result.stdout.splitlines() if line.startswith("isolated-remotes:")
    )
    nested_status, late_status, nested_after, late_after = marker.removeprefix(
        "isolated-remotes:"
    ).split("|")
    assert nested_status == late_status == "0"
    assert nested_after == late_after
    assert nested_remote.joinpath("nested.txt").read_text(encoding="utf-8") == "before\n"
    assert (
        subprocess.run(
            ["git", "-C", str(nested_remote), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == nested_head
    )
    assert not (checkout / "late-remote.git").exists()
    assert (checkout / "tracked.txt").read_text(encoding="utf-8") == "delivered\n"


@pytest.mark.parametrize("worker", ["codex", "claude"])
def test_publication_disabled_rolls_back_ref_when_real_index_update_fails(
    worker: str,
    tmp_path: Path,
    linked_delivery_repository: tuple[Path, Path, Path, str],
) -> None:
    _, checkout, _, base = linked_delivery_repository
    index_path = Path(
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "index",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    index_lock = index_path.with_name("index.lock")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / worker
    fake_worker.write_text(
        "#!/bin/sh\n"
        "printf 'after\\n' > tracked.txt\n"
        "git add tracked.txt\n"
        "git commit -m index-failure >/dev/null 2>&1\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    try:
        index_lock.touch()
        result = subprocess.run(
            PurpleMuxCLIClient._publication_disabled_agent_command(
                worker, "commit a change"
            ),
            cwd=checkout,
            env=environment,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert (
            subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == base
        )
        assert (
            subprocess.run(
                ["git", "-C", str(checkout), "diff", "--cached", "--name-only"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            == ""
        )
        assert (checkout / "tracked.txt").read_text(encoding="utf-8") == "before\n"
    finally:
        index_lock.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("agent_status", "use_pty"),
    [(0, False), (29, False), (0, True), (29, True)],
    ids=("success-no-stdin", "failure-no-stdin", "success-pty", "failure-pty"),
)
def test_publication_disabled_cleanup_is_unconditional_and_noninteractive(
    agent_status: int, use_pty: bool, tmp_path: Path
) -> None:
    initialize_test_repository(tmp_path)
    fake_bin = tmp_path.parent / f"{tmp_path.name}-bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / "codex"
    fake_worker.write_text(
        "#!/bin/sh\n"
        "shadow=\n"
        "previous=\n"
        "for argument do\n"
        '    if [ "$previous" = --add-dir ]; then shadow=$argument; break; fi\n'
        "    previous=$argument\n"
        "done\n"
        "hooks=${GH_CONFIG_DIR%/gh}\n"
        'printf \'cleanup-resources:%s|%s\\n\' "$hooks" "$shadow"\n'
        'for directory in "$shadow"; do\n'
        '    mkdir "$directory/write-protected"\n'
        '    : > "$directory/write-protected/file"\n'
        '    chmod 400 "$directory/write-protected/file"\n'
        '    chmod 500 "$directory/write-protected"\n'
        "done\n"
        f"exit {agent_status}\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    command = PurpleMuxCLIClient._publication_disabled_agent_command(
        "codex", "exercise cleanup"
    )
    arguments: str | list[str] = command
    if use_pty:
        arguments = ["script", "-q", "-e", "-c", command, "/dev/null"]

    result = subprocess.run(
        arguments,
        cwd=tmp_path,
        env=environment,
        shell=not use_pty,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    resource_line = next(
        line
        for line in (result.stdout + result.stderr).splitlines()
        if "cleanup-resources:" in line
    )
    resources = resource_line.split("cleanup-resources:", 1)[1].split("|")
    try:
        assert result.returncode == agent_status
        assert len(resources) == 2
        assert all(not Path(resource).exists() for resource in resources)
    finally:
        for resource in resources:
            shutil.rmtree(resource, ignore_errors=True)


@pytest.mark.parametrize(
    ("sent_signal", "expected_status"),
    [
        (signal.SIGHUP, 129),
        (signal.SIGINT, 130),
        (signal.SIGTERM, 143),
    ],
    ids=("hangup", "interrupt", "terminate"),
)
@pytest.mark.parametrize("use_pty", [False, True], ids=("no-stdin", "pty"))
def test_publication_disabled_signal_cleanup_is_prompt_and_exactly_once(
    sent_signal: signal.Signals,
    expected_status: int,
    use_pty: bool,
    tmp_path: Path,
) -> None:
    initialize_test_repository(tmp_path)
    removal_record = tmp_path / "cleanup-removals"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / "codex"
    fake_worker.write_text(
        "#!/bin/sh\n"
        "shadow=\n"
        "previous=\n"
        "for argument do\n"
        '    if [ "$previous" = --add-dir ]; then shadow=$argument; break; fi\n'
        "    previous=$argument\n"
        "done\n"
        'printf \'%s\\n%s\\n\' "${GH_CONFIG_DIR%/gh}" "$shadow" '
        '> "$shadow/cleanup-resources"\n'
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    real_rm = shutil.which("rm")
    assert real_rm is not None
    fake_rm = fake_bin / "rm"
    fake_rm.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$AWM_TEST_REMOVAL_RECORD"\n'
        f'exec "{real_rm}" "$@"\n',
        encoding="utf-8",
    )
    fake_rm.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["AWM_TEST_REMOVAL_RECORD"] = str(removal_record)
    command = PurpleMuxCLIClient._publication_disabled_agent_command(
        "codex", "wait for interruption"
    )
    master_fd: int | None = None
    slave_fd: int | None = None
    stdin: int = subprocess.DEVNULL
    if use_pty:
        master_fd, slave_fd = pty.openpty()
        stdin = slave_fd
    process = subprocess.Popen(
        command,
        cwd=tmp_path,
        env=environment,
        shell=True,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if slave_fd is not None:
        os.close(slave_fd)
    resources: list[str] = []
    try:
        deadline = time.monotonic() + 5
        while process.poll() is None:
            records = list(
                tmp_path.parent.glob("awm-delivery-shadow.*/cleanup-resources")
            )
            if records:
                resources = records[0].read_text(encoding="utf-8").splitlines()
                if len(resources) == 2:
                    break
            if time.monotonic() >= deadline:
                pytest.fail("blocking worker did not record cleanup resources")
            time.sleep(0.01)
        if len(resources) != 2:
            pytest.fail("blocking worker exited before recording cleanup resources")

        os.killpg(process.pid, sent_signal)
        process.communicate(timeout=5)

        assert process.returncode == expected_status
        assert len(resources) == 2
        assert all(not Path(resource).exists() for resource in resources)
        removals = removal_record.read_text(encoding="utf-8").splitlines()
        directory_removals = [
            removal for removal in removals if removal.startswith("-rf")
        ]
        assert len(directory_removals) == 4
        removed_directories = {
            removal.removeprefix("-rf -- ") for removal in directory_removals
        }
        assert removed_directories.issuperset(resources)
        assert any("awm-delivery-worktree." in path for path in removed_directories)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        if master_fd is not None:
            os.close(master_fd)
        for resource in resources:
            shutil.rmtree(resource, ignore_errors=True)


def test_publication_disabled_cleanup_is_registered_before_shadow_creation(
    tmp_path: Path,
) -> None:
    initialize_test_repository(tmp_path)
    hook_record = tmp_path / "created-hook-directory"
    collision_record = tmp_path / "foreign-collision"
    call_record = tmp_path / "allocator-calls"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        '[ "$1" = -c ] || exec "$AWM_TEST_REAL_PYTHON" "$@"\n'
        "call=0\n"
        'if [ -f "$AWM_TEST_CALL_RECORD" ]; then '
        'IFS= read -r call < "$AWM_TEST_CALL_RECORD"; fi\n'
        "call=$((call + 1))\n"
        'printf \'%s\\n\' "$call" > "$AWM_TEST_CALL_RECORD"\n'
        '[ "$call" -eq 1 ] || exit 73\n'
        'collision="$4/${5}collision"\n'
        'owned="$4/${5}owned"\n'
        'mkdir -- "$collision"\n'
        'printf \'%s\\n\' "$collision" > "$AWM_TEST_COLLISION_RECORD"\n'
        'printf \'%s\\n\' "$owned" > "$AWM_TEST_HOOK_RECORD"\n'
        'exec "$AWM_TEST_REAL_PYTHON" -c '
        "'import secrets, sys; names = iter((\"collision\", \"owned\")); "
        "secrets.token_hex = lambda _: next(names); code = sys.argv[1]; "
        "sys.argv = sys.argv[1:]; exec(code)' "
        '"$2" "$3" "$4" "$5"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment.update(
        {
            "AWM_TEST_CALL_RECORD": str(call_record),
            "AWM_TEST_COLLISION_RECORD": str(collision_record),
            "AWM_TEST_HOOK_RECORD": str(hook_record),
            "AWM_TEST_REAL_PYTHON": sys.executable,
        }
    )

    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command("codex", "not reached"),
        cwd=tmp_path,
        env=environment,
        shell=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 73
    assert not Path(hook_record.read_text(encoding="utf-8").strip()).exists()
    collision = Path(collision_record.read_text(encoding="utf-8").strip())
    try:
        assert collision.is_dir()
    finally:
        collision.rmdir()


@pytest.mark.parametrize(
    "blocked_call",
    [1, 2, 3, 4],
    ids=("hooks", "shadow", "worktree", "recovery"),
)
@pytest.mark.parametrize("use_pty", [False, True], ids=("no-stdin", "pty"))
def test_publication_disabled_signal_cleans_directory_before_allocator_returns(
    blocked_call: int, use_pty: bool, tmp_path: Path
) -> None:
    initialize_test_repository(tmp_path)
    resource_record = tmp_path / "created-resources"
    call_record = tmp_path / "allocator-calls"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        '[ "$1" = -c ] || exec "$AWM_TEST_REAL_PYTHON" "$@"\n'
        "call=0\n"
        'if [ -f "$AWM_TEST_CALL_RECORD" ]; then '
        'IFS= read -r call < "$AWM_TEST_CALL_RECORD"; fi\n'
        "call=$((call + 1))\n"
        'printf \'%s\\n\' "$call" > "$AWM_TEST_CALL_RECORD"\n'
        'created=$("$AWM_TEST_REAL_PYTHON" "$@") || exit $?\n'
        'printf \'%s\\n\' "$created" >> "$AWM_TEST_RESOURCE_RECORD"\n'
        'if [ "$call" -eq "$AWM_TEST_BLOCK_CALL" ]; then\n'
        "    while :; do sleep 1; done\n"
        "fi\n"
        'printf \'%s\\n\' "$created"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "AWM_TEST_BLOCK_CALL": str(blocked_call),
            "AWM_TEST_CALL_RECORD": str(call_record),
            "AWM_TEST_RESOURCE_RECORD": str(resource_record),
            "AWM_TEST_REAL_PYTHON": sys.executable,
        }
    )
    command = PurpleMuxCLIClient._publication_disabled_agent_command(
        "codex", "not reached"
    )
    master_fd: int | None = None
    slave_fd: int | None = None
    stdin: int = subprocess.DEVNULL
    if use_pty:
        master_fd, slave_fd = pty.openpty()
        stdin = slave_fd
    process = subprocess.Popen(
        command,
        cwd=tmp_path,
        env=environment,
        shell=True,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if slave_fd is not None:
        os.close(slave_fd)
    resources: list[str] = []
    try:
        deadline = time.monotonic() + 5
        while process.poll() is None:
            if resource_record.exists():
                resources = resource_record.read_text(encoding="utf-8").splitlines()
                if len(resources) == blocked_call:
                    break
            if time.monotonic() >= deadline:
                pytest.fail("blocking allocator did not record its created directory")
            time.sleep(0.01)
        if len(resources) != blocked_call:
            pytest.fail("allocator exited before the acquisition could be interrupted")

        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=5)

        assert process.returncode == 143
        assert all(not Path(resource).exists() for resource in resources)
        hooks_root = Path(
            subprocess.run(
                ["git", "-C", str(tmp_path), "rev-parse", "--git-path", "hooks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        assert not list(hooks_root.glob(".awm-delivery.*.resources"))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        if master_fd is not None:
            os.close(master_fd)
        for resource in resources:
            shutil.rmtree(resource, ignore_errors=True)


@pytest.mark.parametrize("agent_status", [0, 29], ids=("success", "agent-failure"))
@pytest.mark.parametrize("use_pty", [False, True], ids=("no-stdin", "pty"))
def test_publication_disabled_cleanup_failure_is_bounded_and_preserves_agent_status(
    agent_status: int, use_pty: bool, tmp_path: Path
) -> None:
    initialize_test_repository(tmp_path)
    removal_record = tmp_path / "cleanup-removals"
    fake_bin = tmp_path.parent / f"{tmp_path.name}-bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / "codex"
    fake_worker.write_text(
        "#!/bin/sh\n"
        "shadow=\n"
        "previous=\n"
        "for argument do\n"
        '    if [ "$previous" = --add-dir ]; then shadow=$argument; break; fi\n'
        "    previous=$argument\n"
        "done\n"
        'printf \'cleanup-resources:%s|%s\\n\' "${GH_CONFIG_DIR%/gh}" "$shadow"\n'
        f"exit {agent_status}\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    fake_rm = fake_bin / "rm"
    fake_rm.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$AWM_TEST_REMOVAL_RECORD"\n'
        'printf \'%s\\n\' "$$" >> "$AWM_TEST_CLEANUP_PID_RECORD"\n'
        "trap '' TERM\n"
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    fake_rm.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["AWM_TEST_REMOVAL_RECORD"] = str(removal_record)
    cleanup_pid_record = tmp_path / "cleanup-pids"
    environment["AWM_TEST_CLEANUP_PID_RECORD"] = str(cleanup_pid_record)
    command = PurpleMuxCLIClient._publication_disabled_agent_command(
        "codex", "fail cleanup"
    )
    arguments: str | list[str] = command
    if use_pty:
        arguments = ["script", "-q", "-e", "-c", command, "/dev/null"]

    result = subprocess.run(
        arguments,
        cwd=tmp_path,
        env=environment,
        shell=not use_pty,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    resource_line = next(
        line
        for line in (result.stdout + result.stderr).splitlines()
        if "cleanup-resources:" in line
    )
    resources = resource_line.split("cleanup-resources:", 1)[1].split("|")
    try:
        expected_status = agent_status if agent_status else 1
        assert result.returncode == expected_status
        assert (
            "publication-disabled session cleanup failed; temporary resources may remain"
            in result.stdout + result.stderr
        )
        removals = removal_record.read_text(encoding="utf-8").splitlines()
        directory_removals = [
            removal for removal in removals if removal.startswith("-rf")
        ]
        expected_directory_removals = 4 if agent_status == 0 else 3
        assert len(directory_removals) == expected_directory_removals
        removed_directories = {
            removal.removeprefix("-rf -- ") for removal in directory_removals
        }
        assert removed_directories.issuperset(resources)
        assert any("awm-delivery-worktree." in path for path in removed_directories)
        cleanup_pids = cleanup_pid_record.read_text(encoding="utf-8").splitlines()
        assert len(cleanup_pids) == expected_directory_removals + 1
        for cleanup_pid in cleanup_pids:
            with pytest.raises(ProcessLookupError):
                os.kill(int(cleanup_pid), 0)
    finally:
        for resource in resources:
            shutil.rmtree(resource, ignore_errors=True)


def test_claude_publication_disabled_uses_strict_os_sandbox() -> None:
    command = PurpleMuxCLIClient._publication_disabled_agent_command(
        "claude", "run tests"
    )
    arguments = shlex.split(command)
    settings = json.loads(arguments[arguments.index("--settings") + 1])

    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["failIfUnavailable"] is True
    assert settings["sandbox"]["filesystem"]["denyWrite"] == ["./.git"]
    assert settings["sandbox"]["network"]["allowedDomains"] == []
    assert settings["sandbox"]["network"]["strictAllowlist"] is True
    assert settings["sandbox"]["network"]["deniedDomains"] == [
        "github.com",
        "*.github.com",
    ]
    assert {entry["name"] for entry in settings["sandbox"]["credentials"]["envVars"]} == {
        "GH_TOKEN",
        "GITHUB_TOKEN",
    }
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1" in command


@pytest.mark.parametrize("worker", ["codex", "claude"])
def test_publication_disabled_allows_ephemeral_nested_repository_work(
    worker: str, tmp_path: Path
) -> None:
    subprocess.run(
        ["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "base"],
        check=True,
        capture_output=True,
    )
    fake_bin = tmp_path.parent / f"{tmp_path.name}-bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / worker
    fake_worker.write_text(
        "#!/bin/sh\n"
        'nested=$(mktemp -d "${PWD%/*}/nested-repository.XXXXXX")\n'
        "git -C \"$nested\" init -b main >/dev/null 2>&1\n"
        "git -C \"$nested\" config user.name Test\n"
        "git -C \"$nested\" config user.email test@example.com\n"
        "git -C \"$nested\" commit --allow-empty -m nested >/dev/null 2>&1\n"
        "printf '%s|%s\\n' \"$?\" \"$nested\"\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command(worker, "run tests"),
        cwd=tmp_path,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    commit_status, nested_path = result.stdout.strip().split("|", 1)
    assert commit_status == "0"
    assert not Path(nested_path).exists()


@pytest.mark.parametrize("worker", ["codex", "claude"])
def test_publication_disabled_allows_only_linked_checkout_git_metadata(
    worker: str, tmp_path: Path
) -> None:
    repository = tmp_path / "repository parent"
    checkout = tmp_path / "linked checkout"
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "test@example.com",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--allow-empty", "-m", "base"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-b",
            "feature/scoped-commit",
            str(checkout),
        ],
        check=True,
        capture_output=True,
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_worker = fake_bin / worker
    fake_worker.write_text(
        "#!/bin/sh\n"
        'printf \'agent-argument:%s\\n\' "$@"\n'
        "git commit --allow-empty -m linked-forward >/dev/null 2>&1\n"
        "forward=$?\n"
        "git commit --amend --allow-empty -m forbidden-rewrite >/dev/null 2>&1\n"
        "rewrite=$?\n"
        "git tag forbidden-tag >/dev/null 2>&1\n"
        "tag=$?\n"
        'printf \'%s %s %s\\n\' "$forward" "$rewrite" "$tag"\n',
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command(worker, "commit once"),
        cwd=checkout,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    output_lines = result.stdout.splitlines()
    forward, rewrite, tag = output_lines[-1].split()
    assert forward == "0"
    assert rewrite != "0"
    assert tag != "0"
    arguments = [
        line.removeprefix("agent-argument:")
        for line in output_lines
        if line.startswith("agent-argument:")
    ]
    writable_directories = {
        arguments[index + 1]
        for index, argument in enumerate(arguments[:-1])
        if argument == "--add-dir"
    }
    git_dir = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--absolute-git-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    common_dir = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert len(writable_directories) == 1
    (shadow_git_path,) = writable_directories
    shadow_git_dir = Path(shadow_git_path)
    assert shadow_git_dir.name.startswith("awm-delivery-shadow.")
    assert Path(common_dir) not in shadow_git_dir.parents
    assert git_dir not in writable_directories
    assert common_dir not in writable_directories
    assert str(Path(common_dir) / "objects") not in writable_directories
    assert str(Path(common_dir) / "refs" / "heads" / "feature") not in writable_directories
    assert (
        str(Path(common_dir) / "logs" / "refs" / "heads" / "feature")
        not in writable_directories
    )
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "log", "-1", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "linked-forward"
    )
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "tag", "--list", "forbidden-tag"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )


@pytest.mark.parametrize("worker", ["codex", "claude"])
def test_publication_disabled_rejects_modified_sparse_checkout_before_launch(
    worker: str, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    checkout = tmp_path / "linked-checkout"
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    (repository / "included").mkdir()
    (repository / "included" / "tracked.txt").write_text("before\n", encoding="utf-8")
    (repository / "omitted").mkdir()
    (repository / "omitted" / "tracked.txt").write_text("preserve\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-b",
            "feature/sparse",
            str(checkout),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "sparse-checkout", "set", "included"],
        check=True,
        capture_output=True,
    )
    modified = checkout / "included" / "tracked.txt"
    modified.write_text("after\n", encoding="utf-8")
    original_head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    marker = tmp_path / "worker-launched"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / worker
    fake_worker.write_text(
        "#!/bin/sh\n"
        'printf launched > "$AWM_TEST_LAUNCH_MARKER"\n'
        "git add -A && git commit -m unintended\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["AWM_TEST_LAUNCH_MARKER"] = str(marker)

    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command(worker, "commit change"),
        cwd=checkout,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "do not support sparse checkouts" in result.stderr
    assert not marker.exists()
    assert modified.read_text(encoding="utf-8") == "after\n"
    assert not (checkout / "omitted" / "tracked.txt").exists()
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == original_head
    )
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "status", "--short"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == " M included/tracked.txt\n"
    )


def test_publication_disabled_repeated_commits_transfer_only_new_objects(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    checkout = tmp_path / "linked-checkout"
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    tracked = repository / "tracked"
    tracked.mkdir()
    for index in range(32):
        (tracked / f"file-{index:02}.txt").write_text(
            f"base {index}\n", encoding="utf-8"
        )
    subprocess.run(
        ["git", "-C", str(repository), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    for index in range(3):
        (tracked / f"file-{index:02}.txt").write_text(
            f"history {index}\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "-C", str(repository), "commit", "-am", f"history {index}"],
            check=True,
            capture_output=True,
        )
    historical_object_count = len(
        subprocess.run(
            ["git", "-C", str(repository), "rev-list", "--objects", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-b",
            "feature/repeated",
            str(checkout),
        ],
        check=True,
        capture_output=True,
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "printf 'next\\n' >> tracked/file-00.txt\n"
        "git add tracked/file-00.txt\n"
        "git commit -m incremental >/dev/null 2>&1\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    pack_directory = repository / ".git" / "objects" / "pack"

    transferred_object_counts: list[int] = []
    for _ in range(2):
        packs_before = set(pack_directory.glob("*.idx"))
        result = subprocess.run(
            PurpleMuxCLIClient._publication_disabled_agent_command(
                "codex", "commit one change"
            ),
            cwd=checkout,
            env=environment,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        new_packs = set(pack_directory.glob("*.idx")) - packs_before
        assert len(new_packs) == 1
        (new_pack,) = new_packs
        verify_lines = subprocess.run(
            ["git", "verify-pack", "-v", str(new_pack)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        transferred_object_counts.append(
            sum(
                len(fields) >= 5 and fields[1] in {"blob", "commit", "tag", "tree"}
                for fields in (line.split() for line in verify_lines)
            )
        )

    assert historical_object_count > 32
    assert transferred_object_counts == [4, 4]
    assert all(count < historical_object_count for count in transferred_object_counts)
    assert subprocess.run(
        ["git", "-C", str(checkout), "log", "-2", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines() == ["incremental", "incremental"]


@pytest.mark.parametrize("worker", ["codex", "claude"])
def test_publication_disabled_real_sandbox_denies_live_git_metadata_writes(
    worker: str, tmp_path: Path
) -> None:
    sandbox_engine = shutil.which("codex" if worker == "codex" else "bwrap")
    if sandbox_engine is None:
        pytest.skip(f"{worker} sandbox engine is unavailable")

    repository = tmp_path / "repository"
    checkout = tmp_path / "linked-checkout"
    remote = checkout / "forbidden-remote.git"
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    (repository / ".gitignore").write_text(
        "forbidden-remote.git/\nsandbox-payload\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(repository), "add", ".gitignore"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "branch", "feature/sibling"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-b",
            "feature/scoped-commit",
            str(checkout),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )

    def git_path(name: str) -> Path:
        return Path(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-path",
                    name,
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )

    active_ref = git_path("refs/heads/feature/scoped-commit")
    sibling_ref = git_path("refs/heads/feature/sibling")
    active_log = git_path("logs/refs/heads/feature/scoped-commit")
    git_pointer = checkout / ".git"
    common_git_dir = Path(
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    protected_contents = {
        path: path.read_bytes()
        for path in (active_ref, sibling_ref, active_log, git_pointer)
    }
    base = protected_contents[active_ref].decode().strip()

    payload = checkout / "sandbox-payload"
    payload.write_text(
        "#!/bin/sh\n"
        "git commit --allow-empty -m sandbox-forward >/dev/null 2>&1\n"
        "forward=$?\n"
        "git commit --amend --allow-empty -m forbidden-rewrite >/dev/null 2>&1\n"
        "rewrite=$?\n"
        "git tag forbidden-tag >/dev/null 2>&1\n"
        "tag=$?\n"
        '(printf attack > "$AWM_TEST_ACTIVE_REF") 2>/dev/null\n'
        "active=$?\n"
        '(printf attack > "$AWM_TEST_SIBLING_REF") 2>/dev/null\n'
        "sibling=$?\n"
        '(printf attack >> "$AWM_TEST_ACTIVE_LOG") 2>/dev/null\n'
        "reflog=$?\n"
        "(printf attack > .git) 2>/dev/null\n"
        "git_pointer=$?\n"
        '"$AWM_TEST_REAL_GIT" update-ref "$AWM_TEST_ACTIVE_REF_NAME" '
        '"$AWM_TEST_BASE" >/dev/null 2>&1\n'
        "direct_git=$?\n"
        '"$AWM_TEST_REAL_GIT" --git-dir="$AWM_TEST_SANDBOX_SHADOW" '
        'push --no-verify "$AWM_TEST_REMOTE" '
        "HEAD:refs/heads/forbidden >/dev/null 2>&1\n"
        "direct_push=$?\n"
        'GIT_EXEC_PATH="$AWM_TEST_GIT_EXEC_PATH" "$AWM_TEST_REAL_GIT" '
        '--git-dir="$AWM_TEST_SANDBOX_SHADOW" push --no-verify '
        '"$AWM_TEST_REMOTE" HEAD:refs/heads/forbidden-exec-path '
        ">/dev/null 2>&1\n"
        "exec_path_push=$?\n"
        '"$AWM_TEST_REAL_GIT" --git-dir="$AWM_TEST_SANDBOX_SHADOW" '
        'push --no-verify --receive-pack="$AWM_TEST_RECEIVE_PACK" '
        '"$AWM_TEST_REMOTE" HEAD:refs/heads/forbidden-receive-pack '
        ">/dev/null 2>&1\n"
        "receive_pack_push=$?\n"
        'git -c alias.ship=push ship --no-verify "$AWM_TEST_REMOTE" '
        "HEAD:refs/heads/forbidden-alias >/dev/null 2>&1\n"
        "alias_push=$?\n"
        'printf \'sandbox-results:%s %s %s %s %s %s %s %s %s %s %s %s\\n\' '
        '"$forward" "$rewrite" '
        '"$tag" "$active" "$sibling" "$reflog" "$git_pointer" '
        '"$direct_git" "$direct_push" "$exec_path_push" '
        '"$receive_pack_push" "$alias_push"\n',
        encoding="utf-8",
    )
    payload.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_worker = fake_bin / worker
    fake_worker.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "arguments = sys.argv[1:]\n"
        "shadow = arguments[arguments.index('--add-dir') + 1]\n"
        "os.environ['AWM_TEST_SANDBOX_SHADOW'] = shadow\n"
        "engine = os.environ['AWM_TEST_SANDBOX_ENGINE']\n"
        "payload = os.environ['AWM_TEST_SANDBOX_PAYLOAD']\n"
        "if os.environ['AWM_TEST_SANDBOX_KIND'] == 'codex':\n"
        "    filesystem = (\n"
        '        \'{":workspace_roots"={".git"="read","."="write"},\'\n'
        "        + json.dumps(shadow) + '=\"write\"}'\n"
        "    )\n"
        "    command = [engine, 'sandbox', '-P', 'awm-test', '-C', os.getcwd(),\n"
        "        '-c', 'permissions.awm-test.extends=\":read-only\"',\n"
        "        '-c', 'permissions.awm-test.filesystem=' + filesystem, '--', payload]\n"
        "else:\n"
        "    settings = json.loads(arguments[arguments.index('--settings') + 1])\n"
        "    deny_write = settings['sandbox']['filesystem']['denyWrite']\n"
        "    mandatory_deny_write = [\n"
        "        os.path.join(os.environ['AWM_TEST_COMMON_GIT_DIR'], 'config'),\n"
        "        os.path.join(os.environ['AWM_TEST_COMMON_GIT_DIR'], 'hooks'),\n"
        "    ]\n"
        "    command = [engine, '--die-with-parent', '--new-session', '--unshare-net',\n"
        "        '--ro-bind', '/', '/', '--dev-bind', '/dev', '/dev',\n"
        "        '--proc', '/proc', '--bind', os.getcwd(), os.getcwd(),\n"
        "        '--bind', shadow, shadow]\n"
        "    for denied in deny_write + mandatory_deny_write:\n"
        "        denied_path = (denied if os.path.isabs(denied) else\n"
        "            os.path.realpath(os.path.join(os.getcwd(), denied)))\n"
        "        command.extend(['--ro-bind', denied_path, denied_path])\n"
        "    command.extend(['--chdir', os.getcwd(), payload])\n"
        "raise SystemExit(subprocess.run(command, check=False).returncode)\n",
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "AWM_TEST_ACTIVE_LOG": str(active_log),
            "AWM_TEST_ACTIVE_REF": str(active_ref),
            "AWM_TEST_ACTIVE_REF_NAME": "refs/heads/feature/scoped-commit",
            "AWM_TEST_BASE": base,
            "AWM_TEST_COMMON_GIT_DIR": str(common_git_dir),
            "AWM_TEST_REAL_GIT": shutil.which("git") or "git",
            "AWM_TEST_GIT_EXEC_PATH": subprocess.run(
                ["git", "--exec-path"], check=True, capture_output=True, text=True
            ).stdout.strip(),
            "AWM_TEST_RECEIVE_PACK": shutil.which("git-receive-pack")
            or "git-receive-pack",
            "AWM_TEST_REMOTE": str(remote),
            "AWM_TEST_SANDBOX_ENGINE": sandbox_engine,
            "AWM_TEST_SANDBOX_KIND": worker,
            "AWM_TEST_SANDBOX_PAYLOAD": str(payload),
            "AWM_TEST_SIBLING_REF": str(sibling_ref),
        }
    )
    result = subprocess.run(
        PurpleMuxCLIClient._publication_disabled_agent_command(worker, "commit once"),
        cwd=checkout,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    status_line = next(
        line for line in result.stdout.splitlines() if line.startswith("sandbox-results:")
    )
    statuses = status_line.removeprefix("sandbox-results:").split()
    assert statuses[0] == "0"
    assert all(status != "0" for status in statuses[1:8])
    assert statuses[-1] != "0"
    assert protected_contents[sibling_ref] == sibling_ref.read_bytes()
    assert protected_contents[git_pointer] == git_pointer.read_bytes()
    assert active_ref.read_text(encoding="utf-8").strip() != base
    assert active_log.read_bytes().startswith(protected_contents[active_log])
    for ref in (
        "refs/heads/forbidden",
        "refs/heads/forbidden-exec-path",
        "refs/heads/forbidden-receive-pack",
        "refs/heads/forbidden-alias",
    ):
        assert (
            subprocess.run(
                ["git", "--git-dir", str(remote), "show-ref", "--verify", ref],
                check=False,
                capture_output=True,
            ).returncode
            != 0
        )
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "log", "-1", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "sandbox-forward"
    )
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "tag", "--list", "forbidden-tag"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )


def test_restricted_codex_git_boundary_allows_advance_but_denies_rewrites_and_tags(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "base"],
        check=True,
        capture_output=True,
    )
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "git commit --allow-empty -m advance >/dev/null 2>&1\n"
        "advance=$?\n"
        "git commit --amend --allow-empty -m rewrite >/dev/null 2>&1\n"
        "amend=$?\n"
        "git tag forbidden >/dev/null 2>&1\n"
        "tag=$?\n"
        "printf '%s %s %s\\n' \"$advance\" \"$amend\" \"$tag\"\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"

    result = subprocess.run(
        PurpleMuxCLIClient._restricted_agent_command("codex", "repair safely"),
        cwd=tmp_path,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    advance_status, amend_status, tag_status = result.stdout.strip().split()
    assert advance_status == "0"
    assert amend_status != "0"
    assert tag_status != "0"
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "merge-base", "--is-ancestor", base, "HEAD"],
        check=False,
    ).returncode == 0
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "tag", "--list", "forbidden"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_restricted_git_boundary_allows_fast_forward_merge(
    provider: str, tmp_path: Path
) -> None:
    subprocess.run(
        ["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "base"],
        check=True,
        capture_output=True,
    )
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(tmp_path), "switch", "-c", "authoritative"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "commit",
            "--allow-empty",
            "-m",
            "authoritative advance",
        ],
        check=True,
        capture_output=True,
    )
    authoritative = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(tmp_path), "switch", "main"],
        check=True,
        capture_output=True,
    )
    fake_provider = tmp_path / provider
    fake_provider.write_text(
        "#!/bin/sh\n"
        "git merge --ff-only authoritative >/dev/null 2>&1\n"
        "printf '%s\\n' \"$?\"\n",
        encoding="utf-8",
    )
    fake_provider.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"

    result = subprocess.run(
        PurpleMuxCLIClient._restricted_agent_command(provider, "adopt remote head"),
        cwd=tmp_path,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "0"
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == authoritative
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "ORIG_HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == base


def test_session_deadline_only_limits_tab_create_command() -> None:
    runner = FakeRunner([completed({"tabId": "tab-123"})])
    cli = client(runner, command_timeout_seconds=30)
    deadline_request = CreateSessionRequest(
        worker="codex",
        cwd="/workspace/project",
        command="codex",
        deadline_check=lambda: 0.5,
    )

    assert cli.create_session(deadline_request) == "tab-123"
    create_index = next(
        index
        for index, call in enumerate(runner.calls)
        if call[1:3] == ["tab", "create"]
    )
    assert runner.timeouts[create_index] == 0.5
    assert cli.command_timeout_seconds == 30
    assert runner.timeouts[create_index + 1] == 30


def test_codex_project_is_trusted_before_tab_creation() -> None:
    events: list[str] = []
    runner = FakeRunner([completed({"tabId": "tab-123"})])

    def trust(path: str) -> str:
        assert not any(call[1:3] == ["tab", "create"] for call in runner.calls)
        events.append(path)
        return path

    cli = client(runner, codex_project_truster=trust)
    cli.create_session(request())

    assert events == ["/workspace/project"]
    assert runner.calls[0][1:] == ["workspaces"]


def test_codex_trust_failure_prevents_tab_creation() -> None:
    runner = FakeRunner([])

    def fail(_path: str) -> str:
        raise WorkerFailure("trust unavailable")

    with pytest.raises(WorkerFailure, match="trust unavailable"):
        client(runner, codex_project_truster=fail).create_session(request())

    assert not any(call[1:3] == ["tab", "create"] for call in runner.calls)


def test_codex_request_cwd_must_equal_first_workspace_directory() -> None:
    runner = FakeRunner(
        [], workspace_directories=("/workspace/project", "/workspace/secondary")
    )
    trusted: list[str] = []
    cli = client(
        runner, codex_project_truster=lambda path: trusted.append(path) or path
    )

    unrelated = CreateSessionRequest(
        worker="codex", cwd="/workspace/secondary", command="codex"
    )

    with pytest.raises(WorkerFailure, match="does not match.*launch directory"):
        cli.create_session(unrelated)

    assert trusted == []
    assert not any(call[1:3] == ["tab", "create"] for call in runner.calls)


def test_runtime_client_refreshes_reordered_workspace_before_codex_launch() -> None:
    runner = FakeRunner(
        [completed({"tabId": "tab-123"})],
        workspace_directories=("/workspace/project", "/workspace/secondary"),
    )
    cli = PurpleMuxRuntime(runner=runner).workspace("ws-test")
    trusted: list[str] = []
    cli._codex_project_truster = lambda path: trusted.append(path) or path
    runner.workspace_directories = ("/workspace/secondary", "/workspace/project")

    session_id = cli.create_session(
        CreateSessionRequest(
            worker="codex", cwd="/workspace/secondary", command="codex"
        )
    )

    assert session_id == "tab-123"
    assert trusted == ["/workspace/secondary"]
    assert sum(call[1:] == ["workspaces"] for call in runner.calls) == 2


def test_direct_client_requires_selected_workspace_to_exist() -> None:
    runner = FakeRunner([], workspace_directories=None)

    with pytest.raises(WorkerFailure, match="was not found before Codex project trust"):
        client(runner).create_session(request())

    assert not any(call[1:3] == ["tab", "create"] for call in runner.calls)


def test_direct_client_rejects_workspace_without_directories() -> None:
    runner = FakeRunner([], workspace_directories=())

    with pytest.raises(WorkerFailure, match="has no directory"):
        client(runner).create_session(request())

    assert not any(call[1:3] == ["tab", "create"] for call in runner.calls)


def test_claude_project_is_trusted_before_tab_creation() -> None:
    codex_trusted: list[str] = []
    claude_trusted: list[str] = []
    runner = FakeRunner([completed({"tabId": "tab-claude"})])

    client(
        runner,
        codex_project_truster=lambda path: codex_trusted.append(path) or path,
        claude_project_truster=lambda path: claude_trusted.append(path) or path,
    ).create_session(request("claude-code", "claude"))

    assert codex_trusted == []
    assert claude_trusted == ["/workspace/project"]
    assert runner.calls[0][1:] == ["workspaces"]


def test_claude_trust_runs_in_a_purplemux_terminal_before_provider_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner([completed({"tabId": "tab-claude"})])
    cli = PurpleMuxCLIClient(
        "ws-test",
        poll_interval_seconds=0,
        runner=runner,
        sleep=lambda _: None,
        codex_project_truster=lambda path: path,
    )
    events: list[object] = []

    def start_shell(shell_request, *, on_created=None):  # type: ignore[no-untyped-def]
        events.append(shell_request)
        if on_created is not None:
            on_created("tab-trust", "/tmp/result.json")
        return "tab-trust"

    monkeypatch.setattr(cli, "start_shell", start_shell)
    monkeypatch.setattr(
        cli,
        "wait_for_shell_completion",
        lambda session_id, timeout_seconds: events.append(
            ("wait", session_id, timeout_seconds)
        ),
    )
    monkeypatch.setattr(
        cli, "read_shell_result", lambda session_id: client_module.ShellResult(0)
    )
    monkeypatch.setattr(
        cli, "close_session", lambda session_id: events.append(("close", session_id))
    )

    assert cli.create_session(request("claude-code", "claude")) == "tab-claude"

    shell_request = events[0]
    assert isinstance(shell_request, ShellCommandRequest)
    assert shell_request.cwd == "/workspace/project"
    assert "-I -m purplemux_client.claude_trust /workspace/project" in (
        shell_request.command
    )
    assert events[1:] == [
        ("wait", "tab-trust", cli.command_timeout_seconds),
        ("close", "tab-trust"),
    ]
    create_calls = [call for call in runner.calls if call[1:3] == ["tab", "create"]]
    assert len(create_calls) == 1
    assert create_calls[0][create_calls[0].index("-t") + 1] == "claude-code"


def test_claude_trust_failure_prevents_tab_creation() -> None:
    runner = FakeRunner([])

    def fail(_path: str) -> str:
        raise WorkerFailure("Claude trust unavailable")

    with pytest.raises(WorkerFailure, match="Claude trust unavailable"):
        client(runner, claude_project_truster=fail).create_session(
            request("claude-code", "claude")
        )

    assert not any(call[1:3] == ["tab", "create"] for call in runner.calls)


def test_named_session_derives_run_scoped_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RUN_IDENTITY_ENV, "runner-run-12")
    runner = FakeRunner([completed({"tabId": "tab-named"})])

    session = client(runner).create_session(
        CreateSessionRequest(
            "codex", "/workspace/project", "codex", name="Issue 89 implementer"
        )
    )

    assert session == "tab-named"
    create = next(call for call in runner.calls if call[1:3] == ["tab", "create"])
    name = create[create.index("-n") + 1]
    assert name.startswith("Issue 89 implementer [awm:Issue-89-implementer-")


@pytest.mark.parametrize(
    ("worker", "command"),
    [("claude-code", "claude"), ("unknown", "claude")],
)
def test_create_selects_claude_panel(worker: str, command: str) -> None:
    runner = FakeRunner([completed({"tabId": "tab-claude"})])

    client(runner).create_session(request(worker, command))

    create = next(call for call in runner.calls if call[1:3] == ["tab", "create"])
    assert create[-1] == "claude-code"


def test_create_rejects_unknown_provider() -> None:
    runner = FakeRunner([])

    with pytest.raises(WorkerFailure, match="unsupported PurpleMux worker"):
        client(runner).create_session(request("unknown"))

    assert runner.calls == []


def test_create_requires_tab_id() -> None:
    runner = FakeRunner([completed({"workspaceId": "ws-test"})])

    with pytest.raises(MutationOutcomeUnknown, match="unknown"):
        client(runner).create_session(request())


def test_start_shell_creates_named_terminal_and_sends_cwd_command(
    tmp_path,
) -> None:
    runner = FakeRunner(
        [
            completed({"tabId": "tab-shell"}),
            completed({"status": "sent"}),
            completed({"status": "closed"}),
        ]
    )
    cli = client(runner)

    session_id = cli.start_shell(
        ShellCommandRequest(
            command="printf 'hello world'",
            cwd=str(tmp_path),
            name="Run 12: tests",
        )
    )

    assert session_id == "tab-shell"
    create = next(call for call in runner.calls if call[1:3] == ["tab", "create"])
    assert create == [
        "purplemux",
        "tab",
        "create",
        "-w",
        "ws-test",
        "-n",
        create[6],
        "-t",
        "terminal",
    ]
    assert create[6].startswith("Run 12: tests [awm:Run-12-tests-")
    assert create[6].endswith("]")
    wrapper = next(call for call in runner.calls if call[1:3] == ["tab", "send"])[-1]
    assert f"cd -- {tmp_path}" in wrapper
    assert "bash -lc" in wrapper
    assert 'printf \'{"exitCode":%s}' in wrapper
    cli.close_session(session_id)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_managed_shell_result_captures_both_visible_streams(
    tmp_path: Path, exit_code: int
) -> None:
    runner = FakeRunner(
        [completed({"tabId": "tab-shell"}), completed({"status": "sent"})]
    )
    cli = client(runner)
    session_id = cli.start_shell(
        ShellCommandRequest(
            command=(
                "printf 'public stdout\\n'; "
                "printf 'public stderr\\n' >&2; "
                f"exit {exit_code}"
            ),
            cwd=str(tmp_path),
            name="Captured shell",
        )
    )
    wrapper = next(call for call in runner.calls if call[1:3] == ["tab", "send"])[-1]
    execution = subprocess.run(
        ["bash", "-c", wrapper], capture_output=True, text=True, timeout=5
    )
    assert execution.returncode == 0
    assert execution.stdout == "public stdout\n"
    assert execution.stderr == "public stderr\n"
    result = cli._read_shell_result_file(session_id)
    assert result is not None
    assert (result.exit_code, result.stdout, result.stderr) == (
        exit_code,
        execution.stdout,
        execution.stderr,
    )
    cli._cleanup_shell_result(cli._shell_runs[session_id])


def test_visible_managed_shell_keeps_stderr_separate(tmp_path: Path) -> None:
    runner = FakeRunner(
        [completed({"tabId": "tab-shell"}), completed({"status": "sent"})]
    )
    cli = client(runner)
    session_id = cli.start_shell(
        ShellCommandRequest(
            command="printf 'out\\n'; printf 'err\\n' >&2",
            cwd=str(tmp_path),
            name="Visible capture",
        )
    )
    wrapper = next(call for call in runner.calls if call[1:3] == ["tab", "send"])[-1]
    socket = f"awm-test-{os.getpid()}-{time.monotonic_ns()}"
    tmux = ["tmux", "-L", socket]
    subprocess.run(tmux + ["new-session", "-d", "-s", "managed-shell"], check=True)
    try:
        subprocess.run(
            tmux + ["send-keys", "-t", "managed-shell", wrapper, "Enter"], check=True
        )
        deadline = time.monotonic() + 3
        result = None
        while result is None:
            assert time.monotonic() < deadline
            result = cli._read_shell_result_file(session_id)
            time.sleep(0.02)
        assert (result.exit_code, result.stdout, result.stderr) == (
            0,
            "out\n",
            "err\n",
        )
    finally:
        subprocess.run(tmux + ["kill-server"], check=False, capture_output=True)
        cli._cleanup_shell_result(cli._shell_runs[session_id])


def test_managed_shell_capture_bounds_sidecars_before_result_read(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        [completed({"tabId": "tab-shell"}), completed({"status": "sent"})]
    )
    cli = client(runner)
    script = (
        'import sys; sys.stdout.write("é" * 100000 + "stdout"); '
        'sys.stderr.write("日" * 100000 + "stderr")'
    )
    session_id = cli.start_shell(
        ShellCommandRequest(
            command=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
            cwd=str(tmp_path),
            name="Bounded capture",
            max_output_chars=8,
        )
    )
    wrapper = next(call for call in runner.calls if call[1:3] == ["tab", "send"])[-1]
    execution = subprocess.run(
        ["bash", "-c", wrapper], capture_output=True, text=True, timeout=10
    )
    assert execution.returncode == 0
    assert execution.stdout == "é" * 100000 + "stdout"
    assert execution.stderr == "日" * 100000 + "stderr"
    result = cli._read_shell_result_file(session_id)
    assert result is not None
    assert result.stdout == "éééstdout"
    assert result.stderr == "日日日stderr"
    result_path = cli._shell_runs[session_id].result_path
    assert os.stat(f"{result_path}.stdout").st_size <= 9 * 4
    assert os.stat(f"{result_path}.stderr").st_size <= 9 * 4
    cli._cleanup_shell_result(cli._shell_runs[session_id])


def test_start_shell_bounds_tab_reads_create_and_send_by_deadline(tmp_path) -> None:
    runner = FakeRunner(
        [completed({"tabId": "tab-shell"}), completed({"status": "sent"})]
    )
    cli = client(runner)
    cli.start_shell(
        ShellCommandRequest(
            command="true",
            cwd=str(tmp_path),
            name="Bounded shell",
            deadline_check=lambda: 0.2,
        )
    )
    launch_calls = [
        (call[1:3], timeout) for call, timeout in zip(runner.calls, runner.timeouts)
    ]
    assert (["tab", "create"], 0.2) in launch_calls
    assert (["tab", "send"], 0.2) in launch_calls
    assert sum(command == ["tab", "list"] for command, _ in launch_calls) >= 2
    assert all(timeout <= 0.2 for _, timeout in launch_calls)


def test_run_ownership_is_opt_in_and_registers_shell_result_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registrations: list[tuple[str, str, dict[str, str]]] = []
    monkeypatch.setattr(
        client_module,
        "register_run_resource",
        lambda kind, identity, metadata: registrations.append(
            (kind, identity, metadata)
        ),
    )
    ordinary_runner = FakeRunner([completed({"tabId": "tab-prompt"})])
    client(ordinary_runner).create_session(request())
    assert registrations == []

    owned_runner = FakeRunner(
        [completed({"tabId": "tab-shell"}), completed({"status": "sent"})]
    )
    owned = client(owned_runner, owned_by_run=True)
    tab = owned.start_shell(ShellCommandRequest("true", str(tmp_path), "Owned shell"))

    assert [item[0] for item in registrations] == [
        "purplemux_tab",
        "managed_shell_result",
    ]
    result_directory = Path(registrations[1][1])
    assert registrations[1][2] == {
        "result_path": str(result_directory / "result.json"),
        "tab_id": tab,
        "directory_identity": _path_identity(result_directory),
    }
    owned._cleanup_shell_result(owned._shell_runs[tab])


def _path_identity(path: Path) -> str:
    state = path.stat(follow_symlinks=False)
    return f"{state.st_dev}:{state.st_ino}"


@pytest.mark.parametrize(
    "shell_request",
    [
        ShellCommandRequest(command="", cwd="/tmp", name="run"),
        ShellCommandRequest(command="true", cwd="/tmp", name="  "),
        ShellCommandRequest(command="true\0false", cwd="/tmp", name="run"),
    ],
)
def test_start_shell_rejects_invalid_request(shell_request) -> None:
    runner = FakeRunner([])

    with pytest.raises(ValueError):
        client(runner).start_shell(shell_request)

    assert runner.calls == []


def test_shell_start_timeout_reports_created_tab_for_reconciliation(tmp_path) -> None:
    runner = FakeRunner(
        [
            completed({"tabId": "tab-shell"}),
            subprocess.TimeoutExpired(["purplemux"], 30),
            completed({"status": "closed"}),
        ]
    )
    cli = client(runner)

    with pytest.raises(
        MutationOutcomeUnknown,
        match="shell terminal tab-shell was created.*unknown",
    ):
        cli.start_shell(
            ShellCommandRequest(command="make test", cwd=str(tmp_path), name="Run 5")
        )

    cli.close_session("tab-shell")


@pytest.mark.parametrize("output", ["not-json", "[]", '{"workspaceId":"ws-test"}'])
def test_shell_create_unusable_response_has_unknown_outcome(
    tmp_path, output: str
) -> None:
    runner = FakeRunner([completed(output)])

    with pytest.raises(MutationOutcomeUnknown, match="unknown"):
        client(runner).start_shell(
            ShellCommandRequest(command="make test", cwd=str(tmp_path), name="Run 5")
        )

    assert len([call for call in runner.calls if call[1:3] != ["tab", "list"]]) == 1


@pytest.mark.parametrize("output", ["not-json", "[]"])
def test_shell_send_unusable_response_preserves_created_tab_correlation(
    tmp_path, output: str
) -> None:
    runner = FakeRunner([completed({"tabId": "tab-shell"}), completed(output)])
    cli = client(runner)

    with pytest.raises(
        MutationOutcomeUnknown,
        match="shell terminal tab-shell was created.*unknown",
    ):
        cli.start_shell(
            ShellCommandRequest(command="make test", cwd=str(tmp_path), name="Run 5")
        )

    assert "tab-shell" in cli._shell_runs


def test_shell_completion_uses_structured_sidecar_not_screen_text(tmp_path) -> None:
    runner = FakeRunner(
        [
            completed({"tabId": "tab-shell"}),
            completed({"status": "sent"}),
            completed({"content": "expected branch main, got: feature/work"}),
            completed({"status": "closed"}),
        ]
    )
    cli = client(runner)
    session_id = cli.start_shell(
        ShellCommandRequest(command="exit 7", cwd=str(tmp_path), name="Run 4: check")
    )
    result_path = cli._shell_runs[session_id].result_path
    with pytest.raises(ResultNotReady, match="result is not ready"):
        cli.read_shell_result(session_id)
    with open(result_path, "w", encoding="utf-8") as stream:
        json.dump({"exitCode": 7}, stream)

    cli.wait_for_shell_completion(session_id, 1)

    result = cli.read_shell_result(session_id)
    assert result.exit_code == 7
    assert result.diagnostic_output == "expected branch main, got: feature/work"
    assert result.diagnostic_error is None
    assert result.cwd == str(tmp_path)
    assert result.workspace_id == "ws-test"
    assert result.tab_id == "tab-shell"
    assert result.failure_message("sync and verify main") == (
        "sync and verify main failed (exit code 7)\n"
        f"cwd: {tmp_path}\n"
        "workspace/tab: ws-test / tab-shell\n"
        "expected branch main, got: feature/work"
    )
    assert cli.read_shell_result(session_id) is result
    assert len([call for call in runner.calls if call[1:3] != ["tab", "list"]]) == 3
    assert os.path.exists(result_path)

    cli.close_session(session_id)
    assert not os.path.exists(os.path.dirname(result_path))


def test_shell_commands_can_complete_independently(tmp_path) -> None:
    runner = FakeRunner(
        [
            completed({"tabId": "tab-a"}),
            completed({"status": "sent"}),
            completed({"tabId": "tab-b"}),
            completed({"status": "sent"}),
            completed({"content": "task-a failed"}),
        ]
    )
    cli = client(runner)
    tab_a = cli.start_shell(
        ShellCommandRequest(command="task-a", cwd=str(tmp_path), name="Run A")
    )
    tab_b = cli.start_shell(
        ShellCommandRequest(command="task-b", cwd=str(tmp_path), name="Run B")
    )
    for tab, exit_code in ((tab_b, 0), (tab_a, 3)):
        with open(cli._shell_runs[tab].result_path, "w", encoding="utf-8") as stream:
            json.dump({"exitCode": exit_code}, stream)
        cli.wait_for_shell_completion(tab, 1)

    assert cli.read_shell_result(tab_a).exit_code == 3
    assert cli.read_shell_result(tab_b).exit_code == 0


def test_shell_wait_uses_structured_terminal_status_for_timeout(tmp_path) -> None:
    runner = FakeRunner(
        [
            completed({"tabId": "tab-shell"}),
            completed({"status": "sent"}),
            completed(
                {
                    "panelType": "terminal",
                    "terminalStatus": "running",
                    "alive": True,
                }
            ),
            completed({"status": "closed"}),
        ]
    )
    cli = client(runner)
    session_id = cli.start_shell(
        ShellCommandRequest(command="sleep 5", cwd=str(tmp_path), name="Run 9")
    )

    with pytest.raises(WorkerFailure, match="last terminalStatus=running"):
        cli.wait_for_shell_completion(session_id, 0)
    cli.close_session(session_id)


def test_shell_wait_tolerates_optional_terminal_status_during_startup(
    tmp_path,
) -> None:
    runner = FakeRunner(
        [
            completed({"tabId": "tab-shell"}),
            completed({"status": "sent"}),
            completed(
                {
                    "panelType": "terminal",
                    "cliState": "inactive",
                    "alive": True,
                }
            ),
            completed({"status": "closed"}),
        ]
    )
    cli = client(runner)
    session_id = cli.start_shell(
        ShellCommandRequest(command="true", cwd=str(tmp_path), name="Run 10")
    )

    with pytest.raises(
        WorkerFailure,
        match="last terminalStatus=unavailable; cliState=inactive",
    ):
        cli.wait_for_shell_completion(session_id, 0)
    cli.close_session(session_id)


def test_failed_shell_terminal_is_retained_until_explicit_close(tmp_path) -> None:
    runner = FakeRunner(
        [
            completed({"tabId": "tab-shell"}),
            completed({"status": "sent"}),
            completed({"content": "lint failed"}),
            completed({"status": "closed"}),
        ]
    )
    cli = client(runner)
    session_id = cli.start_shell(
        ShellCommandRequest(command="false", cwd=str(tmp_path), name="Run 3: lint")
    )
    result_path = cli._shell_runs[session_id].result_path
    with open(result_path, "w", encoding="utf-8") as stream:
        json.dump({"exitCode": 1}, stream)
    cli.wait_for_shell_completion(session_id, 1)

    assert runner.calls[-1][2] == "capture"
    assert cli.read_shell_result(session_id).exit_code == 1

    cli.close_session(session_id)
    assert any(call[1:3] == ["tab", "close"] for call in runner.calls)


def test_failed_shell_diagnostic_is_bounded_by_lines_and_encoded_bytes(
    tmp_path,
) -> None:
    capture = "\n".join([*(f"old line {number}" for number in range(50)), "界" * 2_000])
    runner = FakeRunner(
        [
            completed({"tabId": "tab-shell"}),
            completed({"status": "sent"}),
            completed({"content": capture}),
        ]
    )
    cli = client(runner)
    session_id = cli.start_shell(
        ShellCommandRequest(command="false", cwd=str(tmp_path), name="checks")
    )
    with open(cli._shell_runs[session_id].result_path, "w", encoding="utf-8") as stream:
        json.dump({"exitCode": 2}, stream)

    result = cli.read_shell_result(session_id)

    assert result.exit_code == 2
    assert result.diagnostic_output is not None
    assert len(result.diagnostic_output.encode()) <= 2_500
    assert result.diagnostic_output.endswith("界" * 10)
    assert "old line 0" not in result.diagnostic_output


def test_capture_failure_is_secondary_to_structured_shell_failure(tmp_path) -> None:
    runner = FakeRunner(
        [
            completed({"tabId": "tab-shell"}),
            completed({"status": "sent"}),
            completed({}, returncode=2, stderr="capture unavailable"),
        ]
    )
    cli = client(runner)
    session_id = cli.start_shell(
        ShellCommandRequest(command="false", cwd=str(tmp_path), name="checks")
    )
    with open(cli._shell_runs[session_id].result_path, "w", encoding="utf-8") as stream:
        json.dump({"exitCode": 9}, stream)

    result = cli.read_shell_result(session_id)

    assert result.exit_code == 9
    assert result.diagnostic_output is None
    assert result.diagnostic_error is not None
    assert "capture unavailable" in result.diagnostic_error
    assert "failed (exit code 9)" in result.failure_message("checks")
    assert "diagnostic capture failed" in result.failure_message("checks")


def test_read_status_returns_structured_status() -> None:
    status = {
        "tabId": "tab-1",
        "cliState": "idle",
        "alive": True,
        "eventSeq": 3,
    }
    runner = FakeRunner([completed(status)])

    assert client(runner).read_status("tab-1") == status
    assert runner.calls[0][1:3] == ["tab", "status"]


def test_wait_until_ready_polls_until_idle() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "inactive", "alive": True}),
            completed({"cliState": "idle", "alive": True}),
        ]
    )

    client(runner).wait_until_ready("tab-1", 1)

    assert len(runner.calls) == 2


def test_wait_until_ready_accepts_ready_for_review() -> None:
    runner = FakeRunner([completed({"cliState": "ready-for-review", "alive": True})])

    client(runner).wait_until_ready("tab-1", 1)


def test_ready_timeout_reports_last_state() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "inactive", "alive": True}),
            completed({"content": "Trust this folder?"}),
        ]
    )

    with pytest.raises(
        SessionReadyTimeout,
        match=r"(?s)last cliState=inactive.*Trust this folder\?",
    ):
        client(runner).wait_until_ready("tab-1", 0)

    assert runner.calls[-1][2] == "capture"


def test_ready_timeout_reports_capture_failure_without_masking_timeout() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "inactive", "alive": True}),
            completed({}, returncode=2, stderr="capture unavailable"),
        ]
    )

    with pytest.raises(
        SessionReadyTimeout,
        match="PurpleMux capture failed.*capture unavailable",
    ):
        client(runner).wait_until_ready("tab-1", 0)


def test_send_records_baseline_then_uses_public_cli() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "do work")

    assert [call[2] for call in runner.calls] == ["status", "result", "send"]
    assert runner.calls[-1][-1] == "do work"


def test_send_rejects_empty_input_without_running_cli() -> None:
    runner = FakeRunner([])

    with pytest.raises(ValueError, match="must not be empty"):
        client(runner).send_input("tab-1", "")


def test_wait_for_completion_correlates_busy_ready_and_result() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": 200,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "done",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "done"


def test_busy_turn_timeout_warns_once_and_continues_until_completion() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": 200,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "eventually done",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    warnings: list[str] = []
    times = iter([0.0, 1.0, 2.0])
    cli = client(runner, monotonic=lambda: next(times))

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1, on_busy_timeout=warnings.append)

    assert cli.read_result("tab-1") == "eventually done"
    assert len(warnings) == 1
    assert "while still busy; continuing to monitor" in warnings[0]


def test_busy_turn_timeout_emits_structured_warning_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": 200,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "done",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    findings: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        client_module,
        "emit_finding",
        lambda category, message, *, status: findings.append(
            (category, message, status)
        ),
    )
    times = iter([0.0, 1.0, 2.0])
    cli = client(runner, monotonic=lambda: next(times))

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert findings == [
        (
            "runtime",
            "session tab-1 exceeded the agent turn timeout of 1s while still busy; "
            "continuing to monitor while the session remains busy",
            "warning",
        )
    ]


def test_authoritative_failure_after_busy_timeout_still_fails() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed({"cliState": "dead", "alive": False, "eventSeq": 3}),
        ]
    )
    warnings: list[str] = []
    times = iter([0.0, 1.0])
    cli = client(runner, monotonic=lambda: next(times))

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerFailure, match="entered dead"):
        cli.wait_for_turn_completion("tab-1", 1, on_busy_timeout=warnings.append)

    assert len(warnings) == 1


def test_idle_with_stale_result_after_busy_timeout_fails_after_grace() -> None:
    stale_idle = {
        "cliState": "idle",
        "alive": True,
        "eventSeq": 1,
        "lastEvent": {"name": "stop", "seq": 1},
    }
    stale_result = {
        "status": "completed",
        "text": "stale",
        "completionTimestamp": 1,
    }
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(stale_idle),
            completed(stale_result),
            completed(stale_idle),
            completed(stale_result),
        ]
    )
    times = iter([0.0, 1.0, 2.0, 3.0, 32.0])
    cli = client(runner, monotonic=lambda: next(times))

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerFailure, match="within 30s after leaving busy"):
        cli.wait_for_turn_completion("tab-1", 1, on_busy_timeout=lambda _: None)


def test_ready_result_lag_after_busy_timeout_eventually_completes() -> None:
    ready = {
        "cliState": "ready-for-review",
        "alive": True,
        "eventSeq": 3,
        "readyForReviewAt": 200,
        "lastEvent": {"name": "stop", "seq": 3},
    }
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(ready),
            completed({"status": "not-ready", "completionTimestamp": None}),
            completed(ready),
            completed(
                {
                    "status": "completed",
                    "text": "published after lag",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    times = iter([0.0, 1.0, 2.0, 3.0])
    cli = client(runner, monotonic=lambda: next(times))

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1, on_busy_timeout=lambda _: None)

    assert cli.read_result("tab-1") == "published after lag"


@pytest.mark.parametrize("transient_state", ["ready-for-review", "idle"])
def test_post_timeout_return_to_busy_cancels_result_grace(
    transient_state: str,
) -> None:
    transient = {
        "cliState": transient_state,
        "alive": True,
        "eventSeq": 3,
        "readyForReviewAt": 200 if transient_state == "ready-for-review" else None,
        "lastEvent": {"name": "stop", "seq": 3},
    }
    completed_status = {
        "cliState": "ready-for-review",
        "alive": True,
        "eventSeq": 5,
        "readyForReviewAt": 300,
        "lastEvent": {"name": "stop", "seq": 5},
    }
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(transient),
            completed({"status": "not-ready", "completionTimestamp": None}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 4}),
            completed(completed_status),
            completed(
                {
                    "status": "completed",
                    "text": "completed after returning to busy",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    warnings: list[str] = []
    # The final non-busy observation is beyond the first grace deadline. It must
    # start a new grace period because an authoritative busy state intervened.
    times = iter([0.0, 1.0, 2.0, 3.0, 40.0])
    cli = client(runner, monotonic=lambda: next(times))

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1, on_busy_timeout=warnings.append)

    assert cli.read_result("tab-1") == "completed after returning to busy"
    assert len(warnings) == 1


def test_ready_result_lag_after_busy_timeout_fails_when_grace_expires() -> None:
    ready = {
        "cliState": "ready-for-review",
        "alive": True,
        "eventSeq": 3,
        "readyForReviewAt": 200,
        "lastEvent": {"name": "stop", "seq": 3},
    }
    not_ready = {"status": "not-ready", "completionTimestamp": None}
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(ready),
            completed(not_ready),
            completed(ready),
            completed(not_ready),
        ]
    )
    times = iter([0.0, 1.0, 2.0, 3.0, 32.0])
    cli = client(runner, monotonic=lambda: next(times))

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerFailure, match="within 30s after leaving busy"):
        cli.wait_for_turn_completion("tab-1", 1, on_busy_timeout=lambda _: None)


def test_stale_ready_state_is_rejected_until_fresh_event_and_result() -> None:
    runner = FakeRunner(
        [
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 10,
                    "readyForReviewAt": 100,
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 1,
                }
            ),
            completed({"status": "sent"}),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 10,
                    "readyForReviewAt": 100,
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 1,
                }
            ),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 12,
                    "readyForReviewAt": 200,
                    "lastEvent": {"name": "stop", "seq": 12},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "new",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "new"


def test_fresh_result_handles_short_turn_when_busy_event_is_missed() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "ready-for-review", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "fast",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "fast"


def test_idle_accepts_fresh_completion_after_ready_state_is_dismissed() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": None,
                    "dismissedAt": 300,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "dismissed",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "dismissed"


def test_idle_accepts_fresh_completion_when_busy_poll_is_missed() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": None,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "fast dismissed",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "fast dismissed"


def test_send_rejects_unavailable_baseline_event_sequence() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True}),
        ]
    )
    cli = client(runner)

    with pytest.raises(WorkerFailure, match="no event sequence"):
        cli.send_input("tab-1", "work")

    assert [call[2] for call in runner.calls] == ["status"]


def test_idle_rejects_stale_stop_event_and_result() -> None:
    runner = FakeRunner(
        [
            *baseline(
                event_seq=3,
                result_status="completed",
                text="old",
                completion_timestamp=10,
            ),
            completed({"status": "sent"}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": None,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerFailure, match="did not complete"):
        cli.wait_for_turn_completion("tab-1", 0)

    assert [call[2] for call in runner.calls] == ["status", "result", "send", "status"]


def test_idle_uses_baseline_last_event_sequence_to_reject_stale_stop() -> None:
    runner = FakeRunner(
        [
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 10,
                }
            ),
            completed({"status": "sent"}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "readyForReviewAt": None,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerFailure, match="did not complete"):
        cli.wait_for_turn_completion("tab-1", 0)

    assert [call[2] for call in runner.calls] == ["status", "result", "send", "status"]


def test_idle_waits_for_completed_result_after_fresh_stop_event() -> None:
    stopped = {
        "cliState": "idle",
        "alive": True,
        "eventSeq": 3,
        "readyForReviewAt": None,
        "lastEvent": {"name": "stop", "seq": 3},
    }
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed(stopped),
            completed({"status": "not-ready", "completionTimestamp": None}),
            completed(stopped),
            completed(
                {
                    "status": "completed",
                    "text": "published",
                    "completionTimestamp": 2,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "published"


def test_idle_fresh_stop_with_interrupted_result_is_explicit() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "eventSeq": 3,
                    "readyForReviewAt": None,
                    "lastEvent": {"name": "stop", "seq": 3},
                }
            ),
            completed(
                {
                    "status": "interrupted",
                    "reason": "turn-interrupted",
                    "interrupted": True,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerInterrupted, match="interrupted"):
        cli.wait_for_turn_completion("tab-1", 1)


def test_read_result_rejects_stale_result_for_pending_turn() -> None:
    runner = FakeRunner(
        [
            *baseline(
                state="ready-for-review",
                result_status="completed",
                text="old",
                completion_timestamp=10,
            ),
            completed({"status": "sent"}),
            completed(
                {
                    "status": "completed",
                    "text": "old",
                    "completionTimestamp": 10,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(ResultNotReady, match="stale"):
        cli.read_result("tab-1")


def test_older_completion_timestamp_is_not_fresh() -> None:
    runner = FakeRunner(
        [
            *baseline(
                state="ready-for-review",
                result_status="completed",
                text="baseline",
                completion_timestamp=2,
            ),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True}),
            completed({"cliState": "ready-for-review", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "older",
                    "completionTimestamp": 1,
                }
            ),
            completed({"cliState": "ready-for-review", "alive": True}),
            completed(
                {
                    "status": "completed",
                    "text": "fresh",
                    "completionTimestamp": 3,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "fresh"


def test_wait_requires_pending_input() -> None:
    runner = FakeRunner([])

    with pytest.raises(WorkerFailure, match="no pending input"):
        client(runner).wait_for_turn_completion("tab-1", 1)


def test_wait_raises_for_needs_input() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "needs-input", "alive": True}),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerNeedsInput, match="needs input") as raised:
        cli.wait_for_turn_completion("tab-1", 1)
    assert isinstance(raised.value, TerminalSessionError)
    assert not isinstance(raised.value, WorkerFailure)


def test_wait_raises_when_agent_becomes_inactive() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True}),
            completed({"cliState": "inactive", "alive": True}),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerFailure, match="became inactive"):
        cli.wait_for_turn_completion("tab-1", 1)


def test_fresh_interrupt_event_is_explicit() -> None:
    runner = FakeRunner(
        [
            *baseline(),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 2}),
            completed(
                {
                    "cliState": "idle",
                    "alive": True,
                    "eventSeq": 3,
                    "lastEvent": {"name": "interrupt", "seq": 3},
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "work")
    with pytest.raises(WorkerInterrupted, match="interrupted"):
        cli.wait_for_turn_completion("tab-1", 1)


def test_stale_interrupt_result_does_not_poison_reused_session() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 3}),
            completed(
                {
                    "status": "interrupted",
                    "reason": "turn-interrupted",
                    "interrupted": True,
                }
            ),
            completed({"status": "sent"}),
            completed({"cliState": "busy", "alive": True, "eventSeq": 4}),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 5,
                    "readyForReviewAt": 200,
                    "lastEvent": {"name": "stop", "seq": 5},
                }
            ),
            completed(
                {
                    "status": "interrupted",
                    "reason": "turn-interrupted",
                    "interrupted": True,
                }
            ),
            completed(
                {
                    "cliState": "ready-for-review",
                    "alive": True,
                    "eventSeq": 5,
                    "readyForReviewAt": 200,
                    "lastEvent": {"name": "stop", "seq": 5},
                }
            ),
            completed(
                {
                    "status": "completed",
                    "text": "reused",
                    "completionTimestamp": 20,
                    "interrupted": False,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "second turn")
    cli.wait_for_turn_completion("tab-1", 1)

    assert cli.read_result("tab-1") == "reused"


def test_read_result_rejects_stale_interrupt_for_reused_session() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "idle", "alive": True, "eventSeq": 3}),
            completed(
                {
                    "status": "interrupted",
                    "reason": "turn-interrupted",
                    "interrupted": True,
                }
            ),
            completed({"status": "sent"}),
            completed(
                {
                    "status": "interrupted",
                    "reason": "turn-interrupted",
                    "interrupted": True,
                }
            ),
        ]
    )
    cli = client(runner)

    cli.send_input("tab-1", "second turn")
    with pytest.raises(ResultNotReady, match="stale"):
        cli.read_result("tab-1")


def test_interrupted_result_is_explicit() -> None:
    runner = FakeRunner(
        [completed({"status": "interrupted", "reason": "turn-interrupted"})]
    )

    with pytest.raises(WorkerInterrupted, match="turn-interrupted"):
        client(runner).read_result("tab-1")


def test_read_result_returns_structured_text() -> None:
    runner = FakeRunner(
        [
            completed(
                {
                    "status": "completed",
                    "text": "structured output",
                    "completionTimestamp": 10,
                }
            )
        ]
    )

    assert client(runner).read_result("tab-1") == "structured output"


def test_not_ready_result_is_explicit() -> None:
    runner = FakeRunner(
        [completed({"status": "not-ready", "reason": "jsonl-unavailable"})]
    )

    with pytest.raises(ResultNotReady, match="jsonl-unavailable"):
        client(runner).read_result("tab-1")


@pytest.mark.parametrize("status", ["not-applicable", "unavailable"])
def test_unavailable_result_statuses_fail(status: str) -> None:
    runner = FakeRunner([completed({"status": status, "reason": "reason"})])

    with pytest.raises(WorkerFailure, match=status):
        client(runner).read_result("tab-1")


@pytest.mark.parametrize(
    "state", ["cancelled", "dead", "error", "failed", "stopped", "exited"]
)
def test_failed_runtime_states_are_explicit(state: str) -> None:
    runner = FakeRunner([completed({"cliState": state, "alive": True})])

    with pytest.raises(WorkerFailure, match=f"entered {state}"):
        client(runner).wait_until_ready("tab-1", 1)


def test_dead_runtime_is_not_ready() -> None:
    runner = FakeRunner([completed({"cliState": "idle", "alive": False})])

    with pytest.raises(WorkerFailure, match="entered idle"):
        client(runner).wait_until_ready("tab-1", 1)


def test_interrupt_uses_public_cli() -> None:
    runner = FakeRunner(
        [
            completed({"cliState": "busy", "alive": True, "eventSeq": 1}),
            completed({"status": "interrupted"}),
        ]
    )

    client(runner).interrupt("tab-1")

    assert runner.calls[-1][1:] == [
        "tab",
        "interrupt",
        "-w",
        "ws-test",
        "tab-1",
    ]


def test_close_uses_public_cli_and_accepts_plain_ok() -> None:
    runner = FakeRunner([completed("ok\n")])
    runner.tabs["tab-1"] = {
        "tabId": "tab-1",
        "workspaceId": "ws-test",
        "name": "session",
        "panelType": "codex-cli",
        "agentProviderId": "codex",
    }

    client(runner).close_session("tab-1")

    assert any(
        call[1:] == ["tab", "close", "-w", "ws-test", "tab-1"] for call in runner.calls
    )


def test_capture_screen_is_diagnostic_and_separate_from_result() -> None:
    runner = FakeRunner([completed({"content": "diagnostic pane"})])

    assert client(runner).capture_screen("tab-1") == "diagnostic pane"
    assert runner.calls[0][2] == "capture"


def test_cli_non_zero_exit_includes_stderr() -> None:
    runner = FakeRunner([completed({}, returncode=2, stderr="server unavailable")])

    with pytest.raises(WorkerFailure, match="server unavailable"):
        client(runner).read_result("tab-1")


@pytest.mark.parametrize("output", ["not-json", "[]"])
def test_malformed_or_non_object_json(output: str) -> None:
    runner = FakeRunner([completed(output)])

    with pytest.raises(WorkerFailure, match="malformed JSON|non-object JSON"):
        client(runner).read_result("tab-1")


def test_os_error_is_wrapped() -> None:
    runner = FakeRunner([OSError("purplemux missing")])

    with pytest.raises(WorkerFailure, match="could not execute"):
        client(runner).read_status("tab-1")


@pytest.mark.parametrize("operation", ["status", "result", "capture"])
def test_read_timeout_is_retried(operation: str) -> None:
    timeout = subprocess.TimeoutExpired(["purplemux"], 2)
    response = {
        "status": {"cliState": "idle", "alive": True},
        "result": {"status": "completed", "text": "done"},
        "capture": {"content": "diagnostic"},
    }[operation]
    runner = FakeRunner([timeout, completed(response)])
    cli = client(runner)

    if operation == "status":
        assert cli.read_status("tab-1")["cliState"] == "idle"
    elif operation == "result":
        assert cli.read_result("tab-1") == "done"
    else:
        assert cli.capture_screen("tab-1") == "diagnostic"

    assert len(runner.calls) == 2


def test_read_timeout_fails_after_configured_retries() -> None:
    timeout = subprocess.TimeoutExpired(["purplemux"], 2)
    runner = FakeRunner([timeout, timeout])

    with pytest.raises(WorkerFailure, match="status timed out"):
        client(runner).read_status("tab-1")

    assert len(runner.calls) == 2


@pytest.mark.parametrize("operation", ["create", "send", "interrupt", "close"])
def test_mutation_timeout_is_not_retried(operation: str) -> None:
    timeout = subprocess.TimeoutExpired(["purplemux"], 2)
    outcomes: list[
        subprocess.CompletedProcess[str] | subprocess.TimeoutExpired | OSError
    ]
    if operation == "send":
        outcomes = [*baseline(), timeout]
    else:
        outcomes = [timeout]
    runner = FakeRunner(outcomes)
    cli = client(runner)

    if operation == "interrupt":
        runner.outcomes.insert(
            0, completed({"cliState": "busy", "alive": True, "eventSeq": 1})
        )
        runner.outcomes.extend(
            [
                completed({"cliState": "busy", "alive": True, "eventSeq": 1}),
                completed({"cliState": "busy", "alive": True, "eventSeq": 1}),
            ]
        )
    if operation == "close":
        runner.tabs["tab-1"] = {
            "tabId": "tab-1",
            "workspaceId": "ws-test",
            "name": "session",
            "panelType": "codex-cli",
            "agentProviderId": "codex",
        }

    with pytest.raises(MutationOutcomeUnknown, match="unknown"):
        if operation == "create":
            cli.create_session(request())
        elif operation == "send":
            cli.send_input("tab-1", "work")
        elif operation == "interrupt":
            cli.interrupt("tab-1")
        else:
            cli.close_session("tab-1")

    mutation_calls = [
        call for call in runner.calls if len(call) > 2 and call[2] == operation
    ]
    assert len(mutation_calls) == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"workspace_id": ""}, "workspace_id"),
        ({"poll_interval_seconds": -1}, "poll_interval_seconds"),
        ({"command_timeout_seconds": 0}, "command_timeout_seconds"),
        ({"read_timeout_retries": -1}, "read_timeout_retries"),
    ],
)
def test_configuration_validation(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {"workspace_id": "ws-test"}
    base.update(kwargs)

    with pytest.raises(ValueError, match=message):
        PurpleMuxCLIClient(**base)
