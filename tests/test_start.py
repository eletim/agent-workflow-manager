from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.fixture
def start_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy2(REPO_ROOT / "start.sh", repo / "start.sh")
    shutil.copy2(REPO_ROOT / "sample_config.sh", repo / "sample_config.sh")
    return repo


def install_commands(directory: Path, *commands: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for command in commands:
        content = "#!/bin/sh\nexit 0\n"
        if command == "uv":
            content = '#!/bin/sh\nprintf \'%s\\n\' "$PATH" > "$START_PATH_REPORT"\nprintf \'%s\\n\' "$@" > "$START_ARGS_REPORT"\n'
        write_executable(directory / command, content)


def run_start(
    repo: Path,
    tmp_path: Path,
    *,
    input_text: str = "",
    home_commands: tuple[str, ...] = ("purplemux", "gh", "uv"),
    extra_path: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    install_commands(local_bin, *home_commands)
    core_bin = tmp_path / "core-bin"
    core_bin.mkdir(exist_ok=True)
    cat_command = core_bin / "cat"
    if not cat_command.exists():
        cat_command.symlink_to("/bin/cat")
    env = {
        "HOME": str(home),
        "PATH": str(core_bin),
        "START_PATH_REPORT": str(tmp_path / "path-report"),
        "START_ARGS_REPORT": str(tmp_path / "args-report"),
    }
    if extra_path is not None:
        env["TEST_EXTRA_PATH"] = str(extra_path)
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        ["/bin/bash", str(repo / "start.sh")],
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
    )


def config(repo: Path, host: str, port: str, extra_path: str = "") -> None:
    (repo / "config.sh").write_text(
        f"AGENT_WORKFLOW_MANAGER_HOST={shlex.quote(host)}\n"
        f"AGENT_WORKFLOW_MANAGER_PORT={shlex.quote(port)}\n"
        f"AGENT_WORKFLOW_MANAGER_PATH={shlex.quote(extra_path)}\n"
    )


def test_missing_config_prompts_and_saves_defaults(
    start_repo: Path, tmp_path: Path
) -> None:
    result = run_start(start_repo, tmp_path, input_text="\n\n")

    assert result.returncode == 0
    assert "Bind host [127.0.0.1]:" in result.stderr
    assert "Port [8765]:" in result.stderr
    saved = (start_repo / "config.sh").read_text()
    assert "AGENT_WORKFLOW_MANAGER_HOST=127.0.0.1" in saved
    assert "AGENT_WORKFLOW_MANAGER_PORT=8765" in saved


@pytest.mark.parametrize(
    ("existing_host", "existing_port", "input_text", "expected_prompt"),
    [
        ("", "9000", "100.64.1.2\n", "Bind host [127.0.0.1]:"),
        ("127.0.0.1", "", "9001\n", "Port [8765]:"),
    ],
)
def test_only_empty_config_value_is_prompted(
    start_repo: Path,
    tmp_path: Path,
    existing_host: str,
    existing_port: str,
    input_text: str,
    expected_prompt: str,
) -> None:
    config(start_repo, existing_host, existing_port)

    result = run_start(start_repo, tmp_path, input_text=input_text)

    assert result.returncode == 0
    assert result.stderr.count(": ") == 1
    assert expected_prompt in result.stderr


def test_complete_config_starts_noninteractively_and_passes_runner_args(
    start_repo: Path, tmp_path: Path
) -> None:
    config(start_repo, "100.64.1.2", "9000")

    result = run_start(start_repo, tmp_path)

    assert result.returncode == 0
    assert result.stderr == ""
    assert "URL:  http://100.64.1.2:9000" in result.stdout
    assert (tmp_path / "args-report").read_text().splitlines() == [
        "run",
        "python",
        "-m",
        "purplemux_client.web",
        "--host",
        "100.64.1.2",
        "--port",
        "9000",
    ]


@pytest.mark.parametrize("port", ["0", "65536", "abc", "12.5", "-1"])
def test_invalid_port_stops_before_runner(
    start_repo: Path, tmp_path: Path, port: str
) -> None:
    config(start_repo, "127.0.0.1", port)

    result = run_start(start_repo, tmp_path)

    assert result.returncode != 0
    assert "must be an integer from 1 to 65535" in result.stderr
    assert not (tmp_path / "args-report").exists()


def test_user_local_bin_is_added_and_inherited_by_runner(
    start_repo: Path, tmp_path: Path
) -> None:
    config(start_repo, "127.0.0.1", "8765")

    result = run_start(start_repo, tmp_path)

    assert result.returncode == 0
    child_path = (tmp_path / "path-report").read_text().strip().split(":")
    assert child_path[0] == str(tmp_path / "home" / ".local" / "bin")
    assert child_path[-1] == str(tmp_path / "core-bin")


def test_optional_extra_path_is_prepended_and_used_for_dependencies(
    start_repo: Path, tmp_path: Path
) -> None:
    extra_bin = tmp_path / "extra bin"
    install_commands(extra_bin, "purplemux", "gh", "uv")
    config(start_repo, "127.0.0.1", "8765", str(extra_bin))

    result = run_start(start_repo, tmp_path, home_commands=())

    assert result.returncode == 0
    assert (tmp_path / "path-report").read_text().split(":")[0] == str(extra_bin)


@pytest.mark.parametrize(
    ("commands", "message", "guide_fragments"),
    [
        (
            ("gh", "uv"),
            "ERROR: purplemux command was not found.",
            ("node bin/cli.js --help", "command -v purplemux", "does not clone"),
        ),
        (
            ("purplemux", "uv"),
            "ERROR: GitHub CLI (gh) was not found.",
            ("pull-request and issue operations", "gh auth status", "command -v gh"),
        ),
        (
            ("purplemux", "gh"),
            "ERROR: uv was not found.",
            (
                "project Python environment",
                "uv --version",
                "Python tools or packages automatically",
            ),
        ),
    ],
)
def test_missing_dependency_has_detailed_guide_and_does_not_start_runner(
    start_repo: Path,
    tmp_path: Path,
    commands: tuple[str, ...],
    message: str,
    guide_fragments: tuple[str, ...],
) -> None:
    config(start_repo, "127.0.0.1", "8765")

    result = run_start(start_repo, tmp_path, home_commands=commands)

    assert result.returncode != 0
    assert message in result.stderr
    assert all(fragment in result.stderr for fragment in guide_fragments)
    assert "./start.sh" in result.stderr
    assert not (tmp_path / "args-report").exists()
    if "purplemux" not in commands:
        assert not (tmp_path / "home" / ".local" / "bin" / "purplemux").exists()


def test_shell_quoting_round_trips_config_values(
    start_repo: Path, tmp_path: Path
) -> None:
    result = run_start(start_repo, tmp_path, input_text="vpn host;echo nope\n8765\n")
    assert result.returncode == 0

    first_config = (start_repo / "config.sh").read_text()
    second = run_start(start_repo, tmp_path)

    assert second.returncode == 0
    assert (start_repo / "config.sh").read_text() == first_config
    assert "--host\nvpn host;echo nope\n" in (tmp_path / "args-report").read_text()


def test_malformed_config_stops_with_recovery_message(
    start_repo: Path, tmp_path: Path
) -> None:
    (start_repo / "config.sh").write_text("return 7\n")

    result = run_start(start_repo, tmp_path)

    assert result.returncode != 0
    assert "failed to load" in result.stderr
    assert "Fix the shell syntax" in result.stderr
    assert not (tmp_path / "args-report").exists()


def test_runner_child_can_resolve_workflow_commands(
    start_repo: Path, tmp_path: Path
) -> None:
    config(start_repo, "127.0.0.1", "8765")
    local_bin = tmp_path / "home" / ".local" / "bin"
    install_commands(local_bin, "purplemux", "gh")
    write_executable(
        local_bin / "python",
        '#!/bin/sh\nexec /usr/bin/python3 "$@"\n',
    )
    write_executable(
        local_bin / "uv",
        '#!/bin/sh\nshift\nexec "$@"\n',
    )
    package = tmp_path / "pythonpath" / "purplemux_client"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "web.py").write_text(
        "import shutil\n"
        "assert shutil.which('purplemux') is not None\n"
        "assert shutil.which('gh') is not None\n"
        "print('child dependencies visible')\n"
    )
    result = run_start(
        start_repo,
        tmp_path,
        home_commands=(),
        extra_env={"PYTHONPATH": str(package.parent)},
    )

    assert result.returncode == 0
    assert "child dependencies visible" in result.stdout
