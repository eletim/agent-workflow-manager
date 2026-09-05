from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    MutationOutcomeUnknown,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    SessionReadyTimeout,
    TabState,
    WorkerFailure,
    WorkspaceState,
)
from purplemux_client.correlation import RUN_IDENTITY_ENV


class RuntimeRunner:
    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.calls: list[list[str]] = []
        self.tabs: dict[str, dict[str, object]] = {}
        self.workspaces: dict[str, dict[str, object]] = {}
        self.sent: list[str] = []

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
            if self.mode == "workspace-nonzero-after-apply":
                return self.failed("workspace create failed after apply")
            return self.done(workspace)
        if command[1:3] == ["workspace", "delete"]:
            workspace_id = command[command.index("-w") + 1]
            if self.mode == "workspace-delete-concurrent-tab":
                self.tabs["concurrent"] = {
                    "tabId": "concurrent",
                    "workspaceId": workspace_id,
                    "name": "Concurrent",
                    "panelType": "terminal",
                    "agentProviderId": None,
                }
            if any(
                tab.get("workspaceId") == workspace_id for tab in self.tabs.values()
            ):
                return subprocess.CompletedProcess(
                    [],
                    3,
                    json.dumps({"status": "not-empty", "workspaceId": workspace_id}),
                    "",
                )
            if self.mode == "workspace-delete-generic-nonzero":
                return self.failed("server failure")
            if self.mode in {
                "workspace-delete-already-absent",
                "workspace-delete-already-absent-nonzero",
            }:
                self.workspaces.pop(workspace_id, None)
                return subprocess.CompletedProcess(
                    [],
                    0 if self.mode == "workspace-delete-already-absent" else 1,
                    json.dumps(
                        {"status": "already-absent", "workspaceId": workspace_id}
                    ),
                    "",
                )
            if self.workspaces.pop(workspace_id, None) is None:
                return self.failed("workspace not found")
            if self.mode == "workspace-delete-timeout-after-apply":
                raise subprocess.TimeoutExpired(command, 30)
            if self.mode == "workspace-delete-nonzero-after-apply":
                return self.failed("server response lost after dispatch")
            return self.done({"status": "deleted", "workspaceId": workspace_id})
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
            if self.mode == "create-nonzero-after-apply":
                return self.failed("create failed after apply")
            if self.mode == "timeout-unchanged":
                self.tabs.clear()
                raise subprocess.TimeoutExpired(command, timeout)
            return self.done({"tabId": tab_id})
        if command[1:3] == ["tab", "status"]:
            return self.done(
                {"tabId": command[-1], "cliState": "idle", "alive": True, "eventSeq": 1}
            )
        if command[1:3] == ["tab", "result"]:
            return self.done({"status": "not-ready"})
        if command[1:3] == ["tab", "send"]:
            self.sent.append(command[-1])
            if self.mode == "send-nonzero-after-apply":
                return self.failed("send failed after apply")
            return self.done({"status": "sent"})
        if command[1:3] == ["tab", "close"]:
            if self.mode == "close-timeout":
                raise subprocess.TimeoutExpired(command, timeout)
            self.tabs.pop(command[-1], None)
            if self.mode == "close-nonzero-after-apply":
                return self.failed("close failed after apply")
            return self.done({"status": "closed"})
        raise AssertionError(command)

    @staticmethod
    def done(value: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, json.dumps(value), "")

    @staticmethod
    def failed(message: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", message)


def request() -> CreateSessionRequest:
    return CreateSessionRequest(
        "codex", "/repo", "codex", name="probe-corr-1", correlation_id="corr-1"
    )


def topology_client(runner: RuntimeRunner) -> PurpleMuxCLIClient:
    runner.workspaces.setdefault(
        "ws-test", {"id": "ws-test", "name": "Test", "directories": ["/repo"]}
    )
    return PurpleMuxCLIClient(
        "ws-test",
        runner=runner,
        codex_project_truster=lambda path: path,
    )


def probe_client(
    runner: RuntimeRunner, *, monotonic: Callable[[], float] = time.monotonic
) -> PurpleMuxCLIClient:
    runner.workspaces.setdefault(
        "ws-test", {"id": "ws-test", "name": "Test", "directories": ["/repo"]}
    )
    return PurpleMuxCLIClient(
        "ws-test",
        runner=runner,
        poll_interval_seconds=0,
        codex_project_truster=lambda path: path,
        monotonic=monotonic,
    )


def test_authoritative_tab_listing_rejects_incomplete_response() -> None:
    with pytest.raises(WorkerFailure, match="incomplete"):
        PurpleMuxCLIClient(
            "ws-test", runner=RuntimeRunner("incomplete")
        ).list_sessions()


def test_lost_create_response_reconciles_one_exact_new_tab_without_retry() -> None:
    runner = RuntimeRunner("timeout-after-apply")
    tab = topology_client(runner).create_session(request())

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

    assert topology_client(runner).create_session(request()) == ("tab-response")
    assert set(runner.tabs) == {"unrelated", "tab-response"}


@pytest.mark.parametrize(
    "mode", ["timeout-unchanged", "duplicate-after-apply", "response-mismatch"]
)
def test_create_ambiguity_or_possible_late_completion_is_unknown(mode: str) -> None:
    runner = RuntimeRunner(mode)
    with pytest.raises(MutationOutcomeUnknown):
        topology_client(runner).create_session(request())
    assert len([call for call in runner.calls if call[1:3] == ["tab", "create"]]) == 1


def test_nonzero_create_after_apply_reconciles_without_retry() -> None:
    runner = RuntimeRunner("create-nonzero-after-apply")

    assert topology_client(runner).create_session(request()) == ("tab-response")
    assert len([call for call in runner.calls if call[1:3] == ["tab", "create"]]) == 1


def test_nonzero_send_after_apply_remains_unknown_without_retry() -> None:
    runner = RuntimeRunner("send-nonzero-after-apply")
    client = PurpleMuxCLIClient("ws-test", runner=runner)

    with pytest.raises(MutationOutcomeUnknown):
        client.send_input("tab-1", "work")
    assert runner.sent == ["work"]
    assert len([call for call in runner.calls if call[1:3] == ["tab", "send"]]) == 1


def test_nonzero_close_after_apply_reconciles_without_retry() -> None:
    runner = RuntimeRunner("close-nonzero-after-apply")
    runner.tabs["tab-1"] = {
        "tabId": "tab-1",
        "workspaceId": "ws-test",
        "name": "session",
        "panelType": "codex-cli",
        "agentProviderId": "codex",
    }

    PurpleMuxCLIClient("ws-test", runner=runner).close_session("tab-1")
    assert "tab-1" not in runner.tabs
    assert len([call for call in runner.calls if call[1:3] == ["tab", "close"]]) == 1


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
    client = probe_client(runner)
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


def test_probe_trusts_only_current_first_workspace_directory() -> None:
    runner = RuntimeRunner()
    runner.workspaces["ws-test"] = {
        "id": "ws-test",
        "name": "Test",
        "directories": ["/repo", "/secondary"],
    }
    trusted: list[str] = []
    client = PurpleMuxCLIClient(
        "ws-test",
        runner=runner,
        poll_interval_seconds=0,
        codex_project_truster=lambda path: trusted.append(path) or path,
    )

    client.probe_agent_readiness(
        provider="codex",
        probe_name="readiness-corr-1",
        correlation_id="corr-1",
        preexisting_tab_ids=(),
        timeout_seconds=1,
    )

    assert trusted == ["/repo"]


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
        probe_client(runner).probe_agent_readiness(
            provider="codex",
            probe_name="readiness-corr-1",
            correlation_id="corr-1",
            preexisting_tab_ids=(),
            timeout_seconds=1,
        )
    assert not any(call[1:3] == ["tab", "create"] for call in runner.calls)


def test_probe_close_uncertainty_retains_exact_tab_identity() -> None:
    runner = RuntimeRunner("close-timeout")
    client = probe_client(runner)
    with pytest.raises(MutationOutcomeUnknown, match="tab-response.*retained"):
        client.probe_agent_readiness(
            provider="codex",
            probe_name="readiness-corr-1",
            correlation_id="corr-1",
            preexisting_tab_ids=(),
            timeout_seconds=1,
        )
    assert "tab-response" in runner.tabs
    assert len([call for call in runner.calls if call[1:3] == ["tab", "close"]]) == 1


def test_probe_refuses_to_close_a_changed_tab_identity() -> None:
    runner = RuntimeRunner()
    client = probe_client(runner)
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


def test_probe_cleanup_ignores_readiness_state_changes() -> None:
    runner = RuntimeRunner()
    client = probe_client(runner)
    original_status = runner.__call__

    def become_ready(
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
            runner.tabs["tab-response"]["alive"] = True
            runner.tabs["tab-response"]["cliState"] = "idle"
        return result

    client._runner = become_ready
    result = client.probe_agent_readiness(
        provider="codex",
        probe_name="readiness-corr-1",
        correlation_id="corr-1",
        preexisting_tab_ids=(),
        timeout_seconds=1,
    )

    assert result.cleanup_confirmed
    assert runner.tabs == {}


def test_probe_readiness_failure_still_closes_the_exact_probe() -> None:
    runner = RuntimeRunner()
    client = probe_client(runner, monotonic=lambda: 1)
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
    assert len([call for call in runner.calls if call[1:3] == ["tab", "close"]]) == 1


def test_probe_readiness_interruption_still_closes_the_exact_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RuntimeRunner()
    client = probe_client(runner)

    def interrupt_readiness(session_id: str, timeout_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(client, "_wait_until_ready_structured", interrupt_readiness)

    with pytest.raises(KeyboardInterrupt):
        client.probe_agent_readiness(
            provider="codex",
            probe_name="readiness-corr-1",
            correlation_id="corr-1",
            preexisting_tab_ids=(),
            timeout_seconds=1,
        )
    assert runner.tabs == {}
    assert len([call for call in runner.calls if call[1:3] == ["tab", "close"]]) == 1


def test_probe_cleanup_interruption_is_unknown_with_exact_retained_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RuntimeRunner()
    client = probe_client(runner)

    def interrupt_cleanup(
        session_id: str, *, expected_state: TabState | None = None
    ) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(client, "close_session", interrupt_cleanup)

    with pytest.raises(MutationOutcomeUnknown, match="tab-response.*retained"):
        client.probe_agent_readiness(
            provider="codex",
            probe_name="readiness-corr-1",
            correlation_id="corr-1",
            preexisting_tab_ids=(),
            timeout_seconds=1,
        )
    assert "tab-response" in runner.tabs


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


def test_new_run_does_not_collide_with_retained_logical_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = RuntimeRunner()
    runtime = PurpleMuxRuntime(runner=runner)
    monkeypatch.setenv(RUN_IDENTITY_ENV, "run-one")
    first = runtime.create_workspace(
        CreateWorkspaceRequest(str(tmp_path), "Version workflow")
    )
    retained = runner.workspaces.pop(first.id)
    retained["id"] = "ws-retained"
    runner.workspaces["ws-retained"] = retained

    monkeypatch.setenv(RUN_IDENTITY_ENV, "run-two")
    second = runtime.create_workspace(
        CreateWorkspaceRequest(str(tmp_path), "Version workflow")
    )

    assert second.id == "ws-new"
    assert retained["name"] != second.name
    assert len(runner.workspaces) == 2


def test_workspace_nonzero_after_apply_reconciles_without_retry(
    tmp_path: Path,
) -> None:
    runner = RuntimeRunner("workspace-nonzero-after-apply")

    result = PurpleMuxRuntime(runner=runner).create_workspace(
        CreateWorkspaceRequest(str(tmp_path), "Version work", "corr-1")
    )

    assert result.id == "ws-new"
    assert (
        len([call for call in runner.calls if call[1:3] == ["workspace", "create"]])
        == 1
    )


def test_workspace_delete_verifies_identity_and_reconciles_authoritative_absence(
    tmp_path: Path,
) -> None:
    runner = RuntimeRunner()
    workspace = {
        "id": "ws-owned",
        "name": "Owned",
        "directories": [str(tmp_path)],
    }
    runner.workspaces["ws-owned"] = workspace
    delete_calls: list[str] = []

    def delete(workspace_id: str) -> None:
        delete_calls.append(workspace_id)
        runner.workspaces.pop(workspace_id)

    runtime = PurpleMuxRuntime(runner=runner, workspace_deleter=delete)
    runtime.delete_workspace(
        "ws-owned",
        expected_state=WorkspaceState("ws-owned", "Owned", (str(tmp_path),)),
    )

    assert delete_calls == ["ws-owned"]
    assert runtime.list_workspaces() == ()


def test_workspace_delete_uses_public_atomic_empty_workspace_cli_contract(
    tmp_path: Path,
) -> None:
    runner = RuntimeRunner()
    runner.workspaces["ws-owned"] = {
        "id": "ws-owned",
        "name": "Owned",
        "directories": [str(tmp_path)],
    }
    runtime = PurpleMuxRuntime(runner=runner)

    runtime.delete_workspace(
        "ws-owned",
        expected_state=WorkspaceState("ws-owned", "Owned", (str(tmp_path),)),
    )

    assert [call for call in runner.calls if call[1:3] == ["workspace", "delete"]] == [
        [
            "purplemux",
            "workspace",
            "delete",
            "-w",
            "ws-owned",
            "--if-empty",
        ]
    ]
    assert runtime.list_workspaces() == ()


@pytest.mark.parametrize(
    "mode",
    ["workspace-delete-already-absent", "workspace-delete-already-absent-nonzero"],
)
def test_workspace_delete_accepts_authoritative_already_absent_outcome(
    tmp_path: Path, mode: str
) -> None:
    runner = RuntimeRunner(mode)
    runner.workspaces["ws-owned"] = {
        "id": "ws-owned",
        "name": "Owned",
        "directories": [str(tmp_path)],
    }
    runtime = PurpleMuxRuntime(runner=runner)

    runtime.delete_workspace(
        "ws-owned",
        expected_state=WorkspaceState("ws-owned", "Owned", (str(tmp_path),)),
    )

    assert "ws-owned" not in runner.workspaces
    assert len([call for call in runner.calls if call[1:] == ["workspaces"]]) == 1


def test_workspace_delete_atomically_refuses_tab_created_after_preinspection(
    tmp_path: Path,
) -> None:
    runner = RuntimeRunner("workspace-delete-concurrent-tab")
    runner.workspaces["ws-owned"] = {
        "id": "ws-owned",
        "name": "Owned",
        "directories": [str(tmp_path)],
    }
    runtime = PurpleMuxRuntime(runner=runner)

    with pytest.raises(WorkerFailure, match="confirmed_rejected"):
        runtime.delete_workspace(
            "ws-owned",
            expected_state=WorkspaceState("ws-owned", "Owned", (str(tmp_path),)),
        )

    assert "ws-owned" in runner.workspaces
    assert runner.tabs["concurrent"]["workspaceId"] == "ws-owned"


@pytest.mark.parametrize(
    "mode",
    [
        "workspace-delete-timeout-after-apply",
        "workspace-delete-nonzero-after-apply",
    ],
)
def test_workspace_delete_possible_dispatch_failure_reconciles_absence(
    tmp_path: Path, mode: str
) -> None:
    runner = RuntimeRunner(mode)
    runner.workspaces["ws-owned"] = {
        "id": "ws-owned",
        "name": "Owned",
        "directories": [str(tmp_path)],
    }
    runtime = PurpleMuxRuntime(runner=runner)

    runtime.delete_workspace(
        "ws-owned",
        expected_state=WorkspaceState("ws-owned", "Owned", (str(tmp_path),)),
    )

    assert "ws-owned" not in runner.workspaces
    assert (
        len([call for call in runner.calls if call[1:3] == ["workspace", "delete"]])
        == 1
    )


def test_workspace_delete_generic_nonzero_with_unchanged_state_is_uncertain(
    tmp_path: Path,
) -> None:
    runner = RuntimeRunner("workspace-delete-generic-nonzero")
    runner.workspaces["ws-owned"] = {
        "id": "ws-owned",
        "name": "Owned",
        "directories": [str(tmp_path)],
    }
    runtime = PurpleMuxRuntime(runner=runner)

    with pytest.raises(MutationOutcomeUnknown, match="unknown"):
        runtime.delete_workspace(
            "ws-owned",
            expected_state=WorkspaceState("ws-owned", "Owned", (str(tmp_path),)),
        )

    assert "ws-owned" in runner.workspaces
