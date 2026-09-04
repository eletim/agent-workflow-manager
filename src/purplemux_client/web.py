from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from purplemux_client.errors import TerminalSessionError
from purplemux_client.notification_settings import (
    NotificationSettings,
    SettingsError,
    SettingsValidationError,
)
from purplemux_client.notifier import NotifyCLI
from purplemux_client.readiness import (
    AgentReadinessService,
    ReadinessProbeBusy,
    ReadinessReconciliationRequired,
)
from purplemux_client.runner import (
    AlreadyRunningError,
    InvalidExecutionContextError,
    PythonRunner,
    RunCleanupInProgressError,
    RunCleanupNotAllowedError,
    RunNotFoundError,
    RunNotResumableError,
    WorkflowDryRunError,
    WorkflowValidationError,
)

STATIC_DIR = Path(__file__).with_name("web_static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/log-display.js": ("log-display.js", "text/javascript; charset=utf-8"),
    "/output-copy.js": ("output-copy.js", "text/javascript; charset=utf-8"),
    "/python-workflow-guide.md": (
        "python-workflow-guide.md",
        "text/markdown; charset=utf-8",
    ),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _parse_ipv4_number(value: str) -> int | None:
    base = 10
    digits = value
    if value.lower().startswith("0x"):
        base = 16
        digits = value[2:]
    elif len(value) > 1 and value.startswith("0"):
        base = 8
        digits = value[1:]
    if not digits and base == 16:
        return 0
    if not digits:
        return None
    valid_digits = {
        8: re.compile(r"[0-7]+"),
        10: re.compile(r"[0-9]+"),
        16: re.compile(r"[0-9a-fA-F]+"),
    }
    if valid_digits[base].fullmatch(digits) is None:
        return None
    return int(digits, base)


def _looks_like_browser_ipv4(value: str) -> bool:
    parts = value.split(".")
    if not 1 <= len(parts) <= 4:
        return False
    numbers = [_parse_ipv4_number(part) for part in parts]
    if any(number is None for number in numbers):
        return False
    parsed_numbers = cast(list[int], numbers)
    if any(number > 255 for number in parsed_numbers[:-1]):
        return False
    return parsed_numbers[-1] < 256 ** (5 - len(parsed_numbers))


def normalize_host_alias(value: str) -> str:
    """Validate and canonicalize one exact DNS hostname alias."""
    if not value or value != value.strip():
        raise ValueError("hostname alias must not be empty or contain whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("hostname alias must not contain control characters")
    if "*" in value:
        raise ValueError("wildcard hostname aliases are not allowed")
    if value.endswith("."):
        value = value[:-1]
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "hostname alias must contain only ASCII DNS characters"
        ) from exc
    normalized = value.lower()
    if not normalized or len(normalized) > 253:
        raise ValueError("hostname alias must be between 1 and 253 characters")
    if _looks_like_browser_ipv4(normalized):
        raise ValueError("hostname alias must be a DNS hostname, not an IP address")
    if any(HOST_LABEL.fullmatch(label) is None for label in normalized.split(".")):
        raise ValueError("hostname alias contains an invalid DNS label")
    return normalized


def _parse_host_aliases(value: str) -> tuple[str, ...]:
    if value == "":
        return ()
    aliases: list[str] = []
    try:
        for alias in value.split(","):
            normalized = normalize_host_alias(alias)
            if normalized not in aliases:
                aliases.append(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return tuple(aliases)


class RunnerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        runner: PythonRunner | None = None,
        notification_settings: NotificationSettings | None = None,
        host_aliases: tuple[str, ...] = (),
        readiness_service: AgentReadinessService | None = None,
    ) -> None:
        requested_host, _ = server_address
        super().__init__(server_address, RunnerRequestHandler)
        notifier = (
            NotifyCLI.from_environment()
            if runner is None or notification_settings is None
            else None
        )
        self.runner = runner or PythonRunner(notifier=notifier)
        self.notification_settings = notification_settings or NotificationSettings(
            runtime_config=Path(
                os.environ.get("AGENT_WORKFLOW_MANAGER_CONFIG_FILE", "config.sh")
            ),
            notify_config=Path(
                os.environ.get(
                    "NOTIFY_CONFIG", str(Path.home() / ".config/notify/config")
                )
            ),
            notifier=cast(NotifyCLI, notifier),
        )
        self.readiness_service = readiness_service or AgentReadinessService()
        self._settings_notifier = (
            notifier if runner is not None and notification_settings is None else None
        )
        self.request_token = secrets.token_urlsafe(32)
        bound_host, bound_port = cast(tuple[str, int], self.server_address)
        self.allowed_hosts = {
            f"{requested_host}:{bound_port}",
            f"{bound_host}:{bound_port}",
        }
        if bound_host == "127.0.0.1":
            self.allowed_hosts.add(f"localhost:{bound_port}")
        self.host_aliases = frozenset(
            normalize_host_alias(alias) for alias in host_aliases
        )
        self.allowed_hosts.update(
            f"{alias}:{bound_port}" for alias in self.host_aliases
        )

    def is_allowed_host(self, host: str | None) -> bool:
        return self._canonical_authority(host) is not None

    def is_allowed_origin(self, origin: str | None, host: str | None) -> bool:
        if origin is None:
            return True
        if any(ord(character) < 32 or ord(character) == 127 for character in origin):
            return False
        if "?" in origin or "#" in origin:
            return False
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.params
            or parsed.query
            or parsed.fragment
            or parsed.hostname is None
        ):
            return False
        try:
            origin_port = parsed.port
        except ValueError:
            return False
        if origin_port is None:
            return False
        return self._canonical_authority(host) == self._canonical_authority(
            f"{parsed.hostname}:{origin_port}"
        )

    def _canonical_authority(self, authority: str | None) -> str | None:
        if authority in self.allowed_hosts:
            return authority
        if authority is None or authority.count(":") != 1:
            return None
        hostname, port = authority.rsplit(":", 1)
        if port != str(self.server_address[1]):
            return None
        try:
            normalized = normalize_host_alias(hostname)
        except ValueError:
            return None
        if normalized not in self.host_aliases:
            return None
        return f"{normalized}:{port}"

    def server_close(self) -> None:
        runner = getattr(self, "runner", None)
        if runner is not None:
            runner.close()
        settings_notifier = getattr(self, "_settings_notifier", None)
        if settings_notifier is not None:
            settings_notifier.close()
        super().server_close()


class RunnerRequestHandler(BaseHTTPRequestHandler):
    server: RunnerHTTPServer

    def do_GET(self) -> None:
        if not self.server.is_allowed_host(self.headers.get("Host")):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "untrusted host"})
            return
        path = urlparse(self.path).path
        if path == "/api/events":
            self._send_events()
            return
        if path == "/api/token":
            self._send_json(HTTPStatus.OK, {"token": self.server.request_token})
            return
        if path in {"/api/status", "/api/output"}:
            self._send_json(HTTPStatus.OK, self.server.runner.snapshot().as_json())
            return
        if path == "/api/runs":
            self._send_json(
                HTTPStatus.OK,
                {
                    "runs": [
                        run.as_summary_json() for run in self.server.runner.snapshots()
                    ]
                },
            )
            return
        run_match = re.fullmatch(r"/api/runs/([1-9][0-9]*)", path)
        if run_match is not None:
            try:
                snapshot = self.server.runner.snapshot(int(run_match.group(1)))
            except RunNotFoundError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, snapshot.as_json())
            return
        if path == "/api/settings/notifications":
            try:
                settings = self.server.notification_settings.read()
            except SettingsError as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, settings.as_json())
            return
        if path == "/api/readiness":
            try:
                snapshot = self.server.readiness_service.snapshot()
            except TerminalSessionError as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, snapshot)
            return
        static_file = STATIC_FILES.get(path)
        if static_file is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        filename, content_type = static_file
        try:
            content = (STATIC_DIR / filename).read_bytes()
        except OSError:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "static file unavailable"}
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_events(self) -> None:
        revision = self.server.runner.change_revision()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            while True:
                changed = self.server.runner.wait_for_change(revision, timeout=15)
                if changed is None:
                    return
                if changed == revision:
                    content = b": keep-alive\n\n"
                else:
                    revision = changed
                    content = f"event: runner-change\ndata: {revision}\n\n".encode(
                        "ascii"
                    )
                self.wfile.write(content)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        json_paths = {
            "/api/run",
            "/api/validate",
            "/api/dry-run",
            "/api/readiness/probe",
            "/api/readiness/reconcile",
            "/api/settings/notifications",
            "/api/settings/notifications/test",
        }
        if not self._is_trusted_request(require_json=path in json_paths):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "untrusted request"})
            return
        if path in {"/api/run", "/api/validate", "/api/dry-run"}:
            payload = self._read_json()
            if payload is None:
                return
            code = payload.get("code")
            if not isinstance(code, str):
                self._send_json(
                    HTTPStatus.BAD_REQUEST, {"error": "code must be a string"}
                )
                return
            cwd = payload.get("cwd")
            if cwd == "":
                cwd = None
            if cwd is not None and not isinstance(cwd, str):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "cwd must be a string or null"},
                )
                return
            args = payload.get("args", [])
            if not isinstance(args, list) or any(
                not isinstance(argument, str) for argument in args
            ):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "args must be an array of strings"},
                )
                return
            try:
                if path == "/api/validate":
                    result = self.server.runner.validate(code, cwd=cwd, args=args)
                    self._send_json(
                        HTTPStatus.OK
                        if result.valid
                        else HTTPStatus.UNPROCESSABLE_ENTITY,
                        self.server.runner.validation_snapshot().as_json(),
                    )
                    return
                if path == "/api/dry-run":
                    result = self.server.runner.dry_run(code, cwd=cwd, args=args)
                    status = (
                        HTTPStatus.OK
                        if result.status in {"frontier", "complete"}
                        else HTTPStatus.UNPROCESSABLE_ENTITY
                    )
                    self._send_json(
                        status, self.server.runner.validation_snapshot().as_json()
                    )
                    return
                run_id = self.server.runner.start(code, cwd=cwd, args=args)
            except InvalidExecutionContextError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except WorkflowValidationError:
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {
                        "error": "workflow validation failed",
                        **self.server.runner.validation_snapshot().as_json(),
                    },
                )
                return
            except WorkflowDryRunError:
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {
                        "error": "workflow is not eligible for Dry Run",
                        **self.server.runner.validation_snapshot().as_json(),
                    },
                )
                return
            except AlreadyRunningError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            self._send_json(
                HTTPStatus.ACCEPTED,
                {"runId": run_id, **self.server.runner.snapshot(run_id).as_json()},
            )
            return
        if path == "/api/readiness/probe":
            payload = self._read_json()
            if payload is None:
                return
            workspace_id = payload.get("workspaceId")
            provider = payload.get("provider")
            if not isinstance(workspace_id, str) or not isinstance(provider, str):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "workspaceId and provider must be strings"},
                )
                return
            try:
                result = self.server.readiness_service.probe(
                    workspace_id=workspace_id, provider=provider
                )
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except ReadinessProbeBusy as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            except ReadinessReconciliationRequired as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            except TerminalSessionError as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, {"probe": result.as_json()})
            return
        if path == "/api/readiness/reconcile":
            payload = self._read_json()
            if payload is None:
                return
            if payload:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "readiness reconciliation request must be empty"},
                )
                return
            try:
                result = self.server.readiness_service.reconcile()
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except ReadinessProbeBusy as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            except TerminalSessionError as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, {"probe": result.as_json()})
            return
        if path == "/api/stop":
            stopped = self.server.runner.stop()
            self._send_json(
                HTTPStatus.ACCEPTED if stopped else HTTPStatus.CONFLICT,
                {
                    "stopped": stopped,
                    **self.server.runner.snapshot().as_json(),
                },
            )
            return
        stop_match = re.fullmatch(r"/api/runs/([1-9][0-9]*)/stop", path)
        if stop_match is not None:
            run_id = int(stop_match.group(1))
            try:
                stopped = self.server.runner.stop(run_id)
                snapshot = self.server.runner.snapshot(run_id)
            except RunNotFoundError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            self._send_json(
                HTTPStatus.ACCEPTED if stopped else HTTPStatus.CONFLICT,
                {"stopped": stopped, **snapshot.as_json()},
            )
            return
        resume_match = re.fullmatch(r"/api/runs/([1-9][0-9]*)/resume", path)
        if resume_match is not None:
            run_id = int(resume_match.group(1))
            try:
                self.server.runner.resume(run_id)
                snapshot = self.server.runner.snapshot(run_id)
            except RunNotFoundError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            except RunNotResumableError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            except InvalidExecutionContextError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except WorkflowValidationError as exc:
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {
                        "error": "workflow validation failed before resume",
                        "validation": [issue.as_json() for issue in exc.result.issues],
                    },
                )
                return
            self._send_json(HTTPStatus.ACCEPTED, snapshot.as_json())
            return
        cleanup_match = re.fullmatch(r"/api/runs/([1-9][0-9]*)/cleanup", path)
        if cleanup_match is not None:
            run_id = int(cleanup_match.group(1))
            try:
                snapshot = self.server.runner.cleanup(run_id)
            except RunNotFoundError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            except (RunCleanupInProgressError, RunCleanupNotAllowedError) as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, snapshot.as_json())
            return
        if path == "/api/settings/notifications":
            payload = self._read_json()
            if payload is None:
                return
            try:
                settings = self.server.notification_settings.update(payload)
            except SettingsValidationError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except SettingsError as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, settings.as_json())
            return
        if path == "/api/settings/notifications/test":
            payload = self._read_json()
            if payload is None:
                return
            if payload:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "test notification request must be empty"},
                )
                return
            try:
                result = self.server.notification_settings.send_test()
            except SettingsError as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            if result.delivered:
                self._send_json(HTTPStatus.OK, result.as_json())
            else:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": result.message})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _is_trusted_request(self, *, require_json: bool) -> bool:
        host = self.headers.get("Host")
        if not self.server.is_allowed_host(host):
            return False
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if require_json and content_type != "application/json":
            return False
        if self.headers.get("X-Python-Runner-Token") != self.server.request_token:
            return False
        return self.server.is_allowed_origin(self.headers.get("Origin"), host)

    def _read_json(self) -> dict[str, Any] | None:
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            content_length = -1
        if content_length < 0 or content_length > 1_000_000:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return None
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return None
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
            return None
        return payload

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        content = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        return


def _parse_bind_host(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise argparse.ArgumentTypeError(
            "host must be an explicit IPv4 interface address"
        ) from exc
    if address.is_unspecified:
        raise argparse.ArgumentTypeError("host must not be the wildcard 0.0.0.0")
    return str(address)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trusted Python runner UI")
    parser.add_argument("--host", default="127.0.0.1", type=_parse_bind_host)
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument(
        "--host-aliases",
        default=(),
        type=_parse_host_aliases,
        help="comma-separated exact trusted browser hostnames",
    )
    parser.add_argument(
        "--runtime-config",
        default=Path(os.environ.get("AGENT_WORKFLOW_MANAGER_CONFIG_FILE", "config.sh")),
        type=Path,
    )
    parser.add_argument(
        "--notify-config",
        default=Path(
            os.environ.get("NOTIFY_CONFIG", str(Path.home() / ".config/notify/config"))
        ),
        type=Path,
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    notifier = NotifyCLI.from_environment()
    notifier.config_path = str(args.notify_config)
    notification_settings = NotificationSettings(
        runtime_config=args.runtime_config,
        notify_config=args.notify_config,
        notifier=notifier,
    )
    server = RunnerHTTPServer(
        (args.host, args.port),
        runner=PythonRunner(notifier=notifier),
        notification_settings=notification_settings,
        host_aliases=args.host_aliases,
    )
    print(f"Python Runner UI: http://{args.host}:{args.port}")
    print("Trusted-network use only: this server executes arbitrary Python code.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
