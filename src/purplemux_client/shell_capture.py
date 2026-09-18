"""Forward one managed shell stream while retaining a bounded output tail."""

from __future__ import annotations

import codecs
import os
import select
import sys
from collections import deque
from pathlib import Path


def capture(
    path: Path, command_done: Path, max_chars: int, destination_fd: int
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    tail: deque[str] = deque()
    retained_chars = 0

    def retain(text: str) -> None:
        nonlocal retained_chars
        if not text:
            return
        tail.append(text)
        retained_chars += len(text)
        while retained_chars > max_chars:
            overflow = retained_chars - max_chars
            first = tail[0]
            if len(first) <= overflow:
                retained_chars -= len(tail.popleft())
            else:
                tail[0] = first[overflow:]
                retained_chars -= overflow

    published = False

    def publish() -> None:
        nonlocal published
        retain(decoder.decode(b"", final=True))
        pending = path.with_name(f"{path.name}.pending")
        with pending.open("w", encoding="utf-8") as stream:
            stream.writelines(tail)
        os.replace(pending, path)
        tail.clear()
        published = True

    def forward() -> bool:
        chunk = os.read(0, 65536)
        if not chunk:
            return False
        remaining = memoryview(chunk)
        while remaining:
            remaining = remaining[os.write(destination_fd, remaining) :]
        if not published:
            retain(decoder.decode(chunk))
        return True

    while True:
        if select.select([0], [], [], 0.05)[0] and not forward():
            if not published:
                publish()
            return
        if not published and command_done.exists():
            # Drain bytes already queued when the command exited. Detached
            # children may keep the pipe open; their later output is forwarded.
            for _ in range(256):
                if not select.select([0], [], [], 0)[0]:
                    break
                if not forward():
                    break
            publish()


if __name__ == "__main__":
    capture(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
