"""Cancellable system DNS for the external Run transport's owned event loop."""

from __future__ import annotations

import asyncio
import json
import socket
import sys

# Run only the system resolver in a helper, without importing application code.
# A thread executing getaddrinfo cannot be stopped when a request is cancelled.
_RESOLVER_SCRIPT = """
import json, socket, sys
try:
    result = socket.getaddrinfo(**json.loads(sys.argv[1]))
except socket.gaierror as exc:
    print(json.dumps({"error": exc.errno}))
else:
    print(json.dumps({"addresses": result}))
"""
_MAX_RESOLVER_BYTES = 65_536


class ResolverEventLoop(asyncio.SelectorEventLoop):
    """Keep system resolver processes owned until shutdown has reaped them."""

    def __init__(self) -> None:
        super().__init__()
        self._resolvers: set[asyncio.subprocess.Process] = set()

    async def getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
        payload = {
            "host": host.decode("ascii") if isinstance(host, bytes) else host,
            "port": port,
            "family": family,
            "type": type,
            "proto": proto,
            "flags": flags,
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _RESOLVER_SCRIPT,
            json.dumps(payload),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._resolvers.add(process)
        try:
            assert process.stdout is not None
            output = bytearray()
            while True:
                chunk = await process.stdout.read(
                    min(4096, _MAX_RESOLVER_BYTES + 1 - len(output))
                )
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > _MAX_RESOLVER_BYTES:
                    raise OSError("external DNS response too large")
            await process.wait()
            if process.returncode != 0:
                raise OSError("external DNS resolver failed")
            value = json.loads(output)
            if "error" in value:
                raise socket.gaierror(value["error"], "external DNS resolution failed")
            return [
                (family, kind, proto, canonical, tuple(address))
                for family, kind, proto, canonical, address in value["addresses"]
            ]
        finally:
            if process.returncode is None:
                process.kill()
            # Reap during loop shutdown outside the HTTP transport's cancellation
            # scope: cancellation can otherwise interrupt the cleanup await too.

    async def shutdown_resolvers(self) -> None:
        for process in self._resolvers:
            if process.returncode is None:
                process.kill()

        async def reap(process: asyncio.subprocess.Process) -> None:
            # A full StreamReader pauses its pipe transport. Drain after killing
            # the producer so unread oversized output cannot block wait(), while
            # keeping cleanup memory bounded too.
            if process.stdout is not None:
                while await process.stdout.read(65_536):
                    pass
            await process.wait()

        await asyncio.gather(*(reap(process) for process in self._resolvers))
        self._resolvers.clear()
