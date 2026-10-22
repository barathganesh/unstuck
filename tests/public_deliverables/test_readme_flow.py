"""Geometry, causal timing, accessibility, and leakage contracts for the README SVG."""

import json
import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest

from publishing.public_scan import CHECKS, scan_file, scan_text, verify_controls

ROOT = Path(__file__).resolve().parents[2]
FLOW = "docs/unstuck-flow.svg"
NS = "{http://www.w3.org/2000/svg}"
CAPTION = "Run 1 records the dead end; Run 2 uses it before selection."


def tree() -> ElementTree.Element:
    return ElementTree.fromstring((ROOT / FLOW).read_text())


def visible(element: ElementTree.Element) -> str:
    return " ".join(
        node.get("aria-label") or " ".join(node.itertext()).strip()
        for node in element.iter()
        if node.tag == f"{NS}text" or node.get("class") == "lettering"
    )


def stylesheet() -> str:
    return tree().find(f"{NS}style").text or ""


def keyframes(name: str) -> str:
    return stylesheet().split(f"@keyframes {name} {{", 1)[1].split("@", 1)[0]


def assert_self_contained(raw: str) -> None:
    assert not re.search(r"<!DOCTYPE|<!ENTITY|<\?xml-stylesheet", raw, re.I)
    document = ElementTree.fromstring(raw)
    allowed = {"svg", "title", "desc", "style", "g", "rect", "path", "text", "circle"}
    for node in document.iter():
        assert node.tag.removeprefix(NS) in allowed
        for name, value in node.attrib.items():
            assert not name.lower().startswith("on")
            assert name.rsplit("}", 1)[-1] not in {"href", "src"}
            assert not re.search(r"https?:|data:|javascript:|//", value, re.I)
    for style in document.iter(f"{NS}style"):
        assert not re.search(
            r"@import|@font-face|https?:|data:|javascript:|\\", style.text or "", re.I
        )
    assert not re.search(r"url\(", raw, re.I)


def test_xml_lint_and_accessible_widescreen_structure() -> None:
    subprocess.run(["xmllint", "--nonet", "--noout", str(ROOT / FLOW)], check=True)
    document = tree()
    assert document.tag == f"{NS}svg" and document.get("role") == "img"
    assert document.get("viewBox") == "0 0 1280 720"
    assert (document.get("width"), document.get("height")) == ("1280", "720")
    assert 1280 / 720 == 16 / 9
    assert document.get("preserveAspectRatio") == "xMidYMid meet"
    identifiers = [node.get("id") for node in document.iter() if node.get("id")]
    assert len(identifiers) == len(set(identifiers))
    for kind in ("title", "desc"):
        node = document.find(f"{NS}{kind}")
        assert node is not None and len(node.text or "") > 40
        assert node.get("id") in document.get("aria-labelledby", "").split()
    screens = document.findall(f"{NS}g[@data-screen]")
    assert [screen.get("data-screen") for screen in screens] == ["before", "after"]
    for screen, lower, upper in ((screens[0], 0, 306), (screens[1], 306, 636)):
        for node in screen.iter(f"{NS}text"):
            assert lower < float(node.get("y")) < upper
            assert 0 < float(node.get("x")) < 1280
    assert not document.findall(f".//{NS}g[@class='node']")
    assert len(document.findall(f".//{NS}rect")) == 1
    lettering = document.findall(f".//{NS}g[@class='lettering']")
    assert len(lettering) == 4
    assert all(
        node.get("aria-label") and node.find(f"{NS}path") is not None
        for node in lettering
    )


def test_no_script_network_embedded_assets_or_effects() -> None:
    assert_self_contained((ROOT / FLOW).read_text())


@pytest.mark.parametrize(
    "injection",
    [
        "<script>alert(1)</script>",
        '<image href="https://example.invalid/image.png"/>',
        '<use href="#shape"/>',
        '<g onload="alert(1)"/>',
        "<foreignObject/>",
        "<filter/>",
        "<linearGradient/>",
        '<style>@import "https://example.invalid/style.css";</style>',
        "<style>@font-face {src: url(data:font/woff;base64,AA);}</style>",
        '<style>path {fill: url("https://example.invalid/paint");}</style>',
    ],
)
def test_self_containment_positive_controls(injection: str) -> None:
    raw = f'<svg xmlns="http://www.w3.org/2000/svg"><style/> {injection}</svg>'
    with pytest.raises(AssertionError):
        assert_self_contained(raw)


def test_motion_is_one_authored_loop_with_three_bounded_control_calls() -> None:
    raw = (ROOT / FLOW).read_text()
    css = stylesheet()
    assert "<![CDATA[" in raw and "]]>" in raw
    declared = set(re.findall(r"animation:\s*([\w-]+)", css)) - {"none"}
    assert declared == set(re.findall(r"@keyframes\s+([\w-]+)", css))
    assert set(re.findall(r"animation:.*?\b(\d+)s", css)) == {"28"}
    assert "twenty-eight-second choreography" in raw
    assert '@supports (offset-path: path("M0 0 H1"))' in css
    control = keyframes("control-travel")
    assert control.count("offset-distance: 0%") == 3
    assert len(re.findall(r"offset-distance: 100%; opacity: 1", control)) == 3
    first = keyframes("run-one-travel")
    assert len(re.findall(r"offset-distance: 100%; opacity: 1", first)) == 1
    for name in ("control", "run-one", "run-two"):
        assert re.search(rf"\.{name}-signal\s*\{{[^}}]*offset-path:\s*path\(", css)
    for phrase in ("count-first", "count-second", "count-third", "evidence-read"):
        assert phrase in declared


def test_receipt_changes_availability_before_decider_selection() -> None:
    assert "49.1%" in keyframes("receipt-stamp")
    assert "64%, 98%" in keyframes("receipt-carry")
    assert "0%, 64%, 100% { stroke-dashoffset: 48; }" in keyframes("branch-lock")
    assert "66%, 98%" in keyframes("branch-lock")
    assert "72%, 98%" in keyframes("route-open")
    assert "72%, 76% { offset-distance: 0%; opacity: 1; }" in keyframes(
        "run-two-travel"
    )
    assert "88%, 98%" in keyframes("repair-complete")
    assert "stroke-dashoffset" in keyframes("route-open")
    assert "translate(298px, -28px)" in keyframes("receipt-carry")
    assert "offset-anchor: 0px 0px" in stylesheet()
    for label in ("1/3", "2/3", "3/3"):
        assert label in visible(tree())


@pytest.mark.parametrize(
    "name,start,end,property_value,minimum_seconds",
    [
        ("control-travel", 4, 7, "offset-distance: 36%", 0.8),
        ("control-travel", 16, 19, "offset-distance: 36%", 0.8),
        ("control-travel", 28, 31, "offset-distance: 36%", 0.8),
        ("run-one-travel", 41, 44, "offset-distance: 36%", 0.8),
        ("receipt-carry", 49, 58, "transform: translate(0px, 0px)", 2.5),
        ("receipt-stamp", 51, 98, "transform: scale(1) rotate(-3deg)", 13),
        ("run-two-travel", 72, 76, "offset-distance: 0%", 1.1),
        ("run-two-travel", 80, 82, "offset-distance: 48%", 0.55),
        ("repair-complete", 88, 98, "stroke-dashoffset: 0", 2.8),
    ],
)
def test_readable_holds(name, start, end, property_value, minimum_seconds) -> None:
    body = keyframes(name)
    hold = re.search(rf"\b{start}%, {end}%\s*\{{([^}}]+)\}}", body)
    assert hold and property_value in hold.group(1)
    assert (end - start) * 28 / 100 >= minimum_seconds


@pytest.mark.parametrize(
    "signal,track",
    [
        ("control", "control-track"),
        ("run-one", "run-one-track"),
        ("run-two", "successful-route"),
    ],
)
def test_visible_stroke_and_signal_use_identical_geometry(signal, track) -> None:
    motion = re.search(
        rf'\.{signal}-signal\s*\{{\s*offset-path: path\("([^"]+)"\)', stylesheet()
    )
    assert motion
    assert tree().find(f".//{NS}path[@id='{track}']").get("d") == motion.group(1)


def assert_symmetric_circuit(path: str) -> None:
    match = re.fullmatch(
        r"M([\d.]+) ([\d.]+) H([\d.]+) A([\d.]+) ([\d.]+) 0 0 1 ([\d.]+) ([\d.]+) "
        r"H([\d.]+) A([\d.]+) ([\d.]+) 0 0 1 ([\d.]+) ([\d.]+) Z",
        path,
    )
    assert match
    (
        left,
        top,
        right,
        radius_x,
        radius_y,
        end_right,
        bottom,
        end_left,
        other_x,
        other_y,
        close_x,
        close_y,
    ) = map(float, match.groups())
    assert radius_x == radius_y == other_x == other_y == (bottom - top) / 2
    assert left == end_left == close_x < right == end_right
    assert top == close_y


def test_control_is_two_tangent_semicircles_with_equal_radii() -> None:
    assert_symmetric_circuit(tree().find(f".//{NS}path[@id='control-track']").get("d"))


@pytest.mark.parametrize(
    "bad_part", ["A52 51", "A51 50", "1 1091 260", "H591 A", "590 159 Z"]
)
def test_asymmetric_circuit_positive_controls(bad_part) -> None:
    original = "M590 158 H1090 A51 51 0 0 1 1090 260 H590 A51 51 0 0 1 590 158 Z"
    old_part = {
        "A52 51": "A51 51",
        "A51 50": "A51 51",
        "1 1091 260": "1 1090 260",
        "H591 A": "H590 A",
        "590 159 Z": "590 158 Z",
    }[bad_part]
    with pytest.raises(AssertionError):
        assert_symmetric_circuit(original.replace(old_part, bad_part, 1))


def test_run_two_is_an_open_fork_not_a_return_loop() -> None:
    run_two = tree().find(f".//{NS}g[@id='run-two']")
    branch = run_two.find(f"{NS}path[@id='blocked-branch']")
    route = run_two.find(f"{NS}path[@id='successful-route']")
    gate = run_two.find(f"{NS}path[@id='branch-gate']")
    assert branch.get("d") == "M754 552 C786 552 786 492 818 492 H832"
    assert route.get("d") == "M754 552 H1190"
    assert gate.get("d") == "M824 480 L840 504"
    assert (824 + 840) / 2 == 832 and (480 + 504) / 2 == 492
    assert len(run_two.findall(f"{NS}path")) == 6
    assert not re.search(
        r"loose-branch|break-left|break-right|branch-release", (ROOT / FLOW).read_text()
    )
    completed = run_two.find(f"{NS}text[@class='completed']")
    assert float(completed.get("x")) >= 1010
    assert 492 < float(completed.get("y")) <= 522 < 541 < 552


def test_reduced_motion_and_animation_failure_keep_final_comparison_visible() -> None:
    css = stylesheet()
    base = css.split("@supports", 1)[0]
    reduced = css.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert "animation: none !important" in reduced
    assert ".signal, .counter-first, .counter-second { display: none; }" in reduced
    assert ".counter-third, .evidence, .receipt, .stamp { opacity: 1; }" in reduced
    assert ".route, .gate, .terminal { stroke-dashoffset: 0; }" in reduced
    assert ".receipt { transform: translate(298px, -28px); }" in base
    assert "stroke-dashoffset: 0" in base
    assert not re.search(
        r"(?:text|\.lettering|\.receipt|\.stamp)\s*\{[^}]*opacity:\s*0", base
    )
    for signal in tree().iter(f"{NS}circle"):
        if "signal" in signal.get("class", ""):
            assert signal.get("aria-hidden") == "true" and signal.get("opacity") == "0"


def test_dark_paper_ink_palette_with_one_ember() -> None:
    raw = (ROOT / FLOW).read_text()
    assert set(re.findall(r"#[0-9A-Fa-f]{6}\b", raw)) == {
        "#141310",
        "#F5F1E8",
        "#BDB3A2",
        "#F1834B",
    }
    assert not re.search(
        r"shadow|gradient|filter|font-size:\s*(?:[0-9]|1[0-9])px", raw, re.I
    )


def test_causal_story_and_exact_metrics_match_curated_proof() -> None:
    document = tree()
    before = visible(document.find(f"{NS}g[@id='before-screen']"))
    after = visible(document.find(f"{NS}g[@id='after-screen']"))
    footer = visible(document.find(f"{NS}g[@id='comparison']"))
    for phrase in (
        "BEFORE UNSTUCK",
        "3 of 3 unresolved",
        "inspect_pipeline",
        "Partition arrived.",
        "Still unresolved.",
    ):
        assert phrase in before
    for phrase in (
        "WITH UNSTUCK",
        "RUN 1",
        "receipt created",
        "once / unresolved",
        "Decision Receipt",
        "RUN 2",
        "dead end removed before selection",
        "Same Decider",
        "one Decider choice",
        "inspect_schema_keys",
        "one read / verified repair",
        "completed",
    ):
        assert phrase in after
    record = json.loads((ROOT / "dist/public-demo/loop-escape-proof.json").read_text())
    client = record["measured_latency"]["agentcore_client_time"]
    old = client["superseded_run_2_post_d1"]["elapsed"]["value"] / 1000
    new = client["optimized_run_2_preselection"]["elapsed"]["value"] / 1000
    assert f"{old:.1f}s → {new:.1f}s" in footer
    assert (
        f"{client['difference']['relative']['value']}% lower observed Run 2 latency"
        in footer
    )
    assert "n=1 Same synthetic case / each proof once" in footer
    assert "No dollar savings" in footer
    description = document.find(f"{NS}desc").text or ""
    for phrase in (
        "one inference, one read, 21.6 seconds",
        "disposition_unchanged",
        "Run 1 is not saved",
        "before the first Decider request",
        "The receipt never chooses",
        "three diagnostics keep their original order",
        "one inference, 26.6 seconds",
        "two inferences, 54.8 seconds",
        "28.2 seconds",
        "n = 1",
        "No dollar savings measured",
        "not a literal infinite loop",
    ):
        assert phrase in description
    control = record["hosted_decider_proof"]["without_receipts_control"]
    assert control["unresolved"]["value"] == 3
    first, second = record["hosted_decider_proof"]["with_receipts"]
    assert first["terminal_status"] == "unresolved"
    assert second["precluded_before_selection"] is True
    assert second["decider_choice"] == "inspect_schema_keys"
    assert second["decider_choices"]["value"] == 1
    assert second["terminal_status"] == "completed"


def test_no_stale_redecision_story_or_design_rubric() -> None:
    raw = (ROOT / FLOW).read_text()
    assert not re.search(
        r"\bD[12]\b|re-decid|reconsider|one fewer read|two reads|rubric|self.score|weights|guaranteed savings",
        raw,
        re.I,
    )


def assert_readme_placement(readme: str) -> None:
    images = re.findall(r"!\[([^\]]+)\]\(docs/unstuck-flow\.svg\)", readme)
    assert len(images) == 1
    alt = images[0]
    for phrase in (
        "Top:",
        "Bottom:",
        "memoryless control",
        "3 of 3 unresolved",
        "Run 1",
        "disposition_unchanged",
        "Run 2",
        "before the first Decider request",
        "same Decider chooses inspect_schema_keys",
        "verified repair",
        "completed",
        "51.4%",
        "n = 1",
        "No dollar savings",
    ):
        assert phrase in alt
    position = readme.index(f"![{alt}]")
    assert (
        readme.index("Tags:") < position < readme.index("## The thirty second version")
    )
    assert readme[:position].count("\n") < 25
    assert readme.count(CAPTION) == 1 and readme.index(CAPTION) > position
    assert not re.search(r"<svg\b", readme, re.I)
    assert (ROOT / FLOW).is_file()


def test_readme_placement_link_caption_and_alt() -> None:
    assert_readme_placement((ROOT / "README.md").read_text())


@pytest.mark.parametrize("replacement", ["docs/missing.svg", "docs/architecture.svg"])
def test_readme_wrong_link_positive_controls(replacement: str) -> None:
    with pytest.raises(AssertionError):
        assert_readme_placement(
            (ROOT / "README.md").read_text().replace(FLOW, replacement)
        )


def test_public_scanner_covers_the_entire_svg() -> None:
    assert len(CHECKS) == 24 and all(result.ok for result in verify_controls())
    assert scan_file(FLOW, root=ROOT) == []
    assert scan_text(FLOW, visible(tree())) == []


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.name)
def test_scanner_positive_controls_inside_svg(check) -> None:
    raw = (
        (ROOT / FLOW)
        .read_text()
        .replace("</svg>", f"<desc>{check.positive_control}</desc></svg>")
    )
    assert any(finding.check == check.name for finding in scan_text(FLOW, raw))
