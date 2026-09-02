from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from purplemux_client.client import (
    AgentReadinessCleanupUnknown,
    AgentReadinessProbeResult,
    TabState,
    WorkspaceState,
)
from purplemux_client.errors import (
    MutationOutcomeUnknown,
    SessionReadyTimeout,
    WorkerNeedsInput,
)
from purplemux_client.readiness import AgentReadinessService


class ProbeClient:
    def __init__(self, outcome: str = "success") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []
        self.tabs = (TabState("existing", "ws-1", "shell", "terminal", None),)

    def list_sessions(self) -> tuple[TabState, ...]:
        return self.tabs

    def probe_agent_readiness(
        self,
        *,
        provider: str,
        probe_name: str,
        correlation_id: str,
        preexisting_tab_ids: Sequence[str],
        timeout_seconds: float,
        on_identified: Callable[[TabState], None] | None = None,
    ) -> AgentReadinessProbeResult:
        self.calls.append(
            {
                "provider": provider,
                "probe_name": probe_name,
                "correlation_id": correlation_id,
                "preexisting_tab_ids": tuple(preexisting_tab_ids),
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.outcome == "create-unknown":
            raise MutationOutcomeUnknown("create may have been dispatched")
        tab = TabState("probe-tab", "ws-1", probe_name, "codex-cli", "codex")
        if on_identified is not None:
            on_identified(tab)
        if self.outcome == "needs-input":
            raise WorkerNeedsInput("provider requires onboarding")
        if self.outcome == "readiness-failure":
            raise SessionReadyTimeout("provider did not become ready")
        if self.outcome == "cleanup-unknown":
            raise AgentReadinessCleanupUnknown(
                "probe tab retained after cleanup uncertainty",
                tab=tab,
                readiness_error=None,
            )
        return AgentReadinessProbeResult(
            "ws-1", "probe-tab", provider, probe_name, correlation_id, True, True
        )


class ProbeRuntime:
    def __init__(self, client: ProbeClient, *, has_workspace: bool = True) -> None:
        self.client = client
        self.workspaces = (
            (WorkspaceState("ws-1", "Existing workspace", ("/repo",)),)
            if has_workspace
            else ()
        )
        self.workspace_calls: list[str] = []

    def list_workspaces(self) -> tuple[WorkspaceState, ...]:
        return self.workspaces

    def workspace(self, workspace_id: str) -> ProbeClient:
        self.workspace_calls.append(workspace_id)
        return self.client


def service(outcome: str = "success") -> tuple[AgentReadinessService, ProbeClient]:
    client = ProbeClient(outcome)
    return (
        AgentReadinessService(
            ProbeRuntime(client), token_factory=lambda: "unique123", timeout_seconds=7
        ),
        client,
    )


def test_success_captures_precreate_set_and_reports_ready_cleanup_separately() -> None:
    readiness, client = service()

    result = readiness.probe(workspace_id="ws-1", provider="codex")

    assert result.status == "succeeded"
    assert result.readiness == "ready"
    assert result.cleanup == "confirmed"
    assert result.tab_id == "probe-tab"
    assert client.calls == [
        {
            "provider": "codex",
            "probe_name": "awm-readiness-codex-unique123",
            "correlation_id": "unique123",
            "preexisting_tab_ids": ("existing",),
            "timeout_seconds": 7,
        }
    ]


def test_provider_needs_input_is_failed_but_cleanup_is_confirmed() -> None:
    readiness, _ = service("needs-input")

    result = readiness.probe(workspace_id="ws-1", provider="codex")

    assert result.status == "failed"
    assert result.tab_id == "probe-tab"
    assert result.readiness == "failed"
    assert result.cleanup == "confirmed"
    assert "onboarding" in str(result.detail)


def test_readiness_failure_is_distinct_from_confirmed_cleanup() -> None:
    readiness, _ = service("readiness-failure")

    result = readiness.probe(workspace_id="ws-1", provider="codex")

    assert result.status == "failed"
    assert result.readiness == "failed"
    assert result.cleanup == "confirmed"
    assert "did not become ready" in str(result.detail)


def test_uncertain_create_requires_name_based_reconciliation_before_retry() -> None:
    readiness, _ = service("create-unknown")

    result = readiness.probe(workspace_id="ws-1", provider="codex")
    payload = result.as_json()

    assert result.status == "unknown"
    assert result.tab_id is None
    assert result.readiness == "not-observed"
    assert result.cleanup == "not-attempted"
    assert "awm-readiness-codex-unique123" in str(payload["guidance"])
    assert "do not retry" in str(payload["guidance"])


def test_uncertain_cleanup_reports_ready_but_not_success_and_retains_exact_tab() -> (
    None
):
    readiness, _ = service("cleanup-unknown")

    result = readiness.probe(workspace_id="ws-1", provider="codex")
    payload = result.as_json()

    assert result.status == "unknown"
    assert result.readiness == "ready"
    assert result.cleanup == "unknown"
    assert result.retained_tab_id == "probe-tab"
    assert "probe-tab" in str(payload["guidance"])


def test_no_workspace_prevents_any_tab_inspection_or_mutation() -> None:
    client = ProbeClient()
    runtime = ProbeRuntime(client, has_workspace=False)
    readiness = AgentReadinessService(runtime, token_factory=lambda: "unique123")

    assert readiness.snapshot()["workspaces"] == []
    with pytest.raises(ValueError, match="workspace was not found"):
        readiness.probe(workspace_id="ws-1", provider="codex")
    assert runtime.workspace_calls == []
    assert client.calls == []
