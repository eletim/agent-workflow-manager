from __future__ import annotations

import ast
from pathlib import Path

import purplemux_client

ROOT = Path(__file__).parents[1]
GUIDE = ROOT / "src/purplemux_client/web_static/python-workflow-guide.md"
EXAMPLE = ROOT / "examples/sequential-version-development.py"


def test_guide_names_every_public_package_export() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    missing = [name for name in purplemux_client.__all__ if name not in guide]
    assert missing == []


def test_guide_documents_every_runtime_api_used_by_canonical_example() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    tree = ast.parse(EXAMPLE.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "purplemux_client"
        for alias in node.names
    }
    method_receivers = {"client", "runtime", "repo", "github"}
    methods = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in method_receivers
    }

    assert [name for name in sorted(imported) if name not in guide] == []
    assert [name for name in sorted(methods) if f"{name}(" not in guide] == []


def test_guide_rejects_in_place_pending_marker_resume() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    assert "There is no public checkpoint" in guide
    assert "`resume_shell()` operation" in guide
    assert "A `*_pending` marker alone never satisfies that rule" in guide
    assert "approval/final-check" in guide


def test_guide_keeps_terminal_progress_observational() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    assert "concise `[workflow]` lines" in guide
    assert "Never parse stdout to determine workflow state" in guide
    assert "structured\nProgress, Findings, result events" in guide


def test_guide_documents_generated_inline_identity_responsibility() -> None:
    guide = " ".join(GUIDE.read_text(encoding="utf-8").split())

    assert (
        "persisted `WorkItemPlan` state supplies the authoritative fingerprint" in guide
    )
    assert "Neither authority can substitute for the other" in guide
    assert "fresh runs, **Review & Resume**" in guide
    assert "only AWM creates or repairs the fingerprint marker" in guide
    assert "guarded PR-body update times out" in guide
    assert (
        "A different valid fingerprint and ambiguous markers always fail closed"
        in guide
    )


def test_guide_keeps_agent_context_role_minimal_and_decisions_durable() -> None:
    guide = " ".join(GUIDE.read_text(encoding="utf-8").split())

    assert (
        "use `docs/design-principles.md` and "
        "`docs/representative-scenarios.md` as the human context" in guide
    )
    assert "Add the One-Shot Issue" in guide
    assert "configurable list is version-specific validation input" in guide
    assert "managed Review audit sections on child and Base PRs" in guide
    assert "durable GitHub decision record" in guide
    assert "only the context and recorded decisions needed for that role" in guide


def test_guide_documents_agent_provenance_verification() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    assert 'expected_agent="codex",\nexpected_process="implementation")' in guide
    assert "`agent_commit_coauthor()` supplies the same normalized co-author" in guide
    assert "expected_agent=None" in guide
    assert "expected_process=None" in guide


def test_guide_documents_bounded_local_git_recovery() -> None:
    guide = " ".join(GUIDE.read_text(encoding="utf-8").split())

    assert "`local-git-only` restriction" in guide
    assert "no remote Git ref mutation capability" in guide
    assert "every unrelated local ref must remain exact" in guide
    assert "exact advance to the snapshotted authoritative remote head" in guide
    assert "any other new commits require recovery provenance" in guide
    assert "both ref sets to remain exact" not in guide
