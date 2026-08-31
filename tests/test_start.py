from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]


def _executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _start_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    _executable(
        fake_bin / "uv",
        'printf \'uv %s\\n\' "$*" >>"$START_CALL_LOG"\n',
    )
    _executable(fake_bin / "purplemux", "exit 0\n")
    environment = {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "START_CALL_LOG": str(call_log),
    }
    return environment, call_log


def test_start_syncs_and_launches_runner_with_notify_disabled(
    tmp_path: Path,
) -> None:
    environment, call_log = _start_environment(tmp_path)
    environment["AGENT_WORKFLOW_MANAGER_NOTIFICATIONS"] = "disabled"

    completed = subprocess.run(
        ["bash", "start.sh"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "uv sync --locked",
        "uv run python -m purplemux_client.web --host 127.0.0.1 --port 8765",
    ]
    assert "notifications disabled" in completed.stdout.lower()


def test_start_uses_external_token_without_printing_it(tmp_path: Path) -> None:
    environment, _ = _start_environment(tmp_path)
    secret = "tk_start_script_secret"
    environment["NOTIFY_TOKEN"] = secret
    _executable(tmp_path / "bin" / "notify", "exit 0\n")

    completed = subprocess.run(
        ["bash", "start.sh"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "Terminal notifications enabled." in completed.stdout
    assert secret not in completed.stdout
    assert secret not in completed.stderr


def test_start_without_notify_configuration_still_launches(tmp_path: Path) -> None:
    environment, call_log = _start_environment(tmp_path)
    _executable(tmp_path / "bin" / "notify", "exit 0\n")

    completed = subprocess.run(
        ["bash", "start.sh"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "Runner startup will continue" in completed.stdout
    assert "uv run python -m purplemux_client.web" in call_log.read_text(
        encoding="utf-8"
    )


def test_start_installs_missing_notify_with_supported_installer(
    tmp_path: Path,
) -> None:
    environment, call_log = _start_environment(tmp_path)
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

    completed = subprocess.run(
        ["bash", "start.sh"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    calls = call_log.read_text(encoding="utf-8")
    assert "curl --fail --silent --show-error --location --output" in calls
    assert "notify-server/main/install-cli.sh" in calls
    assert "notify-server/main/bin/notify" in calls
    assert "git " not in calls
    assert "tar " not in calls
    assert (Path(environment["HOME"]) / ".local/bin/notify").is_file()


def test_start_requires_repository_root(tmp_path: Path) -> None:
    environment, call_log = _start_environment(tmp_path)

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
    environment, call_log = _start_environment(tmp_path)
    environment["AGENT_WORKFLOW_MANAGER_NOTIFICATIONS"] = "disabled"
    environment["AGENT_WORKFLOW_MANAGER_HOST"] = "100.64.10.20"

    completed = subprocess.run(
        ["bash", "start.sh"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "Trusted-network bind enabled" in completed.stdout
    assert "http://100.64.10.20:8765" in completed.stdout
    assert call_log.read_text(encoding="utf-8").splitlines()[-1] == (
        "uv run python -m purplemux_client.web --host 100.64.10.20 --port 8765"
    )
    assert "tailscale" not in call_log.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "999.1.1.1"])
def test_start_rejects_non_explicit_or_wildcard_host(tmp_path: Path, host: str) -> None:
    environment, call_log = _start_environment(tmp_path)
    environment["AGENT_WORKFLOW_MANAGER_NOTIFICATIONS"] = "disabled"
    environment["AGENT_WORKFLOW_MANAGER_HOST"] = host

    completed = subprocess.run(
        ["bash", "start.sh"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    assert "uv run python -m purplemux_client.web" not in calls
