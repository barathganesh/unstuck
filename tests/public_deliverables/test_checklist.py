"""The submission checklist is Pending by construction. Offline.

Nothing here publishes or submits. The checklist's value is that it refuses to
report readiness while a named verification has not been performed, and that
publishing and submitting remain two separate owner decisions.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from publishing import PublicationFault, PublicationReason
from publishing.checklist import (
    OWNER_ITEM_IDS,
    REQUIRED_ITEM_IDS,
    SUBMISSION_CHECKLIST,
    ChecklistItem,
    resolve_item,
)


def test_every_required_item_is_present_offline() -> None:
    ids = tuple(item.item_id for item in SUBMISSION_CHECKLIST.items)
    assert set(ids) == REQUIRED_ITEM_IDS
    assert len(ids) == len(set(ids)), "no item id appears twice"
    for item in SUBMISSION_CHECKLIST.items:
        assert item.verification.strip(), item.item_id
        assert item.requirement.strip(), item.item_id


def test_every_shipped_item_is_pending_offline(tmp_path: Path) -> None:
    report = SUBMISSION_CHECKLIST.resolve(tmp_path, frozenset())
    assert report.ready is False
    assert len(report.pending) == len(SUBMISSION_CHECKLIST.items)
    for state in report.states:
        assert state.status == "pending", state.item_id
        assert state.reason is not None


def test_the_report_refuses_to_declare_readiness_offline(tmp_path: Path) -> None:
    report = SUBMISSION_CHECKLIST.resolve(tmp_path, frozenset())
    with pytest.raises(PublicationFault) as raised:
        report.require_ready()
    assert raised.value.reason is PublicationReason.CHECKLIST_INCOMPLETE
    assert "eligibility" in raised.value.detail


def test_a_named_verification_item_cannot_pass_without_it_offline(
    tmp_path: Path,
) -> None:
    named = ("licence", "video-host", "eligibility", "one-entry-rule", "deadline")
    for item_id in named:
        item = SUBMISSION_CHECKLIST.item(item_id)
        state = resolve_item(item, tmp_path, frozenset(REQUIRED_ITEM_IDS))
        assert state.status == "pending", item_id
        assert state.reason is PublicationReason.VERIFICATION_MISSING, item_id
        assert item.evidence_paths == (), item_id


def test_an_item_passes_once_its_recorded_verification_exists_offline(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "verified.md").write_text("offline record\n", encoding="utf-8")
    item = ChecklistItem(
        item_id="required-links",
        title="Required links resolve.",
        requirement="REQ-PUB-09",
        verification="Each submission link was opened and resolved.",
        evidence_paths=("docs/verified.md",),
    )
    assert resolve_item(item, tmp_path, frozenset()).status == "passed"

    absent = ChecklistItem(
        item_id="required-links",
        title="Required links resolve.",
        requirement="REQ-PUB-09",
        verification="Each submission link was opened and resolved.",
        evidence_paths=("docs/verified.md", "docs/absent.md"),
    )
    state = resolve_item(absent, tmp_path, frozenset())
    assert state.status == "pending"
    assert state.reason is PublicationReason.EVIDENCE_MISSING
    assert "docs/absent.md" in state.detail


def test_publishing_and_submitting_are_two_owner_items_offline(
    tmp_path: Path,
) -> None:
    assert OWNER_ITEM_IDS == frozenset({"publish-approval", "submit-approval"})
    for item_id in OWNER_ITEM_IDS:
        assert SUBMISSION_CHECKLIST.item(item_id).requires_owner is True

    publish = SUBMISSION_CHECKLIST.item("publish-approval")
    submit = SUBMISSION_CHECKLIST.item("submit-approval")
    assert publish.item_id != submit.item_id
    assert publish.verification != submit.verification


def test_owner_approval_alone_does_not_pass_an_owner_item_offline(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "approved.md").write_text("offline record\n", encoding="utf-8")
    item = ChecklistItem(
        item_id="publish-approval",
        title="The owner approved publishing.",
        requirement="REQ-PUB-08",
        verification="The owner recorded a publishing decision.",
        evidence_paths=("docs/approved.md",),
        requires_owner=True,
    )
    without = resolve_item(item, tmp_path, frozenset())
    assert without.status == "pending"
    assert without.reason is PublicationReason.OWNER_APPROVAL_REQUIRED

    other = resolve_item(item, tmp_path, frozenset({"submit-approval"}))
    assert other.status == "pending", "approving submission is not approving publishing"
    assert other.reason is PublicationReason.OWNER_APPROVAL_REQUIRED

    # An in-memory approval set is not a recorded decision, however it is
    # spelled: the owner item needs the typed record it names.
    named = resolve_item(item, tmp_path, frozenset({"publish-approval"}))
    assert named.status == "pending"
    assert named.reason is PublicationReason.OWNER_APPROVAL_REQUIRED
    assert "not a decision" in named.detail


def test_an_owner_item_without_its_record_stays_pending_offline(
    tmp_path: Path,
) -> None:
    item = ChecklistItem(
        item_id="submit-approval",
        title="The owner approved submitting.",
        requirement="REQ-PUB-08",
        verification="The owner recorded a submission decision.",
        requires_owner=True,
    )
    state = resolve_item(item, tmp_path, frozenset({"submit-approval"}))
    assert state.status == "pending"
    assert state.reason is PublicationReason.VERIFICATION_MISSING


def test_an_unknown_item_is_refused_rather_than_invented_offline() -> None:
    with pytest.raises(PublicationFault) as raised:
        SUBMISSION_CHECKLIST.item("not-an-item")
    assert raised.value.reason is PublicationReason.CHECKLIST_INCOMPLETE


def test_an_item_cannot_ship_without_a_named_verification_offline() -> None:
    with pytest.raises(ValidationError):
        ChecklistItem(
            item_id="deadline",
            title="The deadline holds.",
            requirement="REQ-PUB-09",
            verification="",
        )


def test_a_state_cannot_record_a_pass_with_a_reason_offline() -> None:
    from publishing.checklist import ItemState

    with pytest.raises(ValidationError):
        ItemState(
            item_id="deadline",
            status="passed",
            reason=PublicationReason.VERIFICATION_MISSING,
        )
    with pytest.raises(ValidationError):
        ItemState(item_id="deadline", status="pending")


def test_a_duplicate_item_id_is_refused_offline() -> None:
    from publishing.checklist import Checklist

    item = ChecklistItem(
        item_id="deadline",
        title="The deadline holds.",
        requirement="REQ-PUB-09",
        verification="The owner read the published deadline.",
    )
    with pytest.raises(ValidationError):
        Checklist(items=(item, item))


def test_the_checklist_covers_the_recheck_items_requirements_name_offline() -> None:
    requirements = {
        item.item_id: item.requirement for item in SUBMISSION_CHECKLIST.items
    }
    assert requirements["eligibility"] == "REQ-PUB-09"
    assert requirements["one-entry-rule"] == "REQ-PUB-09"
    assert requirements["deadline"] == "REQ-PUB-09"
    assert requirements["required-links"] == "REQ-PUB-09"
    assert requirements["licence"] == "REQ-PUB-01"
    assert requirements["video-host"] == "REQ-PUB-04"
    assert requirements["claims-audit"] == "REQ-PUB-06"
    assert requirements["synthetic-labels"] == "REQ-PUB-10"
    assert requirements["publish-approval"] == "REQ-PUB-08"
    assert requirements["submit-approval"] == "REQ-PUB-08"
