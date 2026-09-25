"""Private local transport for Workflow control, separate from progress events."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class WorkflowControlServer:
    def __init__(self, control: Callable[[str, dict], dict]) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 2_000_000:
                        raise ValueError("invalid control request size")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise ValueError("control request must be an object")
                    result = control(self.headers.get("X-AWM-Run-Token", ""), payload)
                    status = 200
                except PermissionError as exc:
                    status, result = 403, {"error": str(exc)}
                except Exception as exc:
                    status, result = (
                        400,
                        {"error": str(exc), "error_type": type(exc).__name__},
                    )
                encoded = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/control"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
