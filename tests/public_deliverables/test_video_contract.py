"""The edit leads with the incident and keeps the narration and evidence honest."""

import json
from pathlib import Path
from xml.etree import ElementTree

from publishing.public_scan import scan_file

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "submission/video"


def plan():
    return json.loads((SOURCES / "edit-plan.json").read_text())


def test_timeline_is_contiguous_and_every_cut_is_inside_the_original():
    edit = plan()
    elapsed = 0
    for scene in edit["scenes"]:
        assert scene["start_frame"] == elapsed
        assert all(
            0 <= start < end <= edit["source_frames"]
            for start, end in scene["source_ranges"]
        )
        assert scene["frames"] == sum(b - a for a, b in scene["source_ranges"])
        assert 0 <= scene["card_frames"] <= scene["frames"]
        elapsed += scene["frames"]
    assert elapsed == edit["total_frames"] == 3864
    assert elapsed / edit["fps"] < 180


def test_intro_flow_training_code_demo_metrics_and_credit_order():
    assert [s["key"] for s in plan()["scenes"]] == ['control', 'aws', 'ml-process', 'recipe', 'run-one', 'run-two', 'proof', 'ml-results', 'credit']


def test_script_matches_the_spoken_edit_and_names_the_author():
    script = (ROOT / "docs/video-script.md").read_text()
    blocks = script.split("\n## Scene ")[1:]
    assert len(blocks) == len(plan()["scenes"])
    for scene, block in zip(plan()["scenes"], blocks, strict=True):
        body = block.split("\n## ", 1)[0]
        spoken = " ".join(
            line[2:] for line in body.splitlines() if line.startswith("> ")
        )
        assert spoken == scene["narration"]
    assert "Barath Ganesh" in script
    assert "original Amazon Polly synthetic narration" in script


def test_incorrect_diagnostic_count_is_absent_from_audio_and_visual_sources():
    scene = next(s for s in plan()["scenes"] if s["key"] == "run-two")
    # The discarded spoken sentence lies between these exact source frames.
    assert scene["source_ranges"] == [[2695, 2980], [3088, 3376]]
    assert "three remaining diagnostics" not in scene["narration"].lower()
    svg = (SOURCES / "run-two.svg").read_text()
    assert "2 diagnostics + request_review" in svg
    assert "Original order" in svg
    assert "before selection" in svg


def test_training_success_keeps_comparator_study_design_and_deployment_clear():
    process = (SOURCES / "ml-process.svg").read_text()
    result = (SOURCES / "ml-results.svg").read_text()
    assert "AWS agent uses base Decider 2B" in process
    assert "frozen checkpoint" in process
    for phrase in (
        "97 → 129",
        "of 160 decisions",
        "20 → 15",
        "44 → 49",
        "of 64 full calls",
        "128 and 136",
        "All 20 test situations",
        "One seed",
        "25% fewer wasted first reads vs. base Decider 2B",
        "Target: at least 20%",
        "Offline synthetic study",
    ):
        assert phrase in result


def test_cards_are_local_editable_vectors_with_accessible_titles():
    ns = "{http://www.w3.org/2000/svg}"
    for card in SOURCES.glob("*.svg"):
        document = ElementTree.fromstring(card.read_text())
        assert document.get("viewBox") == "0 0 1280 720"
        assert document.find(f"{ns}title") is not None
        assert document.find(f"{ns}desc") is not None
        assert not document.findall(f".//{ns}script")
        assert not document.findall(f".//{ns}image")
        assert scan_file(str(card.relative_to(ROOT)), root=ROOT) == []
    assert "Barath Ganesh" in (SOURCES / "credit.svg").read_text()
