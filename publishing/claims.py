"""The claims audit (REQ-PUB-06, REQ-PUB-10, NFR8.2).

Every advertised claim must name recorded evidence that resolves to a regular
file inside the repository's own evidence root. Claims in the categories this
project is forbidden from asserting without evidence - novelty, significance,
latency benefit, cost benefit, compliance, achieved rubric score, measurement -
must additionally name a typed evidence record, because for those categories a
file that merely exists proves nothing. A claim about data must say the data is
synthetic.

The audit is deliberately dumb about content and strict about provenance. It
does not judge whether a claim is true; it refuses to let one travel without
the artifact a reader would need to check it, and it refuses to resolve that
artifact from outside the repository.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from publishing import PublicationFault, PublicationReason
from publishing.evidence import EvidenceReference, EvidenceRoot
from unstuck.domain import Contract, Identifier

#: Categories that may never be asserted without resolving evidence.
GUARDED_CATEGORIES: tuple[str, ...] = (
    "novelty",
    "significance",
    "latency_benefit",
    "cost_benefit",
    "compliance",
    "rubric_score",
    "measurement",
)
ClaimCategory = Literal[
    "novelty",
    "significance",
    "latency_benefit",
    "cost_benefit",
    "compliance",
    "rubric_score",
    "measurement",
    "capability",
    "design",
]
#: A claim about data must disclose that the data is synthetic.
SYNTHETIC_LABEL = "synthetic"
#: REQ-PUB-10: the controlled incident track carries a disclosed prefix.
CONTROLLED_PREFIX_NOTE = "controlled incident"


class Claim(Contract):
    """One advertised claim and the evidence a reader can check it against."""

    claim_id: Identifier
    text: Annotated[str, Field(min_length=1, max_length=1024)]
    category: ClaimCategory
    evidence_paths: Annotated[tuple[str, ...], Field(max_length=8)] = ()
    #: Typed records this claim is bound to. A guarded category needs at least
    #: one before the audit can support it.
    evidence_records: Annotated[tuple[EvidenceReference, ...], Field(max_length=8)] = ()
    about_data: bool = False
    about_controlled_track: bool = False

    @model_validator(mode="after")
    def guarded(self) -> Self:
        if self.category in GUARDED_CATEGORIES and not (
            self.evidence_paths or self.evidence_records
        ):
            raise ValueError(
                f"a {self.category} claim must name the evidence that supports it"
            )
        for reference in self.evidence_records:
            if reference.subject_id != self.claim_id:
                raise ValueError("an evidence record names the claim it supports")
        return self

    @property
    def declared(self) -> tuple[str, ...]:
        """Every path this claim declares, records included."""
        return self.evidence_paths + tuple(
            reference.path for reference in self.evidence_records
        )


class ClaimVerdict(Contract):
    """Whether this claim may be advertised, and why not when it may not."""

    claim_id: Identifier
    supported: bool
    reason: PublicationReason | None = None
    detail: Annotated[str, Field(max_length=512)] = ""
    resolved: Annotated[tuple[str, ...], Field(max_length=8)] = ()
    missing: Annotated[tuple[str, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.supported != (self.reason is None):
            raise ValueError("an unsupported claim carries its reason code")
        if self.supported and self.missing:
            raise ValueError("a supported claim has no missing evidence")
        return self


class AuditReport(Contract):
    """The audit over a claim set. Pending until every claim is supported."""

    verdicts: Annotated[tuple[ClaimVerdict, ...], Field(max_length=256)]

    @property
    def unsupported(self) -> tuple[ClaimVerdict, ...]:
        return tuple(item for item in self.verdicts if not item.supported)

    @property
    def publishable(self) -> bool:
        return bool(self.verdicts) and not self.unsupported

    def require_publishable(self) -> None:
        """Refuse to advertise a claim set with an unsupported claim."""
        if not self.publishable:
            names = ", ".join(item.claim_id for item in self.unsupported) or "none"
            raise PublicationFault(
                PublicationReason.CLAIM_UNSUPPORTED, f"unsupported: {names}"
            )


#: An absent file is the ordinary outcome and keeps its own reason. Every other
#: refusal names what was wrong with the evidence itself and is reported.
_ORDINARY: frozenset[PublicationReason] = frozenset(
    {PublicationReason.EVIDENCE_UNRESOLVED}
)


def _hard(fault: PublicationFault) -> PublicationFault | None:
    return None if fault.reason in _ORDINARY else fault


def audit_claim(claim: Claim, root: Path) -> ClaimVerdict:
    """Resolve one claim's evidence inside the repository's evidence root.

    A path is resolved canonically or not at all: an absolute path, a traversal,
    a link, a directory, and anything outside the root are refusals rather than
    resolutions. A declared record must additionally say what the claim bound it
    to say.
    """
    if not claim.declared:
        return ClaimVerdict(
            claim_id=claim.claim_id,
            supported=False,
            reason=PublicationReason.EVIDENCE_MISSING,
            detail="the claim names no evidence",
        )
    try:
        boundary = EvidenceRoot(root=root.absolute())
    except PublicationFault as fault:
        # The declared root itself was refused, so nothing inside it can be
        # read and no claim over it is supported.
        return ClaimVerdict(
            claim_id=claim.claim_id,
            supported=False,
            reason=fault.reason,
            detail=fault.detail or fault.reason.value,
            missing=tuple(claim.declared),
        )
    resolved: list[str] = []
    missing: list[str] = []
    refusal: PublicationFault | None = None
    for relative in claim.evidence_paths:
        try:
            boundary.resolve(relative)
        except PublicationFault as fault:
            missing.append(relative)
            refusal = refusal or _hard(fault)
        else:
            resolved.append(relative)
    for reference in claim.evidence_records:
        try:
            boundary.read_record(reference)
        except PublicationFault as fault:
            missing.append(reference.path)
            refusal = refusal or _hard(fault)
        else:
            resolved.append(reference.path)
    if claim.category in GUARDED_CATEGORIES and not claim.evidence_records:
        return ClaimVerdict(
            claim_id=claim.claim_id,
            supported=False,
            reason=PublicationReason.CLAIM_FORBIDDEN,
            detail=(
                f"a {claim.category} claim needs a typed evidence record; "
                "a file that exists is not a record"
            ),
            resolved=tuple(resolved),
            missing=tuple(missing),
        )
    if refusal is not None:
        # The evidence itself was wrong, not merely absent. That reason is what
        # a reader needs, so it is never flattened into a category refusal.
        return ClaimVerdict(
            claim_id=claim.claim_id,
            supported=False,
            reason=refusal.reason,
            detail=refusal.detail or refusal.reason.value,
            resolved=tuple(resolved),
            missing=tuple(missing),
        )
    if claim.category in GUARDED_CATEGORIES and not resolved:
        return ClaimVerdict(
            claim_id=claim.claim_id,
            supported=False,
            reason=PublicationReason.CLAIM_FORBIDDEN,
            detail=(
                f"a {claim.category} claim needs evidence that resolves; "
                "none of its paths did"
            ),
            missing=tuple(missing),
        )
    if missing:
        return ClaimVerdict(
            claim_id=claim.claim_id,
            supported=False,
            reason=PublicationReason.EVIDENCE_UNRESOLVED,
            detail=f"missing: {', '.join(missing)}",
            resolved=tuple(resolved),
            missing=tuple(missing),
        )
    if claim.about_data and SYNTHETIC_LABEL not in claim.text.lower():
        return ClaimVerdict(
            claim_id=claim.claim_id,
            supported=False,
            reason=PublicationReason.LABEL_MISSING,
            detail="a claim about data must say the data is synthetic",
            resolved=tuple(resolved),
        )
    if (
        claim.about_controlled_track
        and CONTROLLED_PREFIX_NOTE not in claim.text.lower()
    ):
        return ClaimVerdict(
            claim_id=claim.claim_id,
            supported=False,
            reason=PublicationReason.PREFIX_UNDISCLOSED,
            detail="a controlled-track claim must disclose the prefix",
            resolved=tuple(resolved),
        )
    return ClaimVerdict(
        claim_id=claim.claim_id, supported=True, resolved=tuple(resolved)
    )


def audit(claims: Sequence[Claim], root: Path) -> AuditReport:
    """Audit a claim set. Nothing is published; a report is returned."""
    return AuditReport(verdicts=tuple(audit_claim(claim, root) for claim in claims))


__all__ = [
    "CONTROLLED_PREFIX_NOTE",
    "GUARDED_CATEGORIES",
    "SYNTHETIC_LABEL",
    "AuditReport",
    "Claim",
    "ClaimCategory",
    "ClaimVerdict",
    "EvidenceReference",
    "audit",
    "audit_claim",
]
