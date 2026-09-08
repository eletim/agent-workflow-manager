from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parents[1]
INDEX = ROOT / "src/purplemux_client/web_static/index.html"
STYLES = ROOT / "src/purplemux_client/web_static/style.css"


class _StructureParser(HTMLParser):
    _VOID_ELEMENTS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.ancestors_by_id: dict[
            str, tuple[tuple[str, dict[str, str | None]], ...]
        ] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.stack.append((tag, attributes))
        element_id = attributes.get("id")
        if element_id is not None:
            self.ancestors_by_id[element_id] = tuple(self.stack)
        if tag in self._VOID_ELEMENTS:
            self.stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        while self.stack:
            open_tag, _ = self.stack.pop()
            if open_tag == tag:
                return


def _ancestors(element_id: str) -> tuple[tuple[str, dict[str, str | None]], ...]:
    parser = _StructureParser()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    return parser.ancestors_by_id[element_id]


def test_workflow_arguments_are_secondary_to_the_python_editor() -> None:
    html = INDEX.read_text(encoding="utf-8")
    ancestors = _ancestors("run-arguments")

    assert html.index('id="code"') < html.index('id="run-arguments"')
    assert any(
        tag == "details"
        and attributes.get("class") == "advanced-options"
        and "open" not in attributes
        for tag, attributes in ancestors
    )


def test_agent_readiness_is_available_only_in_collapsed_diagnostics() -> None:
    ancestors = _ancestors("run-readiness")

    assert any(
        tag == "details"
        and attributes.get("id") == "diagnostics-panel"
        and "open" not in attributes
        for tag, attributes in ancestors
    )


def test_notifications_are_reached_from_the_header_settings_dialog() -> None:
    html = INDEX.read_text(encoding="utf-8")
    ancestors = _ancestors("notification-settings")

    assert '<button id="settings-open" type="button">Settings</button>' in html
    assert any(
        tag == "dialog" and attributes.get("id") == "settings-dialog"
        for tag, attributes in ancestors
    )
    assert '<label for="notify-server">Notify server URL</label>' in html
    assert 'id="notify-server-link"' in html
    assert 'rel="noopener noreferrer"' in html


def test_prompt_repository_navigation_is_read_only_and_safe() -> None:
    html = INDEX.read_text(encoding="utf-8")

    assert 'id="repository-navigation"' in html
    assert 'id="repository-slug"' in html
    assert 'id="repository-link"' in html
    assert "Open GitHub Repository" in html
    assert 'target="_blank" rel="noopener noreferrer"' in html


def test_run_history_is_collapsible_without_a_duplicate_mobile_view() -> None:
    ancestors = _ancestors("run-list")

    assert any(
        tag == "details"
        and attributes.get("id") == "runs-panel"
        and "open" in attributes
        for tag, attributes in ancestors
    )
    assert "runs-toggle-hint" in INDEX.read_text(encoding="utf-8")


def test_checked_run_deletion_is_available_inside_run_history() -> None:
    ancestors = _ancestors("delete-checked-runs")

    assert any(
        tag == "details" and attributes.get("id") == "runs-panel"
        for tag, attributes in ancestors
    )
    assert 'id="delete-checked-runs"' in INDEX.read_text(encoding="utf-8")


def test_mobile_styles_keep_primary_surfaces_inside_the_viewport() -> None:
    styles = STYLES.read_text(encoding="utf-8")

    assert "@media (max-width: 760px)" in styles
    assert "overflow-x: clip" in styles
    assert ".mode-switch" in styles and "repeat(3, minmax(0, 1fr))" in styles
    assert ".controls" in styles and "repeat(2, minmax(0, 1fr))" in styles
    assert ".output-panel { min-width: 0; }" in styles
    assert 'id="issue-summary-panel"' in INDEX.read_text(encoding="utf-8")
    assert "grid-template-columns: 20px minmax(0, 1fr)" in styles
