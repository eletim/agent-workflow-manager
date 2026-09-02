from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    MutationOutcomeUnknown,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    SessionReadyTimeout,
    WorkerFailure,
)


class RuntimeRunner:
    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.calls: list[list[str]] = []
        self.tabs: dict[str, dict[str, object]] = {}
        self.workspaces: dict[str, dict[str, object]] = {}

    def __call__(
        self,
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        command = list(args)
        self.calls.append(command)
        if command[1:] == ["workspaces"]:
            return self.done({"workspaces": list(self.workspaces.values())})
        if command[1:3] == ["tab", "list"]:
            if self.mode == "incomplete":
                return self.done({"unexpected": []})
            if self.mode == "postcondition-read-failure" and any(
                call[1:3] in (["tab", "create"], ["tab", "close"])
                for call in self.calls[:-1]
            ):
                return self.done({"unexpected": []})
            return self.done({"tabs": list(self.tabs.values())})
        if command[1:3] == ["workspace", "create"]:
            workspace: dict[str, object] = {
                "id": "ws-new",
                "name": command[command.index("--name") + 1],
                "directories": [command[command.index("--cwd") + 1]],
            }
            self.workspaces["ws-new"] = workspace
            if self.mode == "workspace-timeout-after-apply":
                raise subprocess.TimeoutExpired(command, timeout)
            return self.done(workspace)
        if command[1:3] == ["tab", "create"]:
            name = command[command.index("-n") + 1]
            panel_type = command[command.index("-t") + 1]
            tab_id = "tab-response"
            state_id = "tab-listed" if self.mode == "response-mismatch" else tab_id
            state: dict[str, object] = {
                "tabId": state_id,
                "workspaceId": "ws-test",
                "name": name,
                "panelType": panel_type,
                "agentProviderId": "codex" if panel_type == "codex-cli" else None,
            }
            self.tabs[state_id] = state
            if self.mode == "duplicate-after-apply":
                self.tabs["tab-second"] = {**state, "tabId": "tab-second"}
                raise subprocess.TimeoutExpired(command, timeout)
            if self.mode == "timeout-after-apply":
                raise subprocess.TimeoutExpired(command, timeout)
            if self.mode == "timeout-unchanged":
                self.tabs.clear()
                raise subprocess.TimeoutExpired(command, timeout)
            return self.done({"tabId": tab_id})
        if command[1:3] == ["tab", "status"]:
            return self.done(
                {"tabId": command[-1], "cliState": "idle", "alive": True, "eventSeq": 1}
            )
        if command[1:3] == ["tab", "close"]:
            if self.mode == "close-timeout":
                raise subprocess.TimeoutExpired(command, timeout)
            self.tabs.pop(command[-1], None)
            return self.done({"status": "closed"})
        raise AssertionError(command)

    @staticmethod
    def done(value: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, json.dumps(value), "")


def request() -> CreateSessionRequest:
    return CreateSessionRequest(
        "codex", "/repo", "codex", name="probe-corr-1", correlation_id="corr-1"
    )


def test_authoritative_tab_listing_rejects_incomplete_response() -> None:
    with pytest.raises(WorkerFailure, match="incomplete"):
        PurpleMuxCLIClient(
            "ws-test", runner=RuntimeRunner("incomplete")
        ).list_sessions()


def test_lost_create_response_reconciles_one_exact_new_tab_without_retry() -> None:
    runner = RuntimeRunner("timeout-after-apply")
    tab = PurpleMuxCLIClient("ws-test", runner=runner).create_session(request())

    assert tab == "tab-response"
    assert len([call for call in runner.calls if call[1:3] == ["tab", "create"]]) == 1


def test_concurrent_unrelated_tab_does_not_corrupt_exact_create_correlation() -> None:
    runner = RuntimeRunner()
    runner.tabs["unrelated"] = {
        "tabId": "unrelated",
        "workspaceId": "ws-test",
        "name": "someone-else",
        "panelType": "terminal",
        "agentProviderId": None,
    }

    assert PurpleMuxCLIClient("ws-test", runner=runner).create_session(request()) == (
        "tab-response"
    )
    assert set(runner.tabs) == {"unrelated", "tab-response"}


@pytest.mark.parametrize(
    "mode", ["timeout-unchanged", "duplicate-after-apply", "response-mismatch"]
)
def test_create_ambiguity_or_possible_late_completion_is_unknown(mode: str) -> None:
    runner = RuntimeRunner(mode)
    with pytest.raises(MutationOutcomeUnknown):
        PurpleMuxCLIClient("ws-test", runner=runner).create_session(request())
    assert len([call for call in runner.calls if call[1:3] == ["tab", "create"]]) == 1


def test_probe_uses_saved_preexisting_set_structured_readiness_and_exact_cleanup() -> (
    None
):
    runner = RuntimeRunner()
    runner.tabs["old"] = {
        "tabId": "old",
        "workspaceId": "ws-test",
        "name": "other",
        "panelType": "terminal",
        "agentProviderId": None,
    }
    client = PurpleMuxCLIClient("ws-test", runner=runner, poll_interval_seconds=0)
    result = client.probe_agent_readiness(
        provider="codex",
        probe_name="readiness-corr-1",
        correlation_id="corr-1",
        preexisting_tab_ids=("old",),
        timeout_seconds=1,
    )

    assert result.ready and result.cleanup_confirmed
    assert set(runner.tabs) == {"old"}
    assert not any(call[1:3] == ["tab", "capture"] for call in runner.calls)


def test_probe_rejects_stale_preexisting_set_before_creation() -> None:
    runner = RuntimeRunner()
    runner.tabs["concurrent"] = {
        "tabId": "concurrent",
        "workspaceId": "ws-test",
        "name": "other",
        "panelType": "terminal",
        "agentProviderId": None,
    }
    with pytest.raises(WorkerFailure, match="not authoritative"):
        PurpleMuxCLIClient("ws-test", runner=runner).probe_agent_readiness(
            provider="codex",
            probe_name="readiness-corr-1",
            correlation_id="corr-1",
            preexisting_tab_ids=(),
            timeout_seconds=1,
        )
    assert not any(call[1:3] == ["tab", "create"] for call in runner.calls)


def test_probe_close_uncertainty_retains_exact_tab_identity() -> None:
    runner = RuntimeRunner("close-timeout")
    client = PurpleMuxCLIClient("ws-test", runner=runner, poll_interval_seconds=0)
    with pytest.raises(MutationOutcomeUnknown, match="tab-response.*retained"):
        client.probe_agent_readiness(
            provider="codex",
            probe_name="readiness-corr-1",
            correlation_id="corr-1",
            preexisting_tab_ids=(),
            timeout_seconds=1,
        )
    assert "tab-response" in runner.tabs


def test_probe_refuses_to_close_a_changed_tab_identity() -> None:
    runner = RuntimeRunner()
    client = PurpleMuxCLIClient("ws-test", runner=runner, poll_interval_seconds=0)
    original_status = runner.__call__

    def replace_when_ready(
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        command = list(args)
        result = original_status(
            args,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
        )
        if command[1:3] == ["tab", "status"]:
            runner.tabs["tab-response"]["name"] = "replacement"
        return result

    client._runner = replace_when_ready
    with pytest.raises(MutationOutcomeUnknown, match="retained.*identity changed"):
        client.probe_agent_readiness(
            provider="codex",
            probe_name="readiness-corr-1",
            correlation_id="corr-1",
            preexisting_tab_ids=(),
            timeout_seconds=1,
        )
    assert runner.tabs["tab-response"]["name"] == "replacement"
    assert not any(call[1:3] == ["tab", "close"] for call in runner.calls)


def test_probe_readiness_failure_still_closes_the_exact_probe() -> None:
    runner = RuntimeRunner()
    client = PurpleMuxCLIClient(
        "ws-test",
        runner=runner,
        poll_interval_seconds=0,
        monotonic=lambda: 1,
    )
    original_status = runner.__call__

    def never_ready(
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        command = list(args)
        if command[1:3] == ["tab", "status"]:
            runner.calls.append(command)
            return runner.done(
                {"tabId": command[-1], "cliState": "starting", "alive": True}
            )
        return original_status(
            args,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
        )

    client._runner = never_ready
    with pytest.raises(SessionReadyTimeout, match="was not ready"):
        client.probe_agent_readiness(
            provider="codex",
            probe_name="readiness-corr-1",
            correlation_id="corr-1",
            preexisting_tab_ids=(),
            timeout_seconds=0,
        )
    assert runner.tabs == {}


def test_postcondition_read_failure_after_close_is_unknown_and_not_retried() -> None:
    runner = RuntimeRunner("postcondition-read-failure")
    runner.tabs["tab-1"] = {
        "tabId": "tab-1",
        "workspaceId": "ws-test",
        "name": "session",
        "panelType": "codex-cli",
        "agentProviderId": "codex",
    }

    with pytest.raises(MutationOutcomeUnknown, match="reconciliation read failed"):
        PurpleMuxCLIClient("ws-test", runner=runner).close_session("tab-1")
    assert len([call for call in runner.calls if call[1:3] == ["tab", "close"]]) == 1


def test_workspace_creation_is_correlated_after_lost_response(tmp_path: Path) -> None:
    runner = RuntimeRunner("workspace-timeout-after-apply")
    runtime = PurpleMuxRuntime(runner=runner)
    result = runtime.create_workspace(
        CreateWorkspaceRequest(str(tmp_path), "Version work", "corr-1")
    )

    assert result.id == "ws-new"
    assert (
        len([call for call in runner.calls if call[1:3] == ["workspace", "create"]])
        == 1
    )
