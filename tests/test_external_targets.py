from __future__ import annotations

import json
from pathlib import Path

import pytest

from purplemux_client.external_targets import ExternalTargetSettings
from purplemux_client.notification_settings import (
    SettingsError,
    SettingsValidationError,
)


def registration(**updates: str) -> dict[str, str]:
    return {
        "id": "office",
        "destination": "https://awm.example/manager",
        "tokenEnv": "OFFICE_AWM_TOKEN",
        **updates,
    }


def test_registration_reload_and_private_connection(tmp_path: Path) -> None:
    path = tmp_path / "settings/targets.json"
    environment = {"OFFICE_AWM_TOKEN": "private-secret"}
    settings = ExternalTargetSettings(path, environment=environment)
    assert settings.read() == {"targets": []}
    saved = settings.update({"targets": [registration()]})
    reloaded = ExternalTargetSettings(path, environment=environment)
    assert reloaded.read() == saved
    assert saved["targets"] == [{**registration(), "credentialStatus": "configured"}]
    assert "private-secret" not in json.dumps(saved)
    assert "private-secret" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    connection = reloaded.connection("office")
    assert connection.target_id == "office"
    assert connection.destination == "https://awm.example/manager"
    assert connection.headers == {"X-Python-Runner-Token": "private-secret"}
    assert "private-secret" not in repr(connection)
    reloaded.update({"targets": [registration(destination="https://new.example/")]})
    assert reloaded.connection("office").destination == "https://new.example"
    reloaded.update({"targets": []})
    with pytest.raises(SettingsValidationError, match="not registered"):
        reloaded.connection("office")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"targets": {}},
        {"targets": [], "token": "secret"},
        {"targets": [registration(), registration()]},
        {"targets": [registration(id="")]},
        {"targets": [registration(id="bad id")]},
        {"targets": [registration(tokenEnv="secret value")]},
        {"targets": [{**registration(), "token": "secret"}]},
        {"targets": [None]},
        {"targets": [registration()] * 101},
    ],
)
def test_invalid_registration_preserves_file(tmp_path: Path, payload: dict) -> None:
    settings = ExternalTargetSettings(tmp_path / "targets.json", environment={})
    settings.update({"targets": [registration()]})
    original = settings.path.read_bytes()
    with pytest.raises(SettingsValidationError):
        settings.update(payload)
    assert settings.path.read_bytes() == original


@pytest.mark.parametrize(
    "destination",
    [
        "http://remote.example",
        "https://user:secret@awm.example",
        "https://awm.example?token=secret",
        "https://awm.example#secret",
        "ftp://awm.example",
        "https://",
        "https://awm.example:0",
        "https://awm.example:65536",
        "https://awm.example:bad",
        "https://[::1]unexpected",
        "https://bad_host",
        "https://awm.example:",
        "https://awm.example/\nsecret",
        " https://awm.example",
        "https://awm.example\\secret",
        "http://127.0.0.1.evil.example",
        "https://999.999.999.999",
    ],
)
def test_invalid_destination_is_sanitized(tmp_path: Path, destination: str) -> None:
    settings = ExternalTargetSettings(tmp_path / "targets.json")
    with pytest.raises(SettingsValidationError) as error:
        settings.update({"targets": [registration(destination=destination)]})
    assert "secret" not in str(error.value)
    assert not settings.path.exists()


@pytest.mark.parametrize(
    "destination",
    [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
        "https://[2001:db8::1]:443",
    ],
)
def test_supported_destinations(tmp_path: Path, destination: str) -> None:
    settings = ExternalTargetSettings(tmp_path / "targets.json", environment={})
    assert settings.update({"targets": [registration(destination=destination)]})[
        "targets"
    ] == [{**registration(destination=destination), "credentialStatus": "missing"}]


@pytest.mark.parametrize(
    "token", ["", "private-secret\r\ninjected: yes", "private-secret\x00", "é"]
)
def test_missing_or_invalid_credential_is_sanitized(tmp_path: Path, token: str) -> None:
    settings = ExternalTargetSettings(
        tmp_path / "targets.json", environment={"OFFICE_AWM_TOKEN": token}
    )
    settings.update({"targets": [registration()]})
    with pytest.raises(SettingsError) as error:
        settings.connection("office")
    assert "private-secret" not in str(error.value)


@pytest.mark.parametrize(
    "content", ["not json secret", '{"targets":[{"token":"secret"}]}']
)
def test_invalid_persisted_configuration_is_preserved(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "targets.json"
    path.write_text(content)
    settings = ExternalTargetSettings(path)
    with pytest.raises(SettingsError) as error:
        settings.read()
    assert "secret" not in str(error.value)
    with pytest.raises(SettingsError):
        settings.update({"targets": []})
    assert path.read_text() == content


def test_config_path_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_WORKFLOW_MANAGER_EXTERNAL_TARGETS_FILE", raising=False)
    assert (
        ExternalTargetSettings().path
        == tmp_path / "agent-workflow-manager/external-targets.json"
    )
    monkeypatch.setenv(
        "AGENT_WORKFLOW_MANAGER_EXTERNAL_TARGETS_FILE", str(tmp_path / "custom.json")
    )
    assert ExternalTargetSettings().path == tmp_path / "custom.json"
