from __future__ import annotations

import ast
import hashlib
import inspect
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from purplemux_client import (
    BranchState,
    GitHubRepository,
    GitRepository,
    MutationOutcomeUnknown,
    PullRequestState,
    WorkerFailure,
)
from purplemux_client.issue_driven import (
    _MAX_SCENARIO_LIST_BYTES,
    IssueDrivenValidationError,
    classify_issue_topology,
    generate_issue_driven_workflow,
    parse_issue_driven_json,
)
from purplemux_client.preflight import MAX_OUTLINE_ITEMS, WorkflowValidator


def payload(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "mode": "issue-driven",
        "repository": str(Path(__file__).parents[1]),
        "integration_branch": "dev/v0.2.0",
        "final_branch": "main",
        "issues": [90, 89, 91],
        "max_reviews": 5,
        "merge_to_integration": True,
        "final_review": True,
        "merge_final": False,
    }
    value.update(overrides)
    return value


def parse(value: dict[str, object]):
    return parse_issue_driven_json(json.dumps(value))


def topology_pr(
    *,
    number: int = 158,
    state: str = "OPEN",
    head_sha: str = "f" * 40,
    base_sha: str = "b" * 40,
    head_branch: str = "feature/issue-158",
    body: str = "",
) -> PullRequestState:
    return PullRequestState(
        number,
        f"https://example.invalid/{number}",
        state,  # type: ignore[arg-type]
        True,
        "acme/project",
        head_branch,
        head_sha,
        "acme/project",
        "dev/v1",
        base_sha,
        "m" * 40 if state == "MERGED" else None,
        False,
        None,
        f"PR_{number}",
        body,
    )


class TopologyGitHub:
    def __init__(
        self,
        prs: tuple[PullRequestState, ...] = (),
        contains: set[tuple[str, str]] | None = None,
        failure: WorkerFailure | None = None,
    ) -> None:
        self.prs = prs
        self.contains = contains or set()
        self.failure = failure
        self.comparison_calls: list[tuple[str, str]] = []

    def find_pr(self, *, head: str, base: str, state: str):
        if self.failure is not None:
            raise self.failure
        matches = [
            pr
            for pr in self.prs
            if pr.head_branch == head and pr.base_branch == base and pr.state == state
        ]
        if len(matches) > 1:
            raise WorkerFailure("ambiguous matching PRs")
        return matches[0] if matches else None

    def require_pr(self, **kwargs: object) -> PullRequestState:
        pr = self.find_pr(
            head=str(kwargs["head"]),
            base=str(kwargs["base"]),
            state=str(kwargs["state"]),
        )
        assert pr is not None
        if kwargs.get("expected_head_sha") not in (None, pr.head_sha):
            raise WorkerFailure("PR head SHA mismatch")
        if kwargs.get("expected_base_sha") not in (None, pr.base_sha):
            raise WorkerFailure("PR base SHA mismatch")
        return pr

    def compare_commits(self, *, base_sha: str, head_sha: str) -> str:
        self.comparison_calls.append((base_sha, head_sha))
        if base_sha == head_sha:
            return "identical"
        if (base_sha, head_sha) in self.contains:
            return "ahead"
        if (head_sha, base_sha) in self.contains:
            return "behind"
        return "diverged"


def classify(
    remote_sha: str | None,
    github: TopologyGitHub,
):
    repository = SimpleNamespace(
        inspect_branch=lambda branch: BranchState(
            branch, "stale-local-sha", remote_sha, False
        )
    )
    return classify_issue_topology(
        repository,
        github,
        github,
        issue=158,
        branch="feature/issue-158",
        integration_branch="dev/v1",
        integration_sha="b" * 40,
    )


def test_issue_topology_without_remote_branch_is_new() -> None:
    assert classify(None, TopologyGitHub()).classification == "new"


def test_issue_topology_with_current_base_and_expected_pr_is_recoverable() -> None:
    base_sha = "b" * 40
    feature_sha = "f" * 40
    result = classify(
        feature_sha,
        TopologyGitHub(
            (topology_pr(head_sha=feature_sha, base_sha=base_sha),),
            {(base_sha, feature_sha)},
        ),
    )

    assert result.classification == "recoverable"
    assert result.feature_sha == feature_sha


def test_issue_topology_uses_one_comparison_for_existing_branch() -> None:
    base_sha = "b" * 40
    feature_sha = "f" * 40
    github = TopologyGitHub(contains={(base_sha, feature_sha)})

    assert classify(feature_sha, github).classification == "recoverable"
    assert github.comparison_calls == [(base_sha, feature_sha)]


def test_issue_topology_rejects_branch_that_lacks_current_base() -> None:
    with pytest.raises(WorkerFailure, match=r"Issue #158:.*does not contain"):
        classify("f" * 40, TopologyGitHub())


def test_issue_topology_classifies_authoritatively_integrated_head() -> None:
    base_sha = "b" * 40
    feature_sha = "f" * 40

    result = classify(
        feature_sha,
        TopologyGitHub(
            (topology_pr(state="MERGED", head_sha=feature_sha),),
            {(feature_sha, base_sha)},
        ),
    )

    assert result.classification == "already_integrated"


def test_issue_topology_rejects_stale_merged_pr_not_contained_by_integration() -> None:
    merged = topology_pr(state="MERGED", head_sha="f" * 40)

    with pytest.raises(WorkerFailure, match="is not contained by current integration"):
        classify(None, TopologyGitHub((merged,)))


def test_issue_topology_rejects_closed_unmerged_pr() -> None:
    closed = topology_pr(state="CLOSED")

    with pytest.raises(WorkerFailure, match="closed unmerged PR"):
        classify(None, TopologyGitHub((closed,)))


@pytest.mark.parametrize("state", ["OPEN", "MERGED"])
def test_inline_task_topology_rejects_pr_fingerprint_mismatch(state: str) -> None:
    expected = "a" * 64
    branch = "feature/work-item-refresh-run-help"
    pr = topology_pr(
        state=state,
        head_branch=branch,
        body=f"<!-- agent-workflow-manager:inline-task-sha256:{'c' * 64} -->",
    )
    repository = SimpleNamespace(
        inspect_branch=lambda _branch: BranchState(
            branch, None, "f" * 40 if state == "OPEN" else None, False
        )
    )

    with pytest.raises(WorkerFailure, match="inline task fingerprint"):
        classify_issue_topology(
            repository,
            TopologyGitHub((pr,)),
            TopologyGitHub((pr,)),
            issue="Mini task refresh-run-help",
            branch=branch,
            integration_branch="dev/v1",
            integration_sha="b" * 40,
            inline_task_fingerprint=expected,
        )


def test_issue_topology_rejects_pr_sha_mismatch_and_ambiguity() -> None:
    base_sha = "b" * 40
    feature_sha = "f" * 40
    with pytest.raises(WorkerFailure, match="PR head SHA mismatch"):
        classify(
            feature_sha,
            TopologyGitHub(
                (topology_pr(head_sha="e" * 40),), {(base_sha, feature_sha)}
            ),
        )
    with pytest.raises(WorkerFailure, match="ambiguous matching PRs"):
        classify(
            feature_sha,
            TopologyGitHub(failure=WorkerFailure("ambiguous matching PRs")),
        )


def test_issue_topology_uses_remote_sha_instead_of_stale_local_ref() -> None:
    base_sha = "b" * 40
    feature_sha = "f" * 40

    result = classify(
        feature_sha,
        TopologyGitHub(contains={(base_sha, feature_sha)}),
    )

    assert result.feature_sha == feature_sha
    assert result.classification == "recoverable"


def test_valid_json_preserves_issue_order() -> None:
    config = parse(payload(issues=[90, 89, 91]))

    assert config.issues == (90, 89, 91)
    assert config.merge_final is False
    assert config.make_integration_branch is False
    assert config.policy_issue is None


def test_ordered_work_items_mix_github_issues_and_inline_mini_tasks() -> None:
    value = payload()
    value.pop("issues")
    value["work_items"] = [
        90,
        {"id": "refresh-run-help", "task": "Refresh the New Run help."},
        91,
    ]

    config = parse(value)

    assert [item.as_json() for item in config.work_items] == value["work_items"]
    assert config.issues == (90, 91)
    assert "issues" not in config.as_json()
    assert parse(config.as_json()) == config


def test_one_shot_issue_starts_with_an_empty_round_trip_plan() -> None:
    value = payload()
    value.pop("issues")
    value["one_shot_issue"] = 169

    config = parse(value)

    assert config.one_shot_issue == 169
    assert config.work_items == ()
    assert config.issues == ()
    assert "issues" not in config.as_json()
    assert "work_items" not in config.as_json()
    assert parse(config.as_json()) == config


@pytest.mark.parametrize("one_shot_issue", [None, True, False, 0, -1, "169", 1.5])
def test_one_shot_issue_must_be_a_positive_integer(one_shot_issue: object) -> None:
    value = payload()
    value.pop("issues")
    value["one_shot_issue"] = one_shot_issue

    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(value)

    assert ("$.one_shot_issue", "must be a positive integer") in {
        (finding.path, finding.message) for finding in caught.value.findings
    }


@pytest.mark.parametrize("field", ["issues", "work_items"])
def test_one_shot_issue_cannot_be_combined_with_seed_work_items(field: str) -> None:
    value = payload(one_shot_issue=169)
    if field == "work_items":
        value.pop("issues")
        value["work_items"] = [90]

    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(value)

    assert (
        "$.one_shot_issue",
        "must not be combined with issues or work_items",
    ) in {(finding.path, finding.message) for finding in caught.value.findings}


@pytest.mark.parametrize(
    ("work_item", "path"),
    [
        ({"id": "Bad ID", "task": "Do it"}, "$.work_items[0].id"),
        ({"id": "docs", "task": ""}, "$.work_items[0].task"),
        ({"id": "docs"}, "$.work_items[0].task"),
        (
            {"id": "docs", "task": "Do it", "branch": "custom"},
            "$.work_items[0].branch",
        ),
    ],
)
def test_inline_mini_task_validation_is_structured(
    work_item: object, path: str
) -> None:
    value = payload()
    value.pop("issues")
    value["work_items"] = [work_item]

    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(value)

    assert path in {finding.path for finding in caught.value.findings}


def test_inline_mini_tasks_fit_the_persisted_plan_size_boundary() -> None:
    value = payload()
    value.pop("issues")
    value["work_items"] = [
        {"id": f"task-{index}", "task": "x" * 4000} for index in range(7)
    ]
    value["work_items"].extend(
        [
            {"id": "task-7", "task": "x"},
            {"id": "task-8", "task": "x"},
            {"id": "task-9", "task": "x" * 3602},
        ]
    )

    assert len(parse(value).work_items) == 10

    value["work_items"][-1]["task"] += "x"
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(value)

    assert (
        "$.work_items",
        "serialized recovery state must not exceed 32000 bytes",
    ) in {(finding.path, finding.message) for finding in caught.value.findings}


def test_issues_and_work_items_are_mutually_exclusive() -> None:
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(payload(work_items=[90]))

    assert ("$.work_items", "must not be combined with issues") in {
        (finding.path, finding.message) for finding in caught.value.findings
    }


def test_make_integration_branch_round_trips_when_enabled() -> None:
    config = parse(payload(make_integration_branch=True))

    assert config.make_integration_branch is True
    assert config.as_json()["make_integration_branch"] is True
    assert parse(config.as_json()) == config


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_make_integration_branch_must_be_boolean(value: object) -> None:
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(payload(make_integration_branch=value))

    assert [(finding.path, finding.message) for finding in caught.value.findings] == [
        ("$.make_integration_branch", "must be a boolean")
    ]


def test_generated_workflow_can_create_integration_from_final_branch() -> None:
    config = parse(
        payload(
            integration_branch="dev/v0.2.5",
            final_branch="dev/v0.2.4",
            make_integration_branch=True,
        )
    )

    code = generate_issue_driven_workflow(config)

    assert "base_branch='dev/v0.2.4'" in code
    assert "repository.prepare_feature_branch(\n        'dev/v0.2.5'," in code
    assert "base='dev/v0.2.4'" in code
    assert "expected_base_sha=context.base_sha" in code
    assert "repository.ensure_pushed(\n        'dev/v0.2.5'," in code


def test_generated_workflow_requires_existing_integration_by_default() -> None:
    code = generate_issue_driven_workflow(parse(payload()))
    parse_args = code.split("def parse_args() -> Config:\n", 1)[1].split(
        "def short_error(", 1
    )[0]

    assert "base_branch='dev/v0.2.0'" in parse_args
    assert "prepare_feature_branch(" not in parse_args
    assert "ensure_pushed(" not in parse_args
    assert parse_args.index("inspect_issue_driven_topology(") < parse_args.index(
        "prepare_run_repository("
    )


def test_generated_one_shot_workflow_bootstraps_the_manager_from_source_issue() -> None:
    value = payload()
    value.pop("issues")
    value["one_shot_issue"] = 169

    code = generate_issue_driven_workflow(parse(value))
    parse_args = code.split("def parse_args() -> Config:\n", 1)[1].split(
        "def short_error(", 1
    )[0]

    assert "inspect_issue_driven_topology(" not in parse_args
    assert '        (),\n        "git diff --check",' in parse_args
    assert "        WORKFLOW_POLICY_ISSUE,\n        169," in parse_args
    assert "gh issue view\n{config.one_shot_issue} --repo {config.slug}" in code
    assert "short inline mini tasks" in code
    assert "Do not create GitHub Issues" in code
    assert "one_shot_issue" in code
    assert "One-shot source Issue:" in code

    result = WorkflowValidator(check_timeout=10).validate(code)
    assert result.valid, result.issues
    assert result.dry_run_issues == ()


def test_one_shot_source_issue_is_part_of_recovery_identity() -> None:
    first = load_generated_workflow(one_shot_issue=169)
    second = load_generated_workflow(one_shot_issue=170)
    config_type = first["Config"]
    first_config = config_type(
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true", None, 169
    )
    second_config = config_type(
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true", None, 170
    )

    assert first["plan_seed_fingerprint"](first_config) != second[
        "plan_seed_fingerprint"
    ](second_config)


def test_plan_recovery_fails_closed_when_policy_or_seed_branch_changes() -> None:
    workflow = load_generated_workflow(issues=[90])
    config_type = workflow["Config"]
    issue_type = workflow["Issue"]
    original = config_type(
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        (issue_type(90, "feature/custom-90"),),
        "true",
        200,
    )
    body = workflow["with_work_item_plan"](
        "Base PR", workflow["WorkItemPlan"](original)
    )

    changed_policy = replace(original, policy_issue=201)
    changed_branch = replace(original, issues=(issue_type(90, "feature/another-90"),))
    for changed in (changed_policy, changed_branch):
        with pytest.raises(WorkerFailure, match="does not match the workflow seed"):
            workflow["work_item_plan_from_body"](body, changed)


def test_one_shot_manager_dispatches_mini_task_through_existing_issue_flow() -> None:
    workflow = load_generated_workflow(one_shot_issue=169)
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true", None, 169
    )
    decisions = iter(
        (
            json.dumps(
                {
                    "actions": [
                        {
                            "action": "add",
                            "item": {
                                "id": "focused-change",
                                "task": "Implement the required behavior while preserving the public contract.",
                            },
                        }
                    ],
                    "complete": False,
                    "policy_conflicts": [],
                }
            ),
            json.dumps({"actions": [], "complete": True, "policy_conflicts": []}),
        )
    )
    processed: list[object] = []
    prompts: list[str] = []
    inspected: list[object] = []
    workflow["create_agent"] = lambda *args, **kwargs: "manager"
    workflow["run_turn"] = lambda *_args, **kwargs: (
        prompts.append(_args[3]) or next(decisions)
    )
    workflow["process_issue"] = lambda issue, *_args: processed.append(issue)
    workflow["inspect_dynamic_work_item_topology"] = lambda issue, _config: (
        inspected.append(issue)
    )
    workflow["run_outline_step"] = lambda _name, action: action()
    workflow["persist_work_item_plan"] = lambda plan, *_args: _args[-1]
    plan = workflow["WorkItemPlan"](config)

    effective = workflow["process_work_items"](
        config,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        plan,
    )

    assert [item.key for item in processed] == ["focused-change"]
    assert [item.key for item in inspected] == ["focused-change"]
    assert [item.key for item in effective] == ["focused-change"]
    assert all("gh issue view\n169 --repo acme/project" in prompt for prompt in prompts)


def test_one_shot_plan_rejects_numeric_planner_additions() -> None:
    workflow = load_generated_workflow(one_shot_issue=169)
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true", None, 169
    )
    plan = workflow["WorkItemPlan"](config)

    with pytest.raises(WorkerFailure, match="only inline mini tasks"):
        workflow["apply_planner_decision"](
            plan,
            json.dumps(
                {
                    "actions": [{"action": "add", "item": 169}],
                    "complete": False,
                    "policy_conflicts": [],
                }
            ),
        )

    assert plan.snapshot == ()
    assert plan.position == 0


def test_planner_policy_conflicts_use_a_bounded_json_contract() -> None:
    workflow = load_generated_workflow(issues=[90], policy_issue=200)
    config = workflow["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        (workflow["Issue"](90, "feature/issue-90"),),
        "true",
        200,
    )
    context = workflow["policy_context"](
        config, scope="work-item planning", structured_conflicts=True
    )
    prompt = workflow["planner_prompt"](workflow["WorkItemPlan"](config), config)

    assert "policy_conflicts array" in context
    assert "starting with POLICY_CONFLICT:" not in context
    assert 'keys "actions", "complete", and\n"policy_conflicts"' in prompt
    with pytest.raises(WorkerFailure, match="planner policy conflict is invalid"):
        workflow["apply_planner_decision"](
            workflow["WorkItemPlan"](config),
            json.dumps(
                {
                    "actions": [],
                    "complete": False,
                    "policy_conflicts": ["x" * 501],
                }
            ),
        )


@pytest.mark.parametrize(
    "topology_error",
    [
        "merged PR head is not contained by current integration",
        "closed unmerged PR exists for the dynamic task",
    ],
)
def test_dynamic_topology_failure_remains_undispatched_for_recovery(
    topology_error: str,
) -> None:
    workflow = load_generated_workflow(one_shot_issue=169)
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (), "true", None, 169
    )
    stored_body = "Base PR"
    workflow["create_agent"] = lambda *args, **kwargs: "manager"
    workflow["run_turn"] = lambda *args, **kwargs: json.dumps(
        {
            "actions": [
                {
                    "action": "add",
                    "item": {"id": "dynamic-task", "task": "Do the focused work."},
                }
            ],
            "complete": False,
            "policy_conflicts": [],
        }
    )

    def persist(plan, *_args):
        nonlocal stored_body
        stored_body = workflow["with_work_item_plan"](stored_body, plan)
        return _args[-1]

    workflow["persist_work_item_plan"] = persist
    workflow["inspect_dynamic_work_item_topology"] = lambda *_args: (
        _ for _ in ()
    ).throw(WorkerFailure(topology_error))
    workflow["process_issue"] = lambda *_args: pytest.fail(
        "unsafe dynamic work item was dispatched"
    )

    with pytest.raises(WorkerFailure, match=topology_error):
        workflow["process_work_items"](
            config,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            workflow["WorkItemPlan"](config),
        )

    recovered = workflow["work_item_plan_from_body"](stored_body, config)
    assert recovered.position == 0
    assert [item.key for item in recovered.remaining] == ["dynamic-task"]


def test_optional_policy_issue_round_trips_and_is_generated_deterministically() -> None:
    config = parse(payload(policy_issue=200))

    assert config.policy_issue == 200
    assert config.as_json()["policy_issue"] == 200
    first = generate_issue_driven_workflow(config)
    second = generate_issue_driven_workflow(parse(config.as_json()))
    assert first == second
    assert "WORKFLOW_POLICY_ISSUE = 200" in first
    assert '"git diff --check",\n        WORKFLOW_POLICY_ISSUE,' in first


def test_optional_scenarios_round_trip_into_generated_scenario_gate() -> None:
    scenarios = [
        "Existing: a Prompt run still completes successfully.",
        "New: a one-shot run plans its first mini task.",
        "Failure: invalid planner output is rejected without dispatch.",
    ]
    config = parse(payload(scenarios=scenarios))

    assert config.scenarios == tuple(scenarios)
    assert config.as_json()["scenarios"] == scenarios
    first = generate_issue_driven_workflow(config)
    second = generate_issue_driven_workflow(parse(config.as_json()))
    assert first == second
    assert f"SCENARIOS: tuple[str, ...] = {tuple(scenarios)!r}" in first
    assert "Select a small, risk-relevant subset" in first
    assert "exact Before commit" in first
    assert "exact\nAfter commit" in first
    assert "Do not treat this as a fixed" in first
    assert "expected-output test" in first


@pytest.mark.parametrize(
    ("scenarios", "path", "message"),
    [
        ("scenario", "$.scenarios", "must be an array"),
        ([""], "$.scenarios[0]", "must be a non-empty trimmed string"),
        (
            [" duplicate", "duplicate"],
            "$.scenarios[0]",
            "must be a non-empty trimmed string",
        ),
        (["duplicate", "duplicate"], "$.scenarios[1]", "must be unique"),
    ],
)
def test_scenarios_reject_invalid_human_authored_entries(
    scenarios: object, path: str, message: str
) -> None:
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(payload(scenarios=scenarios))

    assert any(
        finding.path == path and message in finding.message
        for finding in caught.value.findings
    )


def test_scenarios_require_whole_version_review() -> None:
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(payload(scenarios=["Existing behavior"], final_review=False))

    assert [(finding.path, finding.message) for finding in caught.value.findings] == [
        ("$.scenarios", "requires final_review to be true")
    ]


def test_scenario_list_boundary_produces_dispatchable_gate_prompt() -> None:
    scenarios = [f"{'x' * (3999 - len(str(index)))}-{index}" for index in range(15)]
    used = len(
        "\n".join(
            f"{index}. {scenario}" for index, scenario in enumerate(scenarios, 1)
        ).encode()
    )
    final_prefix_bytes = len(f"\n{len(scenarios) + 1}. ".encode())
    final_size = _MAX_SCENARIO_LIST_BYTES - used - final_prefix_bytes
    scenarios.append("y" * final_size)

    config = parse(payload(scenarios=scenarios))
    code = generate_issue_driven_workflow(config)
    module_name = "generated_scenario_boundary"
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(code, "<generated-scenario-boundary>", "exec"), module.__dict__)
    finally:
        del sys.modules[module_name]
    runtime_config = module.__dict__["Config"](
        Path("/repo"),
        "acme/project",
        config.integration_branch,
        config.final_branch,
        (module.__dict__["Issue"](90, "feature/issue-90"),),
        "true",
    )
    prompt = module.__dict__["scenario_gate_prompt"](
        topology_pr(head_sha="h" * 40, base_sha="b" * 40),
        runtime_config,
        runtime_config.issues,
    )

    assert _MAX_SCENARIO_LIST_BYTES < len(prompt.encode()) < 66_000
    completed = subprocess.run(
        [sys.executable, "-c", "import sys; assert sys.argv[1]", prompt],
        check=False,
    )
    assert completed.returncode == 0

    scenarios[-1] += "z"
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(payload(scenarios=scenarios))

    assert [(finding.path, finding.message) for finding in caught.value.findings] == [
        (
            "$.scenarios",
            "numbered Scenario List must encode to at most 64000 UTF-8 bytes",
        )
    ]


def test_scenario_list_aggregate_limit_counts_utf8_bytes() -> None:
    scenarios = [f"{index}:{'界' * (3999 - len(str(index)))}" for index in range(6)]
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(payload(scenarios=scenarios))

    assert (
        "$.scenarios",
        "numbered Scenario List must encode to at most 64000 UTF-8 bytes",
    ) in {(finding.path, finding.message) for finding in caught.value.findings}


@pytest.mark.parametrize(
    "policy_issue", [None, True, False, 0, -1, "200", "\ud800", 1.5]
)
def test_policy_issue_must_be_a_positive_integer(policy_issue: object) -> None:
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(payload(policy_issue=policy_issue))

    assert [(finding.path, finding.message) for finding in caught.value.findings] == [
        ("$.policy_issue", "must be a positive integer")
    ]


def test_policy_issue_must_differ_from_implementation_issues() -> None:
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(payload(policy_issue=89))

    assert [(finding.path, finding.message) for finding in caught.value.findings] == [
        ("$.policy_issue", "must differ from every implementation Issue")
    ]


def test_omitted_agents_default_to_codex_and_serialize_explicitly() -> None:
    config = parse(payload())

    assert config.implementer_agent == "codex"
    assert config.reviewer_agent == "codex"
    assert config.as_json()["implementer_agent"] == "codex"
    assert config.as_json()["reviewer_agent"] == "codex"


@pytest.mark.parametrize(
    ("implementer", "reviewer"),
    [
        ("codex", "codex"),
        ("codex", "claude"),
        ("claude", "codex"),
        ("claude", "claude"),
    ],
)
def test_agent_role_combinations_round_trip(implementer: str, reviewer: str) -> None:
    config = parse(payload(implementer_agent=implementer, reviewer_agent=reviewer))

    assert config.implementer_agent == implementer
    assert config.reviewer_agent == reviewer
    assert parse(config.as_json()) == config


@pytest.mark.parametrize(
    ("omitted", "selected"),
    [("implementer_agent", "reviewer_agent"), ("reviewer_agent", "implementer_agent")],
)
def test_each_agent_role_defaults_independently(omitted: str, selected: str) -> None:
    value = payload()
    value[selected] = "claude"

    config = parse(value)

    assert getattr(config, omitted) == "codex"
    assert getattr(config, selected) == "claude"


@pytest.mark.parametrize("key", ["implementer_agent", "reviewer_agent"])
@pytest.mark.parametrize(
    ("agent", "message"),
    [("other", "must be one of: codex, claude"), (1, "must be a string")],
)
def test_invalid_agent_selection_reports_exact_field_path(
    key: str, agent: object, message: str
) -> None:
    value = payload()
    value[key] = agent

    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(value)

    assert [(finding.path, finding.message) for finding in caught.value.findings] == [
        (f"$.{key}", message)
    ]


@pytest.mark.parametrize("final_review", [True, False])
def test_initial_work_item_count_is_bounded_independently_of_outline(
    final_review: bool,
) -> None:
    maximum = MAX_OUTLINE_ITEMS
    accepted = parse(
        payload(issues=list(range(1, maximum + 1)), final_review=final_review)
    )

    assert len(accepted.issues) == maximum
    code = generate_issue_driven_workflow(accepted)
    outline_issues: list[object] = []
    outline = WorkflowValidator()._validate_outline(ast.parse(code), outline_issues)
    assert outline_issues == []
    assert len(outline) == (3 if final_review else 2)

    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(payload(issues=list(range(1, maximum + 2)), final_review=final_review))

    expected_message = f"must contain at most {maximum} items"
    assert ("$.issues", expected_message) in {
        (finding.path, finding.message) for finding in caught.value.findings
    }


@pytest.mark.parametrize("key", ["integration_branch", "final_branch"])
def test_delivery_branches_cannot_collide_with_generated_issue_branch(
    key: str,
) -> None:
    value = payload(issues=[90, 91])
    value[key] = "feature/issue-91"

    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(value)

    assert (f"$.{key}", "must differ from every generated Issue branch") in {
        (finding.path, finding.message) for finding in caught.value.findings
    }


@pytest.mark.parametrize("key", ["integration_branch", "final_branch"])
@pytest.mark.parametrize("branch", [[], {}])
def test_delivery_branch_containers_report_structured_validation(
    key: str, branch: object
) -> None:
    value = payload()
    value[key] = branch

    with pytest.raises(IssueDrivenValidationError) as caught:
        parse(value)

    assert (f"$.{key}", "must be a non-empty trimmed string") in {
        (finding.path, finding.message) for finding in caught.value.findings
    }


@pytest.mark.parametrize(
    ("source", "path"),
    [
        ("not json", "$"),
        ("[]", "$"),
        (json.dumps({}), "$.repository"),
        (json.dumps(payload(issues=[])), "$.issues"),
        (json.dumps(payload(issues=[90, 90])), "$.issues[1]"),
        (json.dumps(payload(max_reviews=0)), "$.max_reviews"),
        (json.dumps(payload(integration_branch="bad..branch")), "$.integration_branch"),
        (
            json.dumps(payload(integration_branch="foo.lock/bar")),
            "$.integration_branch",
        ),
        (
            json.dumps(payload(integration_branch="bad\u007fbranch")),
            "$.integration_branch",
        ),
        (json.dumps(payload(merge_final="false")), "$.merge_final"),
        (json.dumps(payload(extra=True)), "$.extra"),
        ('{"repository":"a","repository":"b"}', "$.repository"),
    ],
)
def test_invalid_json_and_schema_fields_are_reported(source: str, path: str) -> None:
    with pytest.raises(IssueDrivenValidationError) as caught:
        parse_issue_driven_json(source)

    assert path in {finding.path for finding in caught.value.findings}


def test_generation_is_deterministic_parseable_and_uses_ordered_issues() -> None:
    config = parse(payload(issues=[91, 90, 89]))

    first = generate_issue_driven_workflow(config)
    second = generate_issue_driven_workflow(parse(config.as_json()))

    assert first == second
    ast.parse(first)
    positions = [
        first.index(f"Issue({number}, 'feature/issue-{number}')")
        for number in config.issues
    ]
    assert positions == sorted(positions)
    assert "MAX_REVIEWS = 5" in first


def test_generated_inline_task_uses_same_review_flow_without_github_issue() -> None:
    value = payload()
    value.pop("issues")
    value["work_items"] = [
        90,
        {"id": "refresh-run-help", "task": "Refresh the New Run help."},
    ]
    parsed = parse(value)
    item = parsed.work_items[1]
    assert item.task_fingerprint is not None
    code = generate_issue_driven_workflow(parsed)
    module_name = "generated_inline_work_item"
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(code, "<generated-work-item-workflow>", "exec"), module.__dict__)
    finally:
        del sys.modules[module_name]
    mini = module.__dict__["Issue"](
        None,
        item.branch,
        "refresh-run-help",
        "Refresh the New Run help.",
        item.task_fingerprint,
    )
    config = module.__dict__["parse_args"]()

    implementation, scope_review, correctness_review = module.__dict__["issue_prompts"](
        mini, config
    )

    assert code.index("Issue(90, 'feature/issue-90')") < code.index(
        f"Issue(None, '{item.branch}'"
    )
    assert "'Mini task refresh-run-help'" in code
    assert item.task_fingerprint in code
    assert "Refresh the New Run help." in implementation
    assert "Refresh the New Run help." in scope_review
    assert "Refresh the New Run help." in correctness_review
    assert "gh issue view" not in mini.requirement
    assert item.task_fingerprint in mini.pr_body


def test_inline_task_content_changes_topology_recovery_identity() -> None:
    def inline_config(task: str):
        value = payload()
        value.pop("issues")
        value["work_items"] = [{"id": "refresh-run-help", "task": task}]
        return parse(value)

    first = inline_config("Refresh the New Run help.")
    changed = inline_config("Replace the New Run help.")
    first_item = first.work_items[0]
    changed_item = changed.work_items[0]

    assert first_item.branch == changed_item.branch
    assert first_item.task_fingerprint != changed_item.task_fingerprint
    assert first_item.task_fingerprint in generate_issue_driven_workflow(first)
    assert changed_item.task_fingerprint in generate_issue_driven_workflow(changed)


@pytest.mark.parametrize("state", ["OPEN", "MERGED"])
def test_generated_inline_task_recovery_rejects_pr_fingerprint_mismatch(
    state: str,
) -> None:
    value = payload()
    value.pop("issues")
    value["work_items"] = [
        {"id": "refresh-run-help", "task": "Refresh the New Run help."}
    ]
    parsed = parse(value)
    item = parsed.work_items[0]
    assert item.task_fingerprint is not None
    code = generate_issue_driven_workflow(parsed)
    module_name = f"generated_inline_recovery_{state.lower()}"
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(code, "<generated-inline-recovery>", "exec"), module.__dict__)
    finally:
        del sys.modules[module_name]
    issue = module.__dict__["Issue"](
        None,
        item.branch,
        item.id,
        item.task,
        item.task_fingerprint,
    )
    config = module.__dict__["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v0.2.0",
        "main",
        (issue,),
        "true",
    )
    pr = topology_pr(
        state=state,
        head_branch=item.branch,
        body=f"<!-- agent-workflow-manager:inline-task-sha256:{'c' * 64} -->",
    )
    github = SimpleNamespace(
        find_pr=lambda *, head, base, state: pr if state == pr.state else None
    )

    with pytest.raises(WorkerFailure, match="inline task fingerprint"):
        module.__dict__["prepare_issue"](SimpleNamespace(), github, issue, config)


@pytest.mark.parametrize("final_review", [False, True])
def test_generated_workflow_always_preserves_implementation_principle(
    final_review: bool,
) -> None:
    config = parse(payload(final_review=final_review))

    first = generate_issue_driven_workflow(config)
    second = generate_issue_driven_workflow(parse(config.as_json()))

    assert first == second
    assert "Reuse the existing implementation where appropriate" in first
    assert "minimum required for this Issue" in first
    assert "mixing responsibilities unnaturally" in first
    assert "over-generalizing distinct behavior" in first


@pytest.mark.parametrize(
    ("implementer", "reviewer"),
    [
        ("codex", "codex"),
        ("codex", "claude"),
        ("claude", "codex"),
        ("claude", "claude"),
    ],
)
def test_generated_workflow_selects_role_specific_agents(
    implementer: str, reviewer: str
) -> None:
    code = generate_issue_driven_workflow(
        parse(payload(implementer_agent=implementer, reviewer_agent=reviewer))
    )

    assert f"IMPLEMENTER_AGENT = {implementer!r}" in code
    assert f"REVIEWER_AGENT = {reviewer!r}" in code
    assert "CreateSessionRequest(agent_type, str(config.repo), agent_type" in code


def test_generated_workflow_routes_every_agent_session_by_role() -> None:
    tree = ast.parse(generate_issue_driven_workflow(parse(payload())))
    calls: dict[str, str] = {}
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not isinstance(call.func, ast.Name) or call.func.id != "create_agent":
            continue
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        name = keywords["name"]
        agent_type = keywords["agent_type"]
        assert isinstance(agent_type, ast.Name)
        if isinstance(name, ast.Constant):
            calls[str(name.value)] = agent_type.id
        elif isinstance(name, ast.JoinedStr):
            text = "".join(
                str(value.value)
                for value in name.values
                if isinstance(value, ast.Constant)
            )
            calls[text] = agent_type.id

    assert calls == {
        " worktree cleanup": "IMPLEMENTER_AGENT",
        " implementer": "IMPLEMENTER_AGENT",
        " scope reviewer": "REVIEWER_AGENT",
        " correctness reviewer": "REVIEWER_AGENT",
        "Work-item planner": "REVIEWER_AGENT",
        "Whole-version fixer": "IMPLEMENTER_AGENT",
        "Whole-version reviewer": "REVIEWER_AGENT",
        "Scenario Gate reviewer": "REVIEWER_AGENT",
        "Whole-version cleanup": "IMPLEMENTER_AGENT",
        "Base PR human handoff writer": "REVIEWER_AGENT",
    }


@pytest.mark.parametrize(
    ("final_review", "expected"),
    [
        (
            True,
            (
                "Work items",
                "Whole-version review",
                "Final integration PR",
            ),
        ),
        (
            False,
            ("Work items", "Final integration PR"),
        ),
    ],
)
def test_generated_outline_keeps_dynamic_work_items_in_one_run_unit(
    final_review: bool, expected: tuple[str, ...]
) -> None:
    code = generate_issue_driven_workflow(
        parse(payload(issues=[91, 90, 89], final_review=final_review))
    )

    outline_issues: list[object] = []
    outline = WorkflowValidator()._validate_outline(ast.parse(code), outline_issues)

    assert outline_issues == []
    assert outline == expected
    assert "Inspect authoritative Issue topology" not in code
    assert "Prepare or reuse the feature branch" not in code
    assert "Implement and independently review" not in code


def test_generated_outline_step_reports_completed_skipped_issue_and_failures() -> None:
    code = generate_issue_driven_workflow(parse(payload(issues=[114])))
    module_name = "generated_issue_outline_workflow"
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(code, "<generated-issue-workflow>", "exec"), module.__dict__)
    finally:
        del sys.modules[module_name]
    events: list[tuple[str, str]] = []
    module.__dict__["emit_step"] = lambda name, status, **kwargs: events.append(
        (name, status)
    )

    assert module.__dict__["run_outline_step"]("Issue #114", lambda: None) is None
    with pytest.raises(RuntimeError, match="boom"):
        module.__dict__["run_outline_step"](
            "Final integration PR", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )

    assert events == [
        ("Issue #114", "started"),
        ("Issue #114", "completed"),
        ("Final integration PR", "started"),
        ("Final integration PR", "failed"),
    ]


def test_generated_workflow_passes_supported_static_validation() -> None:
    code = generate_issue_driven_workflow(parse(payload()))

    result = WorkflowValidator(check_timeout=10).validate(code)

    assert result.valid, result.issues
    assert result.dry_run_issues == ()


def test_generated_workflow_uses_coding_agent_delivery_contract() -> None:
    code = generate_issue_driven_workflow(parse(payload()))

    assert "require_committed_result(" in code
    assert "repo.ensure_pushed(" in code
    assert "github.create_draft_pr(" in code
    assert "reviewer requested changes, but" in code
    assert "the implementer re-evaluated the finding" in code
    assert "reviewer requested changes, but the " in code
    assert "continuing without reviewer approval" in code
    assert 'status="warning"' in code
    assert "Commit every intended source, test, and configuration" in code
    assert "Push the exact feature branch" in code
    assert "Create or update exactly one Draft PR" in code
    assert "emit_run_pr(pr.number, pr.url)" in code
    assert "emit_issue_driven_context(" in code
    assert "emit_issue_result(" in code
    assert "emit_whole_review_result(" in code
    assert '"continued_with_warning"' in code
    assert '"skipped"' in code
    assert '{"pr_number": pr.number, "pr_url": pr.url}' in code
    assert "Finish with a clean worktree" in code
    assert "commit SHA and PR number or URL" in code
    assert "You may push" not in code
    for prohibited in (
        "reset, rebase, stash, force-push",
        "merge the work-item PR",
        "create unrelated PRs",
        "discard ambiguous local work",
    ):
        assert prohibited in code


def load_generated_workflow(**overrides: object) -> dict[str, object]:
    value = payload(**overrides)
    if "one_shot_issue" in overrides:
        value.pop("issues")
    code = generate_issue_driven_workflow(parse(value))
    module_name = f"generated_handoff_workflow_{len(sys.modules)}"
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(code, "<generated-handoff-workflow>", "exec"), module.__dict__)
    finally:
        del sys.modules[module_name]
    return module.__dict__


def test_generated_workflow_can_add_update_and_skip_pending_work_items() -> None:
    workflow = load_generated_workflow(issues=[90])
    issue_type = workflow["Issue"]
    config_type = workflow["Config"]
    original_task = "Draft the release notes."
    updated_task = "Draft concise release notes and cover the wording."
    original = issue_type(
        None,
        "feature/work-item-release-notes",
        "release-notes",
        original_task,
        hashlib.sha256(original_task.encode()).hexdigest(),
    )
    updated = issue_type(
        None,
        "feature/work-item-release-notes",
        "release-notes",
        updated_task,
        hashlib.sha256(updated_task.encode()).hexdigest(),
    )
    config = config_type(
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        (original, issue_type(90, "feature/issue-90")),
        "true",
    )
    processed: list[object] = []
    planner_decisions = iter(
        (
            json.dumps(
                {
                    "actions": [
                        {
                            "action": "update",
                            "key": "release-notes",
                            "task": updated_task,
                        },
                        {"action": "skip", "key": 90},
                        {"action": "add", "item": 91},
                    ],
                    "complete": False,
                    "policy_conflicts": [],
                }
            ),
            json.dumps(
                {
                    "actions": [{"action": "add", "item": 92}],
                    "complete": False,
                    "policy_conflicts": [],
                }
            ),
            json.dumps({"actions": [], "complete": False, "policy_conflicts": []}),
            json.dumps({"actions": [], "complete": True, "policy_conflicts": []}),
        )
    )
    workflow["create_agent"] = lambda *args, **kwargs: "planner"
    workflow["run_turn"] = lambda *args, **kwargs: next(planner_decisions)
    workflow["process_issue"] = lambda issue, _config, _client, _repo, _github: (
        processed.append(issue)
    )
    workflow["inspect_dynamic_work_item_topology"] = lambda *_args: None
    workflow["run_outline_step"] = lambda _name, action: action()
    persisted: list[str] = []

    def persist(plan, _config, _repo, _github, pr):
        persisted.append(workflow["serialized_work_item_plan"](plan))
        return pr

    workflow["persist_work_item_plan"] = persist
    plan = workflow["WorkItemPlan"](config)

    effective = workflow["process_work_items"](
        config,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        plan,
    )

    assert [issue.key for issue in processed] == ["release-notes", 91, 92]
    assert processed[0].task == updated_task
    assert [issue.key for issue in effective] == ["release-notes", 91, 92]
    assert [issue.key for issue in config.issues] == ["release-notes", 90]
    assert len(persisted) == 7

    plan = workflow["WorkItemPlan"](config)
    assert plan.take_next() is original
    with pytest.raises(ValueError, match="no unprocessed work item"):
        plan.update("release-notes", updated)
    with pytest.raises(WorkerFailure, match="unsupported shape"):
        workflow["apply_planner_decision"](
            plan,
            json.dumps(
                {
                    "actions": [
                        {"action": "add", "item": 91},
                        {"action": "replace", "key": 90},
                    ],
                    "complete": False,
                    "policy_conflicts": [],
                }
            ),
        )
    assert [issue.key for issue in plan.snapshot] == ["release-notes", 90]


def test_planner_rejects_oversized_plan_transactionally() -> None:
    workflow = load_generated_workflow(issues=[90])
    issue_type = workflow["Issue"]
    config = workflow["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        (issue_type(90, "feature/issue-90"),),
        "true",
    )
    plan = workflow["WorkItemPlan"](config)
    actions = [{"action": "skip", "key": 90}]
    actions.extend(
        {
            "action": "add",
            "item": {"id": f"task-{index}", "task": "x" * 4000},
        }
        for index in range(8)
    )

    with pytest.raises(WorkerFailure, match="recovery state exceeds"):
        workflow["apply_planner_decision"](
            plan,
            json.dumps({"actions": actions, "complete": False, "policy_conflicts": []}),
        )

    assert [issue.key for issue in plan.snapshot] == [90]
    assert plan.position == 0
    assert plan.finalized is False


def test_plan_size_boundary_reserves_multi_digit_dispatch_position() -> None:
    workflow = load_generated_workflow(issues=[90])
    tasks = ["x" * 4000 for _ in range(7)] + ["x", "x", "x" * 3602]
    issues = tuple(
        workflow["planner_inline_issue"](f"task-{index}", task)
        for index, task in enumerate(tasks)
    )
    config = workflow["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        issues,
        "true",
    )
    plan = workflow["WorkItemPlan"](config)

    for _ in range(9):
        assert plan.take_next() is not None
    workflow["serialized_work_item_plan"](plan)
    assert plan.take_next() is not None
    source = workflow["work_item_plan_source"](
        plan, position=plan.position, finalized=False
    )

    assert plan.position == 10
    assert len(source.encode()) == 32_000
    workflow["serialized_work_item_plan"](plan)


def test_planner_persists_undispatched_addition_before_interruption() -> None:
    workflow = load_generated_workflow(issues=[90])
    issue_type = workflow["Issue"]
    config = workflow["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        (issue_type(90, "feature/issue-90"),),
        "true",
    )
    task = "Add the missing recovery test."
    decision = json.dumps(
        {
            "actions": [
                {"action": "add", "item": {"id": "recovery-test", "task": task}}
            ],
            "complete": False,
            "policy_conflicts": [],
        }
    )
    stored_body = "Base PR"

    def persist(plan, _config, _repo, _github, pr):
        nonlocal stored_body
        stored_body = workflow["with_work_item_plan"](stored_body, plan)
        return pr

    workflow["create_agent"] = lambda *args, **kwargs: "planner"
    workflow["run_turn"] = lambda *args, **kwargs: decision
    workflow["persist_work_item_plan"] = persist
    workflow["run_outline_step"] = lambda _name, _action: (_ for _ in ()).throw(
        RuntimeError("interrupted")
    )
    plan = workflow["WorkItemPlan"](config)

    with pytest.raises(RuntimeError, match="interrupted"):
        workflow["process_work_items"](
            config,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            plan,
        )

    recovered = workflow["work_item_plan_from_body"](stored_body, config)
    assert recovered.position == 1
    assert [issue.key for issue in recovered.snapshot] == [90, "recovery-test"]
    assert [issue.key for issue in recovered.remaining] == ["recovery-test"]
    assert recovered.remaining[0].task == task
    with pytest.raises(WorkerFailure, match="missing work-item plan"):
        workflow["work_item_plan_from_body"]("Base PR", config)


def test_custom_issue_branch_round_trips_through_plan_recovery() -> None:
    workflow = load_generated_workflow(issues=[90])
    issue = workflow["Issue"](90, "feature/custom-90")
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "true"
    )
    plan = workflow["WorkItemPlan"](config)
    plan.take_next()
    body = workflow["with_work_item_plan"]("Base PR", plan)

    recovered = workflow["work_item_plan_from_body"](body, config)

    assert recovered.position == 1
    assert recovered.snapshot[0].number == 90
    assert recovered.snapshot[0].branch == "feature/custom-90"


def test_planner_policy_conflict_is_persisted_before_dispatch_and_recovered() -> None:
    code = generate_issue_driven_workflow(parse(payload(issues=[90], policy_issue=200)))

    def load_run(name: str) -> dict[str, object]:
        module = ModuleType(name)
        sys.modules[name] = module
        try:
            exec(compile(code, "<generated-planner-policy>", "exec"), module.__dict__)
        finally:
            del sys.modules[name]
        return module.__dict__

    first = load_run("planner_policy_first")
    issue = first["Issue"](90, "feature/issue-90")
    config = first["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        (issue,),
        "true",
        200,
    )
    plan = first["WorkItemPlan"](config)
    current = replace(
        topology_pr(head_branch=config.integration_branch),
        base_branch=config.main_branch,
        body=first["with_work_item_plan"]("Base PR", plan),
    )

    class GitHub:
        def update_pr_body(self, number: int, **kwargs: object) -> PullRequestState:
            nonlocal current
            assert number == current.number
            current = replace(current, body=str(kwargs["body"]))
            return current

    first["create_agent"] = lambda *args, **kwargs: "planner"
    first["run_turn"] = lambda *args, **kwargs: json.dumps(
        {
            "actions": [],
            "complete": False,
            "policy_conflicts": ["the policy requires a different owner"],
        }
    )
    first["persist_work_item_plan"] = lambda active_plan, _config, _repo, github, pr: (
        github.update_pr_body(
            pr.number,
            body=first["with_base_pr_policy_notes"](
                first["with_work_item_plan"](pr.body, active_plan), _config
            ),
        )
    )
    first["run_outline_step"] = lambda _name, _action: (_ for _ in ()).throw(
        RuntimeError("interrupted before dispatch")
    )

    with pytest.raises(RuntimeError, match="interrupted before dispatch"):
        first["process_work_items"](config, object(), object(), GitHub(), current, plan)

    assert "agent-workflow-manager:policy-conflict:" in current.body
    second = load_run("planner_policy_second")
    second_config = second["Config"](
        config.repo,
        config.slug,
        config.integration_branch,
        config.main_branch,
        (second["Issue"](90, "feature/issue-90"),),
        config.check_command,
        config.policy_issue,
    )

    class RecoveryRepository:
        def synchronize_branch(self, branch: str) -> BranchState:
            return BranchState(branch, current.head_sha, current.head_sha, True)

        def inspect_branch(self, branch: str) -> BranchState:
            return BranchState(branch, current.base_sha, current.base_sha, True)

    class RecoveryGitHub:
        def find_pr(
            self, *, head: str, base: str, state: str
        ) -> PullRequestState | None:
            return current if state == "OPEN" else None

        def require_pr(self, **kwargs: object) -> PullRequestState:
            return current

    _, recovered = second["prepare_work_item_plan_pr"](
        second_config, RecoveryRepository(), RecoveryGitHub()
    )

    assert recovered.position == 1
    context = second["policy_context"](
        second_config, scope="work-item planning", structured_conflicts=True
    )
    assert "different owner" in context


def test_policy_plan_reacquires_base_pr_after_first_child_advances_integration() -> (
    None
):
    workflow = load_generated_workflow(issues=[90, 91], policy_issue=200)
    issue_type = workflow["Issue"]
    config = workflow["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        (
            issue_type(90, "feature/issue-90"),
            issue_type(91, "feature/issue-91"),
        ),
        "true",
        200,
    )
    plan = workflow["WorkItemPlan"](config)
    integration_head = "a" * 40
    main_head = "b" * 40
    current = replace(
        topology_pr(head_sha=integration_head, base_sha=main_head),
        head_branch=config.integration_branch,
        base_branch=config.main_branch,
        body=workflow["with_work_item_plan"](
            "Base PR" + workflow["policy_pr_notes"](config), plan
        ),
    )
    updated_heads: list[str] = []

    class Repository:
        def synchronize_branch(self, branch: str) -> BranchState:
            assert branch == config.integration_branch
            return BranchState(branch, integration_head, integration_head, True)

        def inspect_branch(self, branch: str) -> BranchState:
            assert branch == config.main_branch
            return BranchState(branch, main_head, main_head, True)

    class GitHub:
        def require_pr(self, **kwargs: object) -> PullRequestState:
            assert kwargs["expected_head_sha"] == integration_head
            return current

        def update_pr_body(self, number: int, **kwargs: object) -> PullRequestState:
            nonlocal current
            assert number == current.number
            assert kwargs["expected_head_sha"] == integration_head
            updated_heads.append(str(kwargs["expected_head_sha"]))
            current = replace(current, body=str(kwargs["body"]))
            return current

    decisions = iter(
        (
            {"actions": [], "complete": False, "policy_conflicts": []},
            {"actions": [], "complete": False, "policy_conflicts": []},
            {"actions": [], "complete": True, "policy_conflicts": []},
        )
    )
    processed: list[int] = []

    def process(issue: object, *_args: object) -> None:
        nonlocal current, integration_head
        processed.append(issue.number)
        if issue.number == 90:
            integration_head = "c" * 40
            current = replace(current, head_sha=integration_head)

    workflow["create_agent"] = lambda *args, **kwargs: "planner"
    workflow["run_turn"] = lambda *args, **kwargs: json.dumps(next(decisions))
    workflow["process_issue"] = process
    workflow["inspect_dynamic_work_item_topology"] = lambda *_args: None
    workflow["run_outline_step"] = lambda _name, action: action()

    effective = workflow["process_work_items"](
        config, object(), Repository(), GitHub(), current, plan
    )

    assert processed == [90, 91]
    assert [item.number for item in effective] == [90, 91]
    assert "a" * 40 in updated_heads
    assert "c" * 40 in updated_heads


def test_policy_conflicts_and_base_pr_body_have_aggregate_budgets() -> None:
    workflow = load_generated_workflow(issues=[90], policy_issue=200)
    limit = workflow["MAX_POLICY_CONFLICT_WARNINGS"]
    for index in range(limit):
        workflow["record_policy_conflict"](None, f"warning-{index}")

    with pytest.raises(WorkerFailure, match="policy conflict warning limit"):
        workflow["record_policy_conflict"](None, "one warning too many")
    assert len(workflow["POLICY_CONFLICT_WARNINGS"]) == limit


def test_near_max_plan_conflicts_and_handoff_fail_before_body_mutation() -> None:
    workflow = load_generated_workflow(issues=[90], policy_issue=200)
    tasks = ["x" * 4000 for _ in range(7)] + ["x", "x", "x" * 3602]
    issues = tuple(
        workflow["planner_inline_issue"](f"task-{index}", task)
        for index, task in enumerate(tasks)
    )
    config = workflow["Config"](
        Path("/repo"), "acme/project", "dev/v1", "main", issues, "true", 200
    )
    for index in range(workflow["MAX_POLICY_CONFLICT_WARNINGS"]):
        workflow["record_policy_conflict"](
            None,
            f"Policy Issue #200 conflicts with work-item planning: {index}-"
            f"{'x' * 500}; continuing with the implementation work item as the "
            "primary requirement.",
        )
    plan = workflow["WorkItemPlan"](config)
    body = workflow["with_work_item_plan"](
        "Base PR" + workflow["policy_pr_notes"](config), plan
    )
    pr = replace(
        topology_pr(head_branch=config.integration_branch, body=body),
        base_branch=config.main_branch,
    )
    handoff = f"""## 概要

{"あ" * 11_000}

## 主な変更

https://github.com/acme/project/issues/200

## 人間による確認

- [ ] 表示を確認できる

## 自動検証

- passed

## 注意事項

- Policy conflict warnings are recorded."""
    mutations: list[str] = []
    workflow["create_agent"] = lambda *args, **kwargs: "writer"
    workflow["run_turn"] = lambda *args, **kwargs: handoff
    workflow["emit_finding"] = lambda *args, **kwargs: None

    class GitHub:
        def update_pr_body(self, *args: object, **kwargs: object) -> PullRequestState:
            mutations.append("update")
            return pr

    validated = workflow["validate_human_handoff"](handoff, config, has_warnings=True)
    with pytest.raises(WorkerFailure, match="Base PR body exceeds"):
        workflow["with_human_handoff"](body, validated)

    unchanged = workflow["update_base_pr_human_handoff"](
        config,
        issues,
        object(),
        GitHub(),
        pr,
        workflow["ReviewDelivery"]("approved", pr.head_sha, pr.base_sha),
    )

    assert unchanged is pr
    assert mutations == []


@pytest.mark.parametrize("child_state", ["OPEN", "MERGED"])
def test_interrupted_dispatched_item_is_reinspected_before_planning(
    child_state: str,
) -> None:
    workflow = load_generated_workflow(issues=[90])
    issue_type = workflow["Issue"]
    config = workflow["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        (issue_type(90, "feature/issue-90"),),
        "true",
    )
    stored_body = "Base PR"
    events: list[str] = []
    issue = config.issues[0]
    child_pr = topology_pr(
        number=190,
        state=child_state,
        head_branch=issue.branch,
    )

    class GitHub:
        def find_pr(self, *, head: str, base: str, state: str):
            assert head == issue.branch
            assert base == config.integration_branch
            return child_pr if state == child_state else None

    repository = SimpleNamespace(
        require_clean=lambda: None,
        synchronize_branch=lambda branch: BranchState(
            branch, child_pr.head_sha, child_pr.head_sha, True
        ),
        inspect_feature_preparation=lambda *args, **kwargs: SimpleNamespace(
            base_is_ancestor=True
        ),
    )
    github = GitHub()

    def persist(plan, _config, _repo, _github, pr):
        nonlocal stored_body
        stored_body = workflow["with_work_item_plan"](stored_body, plan)
        return pr

    def interrupted_child(*_args):
        events.append(f"child-{child_state.lower()}")
        raise RuntimeError("interrupted after child mutation")

    workflow["create_agent"] = lambda *args, **kwargs: "planner"
    workflow["run_turn"] = lambda *args, **kwargs: json.dumps(
        {"actions": [], "complete": False, "policy_conflicts": []}
    )
    workflow["persist_work_item_plan"] = persist
    workflow["process_issue"] = interrupted_child
    workflow["run_outline_step"] = lambda _name, action: action()

    with pytest.raises(RuntimeError, match="after child mutation"):
        workflow["process_work_items"](
            config,
            SimpleNamespace(),
            repository,
            github,
            SimpleNamespace(),
            workflow["WorkItemPlan"](config),
        )

    recovered = workflow["work_item_plan_from_body"](stored_body, config)
    assert recovered.position == 1
    events.clear()
    prepared: list[object] = []
    prepare_issue = workflow["prepare_issue"]

    def reinspect(*_args):
        events.append(f"reinspect-{child_state.lower()}")
        prepared.append(prepare_issue(repository, github, issue, config))

    workflow["process_issue"] = reinspect
    workflow["create_agent"] = lambda *args, **kwargs: events.append("planner") or "p"
    workflow["run_turn"] = lambda *args, **kwargs: json.dumps(
        {"actions": [], "complete": True, "policy_conflicts": []}
    )

    workflow["process_work_items"](
        config,
        SimpleNamespace(),
        repository,
        github,
        SimpleNamespace(),
        recovered,
    )

    assert events == [f"reinspect-{child_state.lower()}", "planner"]
    if child_state == "OPEN":
        assert prepared[0][0] is child_pr
    else:
        assert prepared == [child_pr]


def test_ready_base_pr_recovery_is_validated_before_draft_mutation() -> None:
    workflow = load_generated_workflow(issues=[90])
    issue_type = workflow["Issue"]
    config = workflow["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        (issue_type(90, "feature/issue-90"),),
        "true",
    )
    ready = replace(
        topology_pr(
            number=190,
            head_branch=config.integration_branch,
            body="Base PR without recovery state",
        ),
        is_draft=False,
        base_branch=config.main_branch,
    )
    mutations: list[str] = []

    class GitHub:
        def find_pr(self, *, head: str, base: str, state: str):
            assert head == config.integration_branch
            assert base == config.main_branch
            return ready if state == "OPEN" else None

        def set_draft(self, *args, **kwargs):
            mutations.append("set-draft")
            return replace(ready, is_draft=True)

    repository = SimpleNamespace(
        synchronize_branch=lambda branch: BranchState(
            branch, ready.head_sha, ready.head_sha, True
        ),
        inspect_branch=lambda branch: BranchState(
            branch, ready.base_sha, ready.base_sha, True
        ),
    )
    workflow["emit_finding"] = lambda *args, **kwargs: None

    with pytest.raises(WorkerFailure, match="missing work-item plan"):
        workflow["prepare_work_item_plan_pr"](config, repository, GitHub())

    assert mutations == []


def test_recovered_dynamic_plan_reuses_open_and_merged_pr_topology() -> None:
    workflow = load_generated_workflow(issues=[90])
    issue_type = workflow["Issue"]
    config = workflow["Config"](
        Path("/repo"),
        "acme/project",
        "dev/v1",
        "main",
        (issue_type(90, "feature/issue-90"),),
        "true",
    )
    task = "Document the recovered dynamic work."
    dynamic_mini = workflow["planner_inline_issue"]("recovery-docs", task)
    plan = workflow["WorkItemPlan"](config)
    plan.skip(90)
    plan.add(issue_type(91, "feature/issue-91"))
    plan.add(dynamic_mini)
    plan.position = len(plan.items)
    plan.finalized = True
    body = workflow["with_work_item_plan"]("Base PR", plan)
    recovered = workflow["work_item_plan_from_body"](body, config)
    dynamic_issue, recovered_mini = recovered.snapshot
    open_dynamic = topology_pr(number=191, head_branch=dynamic_issue.branch)
    merged_mini = topology_pr(
        number=192,
        state="MERGED",
        head_branch=recovered_mini.branch,
        body=recovered_mini.pr_body,
    )

    class GitHub:
        def find_pr(self, *, head: str, base: str, state: str):
            assert base == config.integration_branch
            for pr in (open_dynamic, merged_mini):
                if pr.head_branch == head and pr.state == state:
                    return pr
            return None

    repository = SimpleNamespace(
        require_clean=lambda: None,
        synchronize_branch=lambda branch: BranchState(
            branch,
            open_dynamic.head_sha if branch == dynamic_issue.branch else "b" * 40,
            open_dynamic.head_sha if branch == dynamic_issue.branch else "b" * 40,
            True,
        ),
        inspect_feature_preparation=lambda *args, **kwargs: SimpleNamespace(
            base_is_ancestor=True
        ),
    )

    github = GitHub()
    prepared: list[object] = []
    prepare_issue = workflow["prepare_issue"]
    workflow["process_issue"] = lambda issue, *_args: prepared.append(
        prepare_issue(repository, github, issue, config)
    )
    workflow["inspect_dynamic_work_item_topology"] = lambda *_args: None
    workflow["run_outline_step"] = lambda _name, action: action()
    workflow["create_agent"] = lambda *args, **kwargs: pytest.fail(
        "a finalized recovered plan must not restart its planner"
    )

    effective = workflow["process_work_items"](
        config,
        SimpleNamespace(),
        repository,
        github,
        SimpleNamespace(),
        recovered,
    )

    assert prepared[0][0] is open_dynamic
    assert prepared[1] is merged_mini
    assert [issue.key for issue in effective] == [91, "recovery-docs"]


def test_generated_setup_pushes_exact_final_head_as_new_integration_base() -> None:
    workflow = load_generated_workflow(
        integration_branch="dev/v0.2.5",
        final_branch="dev/v0.2.4",
        make_integration_branch=True,
    )
    final_sha = "f" * 40
    calls: list[tuple[object, ...]] = []
    repository = SimpleNamespace(expected_github_slug="acme/project")

    def prepare_feature_branch(
        branch: str, *, base: str, expected_base_sha: str
    ) -> BranchState:
        calls.append(("prepare", branch, base, expected_base_sha))
        return BranchState(branch, final_sha, None, True)

    def ensure_pushed(branch: str, *, expected_local_sha: str) -> BranchState:
        calls.append(("push", branch, expected_local_sha))
        return BranchState(branch, final_sha, final_sha, True)

    repository.prepare_feature_branch = prepare_feature_branch
    repository.ensure_pushed = ensure_pushed
    workflow["prepare_run_repository"] = lambda **kwargs: SimpleNamespace(
        execution_root=Path("/run"), base_sha=final_sha
    )
    workflow["inspect_issue_driven_topology"] = lambda **kwargs: ()
    workflow["GitRepository"] = SimpleNamespace(open=lambda *args, **kwargs: repository)

    config = workflow["parse_args"]()  # type: ignore[operator]

    assert config.integration_branch == "dev/v0.2.5"
    assert config.main_branch == "dev/v0.2.4"
    assert calls == [
        ("prepare", "dev/v0.2.5", "dev/v0.2.4", final_sha),
        ("push", "dev/v0.2.5", final_sha),
    ]


def test_human_handoff_prompt_and_validation_contract() -> None:
    secret_check_command = "API_TOKEN=sentinel-secret pytest"
    workflow = load_generated_workflow(
        issues=[138], policy_issue=200, reviewer_agent="claude"
    )
    issue = workflow["Issue"](138, "feature/issue-138")  # type: ignore[operator]
    config = workflow["Config"](  # type: ignore[operator]
        Path("/repo"),
        "eletim/agent-workflow-manager",
        "dev/v0.2.4",
        "main",
        (issue,),
        secret_check_command,
        200,
    )
    pr = PullRequestState(
        201,
        "https://github.com/eletim/agent-workflow-manager/pull/201",
        "OPEN",
        False,
        config.slug,
        config.integration_branch,
        "h" * 40,
        config.slug,
        config.main_branch,
        "b" * 40,
        None,
        False,
        None,
        "PR_201",
        "existing",
    )
    delivery = workflow["ReviewDelivery"]("approved", pr.head_sha, pr.base_sha, 2)  # type: ignore[operator]
    prompt = workflow["human_handoff_prompt"](  # type: ignore[operator]
        config, (issue,), pr, delivery, ()
    )

    assert "Reviewer role Agent selected by reviewer_agent" in prompt
    assert "gh issue view NUMBER" in prompt
    assert "Issue numbers: 138" in prompt
    assert "environment values, credentials, tokens, or secrets" in prompt
    assert secret_check_command not in prompt
    assert "sentinel-secret" not in prompt
    assert "configured final checks passed on the exact head" in prompt
    assert "quickly answerable\nYes/No observation" in prompt
    assert "do not require terminal\ncommands" in prompt
    assert "state=Ready" in prompt

    markdown = """## 概要

変更内容を短く説明します。

## 主な変更

- 引き渡し本文を生成します。

## 人間による確認

- [ ] ブラウザでBase PRを開くと日本語の概要が表示される

## 自動検証

- pytest: passed

方針: https://github.com/eletim/agent-workflow-manager/issues/200"""
    validated = workflow["validate_human_handoff"](  # type: ignore[operator]
        markdown, config, has_warnings=False
    )
    assert validated == markdown

    with pytest.raises(WorkerFailure, match="section contract"):
        workflow["validate_human_handoff"](  # type: ignore[operator]
            markdown + "\n\n## 余分", config, has_warnings=False
        )

    english = """## 概要

Summary.

## 主な変更

- Change.

## 人間による確認

- [ ] The page opens

## 自動検証

- pytest: passed

https://github.com/eletim/agent-workflow-manager/issues/200"""
    with pytest.raises(WorkerFailure, match="Japanese"):
        workflow["validate_human_handoff"](  # type: ignore[operator]
            english, config, has_warnings=False
        )


def test_managed_handoff_replacement_preserves_existing_metadata() -> None:
    workflow = load_generated_workflow(issues=[138])
    start = workflow["HUMAN_HANDOFF_START"]
    end = workflow["HUMAN_HANDOFF_END"]
    existing = (
        "Intro\n\n<!-- agent-workflow-manager:create-pr:run-1 -->\n\n"
        f"{start}\nold handoff\n{end}\n\n"
        "<!-- agent-workflow-manager:policy-conflict:c2FmZQ== -->"
    )

    updated = workflow["with_human_handoff"](existing, "new handoff")  # type: ignore[operator]

    assert "old handoff" not in updated
    assert updated.count(start) == updated.count(end) == 1
    assert "create-pr:run-1" in updated
    assert "policy-conflict:c2FmZQ==" in updated


@pytest.mark.parametrize(
    ("draft", "outcome"),
    [(False, "approved"), (True, "continued_with_warning")],
)
def test_handoff_updates_ready_or_warning_draft_without_changing_state(
    draft: bool, outcome: str
) -> None:
    workflow = load_generated_workflow(issues=[138])
    issue = workflow["Issue"](138, "feature/issue-138")  # type: ignore[operator]
    config = workflow["Config"](  # type: ignore[operator]
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "pytest"
    )
    pr = PullRequestState(
        8,
        "https://github.com/acme/project/pull/8",
        "OPEN",
        draft,
        config.slug,
        config.integration_branch,
        "h" * 40,
        config.slug,
        config.main_branch,
        "b" * 40,
        None,
        False,
        None,
        "PR_8",
        "<!-- agent-workflow-manager:create-pr:run-8 -->",
    )
    warnings = ("レビュー警告があります。",) if draft else ()
    delivery = workflow["ReviewDelivery"](  # type: ignore[operator]
        outcome, pr.head_sha, pr.base_sha, 1, warnings
    )
    markdown = """## 概要

変更の概要です。

## 主な変更

- Base PRの説明を改善します。

## 人間による確認

- [ ] ブラウザで説明が読みやすく表示される

## 自動検証

- pytest: passed"""
    if draft:
        markdown += "\n\n## 注意事項\n\n- レビュー警告があります。"
    workflow["create_agent"] = lambda *args, **kwargs: "writer"
    workflow["run_turn"] = lambda *args, **kwargs: markdown

    class GitHub:
        def update_pr_body(self, number: int, **kwargs: object) -> PullRequestState:
            assert number == pr.number
            return replace(pr, body=str(kwargs["body"]))

    updated = workflow["update_base_pr_human_handoff"](  # type: ignore[operator]
        config, (issue,), object(), GitHub(), pr, delivery
    )

    assert updated.is_draft is draft
    assert "## 人間による確認" in updated.body
    assert "create-pr:run-8" in updated.body


def test_handoff_failure_warns_but_mutation_unknown_remains_fail_closed() -> None:
    workflow = load_generated_workflow(issues=[138])
    issue = workflow["Issue"](138, "feature/issue-138")  # type: ignore[operator]
    config = workflow["Config"](  # type: ignore[operator]
        Path("/repo"), "acme/project", "dev/v1", "main", (issue,), "pytest"
    )
    pr = PullRequestState(
        8,
        "https://github.com/acme/project/pull/8",
        "OPEN",
        False,
        config.slug,
        config.integration_branch,
        "h" * 40,
        config.slug,
        config.main_branch,
        "b" * 40,
        None,
        False,
        None,
        "PR_8",
        "existing",
    )
    delivery = workflow["ReviewDelivery"]("approved", pr.head_sha, pr.base_sha)  # type: ignore[operator]
    findings: list[tuple[str, str, str]] = []
    workflow["emit_finding"] = lambda category, message, status="passed": (
        findings.append((category, message, status))
    )
    workflow["create_agent"] = lambda *args, **kwargs: (_ for _ in ()).throw(
        WorkerFailure("agent timed out")
    )

    unchanged = workflow["update_base_pr_human_handoff"](  # type: ignore[operator]
        config, (issue,), object(), object(), pr, delivery
    )
    assert unchanged is pr
    assert findings[-1][2] == "warning"

    valid = """## 概要

概要です。

## 主な変更

- 変更です。

## 人間による確認

- [ ] ブラウザで表示を確認できる

## 自動検証

- pytest: passed"""
    workflow["create_agent"] = lambda *args, **kwargs: "writer"
    workflow["run_turn"] = lambda *args, **kwargs: valid

    class UnknownGitHub:
        def update_pr_body(self, *args: object, **kwargs: object) -> PullRequestState:
            raise MutationOutcomeUnknown("response lost")

    with pytest.raises(MutationOutcomeUnknown, match="response lost"):
        workflow["update_base_pr_human_handoff"](  # type: ignore[operator]
            config, (issue,), object(), UnknownGitHub(), pr, delivery
        )


def test_policy_issue_is_read_first_by_design_roles_and_referenced_by_base_pr() -> None:
    code = generate_issue_driven_workflow(parse(payload(policy_issue=200)))

    assert "Before doing anything else, run `gh issue view" in code
    assert "policy_context(config, scope=issue.label)" in code
    assert 'policy_context(config, scope=f"fixes for {issue.label}")' in code
    assert 'policy_context(config, scope="the whole-version review")' in code
    assert 'policy_context(config, scope="whole-version fixes")' in code
    assert "https://github.com/{config.slug}/issues/{config.policy_issue}" in code
    assert "ensure_base_pr_policy_notes(github, pr, config)" in code
    assert "github.update_pr_body(" in code


def test_policy_conflict_marker_emits_warning_and_preserves_child_precedence() -> None:
    code = generate_issue_driven_workflow(parse(payload(policy_issue=200)))
    module_name = "generated_policy_issue_workflow"
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(code, "<generated-policy-workflow>", "exec"), module.__dict__)
    finally:
        del sys.modules[module_name]
    findings: list[tuple[str, str, str]] = []
    module.__dict__["emit_finding"] = lambda category, message, status="completed": (
        findings.append((category, message, status))
    )
    config = module.__dict__["Config"](
        Path("/repo"),
        "eletim/agent-workflow-manager",
        "dev/v0.2.0",
        "main",
        (),
        "true",
        200,
    )

    module.__dict__["emit_policy_conflicts"](
        "APPROVED\nPOLICY_CONFLICT: child explicitly chooses the other API",
        config,
        scope="review of Issue #90",
    )

    assert len(findings) == 1
    assert findings[0][0] == "policy_issue"
    assert findings[0][2] == "warning"
    assert "child explicitly chooses the other API" in findings[0][1]
    assert "implementation work item as the primary requirement" in findings[0][1]


def test_policy_conflict_survives_interrupted_run_and_merged_issue_skip() -> None:
    code = generate_issue_driven_workflow(parse(payload(issues=[90], policy_issue=200)))

    def load_run(name: str) -> dict[str, object]:
        module = ModuleType(name)
        sys.modules[name] = module
        try:
            exec(compile(code, "<generated-policy-workflow>", "exec"), module.__dict__)
        finally:
            del sys.modules[name]
        return module.__dict__

    first_run = load_run("generated_policy_first_run")
    issue = first_run["Issue"](90, "feature/issue-90")  # type: ignore[operator]
    config = first_run["Config"](  # type: ignore[operator]
        Path("/repo"),
        "eletim/agent-workflow-manager",
        "dev/v0.2.0",
        "main",
        (issue,),
        "true",
        200,
    )
    child_pr = PullRequestState(
        90,
        "https://example.test/pull/90",
        "OPEN",
        True,
        config.slug,
        issue.branch,
        "child-head",
        config.slug,
        config.integration_branch,
        "integration-head",
        None,
        False,
        None,
        "PR_90",
        "Sequential implementation of Issue #90.",
    )

    class ChildGitHub:
        def update_pr_body(self, number: int, **kwargs: object) -> PullRequestState:
            assert number == child_pr.number
            return replace(child_pr, body=str(kwargs["body"]))

    first_run["emit_finding"] = lambda *args, **kwargs: None
    first_run["emit_policy_conflicts"](  # type: ignore[operator]
        "POLICY_CONFLICT: child requires the legacy API",
        config,
        scope="implementation Issue #90",
        issue_number=90,
    )
    persisted = first_run["ensure_issue_pr_policy_conflicts"](  # type: ignore[operator]
        ChildGitHub(), child_pr, issue, config
    )
    assert "agent-workflow-manager:policy-conflict:" in persisted.body

    # A new module models recovery after interruption; no process-local warning
    # state crosses this boundary and the already-merged Issue is skipped.
    second_run = load_run("generated_policy_second_run")
    recovered_issue = second_run["Issue"](90, "feature/issue-90")  # type: ignore[operator]
    recovered_config = second_run["Config"](  # type: ignore[operator]
        Path("/repo"),
        config.slug,
        config.integration_branch,
        config.main_branch,
        (recovered_issue,),
        "true",
        200,
    )
    merged = replace(persisted, state="MERGED", is_draft=False)
    findings: list[tuple[str, str, str]] = []
    process_globals = second_run["process_issue"].__globals__  # type: ignore[attr-defined]
    process_globals["prepare_issue"] = lambda *args: merged
    process_globals["emit_finding"] = lambda category, message, status="completed": (
        findings.append((category, message, status))
    )

    class CleanRepository:
        def inspect_worktree(self) -> SimpleNamespace:
            return SimpleNamespace(dirty=False)

    recovered = second_run["process_issue"](  # type: ignore[operator]
        recovered_issue, recovered_config, object(), CleanRepository(), object()
    )

    assert recovered is merged
    assert findings and findings[0][2] == "warning"
    warning = findings[0][1]
    whole_review_context = second_run["policy_context"](  # type: ignore[operator]
        recovered_config, scope="the whole-version review"
    )
    assert warning in whole_review_context

    base_pr = replace(
        child_pr,
        number=200,
        head_branch=recovered_config.integration_branch,
        head_sha="integration-head",
        base_branch=recovered_config.main_branch,
        base_sha="main-head",
        body="Sequential integration.",
    )
    delivered_bodies: list[str] = []

    class BaseGitHub:
        def update_pr_body(self, number: int, **kwargs: object) -> PullRequestState:
            assert number == base_pr.number
            delivered_bodies.append(str(kwargs["body"]))
            return replace(base_pr, body=delivered_bodies[-1])

    second_run["ensure_base_pr_policy_notes"](  # type: ignore[operator]
        BaseGitHub(), base_pr, recovered_config
    )
    assert delivered_bodies and warning in delivered_bodies[-1]


def test_whole_version_conflict_is_rehydrated_from_base_pr_after_interruption() -> None:
    code = generate_issue_driven_workflow(parse(payload(issues=[90], policy_issue=200)))

    def load_run(name: str) -> dict[str, object]:
        module = ModuleType(name)
        sys.modules[name] = module
        try:
            exec(compile(code, "<generated-policy-workflow>", "exec"), module.__dict__)
        finally:
            del sys.modules[name]
        return module.__dict__

    first_run = load_run("generated_whole_policy_first_run")
    first_config = first_run["Config"](  # type: ignore[operator]
        Path("/repo"),
        "eletim/agent-workflow-manager",
        "dev/v0.2.0",
        "main",
        (),
        "true",
        200,
    )
    base_pr = PullRequestState(
        200,
        "https://example.test/pull/200",
        "OPEN",
        True,
        first_config.slug,
        first_config.integration_branch,
        "integration-head",
        first_config.slug,
        first_config.main_branch,
        "main-head",
        None,
        False,
        None,
        "PR_200",
        "Sequential integration.",
    )

    class FirstGitHub:
        def update_pr_body(self, number: int, **kwargs: object) -> PullRequestState:
            assert number == base_pr.number
            return replace(base_pr, body=str(kwargs["body"]))

    first_run["emit_finding"] = lambda *args, **kwargs: None
    first_run["emit_policy_conflicts"](  # type: ignore[operator]
        "APPROVED\nPOLICY_CONFLICT: integrated features disagree on ownership",
        first_config,
        scope="the integrated version",
    )
    persisted = first_run["ensure_base_pr_policy_notes"](  # type: ignore[operator]
        FirstGitHub(), base_pr, first_config
    )
    assert "agent-workflow-manager:policy-conflict:" in persisted.body

    # Simulate interruption immediately after persistence and start a fresh run.
    second_run = load_run("generated_whole_policy_second_run")
    second_config = second_run["Config"](  # type: ignore[operator]
        Path("/repo"),
        first_config.slug,
        first_config.integration_branch,
        first_config.main_branch,
        (),
        "true",
        200,
    )
    current = persisted
    findings: list[tuple[str, str, str]] = []
    reviewer_contexts: list[str] = []

    class Repository:
        def synchronize_branch(self, branch: str) -> BranchState:
            assert branch == second_config.integration_branch
            return BranchState(branch, current.head_sha, current.head_sha, True)

        def inspect_branch(self, branch: str) -> BranchState:
            assert branch == second_config.main_branch
            return BranchState(branch, current.base_sha, current.base_sha, True)

    class SecondGitHub:
        def find_pr(
            self, *, head: str, base: str, state: str
        ) -> PullRequestState | None:
            return current if state == "OPEN" else None

        def require_pr(self, **kwargs: object) -> PullRequestState:
            return current

        def update_pr_body(self, number: int, **kwargs: object) -> PullRequestState:
            nonlocal current
            assert number == current.number
            current = replace(current, body=str(kwargs["body"]))
            return current

        def set_draft(self, number: int, **kwargs: object) -> PullRequestState:
            nonlocal current
            current = replace(current, is_draft=False)
            return current

    integration_globals = second_run["integration_delivery"].__globals__  # type: ignore[attr-defined]
    integration_globals["emit_finding"] = lambda category, message, status="completed": (
        findings.append((category, message, status))
    )

    def recovered_review(*args: object) -> tuple[PullRequestState, object]:
        context = integration_globals["policy_context"](
            second_config, scope="the whole-version review"
        )
        reviewer_contexts.append(context)
        delivery = integration_globals["ReviewDelivery"](
            "approved", current.head_sha, current.base_sha
        )
        return current, delivery

    integration_globals["review_whole_version"] = recovered_review
    integration_globals["update_base_pr_human_handoff"] = (
        lambda config, work_items, client, github, pr, delivery: pr
    )

    delivered = second_run["integration_delivery"](  # type: ignore[operator]
        second_config, second_config.issues, object(), Repository(), SecondGitHub()
    )

    assert delivered.is_draft is False
    warnings = [message for _, message, status in findings if status == "warning"]
    assert len(warnings) == 1
    warning = warnings[0]
    assert reviewer_contexts and warning in reviewer_contexts[0]
    assert warning in delivered.body


def test_without_policy_issue_keeps_legacy_prompt_semantics() -> None:
    code = generate_issue_driven_workflow(parse(payload()))
    module_name = "generated_without_policy_issue_workflow"
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(code, "<generated-policy-workflow>", "exec"), module.__dict__)
    finally:
        del sys.modules[module_name]
    config = module.__dict__["Config"](
        Path("/repo"),
        "eletim/agent-workflow-manager",
        "dev/v0.2.0",
        "main",
        (),
        "true",
    )
    issue = module.__dict__["Issue"](90, "feature/issue-90")

    implementation, scope_review, correctness_review = module.__dict__["issue_prompts"](
        issue, config
    )

    assert implementation.startswith("Implement Issue #90")
    assert scope_review.startswith("Perform only the Scope / Design Review for")
    assert correctness_review.startswith("Perform only the Correctness Review for")
    assert "policy Issue" not in implementation
    assert "POLICY_CONFLICT" not in scope_review
    assert "POLICY_CONFLICT" not in correctness_review


def test_generated_workflow_has_focused_dirty_worktree_recovery() -> None:
    code = generate_issue_driven_workflow(parse(payload()))

    assert "Your only task is to make the current repository state clean" in code
    assert "Preserve and commit all intended source, test, and" in code
    assert ".gitignore entries" in code
    assert "Remove only clearly disposable generated" in code
    assert "Do not reinterpret or reimplement the original Issue" in code
    assert "Do not push, modify PR state, merge, start a review" in code
    assert "cleanup turn could not safely resolve the worktree" in code
    assert "approval invalidated" in code


def test_generated_post_merge_path_starts_next_issue() -> None:
    code = generate_issue_driven_workflow(parse(payload(issues=[107, 108])))
    module_name = "generated_issue_workflow"
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(code, "<generated-issue-workflow>", "exec"), module.__dict__)
    finally:
        del sys.modules[module_name]
    workflow = module.__dict__
    issue_type = workflow["Issue"]
    config_type = workflow["Config"]
    merge_pr_and_advance = workflow["merge_pr_and_advance"]
    prepare_issue = workflow["prepare_issue"]
    assert callable(issue_type)
    assert callable(config_type)
    assert callable(merge_pr_and_advance)
    assert callable(prepare_issue)

    base_sha = "1" * 40
    reviewed_head = "2" * 40
    merge_sha = "3" * 40
    integration_branch = "dev/v0.2.1"
    first_branch = "feature/issue-107"
    next_branch = "feature/issue-108"
    events: list[str] = []

    class Repository:
        def __init__(self) -> None:
            self.current = first_branch
            self.integration_sha = base_sha

        def synchronize_branch(self, branch: str) -> BranchState:
            assert branch == integration_branch
            events.append(f"synchronize:{self.current}->{branch}")
            self.current = branch
            return BranchState(branch, self.integration_sha, self.integration_sha, True)

        def advance_after_merge(
            self,
            branch: str,
            *,
            previous_sha: str,
            merge_commit_sha: str,
            required_commit_sha: str,
        ) -> BranchState:
            events.append("advance")
            assert self.current == branch == integration_branch
            assert self.integration_sha == previous_sha == base_sha
            assert merge_commit_sha == merge_sha
            assert required_commit_sha == reviewed_head
            self.integration_sha = merge_sha
            return BranchState(branch, merge_sha, merge_sha, True)

        def require_clean(self) -> None:
            events.append("require_clean")

        def recover_feature_branch(
            self, branch: str, *, base: str, expected_base_sha: str
        ) -> SimpleNamespace:
            events.append(f"recover:{branch}")
            assert self.current == base == integration_branch
            assert expected_base_sha == self.integration_sha == merge_sha
            self.current = branch
            return SimpleNamespace(
                branch=BranchState(branch, merge_sha, None, True),
                reused_existing_work=False,
            )

    repository = Repository()

    class GitHub:
        def merge_pr(self, number: int, **kwargs: object) -> SimpleNamespace:
            events.append("merge")
            assert repository.current == integration_branch
            assert number == 107
            assert kwargs == {
                "expected_head": first_branch,
                "expected_head_sha": reviewed_head,
                "expected_base": integration_branch,
                "expected_base_sha": base_sha,
            }
            return SimpleNamespace(
                merge_commit_sha=merge_sha,
                pr=SimpleNamespace(url="https://example.test/pull/107"),
            )

        def find_pr(self, *, head: str, base: str, state: str) -> None:
            assert (head, base) == (next_branch, integration_branch)
            assert state in {"OPEN", "MERGED"}
            return None

    github = GitHub()
    merge_pr_and_advance(
        repository,
        github,
        number=107,
        head=first_branch,
        head_sha=reviewed_head,
        base=integration_branch,
        base_sha=base_sha,
    )
    config = config_type(
        Path("/repo"),
        "eletim/agent-workflow-manager",
        integration_branch,
        "main",
        (),
        "true",
    )
    prepared = prepare_issue(repository, github, issue_type(108, next_branch), config)

    assert prepared is not None
    assert repository.current == next_branch
    assert events[:3] == [
        f"synchronize:{first_branch}->{integration_branch}",
        "merge",
        "advance",
    ]
    assert f"synchronize:{integration_branch}->{integration_branch}" in events
    assert f"recover:{next_branch}" in events
    for unsafe in ("git reset", "git rebase", "git stash", "--force", "-f HEAD:"):
        assert unsafe not in code


def test_generated_repository_calls_match_public_helper_signatures() -> None:
    tree = ast.parse(generate_issue_driven_workflow(parse(payload())))
    contracts = {"repo": GitRepository, "github": GitHubRepository}

    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not isinstance(call.func, ast.Attribute):
            continue
        receiver = call.func.value
        if not isinstance(receiver, ast.Name) or receiver.id not in contracts:
            continue
        method = getattr(contracts[receiver.id], call.func.attr)
        assert all(keyword.arg is not None for keyword in call.keywords)
        inspect.signature(method).bind(
            None,
            *(None for _ in call.args),
            **{keyword.arg: None for keyword in call.keywords if keyword.arg},
        )


def test_generated_workflow_uses_run_scoped_correlation_without_ad_hoc_tokens() -> None:
    code = generate_issue_driven_workflow(parse(payload()))

    assert "run_correlation(" in code
    assert "uuid" not in code
    assert "RUN_TOKEN" not in code
    assert "[awm:" not in code
    assert "CreateSessionRequest(" in code
    assert 'name=f"{issue.label} implementer"' in code


def test_generated_workflow_has_no_in_place_recovery_state() -> None:
    code = generate_issue_driven_workflow(parse(payload()))

    assert "save_checkpoint" not in code
    assert "resume_checkpoint" not in code
    assert "resume_shell" not in code
    assert "_pending" not in code
    assert "inspect_feature_preparation(" in code


def test_merge_to_integration_policy_changes_only_issue_merge_path() -> None:
    merging = generate_issue_driven_workflow(parse(payload(merge_to_integration=True)))
    ready_only = generate_issue_driven_workflow(
        parse(payload(merge_to_integration=False))
    )

    assert "MERGE_TO_INTEGRATION = True" in merging
    assert "MERGE_TO_INTEGRATION = False" in ready_only
    assert "{issue.label} PR is Ready" in ready_only
    assert 'delivery.outcome == "approved"' in ready_only


def test_final_review_policy_selects_the_generated_control_flow() -> None:
    reviewed = generate_issue_driven_workflow(parse(payload(final_review=True)))
    skipped = generate_issue_driven_workflow(parse(payload(final_review=False)))

    assert "FINAL_REVIEW = True" in reviewed
    assert "FINAL_REVIEW = False" in skipped
    assert "if FINAL_REVIEW:" in reviewed


def test_merge_final_false_has_no_final_merge_path() -> None:
    ready_only = generate_issue_driven_workflow(parse(payload(merge_final=False)))
    merging = generate_issue_driven_workflow(parse(payload(merge_final=True)))

    assert "MERGE_FINAL = False" in ready_only
    assert "MERGE_FINAL = True" in merging
    assert "if not MERGE_FINAL:" in ready_only
