"""Persistent external AWM registrations and private connection resolution."""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from purplemux_client.notification_settings import (
    SettingsError,
    SettingsValidationError,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


@dataclass(frozen=True)
class ExternalTargetConnection:
    target_id: str
    destination: str
    headers: dict[str, str] = field(repr=False)


class ExternalTargetSettings:
    """Replace registrations atomically; resolve credentials only for server code."""

    def __init__(
        self, path: Path | None = None, *, environment: Mapping[str, str] | None = None
    ) -> None:
        self.path = path if path is not None else self.default_path()
        self._environment = os.environ if environment is None else environment

    @staticmethod
    def default_path() -> Path:
        configured = os.environ.get("AGENT_WORKFLOW_MANAGER_EXTERNAL_TARGETS_FILE")
        if configured:
            return Path(configured).expanduser()
        root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        return root / "agent-workflow-manager" / "external-targets.json"

    @staticmethod
    def _validate(payload: object) -> list[dict[str, str]]:
        if not isinstance(payload, dict) or set(payload) != {"targets"}:
            raise SettingsValidationError(
                "External settings must contain only targets."
            )
        targets = payload["targets"]
        if not isinstance(targets, list) or len(targets) > 100:
            raise SettingsValidationError(
                "Targets must be a list of at most 100 registrations."
            )
        result = []
        identifiers = set()
        for target in targets:
            if not isinstance(target, dict) or set(target) != {
                "id",
                "destination",
                "tokenEnv",
            }:
                raise SettingsValidationError(
                    "Each target requires id, destination, and tokenEnv only."
                )
            identifier = target["id"]
            token_env = target["tokenEnv"]
            if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
                raise SettingsValidationError(
                    "Target id must use 1-64 letters, numbers, underscores, or hyphens."
                )
            if identifier in identifiers:
                raise SettingsValidationError("Target ids must be unique.")
            identifiers.add(identifier)
            if not isinstance(token_env, str) or not _ENVIRONMENT_NAME.fullmatch(
                token_env
            ):
                raise SettingsValidationError(
                    "tokenEnv must name an environment variable."
                )
            destination = ExternalTargetSettings._destination(target["destination"])
            result.append(
                {"id": identifier, "destination": destination, "tokenEnv": token_env}
            )
        return result

    @staticmethod
    def _destination(value: object) -> str:
        message = "Destination must be HTTPS, or loopback HTTP, without credentials, query, or fragment."
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 2048
            or any(
                ord(char) <= 32 or ord(char) >= 127 or char == "\\" for char in value
            )
        ):
            raise SettingsValidationError(message)
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            port = parsed.port
            loopback = host == "localhost"
            if host is None or "%" in host:
                raise ValueError
            try:
                address = ipaddress.ip_address(host)
                loopback = address.is_loopback
            except ValueError:
                labels = host.removesuffix(".").split(".")
                if (
                    len(host) > 253
                    or labels[-1].isdigit()
                    or any(not _HOST_LABEL.fullmatch(label) for label in labels)
                ):
                    raise ValueError from None
            if (
                parsed.scheme not in {"https", "http"}
                or (parsed.scheme == "http" and not loopback)
                or parsed.username is not None
                or parsed.password is not None
                or "?" in value
                or "#" in value
                or (port is not None and not 1 <= port <= 65535)
                or re.fullmatch(
                    (
                        r"\[[0-9A-Fa-f:.]+\](?::[0-9]+)?"
                        if ":" in host
                        else r"[A-Za-z0-9.-]+(?::[0-9]+)?"
                    ),
                    parsed.netloc,
                )
                is None
            ):
                raise ValueError
        except ValueError:
            raise SettingsValidationError(message) from None
        return value.rstrip("/")

    def _read_targets(self) -> list[dict[str, str]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            raise SettingsError("External target settings could not be read.") from None
        return self._validate(payload)

    @staticmethod
    def _credential_status(token: str | None) -> str:
        if not token:
            return "missing"
        if len(token) > 4096 or any(
            ord(char) < 33 or ord(char) > 126 for char in token
        ):
            return "invalid"
        return "configured"

    def read(self) -> dict[str, object]:
        return {
            "targets": [
                {
                    **target,
                    "credentialStatus": self._credential_status(
                        self._environment.get(target["tokenEnv"])
                    ),
                }
                for target in self._read_targets()
            ]
        }

    def update(self, payload: dict[str, object]) -> dict[str, object]:
        targets = self._validate(payload)
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(f".{self.path.name}.lock")
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(descriptor, "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                # Do not silently overwrite corrupt or ambiguous local configuration.
                self._read_targets()
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent, delete=False
                ) as stream:
                    temporary = stream.name
                    json.dump({"targets": targets}, stream, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                temporary = None
        except OSError:
            raise SettingsError(
                "External target settings could not be saved."
            ) from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return self.read()

    def connection(self, target_id: str) -> ExternalTargetConnection:
        """Resolve a registered destination and AWM request header for server use."""
        target = next(
            (item for item in self._read_targets() if item["id"] == target_id), None
        )
        if target is None:
            raise SettingsValidationError("External target is not registered.")
        token = self._environment.get(target["tokenEnv"])
        if token is None or self._credential_status(token) != "configured":
            raise SettingsError("External target credential is missing or invalid.")
        return ExternalTargetConnection(
            target_id, target["destination"], {"X-Python-Runner-Token": token}
        )
