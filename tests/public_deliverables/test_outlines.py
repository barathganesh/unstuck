"""Outlines carry an evidence-checked brief and refuse arbitrary findings."""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from publishing import PublicationFault, PublicationReason
from publishing.outlines import (
    ARTICLE_OUTLINE,
    MEASURED_STORY,
    OUTLINES,
    SUBMISSION_TAGS,
    VIDEO_OUTLINE,
    VIDEO_SECOND_BOUND,
    VIDEO_SECOND_TARGET,
    EvidenceSlot,
    Outline,
    Section,
    scan_prose,
)


def prose_of(outline: Outline) -> list[tuple[str, str]]:
    """Every prose string in an outline, with where it came from."""
    strings = [(f"{outline.outline_id} title", outline.working_title)]
    for section in outline.sections:
        strings.append((f"{section.section_id} title", section.title))
        strings.append((f"{section.section_id} purpose", section.purpose))
        for slot in section.slots:
            strings.append((f"{slot.slot_id} needs", slot.needs))
    return strings


@pytest.mark.parametrize("outline", OUTLINES, ids=lambda item: item.outline_id)
def test_no_prose_carries_a_claim_marker_offline(outline: Outline) -> None:
    for where, text in prose_of(outline):
        assert scan_prose(text) == (), f"{where}: {scan_prose(text)}"


@pytest.mark.parametrize("outline", OUTLINES, ids=lambda item: item.outline_id)
def test_every_slot_is_awaiting_evidence_offline(outline: Outline) -> None:
    assert outline.open_slots, outline.outline_id
    for slot in outline.open_slots:
        assert slot.status == "awaiting_evidence"
        assert slot.gate.strip()
        assert "Pending" in slot.gate or "Pending" in slot.gate.capitalize()


@pytest.mark.parametrize("outline", OUTLINES, ids=lambda item: item.outline_id)
def test_every_section_states_its_purpose_offline(outline: Outline) -> None:
    for section in outline.sections:
        assert len(section.purpose) > 40, section.section_id


def test_the_video_outline_holds_its_bound_and_target_offline() -> None:
    assert VIDEO_SECOND_BOUND == 180
    assert VIDEO_SECOND_TARGET == 175
    assert VIDEO_OUTLINE.total_seconds <= VIDEO_SECOND_BOUND
    assert VIDEO_OUTLINE.total_seconds == VIDEO_SECOND_TARGET
    for section in VIDEO_OUTLINE.sections:
        assert section.seconds is not None, section.section_id


def test_the_video_outline_carries_the_two_named_scenes_offline() -> None:
    ids = [section.section_id for section in VIDEO_OUTLINE.sections]
    assert "scene-one" in ids
    assert "scene-two" in ids
    first = VIDEO_OUTLINE.sections[ids.index("scene-one")]
    second = VIDEO_OUTLINE.sections[ids.index("scene-two")]
    assert "D1" in first.purpose and "fresh" in first.purpose.lower()
    assert "no-progress" in second.purpose and "before selection" in second.purpose
    assert "fresh choice" in second.purpose and "No D2 occurs" in second.purpose
    assert ids.index("scene-one") < ids.index("scene-two")


def test_a_video_outline_over_its_bound_is_refused_offline() -> None:
    long_section = Section(
        section_id="too-long",
        title="A scene that runs past the bound",
        purpose="A section whose duration alone exceeds what the rules permit.",
        seconds=VIDEO_SECOND_BOUND,
    )
    with pytest.raises(ValidationError):
        Outline(
            outline_id="over-bound",
            medium="video",
            working_title="Over the bound",
            requirements=("REQ-PUB-04",),
            sections=(
                long_section,
                long_section.model_copy(
                    update={"section_id": "one-more", "seconds": 1}
                ),
            ),
        )


def test_a_duplicate_section_id_is_refused_offline() -> None:
    section = Section(
        section_id="twice",
        title="A section",
        purpose="A section that appears more than once in the same outline.",
    )
    with pytest.raises(ValidationError):
        Outline(
            outline_id="duplicated",
            medium="article",
            working_title="An outline with a repeated section",
            requirements=("REQ-PUB-03",),
            sections=(section, section),
        )


def test_an_article_section_carries_no_duration_offline() -> None:
    with pytest.raises(ValidationError):
        Outline(
            outline_id="timed-article",
            medium="article",
            working_title="An article with a stopwatch",
            requirements=("REQ-PUB-03",),
            sections=(
                Section(
                    section_id="only",
                    title="A section",
                    purpose="A section that should not carry a duration at all.",
                    seconds=30,
                ),
            ),
        )


def test_a_video_section_without_its_duration_is_refused_offline() -> None:
    with pytest.raises(ValidationError):
        Outline(
            outline_id="untimed-video",
            medium="video",
            working_title="A video without timings",
            requirements=("REQ-PUB-04",),
            sections=(
                Section(
                    section_id="only",
                    title="A scene",
                    purpose="A scene that should carry a duration and does not.",
                ),
            ),
        )


@pytest.mark.parametrize(
    "prose",
    [
        "The selector achieves a higher verified success rate.",
        "Results show the pattern prevents repeated mistakes.",
        "This is the fastest selector we tried.",
        "A novel approach to tool selection.",
        "Latency improved across every arm.",
        "Success reached 92 percent on the held-out split.",
        "Cost fell by half.",
    ],
)
def test_a_claim_in_a_section_is_refused_offline(prose: str) -> None:
    assert scan_prose(prose) != (), prose
    with pytest.raises(PublicationFault) as raised:
        Section(section_id="claimy", title="A section", purpose=prose)
    assert raised.value.reason is PublicationReason.OUTLINE_CLAIM_PRESENT


def test_a_claim_in_a_slot_is_refused_offline() -> None:
    with pytest.raises(PublicationFault) as raised:
        EvidenceSlot(
            slot_id="claimy",
            needs="Evidence that the pattern significantly reduced latency.",
            gate="Pending.",
        )
    assert raised.value.reason is PublicationReason.OUTLINE_CLAIM_PRESENT


def test_an_identifier_token_is_not_a_quantitative_claim_offline() -> None:
    allowed = (
        "REQ-PUB-04 names two scenes.",
        "Pending PF1, PF2, PF6, PF7 and PF9.",
        "OD-02 is authoritative, and NFR8.2 governs the claim.",
        "Decider D1 decides, and D2 re-decides once.",
        "Decider 2B is the only model selector.",
        "FR12.7 and US4.1 with AC4.1.2.",
    )
    for prose in allowed:
        assert scan_prose(prose) == (), prose


@pytest.mark.parametrize("outline", OUTLINES, ids=lambda item: item.outline_id)
def test_the_rendered_outline_states_that_it_is_an_outline_offline(
    outline: Outline,
) -> None:
    rendered = outline.render()
    assert "outline only" in rendered
    assert "measured brief is recorded evidence" in rendered
    for slot in outline.open_slots:
        assert slot.needs in rendered
        assert slot.gate in rendered


def test_the_rendered_articles_digits_are_structural_only_offline() -> None:
    rendered = ARTICLE_OUTLINE.render().replace(MEASURED_STORY, "")
    assert scan_prose(rendered) == ()


def test_the_rendered_videos_digits_are_durations_only_offline() -> None:
    rendered = VIDEO_OUTLINE.render()
    stripped = re.sub(r"\b\d+ s(?:econds)?\b", "", rendered.replace(MEASURED_STORY, ""))
    assert scan_prose(stripped) == (), scan_prose(stripped)
    assert f"{VIDEO_SECOND_TARGET} seconds" in rendered
    assert f"{VIDEO_SECOND_BOUND} seconds" in rendered


def test_the_submission_tags_are_exactly_the_two_required_offline() -> None:
    assert SUBMISSION_TAGS == ("#strands-decider-2b", "#tool-selection")


def test_the_article_covers_every_topic_its_requirement_names_offline() -> None:
    ids = {section.section_id for section in ARTICLE_OUTLINE.sections}
    assert {
        "problem",
        "causality",
        "receipts",
        "fresh-choice",
        "blocking",
        "arms",
        "findings",
        "limitations",
        "prior-art",
    } <= ids
    rendered = ARTICLE_OUTLINE.render().lower()
    assert "why the same model selector was kept" in rendered
    assert "no automatic gain from memory" in rendered, "REQ-PUB-03 forbids that claim"


def test_the_findings_section_is_empty_by_construction_offline() -> None:
    findings = next(
        section
        for section in ARTICLE_OUTLINE.sections
        if section.section_id == "findings"
    )
    assert "cannot be written yet" in findings.purpose
    assert len(findings.slots) >= 3
    assert scan_prose(findings.purpose) == ()


def test_measured_brief_numbers_are_bound_to_the_curated_record() -> None:
    record = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "dist/public-demo/loop-escape-proof.json"
        ).read_text()
    )
    client = record["measured_latency"]["agentcore_client_time"]
    first = client["run_1_learning"]["elapsed"]["value"]
    old = client["superseded_run_2_post_d1"]["elapsed"]["value"]
    current = client["optimized_run_2_preselection"]["elapsed"]["value"]
    assert re.findall(r"\b\d+(?:\.\d+)?(?: s|%)", MEASURED_STORY) == [
        f"{first / 1000:.1f} s",
        f"{old / 1000:.1f} s",
        f"{current / 1000:.1f} s",
        f"{(old - current) / 1000:.1f} s",
        f"{100 * (old - current) / old:.1f}%",
    ]
    assert record["scope"]["cases"]["value"] == 1
    assert record["scope"]["runs_of_each_proof"]["value"] == 1
    assert "n = 1, the\nsame synthetic case, each proof once" in MEASURED_STORY
