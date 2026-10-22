"""The leakage scan's own coverage, including the pitch deck. Offline.

Two things are asserted here that the scan cannot assert about itself: that the
pitch deck's shipping files are in the public set at all, and that the two
non-text pitch artifacts are read rather than skipped. A file outside the set
is not a clean file, it is an unchecked one.

Nothing here publishes, uploads or sends anything.
"""

import re
import shutil
import zipfile
from pathlib import Path

import pytest

from publishing.public_scan import (
    CHECKS,
    OCR_CHECKS,
    OCR_EXCLUDED_CHECKS,
    PITCH_FRAMES,
    PITCH_PPTX,
    PUBLIC_FILES,
    ocr_available,
    pptx_inspectable_text,
    scan,
    scan_pptx,
    scan_text,
    verify_controls,
)

ROOT = Path(__file__).resolve().parents[2]
PITCH = ROOT / "submission" / "pitch"

#: Every text file the pitch directory ships. `build/node_modules/` and
#: `build/package-lock.json` are third-party install state and never ship.
PITCH_TEXT_FILES = (
    "submission/pitch/README.md",
    "submission/pitch/unstuck-pitch.html",
    "submission/pitch/build/capture.mjs",
    "submission/pitch/build/make-pptx.mjs",
    "submission/pitch/build/package.json",
)


def test_every_pattern_still_catches_its_own_example() -> None:
    broken = [result.check for result in verify_controls() if not result.ok]
    assert broken == []


@pytest.mark.parametrize("relative_path", PITCH_TEXT_FILES)
def test_each_pitch_text_file_is_in_the_public_set(relative_path: str) -> None:
    assert relative_path in PUBLIC_FILES
    assert (ROOT / relative_path).is_file()


def test_the_pitch_directory_ships_no_text_file_outside_the_public_set() -> None:
    """A new shipping file must be declared, not silently unchecked."""
    declared = set(PUBLIC_FILES)
    suffixes = {".md", ".html", ".mjs", ".js", ".json", ".css", ".txt"}
    found = {
        str(path.relative_to(ROOT))
        for path in PITCH.rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and "node_modules" not in path.parts
        and path.name != "package-lock.json"
    }
    assert found - declared == set()


def test_no_office_lock_file_or_qa_scratch_ships() -> None:
    assert list(PITCH.glob("~$*")) == []
    assert not (PITCH / "build" / "qa").exists()


def test_the_powerpoint_file_and_every_frame_are_declared() -> None:
    assert (ROOT / PITCH_PPTX).is_file()
    assert len(PITCH_FRAMES) == 8
    for relative_path in PITCH_FRAMES:
        assert (ROOT / relative_path).is_file()


def test_the_powerpoint_text_parts_carry_the_notes_and_the_alt_text() -> None:
    parts = dict(pptx_inspectable_text(ROOT / PITCH_PPTX))
    notes = [name for name in parts if name.startswith("ppt/notesSlides/")]
    assert len(notes) == 8
    everything = "\n".join(parts.values())
    # The alt text travels in the slide XML, the note in the notes slide.
    assert "A new call can repeat a decision already proven useless" in everything
    assert "three of three unresolved" in everything


def test_the_powerpoint_file_carries_no_authoring_instant() -> None:
    """The writer stamps the build moment; the build strips it back out."""
    with zipfile.ZipFile(ROOT / PITCH_PPTX) as bundle:
        core = bundle.read("docProps/core.xml").decode("utf-8")
    assert "dcterms:created" not in core
    assert "dcterms:modified" not in core


def test_slide_two_shows_the_three_control_calls_it_counts() -> None:
    """Slide two counts the memoryless control, so it shows every call it counts."""
    deck = (PITCH / "unstuck-pitch.html").read_text(encoding="utf-8")
    slide_two = deck.split('id="s2"')[1].split('id="s3"')[0]
    assert (
        len(re.findall(r'<th scope="row" class="name">Control \d</th>', slide_two)) == 3
    )
    assert slide_two.count(">inspect_pipeline<") == 3
    assert "3 of 3" in slide_two and "0 of 3 completed" in slide_two
    assert "Four options offered to D1" in slide_two
    # A count the speaker notes say aloud is a count some slide shows.
    notes = deck.split('id="speaker-notes">')[1].split("</script>")[0].lower()
    slides = re.sub(r"<[^>]+>", "", deck.split('id="speaker-notes">')[0])
    spoken = {
        "six of six": "6 of 6",
        "three of three": "3 of 3",
        "zero of three": "0 of 3",
        "one of one": "1 of 1",
        "thirteen of thirteen": "13 of 13",
        "twenty of twenty": "20 of 20",
    }
    for words, digits in spoken.items():
        if words in notes:
            assert digits in slides, f"the notes say {words!r}, no slide shows it"


def test_the_declared_public_files_and_the_powerpoint_file_are_clean() -> None:
    assert scan() == []
    assert scan_pptx() == []


def test_the_frame_check_drops_only_the_em_dash_pattern() -> None:
    """Recognition cannot tell a hyphen from a long dash; nothing else is waived."""
    assert OCR_EXCLUDED_CHECKS == frozenset({"em-dash"})
    assert {check.name for check in CHECKS} - {check.name for check in OCR_CHECKS} == {
        "em-dash"
    }


def test_the_frame_check_reports_whether_a_recognizer_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence is reported, never reported as a pass."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert ocr_available() is False
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/tesseract")
    assert ocr_available() is True


def test_extracted_text_is_scanned_line_by_line() -> None:
    findings = scan_text("synthetic", "clean line\naccount 123456789012 owns it")
    assert [(finding.check, finding.line_number) for finding in findings] == [
        ("aws-account-id", 2)
    ]
