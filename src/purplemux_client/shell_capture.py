"""Forward one managed shell stream while retaining a bounded output tail."""

from __future__ import annotations

import codecs
import os
import sys
from collections import deque
from pathlib import Path


def capture(path: Path, max_chars: int, destination_fd: int) -> None:
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

    while chunk := os.read(0, 65536):
        remaining = memoryview(chunk)
        while remaining:
            remaining = remaining[os.write(destination_fd, remaining) :]
        retain(decoder.decode(chunk))
    retain(decoder.decode(b"", final=True))
    with path.open("w", encoding="utf-8") as stream:
        stream.writelines(tail)


if __name__ == "__main__":
    capture(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
