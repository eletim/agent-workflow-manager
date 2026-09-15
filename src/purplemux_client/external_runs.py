"""Server-side client for ordinary Runs on registered external AWMs."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from http.client import HTTPException
from urllib import error, request

from purplemux_client.external_targets import ExternalTargetSettings

# Two default million-character streams, up to 12 JSON bytes per Unicode
# character (surrogate pairs), plus truncation notices and envelope overhead.
_MAX_RESPONSE_BYTES = 24_000_000 + 65_536


class ExternalRunError(RuntimeError):
    """The external request failed or its response cannot be trusted."""


class ExternalRunLaunchUnknown(ExternalRunError):
    """A launch may have executed. Inspect the destination; do not retry blindly."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class ExternalRunResult:
    run_id: int
    state: str
    exit_code: int
    stdout: str
    stderr: str


def _positive_id(value: object) -> bool:
    return type(value) is int and value > 0


def _duration(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be finite and positive")


class ExternalRunClient:
    """Resolve registrations and credentials locally; never follow redirects or retry launches."""

    def __init__(
        self,
        settings: ExternalTargetSettings | None = None,
        *,
        request_timeout: float = 30,
    ) -> None:
        _duration(request_timeout)
        self.settings = settings if settings is not None else ExternalTargetSettings()
        self.request_timeout = request_timeout
        self._opener = request.build_opener(_NoRedirect())
        self._destinations: dict[str, str] = {}

    def _request(
        self,
        target_id: str,
        path: str,
        payload: dict | None = None,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> dict:
        connection = self.settings.connection(target_id)
        destination = self._destinations.setdefault(target_id, connection.destination)
        if connection.destination != destination:
            raise ExternalRunError(
                "external target destination changed; outcome unknown"
            )
        launching = payload is not None
        failure = ExternalRunLaunchUnknown if launching else ExternalRunError
        message = request.Request(
            connection.destination + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={**connection.headers, "Content-Type": "application/json"},
            method="POST" if launching else "GET",
        )
        try:
            with self._opener.open(
                message, timeout=timeout or self.request_timeout
            ) as response:
                if (
                    response.status != (202 if launching else 200)
                    or response.headers.get_content_type() != "application/json"
                ):
                    raise ValueError("unexpected response")
                body = bytearray()
                while True:
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("external Run result deadline expired")
                        # urllib returns an HTTPResponse backed by SocketIO for
                        # both HTTP and HTTPS. Limit each read to the time left;
                        # read1 returns available data rather than waiting to fill.
                        if response.fp is not None:
                            response.fp.raw._sock.settimeout(
                                min(self.request_timeout, remaining)
                            )
                    chunk = response.read1(
                        min(65_536, _MAX_RESPONSE_BYTES + 1 - len(body))
                    )
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("external Run result deadline expired")
                    if not chunk:
                        if response.length not in {None, 0}:
                            raise ValueError("incomplete response")
                        break
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise ValueError("response too large")
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError("expected object")
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("external Run result deadline expired")
                return value
        except error.HTTPError as exc:
            # Only explicit pre-execution rejections establish that no Run launched.
            if launching and exc.code in {400, 403, 404, 409, 422}:
                raise ExternalRunError(
                    f"external launch rejected (HTTP {exc.code})"
                ) from None
            raise failure(
                f"external request failed (HTTP {exc.code}); outcome unknown"
            ) from None
        except TimeoutError as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("external Run result deadline expired") from None
            raise failure("external response timed out; outcome unknown") from exc
        except (OSError, ValueError, HTTPException, error.URLError) as exc:
            raise failure(
                "external response unavailable or malformed; outcome unknown"
            ) from exc

    def start_run(self, target_id: str, code: str, *, args: Sequence[str] = ()) -> int:
        """Launch once. An unknown outcome requires inspection at the destination."""
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must be a non-empty string")
        if isinstance(args, str) or any(not isinstance(arg, str) for arg in args):
            raise ValueError("args must be a sequence of strings")
        value = self._request(target_id, "/api/run", {"code": code, "args": list(args)})
        if (
            not _positive_id(value.get("runId"))
            or not isinstance(value.get("state"), str)
            or value.get("state")
            not in {
                "running",
                "success",
                "failed",
                "stopped",
            }
        ):
            raise ExternalRunLaunchUnknown("malformed launch response; outcome unknown")
        return value["runId"]

    def get_run_result(
        self,
        target_id: str,
        run_id: int,
        *,
        _timeout: float | None = None,
        _deadline: float | None = None,
    ) -> ExternalRunResult | None:
        """Return None only for a confirmed running Run; errors remain exceptions."""
        if not _positive_id(run_id):
            raise ValueError("run_id must be a positive integer")
        value = self._request(
            target_id,
            f"/api/runs/{run_id}/result",
            timeout=_timeout,
            deadline=_deadline,
        )
        state, result = value.get("state"), value.get("result")
        if (
            value.get("runId") != run_id
            or not _positive_id(value.get("runId"))
            or "result" not in value
        ):
            raise ExternalRunError("malformed result response; outcome unknown")
        if state == "running" and result is None:
            return None
        if (
            not isinstance(state, str)
            or state not in {"success", "failed", "stopped"}
            or not isinstance(result, dict)
        ):
            raise ExternalRunError("unknown terminal result")
        exit_code = result.get("exitCode")
        if (
            type(exit_code) is not int
            or not isinstance(result.get("stdout"), str)
            or not isinstance(result.get("stderr"), str)
            or (state == "success" and exit_code != 0)
        ):
            raise ExternalRunError("malformed terminal result")
        return ExternalRunResult(
            run_id, state, exit_code, result["stdout"], result["stderr"]
        )

    def wait_run(
        self, target_id: str, run_id: int, *, timeout: float = 300
    ) -> ExternalRunResult:
        """Poll confirmed running states within a deadline; never retry observation failures."""
        _duration(timeout)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("external Run result is still unknown")
            result = self.get_run_result(
                target_id,
                run_id,
                _timeout=min(self.request_timeout, remaining),
                _deadline=deadline,
            )
            if time.monotonic() >= deadline:
                raise TimeoutError("external Run result is still unknown")
            if result is not None:
                return result
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
