"""Structural accessibility tests over the served markup. Offline.

These assert the structure the accessibility checklist requires and can be
checked without a browser. The automated scan and the manual keyboard, zoom,
and screen-reader passes are separate human verification and stay Pending; no
test here claims them.
"""

from html.parser import HTMLParser
from pathlib import Path

import pytest

from unstuck.operator.app import STATIC_ASSETS, STATIC_ROOT

VIEWS = ("setup", "detail", "benchmark", "export")
MANUAL_VERIFICATION_PENDING = (
    "automated accessibility scan",
    "manual keyboard path",
    "screen-reader smoke",
)


class Markup(HTMLParser):
    """Collects the structure the checklist asks about."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.open_stack: list[str] = []
        self.text_for: dict[str, str] = {}
        self.current: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mapping = {key: (value or "") for key, value in attrs}
        self.tags.append((tag, mapping))
        self.open_stack.append(tag)
        if tag in {"h1", "h2", "button", "label", "legend", "caption", "a"}:
            self.current = f"{tag}:{len(self.tags)}"
            self.text_for[self.current] = ""

    def handle_endtag(self, tag: str) -> None:
        if self.open_stack and self.open_stack[-1] == tag:
            self.open_stack.pop()
        self.current = None

    def handle_data(self, data: str) -> None:
        if self.current:
            self.text_for[self.current] += data

    def of(self, tag: str) -> list[dict[str, str]]:
        return [attrs for name, attrs in self.tags if name == tag]


@pytest.fixture
def markup() -> Markup:
    parser = Markup()
    parser.feed((STATIC_ROOT / "index.html").read_text(encoding="utf-8"))
    return parser


def test_every_declared_asset_exists_and_nothing_else_is_served_offline() -> None:
    present = {path.name for path in STATIC_ROOT.iterdir() if path.is_file()}
    assert present == set(STATIC_ASSETS)
    for name in STATIC_ASSETS:
        assert (STATIC_ROOT / name).read_text(encoding="utf-8").strip()


def test_each_view_has_exactly_one_h1_and_logical_h2s_offline(markup: Markup) -> None:
    headings = [
        text.strip() for key, text in markup.text_for.items() if key.startswith("h1:")
    ]
    assert len(headings) == len(VIEWS)
    assert all(heading for heading in headings)
    subs = [
        text.strip() for key, text in markup.text_for.items() if key.startswith("h2:")
    ]
    assert subs and all(sub for sub in subs)
    sections = markup.of("section")
    labelled = [item for item in sections if item.get("aria-labelledby")]
    assert len(labelled) >= len(VIEWS)


def test_the_landmarks_are_present_offline(markup: Markup) -> None:
    for tag in ("header", "nav", "main", "footer"):
        assert markup.of(tag), tag
    assert markup.of("nav")[0].get("aria-label")
    assert markup.of("main")[0].get("id") == "main"
    skip = [item for item in markup.of("a") if item.get("href") == "#main"]
    assert skip, "a skip link is required"


def test_the_spine_is_an_ordered_list_not_a_canvas_offline(markup: Markup) -> None:
    assert markup.of("ol"), "the provenance spine is an ordered list"
    assert not markup.of("canvas")
    assert not markup.of("svg")


def test_tables_carry_captions_and_scoped_headers_offline(markup: Markup) -> None:
    assert markup.of("table")
    assert markup.of("caption")
    headers = markup.of("th")
    assert headers
    assert all(item.get("scope") for item in headers)


def test_the_status_region_is_polite_and_never_steals_focus_offline(
    markup: Markup,
) -> None:
    regions = [item for item in markup.of("p") if item.get("aria-live")]
    assert regions
    assert all(item["aria-live"] == "polite" for item in regions)
    assert any(item.get("role") == "status" for item in regions)
    body = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert "aria-live" not in body or "assertive" not in body
    assert "alert(" not in body


def test_every_control_has_an_accessible_name_offline(markup: Markup) -> None:
    buttons = markup.of("button")
    assert buttons
    named = [
        key
        for key, text in markup.text_for.items()
        if key.startswith("button:") and text.strip()
    ]
    assert len(named) == len(buttons)
    for field in markup.of("input") + markup.of("select"):
        identifier = field.get("id")
        if field.get("type") == "radio":
            continue
        assert identifier, field
        assert any(item.get("for") == identifier for item in markup.of("label")), (
            identifier
        )
    assert markup.of("legend") or markup.of("fieldset") == []


def test_the_tab_pattern_follows_the_expected_roles_offline(markup: Markup) -> None:
    tabs = [item for item in markup.of("button") if item.get("role") == "tab"]
    panels = [item for item in markup.of("div") if item.get("role") == "tabpanel"]
    assert len(tabs) == len(panels) == 3
    assert [item.get("aria-selected") for item in tabs].count("true") == 1
    for tab in tabs:
        assert tab.get("aria-controls")
    for panel in panels:
        assert panel.get("aria-labelledby")
        assert panel.get("tabindex") == "0"
    assert any(item.get("role") == "tablist" for item in markup.of("div"))


def test_state_is_carried_by_text_and_the_labels_are_explicit_offline(
    markup: Markup,
) -> None:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    for label in ("Synthetic", "Offline", "Replay", "Live"):
        assert label in html, label
    body = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    # State reaches the DOM as text, in a dataset attribute and a label.
    assert "textContent" in body
    assert "dataset.state" in body
    assert "Not provided" in body


def test_the_stylesheet_honours_reduced_motion_and_small_viewports_offline() -> None:
    css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: no-preference" in css
    assert ":focus-visible" in css
    assert "@media (max-width:" in css
    # Motion is opt-in, so no transition or animation sits outside that query.
    head, _, tail = css.partition("prefers-reduced-motion")
    assert "transition" not in head
    assert "animation" not in head
    assert "@keyframes" not in css


def test_the_ui_ships_no_framework_remote_asset_or_inline_handler_offline() -> None:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    body = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    for forbidden in ("http://", "https://", "cdn", "integrity=", "crossorigin"):
        assert forbidden not in html, forbidden
    for handler in ("onclick=", "onload=", "onsubmit=", "onerror="):
        assert handler not in html, handler
    assert "eval(" not in body
    assert "innerHTML" not in body
    assert "document.write" not in body


def test_the_manual_accessibility_verification_is_recorded_pending_offline() -> None:
    doc = Path(__file__).resolve().parents[2] / "docs" / "operator-experience.md"
    if not doc.exists():
        pytest.skip("the unit document is written in the final step")
    text = doc.read_text(encoding="utf-8").lower()
    for item in MANUAL_VERIFICATION_PENDING:
        assert item in text, item
    assert "pending" in text
