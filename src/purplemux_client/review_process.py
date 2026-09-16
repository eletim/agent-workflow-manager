"""Linux process containment for generated read-only Review agents."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path


class ReviewProcessScope:
    """Own a cgroup containing a PurpleMux agent and its descendants."""

    def __init__(self) -> None:
        if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
            raise RuntimeError("Review requires Linux cgroup v2 process containment")
        membership = Path("/proc/self/cgroup").read_text().splitlines()
        current = next(
            (line[3:] for line in membership if line.startswith("0::")), None
        )
        if current is None:
            raise RuntimeError("Review cannot identify its cgroup v2 parent")
        parent = Path("/sys/fs/cgroup") / current.lstrip("/")
        self.path = parent / f"awm-review-{os.getpid()}-{secrets.token_hex(8)}"
        self.membership = "0::/" + self.path.relative_to("/sys/fs/cgroup").as_posix()
        try:
            self.path.mkdir(mode=0o700)
            if not (self.path / "cgroup.kill").exists():
                raise RuntimeError("Review requires cgroup.kill support")
        except BaseException:
            if self.path.exists():
                self.path.rmdir()
            raise

    @staticmethod
    def _pane_pid(cli: str, workspace_id: str, tab_id: str) -> int:
        listed = subprocess.run(
            [cli, "tab", "list", "-w", workspace_id],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        tabs = json.loads(listed.stdout).get("tabs")
        if not isinstance(tabs, list):
            raise RuntimeError("Review tab listing is incomplete")
        matches = [
            item
            for item in tabs
            if isinstance(item, dict) and item.get("tabId") == tab_id
        ]
        if len(matches) != 1:
            raise RuntimeError("Review agent tab is missing or ambiguous")
        session = matches[0].get("sessionName")
        if not isinstance(session, str) or not re.fullmatch(
            r"pt-[A-Za-z0-9_-]+", session
        ):
            raise RuntimeError("Review agent tab has no valid tmux session identity")
        pane = subprocess.run(
            [
                "tmux",
                "-L",
                "purple",
                "display-message",
                "-p",
                "-t",
                session,
                "#{pane_pid}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        value = pane.stdout.strip()
        if not value.isdecimal() or int(value) <= 1:
            raise RuntimeError("Review agent pane has no valid process ID")
        return int(value)

    @staticmethod
    def _process_tree(root: int) -> list[tuple[int, int]]:
        processes: dict[int, tuple[int, int]] = {}
        for item in Path("/proc").iterdir():
            if not item.name.isdecimal():
                continue
            try:
                content = (item / "stat").read_text()
                fields = content.rsplit(") ", 1)[1].split()
                processes[int(item.name)] = (int(fields[1]), int(fields[19]))
            except (FileNotFoundError, PermissionError, IndexError, ValueError):
                continue
        result: list[tuple[int, int]] = []
        pending = [root]
        while pending:
            pid = pending.pop()
            if pid not in processes:
                if pid == root:
                    raise RuntimeError("Review agent pane process disappeared")
                continue
            result.append((pid, processes[pid][1]))
            pending.extend(
                child for child, (parent, _) in processes.items() if parent == pid
            )
        return result

    def attach(self, cli: str, workspace_id: str, tab_id: str) -> None:
        """Move the pane and existing descendants before sending agent input."""
        if shutil.which("tmux") is None:
            raise RuntimeError("Review containment requires tmux")
        root = self._pane_pid(cli, workspace_id, tab_id)
        self._attach_root(root)

    def _attach_root(self, root: int) -> None:
        for _ in range(5):
            for pid, start_time in self._process_tree(root):
                proc = Path("/proc") / str(pid)
                try:
                    if proc.stat().st_uid != os.getuid():
                        raise RuntimeError("Review agent process has unexpected owner")
                    fields = (proc / "stat").read_text().rsplit(") ", 1)[1].split()
                    if int(fields[19]) != start_time:
                        continue
                    (self.path / "cgroup.procs").write_text(str(pid))
                except FileNotFoundError:
                    if pid == root:
                        raise RuntimeError(
                            "Review agent pane process disappeared"
                        ) from None
            remaining = []
            for pid, _ in self._process_tree(root):
                try:
                    membership = (
                        (Path("/proc") / str(pid) / "cgroup").read_text().splitlines()
                    )
                except FileNotFoundError:
                    continue
                if self.membership not in membership:
                    remaining.append(pid)
            if not remaining:
                return
        raise RuntimeError("Review could not contain every agent process")

    def stop(self) -> None:
        """Kill all descendants, including detached children, before verification."""
        if not self.path.exists():
            return
        (self.path / "cgroup.kill").write_text("1")
        deadline = time.monotonic() + 10
        while (
            "populated 0" not in (self.path / "cgroup.events").read_text().splitlines()
        ):
            if time.monotonic() >= deadline:
                raise RuntimeError("Review agent process scope did not stop")
            time.sleep(0.05)
        self.path.rmdir()
