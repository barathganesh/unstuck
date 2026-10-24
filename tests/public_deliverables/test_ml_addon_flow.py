"""Structure, motion, honesty, clarity, and README contracts for the ML flow figure.

The figure and the README tell one story: Decision Receipts constrain the
offer, a LoRA-adapted Decider 2B ranks what remains, and the verifier decides.
Every number is read from a committed record, every percentage is recomputed,
and no bare ``n/d`` ratio reaches the ML narrative or the visible figure.
"""

import hashlib
import json
import re
import subprocess
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from xml.etree import ElementTree

import pytest

from publishing.public_scan import CHECKS, scan_file, scan_text

ROOT = Path(__file__).resolve().parents[2]
FIGURE = "docs/unstuck-ml-addon-flow.svg"
ORIGINAL = "docs/unstuck-flow.svg"
NS = "{http://www.w3.org/2000/svg}"
PALETTE = {"#141310", "#F5F1E8", "#BDB3A2", "#F1834B"}
HEADLINE = "Receipts constrain. LoRA ranks what remains."
ML_HEADING = "## Why receipts need learned ranking"
BASE_HEADING = "## The thirty second version"
EVIDENCE_HEADING = "### The offline fine-tune evidence"
CAPTION = "Receipts constrain before ranking; the verifier decides after it."
EMBED = re.compile(r"!\[([^\]]+)\]\(docs/unstuck-ml-addon-flow\.svg\)")

#: Wording the evidence cannot carry. Each matches only the claim, never its
#: honest negation, so a match anywhere is a failure.
OVERCLAIMS: dict[str, re.Pattern[str]] = {
    "significance": re.compile(r"statistical(?:ly)? significan", re.I),
    "generalization": re.compile(r"generali[sz]", re.I),
    "unseen states": re.compile(r"\bunseen\b|\bheld[- ]out\b|\bhold-?out\b", re.I),
    "safety": re.compile(
        r"\b(?:is|are|proven|shown|demonstrably) safe\b|safety (?:guarantee|proof)",
        re.I,
    ),
    "guarantee": re.compile(r"\bguarantee", re.I),
    "accuracy": re.compile(r"\baccuracy\b", re.I),
    "production": re.compile(r"production[- ]ready|in production\b", re.I),
    "latency or cost benefit": re.compile(
        r"(?:latency|cost|speed) (?:benefit|saving|improvement|reduction|gain)"
        r"|\bfaster\b|\bcheaper\b",
        re.I,
    ),
    "autonomous learning": re.compile(
        r"\b(?:decider(?: 2b)?|the model|the checkpoint|the agent|it)\s+"
        r"(?:learns|learned|keeps learning|self-improves)\b|(?<!no )online learning"
        r"|learns over time|continual",
        re.I,
    ),
    "replacement": re.compile(
        r"(?<!never )(?<!not )\breplac(?:e|es|ed|ing) (?:the )?"
        r"(?:decision receipts?|receipts?|verifier)",
        re.I,
    ),
    "hosted success": re.compile(
        r"hosted (?:parity|success|result (?:holds|matches|passed))"
        r"|parity (?:passed|confirmed)|promotion[_ ]eligible",
        re.I,
    ),
    "deployed adaptation": re.compile(
        r"(?<!not )(?<!not the )(?<!not the currently )"
        r"\b(?:is|now) (?:the )?(?:currently )?deployed",
        re.I,
    ),
}
#: LoRA is a core mechanism. None of these may frame it as an afterthought.
SIDELINED = re.compile(
    r"\badd-?ons?\b|\badd on\b|\boptional(?:ly)?\b|\baddend(?:um|a)\b"
    r"|\blast step\b|\bbolt-?on\b|\bafterthought\b",
    re.I,
)
#: A ratio written as ``n/d`` with no words around it.
BARE_FRACTION = re.compile(r"\b\d+\s*/\s*\d+\b")
#: ``n of d`` followed, after an optional unit, by its percentage.
STATED_SHARE = re.compile(r"\b(\d+) of (\d+)\b[^()%]{0,24}\((\d+\.\d)%\)")
#: The baseline beside it, ``up from n (p%)``, read against the same denominator.
BASELINE_SHARE = re.compile(
    r"\b\d+ of (\d+)\b[^()%]{0,24}(?:\(\d+\.\d%\))?,? (?:up|down) from "
    r"(\d+) \((\d+\.\d)%\)"
)
#: Content no public figure may carry.
OUT_OF_SCOPE = re.compile(
    r"\b(?:nba|basketball|customers?|submission|hackathon|judges?|internal"
    r"|account|credentials?|endpoint|sagemaker|agentcore|bedrock|arn)\b",
    re.I,
)


def raw() -> str:
    return (ROOT / FIGURE).read_text(encoding="utf-8")


def tree(text: str | None = None) -> ElementTree.Element:
    return ElementTree.fromstring(raw() if text is None else text)


def visible(element: ElementTree.Element) -> str:
    texts = ["".join(node.itertext()) for node in element.iter(f"{NS}text")]
    return re.sub(r"\s+", " ", " ".join(texts)).strip()


def group(identifier: str, document: ElementTree.Element | None = None):
    found = (tree() if document is None else document).find(
        f".//{NS}g[@id='{identifier}']"
    )
    assert found is not None, identifier
    return found


def description(document: ElementTree.Element | None = None) -> str:
    node = (tree() if document is None else document).find(f"{NS}desc")
    return re.sub(r"\s+", " ", node.text or "")


def title() -> str:
    return tree().find(f"{NS}title").text or ""


def stylesheet(document: ElementTree.Element | None = None) -> str:
    style = (tree() if document is None else document).find(f"{NS}defs/{NS}style")
    assert style is not None
    return style.text or ""


def keyframes(name: str, css: str | None = None) -> str:
    body = stylesheet() if css is None else css
    return body.split(f"@keyframes {name} {{", 1)[1].split("@", 1)[0]


def readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("**", "")).strip()


def ml_section(text: str) -> str:
    after = text.split(ML_HEADING, 1)[1]
    return ML_HEADING + re.split(r"\n## ", after, maxsplit=1)[0]


def opening(text: str) -> str:
    return text[: text.index("](docs/unstuck-flow.svg)")]


def record(relative: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads((ROOT / relative).read_text())
    return loaded


def overclaims(text: str) -> list[str]:
    flattened = re.sub(r"\s+", " ", text)
    return [name for name, pattern in OVERCLAIMS.items() if pattern.search(flattened)]


def share(numerator: int, denominator: int) -> str:
    """A percentage to one decimal, rounded half up as the prose states it."""
    value = Decimal(100 * numerator) / Decimal(denominator)
    return f"{value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def ratio(value: object) -> tuple[int, int]:
    numerator, denominator = str(value).split("/")
    return int(numerator), int(denominator)


# ---------------------------------------------------------------------------
# A valid, accessible, self-contained figure


def test_xml_lint_and_accessible_widescreen_structure() -> None:
    subprocess.run(["xmllint", "--nonet", "--noout", str(ROOT / FIGURE)], check=True)
    document = tree()
    assert document.tag == f"{NS}svg" and document.get("role") == "img"
    assert document.get("viewBox") == "0 0 1280 720"
    assert (document.get("width"), document.get("height")) == ("1280", "720")
    assert document.get("preserveAspectRatio") == "xMidYMid meet"
    identifiers = [node.get("id") for node in document.iter() if node.get("id")]
    assert len(identifiers) == len(set(identifiers))
    labelled_by = document.get("aria-labelledby", "").split()
    for kind, minimum in (("title", 60), ("desc", 400)):
        node = document.find(f"{NS}{kind}")
        assert node is not None and len(node.text or "") > minimum
        assert node.get("id") in labelled_by
    assert len(document.findall(f".//{NS}rect")) == 1
    for node in document.iter(f"{NS}text"):
        assert 0 < float(node.get("x")) <= 1236 and 0 < float(node.get("y")) < 720


def assert_self_contained(text: str) -> None:
    assert not re.search(r"<!DOCTYPE|<!ENTITY|<\?xml-stylesheet", text, re.I)
    document = ElementTree.fromstring(text)
    allowed = {"svg", "title", "desc", "defs", "style", "g", "rect", "path", "text"}
    allowed |= {"tspan"}
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
    assert not re.search(r"url\(|<script", text, re.I)


def test_no_script_network_embedded_assets_or_effects() -> None:
    assert_self_contained(raw())


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
    with pytest.raises(AssertionError):
        assert_self_contained(raw().replace("</svg>", f"{injection}</svg>"))


def test_styles_live_in_defs_as_cdata() -> None:
    text = raw()
    assert text.count("<style>") == 1
    defs = text.split("<defs>", 1)[1].split("</defs>", 1)[0]
    assert "<style><![CDATA[" in defs and "]]></style>" in defs


# ---------------------------------------------------------------------------
# Motion: one animation, once, where LoRA acts, never on the boundary


def assert_finite_motion(css: str) -> None:
    assert "infinite" not in css
    declarations = re.findall(r"animation:\s*([^;]+);", css)
    used = [d for d in declarations if d.strip() != "none !important"]
    assert used
    for declaration in used:
        assert re.search(r"\s1\s+both$", declaration.strip()), declaration
    names = {d.split()[0] for d in used}
    assert names == set(re.findall(r"@keyframes\s+([\w-]+)", css))
    # One justified motion: the ranking swap, as a matched rise and fall.
    assert names == {"rank-rise", "rank-fall"}


def test_motion_plays_once_and_never_loops() -> None:
    assert_finite_motion(stylesheet())


@pytest.mark.parametrize(
    "old,new",
    [
        (" 1 both", " infinite both"),
        (" 1 both", " 3 both"),
        (" 1 both", " both"),
        (
            "@keyframes rank-rise {",
            ".pulse { animation: pulse 2s ease 0s 1 both; }\n"
            "@keyframes pulse { 0% { opacity: 1; } }\n@keyframes rank-rise {",
        ),
    ],
)
def test_finite_motion_positive_controls(old: str, new: str) -> None:
    assert old in stylesheet()
    with pytest.raises(AssertionError):
        assert_finite_motion(stylesheet().replace(old, new, 1))


def test_ranking_rises_from_second_to_first_inside_the_decider() -> None:
    ranking = group("ranking-station")
    assert ranking.find(f".//{NS}g[@id='productive-candidate']") is not None
    productive = ranking.find(f"{NS}g[@id='productive-candidate']")
    other = ranking.find(f"{NS}g[@id='other-candidate']")
    assert productive.get("class") == "rank-rise"
    assert other.get("class") == "rank-fall"
    # The markup is the final order: the productive read sits in the first slot.
    first = float(productive.find(f"{NS}path").get("d").split()[1])
    second = float(other.find(f"{NS}path").get("d").split()[1])
    gap = second - first
    assert gap == 42
    rise, fall = keyframes("rank-rise"), keyframes("rank-fall")
    assert f"0%, 35% {{ transform: translate(0px, {gap:.0f}px); }}" in rise
    assert f"0%, 35% {{ transform: translate(0px, -{gap:.0f}px); }}" in fall
    for body in (rise, fall):
        assert "65%, 100% { transform: translate(0px, 0px); }" in body
    assert "productive read" in visible(productive)
    assert visible(ranking).startswith("Decider 2B ranks base or adapted")


def animated_classes(css: str) -> set[str]:
    return {
        name
        for name, body in re.findall(r"\.([\w-]+)\s*\{([^}]*)\}", css)
        if "animation:" in body and "none" not in body
    }


def assert_boundary_fixed(document: ElementTree.Element) -> None:
    moving = animated_classes(stylesheet(document))
    fixed = document.findall(f".//{NS}g[@data-boundary='fixed']")
    assert {node.get("id") for node in fixed} == {
        "receipt-station",
        "offer-station",
        "controller-station",
        "verifier-station",
    }
    for station in fixed:
        for node in station.iter():
            assert not set((node.get("class") or "").split()) & moving
            assert node.get("transform") is None
    for node in document.iter():
        assert node.get("transform") is None
        if set((node.get("class") or "").split()) & {"rank-rise", "rank-fall"}:
            assert node.get("id") in {"productive-candidate", "other-candidate"}


def test_receipt_offer_controller_and_verifier_never_move() -> None:
    assert_boundary_fixed(tree())


@pytest.mark.parametrize(
    "old,new",
    [
        ('id="receipt-station"', 'id="receipt-station" class="rank-rise"'),
        (
            '<text x="1052" y="414">Verifier</text>',
            '<text class="rank-fall" x="1052" y="414">Verifier</text>',
        ),
        ('id="offer-station"', 'id="offer-station" transform="translate(0 4)"'),
        ('id="train-lora"', 'id="train-lora" class="rank-rise"'),
    ],
)
def test_boundary_detector_positive_controls(old: str, new: str) -> None:
    assert raw().count(old) == 1
    with pytest.raises(AssertionError):
        assert_boundary_fixed(tree(raw().replace(old, new, 1)))


def test_reduced_motion_and_animation_failure_show_the_final_state() -> None:
    css = stylesheet()
    reduced = css.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert "* { animation: none !important; }" in reduced
    assert ".rank-rise, .rank-fall { transform: translate(0px, 0px); }" in reduced
    base = css.split("@keyframes", 1)[0]
    assert not re.search(r"(?:fill-)?opacity:\s*0", base)
    assert not re.search(r"transform:", base)
    assert not re.search(r'\s(?:fill-)?opacity="0', raw())
    assert "Reduced motion and animation failure show the final order" in (
        description()
    )


# ---------------------------------------------------------------------------
# WHY, OFFLINE TRAINING, RUNTIME DECISION, then a small outcome panel


ZONES = (("why", 98, 204), ("training", 204, 352), ("runtime", 352, 592))


def test_headline_and_three_zones_in_reading_order() -> None:
    document = tree()
    header = visible(group("header", document))
    assert HEADLINE in header
    assert "Experimental checkpoint, not the deployed selector" in header
    assert title().startswith(HEADLINE)
    zones = document.findall(f"{NS}g[@data-zone]")
    assert [zone.get("data-zone") for zone in zones] == [z for z, _, _ in ZONES]
    for zone, (_, lower, upper) in zip(zones, ZONES, strict=True):
        for node in zone.iter(f"{NS}text"):
            assert lower < float(node.get("y")) < upper
    labels = [visible(zone).split(" ", 1)[0] for zone in zones]
    assert labels == ["WHY", "OFFLINE", "RUNTIME"]
    assert "OFFLINE TRAINING, ONCE, BEFORE ANY RUN" in visible(zones[1])
    assert "RUNTIME DECISION, EVERY CALL" in visible(zones[2])
    outcome = group("outcome-panel", document)
    assert min(float(node.get("y")) for node in outcome.iter(f"{NS}text")) > 592


def test_why_names_both_problems_and_both_mechanisms() -> None:
    why = visible(group("why-zone"))
    order = [
        why.index(phrase)
        for phrase in (
            "A new call can repeat a choice already proven useless.",
            "Decision Receipt precludes only that exact repeat.",
            "Several valid tools remain; a receipt cannot rank them.",
            "LoRA-adapted Decider 2B ranks what remains.",
        )
    ]
    assert order == sorted(order)


def test_offline_training_is_verifier_labelled_and_ordered() -> None:
    training = group("training-zone")
    steps = training.findall(f"{NS}g[@data-step]")
    assert [step.get("data-step") for step in steps] == ["1", "2", "3", "4", "5"]
    assert [visible(step) for step in steps] == [
        "Frozen corpus synthetic offers",
        "Execute every offered read",
        "Verifier labels which read moved the disposition",
        "Train LoRA adapters and choice head",
        "Freeze, replay same controller path",
    ]
    assert "labels come from the verifier, not a model" in visible(training)
    lefts = [float(step.find(f"{NS}path").get("d").split()[0][1:]) for step in steps]
    assert lefts == sorted(lefts)


def test_runtime_path_is_receipt_offer_decider_controller_verifier() -> None:
    runtime = group("runtime-zone")
    stations = [
        node
        for node in runtime.findall(f"{NS}g")
        if node.get("data-boundary") or node.get("data-stage")
    ]
    assert [node.get("id") for node in stations] == [
        "receipt-station",
        "offer-station",
        "ranking-station",
        "controller-station",
        "verifier-station",
    ]
    lefts = [float(node.find(f"{NS}path").get("d").split()[0][1:]) for node in stations]
    assert lefts == sorted(lefts)
    shown = visible(runtime)
    for phrase in (
        "exact match precludes only that repeat",
        "remaining tools, original order",
        "executes the chosen read",
        "decides outcome, writes the next receipt",
        "Nothing learns at runtime.",
    ):
        assert phrase in shown, phrase


def feed_points() -> list[tuple[float, float]]:
    feed = group("checkpoint-feed")
    line = feed.find(f"{NS}path[@class='feed']").get("d")
    x, y, points = 0.0, 0.0, []
    for command, value in re.findall(r"([MHV])\s*([\d.]+(?:\s+[\d.]+)?)", line):
        numbers = [float(n) for n in value.split()]
        if command == "M":
            x, y = numbers
        elif command == "H":
            x = numbers[0]
        else:
            y = numbers[0]
        points.append((x, y))
    return points


def box(identifier: str) -> tuple[float, float, float, float]:
    d = group(identifier).find(f"{NS}path").get("d")
    left = float(d.split()[0][1:])
    top = float(d.split()[1])
    right = float(re.search(r"H([\d.]+)", d).group(1))
    bottom = float(re.search(r"V([\d.]+)", d).group(1))
    return left, top, right, bottom


def test_only_the_frozen_checkpoint_crosses_into_runtime_one_way() -> None:
    feed = group("checkpoint-feed")
    assert feed.get("data-direction") == "one-way"
    assert "frozen checkpoint only; nothing flows back" in visible(feed)
    points = feed_points()
    start, end = points[0], points[-1]
    left, _, right, bottom = box("train-freeze")
    assert left < start[0] < right and abs(start[1] - bottom) <= 4
    left, top, right, _ = box("ranking-station")
    assert left < end[0] < right and 0 <= top - end[1] <= 10
    assert all(y >= start[1] for _, y in points)
    # No other stroke links runtime back into training.
    for node in group("runtime-zone").iter(f"{NS}path"):
        ys = [float(n) for n in re.findall(r"[MLV]\s*[\d.]+\s+([\d.]+)", node.get("d"))]
        ys += [float(n) for n in re.findall(r"V([\d.]+)", node.get("d"))]
        assert all(y > 352 for y in ys), node.get("d")


def test_the_outcome_panel_is_labelled_offline_and_synthetic() -> None:
    panel = visible(group("outcome-panel"))
    assert panel.startswith("OFFLINE RESULT, SYNTHETIC, ONE SEED")
    assert "160 first-choice decisions, 64 full calls" in panel


# ---------------------------------------------------------------------------
# Every number from a committed record, with named denominators


def counts() -> dict[str, tuple[int, int, int]]:
    """Each measure as (baseline, adapted, denominator)."""
    lora = record("dist/public-demo/decider-finetune-lora-head-v1.json")["comparison"]
    found: dict[str, tuple[int, int, int]] = {}
    for key in (
        "first_choice_correct",
        "integrated_wasted_first_reads",
        "integrated_verified_completions",
    ):
        base, total = ratio(lora[key]["baseline"])
        adapted, again = ratio(lora[key]["adapted"])
        assert total == again
        found[key] = (base, adapted, total)
    head = record("dist/public-demo/decider-finetune-head-only-v1.json")["comparison"]
    found["head_only_first_choice"] = (
        ratio(head["first_choice_correct"]["baseline"])[0],
        *ratio(head["first_choice_correct"]["adapted"]),
    )
    found["head_only_wasted"] = (
        ratio(head["integrated_wasted_first_reads"]["baseline"])[0],
        *ratio(head["integrated_wasted_first_reads"]["adapted"]),
    )
    cpu = record("experiments/decider_finetune/device_parity/device-parity-cpu-v1.json")
    pair = cpu["same_device_comparison"]
    for key in (
        "first_choice_correct",
        "integrated_wasted_first_reads",
        "integrated_verified_completions",
    ):
        base, total = ratio(pair["baseline"][key])
        found[f"cpu_{key}"] = (base, ratio(pair["adapted"][key])[0], total)
    agreement = cpu["device_variance"]["adapted_cpu_versus_adapted_cuda"]
    agreed, rows = ratio(agreement["choice_agreement"])
    found["cpu_cuda_differences"] = (rows - agreed, rows - agreed, rows)
    return found


def figure_outcomes() -> dict[str, list[str]]:
    c = counts()
    choice, wasted, done = (
        c["first_choice_correct"],
        c["integrated_wasted_first_reads"],
        c["integrated_verified_completions"],
    )
    head_choice, head_wasted = c["head_only_first_choice"], c["head_only_wasted"]
    return {
        "outcome-choices": [
            f"Correct first choices: {share(choice[0], choice[2])} "
            f"→ {share(choice[1], choice[2])}",
            f"{choice[1]} of {choice[2]}, up from {choice[0]}: "
            f"{choice[1] - choice[0]} more",
        ],
        "outcome-wasted": [
            f"Wasted first reads: {share(wasted[0], wasted[2])} "
            f"→ {share(wasted[1], wasted[2])}",
            f"{wasted[1]} of {wasted[2]} calls, down from {wasted[0]}: "
            f"{wasted[0] - wasted[1]} fewer",
        ],
        "outcome-completions": [
            f"Verified completions: {share(done[0], done[2])} "
            f"→ {share(done[1], done[2])}",
            f"{done[1]} of {done[2]} calls, up from {done[0]}: "
            f"{done[1] - done[0]} more",
        ],
        "outcome-head-only": [
            f"Head-only training reached {head_choice[1]} of {head_choice[2]} "
            f"correct, but wasted reads stayed at {head_wasted[1]} of "
            f"{head_wasted[2]} calls, so it was rejected."
        ],
    }


def test_every_visible_outcome_matches_its_record() -> None:
    document = tree()
    for identifier, phrases in figure_outcomes().items():
        node = document.find(f".//{NS}*[@id='{identifier}']")
        shown = visible(node) if node.tag == f"{NS}g" else "".join(node.itertext())
        for phrase in phrases:
            assert phrase in shown, f"{identifier}: {phrase}"


def readme_table_rows() -> list[str]:
    c = counts()
    rows = []
    for label, key, unit, verb in (
        ("Correct first choices", "first_choice_correct", "decisions", "more"),
        ("Wasted first reads", "integrated_wasted_first_reads", "full calls", None),
        ("Verified completions", "integrated_verified_completions", "calls", "more"),
    ):
        base, adapted, total = c[key]
        if verb is None:
            fewer = base - adapted
            relative = f"{Decimal(100 * fewer) / Decimal(base):.0f}%"
            change = f"{fewer} fewer calls, {relative} fewer"
        else:
            noun = "decisions" if unit == "decisions" else "calls"
            change = f"{adapted - base} more {noun}"
        rows.append(
            f"| {label} | {base} of {total} {unit} ({share(base, total)}) "
            f"| {adapted} of {total} {unit} ({share(adapted, total)}) | {change} |"
        )
    return rows


def test_the_readme_table_names_denominators_and_deltas() -> None:
    section = ml_section(readme()).replace("**", "")
    assert "| Measure | Base Decider 2B | LoRA plus head | Change |" in section
    for row in readme_table_rows():
        assert row in section, row


def test_the_readme_states_head_only_and_cpu_replay_from_records() -> None:
    c = counts()
    section = flat(ml_section(readme()))
    head = c["head_only_first_choice"]
    cpu_choice = c["cpu_first_choice_correct"]
    cpu_wasted = c["cpu_integrated_wasted_first_reads"]
    cpu_done = c["cpu_integrated_verified_completions"]
    differences = c["cpu_cuda_differences"]
    for phrase in (
        f"Head-only training reached {head[1]} correct first choices out of "
        f"{head[2]} ({share(head[1], head[2])}), but wasted reads stayed at "
        f"{c['head_only_wasted'][1]} of {c['head_only_wasted'][2]} calls, "
        "so it was rejected.",
        f"correct first choices to {cpu_choice[1]} of {cpu_choice[2]} "
        f"({share(cpu_choice[1], cpu_choice[2])}), up from {cpu_choice[0]} "
        f"({share(cpu_choice[0], cpu_choice[2])})",
        f"wasted first reads to {cpu_wasted[1]} of {cpu_wasted[2]}, "
        f"down from {cpu_wasted[0]}",
        f"completions to {cpu_done[1]} of {cpu_done[2]}, up from {cpu_done[0]}",
        f"CPU and CUDA differed on {differences[0]} of {differences[2]} "
        "near-tie decisions",
    ):
        assert phrase in section, phrase


def test_the_description_carries_every_outcome_and_boundary() -> None:
    c = counts()
    text = description()
    choice = c["first_choice_correct"]
    wasted = c["integrated_wasted_first_reads"]
    done = c["integrated_verified_completions"]
    for phrase in (
        f"correct first choices {choice[1]} of {choice[2]} decisions "
        f"({share(choice[1], choice[2])}), up from {choice[0]} "
        f"({share(choice[0], choice[2])}), {choice[1] - choice[0]} more decisions",
        f"wasted first reads {wasted[1]} of {wasted[2]} full calls "
        f"({share(wasted[1], wasted[2])}), down from {wasted[0]} "
        f"({share(wasted[0], wasted[2])}), {wasted[0] - wasted[1]} fewer calls",
        f"verified completions {done[1]} of {done[2]} calls "
        f"({share(done[1], done[2])}), up from {done[0]} "
        f"({share(done[0], done[2])}), {done[1] - done[0]} more calls",
        "exact-precludes only that repeat before selection",
        "a receipt cannot say which of them is most productive",
        "the deterministic verifier labelled which read moved the supported "
        "disposition",
        "trained LoRA adapters and the Decider choice head",
        "frozen and replayed through the same controller path",
        "carries only the frozen checkpoint into runtime; nothing flows back",
        "remains the only model tool selector",
        "the verifier decides the outcome and writes the next receipt",
        "Nothing learns at runtime.",
        "The animation plays once",
        "not a measured row",
        "so it was rejected",
        "in-distribution controlled variation",
        "is experimental and is not the deployed runtime selector",
        "No hosted, latency or cost result is shown.",
    ):
        assert phrase in text, phrase


# ---------------------------------------------------------------------------
# Clarity: no bare ratios, and every stated percentage is arithmetic


def bare_fractions(text: str) -> list[str]:
    stripped = re.sub(r"\]\([^)]*\)|`[^`]*`", " ", text)
    return BARE_FRACTION.findall(stripped)


def wrong_shares(text: str) -> list[str]:
    body = flat(text)
    wrong = [
        match.group(0)
        for match in STATED_SHARE.finditer(body)
        if share(int(match.group(1)), int(match.group(2))) != f"{match.group(3)}%"
    ]
    return wrong + [
        match.group(0)
        for match in BASELINE_SHARE.finditer(body)
        if share(int(match.group(2)), int(match.group(1))) != f"{match.group(3)}%"
    ]


def clarity_sites() -> dict[str, str]:
    document = tree()
    text = readme()
    return {
        "figure text": visible(document),
        "figure title": title(),
        "figure description": description(document),
        "readme opening": opening(text),
        "readme ml section": ml_section(text),
    }


@pytest.mark.parametrize("site", sorted(clarity_sites()))
def test_no_bare_fraction_in_the_ml_story(site: str) -> None:
    assert bare_fractions(clarity_sites()[site]) == []


@pytest.mark.parametrize("site", sorted(clarity_sites()))
def test_every_stated_percentage_is_arithmetic(site: str) -> None:
    assert wrong_shares(clarity_sites()[site]) == []


def test_percentages_are_actually_stated() -> None:
    section = flat(ml_section(readme()))
    assert len(STATED_SHARE.findall(section)) >= 7
    assert len(BASELINE_SHARE.findall(section)) >= 1
    assert len(STATED_SHARE.findall(description())) >= 4
    assert len(BASELINE_SHARE.findall(description())) >= 4


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Correct first choices: 129/160.", ["129/160"]),
        ("wasted 20 / 64", ["20 / 64"]),
        ("See [`a/b`](docs/x/1/2.md) and 129 of 160.", []),
    ],
)
def test_bare_fraction_detector_positive_controls(
    text: str, expected: list[str]
) -> None:
    assert bare_fractions(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "15 of 64 full calls (23.5%)",
        "20 of 64 calls (31.2%)",
        "129 of 160 decisions (80.7%)",
        "15 of 64 full calls (23.4%), down from 20 (31.2%)",
        "132 of 160 (82.5%), up from 96 (61.0%)",
    ],
)
def test_wrong_share_detector_positive_controls(text: str) -> None:
    assert wrong_shares(text)


def test_share_rounds_half_up() -> None:
    assert (share(20, 64), share(44, 64), share(129, 160)) == (
        "31.3%",
        "68.8%",
        "80.6%",
    )


# ---------------------------------------------------------------------------
# No overclaim, no sidelining, no private or out-of-scope content


def test_the_figure_makes_no_overclaim() -> None:
    for text in (visible(tree()), title(), description()):
        assert overclaims(text) == []


@pytest.mark.parametrize(
    "claim",
    [
        "The result is statistically significant.",
        "The adapted checkpoint generalizes.",
        "It ranks unseen situations.",
        "The ranking is proven safe.",
        "A guaranteed improvement.",
        "This raises accuracy.",
        "Production-ready ranking.",
        "A latency improvement on every call.",
        "Decider 2B learns from each receipt.",
        "It uses online learning.",
        "The adapted ranking replaces the verifier.",
        "Hosted parity passed.",
        "Verdict: promotion_eligible.",
        "The adapted checkpoint is now deployed.",
    ],
)
def test_the_overclaim_detector_catches_its_own_example(claim: str) -> None:
    assert overclaims(claim)


@pytest.mark.parametrize(
    "honest",
    [
        "LoRA never replaces Decision Receipts or the verifier.",
        "Zero false repairs were structurally unreachable: not safety evidence.",
        "Follow-up studies stopped: STOP_INSUFFICIENT_HOLDOUT.",
        "No hosted, latency or cost result is shown.",
        "Nothing learns at runtime. No online learning.",
        "That adapted checkpoint is not the currently deployed runtime selector.",
    ],
)
def test_the_overclaim_detector_passes_honest_negations(honest: str) -> None:
    assert overclaims(honest) == []


def sidelining(text: str) -> list[str]:
    return SIDELINED.findall(text.replace(FIGURE, " "))


def test_lora_is_never_framed_as_an_afterthought() -> None:
    text = readme()
    for site in (
        opening(text),
        ml_section(text),
        raw(),
    ):
        assert sidelining(site) == []
    headings = re.findall(r"^#{1,3} .*$", text, re.M)
    assert [h for h in headings if sidelining(h)] == []


@pytest.mark.parametrize(
    "framing",
    [
        "## ML add-on: LoRA ranks the offer",
        "An optional ranking addon.",
        "An addendum on learned ranking.",
        "LoRA is the last step.",
    ],
)
def test_sidelining_detector_positive_controls(framing: str) -> None:
    assert sidelining(framing)


def test_the_readme_ml_story_makes_no_overclaim() -> None:
    text = readme()
    assert overclaims(opening(text)) == []
    assert overclaims(ml_section(text)) == []


def test_the_figure_carries_no_private_or_out_of_scope_content() -> None:
    assert scan_file(FIGURE, root=ROOT) == []
    assert scan_text(FIGURE, raw()) == []
    assert OUT_OF_SCOPE.findall(raw()) == []


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.name)
def test_scanner_positive_controls_inside_the_figure(check) -> None:
    injected = raw().replace("</svg>", f"<desc>{check.positive_control}</desc></svg>")
    assert any(finding.check == check.name for finding in scan_text(FIGURE, injected))


@pytest.mark.parametrize(
    "term", ["NBA", "customer", "submission", "SageMaker endpoint", "account"]
)
def test_out_of_scope_detector_catches_its_own_example(term: str) -> None:
    assert OUT_OF_SCOPE.search(f"<text>{term} result</text>")


def test_one_accent_and_readable_type() -> None:
    text = raw()
    assert set(re.findall(r"#[0-9A-Fa-f]{6}\b", text)) == PALETTE
    # Three warm neutrals and one saturated accent.
    spreads = {
        colour: max(channels) - min(channels)
        for colour in PALETTE
        for channels in [[int(colour[i : i + 2], 16) for i in (1, 3, 5)]]
    }
    assert [colour for colour, spread in spreads.items() if spread > 60] == ["#F1834B"]
    assert not re.search(r"shadow|gradient|<filter|filter\s*:|blur", text, re.I)
    sizes = [int(size) for size in re.findall(r"font-size:\s*(\d+)px", text)]
    assert sizes and min(sizes) >= 20
    assert "font-size=" not in text


def test_the_original_flow_figure_is_untouched() -> None:
    manifest = json.loads((ROOT / "PUBLIC-MANIFEST.json").read_text())
    entry = next(item for item in manifest["files"] if item["path"] == ORIGINAL)
    digest = hashlib.sha256((ROOT / ORIGINAL).read_bytes()).hexdigest()
    assert digest == entry["sha256"]


# ---------------------------------------------------------------------------
# README: LoRA is one of two core mechanisms, explained before its results


def assert_readme_story(text: str) -> None:
    head = flat(opening(text))
    assert "Unstuck has two core mechanisms." in head
    assert head.index("Decision Receipts stop a new call") < head.index(
        "A LoRA-adapted Decider 2B then ranks the tools that remain"
    )
    assert "is experimental and is not the currently deployed runtime selector" in (
        head
    )
    headings = re.findall(r"^## .*$", text, re.M)
    base = headings.index(BASE_HEADING)
    assert headings[base + 1] == ML_HEADING
    section = ml_section(text)
    images = EMBED.findall(text)
    assert len(images) == 1 and len(EMBED.findall(section)) == 1
    marks = [
        section.index(phrase)
        for phrase in (
            "a receipt cannot tell Decider 2B which of them is most productive",
            "without adding a second selector",
            "**What we trained.**",
            "**Where it acts.**",
            "](docs/unstuck-ml-addon-flow.svg)",
            CAPTION,
            "| Measure |",
        )
    ]
    assert marks == sorted(marks)
    flattened = flat(section)
    for phrase in (
        "every diagnostic offered in a frozen synthetic corpus was executed",
        "the deterministic verifier labelled which read moved the supported "
        "disposition",
        "Those labels trained LoRA adapters plus the Decider choice head",
        "The checkpoint was then frozen and replayed through the same controller path",
        "Exact receipts filter the offer first",
        "The base or adapted Decider 2B ranks what remains",
        "the verifier decides the outcome and writes the next receipt",
        "Nothing learns at runtime.",
        "Decider 2B stays the only model that selects a tool",
        "no authority over receipts, repairs or truth",
        "(docs/research/decider-adaptation-findings.md)",
        "(experiments/decider_finetune/device_parity/FINDINGS.md)",
    ):
        assert phrase in flattened, phrase
    alt = images[0]
    for phrase in (
        "Receipts constrain. LoRA ranks what remains.",
        "Offline training, once, before any run",
        "Runtime decision, every call",
        "nothing flows back",
        "The animation plays once",
        "head-only training was rejected",
    ):
        assert phrase in alt, phrase
    assert text.count(CAPTION) == 1
    assert not re.search(r"<svg\b", text, re.I)
    assert len(re.findall(r"\]\(docs/unstuck-flow\.svg\)", text)) == 1
    assert 100 <= len(text.splitlines()) <= 130


def test_readme_places_learned_ranking_beside_receipts() -> None:
    assert_readme_story(readme())


def _ml_section_moved_to_the_end(text: str) -> str:
    section = ml_section(text)
    return text.replace(section, "", 1).rstrip() + "\n\n" + section


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: text.replace(FIGURE, "docs/missing.svg"),
        lambda text: text.replace("Unstuck has two core mechanisms. ", ""),
        lambda text: text.replace("**What we trained.**", "**Training.**"),
        lambda text: text.replace("Nothing learns at runtime.", "", 1),
        _ml_section_moved_to_the_end,
    ],
    ids=["missing", "opening", "training", "runtime", "placement"],
)
def test_readme_story_positive_controls(mutate) -> None:
    assert mutate(readme()) != readme()
    with pytest.raises((AssertionError, ValueError)):
        assert_readme_story(mutate(readme()))


def test_the_record_wording_stays_in_the_audit_subsection() -> None:
    text = readme()
    # The research-record guard pins these record strings verbatim; they live
    # only in the audit subsection, each beside its plain-language form.
    audit = text.split(EVIDENCE_HEADING, 1)[1].split("\n## ", 1)[0]
    assert text.index(EVIDENCE_HEADING) > text.index("## Limits")
    assert "from 64 of 80 (80.0%) to 70 of 80 (87.5%), recorded as 64/80" in audit
    assert "from 20 of 64 to 15 of 64 calls, recorded as 20/64 to 15/64" in audit
    assert wrong_shares(audit) == []
