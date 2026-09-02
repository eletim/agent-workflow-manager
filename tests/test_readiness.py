from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

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
    WorkerFailure,
    WorkerNeedsInput,
)
from purplemux_client.readiness import (
    AgentReadinessService,
    AgentReadinessStatus,
    ReadinessProbeBusy,
    ReadinessReconciliationRequired,
)


class ProbeClient:
    def __init__(self, outcome: str = "success") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []
        self.tabs = (TabState("existing", "ws-1", "shell", "terminal", None),)

    def list_sessions(self) -> tuple[TabState, ...]:
        if self.outcome == "list-failure":
            raise WorkerFailure("authoritative tab listing failed")
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
        if self.outcome == "pre-create-failure":
            self.tabs += (TabState("concurrent", "ws-1", "other", "terminal", None),)
            raise WorkerFailure(
                "probe preexisting tab set is not authoritative/current"
            )
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


class BlockingProbeClient(ProbeClient):
    def __init__(self) -> None:
        super().__init__("create-unknown")
        self.entered = threading.Event()
        self.release = threading.Event()

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
        self.entered.set()
        if not self.release.wait(3):
            raise AssertionError("test did not release the owning readiness probe")
        return super().probe_agent_readiness(
            provider=provider,
            probe_name=probe_name,
            correlation_id=correlation_id,
            preexisting_tab_ids=preexisting_tab_ids,
            timeout_seconds=timeout_seconds,
            on_identified=on_identified,
        )


class IdentifyingBlockingProbeClient(ProbeClient):
    def __init__(self) -> None:
        super().__init__("cleanup-unknown")
        self.before_identification = threading.Event()
        self.allow_identification = threading.Event()
        self.identified = threading.Event()
        self.release = threading.Event()

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
        self.before_identification.set()
        if not self.allow_identification.wait(3):
            raise AssertionError("test did not allow probe identification")
        tab = TabState("probe-tab", "ws-1", probe_name, "codex-cli", "codex")
        if on_identified is not None:
            on_identified(tab)
        self.identified.set()
        if not self.release.wait(3):
            raise AssertionError("test did not release identified readiness probe")
        raise AgentReadinessCleanupUnknown(
            "probe tab retained after cleanup uncertainty",
            tab=tab,
            readiness_error=None,
        )


def service(
    tmp_path: Path, outcome: str = "success"
) -> tuple[AgentReadinessService, ProbeClient]:
    client = ProbeClient(outcome)
    return (
        AgentReadinessService(
            ProbeRuntime(client),
            token_factory=lambda: "unique123",
            timeout_seconds=7,
            state_file=tmp_path / "readiness.json",
        ),
        client,
    )


def test_success_captures_precreate_set_and_reports_ready_cleanup_separately(
    tmp_path: Path,
) -> None:
    readiness, client = service(tmp_path)

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


def test_provider_needs_input_is_failed_but_cleanup_is_confirmed(
    tmp_path: Path,
) -> None:
    readiness, _ = service(tmp_path, "needs-input")

    result = readiness.probe(workspace_id="ws-1", provider="codex")

    assert result.status == "failed"
    assert result.tab_id == "probe-tab"
    assert result.readiness == "failed"
    assert result.cleanup == "confirmed"
    assert "onboarding" in str(result.detail)


def test_readiness_failure_is_distinct_from_confirmed_cleanup(
    tmp_path: Path,
) -> None:
    readiness, _ = service(tmp_path, "readiness-failure")

    result = readiness.probe(workspace_id="ws-1", provider="codex")

    assert result.status == "failed"
    assert result.readiness == "failed"
    assert result.cleanup == "confirmed"
    assert "did not become ready" in str(result.detail)


def test_precreation_tab_set_change_is_not_reported_as_readiness_or_cleanup(
    tmp_path: Path,
) -> None:
    readiness, client = service(tmp_path, "pre-create-failure")

    result = readiness.probe(workspace_id="ws-1", provider="codex")

    assert result.status == "failed"
    assert result.tab_id is None
    assert result.readiness == "not-observed"
    assert result.cleanup == "not-attempted"
    assert "not authoritative/current" in str(result.detail)
    assert {tab.id for tab in client.tabs} == {"existing", "concurrent"}
    assert not (tmp_path / "readiness.json").exists()


def test_precreation_listing_failure_is_not_reported_as_readiness_or_cleanup(
    tmp_path: Path,
) -> None:
    readiness, client = service(tmp_path, "list-failure")

    result = readiness.probe(workspace_id="ws-1", provider="codex")

    assert result.readiness == "not-observed"
    assert result.cleanup == "not-attempted"
    assert client.calls == []


def test_uncertain_create_requires_name_based_reconciliation_before_retry(
    tmp_path: Path,
) -> None:
    readiness, _ = service(tmp_path, "create-unknown")

    result = readiness.probe(workspace_id="ws-1", provider="codex")
    payload = result.as_json()

    assert result.status == "unknown"
    assert result.tab_id is None
    assert result.readiness == "not-observed"
    assert result.cleanup == "not-attempted"
    assert "awm-readiness-codex-unique123" in str(payload["guidance"])
    assert "do not retry" in str(payload["guidance"])


def test_uncertain_cleanup_persists_and_blocks_direct_retry(
    tmp_path: Path,
) -> None:
    readiness, client = service(tmp_path, "cleanup-unknown")

    result = readiness.probe(workspace_id="ws-1", provider="codex")
    payload = result.as_json()

    assert result.status == "unknown"
    assert result.readiness == "ready"
    assert result.cleanup == "unknown"
    assert result.retained_tab_id == "probe-tab"
    assert "probe-tab" in str(payload["guidance"])
    with pytest.raises(ReadinessReconciliationRequired, match="reconciled"):
        readiness.probe(workspace_id="ws-1", provider="codex")
    assert len(client.calls) == 1


def test_recorded_id_absent_but_correlated_tab_present_remains_unknown(
    tmp_path: Path,
) -> None:
    readiness, client = service(tmp_path, "cleanup-unknown")
    unresolved = readiness.probe(workspace_id="ws-1", provider="codex")
    client.outcome = "success"
    client.tabs += (
        TabState(
            "replacement-id",
            "ws-1",
            unresolved.probe_name,
            "codex-cli",
            "codex",
        ),
    )

    reconciled = readiness.reconcile()

    assert reconciled.status == "unknown"
    assert reconciled.tab_id == "replacement-id"
    assert reconciled.retained_tab_id == "replacement-id"
    assert "Recorded probe tab 'probe-tab' is absent" in str(reconciled.detail)
    assert (tmp_path / "readiness.json").exists()


def test_two_services_serialize_probe_ownership_and_reload_shared_state(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "readiness.json"
    first_client = BlockingProbeClient()
    second_client = ProbeClient()
    first = AgentReadinessService(
        ProbeRuntime(first_client),
        token_factory=lambda: "first-owner",
        state_file=state_file,
    )
    second = AgentReadinessService(
        ProbeRuntime(second_client),
        token_factory=lambda: "second-owner",
        state_file=state_file,
    )
    results: list[AgentReadinessStatus] = []
    errors: list[BaseException] = []

    def run_first() -> None:
        try:
            results.append(first.probe(workspace_id="ws-1", provider="codex"))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert first_client.entered.wait(3)
    try:
        with pytest.raises(ReadinessProbeBusy, match="another Runner process"):
            second.probe(workspace_id="ws-1", provider="codex")
        assert second_client.calls == []
        persisted = json.loads(state_file.read_text(encoding="utf-8"))
        assert persisted["probe"]["correlationId"] == "first-owner"
    finally:
        first_client.release.set()
        thread.join(3)

    assert not thread.is_alive()
    assert errors == []
    assert [result.status for result in results] == ["unknown"]
    with pytest.raises(ReadinessReconciliationRequired, match="reconciled"):
        second.probe(workspace_id="ws-1", provider="codex")
    assert second_client.calls == []


def test_service_construction_cannot_overwrite_newer_identified_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "readiness.json"
    client = IdentifyingBlockingProbeClient()
    runtime = ProbeRuntime(client)
    first = AgentReadinessService(
        runtime, token_factory=lambda: "first-owner", state_file=state_file
    )
    probe_errors: list[BaseException] = []
    constructed: list[AgentReadinessService] = []
    construction_errors: list[BaseException] = []
    constructor_read = threading.Event()
    release_constructor = threading.Event()
    original_read_text = Path.read_text

    def delayed_read_text(path: Path, *args: object, **kwargs: object) -> str:
        content = original_read_text(path, *args, **kwargs)
        if path == state_file and threading.current_thread().name == "constructor":
            constructor_read.set()
            if not release_constructor.wait(3):
                raise AssertionError("test did not release service construction")
        return content

    monkeypatch.setattr(Path, "read_text", delayed_read_text)

    def run_probe() -> None:
        try:
            first.probe(workspace_id="ws-1", provider="codex")
        except BaseException as exc:
            probe_errors.append(exc)

    def construct_second() -> None:
        try:
            constructed.append(AgentReadinessService(runtime, state_file=state_file))
        except BaseException as exc:
            construction_errors.append(exc)

    probe_thread = threading.Thread(target=run_probe)
    constructor_thread = threading.Thread(target=construct_second, name="constructor")
    probe_thread.start()
    assert client.before_identification.wait(3)
    constructor_thread.start()
    assert constructor_read.wait(3)
    client.allow_identification.set()
    assert client.identified.wait(3)
    try:
        release_constructor.set()
        constructor_thread.join(3)
        persisted = json.loads(state_file.read_text(encoding="utf-8"))
        assert persisted["probe"]["tabId"] == "probe-tab"
    finally:
        release_constructor.set()
        client.release.set()
        constructor_thread.join(3)
        probe_thread.join(3)

    assert not constructor_thread.is_alive()
    assert not probe_thread.is_alive()
    assert construction_errors == []
    assert probe_errors == []
    assert len(constructed) == 1


def test_unresolved_creation_survives_restart_until_explicit_reconciliation(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "readiness.json"
    client = ProbeClient("create-unknown")
    runtime = ProbeRuntime(client)
    first = AgentReadinessService(
        runtime, token_factory=lambda: "first123", state_file=state_file
    )
    assert first.probe(workspace_id="ws-1", provider="codex").status == "unknown"
    assert state_file.exists()

    restarted = AgentReadinessService(
        runtime, token_factory=lambda: "second123", state_file=state_file
    )
    with pytest.raises(ReadinessReconciliationRequired, match="reconciled"):
        restarted.probe(workspace_id="ws-1", provider="codex")
    assert len(client.calls) == 1

    client.tabs += (
        TabState(
            "late-probe",
            "ws-1",
            "awm-readiness-codex-first123",
            "codex-cli",
            "codex",
        ),
    )
    rediscovered = restarted.reconcile()
    assert rediscovered.status == "unknown"
    assert rediscovered.retained_tab_id == "late-probe"
    assert state_file.exists()
    with pytest.raises(ReadinessReconciliationRequired, match="reconciled"):
        restarted.probe(workspace_id="ws-1", provider="codex")

    client.tabs = tuple(tab for tab in client.tabs if tab.id != "late-probe")
    reconciled = restarted.reconcile()
    assert reconciled.status == "reconciled"
    assert reconciled.cleanup == "confirmed-absent"
    assert not state_file.exists()

    client.outcome = "success"
    after_reconciliation = AgentReadinessService(
        runtime, token_factory=lambda: "third123", state_file=state_file
    )
    assert (
        after_reconciliation.probe(workspace_id="ws-1", provider="codex").status
        == "succeeded"
    )


def test_no_workspace_prevents_any_tab_inspection_or_mutation(tmp_path: Path) -> None:
    client = ProbeClient()
    runtime = ProbeRuntime(client, has_workspace=False)
    readiness = AgentReadinessService(
        runtime,
        token_factory=lambda: "unique123",
        state_file=tmp_path / "readiness.json",
    )

    assert readiness.snapshot()["workspaces"] == []
    with pytest.raises(ValueError, match="workspace was not found"):
        readiness.probe(workspace_id="ws-1", provider="codex")
    assert runtime.workspace_calls == []
    assert client.calls == []
