"""The Decision Receipt contract.

A Decision Receipt is verifier-owned evidence about one past decision and one
executed diagnostic call. It is not a cached answer, a model memory, a hidden
label, or any kind of tool-selection authority.

Two independent dimensions are proven about every read and they are never
collapsed into one. *Evidence novelty* is whether the read admitted a canonical
fact the run did not already hold. *Decision progress* is whether that fact
moved a supported disposition. Four outcomes follow from the pair: a read that
admitted a fact and moved a gate is `useful_evidence`; a read that admitted a
fact and moved nothing is `disposition_unchanged`; a read that admitted nothing
is the strict `no_progress`; and a failed or inadmissible read keeps its own
class and is never relabelled as either.

The only execution authority a receipt carries is negative and narrow. An exact
`no_progress` receipt proves that repeating the identical capability and
arguments under the identical current context admitted nothing and moved
nothing. An exact `disposition_unchanged` receipt proves the narrower fact that
the repetition admitted a canonical fact and still moved no supported
disposition, which is the blind spot a selector repeats. No other class may
ever refuse an execution, and neither label is taken on trust: `blocking`
re-derives the conjunction it needs from the two proven hash pairs.

Eligible is not blocked. A refusal additionally requires an exact match on the
identical capability, the identical arguments, and the identical decision
context, which binds every version the receipt was written under. Current
authoritative facts therefore always dominate history.

`observed_at` is audit time only. It is excluded from receipt identity so the
same verified fact written twice is one row, and it is never an expiry rule.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from unstuck.cascade.identity import BoundVersions
from unstuck.cascade.serialization import canonical_digest
from unstuck.domain import Contract, Digest, Identifier, Reason

OutcomeClass = Literal[
    "useful_evidence",
    "disposition_unchanged",
    "no_progress",
    "provider_error",
    "invalid_evidence",
    "superseded",
]

#: The two outcomes that prove a canonical fact was admitted. Both are valid
#: evidence and both resolve a cascade; they differ only in whether the fact
#: moved a supported disposition.
EVIDENCE_OUTCOMES = frozenset({"useful_evidence", "disposition_unchanged"})

#: The only outcomes that may ever be eligible to block a future identical
#: execution. Both are disposition-negative: one admitted nothing at all, the
#: other admitted a fact that moved no supported disposition. Eligibility is
#: still not a block; see `DecisionReceipt.blocking` and the exact-match rule.
BLOCKING_OUTCOMES = frozenset({"no_progress", "disposition_unchanged"})

#: Reasons that name the supported disposition a read opened. A read whose
#: admitted fact moved a gate carries exactly one of these.
PROGRESS_REASONS = frozenset(
    {
        Reason.POPULATED_INITIAL,
        Reason.POPULATED_AFTER_WAIT,
        Reason.POPULATED_AFTER_REPAIR,
        Reason.EMPTY_INITIAL,
        Reason.EMPTY_AFTER_WAIT,
        Reason.EMPTY_AFTER_REPAIR,
        Reason.STILL_PENDING,
    }
)
#: The one reason a read that opened no supported disposition may carry.
NEUTRAL_REASONS = frozenset({Reason.INSUFFICIENT})
NO_PROGRESS_REASONS = frozenset(
    {
        Reason.INSUFFICIENT,
        Reason.NO_READ,
        Reason.UNSUPPORTED_REPAIR,
        Reason.REPAIR_FAILED,
        Reason.DIAGNOSTICS_EXHAUSTED,
    }
)
PROVIDER_REASONS = frozenset(
    {Reason.TOOL_ERROR, Reason.DECIDER_ERROR, Reason.BEDROCK_ERROR}
)
INVALID_REASONS = frozenset(
    {Reason.CONFLICT, Reason.RECEIPT_INVALID, Reason.TASK_MISMATCH}
)
SUPERSEDED_REASONS = frozenset({Reason.RECEIPT_INVALID, Reason.REPEAT_BLOCKED})

#: Closed per-outcome reason taxonomy owned by the deterministic verifier, as
#: a stored receipt may be read back. `useful_evidence` admits the neutral
#: reason here for exactly one reason: a receipt written before the disposition
#: dimension was separated out carries `useful_evidence` with no moved gate,
#: and it must still read back. Such a row is never blocking.
OUTCOME_REASONS: dict[str, frozenset[Reason]] = {
    "useful_evidence": PROGRESS_REASONS | NEUTRAL_REASONS,
    "disposition_unchanged": NEUTRAL_REASONS,
    "no_progress": NO_PROGRESS_REASONS,
    "provider_error": PROVIDER_REASONS,
    "invalid_evidence": INVALID_REASONS,
    "superseded": SUPERSEDED_REASONS,
}
#: The taxonomy a freshly observed read is admitted under. There is no legacy
#: shape to accept here, so a moved gate must name what it opened.
OBSERVED_REASONS: dict[str, frozenset[Reason]] = OUTCOME_REASONS | {
    "useful_evidence": PROGRESS_REASONS
}
RECEIPT_REASONS = frozenset().union(*OUTCOME_REASONS.values())


class ReceiptNamespace(Contract):
    """Isolation boundary. Each arm and case starts empty."""

    arm: Literal["decider", "decider_memory"]
    case_id: Identifier

    @property
    def key(self) -> tuple[str, str]:
        return self.arm, self.case_id


class EvidenceDelta(Contract):
    """Proof of the observed change in admissible evidence and disposition.

    Two deltas are proven separately, because they answer two questions. The
    evidence hashes cover the canonical admissible evidence set, so every
    newly admitted record is named here and an inadmissible or failed read
    leaves them equal by construction. The set is canonical rather than
    operational: re-observing facts the run already holds leaves the hashes
    equal however many fresh record identities the provider minted, so a
    duplicate observation proves it admitted nothing new.

    The disposition hashes cover the supported dispositions that evidence
    opens. They are optional only for a caller that cannot separate the two;
    absent, they default to the evidence delta. A disposition can never move
    without new admissible evidence.

    `stalled` is the conjunction of both negatives, and it is the only delta
    the strict `no_progress` outcome may be claimed from. The weaker
    conjunction, `changed and not progressed`, is what
    `disposition_unchanged` is claimed from: a real fact was admitted and no
    supported disposition moved.
    """

    evidence_before_hash: Digest
    evidence_after_hash: Digest
    evidence_ids: Annotated[tuple[UUID, ...], Field(max_length=8)] = ()
    disposition_before_hash: Digest | None = None
    disposition_after_hash: Digest | None = None

    @model_validator(mode="after")
    def proven(self) -> Self:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("duplicate evidence identities")
        if (self.evidence_before_hash != self.evidence_after_hash) != bool(
            self.evidence_ids
        ):
            raise ValueError("evidence delta does not match admitted evidence")
        if (self.disposition_before_hash is None) != (
            self.disposition_after_hash is None
        ):
            raise ValueError("a disposition delta states both of its hashes")
        if self.progressed and not self.changed:
            raise ValueError("a disposition cannot move without admitted evidence")
        return self

    @property
    def changed(self) -> bool:
        """Whether this read admitted a canonical fact the run did not hold."""
        return self.evidence_before_hash != self.evidence_after_hash

    @property
    def progressed(self) -> bool:
        """Whether the admitted evidence moved a supported disposition."""
        if self.disposition_before_hash is None:
            return self.changed
        return self.disposition_before_hash != self.disposition_after_hash

    @property
    def stalled(self) -> bool:
        """Whether this read added no admissible fact and moved no gate.

        Both negatives are required, so a read that admitted a canonical fact
        is never stalled and can never ground the strict no-progress claim.
        """
        return not self.changed and not self.progressed

    @property
    def neutral(self) -> bool:
        """Whether this read admitted a fact that moved no supported gate.

        This is the dimension the strict `stalled` conjunction cannot express:
        the evidence is real and admissible, and the decision is exactly where
        it was before the read.
        """
        return self.changed and not self.progressed


class ReceiptIdentityFields(Contract):
    """The normalized fields that define receipt identity."""

    namespace: ReceiptNamespace
    decision_id: UUID
    model_call_id: UUID
    diagnostic_call_id: UUID | None
    problem_fingerprint: Digest
    decision_context_hash: Digest
    selected_capability: Identifier
    public_tool_id: Identifier
    arguments_hash: Digest
    delta: EvidenceDelta
    outcome_class: OutcomeClass
    reason_code: Reason
    versions: BoundVersions


def receipt_identity(value: "DecisionReceipt | Mapping[str, object]") -> str:
    """SHA-256 identity of the normalized receipt, excluding audit time."""
    source: Mapping[str, object] = (
        value.model_dump() if isinstance(value, Contract) else value
    )
    try:
        fields = {
            name: source[name]
            for name in ReceiptIdentityFields.model_fields
            if name != "schema_version"
        }
    except KeyError as error:
        raise ValueError("incomplete receipt identity") from error
    return canonical_digest(ReceiptIdentityFields.model_validate(fields))


class DecisionReceipt(Contract):
    """Verifier-owned evidence about one decision and one diagnostic call."""

    receipt_id: Digest
    namespace: ReceiptNamespace
    decision_id: UUID
    model_call_id: UUID
    diagnostic_call_id: UUID | None
    problem_fingerprint: Digest
    decision_context_hash: Digest
    selected_capability: Identifier
    public_tool_id: Identifier
    arguments_hash: Digest
    delta: EvidenceDelta
    outcome_class: OutcomeClass
    reason_code: Reason
    versions: BoundVersions
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("UTC timestamp required")
        return value

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.reason_code not in OUTCOME_REASONS[self.outcome_class]:
            raise ValueError("reason code is outside the verifier taxonomy")
        if self.delta.changed != (self.outcome_class in EVIDENCE_OUTCOMES):
            raise ValueError("an evidence outcome is exactly a newly admitted fact")
        if self.outcome_class == "useful_evidence":
            if self.delta.progressed and self.reason_code not in PROGRESS_REASONS:
                raise ValueError("a moved disposition names the gate it opened")
            if not self.delta.progressed and self.reason_code not in NEUTRAL_REASONS:
                raise ValueError("an unmoved disposition opened no named gate")
        if self.outcome_class == "disposition_unchanged" and not self.delta.neutral:
            raise ValueError("a neutral read admitted a fact and moved no gate")
        if self.outcome_class == "no_progress" and not self.delta.stalled:
            raise ValueError("no progress requires no admitted fact and no moved gate")
        if self.diagnostic_call_id is None and self.outcome_class not in {
            "provider_error",
            "superseded",
        }:
            raise ValueError("verified outcome requires an executed diagnostic call")
        if self.receipt_id != receipt_identity(self):
            raise ValueError("receipt identity mismatch")
        return self

    @property
    def blocking(self) -> bool:
        """Whether this receipt is eligible to refuse a repeated execution.

        Two classes qualify and each proves its own conjunction, which is
        re-derived here from the two proven hash pairs rather than taken on
        trust from the label:

        * `no_progress`: under this identical decision context, this capability
          and these arguments admitted no fact the run did not already hold and
          moved no supported disposition.
        * `disposition_unchanged`: they admitted a real, admissible fact and
          still moved no supported disposition.

        Every other class returns false unconditionally, including a stored
        `useful_evidence` row written before the two dimensions were separated,
        so no receipt can acquire authority it was not written with.

        Eligibility is not a refusal. The caller must also find an exact
        current-context match on the identical capability and arguments.
        """
        if self.outcome_class == "no_progress":
            return self.delta.stalled
        if self.outcome_class == "disposition_unchanged":
            return self.delta.neutral
        return False

    @property
    def execution_key(self) -> tuple[str, str]:
        return self.selected_capability, self.arguments_hash
