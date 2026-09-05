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
