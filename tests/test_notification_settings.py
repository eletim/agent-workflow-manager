from __future__ import annotations

import json
from pathlib import Path

import pytest

from purplemux_client.notification_settings import (
    NotificationSettings,
    SettingsValidationError,
)
from purplemux_client.notifier import NotificationResult


class FakeNotifier:
    def __init__(
        self,
        *,
        policy: tuple[bool, bool, bool, bool] = (True, True, True, False),
        test_result: NotificationResult | None = None,
    ) -> None:
        self.current_policy = policy
        self.test_result = test_result or NotificationResult(
            True, True, "notification sent"
        )
        self.configure_calls: list[tuple[bool, bool, bool, bool]] = []
        self.transport_calls: list[tuple[str, str, str | None]] = []
        self.test_calls = 0

    def policy(self) -> tuple[bool, bool, bool, bool]:
        return self.current_policy

    def configure_policy(
        self,
        *,
        enabled: bool,
        notify_success: bool,
        notify_failure: bool,
        notify_stopped: bool,
    ) -> None:
        self.current_policy = (
            enabled,
            notify_success,
            notify_failure,
            notify_stopped,
        )
        self.configure_calls.append(self.current_policy)

    def send_test(self) -> NotificationResult:
        self.test_calls += 1
        return self.test_result

    def configure_transport(
        self, *, server: str, topic: str, replacement_token: str | None
    ) -> None:
        self.transport_calls.append((server, topic, replacement_token))


def _settings(
    tmp_path: Path,
    notifier: FakeNotifier | None = None,
    *,
    environment: dict[str, str] | None = None,
) -> tuple[NotificationSettings, Path, Path, FakeNotifier]:
    runtime_config = tmp_path / "config.sh"
    notify_config = tmp_path / "notify/config"
    fake_notifier = notifier or FakeNotifier()
    settings = NotificationSettings(
        runtime_config=runtime_config,
        notify_config=notify_config,
        notifier=fake_notifier,
        environment={} if environment is None else environment,
    )
    return settings, runtime_config, notify_config, fake_notifier


def test_settings_read_reports_policy_transport_and_credential_status(
    tmp_path: Path,
) -> None:
    settings, _, notify_config, _ = _settings(tmp_path)
    notify_config.parent.mkdir()
    secret = "tk_read_secret"
    notify_config.write_text(
        "NOTIFY_SERVER=https://notify.example\n"
        "NOTIFY_TOPIC=runner_team\n"
        f"NOTIFY_TOKEN={secret}\n",
        encoding="utf-8",
    )

    payload = settings.read().as_json()

    assert payload == {
        "enabled": True,
        "onSuccess": True,
        "onFailure": True,
        "onStopped": False,
        "server": "https://notify.example",
        "topic": "runner_team",
        "credentialStatus": "configured",
        "restartRequired": False,
    }
    assert secret not in json.dumps(payload)


def test_settings_read_reports_missing_credential_without_config(
    tmp_path: Path,
) -> None:
    settings, _, _, _ = _settings(tmp_path)

    payload = settings.read().as_json()

    assert payload["server"] == "https://eletim.jp"
    assert payload["topic"] == "agents"
    assert payload["credentialStatus"] == "missing"


def test_settings_write_updates_only_owned_values_and_applies_policy(
    tmp_path: Path,
) -> None:
    settings, runtime_config, notify_config, notifier = _settings(tmp_path)
    runtime_config.write_text(
        'AGENT_WORKFLOW_MANAGER_HOST="127.0.0.1"\nAGENT_WORKFLOW_MANAGER_PORT="8765"\n',
        encoding="utf-8",
    )
    notify_config.parent.mkdir()
    notify_config.write_text(
        "# notify transport\n"
        "NOTIFY_SERVER=https://old.example\n"
        "NOTIFY_TOPIC=old-topic\n"
        "NOTIFY_TOKEN=old-secret\n",
        encoding="utf-8",
    )

    payload = settings.update(
        {
            "enabled": False,
            "onSuccess": False,
            "onFailure": True,
            "onStopped": True,
            "server": "https://notify.example",
            "topic": "new_topic",
            "replacementToken": "tk_new_secret",
        }
    ).as_json()

    assert payload["credentialStatus"] == "configured"
    assert "tk_new_secret" not in json.dumps(payload)
    runtime_content = runtime_config.read_text(encoding="utf-8")
    assert 'AGENT_WORKFLOW_MANAGER_HOST="127.0.0.1"' in runtime_content
    assert "AGENT_WORKFLOW_MANAGER_NOTIFICATIONS=disabled" in runtime_content
    assert "AGENT_WORKFLOW_MANAGER_NOTIFY_SUCCESS=false" in runtime_content
    assert "AGENT_WORKFLOW_MANAGER_NOTIFY_FAILURE=true" in runtime_content
    assert "AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED=true" in runtime_content
    assert "tk_new_secret" not in runtime_content
    notify_content = notify_config.read_text(encoding="utf-8")
    assert "NOTIFY_SERVER=https://notify.example" in notify_content
    assert "NOTIFY_TOPIC=new_topic" in notify_content
    assert "NOTIFY_TOKEN=tk_new_secret" in notify_content
    assert notifier.configure_calls == [(False, False, True, True)]
    assert notifier.transport_calls == [
        ("https://notify.example", "new_topic", "tk_new_secret")
    ]
    assert runtime_config.stat().st_mode & 0o777 == 0o600
    assert notify_config.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.rglob(".config.sh.*")) == []
    assert list(tmp_path.rglob(".config.*")) == []


def test_server_topic_update_preserves_existing_token(tmp_path: Path) -> None:
    settings, _, notify_config, _ = _settings(tmp_path)
    notify_config.parent.mkdir()
    notify_config.write_text(
        "NOTIFY_SERVER=https://old.example\n"
        "NOTIFY_TOPIC=old-topic\n"
        "NOTIFY_TOKEN=tk_keep_secret\n",
        encoding="utf-8",
    )

    payload = settings.update(
        {"server": "https://new.example", "topic": "new-topic"}
    ).as_json()

    notify_content = notify_config.read_text(encoding="utf-8")
    assert "NOTIFY_TOKEN=tk_keep_secret" in notify_content
    assert payload["credentialStatus"] == "configured"
    assert "tk_keep_secret" not in json.dumps(payload)


def test_environment_transport_overrides_are_reported_and_used(tmp_path: Path) -> None:
    notifier = FakeNotifier()
    settings, _, notify_config, _ = _settings(
        tmp_path,
        notifier,
        environment={
            "NOTIFY_SERVER": "https://environment.example",
            "NOTIFY_TOPIC": "environment_topic",
            "NOTIFY_TOKEN": "tk_environment_secret",
        },
    )
    notify_config.parent.mkdir()
    notify_config.write_text(
        "NOTIFY_SERVER=https://file.example\nNOTIFY_TOPIC=file_topic\n",
        encoding="utf-8",
    )

    payload = settings.read().as_json()
    result = settings.send_test()
    updated = settings.update(
        {
            "server": "https://saved.example",
            "topic": "saved_topic",
            "replacementToken": "tk_saved_secret",
        }
    ).as_json()

    assert payload["server"] == "https://environment.example"
    assert payload["topic"] == "environment_topic"
    assert payload["credentialStatus"] == "configured"
    assert "tk_environment_secret" not in json.dumps(payload)
    assert result.delivered is True
    assert updated["server"] == "https://saved.example"
    assert updated["topic"] == "saved_topic"
    assert "tk_saved_secret" not in json.dumps(updated)
    assert notifier.transport_calls == [
        ("https://environment.example", "environment_topic", "tk_environment_secret"),
        ("https://saved.example", "saved_topic", "tk_saved_secret"),
    ]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("enabled", "yes", "enabled must be true or false"),
        ("server", "ftp://notify.example", r"valid HTTP\(S\) URL"),
        ("server", "https://token@notify.example", r"valid HTTP\(S\) URL"),
        ("server", "http://192.168.1.2", "must use HTTPS"),
        ("topic", "not a topic", "1-64 letters"),
        ("replacementToken", "", "Replacement token is invalid"),
    ],
)
def test_settings_write_validates_all_inputs_before_writing(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    settings, runtime_config, notify_config, _ = _settings(tmp_path)
    payload: dict[str, object] = {
        "enabled": True,
        "onSuccess": True,
        "onFailure": True,
        "onStopped": False,
        "server": "https://notify.example",
        "topic": "agents",
    }
    payload[field] = value

    with pytest.raises(SettingsValidationError, match=message):
        settings.update(payload)

    assert not runtime_config.exists()
    assert not notify_config.exists()


def test_settings_accepts_loopback_http_notify_server(tmp_path: Path) -> None:
    settings, _, _, _ = _settings(tmp_path)

    payload = settings.update(
        {"server": "http://127.0.0.1:8080", "topic": "agents"}
    ).as_json()

    assert payload["server"] == "http://127.0.0.1:8080"


@pytest.mark.parametrize(
    "server",
    [
        "https://[",
        "https://notify.example\rINJECTED=value",
        "https://notify.example\t",
        "https://notify.example\u0085INJECTED=value",
        "https://notify.example\u2028INJECTED=value",
        "https://notify.example\u2029INJECTED=value",
    ],
)
def test_malformed_or_control_character_server_is_safely_rejected(
    tmp_path: Path, server: str
) -> None:
    settings, runtime_config, notify_config, _ = _settings(tmp_path)

    with pytest.raises(SettingsValidationError, match="valid HTTP"):
        settings.update({"server": server, "topic": "agents"})

    assert not runtime_config.exists()
    assert not notify_config.exists()


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_separator_in_replacement_token_is_safely_rejected(
    tmp_path: Path, separator: str
) -> None:
    settings, runtime_config, notify_config, _ = _settings(tmp_path)

    with pytest.raises(SettingsValidationError, match="Replacement token is invalid"):
        settings.update(
            {
                "server": "https://notify.example",
                "topic": "agents",
                "replacementToken": f"tk_before{separator}NOTIFY_TOKEN=injected",
            }
        )

    assert not runtime_config.exists()
    assert not notify_config.exists()


def test_test_notification_requires_config_and_credential(tmp_path: Path) -> None:
    settings, _, notify_config, notifier = _settings(tmp_path)

    missing_config = settings.send_test()
    notify_config.parent.mkdir()
    notify_config.write_text(
        "NOTIFY_SERVER=https://notify.example\nNOTIFY_TOPIC=agents\n",
        encoding="utf-8",
    )
    missing_credential = settings.send_test()

    assert missing_config.delivered is False
    assert "configuration is missing" in missing_config.message
    assert missing_credential.delivered is False
    assert "credential is missing" in missing_credential.message
    assert notifier.test_calls == 0


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        ("notify command unavailable", "Notify CLI is missing"),
        ("notify command timed out", "server and network"),
        ("notify exited with status 2", "server, topic, token, and network"),
    ],
)
def test_test_notification_returns_sanitized_actionable_failure(
    tmp_path: Path, diagnostic: str, expected: str
) -> None:
    secret = "tk_failure_secret"
    notifier = FakeNotifier(
        test_result=NotificationResult(True, False, diagnostic + secret)
        if diagnostic.startswith("notify exited")
        else NotificationResult(True, False, diagnostic)
    )
    settings, _, notify_config, _ = _settings(tmp_path, notifier)
    notify_config.parent.mkdir()
    notify_config.write_text(
        "NOTIFY_SERVER=https://notify.example\n"
        "NOTIFY_TOPIC=agents\n"
        f"NOTIFY_TOKEN={secret}\n",
        encoding="utf-8",
    )

    result = settings.send_test()

    assert result.delivered is False
    assert expected in result.message
    assert secret not in result.message


def test_test_notification_success(tmp_path: Path) -> None:
    settings, _, notify_config, notifier = _settings(tmp_path)
    notify_config.parent.mkdir()
    notify_config.write_text(
        "NOTIFY_SERVER=https://notify.example\n"
        "NOTIFY_TOPIC=agents\n"
        "NOTIFY_TOKEN=tk_configured\n",
        encoding="utf-8",
    )

    result = settings.send_test()

    assert result.delivered is True
    assert result.message == "Test notification sent."
    assert notifier.test_calls == 1
