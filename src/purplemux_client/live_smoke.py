from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from purplemux_client import (
    CreateSessionRequest,
    PurpleMuxCLIClient,
    TerminalSessionError,
    WorkerFailure,
)

EXPECTED_RESULT = "AGENT_WORKFLOW_MANAGER_PURPLEMUX_OK"


def _record(event: str, **details: Any) -> None:
    print(json.dumps({"event": event, **details}, sort_keys=True), flush=True)


def _close_and_verify(client: PurpleMuxCLIClient, session_id: str) -> None:
    client.close_session(session_id)
    try:
        client.read_status(session_id)
    except WorkerFailure as exc:
        if "not found" not in str(exc).lower():
            raise WorkerFailure(
                f"could not verify that session {session_id} disappeared: {exc}"
            ) from exc
        _record("closed", session_id=session_id)
        return
    raise WorkerFailure(f"closed session {session_id} is still visible")


def lifecycle_smoke(
    client: PurpleMuxCLIClient,
    cwd: str,
    ready_timeout: float,
    turn_timeout: float,
) -> None:
    session_id = client.create_session(
        CreateSessionRequest(worker="codex", cwd=cwd, command="codex")
    )
    _record("created", session_id=session_id)
    try:
        client.wait_until_ready(session_id, ready_timeout)
        ready = client.read_status(session_id)
        _record(
            "ready",
            session_id=session_id,
            cli_state=ready.get("cliState"),
            event_seq=ready.get("eventSeq"),
        )
        client.send_input(
            session_id,
            "exactly AGENT_WORKFLOW_MANAGER_PURPLEMUX_OK とだけ返してください",
        )
        client.wait_for_turn_completion(session_id, turn_timeout)
        result = client.read_result(session_id)
        _record("completed", session_id=session_id, result=result)
        if result.strip() != EXPECTED_RESULT:
            raise WorkerFailure(
                f"unexpected structured result: {result!r}; "
                f"expected {EXPECTED_RESULT!r}"
            )
    finally:
        _close_and_verify(client, session_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live lifecycle check for the PurpleMux Python adapter."
    )
    parser.add_argument("mode", choices=("lifecycle",))
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--ready-timeout", type=float, default=60.0)
    parser.add_argument("--turn-timeout", type=float, default=180.0)
    args = parser.parse_args()

    client = PurpleMuxCLIClient(args.workspace, poll_interval_seconds=1.0)
    try:
        lifecycle_smoke(client, args.cwd, args.ready_timeout, args.turn_timeout)
    except TerminalSessionError as exc:
        parser.exit(1, f"live smoke failed: {exc}\n")


if __name__ == "__main__":
    main()
