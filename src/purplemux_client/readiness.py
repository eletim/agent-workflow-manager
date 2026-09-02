from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from purplemux_client.client import (
    AgentReadinessCleanupUnknown,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    TabState,
    WorkspaceState,
)
from purplemux_client.errors import MutationOutcomeUnknown, TerminalSessionError


class ReadinessRuntime(Protocol):
    def list_workspaces(self) -> tuple[WorkspaceState, ...]: ...

    def workspace(self, workspace_id: str) -> PurpleMuxCLIClient: ...


class ReadinessProbeBusy(RuntimeError):
    """Raised when an explicit readiness probe is already in progress."""


@dataclass(frozen=True)
class AgentReadinessStatus:
    status: str
    workspace_id: str
    workspace_name: str
    provider: str
    probe_name: str
    correlation_id: str
    tab_id: str | None
    readiness: str
    cleanup: str
    detail: str | None = None
    retained_tab_id: str | None = None

    def as_json(self) -> dict[str, object]:
        guidance = None
        if self.retained_tab_id is not None:
            guidance = (
                f"Inspect retained tab {self.retained_tab_id!r} in workspace "
                f"{self.workspace_id!r}; do not start another probe until its "
                "identity and cleanup are reconciled."
            )
        elif self.status == "unknown":
            guidance = (
                f"Inspect workspace {self.workspace_id!r} for exact probe name "
                f"{self.probe_name!r}; do not retry until creation is reconciled."
            )
        return {
            "status": self.status,
            "workspaceId": self.workspace_id,
            "workspaceName": self.workspace_name,
            "provider": self.provider,
            "probeName": self.probe_name,
            "correlationId": self.correlation_id,
            "tabId": self.tab_id,
            "readiness": self.readiness,
            "cleanup": self.cleanup,
            "detail": self.detail,
            "retainedTabId": self.retained_tab_id,
            "guidance": guidance,
        }


class AgentReadinessService:
    """Run an explicit, observable provider probe in one existing workspace."""

    def __init__(
        self,
        runtime: ReadinessRuntime | None = None,
        *,
        token_factory: Callable[[], str] | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._runtime = runtime or PurpleMuxRuntime()
        self._token_factory = token_factory or (lambda: secrets.token_hex(8))
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._running = False
        self._last: AgentReadinessStatus | None = None

    def snapshot(self) -> dict[str, object]:
        workspaces = self._runtime.list_workspaces()
        with self._lock:
            last = self._last
            running = self._running
        return {
            "workspaces": [
                {
                    "id": workspace.id,
                    "name": workspace.name,
                    "directories": list(workspace.directories),
                }
                for workspace in workspaces
            ],
            "running": running,
            "probe": None if last is None else last.as_json(),
        }

    def probe(self, *, workspace_id: str, provider: str) -> AgentReadinessStatus:
        normalized_provider = provider.lower()
        if normalized_provider not in {"codex", "claude-code"}:
            raise ValueError("provider must be codex or claude-code")
        if not workspace_id:
            raise ValueError("workspaceId must be a non-empty string")
        with self._lock:
            if self._running:
                raise ReadinessProbeBusy("an Agent readiness probe is already running")
            self._running = True
        try:
            workspaces = self._runtime.list_workspaces()
            selected = next(
                (workspace for workspace in workspaces if workspace.id == workspace_id),
                None,
            )
            if selected is None:
                raise ValueError("selected PurpleMux workspace was not found")
            correlation_id = self._token_factory()
            if (
                not correlation_id
                or len(correlation_id) > 32
                or not correlation_id.replace("-", "").replace("_", "").isalnum()
                or not correlation_id.isascii()
            ):
                raise RuntimeError(
                    "readiness probe identity generator returned an invalid value"
                )
            probe_name = f"awm-readiness-{normalized_provider}-{correlation_id}"
            client = self._runtime.workspace(workspace_id)
            before = client.list_sessions()
            pending = AgentReadinessStatus(
                "pending",
                workspace_id,
                selected.name,
                normalized_provider,
                probe_name,
                correlation_id,
                None,
                "not-observed",
                "not-attempted",
            )
            self._set_last(pending)
            identified: TabState | None = None

            def remember_tab(tab: TabState) -> None:
                nonlocal identified
                identified = tab
                self._set_last(
                    AgentReadinessStatus(
                        "pending",
                        workspace_id,
                        selected.name,
                        normalized_provider,
                        probe_name,
                        correlation_id,
                        tab.id,
                        "waiting",
                        "pending",
                    )
                )

            try:
                result = client.probe_agent_readiness(
                    provider=normalized_provider,
                    probe_name=probe_name,
                    correlation_id=correlation_id,
                    preexisting_tab_ids=tuple(tab.id for tab in before),
                    timeout_seconds=self._timeout_seconds,
                    on_identified=remember_tab,
                )
            except AgentReadinessCleanupUnknown as exc:
                readiness = "ready" if exc.readiness_error is None else "failed"
                status = AgentReadinessStatus(
                    "unknown",
                    workspace_id,
                    selected.name,
                    normalized_provider,
                    probe_name,
                    correlation_id,
                    exc.tab.id,
                    readiness,
                    "unknown",
                    str(exc),
                    exc.tab.id,
                )
            except MutationOutcomeUnknown as exc:
                status = AgentReadinessStatus(
                    "unknown",
                    workspace_id,
                    selected.name,
                    normalized_provider,
                    probe_name,
                    correlation_id,
                    None,
                    "not-observed",
                    "not-attempted",
                    str(exc),
                )
            except TerminalSessionError as exc:
                status = AgentReadinessStatus(
                    "failed",
                    workspace_id,
                    selected.name,
                    normalized_provider,
                    probe_name,
                    correlation_id,
                    None if identified is None else identified.id,
                    "failed",
                    "confirmed",
                    str(exc),
                )
            else:
                status = AgentReadinessStatus(
                    "succeeded",
                    workspace_id,
                    selected.name,
                    normalized_provider,
                    probe_name,
                    correlation_id,
                    result.tab_id,
                    "ready",
                    "confirmed",
                )
            self._set_last(status)
            return status
        finally:
            with self._lock:
                self._running = False

    def _set_last(self, status: AgentReadinessStatus) -> None:
        with self._lock:
            self._last = status
