"""An owner approval authorizes one immutable artifact set. Offline.

Repair wave 3, finding 6: `EvidenceReference.content_hash` was optional and the
shipped publish and submit references carried none, while `EvidenceRecordBody`
recorded type, subject, decision, time, version, detail and classification and
no artifact binding at all. Either approval file could therefore be changed
after approval and still pass, and the same approval could authorize a
different repository, article, video or submission payload, because it was
bound only to the checklist item id.

These tests assert the binding end to end: the shipped declaration cannot pass,
a bound one can, publishing and submitting bind separate manifests, and any
change to the approval record, the manifest, or a bound artifact returns the
item to Pending.

Nothing here publishes, submits, or sends anything.
"""

from pathlib import Path

import pytest
from test_evidence import ARTIFACT, ARTIFACT_BODY, _record, _write, binding

from publishing import PublicationFault, PublicationReason
from publishing.checklist import (
    OWNER_APPROVAL_RECORDS,
    OWNER_ITEM_IDS,
    SUBMISSION_CHECKLIST,
    owner_record,
    resolve_item,
)
from publishing.evidence import (
    MANIFEST_KIND_FOR,
    ApprovalBinding,
    ArtifactEntry,
    ArtifactManifest,
    EvidenceRecordBody,
    EvidenceReference,
    EvidenceRoot,
    artifact_manifest,
)


def approved(root: Path, item_id: str) -> tuple[str, ApprovalBinding]:
    """Record a real owner approval over a real artifact set."""
    kind = MANIFEST_KIND_FOR[item_id]
    bound = binding(root, kind)
    relative = _record(
        root,
        OWNER_APPROVAL_RECORDS[item_id],
        record_type=item_id,
        subject_id=item_id,
        decision="approved",
        approval=bound,
    )
    return relative, bound


def bound_item(
    root: Path, item_id: str, bound: ApprovalBinding, relative: str
) -> object:
    reference = owner_record(
        item_id,
        content_hash=EvidenceRoot(root=root).resolve(relative).content_hash,
        artifact_manifest_hash=bound.artifact_manifest_hash,
    )
    return SUBMISSION_CHECKLIST.item(item_id).model_copy(
        update={"evidence_records": (reference,)}
    )


@pytest.mark.parametrize("item_id", sorted(OWNER_ITEM_IDS))
def test_the_shipped_owner_declaration_binds_no_content_offline(item_id: str) -> None:
    reference = SUBMISSION_CHECKLIST.item(item_id).evidence_records[0]
    assert reference.content_hash is None
    assert reference.artifact_manifest_hash is None
    assert reference.bound is False
    assert reference.owner_decision is True


@pytest.mark.parametrize("item_id", sorted(OWNER_ITEM_IDS))
def test_an_unbound_owner_approval_never_passes_its_item_offline(
    tmp_path: Path, item_id: str
) -> None:
    """The confirmed defect: a recorded file alone authorized nothing."""
    approved(tmp_path, item_id)
    state = resolve_item(SUBMISSION_CHECKLIST.item(item_id), tmp_path, frozenset())
    assert state.status == "pending"
    assert state.reason is PublicationReason.OWNER_APPROVAL_REQUIRED
    assert "artifact manifest" in state.detail


@pytest.mark.parametrize("item_id", sorted(OWNER_ITEM_IDS))
def test_a_bound_owner_approval_passes_its_own_item_offline(
    tmp_path: Path, item_id: str
) -> None:
    relative, bound = approved(tmp_path, item_id)
    state = resolve_item(
        bound_item(tmp_path, item_id, bound, relative),  # type: ignore[arg-type]
        tmp_path,
        frozenset(),
    )
    assert state.status == "passed"
    assert state.reason is None


@pytest.mark.parametrize("item_id", sorted(OWNER_ITEM_IDS))
def test_a_changed_artifact_invalidates_the_approval_offline(
    tmp_path: Path, item_id: str
) -> None:
    relative, bound = approved(tmp_path, item_id)
    item = bound_item(tmp_path, item_id, bound, relative)
    assert resolve_item(item, tmp_path, frozenset()).status == "passed"  # type: ignore[arg-type]

    _write(tmp_path, ARTIFACT, ARTIFACT_BODY + "a line added after approval\n")
    state = resolve_item(item, tmp_path, frozenset())  # type: ignore[arg-type]
    assert state.status == "pending"
    assert state.reason is PublicationReason.APPROVAL_ARTIFACT_CHANGED
    assert ARTIFACT in state.detail


@pytest.mark.parametrize("item_id", sorted(OWNER_ITEM_IDS))
def test_a_removed_artifact_invalidates_the_approval_offline(
    tmp_path: Path, item_id: str
) -> None:
    relative, bound = approved(tmp_path, item_id)
    item = bound_item(tmp_path, item_id, bound, relative)
    (tmp_path / ARTIFACT).unlink()
    state = resolve_item(item, tmp_path, frozenset())  # type: ignore[arg-type]
    assert state.status == "pending"
    assert state.reason is PublicationReason.APPROVAL_ARTIFACT_CHANGED


@pytest.mark.parametrize("item_id", sorted(OWNER_ITEM_IDS))
def test_a_changed_approval_record_invalidates_the_approval_offline(
    tmp_path: Path, item_id: str
) -> None:
    relative, bound = approved(tmp_path, item_id)
    item = bound_item(tmp_path, item_id, bound, relative)
    path = tmp_path / relative
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    state = resolve_item(item, tmp_path, frozenset())  # type: ignore[arg-type]
    assert state.status == "pending"
    assert state.reason is PublicationReason.EVIDENCE_HASH_MISMATCH


@pytest.mark.parametrize("item_id", sorted(OWNER_ITEM_IDS))
def test_an_approval_of_another_manifest_is_refused_offline(
    tmp_path: Path, item_id: str
) -> None:
    """Rebinding the item to another manifest hash does not carry over."""
    relative, bound = approved(tmp_path, item_id)
    other = artifact_manifest(
        EvidenceRoot(root=tmp_path),
        MANIFEST_KIND_FOR[item_id],
        (_write(tmp_path, "docs/public/another.md", "another artifact\n"),),
    )
    assert other.manifest_hash != bound.artifact_manifest_hash
    item = SUBMISSION_CHECKLIST.item(item_id).model_copy(
        update={
            "evidence_records": (
                owner_record(
                    item_id,
                    content_hash=EvidenceRoot(root=tmp_path)
                    .resolve(relative)
                    .content_hash,
                    artifact_manifest_hash=other.manifest_hash,
                ),
            )
        }
    )
    state = resolve_item(item, tmp_path, frozenset())
    assert state.status == "pending"
    assert state.reason is PublicationReason.APPROVAL_MANIFEST_MISMATCH


def test_publishing_and_submitting_bind_separate_manifests_offline(
    tmp_path: Path,
) -> None:
    publish_relative, publish_bound = approved(tmp_path, "publish-approval")
    submit_relative, submit_bound = approved(tmp_path, "submit-approval")
    assert publish_bound.manifest_kind == "publication"
    assert submit_bound.manifest_kind == "submission"
    assert publish_bound.artifact_manifest_hash != submit_bound.artifact_manifest_hash
    assert publish_bound.submission_payload_hash is None
    assert submit_bound.submission_payload_hash is not None
    assert submit_bound.required_links

    # Each bound item passes its own, and the publication manifest hash never
    # satisfies the submission item.
    for item_id, bound, relative in (
        ("publish-approval", publish_bound, publish_relative),
        ("submit-approval", submit_bound, submit_relative),
    ):
        state = resolve_item(
            bound_item(tmp_path, item_id, bound, relative),  # type: ignore[arg-type]
            tmp_path,
            frozenset(),
        )
        assert state.status == "passed", item_id

    crossed = SUBMISSION_CHECKLIST.item("submit-approval").model_copy(
        update={
            "evidence_records": (
                owner_record(
                    "submit-approval",
                    content_hash=EvidenceRoot(root=tmp_path)
                    .resolve(submit_relative)
                    .content_hash,
                    artifact_manifest_hash=publish_bound.artifact_manifest_hash,
                ),
            )
        }
    )
    state = resolve_item(crossed, tmp_path, frozenset())
    assert state.status == "pending"
    assert state.reason is PublicationReason.APPROVAL_MANIFEST_MISMATCH


def test_a_half_bound_owner_declaration_is_refused_offline() -> None:
    """A content hash without a manifest hash approves no artifact set."""
    for changes in (
        {"content_hash": "a" * 64},
        {"artifact_manifest_hash": "a" * 64},
    ):
        with pytest.raises(ValueError):
            EvidenceReference(
                path="docs/evidence/publish-approval.json",
                record_type="publish-approval",
                subject_id="publish-approval",
                decision="approved",
                **changes,  # type: ignore[arg-type]
            )


def test_only_an_owner_approval_binds_an_artifact_manifest_offline() -> None:
    with pytest.raises(ValueError):
        EvidenceReference(
            path="docs/evidence/claim.json",
            record_type="claim-evidence",
            subject_id="C-1",
            content_hash="a" * 64,
            artifact_manifest_hash="b" * 64,
        )


def test_an_owner_record_without_a_binding_is_not_a_record_offline() -> None:
    from datetime import UTC, datetime

    for record_type in sorted(MANIFEST_KIND_FOR):
        with pytest.raises(ValueError):
            EvidenceRecordBody(
                record_type=record_type,  # type: ignore[arg-type]
                subject_id=record_type,
                decision="approved",
                recorded_at=datetime.now(UTC),
                record_version="rec-1",
            )


def test_an_approval_cannot_name_a_manifest_it_does_not_carry_offline() -> None:
    entries = (ArtifactEntry(path="docs/public/overview.md", content_hash="a" * 64),)
    manifest = ArtifactManifest(kind="publication", entries=entries)
    with pytest.raises(ValueError):
        ApprovalBinding(
            manifest_kind="publication",
            artifact_manifest_hash="f" * 64,
            artifacts=entries,
        )
    assert (
        ApprovalBinding(
            manifest_kind="publication",
            artifact_manifest_hash=manifest.manifest_hash,
            artifacts=entries,
        ).manifest.manifest_hash
        == manifest.manifest_hash
    )


def test_a_submission_approval_binds_its_payload_and_links_offline() -> None:
    entries = (ArtifactEntry(path="docs/public/overview.md", content_hash="a" * 64),)
    manifest = ArtifactManifest(kind="submission", entries=entries)
    with pytest.raises(ValueError):
        ApprovalBinding(
            manifest_kind="submission",
            artifact_manifest_hash=manifest.manifest_hash,
            artifacts=entries,
        )
    with pytest.raises(ValueError):
        ApprovalBinding(
            manifest_kind="submission",
            artifact_manifest_hash=manifest.manifest_hash,
            artifacts=entries,
            submission_payload_hash="b" * 64,
        )
    bound = ApprovalBinding(
        manifest_kind="submission",
        artifact_manifest_hash=manifest.manifest_hash,
        artifacts=entries,
        submission_payload_hash="b" * 64,
        required_links=("repository", "article", "video"),
    )
    assert bound.required_links == ("repository", "article", "video")


def test_a_publication_approval_binds_no_submission_payload_offline() -> None:
    entries = (ArtifactEntry(path="docs/public/overview.md", content_hash="a" * 64),)
    manifest = ArtifactManifest(kind="publication", entries=entries)
    with pytest.raises(ValueError):
        ApprovalBinding(
            manifest_kind="publication",
            artifact_manifest_hash=manifest.manifest_hash,
            artifacts=entries,
            submission_payload_hash="b" * 64,
        )


def test_a_manifest_hash_is_a_function_of_the_artifact_set_offline() -> None:
    first = ArtifactManifest(
        kind="publication",
        entries=(
            ArtifactEntry(path="docs/a.md", content_hash="a" * 64),
            ArtifactEntry(path="docs/b.md", content_hash="b" * 64),
        ),
    )
    changed = ArtifactManifest(
        kind="publication",
        entries=(
            ArtifactEntry(path="docs/a.md", content_hash="a" * 64),
            ArtifactEntry(path="docs/b.md", content_hash="c" * 64),
        ),
    )
    added = ArtifactManifest(
        kind="publication",
        entries=(ArtifactEntry(path="docs/a.md", content_hash="a" * 64),),
    )
    submission = ArtifactManifest(kind="submission", entries=first.entries)
    hashes = {
        first.manifest_hash,
        changed.manifest_hash,
        added.manifest_hash,
        submission.manifest_hash,
    }
    assert len(hashes) == 4
    with pytest.raises(ValueError):
        ArtifactManifest(
            kind="publication",
            entries=(
                ArtifactEntry(path="docs/b.md", content_hash="b" * 64),
                ArtifactEntry(path="docs/a.md", content_hash="a" * 64),
            ),
        )
    with pytest.raises(ValueError):
        ArtifactManifest(
            kind="publication",
            entries=(
                ArtifactEntry(path="docs/a.md", content_hash="a" * 64),
                ArtifactEntry(path="docs/a.md", content_hash="b" * 64),
            ),
        )


def test_a_manifest_is_hashed_from_the_content_on_disk_offline(tmp_path: Path) -> None:
    _write(tmp_path, "docs/public/one.md", "one\n")
    _write(tmp_path, "docs/public/two.md", "two\n")
    boundary = EvidenceRoot(root=tmp_path)
    manifest = artifact_manifest(
        boundary, "publication", ("docs/public/two.md", "docs/public/one.md")
    )
    assert [entry.path for entry in manifest.entries] == [
        "docs/public/one.md",
        "docs/public/two.md",
    ]
    assert (
        manifest.entries[0].content_hash
        == boundary.resolve("docs/public/one.md").content_hash
    )

    _write(tmp_path, "docs/public/one.md", "one, rewritten\n")
    assert (
        artifact_manifest(
            boundary, "publication", ("docs/public/two.md", "docs/public/one.md")
        ).manifest_hash
        != manifest.manifest_hash
    )


def test_a_manifest_artifact_outside_the_root_is_refused_offline(
    tmp_path: Path,
) -> None:
    with pytest.raises(PublicationFault) as raised:
        artifact_manifest(
            EvidenceRoot(root=tmp_path), "publication", ("../outside.md",)
        )
    assert raised.value.reason is PublicationReason.EVIDENCE_PATH_REJECTED


def test_the_whole_shipped_checklist_is_still_pending_offline(tmp_path: Path) -> None:
    report = SUBMISSION_CHECKLIST.resolve(tmp_path)
    assert report.ready is False
    assert {state.item_id for state in report.pending} >= OWNER_ITEM_IDS
    with pytest.raises(PublicationFault) as raised:
        report.require_ready()
    assert raised.value.reason is PublicationReason.CHECKLIST_INCOMPLETE
