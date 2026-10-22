"""Deterministic receipt admission.

The outcome class of a diagnostic read is derived, never supplied. A model can
state nothing about it: the controller reports the structured facts it observed
and the verifier maps those facts onto the closed outcome taxonomy.

The facts are two independent deltas, and both are needed. Whether the read
admitted a canonical fact the run did not hold decides evidence novelty;
whether that fact moved a supported disposition decides decision progress. The
pair is what distinguishes a productive read from a real read that left the
decision exactly where it was.

Admission also refuses anything that is not verifier-owned fact: evaluator
truth, hidden causes, expected answers, credentials, and model prose are all
rejected by the shared persistence boundary before a receipt exists.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import model_validator

from unstuck.cascade.cards import CardBinding, canonical_capability, capability_cards
from unstuck.cascade.identity import DecisionContext
from unstuck.cascade.receipts import (
    OBSERVED_REASONS,
    DecisionReceipt,
    EvidenceDelta,
    OutcomeClass,
    ReceiptNamespace,
    receipt_identity,
)
from unstuck.cascade.repository import ReceiptWriteToken, mint_write_token
from unstuck.cascade.serialization import StageMeasurement
from unstuck.clock import Clock
from unstuck.domain import Contract, Digest, Identifier, Reason, RuntimeFault
from unstuck.events import PersistenceBoundary, Redactor

#: Deterministic map from observed facts to outcome class. The keys are the
#: reported status and the two proven deltas, never the model's opinion:
#: evidence novelty and decision progress are independent, so a read that
#: admitted a canonical fact is classified by whether that fact moved a
#: supported disposition, and the strict no-progress class stays reserved for a
#: read that admitted nothing at all. A failed or inadmissible read keeps its
#: own class and is never relabelled as either.
_OUTCOMES: dict[tuple[str, bool, bool], OutcomeClass] = {
    ("evidence", True, True): "useful_evidence",
    ("evidence", True, False): "disposition_unchanged",
    ("evidence", False, False): "no_progress",
    ("provider_error", False, False): "provider_error",
    ("invalid_evidence", False, False): "invalid_evidence",
}


class DiagnosticReport(Contract):
    """Controller-owned structured result of one executed diagnostic read.

    This carries facts only. There is no model prose, confidence, or proposed
    outcome label anywhere in the contract.
    """

    diagnostic_call_id: UUID | None
    capability: Identifier
    public_tool_id: Identifier
    arguments_hash: Digest
    status: Literal["evidence", "provider_error", "invalid_evidence"]
    delta: EvidenceDelta
    admissible: bool
    reason_code: Reason
    latency_ms: StageMeasurement = StageMeasurement()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        outcome = _OUTCOMES.get(
            (self.status, self.delta.changed, self.delta.progressed)
        )
        if outcome is None:
            raise ValueError("a failed or inadmissible read admitted no evidence")
        if self.admissible != (self.status == "evidence"):
            raise ValueError("admission flag contradicts the reported status")
        if self.reason_code not in OBSERVED_REASONS[outcome]:
            raise ValueError("reason code is outside the verifier taxonomy")
        if self.diagnostic_call_id is None and self.status != "provider_error":
            raise ValueError("a completed read requires its diagnostic call identity")
        return self

    @property
    def outcome_class(self) -> OutcomeClass:
        return _OUTCOMES[(self.status, self.delta.changed, self.delta.progressed)]


class ReceiptVerifier:
    """The only creator of Decision Receipts."""

    def __init__(
        self,
        clock: Clock,
        *,
        cards: tuple[CardBinding, ...] | None = None,
        redactor: Redactor | None = None,
    ):
        self.clock = clock
        self.cards = cards or capability_cards()
        self.boundary = PersistenceBoundary(redactor or Redactor())

    @property
    def token(self) -> ReceiptWriteToken:
        return mint_write_token()

    def classify(self, report: DiagnosticReport) -> OutcomeClass:
        """Derive the outcome class from observed facts alone."""
        if not isinstance(report, DiagnosticReport):
            raise RuntimeFault(Reason.RECEIPT_INVALID)
        return DiagnosticReport.model_validate(report).outcome_class

    def create(
        self,
        *,
        namespace: ReceiptNamespace,
        decision_id: UUID,
        model_call_id: UUID,
        context: DecisionContext,
        report: DiagnosticReport,
    ) -> DecisionReceipt:
        outcome = self.classify(report)
        try:
            capability = canonical_capability(report.capability, self.cards)
            if canonical_capability(report.public_tool_id, self.cards) != capability:
                raise ValueError("public identity does not resolve to the capability")
        except ValueError as error:
            raise RuntimeFault(Reason.RECEIPT_INVALID) from error
        payload: dict[str, object] = {
            "namespace": namespace,
            "decision_id": decision_id,
            "model_call_id": model_call_id,
            "diagnostic_call_id": report.diagnostic_call_id,
            "problem_fingerprint": context.problem_fingerprint,
            "decision_context_hash": context.decision_context_hash,
            "selected_capability": capability,
            "public_tool_id": report.public_tool_id,
            "arguments_hash": report.arguments_hash,
            "delta": report.delta,
            "outcome_class": outcome,
            "reason_code": report.reason_code,
            "versions": context.versions,
        }
        receipt = DecisionReceipt.model_validate(
            payload
            | {
                "receipt_id": receipt_identity(payload),
                "observed_at": self.clock.utcnow(),
            }
        )
        self.boundary.validate(receipt, Reason.RECEIPT_INVALID)
        return receipt
