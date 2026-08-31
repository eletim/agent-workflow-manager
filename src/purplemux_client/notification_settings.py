from __future__ import annotations

import os
import re
import shlex
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from purplemux_client.notifier import NotificationResult

DEFAULT_NOTIFY_SERVER = "https://eletim.jp"
DEFAULT_NOTIFY_TOPIC = "agents"
_ASSIGNMENT = re.compile(
    r"^(?P<indent>\s*)(?:export\s+)?(?P<key>[A-Z_][A-Z0-9_]*)=(?P<value>.*)$"
)
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SettingsError(RuntimeError):
    """A sanitized settings error that is safe to return to the UI."""


class SettingsValidationError(SettingsError):
    """A settings value failed validation."""


class MutableNotifier(Protocol):
    def policy(self) -> tuple[bool, bool, bool, bool]: ...

    def configure_policy(
        self,
        *,
        enabled: bool,
        notify_success: bool,
        notify_failure: bool,
        notify_stopped: bool,
    ) -> None: ...

    def send_test(self) -> NotificationResult: ...


@dataclass(frozen=True)
class NotificationSettingsSnapshot:
    enabled: bool
    on_success: bool
    on_failure: bool
    on_stopped: bool
    server: str
    topic: str
    credential_configured: bool

    def as_json(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "onSuccess": self.on_success,
            "onFailure": self.on_failure,
            "onStopped": self.on_stopped,
            "server": self.server,
            "topic": self.topic,
            "credentialStatus": (
                "configured" if self.credential_configured else "missing"
            ),
            "restartRequired": False,
        }


@dataclass(frozen=True)
class TestNotificationResult:
    delivered: bool
    message: str

    def as_json(self) -> dict[str, object]:
        return {"delivered": self.delivered, "message": self.message}


class NotificationSettings:
    """Manage notification policy and notify CLI config without exposing secrets."""

    _POLICY_KEYS = {
        "AGENT_WORKFLOW_MANAGER_NOTIFICATIONS",
        "AGENT_WORKFLOW_MANAGER_NOTIFY_SUCCESS",
        "AGENT_WORKFLOW_MANAGER_NOTIFY_FAILURE",
        "AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED",
    }
    _NOTIFY_KEYS = {"NOTIFY_SERVER", "NOTIFY_TOPIC", "NOTIFY_TOKEN"}

    def __init__(
        self,
        *,
        runtime_config: Path,
        notify_config: Path,
        notifier: MutableNotifier,
    ) -> None:
        self._runtime_config = runtime_config
        self._notify_config = notify_config
        self._notifier = notifier
        self._lock = threading.Lock()

    def read(self) -> NotificationSettingsSnapshot:
        with self._lock:
            notify_values = self._read_assignments(
                self._notify_config, allowed=self._NOTIFY_KEYS
            )
            enabled, on_success, on_failure, on_stopped = self._notifier.policy()
            return NotificationSettingsSnapshot(
                enabled=enabled,
                on_success=on_success,
                on_failure=on_failure,
                on_stopped=on_stopped,
                server=notify_values.get("NOTIFY_SERVER", DEFAULT_NOTIFY_SERVER),
                topic=notify_values.get("NOTIFY_TOPIC", DEFAULT_NOTIFY_TOPIC),
                credential_configured=bool(notify_values.get("NOTIFY_TOKEN")),
            )

    def update(self, payload: dict[str, object]) -> NotificationSettingsSnapshot:
        allowed_fields = {
            "enabled",
            "onSuccess",
            "onFailure",
            "onStopped",
            "server",
            "topic",
            "replacementToken",
        }
        unknown = payload.keys() - allowed_fields
        if unknown:
            raise SettingsValidationError("unknown notification setting")

        current = self.read()
        enabled = self._boolean(payload, "enabled", current.enabled)
        on_success = self._boolean(payload, "onSuccess", current.on_success)
        on_failure = self._boolean(payload, "onFailure", current.on_failure)
        on_stopped = self._boolean(payload, "onStopped", current.on_stopped)
        server = self._server(payload.get("server", current.server))
        topic = self._topic(payload.get("topic", current.topic))
        replacement_token = payload.get("replacementToken")
        if replacement_token is not None:
            replacement_token = self._token(replacement_token)

        runtime_updates = {
            "AGENT_WORKFLOW_MANAGER_NOTIFICATIONS": (
                "enabled" if enabled else "disabled"
            ),
            "AGENT_WORKFLOW_MANAGER_NOTIFY_SUCCESS": self._shell_boolean(on_success),
            "AGENT_WORKFLOW_MANAGER_NOTIFY_FAILURE": self._shell_boolean(on_failure),
            "AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED": self._shell_boolean(on_stopped),
        }
        notify_updates = {"NOTIFY_SERVER": server, "NOTIFY_TOPIC": topic}
        if replacement_token is not None:
            notify_updates["NOTIFY_TOKEN"] = replacement_token

        with self._lock:
            self._update_file(
                self._notify_config,
                notify_updates,
                allowed=self._NOTIFY_KEYS,
                mode=0o600,
            )
            self._update_file(
                self._runtime_config,
                runtime_updates,
                allowed=self._POLICY_KEYS,
                mode=0o600,
            )
            self._notifier.configure_policy(
                enabled=enabled,
                notify_success=on_success,
                notify_failure=on_failure,
                notify_stopped=on_stopped,
            )
            notify_values = self._read_assignments(
                self._notify_config, allowed=self._NOTIFY_KEYS
            )

        return NotificationSettingsSnapshot(
            enabled=enabled,
            on_success=on_success,
            on_failure=on_failure,
            on_stopped=on_stopped,
            server=server,
            topic=topic,
            credential_configured=bool(notify_values.get("NOTIFY_TOKEN")),
        )

    def send_test(self) -> TestNotificationResult:
        with self._lock:
            if not self._notify_config.is_file():
                return TestNotificationResult(
                    False, "Notify configuration is missing; save settings first."
                )
            values = self._read_assignments(
                self._notify_config, allowed=self._NOTIFY_KEYS
            )
            try:
                self._server(values.get("NOTIFY_SERVER", ""))
                self._topic(values.get("NOTIFY_TOPIC", ""))
            except SettingsValidationError as exc:
                return TestNotificationResult(False, str(exc))
            if not values.get("NOTIFY_TOKEN"):
                return TestNotificationResult(
                    False, "Notify credential is missing; replace the token and save."
                )

        result = self._notifier.send_test()
        return self._sanitize_test_result(result)

    @staticmethod
    def _boolean(payload: dict[str, object], key: str, default: bool) -> bool:
        value = payload.get(key, default)
        if not isinstance(value, bool):
            raise SettingsValidationError(f"{key} must be true or false")
        return value

    @staticmethod
    def _server(value: object) -> str:
        if not isinstance(value, str) or value != value.strip() or len(value) > 2048:
            raise SettingsValidationError("Notify server must be a valid HTTP(S) URL.")
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise SettingsValidationError("Notify server must be a valid HTTP(S) URL.")
        return value

    @staticmethod
    def _topic(value: object) -> str:
        if not isinstance(value, str) or not _TOPIC.fullmatch(value):
            raise SettingsValidationError(
                "Notify topic must use 1-64 letters, numbers, underscores, or hyphens."
            )
        return value

    @staticmethod
    def _token(value: object) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise SettingsValidationError("Replacement token is invalid.")
        return value

    @staticmethod
    def _shell_boolean(value: bool) -> str:
        return "true" if value else "false"

    @staticmethod
    def _sanitize_test_result(result: NotificationResult) -> TestNotificationResult:
        if result.delivered:
            return TestNotificationResult(True, "Test notification sent.")
        if result.diagnostic == "notify command unavailable":
            message = "Notify CLI is missing; run bash start.sh to install it."
        elif result.diagnostic == "notify command timed out":
            message = "Notify timed out; check the server and network connection."
        elif result.diagnostic == "notifier closed":
            message = "Notifications are unavailable while the Runner is shutting down."
        else:
            message = "Notify failed; check the server, topic, token, and network."
        return TestNotificationResult(False, message)

    @classmethod
    def _read_assignments(cls, path: Path, *, allowed: set[str]) -> dict[str, str]:
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise SettingsError("Notification settings could not be read.") from exc

        values: dict[str, str] = {}
        for line in content.splitlines():
            match = _ASSIGNMENT.match(line)
            if match is None or match.group("key") not in allowed:
                continue
            try:
                tokens = shlex.split(match.group("value"), comments=True, posix=True)
            except ValueError as exc:
                raise SettingsError("Notification configuration is invalid.") from exc
            if len(tokens) > 1:
                raise SettingsError("Notification configuration is invalid.")
            values[match.group("key")] = tokens[0] if tokens else ""
        return values

    @classmethod
    def _update_file(
        cls,
        path: Path,
        updates: dict[str, str],
        *,
        allowed: set[str],
        mode: int,
    ) -> None:
        if not updates.keys() <= allowed:
            raise SettingsError("Notification settings update was rejected.")
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            lines = existing.splitlines()
            written: set[str] = set()
            rendered: list[str] = []
            for line in lines:
                match = _ASSIGNMENT.match(line)
                key = match.group("key") if match is not None else None
                if key in updates:
                    rendered.append(f"{key}={shlex.quote(updates[key])}")
                    written.add(key)
                else:
                    rendered.append(line)
            for key, value in updates.items():
                if key not in written:
                    rendered.append(f"{key}={shlex.quote(value)}")
            content = "\n".join(rendered) + "\n"

            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=path.parent
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                    temporary.write(content)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                temporary_path.chmod(mode)
                os.replace(temporary_path, path)
            finally:
                temporary_path.unlink(missing_ok=True)
        except OSError as exc:
            raise SettingsError("Notification settings could not be saved.") from exc
