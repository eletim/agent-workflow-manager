from __future__ import annotations

import ast
import re
from pathlib import Path

import purplemux_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SAMPLE = REPOSITORY_ROOT / "examples" / "sequential-version-development.py"
GUIDE = (
    REPOSITORY_ROOT
    / "src"
    / "purplemux_client"
    / "web_static"
    / "python-workflow-guide.md"
)


def sample_tree() -> ast.Module:
    source = SAMPLE.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(SAMPLE))


def test_canonical_sample_compiles_and_uses_only_public_client_api() -> None:
    tree = sample_tree()
    compile(tree, str(SAMPLE), "exec")
    public_api = set(purplemux_client.__all__)
    imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "purplemux_client"
        for alias in node.names
    }

    assert imports
    assert imports <= public_api


def test_workflow_guide_prominently_links_to_existing_canonical_sample() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    first_section = guide.split("## Architecture and responsibility", maxsplit=1)[0]
    match = re.search(
        r"\[[^]]+\]\(([^)]+sequential-version-development\.py)\)", first_section
    )

    assert match is not None
    assert (GUIDE.parent / match.group(1)).resolve() == SAMPLE.resolve()
    assert SAMPLE.is_file()


def test_sample_has_separate_issue_and_whole_version_review_phases() -> None:
    source = SAMPLE.read_text(encoding="utf-8")

    assert "def process_issue(" in source
    assert "def integration_review(" in source
    assert "whole-version integration review" in source
    assert "MAX_REVIEWS = 4" in source
    assert "reviewer=create_agent" not in source  # no hidden/delegated orchestration
    assert "issue_reviewer_create_pending" in source
    assert "integration_reviewer_create_pending" in source
    assert "integration_checks_start_pending" in source
    assert "do not replay creation" in source


def test_issue_prs_can_only_target_integration_and_main_is_never_merged() -> None:
    tree = sample_tree()
    source = SAMPLE.read_text(encoding="utf-8")
    functions = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    merge_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        segment = ast.get_source_segment(source, node) or ""
        if '"merge"' in segment and '"gh"' in segment:
            merge_calls.append(segment)

    assert "config.integration_branch" in functions["issue_prs"]
    assert "config.main_branch" not in functions["issue_prs"]
    assert merge_calls
    assert all("main_branch" not in call for call in merge_calls)
    assert all('"--auto"' not in call for call in merge_calls)
    assert '"--match-head-commit"' in functions["merge_issue_pr"]
    assert "--delete-branch=false" not in source
    assert "merge_issue_pr(issue, config)" in source
    assert "never merged automatically" in source
