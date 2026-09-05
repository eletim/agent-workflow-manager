from __future__ import annotations

from pathlib import Path

import pytest

from purplemux_client.preflight import WorkflowValidator
from purplemux_client.runner import PythonRunner, WorkflowDryRunError


def test_static_validation_reports_dry_run_eligibility_separately() -> None:
    result = WorkflowValidator().validate(
        "import subprocess\nWORKFLOW_DRY_RUN = 1\nsubprocess.run(['true'])\n"
    )

    assert result.valid
    assert not result.as_json()["dryRunEligible"]
    assert "subprocess.run" in result.dry_run_issues[0].message


@pytest.mark.parametrize(
    "source",
    [
        "import shutil\nshutil.move('before', 'after')",
        "open('result.txt', 'w').write('changed')",
        "from pathlib import Path\nPath('result').write_text('changed')",
    ],
)
def test_static_validation_rejects_raw_filesystem_mutation_facilities(
    source: str,
) -> None:
    result = WorkflowValidator().validate(f"WORKFLOW_DRY_RUN = 1\n{source}\n")

    assert result.valid
    assert result.dry_run_issues


@pytest.mark.parametrize(
    "source",
    [
        "from pathlib import Path\nPath('changed').open('w').write('changed')",
        "from pathlib import Path\npath = Path('changed')\npath.open('w').close()",
        ("from pathlib import Path\nmode = 'w'\nPath('changed').open(mode).close()"),
        ("import os\nfd = os.open('changed', os.O_WRONLY | os.O_CREAT)\nos.close(fd)"),
    ],
)
def test_dry_run_rejects_raw_open_before_it_can_modify_files(
    tmp_path: Path, source: str
) -> None:
    runner = PythonRunner(managed_workflows=False, workflow_cwd=tmp_path)
    try:
        with pytest.raises(WorkflowDryRunError):
            runner.dry_run(f"WORKFLOW_DRY_RUN = 1\n{source}\n")
    finally:
        runner.close()

    assert not (tmp_path / "changed").exists()


def test_dry_run_boundary_cannot_be_swallowed_and_dispatch_never_runs() -> None:
    code = """
WORKFLOW_DRY_RUN = 1
from purplemux_client.operations import execute_mutation, Reconciliation, MutationResolution
from purplemux_client import emit_finding
emit_finding("github", "same-head candidates exhausted")
try:
    execute_mutation(
        operation="create test resource",
        target="resource-1",
        pre_state={"exists": False},
        dispatch=lambda: print("DISPATCHED"),
        reconcile=lambda _quiescent: Reconciliation(MutationResolution.UNKNOWN),
        plan={"kind": "test_create", "id": "resource-1"},
    )
except BaseException:
    print("CAUGHT")
finally:
    print("FINALLY")
"""
    runner = PythonRunner(
        managed_workflows=False,
    )
    try:
        result = runner.dry_run(code)
    finally:
        runner.close()

    assert result.status == "frontier"
    assert result.stdout == ""
    assert result.next_mutation is not None
    assert result.next_mutation["operation"] == "create test resource"
    assert result.findings[0].category == "github"


def test_dry_run_follows_real_dynamic_branch_without_fabricated_continuation() -> None:
    code = """
import sys
WORKFLOW_DRY_RUN = 1
from purplemux_client.operations import execute_mutation, Reconciliation, MutationResolution
if sys.argv[1] == "read-only":
    print("done")
else:
    execute_mutation(
        operation="dynamic mutation", target=sys.argv[1], pre_state=None,
        dispatch=lambda: None,
        reconcile=lambda _: Reconciliation(MutationResolution.UNKNOWN),
    )
    print("fabricated future")
"""
    runner = PythonRunner(
        managed_workflows=False,
    )
    try:
        complete = runner.dry_run(code, args=("read-only",))
        frontier = runner.dry_run(code, args=("mutating",))
    finally:
        runner.close()

    assert complete.status == "complete" and complete.stdout == "done\n"
    assert frontier.status == "frontier"
    assert "fabricated future" not in frontier.stdout


def test_dry_run_rejects_ineligible_workflow_without_execution() -> None:
    runner = PythonRunner(
        managed_workflows=False,
    )
    try:
        with pytest.raises(WorkflowDryRunError):
            runner.dry_run("print('ran')")
    finally:
        runner.close()
