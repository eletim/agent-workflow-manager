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
    assert config.max_reviews == 5
    assert config.merge_final is False


def test_documented_fields_exactly_match_the_parser_schema() -> None:
    documented = set(
        re.findall(r"^\| `([a-z_]+)` \|", guide_text(), flags=re.MULTILINE)
    )

    assert documented == _ALLOWED_FIELDS


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
    assert "base_branch='dev/v0.2.1'" in generated


def test_starter_and_template_use_five_reviews_and_safe_final_delivery() -> None:
    index = INDEX.read_text(encoding="utf-8")
    example = EXAMPLE.read_text(encoding="utf-8")

    assert '"max_reviews": 5' in index
    assert '"merge_final": false' in index
    assert "MAX_REVIEWS = 5" in example


def test_guide_avoids_removed_recovery_contract() -> None:
    guide = guide_text().lower()

    assert "checkpoint" not in guide
    assert "resume" not in guide
