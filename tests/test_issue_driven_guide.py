from __future__ import annotations

import json
import re
from pathlib import Path

from purplemux_client.issue_driven import (
    _ALLOWED_FIELDS,
    generate_issue_driven_workflow,
    parse_issue_driven_json,
)

ROOT = Path(__file__).parents[1]
GUIDE = ROOT / "src/purplemux_client/web_static/issue-driven-guide.md"
INDEX = ROOT / "src/purplemux_client/web_static/index.html"
EXAMPLE = ROOT / "examples/sequential-version-development.py"


def guide_text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def canonical_json() -> dict[str, object]:
    match = re.search(
        r"## Canonical example\s+```json\n(.*?)\n```", guide_text(), re.DOTALL
    )
    assert match is not None
    value = json.loads(match.group(1))
    assert isinstance(value, dict)
    return value


def test_canonical_example_parses_and_uses_recommended_defaults() -> None:
    config = parse_issue_driven_json(json.dumps(canonical_json()))

    assert config.issues == (86, 99, 87, 84)
    assert config.max_reviews == 4
    assert config.scope_max_reviews == 6
    assert config.turn_timeout == 7200
    assert config.merge_final is False
    assert config.implementer_agent == "codex"
    assert config.reviewer_agent == "claude"


def test_documented_fields_exactly_match_the_parser_schema() -> None:
    documented = set(
        re.findall(r"^\| `([a-z_]+)` \|", guide_text(), flags=re.MULTILINE)
    )

    assert documented == _ALLOWED_FIELDS


def test_documented_one_shot_example_is_valid_and_starts_empty() -> None:
    match = re.search(
        r"To deliver one large Issue.*?```json\n(.*?)\n```",
        guide_text(),
        re.DOTALL,
    )
    assert match is not None

    config = parse_issue_driven_json(match.group(1))

    assert config.one_shot_issue == 169
    assert config.work_items == ()


def test_repository_guidance_matches_generated_worktree_semantics() -> None:
    guide = guide_text()
    config = parse_issue_driven_json(json.dumps(canonical_json()))
    generated = generate_issue_driven_workflow(config)

    assert "existing source repository" in guide
    assert "fresh, run-specific worktree" in guide
    assert (
        "prepare_run_repository(repo=repository, base_branch=integration_branch)"
        in guide
    )
    assert "repo='~/DevEnv/agent-workflow-manager'" in generated
    assert "base_branch='main'" in generated
    assert "base='main'" in generated
    assert "expected_base_sha=context.base_sha" in generated


def test_starter_and_template_use_recommended_review_limits_and_safe_delivery() -> None:
    index = INDEX.read_text(encoding="utf-8")
    example = EXAMPLE.read_text(encoding="utf-8")
    guide = guide_text()

    assert '"max_reviews": 4' in index
    assert '"scope_max_reviews": 6' in index
    assert '"turn_timeout": 7200' in index
    assert '"implementer_agent": "codex"' in index
    assert '"reviewer_agent": "codex"' in index
    assert '"merge_final": false' in index
    assert "MAX_REVIEWS = 4" in example
    assert "MAX_SCOPE_REVIEWS = 6" in example
    assert "TURN_TIMEOUT = 7200" in example
    assert "`scope_max_reviews` (default 3)" in guide
    assert "Correctness Review uses `max_reviews`" in guide


def test_guide_avoids_removed_recovery_contract() -> None:
    guide = guide_text().lower()

    assert "checkpoint" not in guide
    assert "review & resume" in guide
    assert "distinct run" in guide
    assert "authoritative git, github, and purplemux state" in guide


def test_guide_documents_inline_identity_responsibility_boundary() -> None:
    guide = " ".join(guide_text().split())

    assert "Across a fresh run, **Review & Resume**" in guide
    assert (
        "persisted `WorkItemPlan` supplies the authoritative task fingerprint" in guide
    )
    assert "Neither source is sufficient by itself" in guide
    assert "Only AWM creates or repairs the fingerprint marker" in guide
    assert "recovery from a timed-out marker update" in guide
    assert (
        "A different valid fingerprint and ambiguous markers always fail closed"
        in guide
    )


def test_guide_defines_minimal_human_context_and_github_decision_record() -> None:
    guide = " ".join(guide_text().split())

    assert (
        "Routine human context is `docs/design-principles.md` plus "
        "`docs/representative-scenarios.md`" in guide
    )
    assert "the One-Shot Issue is added to that context" in guide
    assert "not the configurable `scenarios` list" in guide
    assert "run-specific validation input for the Scenario Gate" in guide
    assert "managed Review audit sections on child and Base PRs" in guide
    assert "durable GitHub decision record" in guide
    assert "Keep agent context role-minimal" in guide
