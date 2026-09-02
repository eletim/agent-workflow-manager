from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from purplemux_client import MutationConflict, MutationOutcomeUnknown, WorkerFailure
from purplemux_client.operations import (
    DRY_RUN_BOUNDARY_EXIT_CODE,
    DRY_RUN_FD_ENV,
    AuthoritativeMutationRejection,
    MutationResolution,
    PossibleDispatchFailure,
    Reconciliation,
    execute_mutation,
)


def test_possible_dispatch_can_reconcile_late_success_without_retry() -> None:
    calls = 0

    def dispatch() -> str:
        nonlocal calls
        calls += 1
        raise PossibleDispatchFailure("response lost")

    result = execute_mutation(
        operation="create",
        target="item",
        pre_state="absent",
        dispatch=dispatch,
        reconcile=lambda _quiescent: Reconciliation(
            MutationResolution.DESIRED, "created"
        ),
    )

    assert result == "created"
    assert calls == 1


def test_unchanged_read_after_possible_dispatch_remains_unknown() -> None:
    with pytest.raises(MutationOutcomeUnknown, match="unknown"):
        execute_mutation(
            operation="update",
            target="item",
            pre_state="old",
            dispatch=lambda: (_ for _ in ()).throw(
                PossibleDispatchFailure("transport lost")
            ),
            reconcile=lambda _quiescent: Reconciliation(
                MutationResolution.UNKNOWN, detail="still old; later apply is possible"
            ),
        )


def test_authoritative_rejection_and_conflict_are_distinct() -> None:
    def rejected() -> str:
        raise AuthoritativeMutationRejection("HTTP 422")

    with pytest.raises(WorkerFailure, match="confirmed_rejected"):
        execute_mutation(
            operation="update",
            target="item",
            pre_state="old",
            dispatch=rejected,
            reconcile=lambda quiescent: Reconciliation(
                MutationResolution.REJECTED,
                detail=f"quiescent={quiescent}",
            ),
        )
    with pytest.raises(MutationConflict, match="confirmed_conflict"):
        execute_mutation(
            operation="update",
            target="item",
            pre_state="old",
            dispatch=rejected,
            reconcile=lambda _quiescent: Reconciliation(
                MutationResolution.CONFLICT, detail="changed another way"
            ),
        )


def test_dry_run_boundary_is_not_catchable_by_base_exception() -> None:
    read_fd, write_fd = os.pipe()
    environment = dict(os.environ)
    environment[DRY_RUN_FD_ENV] = str(write_fd)
    code = """
from purplemux_client.operations import execute_mutation
try:
    execute_mutation(operation='merge', target='repo#1', pre_state={'sha':'abc'},
                     dispatch=lambda: print('DISPATCHED'),
                     reconcile=lambda _: None,
                     plan={'kind':'merge','sha':'abc'})
except BaseException:
    print('CAUGHT')
print('CONTINUED')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        pass_fds=(write_fd,),
        capture_output=True,
        text=True,
        check=False,
    )
    os.close(write_fd)
    boundary = os.read(read_fd, 65_537)
    os.close(read_fd)

    assert completed.returncode == DRY_RUN_BOUNDARY_EXIT_CODE
    assert completed.stdout == ""
    assert json.loads(boundary) == {
        "protocol": 1,
        "operation": "merge",
        "target": "repo#1",
        "preState": {"kind": "merge", "sha": "abc"},
    }
