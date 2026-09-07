from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parents[1]
INDEX = ROOT / "src/purplemux_client/web_static/index.html"


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
