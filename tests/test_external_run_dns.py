from __future__ import annotations

import asyncio
import time

import pytest

from purplemux_client import external_run_dns


@pytest.fixture
def resolver_loop(monkeypatch):
    processes = []
    original_spawn = asyncio.create_subprocess_exec

    async def track_process(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track_process)
    loop = external_run_dns.ResolverEventLoop()
    asyncio.set_event_loop(loop)
    try:
        yield loop, processes
    finally:
        loop.run_until_complete(asyncio.wait_for(loop.shutdown_resolvers(), timeout=2))
        asyncio.set_event_loop(None)
        loop.close()
        assert all(process.returncode is not None for process in processes)


def test_fragmented_resolver_json_is_read_through_eof(resolver_loop, monkeypatch):
    loop, processes = resolver_loop
    monkeypatch.setattr(
        external_run_dns,
        "_RESOLVER_SCRIPT",
        """
import json, sys, time
body = json.dumps({"addresses": [[2, 1, 6, "", ["127.0.0.1", 8765]]]})
for chunk in (body[:5], body[5:20], body[20:]):
    sys.stdout.write(chunk)
    sys.stdout.flush()
    time.sleep(0.02)
""",
    )
    result = loop.run_until_complete(
        asyncio.wait_for(loop.getaddrinfo("localhost", 8765), timeout=2)
    )
    assert result == [(2, 1, 6, "", ("127.0.0.1", 8765))]
    assert len(processes) == 1
    assert processes[0].returncode == 0


@pytest.mark.parametrize("output_size", [65_537, 1_000_000])
def test_oversized_resolver_output_is_bounded_and_reaped(
    output_size, resolver_loop, monkeypatch
):
    loop, processes = resolver_loop
    monkeypatch.setattr(
        external_run_dns,
        "_RESOLVER_SCRIPT",
        f"""
import sys, time
sys.stdout.write("x" * {output_size})
sys.stdout.flush()
time.sleep(3600)
""",
    )
    started = time.monotonic()
    with pytest.raises(OSError, match="response too large"):
        loop.run_until_complete(
            asyncio.wait_for(loop.getaddrinfo("localhost", 8765), timeout=2)
        )
    loop.run_until_complete(asyncio.wait_for(loop.shutdown_resolvers(), timeout=2))
    assert time.monotonic() - started < 2
    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert processes[0].returncode < 0


def test_resolver_output_at_limit_is_accepted(resolver_loop, monkeypatch):
    loop, processes = resolver_loop
    monkeypatch.setattr(
        external_run_dns,
        "_RESOLVER_SCRIPT",
        """
import json, sys
body = json.dumps({"addresses": [[2, 1, 6, "", ["127.0.0.1", 8765]]]})
sys.stdout.write(body + " " * (65536 - len(body)))
""",
    )
    result = loop.run_until_complete(
        asyncio.wait_for(loop.getaddrinfo("localhost", 8765), timeout=2)
    )
    assert result == [(2, 1, 6, "", ("127.0.0.1", 8765))]
    assert processes[0].returncode == 0
