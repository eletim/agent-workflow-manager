from __future__ import annotations

import argparse
import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from purplemux_client.runner import AlreadyRunningError, PythonRunner

STATIC_DIR = Path(__file__).with_name("web_static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class RunnerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        runner: PythonRunner | None = None,
    ) -> None:
        requested_host, _ = server_address
        super().__init__(server_address, RunnerRequestHandler)
        self.runner = runner or PythonRunner()
        self.request_token = secrets.token_urlsafe(32)
        bound_host, bound_port = cast(tuple[str, int], self.server_address)
        self.allowed_hosts = {
            f"{requested_host}:{bound_port}",
            f"{bound_host}:{bound_port}",
        }
        if bound_host == "127.0.0.1":
            self.allowed_hosts.add(f"localhost:{bound_port}")

    def is_allowed_host(self, host: str | None) -> bool:
        return host in self.allowed_hosts

    def server_close(self) -> None:
        self.runner.close()
        super().server_close()


class RunnerRequestHandler(BaseHTTPRequestHandler):
    server: RunnerHTTPServer

    def do_GET(self) -> None:
        if not self.server.is_allowed_host(self.headers.get("Host")):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "untrusted host"})
            return
        path = urlparse(self.path).path
        if path == "/api/token":
            self._send_json(HTTPStatus.OK, {"token": self.server.request_token})
            return
        if path in {"/api/status", "/api/output"}:
            self._send_json(HTTPStatus.OK, self.server.runner.snapshot().as_json())
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

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._is_trusted_request(require_json=path == "/api/run"):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "untrusted request"})
            return
        if path == "/api/run":
            payload = self._read_json()
            if payload is None:
                return
            code = payload.get("code")
            if not isinstance(code, str):
                self._send_json(
                    HTTPStatus.BAD_REQUEST, {"error": "code must be a string"}
                )
                return
            try:
                run_id = self.server.runner.start(code)
            except AlreadyRunningError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            self._send_json(
                HTTPStatus.ACCEPTED,
                {"runId": run_id, **self.server.runner.snapshot().as_json()},
            )
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
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin == f"http://{host}"

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local trusted Python runner UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    server = RunnerHTTPServer((args.host, args.port))
    print(f"Python Runner UI: http://{args.host}:{args.port}")
    print("Trusted local use only: this server executes arbitrary Python code.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
