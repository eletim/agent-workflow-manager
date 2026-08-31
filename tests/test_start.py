from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]


def _executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _write_runtime_config(
    path: Path,
    *,
    host: str = "127.0.0.1",
    port: str = "8765",
    notifications: str = "auto",
    notify_success: str = "true",
    notify_failure: str = "true",
    notify_stopped: str = "false",
    notify_config: Path,
) -> None:
    path.write_text(
        "\n".join(
            (
                f"AGENT_WORKFLOW_MANAGER_HOST={shlex.quote(host)}",
                f"AGENT_WORKFLOW_MANAGER_PORT={shlex.quote(port)}",
                f"AGENT_WORKFLOW_MANAGER_NOTIFICATIONS={shlex.quote(notifications)}",
                f"AGENT_WORKFLOW_MANAGER_NOTIFY_SUCCESS={shlex.quote(notify_success)}",
                f"AGENT_WORKFLOW_MANAGER_NOTIFY_FAILURE={shlex.quote(notify_failure)}",
                f"AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED={shlex.quote(notify_stopped)}",
                f"NOTIFY_CONFIG={shlex.quote(str(notify_config))}",
                "",
            )
        ),
        encoding="utf-8",
    )


def _start_environment(
    tmp_path: Path,
    *,
    create_config: bool = True,
    with_notify: bool = True,
) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    home = tmp_path / "home"
    home.mkdir()
    config_file = tmp_path / "config.sh"
    _executable(
        fake_bin / "uv",
        'printf \'uv %s\\n\' "$*" >>"$START_CALL_LOG"\n',
    )
    _executable(fake_bin / "purplemux", "exit 0\n")
    _executable(fake_bin / "tailscale", "exit 1\n")
    if with_notify:
        _executable(fake_bin / "notify", "exit 0\n")
    environment = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "START_CALL_LOG": str(call_log),
        "AGENT_WORKFLOW_MANAGER_CONFIG_FILE": str(config_file),
    }
    if create_config:
        _write_runtime_config(
            config_file,
            notifications="disabled",
            notify_config=home / ".config/notify/config",
        )
    return environment, call_log, config_file


def _run_start(
    environment: dict[str, str],
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "start.sh"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        input=input_text,
        stdin=subprocess.DEVNULL if input_text is None else None,
        check=False,
        capture_output=True,
        text=True,
    )


def test_start_syncs_and_launches_runner_with_notify_disabled(
    tmp_path: Path,
) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    completed = _run_start(environment)

    assert completed.returncode == 0
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "uv sync --locked"
    assert calls[1].startswith(
        "uv run python -m purplemux_client.web --host 127.0.0.1 --port 8765"
    )
    assert (
        f"--runtime-config {environment['AGENT_WORKFLOW_MANAGER_CONFIG_FILE']}"
        in calls[1]
    )
    assert (
        f"--notify-config {Path(environment['HOME']) / '.config/notify/config'}"
        in calls[1]
    )
    assert "notifications disabled" in completed.stdout.lower()


def test_existing_config_skips_first_run_setup(tmp_path: Path) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    _executable(
        tmp_path / "bin" / "tailscale",
        'printf \'tailscale %s\\n\' "$*" >>"$START_CALL_LOG"\nexit 1\n',
    )
    completed = _run_start(environment)

    assert completed.returncode == 0
    assert "Tailscale IPv4" not in completed.stderr
    assert "tailscale " not in call_log.read_text(encoding="utf-8")


def test_start_exports_saved_terminal_notification_policy(tmp_path: Path) -> None:
    environment, _, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        notifications="disabled",
        notify_success="false",
        notify_failure="false",
        notify_stopped="true",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    _executable(
        tmp_path / "bin" / "uv",
        """
if [[ $1 == run ]]; then
    [[ $AGENT_WORKFLOW_MANAGER_NOTIFY_SUCCESS == false ]]
    [[ $AGENT_WORKFLOW_MANAGER_NOTIFY_FAILURE == false ]]
    [[ $AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED == true ]]
fi
printf 'uv %s\n' "$*" >>"$START_CALL_LOG"
""",
    )

    completed = _run_start(environment)

    assert completed.returncode == 0


def test_explicit_environment_values_override_existing_config(tmp_path: Path) -> None:
    environment, call_log, config_file = _start_environment(tmp_path)
    environment.update(
        {
            "AGENT_WORKFLOW_MANAGER_HOST": "100.70.80.91",
            "AGENT_WORKFLOW_MANAGER_PORT": "9000",
            "AGENT_WORKFLOW_MANAGER_NOTIFICATIONS": "disabled",
            "NOTIFY_CONFIG": str(tmp_path / "external-notify/config"),
        }
    )
    original_config = config_file.read_text(encoding="utf-8")
    completed = _run_start(environment)

    assert completed.returncode == 0
    assert "http://100.70.80.91:9000" in completed.stdout
    assert f"Notify config: {environment['NOTIFY_CONFIG']}" in completed.stdout
    assert (
        call_log.read_text(encoding="utf-8")
        .splitlines()[-1]
        .startswith(
            "uv run python -m purplemux_client.web --host 100.70.80.91 --port 9000"
        )
    )
    assert config_file.read_text(encoding="utf-8") == original_config


def test_missing_config_generates_persistent_runtime_config(tmp_path: Path) -> None:
    environment, _, config_file = _start_environment(tmp_path, create_config=False)
    completed = _run_start(environment, input_text="\n")

    assert completed.returncode == 0
    assert config_file.is_file()
    assert config_file.stat().st_mode & 0o777 == 0o600
    config = config_file.read_text(encoding="utf-8")
    assert "AGENT_WORKFLOW_MANAGER_HOST=127.0.0.1" in config
    assert "AGENT_WORKFLOW_MANAGER_PORT=8765" in config
    assert "AGENT_WORKFLOW_MANAGER_NOTIFICATIONS=auto" in config
    assert "AGENT_WORKFLOW_MANAGER_NOTIFY_SUCCESS=true" in config
    assert "AGENT_WORKFLOW_MANAGER_NOTIFY_FAILURE=true" in config
    assert "AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED=false" in config
    assert f"NOTIFY_CONFIG={environment['HOME']}/.config/notify/config" in config
    assert "Saved runtime configuration" in completed.stdout


def test_incomplete_first_run_is_retried_next_start(tmp_path: Path) -> None:
    environment, _, config_file = _start_environment(tmp_path, create_config=False)
    completed = _run_start(environment, input_text="n\n")

    assert completed.returncode != 0
    assert "setup input ended" in completed.stderr
    assert not config_file.exists()


def test_first_run_accepts_detected_tailscale_ipv4(tmp_path: Path) -> None:
    environment, call_log, config_file = _start_environment(
        tmp_path, create_config=False
    )
    _executable(
        tmp_path / "bin" / "tailscale",
        "printf 'tailscale %s\\n' \"$*\" >>\"$START_CALL_LOG\"\nprintf '100.70.80.90\\n'\n",
    )
    completed = _run_start(environment, input_text="\n")

    assert completed.returncode == 0
    assert "Tailscale IPv4 100.70.80.90 was detected." in completed.stderr
    assert "AGENT_WORKFLOW_MANAGER_HOST=100.70.80.90" in config_file.read_text(
        encoding="utf-8"
    )
    assert "http://100.70.80.90:8765" in completed.stdout
    assert "tailscale ip -4" in call_log.read_text(encoding="utf-8")


def test_first_run_declines_detected_tailscale_ipv4(tmp_path: Path) -> None:
    environment, _, config_file = _start_environment(tmp_path, create_config=False)
    _executable(tmp_path / "bin" / "tailscale", "printf '100.70.80.90\\n'\n")
    completed = _run_start(environment, input_text="n\n")

    assert completed.returncode == 0
    assert "AGENT_WORKFLOW_MANAGER_HOST=127.0.0.1" in config_file.read_text(
        encoding="utf-8"
    )


def test_first_run_without_tailscale_defaults_to_localhost(tmp_path: Path) -> None:
    environment, _, config_file = _start_environment(tmp_path, create_config=False)
    completed = _run_start(environment, input_text="\n")

    assert completed.returncode == 0
    assert "Tailscale IPv4 could not be detected." in completed.stderr
    assert "AGENT_WORKFLOW_MANAGER_HOST=127.0.0.1" in config_file.read_text(
        encoding="utf-8"
    )


def test_first_run_allows_manual_explicit_ipv4(tmp_path: Path) -> None:
    environment, _, config_file = _start_environment(tmp_path, create_config=False)
    completed = _run_start(environment, input_text="n\n192.168.50.20\n")

    assert completed.returncode == 0
    assert "AGENT_WORKFLOW_MANAGER_HOST=192.168.50.20" in config_file.read_text(
        encoding="utf-8"
    )
    assert "http://192.168.50.20:8765" in completed.stdout


def test_start_uses_external_token_without_printing_or_persisting_it(
    tmp_path: Path,
) -> None:
    environment, _, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        notifications="auto",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    secret = "tk_start_script_secret"
    environment["NOTIFY_TOKEN"] = secret
    completed = _run_start(environment)

    assert completed.returncode == 0
    assert "Terminal notifications enabled." in completed.stdout
    assert secret not in completed.stdout
    assert secret not in completed.stderr
    runtime_config = config_file.read_text(encoding="utf-8")
    assert secret not in runtime_config
    assert "NOTIFY_TOKEN" not in runtime_config


def test_installed_notify_does_not_invoke_curl(tmp_path: Path) -> None:
    environment, call_log, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        notifications="auto",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    environment["NOTIFY_TOKEN"] = "tk_external"
    _executable(
        tmp_path / "bin" / "curl",
        "printf 'curl invoked\\n' >>\"$START_CALL_LOG\"\nexit 99\n",
    )
    completed = _run_start(environment)

    assert completed.returncode == 0
    assert "curl invoked" not in call_log.read_text(encoding="utf-8")


def test_start_without_notify_configuration_still_launches(tmp_path: Path) -> None:
    environment, call_log, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        notifications="auto",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    completed = _run_start(environment)

    assert completed.returncode == 0
    assert "Runner startup will continue" in completed.stdout
    assert "uv run python -m purplemux_client.web" in call_log.read_text(
        encoding="utf-8"
    )


def test_start_installs_missing_notify_with_supported_installer(
    tmp_path: Path,
) -> None:
    environment, call_log, config_file = _start_environment(tmp_path, with_notify=False)
    _write_runtime_config(
        config_file,
        notifications="auto",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    _executable(
        tmp_path / "bin" / "curl",
        """
printf 'curl %s\n' "$*" >>"$START_CALL_LOG"
output=
url=
while (($#)); do
    if [[ $1 == --output ]]; then
        output=$2
        shift 2
    else
        url=$1
        shift
    fi
done
if [[ $url == */install-cli.sh ]]; then
cat >"$output" <<'INSTALLER'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p "$HOME/.local/bin"
install -m 0755 bin/notify "$HOME/.local/bin/notify"
INSTALLER
else
cat >"$output" <<'NOTIFY'
#!/usr/bin/env bash
exit 0
NOTIFY
fi
""",
    )
    completed = _run_start(environment)

    assert completed.returncode == 0
    calls = call_log.read_text(encoding="utf-8")
    assert "curl --fail --silent --show-error --location --output" in calls
    assert "notify-server/main/install-cli.sh" in calls
    assert "notify-server/main/bin/notify" in calls
    assert "git " not in calls
    assert "tar " not in calls
    assert (Path(environment["HOME"]) / ".local/bin/notify").is_file()


def test_start_requires_repository_root(tmp_path: Path) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    completed = subprocess.run(
        ["bash", str(REPOSITORY_ROOT / "start.sh")],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "run start.sh from the repository root" in completed.stderr
    assert not call_log.exists()


def test_start_uses_explicit_remote_interface_and_prints_url(tmp_path: Path) -> None:
    environment, call_log, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        host="100.64.10.20",
        notifications="disabled",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    completed = _run_start(environment)

    assert completed.returncode == 0
    assert "never expose this address publicly" in completed.stdout
    assert "http://100.64.10.20:8765" in completed.stdout
    assert (
        call_log.read_text(encoding="utf-8")
        .splitlines()[-1]
        .startswith(
            "uv run python -m purplemux_client.web --host 100.64.10.20 --port 8765"
        )
    )
    assert "tailscale" not in call_log.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "999.1.1.1"])
def test_start_rejects_non_explicit_or_wildcard_host(tmp_path: Path, host: str) -> None:
    environment, call_log, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        host=host,
        notifications="disabled",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    completed = _run_start(environment)

    assert completed.returncode != 0
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    assert "uv run python -m purplemux_client.web" not in calls
