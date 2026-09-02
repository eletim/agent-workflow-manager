from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar, cast

from purplemux_client.client import MutationOutcomeUnknown, WorkerFailure

DRY_RUN_FD_ENV = "AGENT_WORKFLOW_MANAGER_DRY_RUN_FD"
DRY_RUN_BOUNDARY_EXIT_CODE = 86


class MutationResolution(str, Enum):
    """Authoritative result of inspecting state after a mutation attempt."""

    DESIRED = "confirmed_desired"
    REJECTED = "confirmed_rejected"
    CONFLICT = "confirmed_conflict"
    UNKNOWN = "unknown"


class MutationConflict(WorkerFailure):
    """Raised when a mutation quiesced in a conflicting state."""


class PreDispatchFailure(WorkerFailure):
    """The mutation command could not be dispatched."""


class AuthoritativeMutationRejection(WorkerFailure):
    """A synchronous rejection which proves no work remains in flight."""


class PossibleDispatchFailure(WorkerFailure):
    """Communication failed after the mutation may have been dispatched."""


T = TypeVar("T")


@dataclass(frozen=True)
class Reconciliation(Generic[T]):
    resolution: MutationResolution
    value: T | None = None
    detail: str = ""


def dry_run_boundary(operation: str, target: str, pre_state: object) -> None:
    """Stop at the first mutation when the Runner's private protocol is active.

    ``os._exit`` is intentional: trusted workflow code may contain an overly broad
    ``except BaseException``, which must not be able to swallow the boundary and
    continue into a later mutation.
    """

    descriptor = os.environ.get(DRY_RUN_FD_ENV)
    if descriptor is None:
        return
    try:
        fd = int(descriptor)
        if fd < 0:
            raise ValueError
        payload = json.dumps(
            {
                "protocol": 1,
                "operation": operation,
                "target": target,
                "preState": pre_state,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        if len(payload) > 65_536:
            raise ValueError
        os.write(fd, payload + b"\n")
    except (OSError, TypeError, ValueError):
        os._exit(1)
    os._exit(DRY_RUN_BOUNDARY_EXIT_CODE)


def execute_mutation(
    *,
    operation: str,
    target: str,
    pre_state: object,
    dispatch: Callable[[], T],
    reconcile: Callable[[bool], Reconciliation[T]],
    plan: Mapping[str, object] | None = None,
) -> T:
    """Attempt one mutation and conservatively reconcile possible dispatch.

    The boolean supplied to ``reconcile`` says whether synchronous rejection or
    local process quiescence was established. Merely observing unchanged state is
    insufficient when it is false.
    """

    dry_run_boundary(operation, target, plan if plan is not None else pre_state)
    rejection_proved = False
    try:
        return dispatch()
    except AuthoritativeMutationRejection:
        rejection_proved = True
    except PossibleDispatchFailure:
        pass

    try:
        result = reconcile(rejection_proved)
    except MutationOutcomeUnknown:
        raise
    except WorkerFailure as exc:
        raise MutationOutcomeUnknown(
            _context(
                operation,
                target,
                pre_state,
                "reconciliation read failed",
                str(exc),
                rejection_proved,
            )
        ) from exc

    if result.resolution is MutationResolution.DESIRED:
        return cast(T, result.value)
    context = _context(
        operation,
        target,
        pre_state,
        result.resolution.value,
        result.detail,
        rejection_proved,
    )
    if result.resolution is MutationResolution.REJECTED and rejection_proved:
        raise WorkerFailure(context)
    if result.resolution is MutationResolution.CONFLICT and rejection_proved:
        raise MutationConflict(context)
    raise MutationOutcomeUnknown(context)


def _context(
    operation: str,
    target: str,
    pre_state: object,
    outcome: str,
    detail: str,
    quiescent: bool,
) -> str:
    pre_state_text = _short(repr(pre_state), 500)
    suffix = f"; {_short(detail, 500)}" if detail else ""
    return (
        f"{operation} for {target}: {outcome}; pre-state={pre_state_text}; "
        f"rejection/quiescence established={quiescent}{suffix}"
    )


def _short(value: str, limit: int) -> str:
    flattened = value.replace("\r", " ").replace("\n", " ")
    return flattened if len(flattened) <= limit else flattened[: limit - 3] + "..."
