"""The evidence root is verified, never canonicalized. Offline.

Repair wave 3, finding 5: `EvidenceRoot` validated only that the declared root
was absolute, then used `realpath(root)` as its trust anchor and built every
candidate beneath that canonical target. A root that was itself a link
therefore made its target the trusted root, and a regular file in that target
passed every later containment check even though it was outside the root the
caller declared. The existing tests covered a linked file and a linked child
directory, but not a linked root.

These tests assert the root itself, every component above it, the descriptor
the files are read from, and the kind of file that may be read at all.
"""

import os
from pathlib import Path

import pytest

from publishing import PublicationFault, PublicationReason
from publishing.checklist import ChecklistItem, resolve_item
from publishing.claims import Claim, audit_claim
from publishing.evidence import EvidenceReference, EvidenceRoot


def _write(root: Path, relative: str, body: str = "evidence\n") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def test_a_linked_evidence_root_is_refused_at_construction_offline(
    tmp_path: Path,
) -> None:
    """The confirmed defect: the link's target became the trusted root."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write(elsewhere, "docs/evidence/run.md")
    linked = tmp_path / "repo"
    os.symlink(elsewhere, linked)

    with pytest.raises(PublicationFault) as raised:
        EvidenceRoot(root=linked)
    assert raised.value.reason is PublicationReason.EVIDENCE_ROOT_REJECTED
    assert str(linked) in str(raised.value.detail)

    # The real root over the same files is fine, so the refusal is about the
    # link and not about the content.
    assert EvidenceRoot(root=elsewhere).resolve("docs/evidence/run.md").bytes_read > 0


def test_a_linked_component_above_the_root_is_refused_offline(tmp_path: Path) -> None:
    real = tmp_path / "real" / "repo"
    real.mkdir(parents=True)
    _write(real, "docs/evidence/run.md")
    os.symlink(tmp_path / "real", tmp_path / "linked")
    with pytest.raises(PublicationFault) as raised:
        EvidenceRoot(root=tmp_path / "linked" / "repo")
    assert raised.value.reason is PublicationReason.EVIDENCE_ROOT_REJECTED


def test_a_claim_over_a_linked_root_is_never_supported_offline(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write(elsewhere, "docs/evidence/capability.md")
    linked = tmp_path / "repo"
    os.symlink(elsewhere, linked)
    verdict = audit_claim(
        Claim(
            claim_id="C-root",
            text="A capability this project has.",
            category="capability",
            evidence_paths=("docs/evidence/capability.md",),
        ),
        linked,
    )
    assert verdict.supported is False
    assert verdict.reason is PublicationReason.EVIDENCE_ROOT_REJECTED
    assert verdict.resolved == ()


def test_a_checklist_item_over_a_linked_root_stays_pending_offline(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write(elsewhere, "docs/evidence/run.md")
    linked = tmp_path / "repo"
    os.symlink(elsewhere, linked)
    item = ChecklistItem(
        item_id="eligibility",
        title="Entry eligibility is confirmed.",
        requirement="REQ-PUB-09",
        verification="The owner recorded the eligibility result.",
        evidence_paths=("docs/evidence/run.md",),
    )
    state = resolve_item(item, linked, frozenset())
    assert state.status == "pending"
    assert state.reason is PublicationReason.EVIDENCE_ROOT_REJECTED


def test_a_relative_root_is_still_refused_offline(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        EvidenceRoot(root=Path("docs"))


def test_a_root_replaced_underneath_the_boundary_is_refused_offline(
    tmp_path: Path,
) -> None:
    """The descriptor identity is re-checked on every open."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "docs/evidence/run.md")
    boundary = EvidenceRoot(root=root)
    assert boundary.resolve("docs/evidence/run.md").bytes_read > 0

    replacement = tmp_path / "replacement"
    replacement.mkdir()
    _write(replacement, "docs/evidence/run.md", "someone else's evidence\n")
    root.rename(tmp_path / "moved-away")
    replacement.rename(root)
    with pytest.raises(PublicationFault) as raised:
        boundary.resolve("docs/evidence/run.md")
    assert raised.value.reason is PublicationReason.EVIDENCE_ROOT_REJECTED


def test_a_linked_file_inside_the_root_is_still_refused_offline(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("not this project's evidence\n", encoding="utf-8")
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    os.symlink(outside, root / "docs" / "linked.md")
    with pytest.raises(PublicationFault) as raised:
        EvidenceRoot(root=root).resolve("docs/linked.md")
    assert raised.value.reason is PublicationReason.EVIDENCE_SYMLINK


def test_a_hard_linked_file_is_not_a_single_named_file_offline(
    tmp_path: Path,
) -> None:
    """A second name for the same inode is not the declared evidence."""
    root = tmp_path / "repo"
    root.mkdir()
    target = _write(root, "docs/evidence/run.md")
    os.link(target, root / "docs" / "evidence" / "second-name.md")
    with pytest.raises(PublicationFault) as raised:
        EvidenceRoot(root=root).resolve("docs/evidence/run.md")
    assert raised.value.reason is PublicationReason.EVIDENCE_NOT_A_FILE
    assert "more than one name" in str(raised.value.detail)


def test_a_path_through_a_regular_file_is_refused_offline(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "docs/evidence.md")
    with pytest.raises(PublicationFault) as raised:
        EvidenceRoot(root=root).resolve("docs/evidence.md/run.md")
    assert raised.value.reason in {
        PublicationReason.EVIDENCE_SYMLINK,
        PublicationReason.EVIDENCE_PATH_REJECTED,
    }


def test_a_record_is_read_from_the_descriptor_that_passed_offline(
    tmp_path: Path,
) -> None:
    """The bytes hashed and the bytes parsed are the same read."""
    from test_evidence import _record

    root = tmp_path / "repo"
    root.mkdir()
    relative = _record(root, "docs/evidence/claim.json", subject_id="C-1")
    boundary = EvidenceRoot(root=root)
    resolved = boundary.resolve(relative)
    record = boundary.read_record(
        EvidenceReference(
            path=relative,
            record_type="claim-evidence",
            subject_id="C-1",
            content_hash=resolved.content_hash,
        )
    )
    assert record.subject_id == "C-1"
    assert resolved.relative == relative
