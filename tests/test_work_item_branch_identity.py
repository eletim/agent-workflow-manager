from __future__ import annotations

import base64
import hashlib
import json
import runpy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from purplemux_client.errors import WorkerFailure
from purplemux_client.issue_driven import (
    IssueDrivenValidationError,
    WorkItem,
    _work_item_plan_state_size,
    generate_issue_driven_workflow,
    inline_task_branch,
    parse_issue_driven_json,
)
from purplemux_client.runner import PythonRunner

EXAMPLE = Path(__file__).parents[1] / "examples" / "sequential-version-development.py"


@pytest.fixture
def workflow():
    return runpy.run_path(str(EXAMPLE), run_name="branch_identity_test")


def config_for(workflow, integration="dev/v0.6.8", *, task="Improve recovery."):
    issue = workflow["planner_inline_issue"](
        "repair-recovery", task, integration_branch=integration
    )
    return workflow["Config"](
        Path("/repo"), "acme/project", integration, "main", (issue,), "true"
    )


def payload_for(integration="dev/v0.6.8"):
    return {
        "repository": str(EXAMPLE.parents[1]),
        "integration_branch": integration,
        "final_branch": "main",
        "work_items": [{"id": "repair-recovery", "task": "Improve recovery."}],
        "max_reviews": 3,
        "merge_to_integration": True,
        "final_review": True,
        "merge_final": False,
    }


def marker(workflow, payload):
    source = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    encoded = base64.urlsafe_b64encode(source.encode()).decode()
    return f"<!-- {workflow['WORK_ITEM_PLAN_MARKER']}{encoded} -->"


def legacy_payload(config, *, finalized=False):
    items = [{"id": issue.task_id, "task": issue.task} for issue in config.issues]
    seed = json.dumps(
        {
            "items": items,
            "one_shot_issue": config.one_shot_issue,
            "policy_issue": config.policy_issue,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "version": 1,
        "seed_sha256": hashlib.sha256(seed.encode()).hexdigest(),
        "items": items,
        "position": len(items) if finalized else 0,
        "finalized": finalized,
    }


def decision(workflow, plan, actions):
    return workflow["apply_planner_decision"](
        plan,
        json.dumps({"actions": actions, "complete": False, "policy_conflicts": []}),
    )


def test_inline_branch_identity_is_stable_and_integration_scoped():
    branch = inline_task_branch("repair-recovery", integration_branch="dev/v0.6.8")
    assert branch == inline_task_branch(
        "repair-recovery", integration_branch="dev/v0.6.8"
    )
    assert branch != inline_task_branch(
        "repair-recovery", integration_branch="dev/v0.6.9"
    )
    assert branch != inline_task_branch("other-task", integration_branch="dev/v0.6.8")
    assert inline_task_branch("repair-recovery") == "feature/work-item-repair-recovery"


@pytest.mark.parametrize("integration", ["dev/v0.6.8", "dev/v0.6.9"])
def test_seed_generation_and_dynamic_planner_share_branch_identity(
    workflow, integration
):
    parsed = parse_issue_driven_json(json.dumps(payload_for(integration)))
    code = generate_issue_driven_workflow(parsed)
    expected = inline_task_branch("repair-recovery", integration_branch=integration)
    assert parsed.work_items[0].branch == expected
    assert f"Issue(None, {expected!r}, 'repair-recovery'" in code
    assert f"('Mini task repair-recovery', {expected!r}, " in code
    config = replace(config_for(workflow, integration), issues=())
    plan = workflow["WorkItemPlan"](config)
    decision(
        workflow, plan, [{"action": "add", "item": payload_for()["work_items"][0]}]
    )
    assert plan.items[0].branch == expected


def test_v2_plan_persists_and_recovers_exact_inline_branch(workflow):
    config = config_for(workflow)
    plan = workflow["WorkItemPlan"](config)
    source = workflow["work_item_plan_source"](plan, position=0, finalized=False)
    payload = json.loads(source)
    assert payload["version"] == 2
    assert payload["items"][0]["branch"] == config.issues[0].branch
    recovered = workflow["work_item_plan_from_body"](marker(workflow, payload), config)
    assert recovered.items == plan.items
    assert workflow["serialized_work_item_plan"](recovered) == marker(workflow, payload)


@pytest.mark.parametrize("legacy", [False, True])
def test_planner_updates_preserve_scoped_and_legacy_branches(workflow, legacy):
    config = config_for(workflow)
    plan = (
        workflow["work_item_plan_from_body"](
            marker(workflow, legacy_payload(config)), config
        )
        if legacy
        else workflow["WorkItemPlan"](config)
    )
    original = plan.items[0]
    decision(
        workflow,
        plan,
        [{"action": "update", "key": original.key, "task": "Add coverage."}],
    )
    assert plan.items[0].branch == original.branch
    assert plan.items[0].task_fingerprint != original.task_fingerprint
    recovered = workflow["work_item_plan_from_body"](
        workflow["serialized_work_item_plan"](plan), config
    )
    assert recovered.items == plan.items


def test_legacy_plan_keeps_seed_and_unscoped_branch_identity(workflow):
    config = config_for(workflow)
    payload = legacy_payload(config)
    body = marker(workflow, payload)
    recovered = workflow["work_item_plan_from_body"](body, config)
    assert recovered.items[0].branch == "feature/work-item-repair-recovery"
    assert workflow["plan_seed_fingerprint"](config) == payload["seed_sha256"]
    assert workflow["serialized_work_item_plan"](recovered) == body


def test_completed_legacy_plan_is_byte_preserving_noop_on_merged_pr(workflow):
    config = config_for(workflow)
    body = (
        "Base PR\n\n" + marker(workflow, legacy_payload(config, finalized=True)) + "\n"
    )
    recovered = workflow["work_item_plan_from_body"](body, config)
    assert recovered.finalized
    assert workflow["with_work_item_plan"](body, recovered) == body
    pr = SimpleNamespace(state="MERGED", body=body)
    assert workflow["persist_work_item_plan"](recovered, config, None, None, pr) is pr


def test_legacy_plan_upgrade_preserves_existing_names_and_scopes_additions(workflow):
    config = config_for(workflow)
    plan = workflow["work_item_plan_from_body"](
        marker(workflow, legacy_payload(config)), config
    )
    decision(
        workflow,
        plan,
        [{"action": "add", "item": {"id": "new-task", "task": "Add tests."}}],
    )
    source = workflow["work_item_plan_source"](plan, position=0, finalized=False)
    assert json.loads(source)["version"] == 2
    assert plan.items[0].branch == "feature/work-item-repair-recovery"
    assert plan.items[1].branch == inline_task_branch(
        "new-task", integration_branch=config.integration_branch
    )
    recovered = workflow["work_item_plan_from_body"](
        workflow["serialized_work_item_plan"](plan), config
    )
    assert recovered.items == plan.items


def test_legacy_only_updates_and_dispatch_preserve_v1_capacity(workflow):
    config = config_for(workflow)
    payload = legacy_payload(config)
    payload["items"] = [
        {"id": f"task-{index}", "task": "x" * 4000} for index in range(7)
    ] + [
        {"id": "task-7", "task": "x"},
        {"id": "task-8", "task": "x"},
        {"id": "task-9", "task": "x" * 3602},
    ]
    plan = workflow["work_item_plan_from_body"](marker(workflow, payload), config)
    decision(workflow, plan, [{"action": "update", "key": "task-8", "task": "y"}])
    for _ in range(10):
        assert plan.take_next() is not None
    source = workflow["work_item_plan_source"](plan, position=10, finalized=False)
    assert json.loads(source)["version"] == 1
    assert len(source.encode()) == 32_000
    plan.finalized = True
    recovered = workflow["work_item_plan_from_body"](
        workflow["serialized_work_item_plan"](plan), config
    )
    assert recovered.finalized
    assert recovered.items == plan.items


def test_persisted_inline_branch_from_other_integration_is_rejected(workflow):
    config = config_for(workflow)
    plan = workflow["WorkItemPlan"](config)
    other = config_for(workflow, "dev/v0.6.9")
    with pytest.raises(WorkerFailure, match="branch does not match its integration"):
        workflow["work_item_plan_from_body"](
            workflow["serialized_work_item_plan"](plan), other
        )


@pytest.mark.parametrize(
    "branch", [None, "feature/unrelated", "feature/work-item-another-task"]
)
def test_v2_inline_branch_must_be_explicit_and_match_task(workflow, branch):
    config = config_for(workflow)
    payload = legacy_payload(config)
    payload["version"] = 2
    if branch is not None:
        payload["items"][0]["branch"] = branch
    with pytest.raises(WorkerFailure, match="branch"):
        workflow["work_item_plan_from_body"](marker(workflow, payload), config)


def test_legacy_recovery_still_rejects_changed_seed_task(workflow):
    config = config_for(workflow)
    changed = config_for(workflow, task="Different requirement.")
    with pytest.raises(WorkerFailure, match="does not match the workflow seed"):
        workflow["work_item_plan_from_body"](
            marker(workflow, legacy_payload(config)), changed
        )


def test_declared_size_limit_accounts_for_persisted_scoped_branches(workflow):
    config = config_for(workflow, task=("検証 " * 500).strip())
    plan = workflow["WorkItemPlan"](config)
    source = workflow["work_item_plan_source"](
        plan, position=len(plan.items), finalized=False
    )
    expected = _work_item_plan_state_size(
        [WorkItem(id=issue.task_id, task=issue.task) for issue in config.issues],
        config.policy_issue,
        config.one_shot_issue,
        config.integration_branch,
    )
    assert expected == len(source.encode())


def test_scoped_branch_collision_with_final_branch_is_rejected():
    payload = payload_for()
    payload["final_branch"] = inline_task_branch(
        "repair-recovery", integration_branch=payload["integration_branch"]
    )
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse_issue_driven_json(json.dumps(payload))
    assert "$.final_branch" in {finding.path for finding in caught.value.findings}


def test_invalid_integration_unicode_is_rejected_before_hashing():
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse_issue_driven_json(json.dumps(payload_for("dev/\ud800")))
    assert "$.integration_branch" in {finding.path for finding in caught.value.findings}


def legacy_capacity_source():
    payload = payload_for()
    payload["work_items"] = [
        {"id": f"task-{index}", "task": "x" * 4000} for index in range(7)
    ] + [
        {"id": "task-7", "task": "x"},
        {"id": "task-8", "task": "x"},
        {"id": "task-9", "task": "x" * 3602},
    ]
    return json.dumps(payload)


def test_near_limit_legacy_seed_is_accepted_only_with_resume_allowance():
    source = legacy_capacity_source()
    with pytest.raises(IssueDrivenValidationError, match="validation failed"):
        parse_issue_driven_json(source)
    config = parse_issue_driven_json(source, allow_legacy_plan=True)
    assert (
        _work_item_plan_state_size(list(config.work_items), None, None, version=1)
        == 32_000
    )
    assert (
        _work_item_plan_state_size(
            list(config.work_items), None, None, config.integration_branch
        )
        == 32_530
    )
    payload = json.loads(source)
    payload["work_items"][-1]["task"] += "x"
    with pytest.raises(IssueDrivenValidationError, match="validation failed"):
        parse_issue_driven_json(json.dumps(payload), allow_legacy_plan=True)


def test_runner_resume_allows_saved_legacy_seed_at_original_capacity(monkeypatch):
    source = legacy_capacity_source()
    saved = SimpleNamespace(state="failed", args=(), issue_driven_json=source)
    started = []
    runner = PythonRunner(managed_workflows=False)
    monkeypatch.setattr(runner, "_get_run", lambda run_id: saved)
    monkeypatch.setattr(
        runner, "start", lambda code, **kwargs: started.append((code, kwargs)) or 2
    )
    try:
        assert runner.resume(1) == 2
    finally:
        runner.close()
    assert started[0][1]["issue_driven_json"] == source
    assert started[0][1]["resumed_from_run_id"] == 1
    assert (
        inline_task_branch("task-0", integration_branch="dev/v0.6.8") in started[0][0]
    )
