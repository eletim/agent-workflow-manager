from __future__ import annotations

import fcntl
import os
import shlex
import shutil
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
    host_aliases: str = "",
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
                f"AGENT_WORKFLOW_MANAGER_HOST_ALIASES={shlex.quote(host_aliases)}",
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
        """
printf 'uv %s\\n' "$*" >>"$START_CALL_LOG"
if [[ ${1-} == run && ${2-} == --no-sync && ${3-} == python ]]; then
    shift 3
    exec /usr/bin/python3 "$@"
fi
""",
    )
    _executable(
        fake_bin / "purplemux",
        """
if [[ ${1-} == help ]]; then
    cat <<'HELP'
purplemux CLI
workspaces
workspace create --cwd PATH [--name NAME]
workspace delete -w WS --if-empty
tab create -w WS [-n NAME] [-t TYPE]
tab send -w WS TAB_ID CONTENT...
tab interrupt -w WS TAB_ID
tab status -w WS TAB_ID
tab result -w WS TAB_ID
tab capture -w WS TAB_ID
tab close -w WS TAB_ID
HELP
elif [[ ${1-} == workspaces ]]; then
    runtime_output=${PURPLEMUX_WORKSPACES_OUTPUT-'{"workspaces":[]}'}
    printf '%s\n' "$runtime_output"
else
    exit 2
fi
""",
    )
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
    assert calls[1].startswith("uv run --no-sync python -c import json, sys;")
    assert calls[2].startswith(
        "uv run python -m purplemux_client.web --host 127.0.0.1 --port 8765"
    )
    assert (
        f"--runtime-config {environment['AGENT_WORKFLOW_MANAGER_CONFIG_FILE']}"
        in calls[2]
    )
    assert (
        f"--notify-config {Path(environment['HOME']) / '.config/notify/config'}"
        in calls[2]
    )
    assert "notifications disabled" in completed.stdout.lower()


def test_start_reports_custom_install_remediation_when_purplemux_is_missing(
    tmp_path: Path,
) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    (tmp_path / "bin" / "purplemux").unlink()

    completed = _run_start(environment)

    assert completed.returncode != 0
    assert "required custom purplemux CLI not found" in completed.stderr
    assert "eletim/purplemux" in completed.stderr
    assert "cli-token" in completed.stderr
    assert "uv run python -m purplemux_client.web" not in call_log.read_text(
        encoding="utf-8"
    )


def test_start_rejects_incompatible_upstream_purplemux_cli(tmp_path: Path) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    _executable(
        tmp_path / "bin" / "purplemux",
        "printf '%s\n' 'Usage: purplemux [options] <files...>'\n",
    )

    completed = _run_start(environment)

    assert completed.returncode != 0
    assert "does not provide the required custom CLI contract" in completed.stderr
    assert "upstream npm purplemux CLI" in completed.stderr
    assert "eletim/purplemux" in completed.stderr
    assert "uv run python -m purplemux_client.web" not in call_log.read_text(
        encoding="utf-8"
    )


def test_start_uses_supported_purplemux_already_selected_by_path(
    tmp_path: Path,
) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    supported_cli = tmp_path / "bin" / "purplemux"
    original = supported_cli.read_text(encoding="utf-8")
    supported_cli.write_text(
        original.replace(
            "if [[ ${1-} == help ]]; then",
            "if [[ ${1-} == --version ]]; then\n    printf '%s\\n' 0.4.6\n"
            "elif [[ ${1-} == help ]]; then",
        ),
        encoding="utf-8",
    )
    legacy_bin = Path(environment["HOME"]) / ".local" / "bin"
    legacy_bin.mkdir(parents=True)
    _executable(
        legacy_bin / "purplemux",
        """
if [[ ${1-} == help ]]; then
    printf '%s\n' 'purplemux CLI' 'workspaces' 'workspace create --cwd PATH'
else
    exit 2
fi
""",
    )

    selected_cli = shutil.which("purplemux", path=environment["PATH"])
    version = subprocess.run(
        [str(selected_cli), "--version"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    completed = _run_start(environment)

    assert selected_cli == str(supported_cli)
    assert version.stdout.strip() == "0.4.6"
    assert completed.returncode == 0
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls[1].startswith("uv run --no-sync python -c import json, sys;")
    assert calls[2].startswith("uv run python -m purplemux_client.web")


def test_start_reports_purplemux_help_cli_failure(tmp_path: Path) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    _executable(
        tmp_path / "bin" / "purplemux",
        "printf '%s\\n' 'unknown command from purplemux' >&2\nexit 7\n",
    )

    completed = _run_start(environment)

    assert completed.returncode != 0
    assert "purplemux help failed (exit 7)" in completed.stderr
    assert "unknown command from purplemux" in completed.stderr
    assert "did not respond within 5 seconds" not in completed.stderr
    assert "uv run python -m purplemux_client.web" not in call_log.read_text(
        encoding="utf-8"
    )


def test_hanging_purplemux_help_check_is_reported_as_timeout(tmp_path: Path) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    _executable(tmp_path / "bin" / "purplemux", "sleep 30\n")

    completed = subprocess.run(
        ["bash", "start.sh"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
    )

    assert completed.returncode != 0
    assert "did not respond within 5 seconds" in completed.stderr
    assert "purplemux help failed" not in completed.stderr
    assert "uv run python -m purplemux_client.web" not in call_log.read_text(
        encoding="utf-8"
    )


def test_start_rejects_purplemux_cli_without_tab_type_option(tmp_path: Path) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    purplemux = tmp_path / "bin" / "purplemux"
    original = purplemux.read_text(encoding="utf-8")
    purplemux.write_text(
        original.replace(
            "tab create -w WS [-n NAME] [-t TYPE]",
            "tab create -w WS [-n NAME]",
        ),
        encoding="utf-8",
    )

    completed = _run_start(environment)

    assert completed.returncode != 0
    assert "does not provide the required custom CLI contract" in completed.stderr
    assert "tab create -w WS [-n NAME] [-t TYPE]" in completed.stderr
    assert "uv run python -m purplemux_client.web" not in call_log.read_text(
        encoding="utf-8"
    )


def test_start_rejects_purplemux_without_atomic_workspace_cleanup_contract(
    tmp_path: Path,
) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    purplemux = tmp_path / "bin" / "purplemux"
    original = purplemux.read_text(encoding="utf-8")
    purplemux.write_text(
        original.replace("workspace delete -w WS --if-empty\n", ""),
        encoding="utf-8",
    )

    completed = _run_start(environment)

    assert completed.returncode != 0
    assert "does not provide the required custom CLI contract" in completed.stderr
    assert "workspace delete -w WS --if-empty" in completed.stderr
    assert "uv run python -m purplemux_client.web" not in call_log.read_text(
        encoding="utf-8"
    )


def test_start_rejects_unreachable_purplemux_runtime(tmp_path: Path) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    purplemux = tmp_path / "bin" / "purplemux"
    original = purplemux.read_text(encoding="utf-8")
    purplemux.write_text(
        original.replace(
            "if [[ ${1-} == help ]]; then",
            "if [[ ${1-} == workspaces ]]; then\n    exit 7\n"
            "elif [[ ${1-} == help ]]; then",
        ),
        encoding="utf-8",
    )

    completed = _run_start(environment)

    assert completed.returncode != 0
    assert "could not reach its runtime within 5 seconds" in completed.stderr
    assert "cli-token" in completed.stderr
    assert "uv run python -m purplemux_client.web" not in call_log.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "runtime_output",
    (
        'not-json "workspaces":',
        '{"workspaces":null}',
        "[]",
    ),
)
def test_start_rejects_invalid_purplemux_runtime_response(
    tmp_path: Path,
    runtime_output: str,
) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    environment["PURPLEMUX_WORKSPACES_OUTPUT"] = runtime_output

    completed = _run_start(environment)

    assert completed.returncode != 0
    assert "returned an unexpected response" in completed.stderr
    assert "uv run python -m purplemux_client.web" not in call_log.read_text(
        encoding="utf-8"
    )


def test_hanging_purplemux_runtime_check_is_bounded(tmp_path: Path) -> None:
    environment, call_log, _ = _start_environment(tmp_path)
    purplemux = tmp_path / "bin" / "purplemux"
    original = purplemux.read_text(encoding="utf-8")
    purplemux.write_text(
        original.replace(
            "if [[ ${1-} == help ]]; then",
            "if [[ ${1-} == workspaces ]]; then\n    sleep 30\n"
            "elif [[ ${1-} == help ]]; then",
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["bash", "start.sh"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
    )

    assert completed.returncode != 0
    assert "could not reach its runtime within 5 seconds" in completed.stderr
    assert "uv run python -m purplemux_client.web" not in call_log.read_text(
        encoding="utf-8"
    )


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
    assert "AGENT_WORKFLOW_MANAGER_HOST_ALIASES=''" in config
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


def test_first_run_detects_and_accepts_magicdns_hostname(tmp_path: Path) -> None:
    environment, call_log, config_file = _start_environment(
        tmp_path, create_config=False
    )
    _executable(
        tmp_path / "bin" / "tailscale",
        """
printf 'tailscale %s\n' "$*" >>"$START_CALL_LOG"
if [[ $* == "ip -4" ]]; then
    printf '100.70.80.90\n'
elif [[ $* == "status --json" ]]; then
    printf '%s\n' '{"Self":{"DNSName":"E-Ryzen.tail6bc726.ts.net."}}'
else
    exit 1
fi
""",
    )
    completed = _run_start(environment, input_text="\n\n")

    assert completed.returncode == 0
    config = config_file.read_text(encoding="utf-8")
    assert "AGENT_WORKFLOW_MANAGER_HOST=100.70.80.90" in config
    assert "AGENT_WORKFLOW_MANAGER_HOST_ALIASES=e-ryzen.tail6bc726.ts.net" in config
    assert "http://e-ryzen.tail6bc726.ts.net:8765" in completed.stdout
    assert "tailscale status --json" in call_log.read_text(encoding="utf-8")


def test_first_run_can_decline_detected_magicdns_hostname(tmp_path: Path) -> None:
    environment, _, config_file = _start_environment(tmp_path, create_config=False)
    _executable(
        tmp_path / "bin" / "tailscale",
        """
if [[ $* == "ip -4" ]]; then
    printf '100.70.80.90\n'
else
    printf '%s\n' '{"Self":{"DNSName":"e-ryzen.tail6bc726.ts.net."}}'
fi
""",
    )
    completed = _run_start(environment, input_text="\nn\n")

    assert completed.returncode == 0
    assert "AGENT_WORKFLOW_MANAGER_HOST_ALIASES=''" in config_file.read_text(
        encoding="utf-8"
    )


def test_existing_config_supports_manual_hostname_alias_without_tailscale(
    tmp_path: Path,
) -> None:
    environment, call_log, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        host="192.168.50.20",
        host_aliases="runner.lan",
        notifications="disabled",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    completed = _run_start(environment)

    assert completed.returncode == 0
    assert "http://192.168.50.20:8765" in completed.stdout
    assert "http://runner.lan:8765" in completed.stdout
    calls = call_log.read_text(encoding="utf-8")
    assert "--host-aliases runner.lan" in calls
    assert "tailscale" not in calls


def test_existing_config_accepts_detected_magicdns_alias_and_preserves_values(
    tmp_path: Path,
) -> None:
    environment, call_log, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        host="100.70.80.90",
        notifications="disabled",
        notify_failure="false",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    config = config_file.read_text(encoding="utf-8")
    config = config.replace(
        "AGENT_WORKFLOW_MANAGER_HOST_ALIASES=''\n",
        "AGENT_WORKFLOW_MANAGER_HOST_ALIASES=''; "
        "UNRELATED_INLINE_VALUE='keep inline' # keep this comment\n",
    )
    config_file.write_text(
        "# Existing installation settings\n"
        + config
        + "UNRELATED_RUNTIME_VALUE='preserve me'\n",
        encoding="utf-8",
    )
    _executable(
        tmp_path / "bin" / "tailscale",
        """
printf 'tailscale %s\n' "$*" >>"$START_CALL_LOG"
printf '%s\n' '{"Self":{"DNSName":"E-Ryzen.tail6bc726.ts.net."}}'
""",
    )

    completed = _run_start(environment, input_text="\n")

    assert completed.returncode == 0
    assert (
        "MagicDNS hostname e-ryzen.tail6bc726.ts.net was detected." in completed.stderr
    )
    assert "Allow browser access using this hostname? [Y/n]" in completed.stderr
    updated = config_file.read_text(encoding="utf-8")
    assert "AGENT_WORKFLOW_MANAGER_HOST=100.70.80.90" in updated
    assert "AGENT_WORKFLOW_MANAGER_HOST_ALIASES=e-ryzen.tail6bc726.ts.net" in updated
    assert "AGENT_WORKFLOW_MANAGER_NOTIFY_FAILURE=false" in updated
    assert "UNRELATED_RUNTIME_VALUE='preserve me'" in updated
    assert (
        "AGENT_WORKFLOW_MANAGER_HOST_ALIASES=''; "
        "UNRELATED_INLINE_VALUE='keep inline' # keep this comment" in updated
    )
    assert "# Existing installation settings" in updated
    assert config_file.stat().st_mode & 0o777 == 0o600
    assert "http://e-ryzen.tail6bc726.ts.net:8765" in completed.stdout
    calls = call_log.read_text(encoding="utf-8")
    assert "tailscale status --json" in calls
    assert "tailscale ip -4" not in calls


def test_existing_config_can_decline_detected_magicdns_alias(tmp_path: Path) -> None:
    environment, _, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        host="100.70.80.90",
        notifications="disabled",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    original = config_file.read_bytes()
    _executable(
        tmp_path / "bin" / "tailscale",
        'printf \'%s\n\' \'{"Self":{"DNSName":"e-ryzen.tail6bc726.ts.net."}}\'\n',
    )

    completed = _run_start(environment, input_text="n\n")

    assert completed.returncode == 0
    assert "Allow browser access using this hostname? [Y/n]" in completed.stderr
    assert config_file.read_bytes() == original
    assert "http://e-ryzen.tail6bc726.ts.net:8765" not in completed.stdout


def test_existing_configured_alias_skips_magicdns_migration(tmp_path: Path) -> None:
    environment, call_log, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        host="100.70.80.90",
        host_aliases="runner.example.test",
        notifications="disabled",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    original = config_file.read_bytes()
    _executable(
        tmp_path / "bin" / "tailscale",
        'printf \'tailscale %s\n\' "$*" >>"$START_CALL_LOG"\nexit 99\n',
    )

    completed = _run_start(environment)

    assert completed.returncode == 0
    assert "Allow browser access" not in completed.stderr
    assert config_file.read_bytes() == original
    assert "tailscale" not in call_log.read_text(encoding="utf-8")


def test_existing_config_magicdns_detection_failure_is_nonfatal(
    tmp_path: Path,
) -> None:
    environment, call_log, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        host="100.70.80.90",
        notifications="disabled",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    original = config_file.read_bytes()
    _executable(
        tmp_path / "bin" / "tailscale",
        'printf \'tailscale %s\n\' "$*" >>"$START_CALL_LOG"\nexit 1\n',
    )

    completed = _run_start(environment)

    assert completed.returncode == 0
    assert "Allow browser access" not in completed.stderr
    assert "http://100.70.80.90:8765" in completed.stdout
    assert config_file.read_bytes() == original
    assert "tailscale status --json" in call_log.read_text(encoding="utf-8")


def test_existing_config_environment_alias_override_skips_migration(
    tmp_path: Path,
) -> None:
    environment, call_log, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        host="100.70.80.90",
        notifications="disabled",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    environment["AGENT_WORKFLOW_MANAGER_HOST_ALIASES"] = "override.example.test"
    original = config_file.read_bytes()
    _executable(
        tmp_path / "bin" / "tailscale",
        'printf \'tailscale %s\n\' "$*" >>"$START_CALL_LOG"\nexit 99\n',
    )

    completed = _run_start(environment)

    assert completed.returncode == 0
    assert "Allow browser access" not in completed.stderr
    assert "http://override.example.test:8765" in completed.stdout
    assert config_file.read_bytes() == original
    assert "tailscale" not in call_log.read_text(encoding="utf-8")


def test_existing_config_migration_preserves_concurrent_locked_update(
    tmp_path: Path,
) -> None:
    environment, _, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        host="100.70.80.90",
        notifications="disabled",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    _executable(
        tmp_path / "bin" / "tailscale",
        'printf \'%s\n\' \'{"Self":{"DNSName":"e-ryzen.tail6bc726.ts.net."}}\'\n',
    )
    lock_path = config_file.with_name(f".{config_file.name}.lock")
    lock_path.touch(mode=0o600)

    with lock_path.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = subprocess.Popen(
            ["bash", "start.sh"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdin is not None
        process.stdin.write("\n")
        process.stdin.flush()

        concurrent_content = (
            config_file.read_text(encoding="utf-8")
            + "CONCURRENT_SETTINGS_VALUE='preserved'\n"
        )
        concurrent_temp = config_file.with_suffix(".concurrent")
        concurrent_temp.write_text(concurrent_content, encoding="utf-8")
        concurrent_temp.chmod(0o600)
        os.replace(concurrent_temp, config_file)
        fcntl.flock(lock, fcntl.LOCK_UN)

    stdout, stderr = process.communicate(timeout=6)

    assert process.returncode == 0
    updated = config_file.read_text(encoding="utf-8")
    assert "CONCURRENT_SETTINGS_VALUE='preserved'" in updated
    assert "AGENT_WORKFLOW_MANAGER_HOST_ALIASES=e-ryzen.tail6bc726.ts.net" in updated
    assert "http://e-ryzen.tail6bc726.ts.net:8765" in stdout
    assert "Allow browser access using this hostname? [Y/n]" in stderr


@pytest.mark.parametrize(
    "alias",
    [
        "*.ts.net",
        "https://runner.ts.net",
        "runner.ts.net/path",
        "user@runner.ts.net",
        "runner.ts.net?query",
        "runner.ts.net\nforged",
        "-runner.ts.net",
        "runner..ts.net",
        "100.70.80.90",
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
def test_start_rejects_invalid_hostname_alias(tmp_path: Path, alias: str) -> None:
    environment, call_log, config_file = _start_environment(tmp_path)
    _write_runtime_config(
        config_file,
        host_aliases=alias,
        notifications="disabled",
        notify_config=Path(environment["HOME"]) / ".config/notify/config",
    )
    completed = _run_start(environment)

    assert completed.returncode == 2
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    assert "uv run python -m purplemux_client.web" not in calls


def test_first_run_without_tailscale_defaults_to_localhost(tmp_path: Path) -> None:
    environment, _, config_file = _start_environment(tmp_path, create_config=False)
    completed = _run_start(environment, input_text="\n")

    assert completed.returncode == 0
    assert "Tailscale IPv4 could not be detected." in completed.stderr
    assert "AGENT_WORKFLOW_MANAGER_HOST=127.0.0.1" in config_file.read_text(
        encoding="utf-8"
    )


def test_hanging_tailscale_discovery_is_bounded_and_falls_back(
    tmp_path: Path,
) -> None:
    environment, _, config_file = _start_environment(tmp_path, create_config=False)
    _executable(tmp_path / "bin" / "tailscale", "sleep 30\n")

    completed = subprocess.run(
        ["bash", "start.sh"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        input="\n",
        check=False,
        capture_output=True,
        text=True,
        timeout=6,
    )

    assert completed.returncode == 0
    assert "Tailscale IPv4 could not be detected." in completed.stderr
    assert "AGENT_WORKFLOW_MANAGER_HOST=127.0.0.1" in config_file.read_text(
        encoding="utf-8"
    )


def test_hanging_magicdns_discovery_is_bounded_and_keeps_detected_ip(
    tmp_path: Path,
) -> None:
    environment, _, config_file = _start_environment(tmp_path, create_config=False)
    _executable(
        tmp_path / "bin" / "tailscale",
        """
if [[ $* == "ip -4" ]]; then
    printf '100.70.80.90\n'
else
    sleep 30
fi
""",
    )

    completed = subprocess.run(
        ["bash", "start.sh"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        input="\n",
        check=False,
        capture_output=True,
        text=True,
        timeout=6,
    )

    assert completed.returncode == 0
    config = config_file.read_text(encoding="utf-8")
    assert "AGENT_WORKFLOW_MANAGER_HOST=100.70.80.90" in config
    assert "AGENT_WORKFLOW_MANAGER_HOST_ALIASES=''" in config


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
