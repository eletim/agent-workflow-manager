from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from purplemux_client.client import (
    AgentReadinessCleanupUnknown,
    PurpleMuxCLIClient,
    PurpleMuxRuntime,
    TabState,
    WorkspaceState,
)
from purplemux_client.errors import (
    MutationOutcomeUnknown,
    TerminalSessionError,
    WorkerFailure,
)


class ReadinessRuntime(Protocol):
    def list_workspaces(self) -> tuple[WorkspaceState, ...]: ...

    def workspace(self, workspace_id: str) -> PurpleMuxCLIClient: ...


class ReadinessProbeBusy(RuntimeError):
    """Raised when an explicit readiness probe is already in progress."""


class ReadinessReconciliationRequired(RuntimeError):
    """Raised when an unresolved probe must be reconciled before another probe."""


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
        state_file: Path | None = None,
    ) -> None:
        self._runtime = runtime or PurpleMuxRuntime()
        self._token_factory = token_factory or (lambda: secrets.token_hex(8))
        self._timeout_seconds = timeout_seconds
        self._state_file = state_file or self._default_state_file()
        self._lock = threading.Lock()
        self._running = False
        self._last = self._load_unresolved()

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
        ownership: int | None = None
        try:
            ownership = self._acquire_ownership()
            persisted = self._load_unresolved()
            with self._lock:
                if persisted is not None:
                    self._last = persisted
                elif self._last is not None and self._last.status in {
                    "pending",
                    "unknown",
                }:
                    self._last = None
                unresolved = self._last
            if unresolved is not None and unresolved.status in {"pending", "unknown"}:
                raise ReadinessReconciliationRequired(
                    "the unresolved Agent readiness probe must be authoritatively "
                    "reconciled before another probe"
                )
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
            try:
                client = self._runtime.workspace(workspace_id)
                before = client.list_sessions()
            except TerminalSessionError as exc:
                status = AgentReadinessStatus(
                    "failed",
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
                self._set_last(status)
                return status
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
                if identified is None:
                    status = AgentReadinessStatus(
                        "failed",
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
                else:
                    status = AgentReadinessStatus(
                        "failed",
                        workspace_id,
                        selected.name,
                        normalized_provider,
                        probe_name,
                        correlation_id,
                        identified.id,
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
            if ownership is not None:
                self._release_ownership(ownership)
            with self._lock:
                self._running = False

    def reconcile(self) -> AgentReadinessStatus:
        """Inspect an unresolved identity and clear it only when absence is proven."""
        with self._lock:
            if self._running:
                raise ReadinessProbeBusy("an Agent readiness probe is already running")
            self._running = True
        ownership: int | None = None
        try:
            ownership = self._acquire_ownership()
            unresolved = self._load_unresolved()
            with self._lock:
                self._last = unresolved
            if unresolved is None:
                raise ValueError("there is no unresolved Agent readiness probe")
            if unresolved.status == "pending":
                unresolved = AgentReadinessStatus(
                    "unknown",
                    unresolved.workspace_id,
                    unresolved.workspace_name,
                    unresolved.provider,
                    unresolved.probe_name,
                    unresolved.correlation_id,
                    unresolved.tab_id,
                    "not-observed",
                    "unknown" if unresolved.tab_id is not None else "not-attempted",
                    "The prior probe did not reach a recorded outcome; explicit "
                    "authoritative reconciliation is required.",
                    unresolved.tab_id,
                )
                self._set_last(unresolved)
            workspaces = self._runtime.list_workspaces()
            selected = next(
                (
                    workspace
                    for workspace in workspaces
                    if workspace.id == unresolved.workspace_id
                ),
                None,
            )
            if selected is None:
                return self._confirm_absent(
                    unresolved,
                    "The original workspace is authoritatively absent; the probe "
                    "block was cleared.",
                )
            tabs = self._runtime.workspace(unresolved.workspace_id).list_sessions()
            matches = tuple(tab for tab in tabs if self._matches_probe(tab, unresolved))
            if unresolved.tab_id is not None:
                matching_id = next(
                    (tab for tab in tabs if tab.id == unresolved.tab_id), None
                )
                if matching_id is None:
                    if len(matches) == 1:
                        discovered = matches[0]
                        retained = AgentReadinessStatus(
                            "unknown",
                            unresolved.workspace_id,
                            selected.name,
                            unresolved.provider,
                            unresolved.probe_name,
                            unresolved.correlation_id,
                            discovered.id,
                            unresolved.readiness,
                            unresolved.cleanup,
                            f"Recorded probe tab {unresolved.tab_id!r} is absent, but "
                            f"the correlated identity is present as {discovered.id!r}; "
                            "manual inspection is required.",
                            discovered.id,
                        )
                        self._set_last(retained)
                        return retained
                    if len(matches) > 1:
                        ambiguous = AgentReadinessStatus(
                            "unknown",
                            unresolved.workspace_id,
                            selected.name,
                            unresolved.provider,
                            unresolved.probe_name,
                            unresolved.correlation_id,
                            None,
                            unresolved.readiness,
                            unresolved.cleanup,
                            f"Recorded probe tab {unresolved.tab_id!r} is absent, but "
                            "multiple tabs match the correlated identity; manual "
                            "inspection is required.",
                        )
                        self._set_last(ambiguous)
                        return ambiguous
                    return self._confirm_absent(
                        unresolved,
                        f"Probe tab {unresolved.tab_id!r} is authoritatively absent; "
                        "the probe block was cleared.",
                    )
                if not self._matches_probe(matching_id, unresolved):
                    detail = (
                        f"Tab ID {unresolved.tab_id!r} now has a different identity; "
                        "the unresolved probe remains blocked for manual inspection."
                    )
                else:
                    detail = (
                        f"Probe tab {unresolved.tab_id!r} is still present. Close it "
                        "manually, then run authoritative reconciliation again."
                    )
                retained = AgentReadinessStatus(
                    "unknown",
                    unresolved.workspace_id,
                    selected.name,
                    unresolved.provider,
                    unresolved.probe_name,
                    unresolved.correlation_id,
                    unresolved.tab_id,
                    unresolved.readiness,
                    unresolved.cleanup,
                    detail,
                    unresolved.tab_id,
                )
                self._set_last(retained)
                return retained

            if not matches:
                return self._confirm_absent(
                    unresolved,
                    f"No tab matches probe identity {unresolved.probe_name!r}; the "
                    "probe block was cleared.",
                )
            if len(matches) > 1:
                ambiguous = AgentReadinessStatus(
                    "unknown",
                    unresolved.workspace_id,
                    selected.name,
                    unresolved.provider,
                    unresolved.probe_name,
                    unresolved.correlation_id,
                    None,
                    unresolved.readiness,
                    unresolved.cleanup,
                    "Multiple tabs match the unresolved probe identity; manual "
                    "inspection is required.",
                )
                self._set_last(ambiguous)
                return ambiguous
            discovered = matches[0]
            retained = AgentReadinessStatus(
                "unknown",
                unresolved.workspace_id,
                selected.name,
                unresolved.provider,
                unresolved.probe_name,
                unresolved.correlation_id,
                discovered.id,
                unresolved.readiness,
                unresolved.cleanup,
                f"Probe tab {discovered.id!r} was authoritatively rediscovered. "
                "Close it manually, then run authoritative reconciliation again.",
                discovered.id,
            )
            self._set_last(retained)
            return retained
        finally:
            if ownership is not None:
                self._release_ownership(ownership)
            with self._lock:
                self._running = False

    def _set_last(self, status: AgentReadinessStatus) -> None:
        if status.status in {"pending", "unknown"}:
            self._save_unresolved(status)
        else:
            self._clear_unresolved()
        with self._lock:
            self._last = status

    def _confirm_absent(
        self, unresolved: AgentReadinessStatus, detail: str
    ) -> AgentReadinessStatus:
        resolved = AgentReadinessStatus(
            "reconciled",
            unresolved.workspace_id,
            unresolved.workspace_name,
            unresolved.provider,
            unresolved.probe_name,
            unresolved.correlation_id,
            unresolved.tab_id,
            unresolved.readiness,
            "confirmed-absent",
            detail,
        )
        self._set_last(resolved)
        return resolved

    @staticmethod
    def _matches_probe(tab: TabState, probe: AgentReadinessStatus) -> bool:
        panel_type = "codex-cli" if probe.provider == "codex" else "claude-code"
        provider = "codex" if probe.provider == "codex" else "claude"
        return (
            tab.workspace_id == probe.workspace_id
            and tab.name == probe.probe_name
            and tab.panel_type == panel_type
            and tab.provider == provider
        )

    @staticmethod
    def _default_state_file() -> Path:
        configured = os.environ.get("AGENT_WORKFLOW_MANAGER_READINESS_STATE_FILE")
        if configured:
            return Path(configured).expanduser()
        state_home = os.environ.get("XDG_STATE_HOME")
        root = (
            Path(state_home).expanduser()
            if state_home
            else Path.home() / ".local/state"
        )
        return root / "agent-workflow-manager" / "readiness-probe.json"

    def _acquire_ownership(self) -> int:
        lock_path = self._state_file.with_name(f".{self._state_file.name}.lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise ReadinessProbeBusy(
                    "another Runner process owns Agent readiness probe recovery"
                ) from exc
            return descriptor
        except ReadinessProbeBusy:
            raise
        except OSError as exc:
            raise WorkerFailure(
                "Agent readiness ownership lock could not be acquired"
            ) from exc

    @staticmethod
    def _release_ownership(descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _save_unresolved(self, status: AgentReadinessStatus) -> None:
        payload = json.dumps(
            {"version": 1, "probe": status.as_json()},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        temporary_path: Path | None = None
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self._state_file.name}.", dir=self._state_file.parent
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self._state_file)
        except OSError as exc:
            raise WorkerFailure(
                "Agent readiness recovery state could not be durably saved; "
                "preserve the current process and reconcile before another probe"
            ) from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _clear_unresolved(self) -> None:
        try:
            self._state_file.unlink(missing_ok=True)
        except OSError as exc:
            raise WorkerFailure(
                "Agent readiness recovery state could not be cleared"
            ) from exc

    def _load_unresolved(self) -> AgentReadinessStatus | None:
        try:
            try:
                content = self._state_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            payload = json.loads(content)
            probe = payload.get("probe") if isinstance(payload, dict) else None
            if not isinstance(probe, dict) or payload.get("version") != 1:
                raise ValueError
            required = {
                "status": str,
                "workspaceId": str,
                "workspaceName": str,
                "provider": str,
                "probeName": str,
                "correlationId": str,
                "readiness": str,
                "cleanup": str,
            }
            if any(
                not isinstance(probe.get(key), kind) for key, kind in required.items()
            ):
                raise ValueError
            tab_id = probe.get("tabId")
            retained_tab_id = probe.get("retainedTabId")
            detail = probe.get("detail")
            if any(
                value is not None and not isinstance(value, str)
                for value in (tab_id, retained_tab_id, detail)
            ):
                raise ValueError
            if probe["status"] not in {"pending", "unknown"}:
                raise ValueError
            if (
                probe["provider"] not in {"codex", "claude-code"}
                or not probe["workspaceId"]
                or not probe["probeName"]
                or not probe["correlationId"]
                or probe["correlationId"] not in probe["probeName"]
                or probe["readiness"]
                not in {"not-observed", "waiting", "ready", "failed"}
                or probe["cleanup"] not in {"not-attempted", "pending", "unknown"}
                or tab_id == ""
                or retained_tab_id == ""
            ):
                raise ValueError
            loaded = AgentReadinessStatus(
                "unknown",
                probe["workspaceId"],
                probe["workspaceName"],
                probe["provider"],
                probe["probeName"],
                probe["correlationId"],
                tab_id,
                "not-observed" if probe["status"] == "pending" else probe["readiness"],
                "unknown" if tab_id is not None else "not-attempted",
                "The previous readiness process ended before its outcome was fully "
                "recorded; explicit authoritative reconciliation is required."
                if probe["status"] == "pending"
                else detail,
                tab_id if tab_id is not None else retained_tab_id,
            )
            self._save_unresolved(loaded)
            return loaded
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise WorkerFailure(
                "Agent readiness recovery state is unreadable; preserve it and "
                "repair or remove it only after manual reconciliation"
            ) from exc
