from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
README = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")


def test_personal_setup_is_first_and_uses_custom_purplemux_cli() -> None:
    assert README.startswith("## Personal self-hosted setup\n")
    assert "git clone https://github.com/eletim/purplemux.git" in README
    assert 'exec node "$HOME/DevEnv/purplemux/bin/cli.js" "$@"' in README
    assert "Do **not** substitute" in README
    assert "npm install -g purplemux" in README


def test_personal_setup_starts_agent_workflow_manager_without_secrets() -> None:
    personal_setup, _ = README.split("# Agent Workflow Manager", maxsplit=1)

    assert "bash start.sh" in personal_setup
    assert "PMUX_TOKEN=" not in personal_setup
    assert "cli-token" not in personal_setup


def test_issue_driven_overview_documents_optional_scenario_gate() -> None:
    assert "two agent fields, and `scenarios` are optional" in README
    assert "dedicated AI Scenario Gate" in README
    assert "exact final-base commit (Before)" in README
    assert "integration-head commit\n(After)" in README
    assert "not fixed expected-output assertions" in README
    assert "embedded in generated plain Python" in README


def test_whole_review_documents_dedicated_design_principles_conformance() -> None:
    assert "dedicated Design Principles reviewer" in README
    assert "exists at the exact integration head" in README
    assert "`docs/design-principles.md` from the exact integration head" in README
    assert "reviews solely\nfor conformance" in README
    assert "does\nnot request creation or restoration" in README
    assert "bounded whole-review loop" in README


def test_human_context_and_durable_decision_record_are_distinguished() -> None:
    section = README.split("## Human context and durable decisions", maxsplit=1)[1]
    section = section.split("## Issue Driven mode", maxsplit=1)[0]

    assert "`docs/design-principles.md`" in section
    assert "`docs/representative-scenarios.md`" in section
    assert "add the One-Shot Issue" in section
    assert "not the configurable `scenarios` list" in section
    assert "persisted work-item plan" in section
    assert "managed Review\naudit sections on child and Base PRs" in section
    assert "only the portion" in section


def test_issue_driven_preview_and_observed_run_story_are_documented() -> None:
    section = README.split("## Human context and durable decisions", maxsplit=1)[1]
    section = section.split("## Review mode", maxsplit=1)[0]

    assert "Planned run preview" in section
    assert "capability preview, not a prediction" in section
    assert "select the actual turns" in section
    assert "disclosed only on demand" in section


def test_git_delivery_example_verifies_agent_provenance() -> None:
    assert "feature = repo.normalize_agent_commit_provenance(" in README
    assert "authoritative remote refs (including tags)" in README
    assert 'expected_agent="codex"' in README
    assert 'expected_process="implementation"' in README


def test_runner_ui_documents_peer_draft_and_authoritative_run_contexts() -> None:
    section = README.split("## Local Python Runner UI", maxsplit=1)[1]

    assert "**New Run** and every existing **Run** as peer" in section
    assert "authoritative persisted snapshot" in section
    assert "independently retained editable draft" in section
