"""Server-side client for ordinary Runs on registered external AWMs."""

from __future__ import annotations

import asyncio
import json
import math
import re
import ssl
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

import httpx

from purplemux_client.external_run_dns import ResolverEventLoop
from purplemux_client.external_targets import ExternalTargetSettings
from purplemux_client.workflow import ChildRunResult as ExternalRunResult

# Two default million-character streams, up to 12 JSON bytes per Unicode
# character (surrogate pairs), plus truncation notices and envelope overhead.
_MAX_RESPONSE_BYTES = 24_000_000 + 65_536


class ExternalRunError(RuntimeError):
    """The external request failed or its response cannot be trusted."""


class ExternalRunLaunchUnknown(ExternalRunError):
    """A launch may have executed. Inspect the destination; do not retry blindly."""


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
        self._tls_context = ssl.create_default_context()
        self._destinations: dict[str, str] = {}
        self.run_identities: dict[tuple[str, int], str] = {}

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

        async def exchange() -> dict:
            transport = httpx.AsyncHTTPTransport(
                verify=self._tls_context, retries=0, trust_env=False
            )
            async with httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                timeout=timeout or self.request_timeout,
            ) as client:
                async with client.stream(
                    "POST" if launching else "GET",
                    connection.destination + path,
                    content=None if payload is None else json.dumps(payload).encode(),
                    headers={
                        **connection.headers,
                        "Content-Type": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if response.status_code != (202 if launching else 200):
                        # Only explicit pre-execution rejections establish that
                        # no Run launched. Redirects are never followed.
                        if launching and response.status_code in {
                            400,
                            403,
                            404,
                            409,
                            422,
                        }:
                            raise ExternalRunError(
                                f"external launch rejected (HTTP {response.status_code})"
                            )
                        raise failure(
                            f"external request failed (HTTP {response.status_code}); outcome unknown"
                        )
                    if (
                        response.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                        != "application/json"
                    ):
                        raise ValueError("unexpected response")
                    body = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=65_536):
                        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise ValueError("response too large")
                        body.extend(chunk)
                    value = json.loads(body)
                    if not isinstance(value, dict):
                        raise ValueError("expected object")
                    return value

        async def timed_exchange() -> dict:
            remaining = timeout or self.request_timeout
            if deadline is not None:
                remaining = min(remaining, deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError("external Run result deadline expired")
            return await asyncio.wait_for(exchange(), timeout=remaining)

        def run_exchange() -> dict:
            loop = ResolverEventLoop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(timed_exchange())
            finally:
                try:
                    tasks = asyncio.all_tasks(loop)
                    for task in tasks:
                        task.cancel()
                    loop.run_until_complete(
                        asyncio.gather(*tasks, return_exceptions=True)
                    )
                    loop.run_until_complete(loop.shutdown_resolvers())
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.run_until_complete(loop.shutdown_default_executor())
                finally:
                    asyncio.set_event_loop(None)
                    loop.close()

        try:
            # Own the loop in a worker even when the caller runs an asyncio loop.
            # All resolver helpers are killed and reaped before this worker exits.
            with ThreadPoolExecutor(max_workers=1) as worker:
                value = worker.submit(run_exchange).result()
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("external Run result deadline expired")
            return value
        except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException) as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("external Run result deadline expired") from None
            raise failure("external response timed out; outcome unknown") from exc
        except (OSError, ValueError, httpx.HTTPError) as exc:
            raise failure(
                "external response unavailable or malformed; outcome unknown"
            ) from exc

    def start_run(
        self,
        target_id: str,
        code: str,
        *,
        args: Sequence[str] = (),
        parent_identity: str | None = None,
        _on_identity: Callable[[int, str], None] | None = None,
    ) -> int:
        """Launch once. An unknown outcome requires inspection at the destination."""
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must be a non-empty string")
        if isinstance(args, str) or any(not isinstance(arg, str) for arg in args):
            raise ValueError("args must be a sequence of strings")
        payload = {"code": code, "args": list(args)}
        if parent_identity is not None:
            payload["parentRun"] = parent_identity
        value = self._request(target_id, "/api/run", payload)
        identity = value.get("identity")
        if (
            _positive_id(value.get("runId"))
            and isinstance(identity, str)
            and re.fullmatch(r"[0-9a-f]{32}-[1-9][0-9]*", identity)
            and int(identity.rsplit("-", 1)[1]) == value["runId"]
        ):
            self.run_identities[target_id, value["runId"]] = identity
            if _on_identity is not None:
                _on_identity(value["runId"], identity)
        elif parent_identity is not None:
            raise ExternalRunLaunchUnknown(
                "external child identity unavailable; outcome unknown"
            )
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
        identity = self.run_identities.get((target_id, run_id))
        if identity is not None and value.get("identity") != identity:
            raise ExternalRunError(
                "external Run identity changed or unavailable; outcome unknown"
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
