"""The drawn story holds its shape: figure, demo, deck, frames and PowerPoint. Offline.

The figure, the offline demo and the pitch deck tell one causal sequence. The
memoryless control repeats the dead-end first choice and stays unresolved. Run 1
spends its one read on that choice, stays unresolved, and the verifier writes a
Decision Receipt. Run 2 matches the receipt exactly, precludes the dead end before
the first Decider request, and completes on one fresh choice and one read.

These tests fail when a visual drifts back to the earlier two-read story, loses
the control, the one-read labels, pre-selection or the Decider-owned choice,
carries a number the curated record does not, carries an
identifier, or when the frames lose their size, the deck its slide order, the
PowerPoint file its notes, or the demo its controls. Each detector carries its
own positive control, so a detector that stops detecting fails rather than
passing vacuously.
"""

import html
import json
import re
import struct
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from publishing.public_scan import (
    PITCH_FRAMES,
    PITCH_PPTX,
    pptx_inspectable_text,
    scan_file,
    scan_text,
)

ROOT = Path(__file__).resolve().parents[2]
CURATED = ROOT / "dist" / "public-demo" / "loop-escape-proof.json"
FIGURE = "docs/architecture.svg"
DEMO = "docs/demo.html"
DECK = "submission/pitch/unstuck-pitch.html"
PPTX_BUILD = "submission/pitch/build/make-pptx.mjs"
VISUALS: tuple[str, ...] = (FIGURE, DEMO, DECK)

FRAME_SIZE = (1280, 720)
#: 13.333 x 7.5 inches in English Metric Units, PowerPoint's wide layout.
WIDE_LAYOUT_EMU = (12192000, 6858000)

#: The approved slide order, title for title.
SLIDE_TITLES: tuple[str, ...] = (
    "Unstuck gets Decider 2B out of a decision loop.",
    "A new call can repeat a decision already proven useless.",
    "Run 1 pays the learning cost once.",
    "Run 2 uses the receipt before selection.",
    "Measured Run 2 improvement.",
    "The private AWS execution path.",
    "The reusable pattern.",
    "One failed decision becomes a boundary.",
)
DECK_SUBTITLE = "Run 1 pays once. Run 2 uses the receipt before selection."

#: The earlier lead: a first call that repaired on two reads, one fewer later,
#: and the figures that only that pair or private records carried.
OLD_VISUAL_STORY = re.compile(
    r"one fewer (?:diagnostic )?read|fewer diagnostic read|\b2 reads\b"
    r"|two diagnostic reads|spent two|pair of calls|same verified rows"
    r"|0\.3067|0\.4746|0\.2158|0\.6498|\bIN 4\b|\bUS 5\b|unsigned probes?"
    r"|\b4 of 4\b|\b0\.80\b|(?:call one|call 1|run 1)\b[^.|]{0,80}"
    r"(?:repaired|populated_after_repair|repair applie)",
    re.IGNORECASE,
)
OLD_STORY_EXAMPLES = (
    "Same verified rows, one fewer read.",
    "CALL 1 history: empty / 2 reads",
    "inspect_pipeline chosen, 0.3067",
    "Verified rows IN 4 US 5",
    "4 of 4 unsigned probes denied",
    "Call 1 re-decides, repairs, and finishes with populated_after_repair",
)

POST_CHOICE_STORY = re.compile(
    r"\bD2\b|re-decision|re-decides|both choices|first choice is inspect_pipeline again"
    r"|D1 repeats|fresh D1 repeats|choice opens|block opens|chosen again",
    re.I,
)
JUDGING_LANGUAGE = re.compile(
    r"why it scores|rubric|judging|creativity and cool factor"
    r"|communication quality|originality|(?:achieved|predicted|estimated) score",
    re.I,
)

#: A claim the evidence cannot carry. A sentence naming one must negate it.
FORBIDDEN_CLAIMS: dict[str, re.Pattern[str]] = {
    "infinite loop": re.compile(r"infinite loop", re.IGNORECASE),
    "biggest problem": re.compile(r"biggest problem", re.IGNORECASE),
    "model learning": re.compile(
        r"\b(?:the model|decider(?: 2b)?|the checkpoint)\s+(?:learn|learns|learned"
        r"|improves|improved)\b(?!\s+nothing)",
        re.IGNORECASE,
    ),
    "accuracy": re.compile(r"\baccura(?:cy|te)\b", re.IGNORECASE),
    "speed": re.compile(r"\bfast(?:er|est)?\b|\bquick(?:ly|er)?\b", re.IGNORECASE),
    "latency benefit": re.compile(r"latency (?:benefit|improvement|gain)", re.I),
    "cost benefit": re.compile(r"cost (?:benefit|saving|improvement|reduction)", re.I),
    "novelty": re.compile(r"\bnovel(?:ty)?\b|\bfirst ever\b", re.IGNORECASE),
    "perfect score": re.compile(r"perfect score|\b100 ?%|full marks", re.IGNORECASE),
    "production": re.compile(r"production[- ]ready|production readiness", re.I),
}
NEGATION = re.compile(
    r"\b(?:not|no|never|nor|nothing|none|without|cannot|refused|unclaimed)\b"
    r"|n't\b",
    re.IGNORECASE,
)
CLAIM_EXAMPLES: dict[str, str] = {
    "infinite loop": "Unstuck breaks the infinite loop.",
    "biggest problem": "This is the biggest problem in agents.",
    "model learning": "Decider 2B learns from the receipt.",
    "accuracy": "Accuracy rises with receipts.",
    "speed": "Decider 2B makes fast tool decisions.",
    "latency benefit": "A latency benefit appears on Run 2.",
    "cost benefit": "A cost saving appears on Run 2.",
    "novelty": "A novel mechanism.",
    "perfect score": "This entry earns a perfect score.",
    "production": "It is production ready.",
}

#: What every visual must show, as a label a viewer can read.
REQUIRED_STORY: dict[str, re.Pattern[str]] = {
    "the control": re.compile(r"memoryless control|without receipts", re.I),
    "three control calls unresolved": re.compile(
        r"\b3 of 3\b[^.]{0,40}unresolved"
        r"|unresolved[^.]{0,40}\b3 of 3\b",
        re.I,
    ),
    "run 1": re.compile(r"\brun 1\b", re.I),
    "run 2": re.compile(r"\brun 2\b", re.I),
    "the dead end": re.compile(r"inspect_pipeline"),
    "the receipt": re.compile(r"disposition_unchanged"),
    "one read per call": re.compile(r"\b(?:1|one) read\b", re.I),
    "pre-selection": re.compile(r"before the first Decider request", re.I),
    "remaining offer in order": re.compile(r"original order", re.I),
    "one fresh choice": re.compile(r"one fresh choice", re.I),
    "no second call": re.compile(r"no second Decider call", re.I),
    "the new path": re.compile(r"inspect_schema_keys"),
    "the same decider": re.compile(r"same decider", re.I),
    "completion": re.compile(r"completed", re.I),
    "n = 1": re.compile(r"\bn = 1\b"),
}
MISSING_STORY_EXAMPLE = "Two calls, one blocked repeat. Call 1 repairs."

IDENTIFIER_EXAMPLES = (
    "arn:aws:sagemaker:us-east-1:123456789012:endpoint/decider",
    "account 123456789012 owns the endpoint",
    "/Users/someone/Documents/private/proof.json",
    "receipt deadbeefdeadbeefdeadbeef0123456789abcdef0123456789abcdef01234567",
)


def text_of(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def visible_text(relative_path: str) -> str:
    """What a viewer reads: text nodes, attribute labels, script strings, notes.

    Style sheets and markup are dropped; double-quoted script strings are kept
    because the demo builds every step from them.
    """
    raw = text_of(relative_path)
    if relative_path.endswith(".svg"):
        tree = ElementTree.fromstring(raw)
        pieces = [
            text
            for element in tree.iter()
            if not str(element.tag).endswith("style")
            for text in (element.text, element.tail)
            if text
        ]
        return flat(" ".join(pieces))
    raw = re.sub(r"<style\b[^>]*>.*?</style>", " ", raw, flags=re.S)
    strings: list[str] = []
    for script in re.findall(r"<script\b[^>]*>(.*?)</script>", raw, flags=re.S):
        strings.extend(re.findall(r'"((?:[^"\\]|\\.)*)"', script))
    raw = re.sub(r"<script\b[^>]*>.*?</script>", " ", raw, flags=re.S)
    labels = re.findall(r'(?:aria-label|alt|title|content)="([^"]*)"', raw)
    body = re.sub(r"<[^>]+>", " ", raw)
    return flat(html.unescape(" ".join([body, *labels, *strings])))


def deck_notes() -> list[str]:
    block = re.search(
        r'<script type="application/json" id="speaker-notes">(.*?)</script>',
        text_of(DECK),
        flags=re.S,
    )
    assert block, "the deck carries no speaker-notes block"
    notes = json.loads(block.group(1))
    assert isinstance(notes, list)
    return [str(note) for note in notes]


def build_alt_text() -> list[str]:
    block = re.search(r"const ALT = \[(.*?)\n\];", text_of(PPTX_BUILD), flags=re.S)
    assert block, "make-pptx.mjs lost its ALT array"
    return re.findall(r'^\s*"((?:[^"\\]|\\.)*)",?\s*$', block.group(1), flags=re.M)


def sentences(text: str) -> list[str]:
    return [piece for piece in re.split(r"(?<=[.!?])\s+", text) if piece.strip()]


def unsupported_claims(text: str) -> list[str]:
    found = []
    for sentence in sentences(text):
        for name, pattern in FORBIDDEN_CLAIMS.items():
            if pattern.search(sentence) and not NEGATION.search(sentence):
                found.append(f"{name}: {sentence[:120]}")
    return found


def curated_numbers() -> set[str]:
    """Every count the curated record carries, as written."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "value" and isinstance(value, int):
                    found.add(str(value))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            found.update(re.findall(r"\b\d+\b", node))

    walk(json.loads(CURATED.read_text(encoding="utf-8")))
    return found


STRUCTURAL_NUMBERS = {str(number) for number in range(11)}


def client_display_values() -> set[str]:
    record = json.loads(CURATED.read_text(encoding="utf-8"))
    client = record["measured_latency"]["agentcore_client_time"]
    return {
        *(
            f"{client[key]['elapsed']['value'] / 1000:.1f}"
            for key in (
                "run_1_learning",
                "superseded_run_2_post_d1",
                "optimized_run_2_preselection",
            )
        ),
        f"{client['difference']['absolute']['value'] / 1000:.1f}",
        str(client["difference"]["relative"]["value"]),
    }


def stray_numbers(text: str) -> list[str]:
    allowed = curated_numbers() | STRUCTURAL_NUMBERS
    stray = [
        d for d in re.findall(r"\b\d+\.\d+\b", text) if d not in client_display_values()
    ]
    without_decimals = re.sub(r"\b\d+\.\d+\b", " ", text)
    for number in re.findall(r"(?<![\w.-])\d+(?![\w.])", without_decimals):
        if str(int(number)) not in allowed:
            stray.append(number)
    return stray


def png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", head[16:24])
    return width, height


# ----------------------------------------------------------------- the story


@pytest.mark.parametrize("relative_path", VISUALS)
def test_no_visual_carries_the_earlier_two_read_story(relative_path: str) -> None:
    stale = OLD_VISUAL_STORY.findall(visible_text(relative_path))
    assert not stale, f"{relative_path} still tells the two-read story: {stale}"


@pytest.mark.parametrize("stale", OLD_STORY_EXAMPLES)
def test_the_old_story_detector_catches_its_own_example(stale: str) -> None:
    assert OLD_VISUAL_STORY.search(stale), stale


@pytest.mark.parametrize("relative_path", VISUALS)
@pytest.mark.parametrize("label", sorted(REQUIRED_STORY))
def test_every_visual_shows_the_causal_story(relative_path: str, label: str) -> None:
    text = visible_text(relative_path)
    assert REQUIRED_STORY[label].search(text), f"{relative_path} lost {label}"


def test_the_story_detector_fails_on_the_old_figure_text() -> None:
    missing = [
        label
        for label, pattern in REQUIRED_STORY.items()
        if not pattern.search(MISSING_STORY_EXAMPLE)
    ]
    assert {"the control", "the receipt", "the same decider", "n = 1"} <= set(missing)


@pytest.mark.parametrize("relative_path", VISUALS)
def test_no_visual_makes_an_unsupported_claim(relative_path: str) -> None:
    claims = unsupported_claims(visible_text(relative_path))
    assert not claims, f"{relative_path}: {claims}"


@pytest.mark.parametrize("name", sorted(CLAIM_EXAMPLES))
def test_the_claim_detector_catches_its_own_example(name: str) -> None:
    assert unsupported_claims(CLAIM_EXAMPLES[name]), name


@pytest.mark.parametrize("relative_path", VISUALS)
def test_every_number_on_a_visual_comes_from_the_curated_record(
    relative_path: str,
) -> None:
    stray = stray_numbers(visible_text(relative_path))
    assert not stray, f"{relative_path} shows numbers the record does not: {stray}"


def test_the_number_detector_catches_an_invented_figure() -> None:
    assert stray_numbers("D1 at 0.3067, then 23 seconds, then 403 denied") == [
        "0.3067",
        "23",
        "403",
    ]
    assert stray_numbers("13 of 13 checks, 25 of 25, 5 choices, Decider 2B") == []
    assert stray_numbers("21.6 s, 54.8 s, 26.6 s, 28.2 s, 51.4%") == []
    assert stray_numbers("20 of 20 checks, 52.6% lower client latency")


@pytest.mark.parametrize("relative_path", (*VISUALS, PPTX_BUILD))
def test_no_visual_source_carries_an_identifier(relative_path: str) -> None:
    findings = scan_file(relative_path)
    assert not findings, [f"{f.check}: {f.matched}" for f in findings]


@pytest.mark.parametrize("example", IDENTIFIER_EXAMPLES)
def test_the_identifier_scan_catches_its_own_example(example: str) -> None:
    assert scan_text("example", example), example


# ------------------------------------------------------------------ the figure


def test_the_figure_is_valid_accessible_xml() -> None:
    tree = ElementTree.fromstring(text_of(FIGURE))
    ns = "{http://www.w3.org/2000/svg}"
    assert tree.tag == f"{ns}svg"
    assert tree.get("role") == "img"
    title = tree.find(f"{ns}title")
    desc = tree.find(f"{ns}desc")
    assert title is not None and (title.text or "").strip()
    assert desc is not None and len((desc.text or "").strip()) > 200
    labelled = (tree.get("aria-labelledby") or "").split()
    assert title.get("id") in labelled and desc.get("id") in labelled


def test_the_figure_reaches_nothing_outside_itself() -> None:
    raw = text_of(FIGURE)
    assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", raw)
    assert not re.search(r"<image\b|xlink:href|@import|url\(", raw)


def test_the_figure_description_tells_all_three_lanes() -> None:
    desc = ElementTree.fromstring(text_of(FIGURE)).find(
        "{http://www.w3.org/2000/svg}desc"
    )
    text = flat((desc.text or "") if desc is not None else "").lower()
    for phrase in ("control", "run 1", "run 2", "disposition_unchanged", "block"):
        assert phrase in text, phrase


# -------------------------------------------------------------------- the demo


def test_the_demo_has_working_accessible_controls() -> None:
    raw = text_of(DEMO)
    buttons = re.findall(r"<button\b([^>]*)>(.*?)</button>", raw, flags=re.S)
    assert len(buttons) >= 3
    for attributes, label in buttons:
        assert 'type="button"' in attributes
        assert re.sub(r"<[^>]+>", "", label).strip() or "aria-label=" in attributes
    for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
        assert f'"{key}"' in raw, f"the demo lost the {key} key"
    assert "prefers-reduced-motion" in raw
    assert ":focus-visible" in raw
    assert 'aria-live="polite"' in raw
    assert "aria-current" in raw
    assert re.search(r"<h1\b", raw) and "h2" in raw
    assert "@media (max-width" in raw


@pytest.mark.parametrize("relative_path", (DEMO, DECK))
def test_the_html_needs_no_network(relative_path: str) -> None:
    raw = text_of(relative_path)
    assert not re.search(r"https?://", raw), "a URL in an offline file"
    assert not re.search(r"\bfetch\(|XMLHttpRequest|WebSocket|EventSource", raw)
    assert not re.search(r"<link\b|@import|<script\b[^>]*\bsrc=|<img\b", raw)


def test_the_network_detector_catches_its_own_example() -> None:
    sample = '<link rel="stylesheet" href="https://fonts.example/x.css">'
    assert re.search(r"https?://", sample) and re.search(r"<link\b", sample)


# -------------------------------------------------------------------- the deck


def deck_titles() -> list[str]:
    sections = re.findall(
        r'<section class="slide"[^>]*>(.*?)</section>', text_of(DECK), flags=re.S
    )
    titles = []
    for section in sections:
        heading = re.search(r"<h([12])\b[^>]*>(.*?)</h\1>", section, flags=re.S)
        assert heading, "a slide without a heading"
        titles.append(flat(html.unescape(re.sub(r"<[^>]+>", " ", heading.group(2)))))
    return [re.sub(r"\s+([.,])", r"\1", title) for title in titles]


def test_the_deck_keeps_the_approved_slide_order() -> None:
    assert deck_titles() == list(SLIDE_TITLES)


def test_the_order_detector_rejects_the_earlier_deck() -> None:
    earlier = [
        "Agents remember facts. They still repeat bad decisions.",
        "Four reads offered. It picks the one that cannot answer.",
    ]
    assert earlier != list(SLIDE_TITLES[:2])


def test_the_deck_opens_on_the_approved_subtitle() -> None:
    assert DECK_SUBTITLE in re.sub(r"\s+([.,])", r"\1", visible_text(DECK))


def test_every_slide_is_labelled_with_its_position_and_title() -> None:
    labels = re.findall(
        r'<section class="slide"[^>]*aria-label="([^"]*)"', text_of(DECK)
    )
    assert len(labels) == len(SLIDE_TITLES)
    for index, (label, title) in enumerate(zip(labels, SLIDE_TITLES, strict=True), 1):
        assert label.startswith(f"Slide {index} of 8."), label
        assert title.rstrip(".") in html.unescape(label), label


def test_the_notes_and_alt_text_are_plain_current_and_complete() -> None:
    notes = deck_notes()
    alts = build_alt_text()
    assert len(notes) == len(SLIDE_TITLES) and len(alts) == len(SLIDE_TITLES)
    for text in (*notes, *alts):
        assert text.strip()
        assert re.fullmatch(r"[\x20-\x7E]*", text), "non-ASCII in a note or alt text"
        assert "--" not in text
        assert not OLD_VISUAL_STORY.search(text), text[:120]
        assert not unsupported_claims(text), text[:120]
        assert not stray_numbers(text), (stray_numbers(text), text[:80])
    joined = " ".join(notes).lower()
    for phrase in ("control", "run 1", "run 2", "inspect_schema_keys"):
        assert phrase in joined, phrase
    assert "n = 1" in joined or "n equals one" in joined, "the notes lost n = 1"


# ------------------------------------------------------- frames and PowerPoint


@pytest.mark.parametrize("relative_path", PITCH_FRAMES)
def test_every_frame_is_exactly_the_capture_size(relative_path: str) -> None:
    assert png_size(ROOT / relative_path) == FRAME_SIZE


def test_the_size_detector_reads_a_wrong_frame(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong.png"
    wrong.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1920, 1080)
    )
    assert png_size(wrong) != FRAME_SIZE


def test_the_powerpoint_file_is_eight_wide_slides_with_the_deck_notes() -> None:
    with zipfile.ZipFile(ROOT / PITCH_PPTX) as bundle:
        names = bundle.namelist()
        presentation = bundle.read("ppt/presentation.xml").decode("utf-8")
        slides = sorted(
            n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
        )
        notes = sorted(
            n for n in names if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", n)
        )
        size = re.search(r'<p:sldSz cx="(\d+)" cy="(\d+)"', presentation)
        assert size and (int(size.group(1)), int(size.group(2))) == WIDE_LAYOUT_EMU
        assert len(slides) == len(SLIDE_TITLES) == len(notes)
        for name in slides:
            xml = bundle.read(name).decode("utf-8")
            assert xml.count("<p:pic>") == 1, f"{name} is not one full-bleed frame"
            assert re.search(r'descr="[^"]{40,}"', xml), f"{name} lost its alt text"
    texts = dict(pptx_inspectable_text(ROOT / PITCH_PPTX))
    expected = deck_notes()
    for index, note in enumerate(expected, 1):
        part = texts.get(f"ppt/notesSlides/notesSlide{index}.xml", "")
        assert flat(note) in flat(part), f"notes slide {index} differs from the deck"


def test_the_powerpoint_file_carries_no_stale_or_private_text() -> None:
    for part, text in pptx_inspectable_text(ROOT / PITCH_PPTX):
        assert not OLD_VISUAL_STORY.search(text), part
        assert not scan_text(part, text), part
        assert not re.search(r"(?:/Users/|/home/|[A-Z]:\\)", text), part
    with zipfile.ZipFile(ROOT / PITCH_PPTX) as bundle:
        core = bundle.read("docProps/core.xml").decode("utf-8")
    assert "<dcterms:created" not in core and "<dcterms:modified" not in core


@pytest.mark.parametrize("relative_path", VISUALS)
def test_current_visuals_never_select_the_dead_end_then_redecide(
    relative_path: str,
) -> None:
    assert not POST_CHOICE_STORY.search(visible_text(relative_path))


@pytest.mark.parametrize(
    "stale",
    [
        "Run 2 fresh D1 repeats inspect_pipeline.",
        "The block opens a re-decision.",
        "Same Decider, D2, inspect_schema_keys.",
        "Decider made both choices.",
    ],
)
def test_post_choice_detector_positive_controls(stale: str) -> None:
    assert POST_CHOICE_STORY.search(stale)


@pytest.mark.parametrize("relative_path", VISUALS)
def test_each_visual_carries_only_the_scoped_client_comparison(
    relative_path: str,
) -> None:
    text = visible_text(relative_path)
    for value in client_display_values():
        assert value in text
    for phrase in (
        "Run 1 is not saved",
        "superseded post-choice",
        "client",
        "n = 1",
        "No dollar savings measured",
    ):
        assert phrase.lower() in text.lower()


def test_deck_contains_no_judging_language_and_percent_only_on_slide_five() -> None:
    assert not JUDGING_LANGUAGE.search(visible_text(DECK))
    sections = re.findall(
        r'<section class="slide"[^>]*>(.*?)</section>', text_of(DECK), re.S
    )
    for position, section in enumerate(sections, 1):
        text = html.unescape(re.sub(r"<[^>]+>", " ", section))
        percents = re.findall(r"\d+(?:\.\d+)?\s*%", text)
        assert percents == (["51.4%"] if position == 5 else [])
    for text in (*deck_notes(), *build_alt_text()):
        assert not JUDGING_LANGUAGE.search(text)
        assert not POST_CHOICE_STORY.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "Why it scores",
        "Official rubric: 30%",
        "Creativity and cool factor",
        "Predicted score: 95",
        "Communication quality: 25%",
    ],
)
def test_judging_detector_positive_controls(text: str) -> None:
    assert JUDGING_LANGUAGE.search(text)


def test_demo_sequence_precedes_choice_and_preserves_offer() -> None:
    raw = text_of(DEMO)
    steps = raw.split("var STEPS = [", 1)[1].split("var COMPARE =", 1)[0]
    run_two = steps.split('plate: "New call, exact receipt matches"', 1)[1]
    assert run_two.index(
        'plate: "Dead end precluded before selection"'
    ) < run_two.index('plate: "Same Decider picks a new path"')
    preclusion, choice = run_two.split('plate: "Same Decider picks a new path"', 1)
    assert '"2 diagnostics + request_review, original order"' in preclusion
    assert '"First Decider request", "not sent yet"' in preclusion
    assert '"Reads and inferences spent here", "0 and 0"' in preclusion
    assert '"Fresh choice", "inspect_schema_keys"' in choice
    assert '"Run 2 inferences", "1"' in choice


def test_powerpoint_embeds_exact_current_frames_and_alt_text() -> None:
    namespace = {
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    assert sorted(
        path.name for path in (ROOT / "submission/pitch/frames").iterdir()
    ) == [f"slide-{number:02}.png" for number in range(1, 9)]
    with zipfile.ZipFile(ROOT / PITCH_PPTX) as bundle:
        for number, alt in enumerate(build_alt_text(), 1):
            slide = ElementTree.fromstring(bundle.read(f"ppt/slides/slide{number}.xml"))
            picture = slide.find(".//p:pic", namespace)
            assert picture is not None
            properties = picture.find("p:nvPicPr/p:cNvPr", namespace)
            assert properties is not None and properties.get("descr") == alt
            blip = picture.find(".//a:blip", namespace)
            assert blip is not None
            relationship_id = blip.get(f"{{{namespace['r']}}}embed")
            relationships = ElementTree.fromstring(
                bundle.read(f"ppt/slides/_rels/slide{number}.xml.rels")
            )
            target = next(
                item.get("Target")
                for item in relationships
                if item.get("Id") == relationship_id
            )
            assert target is not None and target.startswith("../media/")
            assert (
                bundle.read("ppt/" + target.removeprefix("../"))
                == (
                    ROOT / f"submission/pitch/frames/slide-{number:02}.png"
                ).read_bytes()
            )
            extent = picture.find("p:spPr/a:xfrm/a:ext", namespace)
            assert extent is not None
            assert (
                int(extent.get("cx", "0")),
                int(extent.get("cy", "0")),
            ) == WIDE_LAYOUT_EMU


def test_deliverable_status_records_verified_video_awaiting_publication() -> None:
    status = text_of("docs/public-deliverables.md")
    for artifact in ("Architecture figure", "Offline demo", "Pitch deck"):
        assert f"| {artifact} | Current |" in status
    assert "| Video | Ready to publish |" in status
    assert re.search(r"\bstale\b|pending (?:a )?rebuild", status, re.I) is None


def test_shipped_video_records_verified_video_awaiting_publication() -> None:
    report = json.loads(text_of("dist/video/validation.json"))
    assert report["status"] == "PASS"
    assert all(entry["status"] == "PASS" for entry in report["checks"])
    checklist = flat(text_of("docs/submission-checklist.md"))
    assert "Verified local promotion is complete; public hosting remains Pending" in (
        checklist
    )
    for document in (
        "README.md",
        "dist/video/README.md",
        "docs/video-script.md",
        "docs/submission-checklist.md",
    ):
        assert (
            re.search(r"\bstale\b|pending (?:a )?rebuild", text_of(document), re.I)
            is None
        )
