"""Publication evidence stays inside the repository and must say something.

Finding 14 had two halves. Evidence resolved with a bare `exists()` check, so a
traversing path cleared a claim from outside the project; and the shipped owner
items had no evidence path at all, so a real recorded approval could never pass
one. Both halves are asserted here.

An owner approval is additionally bound to the artifact set it approves (repair
wave 3, finding 6), so the helpers below write a real artifact and bind a real
manifest hash to it. The content-binding rules themselves are asserted in
`test_approval_binding.py`.

Nothing in this file publishes, uploads, submits, or sends anything. It writes
records into a temporary directory and reads verdicts back.
"""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from publishing import PublicationFault, PublicationReason
from publishing.checklist import (
    OWNER_APPROVAL_RECORDS,
    OWNER_ITEM_IDS,
    SUBMISSION_CHECKLIST,
    owner_record,
    resolve_item,
)
from publishing.claims import Claim, audit_claim
from publishing.evidence import (
    MANIFEST_KIND_FOR,
    ApprovalBinding,
    ArtifactEntry,
    ArtifactManifest,
    EvidenceRecordBody,
    EvidenceReference,
    EvidenceRoot,
    ManifestKind,
    record_document,
)

ESCAPES = (
    "../../../../etc/hosts",
    "../outside.md",
    "docs/../../outside.md",
    "/etc/hosts",
    " docs/evidence/run.md",
)
ARTIFACT = "docs/public/overview.md"
ARTIFACT_BODY = "The published overview, offline and synthetic.\n"
SUBMISSION_PAYLOAD = "the submission payload this entry declares\n"


def _write(root: Path, relative: str, body: str = "offline evidence record\n") -> str:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return relative


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def binding(
    root: Path, kind: ManifestKind, *, body: str = ARTIFACT_BODY
) -> ApprovalBinding:
    """A real artifact on disk, bound by its real manifest hash."""
    _write(root, ARTIFACT, body)
    manifest = ArtifactManifest(
        kind=kind, entries=(ArtifactEntry(path=ARTIFACT, content_hash=_digest(body)),)
    )
    extra: dict[str, object] = {}
    if kind == "submission":
        extra = {
            "submission_payload_hash": _digest(SUBMISSION_PAYLOAD),
            "required_links": ("repository", "article", "video"),
        }
    return ApprovalBinding(
        manifest_kind=kind,
        artifact_manifest_hash=manifest.manifest_hash,
        artifacts=manifest.entries,
        **extra,  # type: ignore[arg-type]
    )


def _record(
    root: Path,
    relative: str,
    *,
    record_type: str = "claim-evidence",
    subject_id: str = "C-1",
    decision: str = "recorded",
    approval: ApprovalBinding | None = None,
) -> str:
    kind = MANIFEST_KIND_FOR.get(record_type)
    if kind is not None and approval is None:
        approval = binding(root, kind)
    body = EvidenceRecordBody(
        record_type=record_type,  # type: ignore[arg-type]
        subject_id=subject_id,
        decision=decision,  # type: ignore[arg-type]
        recorded_at=datetime.now(UTC),
        record_version="rec-1",
        detail="Recorded offline for this test.",
        approval=approval,
    )
    return _write(root, relative, record_document(body) + "\n")


@pytest.mark.parametrize("declared", ESCAPES)
def test_no_declared_path_escapes_the_evidence_root_offline(
    tmp_path: Path, declared: str
) -> None:
    boundary = EvidenceRoot(root=tmp_path)
    with pytest.raises(PublicationFault) as raised:
        boundary.resolve(declared)
    assert raised.value.reason is PublicationReason.EVIDENCE_PATH_REJECTED


@pytest.mark.parametrize("declared", ESCAPES)
def test_a_traversing_claim_is_never_supported_offline(
    tmp_path: Path, declared: str
) -> None:
    verdict = audit_claim(
        Claim(
            claim_id="C-escape",
            text="A capability this project has.",
            category="capability",
            evidence_paths=(declared,),
        ),
        tmp_path,
    )
    assert verdict.supported is False
    assert verdict.reason is PublicationReason.EVIDENCE_PATH_REJECTED
    assert verdict.resolved == ()


def test_a_symlink_into_the_root_is_refused_offline(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-evidence.md"
    outside.write_text("not this project's evidence\n", encoding="utf-8")
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    os.symlink(outside, root / "docs" / "linked.md")
    boundary = EvidenceRoot(root=root)
    with pytest.raises(PublicationFault) as raised:
        boundary.resolve("docs/linked.md")
    assert raised.value.reason is PublicationReason.EVIDENCE_SYMLINK

    verdict = audit_claim(
        Claim(
            claim_id="C-link",
            text="A capability this project has.",
            category="capability",
            evidence_paths=("docs/linked.md",),
        ),
        root,
    )
    assert verdict.supported is False
    assert verdict.reason is PublicationReason.EVIDENCE_SYMLINK


def test_a_symlinked_parent_directory_is_refused_offline(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "record.md").write_text("elsewhere\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    os.symlink(elsewhere, root / "docs")
    with pytest.raises(PublicationFault) as raised:
        EvidenceRoot(root=root).resolve("docs/record.md")
    assert raised.value.reason is PublicationReason.EVIDENCE_SYMLINK


def test_a_directory_is_not_evidence_offline(tmp_path: Path) -> None:
    (tmp_path / "docs" / "evidence").mkdir(parents=True)
    with pytest.raises(PublicationFault) as raised:
        EvidenceRoot(root=tmp_path).resolve("docs/evidence")
    assert raised.value.reason is PublicationReason.EVIDENCE_NOT_A_FILE


def test_an_absent_file_keeps_its_own_reason_offline(tmp_path: Path) -> None:
    with pytest.raises(PublicationFault) as raised:
        EvidenceRoot(root=tmp_path).resolve("docs/absent.md")
    assert raised.value.reason is PublicationReason.EVIDENCE_UNRESOLVED


def test_a_contained_regular_file_resolves_with_its_hash_offline(
    tmp_path: Path,
) -> None:
    relative = _write(tmp_path, "docs/evidence/run.md")
    resolved = EvidenceRoot(root=tmp_path).resolve(relative)
    assert resolved.relative == relative
    assert resolved.bytes_read > 0
    assert len(resolved.content_hash) == 64


def test_a_guarded_claim_needs_a_typed_record_not_a_file_offline(
    tmp_path: Path,
) -> None:
    """Existence alone never proves a guarded claim."""
    relative = _write(tmp_path, "docs/evidence/latency.json")
    verdict = audit_claim(
        Claim(
            claim_id="C-guarded",
            text="The pattern lowers latency.",
            category="latency_benefit",
            evidence_paths=(relative,),
        ),
        tmp_path,
    )
    assert verdict.supported is False
    assert verdict.reason is PublicationReason.CLAIM_FORBIDDEN
    assert "typed evidence record" in verdict.detail


def test_a_guarded_claim_passes_on_its_recorded_typed_evidence_offline(
    tmp_path: Path,
) -> None:
    relative = _record(
        tmp_path,
        "docs/evidence/latency.json",
        subject_id="C-guarded",
    )
    reference = EvidenceReference(
        path=relative,
        record_type="claim-evidence",
        subject_id="C-guarded",
        decision="recorded",
        content_hash=EvidenceRoot(root=tmp_path).resolve(relative).content_hash,
    )
    verdict = audit_claim(
        Claim(
            claim_id="C-guarded",
            text="The measured figure is recorded in its evidence.",
            category="measurement",
            evidence_records=(reference,),
        ),
        tmp_path,
    )
    assert verdict.supported is True
    assert verdict.resolved == (relative,)


def test_a_record_bound_to_other_content_is_refused_offline(tmp_path: Path) -> None:
    relative = _record(tmp_path, "docs/evidence/bound.json", subject_id="C-bound")
    reference = EvidenceReference(
        path=relative,
        record_type="claim-evidence",
        subject_id="C-bound",
        content_hash="0" * 64,
    )
    verdict = audit_claim(
        Claim(
            claim_id="C-bound",
            text="A measured figure.",
            category="measurement",
            evidence_records=(reference,),
        ),
        tmp_path,
    )
    assert verdict.supported is False
    assert verdict.reason is PublicationReason.EVIDENCE_HASH_MISMATCH


def test_a_record_for_another_subject_is_refused_offline(tmp_path: Path) -> None:
    relative = _record(tmp_path, "docs/evidence/other.json", subject_id="C-other")
    with pytest.raises(PublicationFault) as raised:
        EvidenceRoot(root=tmp_path).read_record(
            EvidenceReference(
                path=relative,
                record_type="claim-evidence",
                subject_id="C-mine",
            )
        )
    assert raised.value.reason is PublicationReason.EVIDENCE_SUBJECT_MISMATCH


def test_an_untyped_document_is_not_a_record_offline(tmp_path: Path) -> None:
    relative = _write(tmp_path, "docs/evidence/prose.json", "just some prose\n")
    with pytest.raises(PublicationFault) as raised:
        EvidenceRoot(root=tmp_path).read_record(
            EvidenceReference(
                path=relative, record_type="claim-evidence", subject_id="C-1"
            )
        )
    assert raised.value.reason is PublicationReason.EVIDENCE_SCHEMA_INVALID


def test_a_record_without_its_moment_or_version_is_refused_offline(
    tmp_path: Path,
) -> None:
    for missing in ("recorded_at", "record_version"):
        body = {
            "record_type": "claim-evidence",
            "subject_id": "C-1",
            "decision": "recorded",
            "recorded_at": datetime.now(UTC).isoformat(),
            "record_version": "rec-1",
        }
        body.pop(missing)
        relative = _write(
            tmp_path, f"docs/evidence/no-{missing}.json", json.dumps(body)
        )
        with pytest.raises(PublicationFault) as raised:
            EvidenceRoot(root=tmp_path).read_record(
                EvidenceReference(
                    path=relative, record_type="claim-evidence", subject_id="C-1"
                )
            )
        assert raised.value.reason is PublicationReason.EVIDENCE_SCHEMA_INVALID, missing


def test_a_record_must_be_recorded_in_utc_offline(tmp_path: Path) -> None:
    relative = _write(
        tmp_path,
        "docs/evidence/local-time.json",
        json.dumps(
            {
                "record_type": "claim-evidence",
                "subject_id": "C-1",
                "decision": "recorded",
                "recorded_at": "2026-10-19T12:00:00+02:00",
                "record_version": "rec-1",
            }
        ),
    )
    with pytest.raises(PublicationFault) as raised:
        EvidenceRoot(root=tmp_path).read_record(
            EvidenceReference(
                path=relative, record_type="claim-evidence", subject_id="C-1"
            )
        )
    assert raised.value.reason is PublicationReason.EVIDENCE_SCHEMA_INVALID


def test_every_shipped_owner_item_names_a_consumable_record_offline() -> None:
    for item_id in OWNER_ITEM_IDS:
        item = SUBMISSION_CHECKLIST.item(item_id)
        assert item.evidence_records, item_id
        reference = item.evidence_records[0]
        assert reference.path == OWNER_APPROVAL_RECORDS[item_id]
        assert reference.subject_id == item_id
        assert reference.decision == "approved"
        assert reference.record_type == item_id


def test_a_recorded_owner_approval_passes_its_own_item_offline(
    tmp_path: Path,
) -> None:
    """The half of finding 14 where a legitimate approval had no path to pass."""
    publish = SUBMISSION_CHECKLIST.item("publish-approval")
    approval = binding(tmp_path, "publication")
    relative = _record(
        tmp_path,
        OWNER_APPROVAL_RECORDS["publish-approval"],
        record_type="publish-approval",
        subject_id="publish-approval",
        decision="approved",
        approval=approval,
    )
    # The shipped item names the awaited record and binds no content, so it
    # cannot pass on the file's existence alone (repair wave 3, finding 6).
    awaited = resolve_item(publish, tmp_path, frozenset())
    assert awaited.status == "pending"
    assert awaited.reason is PublicationReason.OWNER_APPROVAL_REQUIRED

    bound = publish.model_copy(
        update={
            "evidence_records": (
                owner_record(
                    "publish-approval",
                    content_hash=EvidenceRoot(root=tmp_path)
                    .resolve(relative)
                    .content_hash,
                    artifact_manifest_hash=approval.artifact_manifest_hash,
                ),
            )
        }
    )
    state = resolve_item(bound, tmp_path, frozenset())
    assert state.status == "passed"
    assert state.reason is None

    # Approving publication is still not approving submission.
    submit = SUBMISSION_CHECKLIST.item("submit-approval")
    pending = resolve_item(submit, tmp_path, frozenset())
    assert pending.status == "pending"
    assert pending.reason is PublicationReason.OWNER_APPROVAL_REQUIRED


def test_a_declined_owner_decision_never_passes_its_item_offline(
    tmp_path: Path,
) -> None:
    _record(
        tmp_path,
        OWNER_APPROVAL_RECORDS["submit-approval"],
        record_type="submit-approval",
        subject_id="submit-approval",
        decision="declined",
    )
    state = resolve_item(
        SUBMISSION_CHECKLIST.item("submit-approval"), tmp_path, frozenset()
    )
    assert state.status == "pending"
    assert state.reason is PublicationReason.EVIDENCE_DECISION_MISMATCH


def test_a_publish_record_cannot_satisfy_the_submit_item_offline(
    tmp_path: Path,
) -> None:
    _record(
        tmp_path,
        OWNER_APPROVAL_RECORDS["submit-approval"],
        record_type="publish-approval",
        subject_id="submit-approval",
        decision="approved",
    )
    state = resolve_item(
        SUBMISSION_CHECKLIST.item("submit-approval"), tmp_path, frozenset()
    )
    assert state.status == "pending"
    assert state.reason is PublicationReason.EVIDENCE_SCHEMA_INVALID


def test_an_owner_record_outside_the_repository_is_refused_offline(
    tmp_path: Path,
) -> None:
    from publishing.checklist import ChecklistItem

    item = ChecklistItem(
        item_id="publish-approval",
        title="The owner approved publishing.",
        requirement="REQ-PUB-08",
        verification="The owner recorded a publishing decision.",
        evidence_records=(
            EvidenceReference(
                path="../publish-approval.json",
                record_type="publish-approval",
                subject_id="publish-approval",
                decision="approved",
            ),
        ),
        requires_owner=True,
    )
    _record(
        tmp_path.parent,
        "publish-approval.json",
        record_type="publish-approval",
        subject_id="publish-approval",
        decision="approved",
    )
    state = resolve_item(item, tmp_path, frozenset())
    assert state.status == "pending"
    assert state.reason is PublicationReason.EVIDENCE_PATH_REJECTED


def test_resolving_evidence_sends_nothing_offline(tmp_path: Path) -> None:
    """The module reads files. It has no transport of any kind."""
    import inspect

    from publishing import checklist, claims, evidence

    for module in (evidence, claims, checklist):
        source = inspect.getsource(module)
        for forbidden in (
            "urllib",
            "requests",
            "boto3",
            "socket",
            "subprocess",
            "smtplib",
            "http.client",
        ):
            assert forbidden not in source, f"{module.__name__}: {forbidden}"
