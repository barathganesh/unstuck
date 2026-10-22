"""The submission checklist (REQ-PUB-08, REQ-PUB-09, NFR8.1).

Pending by construction. Every item names the verification it waits for, and
no item can be reported as passed while that verification has not been recorded
on disk. The shipped checklist therefore resolves entirely to Pending: none of
these verifications has been performed.

Publishing and submitting are two separate items (REQ-PUB-08), each needing its
own owner approval. Approving one never approves the other, and nothing in this
module performs either action.

An owner approval is a **consumable record**, not a flag. Each owner item names
the typed record it waits for, so a recorded approval has a real path to
passing; the record must name that item, record an approval, and carry its own
moment and version. An in-memory approval set alone never passes an item, and
an absent or mismatched record stays Pending.

It is also **bound to content**. The shipped owner items name the record they
wait for and nothing more, because no decision has been taken: a declaration
that names no content hash and no artifact manifest hash cannot pass. An item
passes only once its declaration binds the approval record's own content hash
and the hash of the artifact manifest that record approves, and only while
every artifact in that manifest still hashes to the content the owner
approved. Changing a published artifact after approval therefore returns the
item to Pending rather than leaving a stale approval standing.
"""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from publishing import PublicationFault, PublicationReason
from publishing.evidence import (
    OWNER_RECORD_TYPES,
    EvidenceReference,
    EvidenceRoot,
)
from unstuck.domain import Contract, Identifier

ItemStatus = Literal["pending", "passed"]


class ChecklistItem(Contract):
    """One submission item, the requirement behind it, and its verification."""

    item_id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=256)]
    requirement: Annotated[str, Field(min_length=1, max_length=64)]
    verification: Annotated[str, Field(min_length=1, max_length=512)]
    evidence_paths: Annotated[tuple[str, ...], Field(max_length=8)] = ()
    #: The typed records this item waits for. An owner item names exactly the
    #: approval record that can satisfy it.
    evidence_records: Annotated[tuple[EvidenceReference, ...], Field(max_length=4)] = ()
    requires_owner: bool = False

    @model_validator(mode="after")
    def owner_items_name_their_record(self) -> Self:
        for reference in self.evidence_records:
            if reference.subject_id != self.item_id:
                raise ValueError("an evidence record names the item it satisfies")
        if not self.requires_owner:
            return self
        expected = OWNER_RECORD_TYPES.get(self.item_id)
        if expected is None:
            return self
        kinds = {reference.record_type for reference in self.evidence_records}
        if kinds and expected not in kinds:
            raise ValueError("an owner item names its own approval record")
        return self

    @property
    def declared(self) -> tuple[str, ...]:
        return self.evidence_paths + tuple(
            reference.path for reference in self.evidence_records
        )


class ItemState(Contract):
    """How one item resolves against the repository and owner approvals."""

    item_id: Identifier
    status: ItemStatus
    reason: PublicationReason | None = None
    detail: Annotated[str, Field(max_length=512)] = ""

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.status == "passed") != (self.reason is None):
            raise ValueError("a pending item carries its reason code")
        return self


class ChecklistReport(Contract):
    """The resolved checklist. Readiness is refused while anything pends."""

    states: Annotated[tuple[ItemState, ...], Field(min_length=1, max_length=64)]

    @property
    def pending(self) -> tuple[ItemState, ...]:
        return tuple(state for state in self.states if state.status == "pending")

    @property
    def ready(self) -> bool:
        return not self.pending

    def require_ready(self) -> None:
        """Refuse to report readiness while a verification is outstanding."""
        if not self.ready:
            names = ", ".join(state.item_id for state in self.pending)
            raise PublicationFault(
                PublicationReason.CHECKLIST_INCOMPLETE, f"pending: {names}"
            )


def resolve_item(
    item: ChecklistItem, root: Path, owner_approvals: frozenset[str]
) -> ItemState:
    """Resolve one item. Absence of a verification is never a pass.

    The owner decision is evaluated from the record the item names, so an owner
    item that carries one has a real path to passing. `owner_approvals` is an
    optional cross-check the caller may narrow with: when it is non-empty, an
    item it does not name stays Pending whatever is on disk.
    """
    if not item.declared:
        return ItemState(
            item_id=item.item_id,
            status="pending",
            reason=PublicationReason.VERIFICATION_MISSING,
            detail=f"awaiting: {item.verification}",
        )
    try:
        boundary = EvidenceRoot(root=root.absolute())
    except PublicationFault as fault:
        return ItemState(
            item_id=item.item_id,
            status="pending",
            reason=fault.reason,
            detail=fault.detail or fault.reason.value,
        )
    for relative in item.evidence_paths:
        try:
            boundary.resolve(relative)
        except PublicationFault as fault:
            return ItemState(
                item_id=item.item_id,
                status="pending",
                reason=(
                    PublicationReason.EVIDENCE_MISSING
                    if fault.reason is PublicationReason.EVIDENCE_UNRESOLVED
                    else fault.reason
                ),
                detail=f"missing: {relative}",
            )
    if item.requires_owner and not item.evidence_records:
        return ItemState(
            item_id=item.item_id,
            status="pending",
            reason=PublicationReason.OWNER_APPROVAL_REQUIRED,
            detail=(
                f"{item.item_id} needs a recorded owner decision; "
                "a file that exists is not a decision"
            ),
        )
    for reference in item.evidence_records:
        try:
            boundary.read_record(reference)
        except PublicationFault as fault:
            return ItemState(
                item_id=item.item_id,
                status="pending",
                reason=(
                    PublicationReason.OWNER_APPROVAL_REQUIRED
                    if item.requires_owner
                    and fault.reason
                    in {
                        PublicationReason.EVIDENCE_UNRESOLVED,
                        PublicationReason.APPROVAL_BINDING_MISSING,
                    }
                    else fault.reason
                ),
                detail=f"{reference.path}: {fault.detail or fault.reason.value}",
            )
    if item.requires_owner and owner_approvals and item.item_id not in owner_approvals:
        return ItemState(
            item_id=item.item_id,
            status="pending",
            reason=PublicationReason.OWNER_APPROVAL_REQUIRED,
            detail=f"the owner has not approved {item.item_id}",
        )
    return ItemState(item_id=item.item_id, status="passed")


class Checklist(Contract):
    """An ordered item set, resolved as a whole."""

    items: Annotated[tuple[ChecklistItem, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def unique(self) -> Self:
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("an item id appears twice")
        return self

    def item(self, item_id: str) -> ChecklistItem:
        for candidate in self.items:
            if candidate.item_id == item_id:
                return candidate
        raise PublicationFault(
            PublicationReason.CHECKLIST_INCOMPLETE, f"unknown item: {item_id}"
        )

    def resolve(
        self, root: Path, owner_approvals: frozenset[str] = frozenset()
    ) -> ChecklistReport:
        return ChecklistReport(
            states=tuple(
                resolve_item(item, root, owner_approvals) for item in self.items
            )
        )


#: Where each owner decision is recorded, relative to the repository root. The
#: files do not exist: that is why both items are Pending, and writing the
#: matching typed record and binding it is the one way either passes.
OWNER_APPROVAL_RECORDS: dict[str, str] = {
    "publish-approval": "docs/evidence/publish-approval.json",
    "submit-approval": "docs/evidence/submit-approval.json",
}


def _owner_record(item_id: str) -> EvidenceReference:
    """The awaited owner record: named, and deliberately not yet bound.

    No decision has been taken, so there is no approval content to hash and no
    artifact manifest to bind. The declaration names what is awaited, and an
    unbound owner declaration can never pass its item.
    """
    return EvidenceReference(
        path=OWNER_APPROVAL_RECORDS[item_id],
        record_type=OWNER_RECORD_TYPES[item_id],
        subject_id=item_id,
        decision="approved",
    )


def owner_record(
    item_id: str, *, content_hash: str, artifact_manifest_hash: str
) -> EvidenceReference:
    """Bind one owner approval to its own content and its artifact manifest.

    Offered so an owner decision that has actually been recorded has a correct
    declaration to be checked against, rather than inviting a caller to invent
    the shape. Both hashes are required: the approval record's own content, and
    the immutable artifact manifest that record approves.
    """
    return EvidenceReference(
        path=OWNER_APPROVAL_RECORDS[item_id],
        record_type=OWNER_RECORD_TYPES[item_id],
        subject_id=item_id,
        decision="approved",
        content_hash=content_hash,
        artifact_manifest_hash=artifact_manifest_hash,
    )


#: The shipped checklist. Every item is Pending: no verification has been run.
SUBMISSION_CHECKLIST = Checklist(
    items=(
        ChecklistItem(
            item_id="eligibility",
            title="Entry eligibility is confirmed against the published rules.",
            requirement="REQ-PUB-09",
            verification=(
                "The owner read the current published eligibility rules at "
                "submission time and recorded the result."
            ),
        ),
        ChecklistItem(
            item_id="one-entry-rule",
            title="The one-entry rule is satisfied.",
            requirement="REQ-PUB-09",
            verification=(
                "The owner confirmed no other entry has been submitted under "
                "the same identity and recorded the result."
            ),
        ),
        ChecklistItem(
            item_id="deadline",
            title="The submission deadline has not passed.",
            requirement="REQ-PUB-09",
            verification=(
                "The owner read the published deadline at submission time and "
                "recorded it against the submission moment."
            ),
        ),
        ChecklistItem(
            item_id="required-links",
            title="Every required submission link resolves.",
            requirement="REQ-PUB-09",
            verification=(
                "Each required link was opened and recorded as resolving, "
                "including the repository, the article and the video."
            ),
        ),
        ChecklistItem(
            item_id="licence",
            title="The licence and dependency notices are verified.",
            requirement="REQ-PUB-01",
            verification=(
                "NFR8.1 verification: each pinned dependency's and the Decider "
                "model's licence was read and recorded. NOTICE currently "
                "records every licence as unverified."
            ),
        ),
        ChecklistItem(
            item_id="video-host",
            title="The video is hosted where the rules permit.",
            requirement="REQ-PUB-04",
            verification=(
                "The owner confirmed the host is an approved one and recorded "
                "the resolving public URL."
            ),
        ),
        ChecklistItem(
            item_id="claims-audit",
            title="Every advertised claim passes the claims audit.",
            requirement="REQ-PUB-06",
            verification=(
                "publishing.claims.audit reports no unsupported claim over "
                "the claim set actually advertised."
            ),
        ),
        ChecklistItem(
            item_id="synthetic-labels",
            title="Synthetic data is labelled and the controlled prefix disclosed.",
            requirement="REQ-PUB-10",
            verification=(
                "Every published artifact that reports data labels it "
                "synthetic and discloses the controlled incident prefix."
            ),
        ),
        ChecklistItem(
            item_id="publish-approval",
            title="The owner approved publishing.",
            requirement="REQ-PUB-08",
            verification=(
                "The owner recorded an explicit decision to publish, as the "
                "typed record this item names. Publishing is a separate "
                "action from submitting."
            ),
            evidence_records=(_owner_record("publish-approval"),),
            requires_owner=True,
        ),
        ChecklistItem(
            item_id="submit-approval",
            title="The owner approved submitting.",
            requirement="REQ-PUB-08",
            verification=(
                "The owner recorded an explicit decision to submit, as the "
                "typed record this item names. Submitting is a separate "
                "action from publishing."
            ),
            evidence_records=(_owner_record("submit-approval"),),
            requires_owner=True,
        ),
    )
)

#: Every item the shipped checklist must carry.
REQUIRED_ITEM_IDS: frozenset[str] = frozenset(
    item.item_id for item in SUBMISSION_CHECKLIST.items
)
#: The two items that need the owner, and that are never the same decision.
OWNER_ITEM_IDS: frozenset[str] = frozenset(
    item.item_id for item in SUBMISSION_CHECKLIST.items if item.requires_owner
)


__all__ = [
    "OWNER_APPROVAL_RECORDS",
    "OWNER_ITEM_IDS",
    "REQUIRED_ITEM_IDS",
    "SUBMISSION_CHECKLIST",
    "Checklist",
    "ChecklistItem",
    "ChecklistReport",
    "ItemState",
    "ItemStatus",
    "owner_record",
    "resolve_item",
]
