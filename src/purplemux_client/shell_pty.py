"""Run a managed shell on terminal streams and retain bounded output."""

from __future__ import annotations

import codecs
import errno
import fcntl
import json
import os
import pty
import select
import subprocess
import sys
import termios
from collections import deque
from pathlib import Path


class _Stream:
    def __init__(self, path: Path, destination: int, max_chars: int) -> None:
        self.path = path
        self.destination = destination
        self.max_chars = max_chars
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.tail: deque[str] = deque()
        self.length = 0

    def retain(self, data: bytes, *, final: bool = False) -> None:
        self.tail.append(self.decoder.decode(data, final=final))
        self.length += len(self.tail[-1])
        while self.length > self.max_chars:
            overflow = self.length - self.max_chars
            first = self.tail[0]
            if len(first) <= overflow:
                self.length -= len(self.tail.popleft())
            else:
                self.tail[0] = first[overflow:]
                self.length -= overflow

    def publish(self) -> None:
        self.retain(b"", final=True)
        pending = self.path.with_name(f"{self.path.name}.pending")
        with pending.open("w", encoding="utf-8") as stream:
            stream.writelines(self.tail)
        os.replace(pending, self.path)


def _terminal_pair(source_fd: int) -> tuple[int, int]:
    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[1] &= ~termios.ONLCR
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    try:
        size = fcntl.ioctl(source_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
    except OSError:
        pass
    return master, slave


def run(cwd: str, command: str, result_path: Path, max_chars: int) -> None:
    stdout_master, stdout_slave = _terminal_pair(1)
    stderr_master, stderr_slave = _terminal_pair(2)
    try:
        child = subprocess.Popen(
            ["bash", "-lc", command],
            cwd=cwd,
            stdout=stdout_slave,
            stderr=stderr_slave,
        )
    finally:
        os.close(stdout_slave)
        os.close(stderr_slave)

    streams = {
        stdout_master: _Stream(Path(f"{result_path}.stdout"), 1, max_chars),
        stderr_master: _Stream(Path(f"{result_path}.stderr"), 2, max_chars),
    }
    try:
        while streams:
            ready, _, _ = select.select(list(streams), [], [], 0.05)
            for fd in ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    os.close(fd)
                    streams.pop(fd).publish()
                    continue
                stream = streams[fd]
                remaining = memoryview(chunk)
                while remaining:
                    remaining = remaining[os.write(stream.destination, remaining) :]
                stream.retain(chunk)
            if not ready and child.poll() is not None:
                # A detached descendant can retain a slave fd indefinitely.
                break
        for fd, stream in streams.items():
            os.close(fd)
            stream.publish()
        exit_code = child.wait()
    finally:
        for fd in streams:
            try:
                os.close(fd)
            except OSError:
                pass
    pending = result_path.with_name(f"{result_path.name}.pending")
    pending.write_text(json.dumps({"exitCode": exit_code}) + "\n", encoding="utf-8")
    os.replace(pending, result_path)


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]))
