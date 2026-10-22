"""Deterministic, bounded Decision Receipt retrieval and classification.

Matching is computed against the pre-D1 decision context. An exact match
satisfies every current binding. A compatible receipt shares the problem
lineage and all bound versions but describes a different current context; it is
labeled historical evidence and can never block an execution. Anything whose
bound versions have moved on is audit-only: it stays in the store and is not
handed to the selector at all.

Retrieval order is fixed and timestamp-independent: the two exact
blocking-eligible classes first, strict no-progress before disposition-neutral,
then other exact receipts, then the compatible ones.

Truncation is explicit rather than silent, and the two kinds are not the same
fact. `truncated` means the whole namespace was read and classified matches
were dropped to stay inside the reference bound: the history is complete, so
every receipt that could refuse an execution was seen. `retrieval_truncated`
means the store's read budget stopped before the namespace was exhausted, so
receipts were left unread and an exact no-progress blocker may be sitting in
the remainder. An incomplete history cannot be reasoned over, so it fails
closed: the lookup returns explicit `unavailable` with `retrieval_truncated`
recorded, which is neither a cold start nor a no-match, and the caller stops
before any selector or tool call.

Neither signal carries positive authority. Matching is still computed only
from the current decision context, and only an exact match whose recorded
deltas prove a disposition-negative outcome ever blocks an execution.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from unstuck.cascade.identity import DecisionContext
from unstuck.cascade.receipts import (
    BLOCKING_OUTCOMES,
    DecisionReceipt,
    OutcomeClass,
    ReceiptNamespace,
)
from unstuck.cascade.repository import (
    BoundedReceiptRepository,
    ReceiptLookupError,
    ReceiptPage,
    ReceiptRepository,
)
from unstuck.cascade.serialization import MAX_RECEIPT_REFERENCES
from unstuck.decision import ReceiptReference
from unstuck.domain import Contract, Digest, Identifier, Reason, RuntimeFault

MatchClass = Literal["exact", "compatible"]
HistoryStatus = Literal["not_requested", "unavailable", "empty", "matched"]

_EXACT_RANK: dict[str, int] = {"no_progress": 0, "disposition_unchanged": 1}
_COMPATIBLE_RANK: dict[str, int] = {
    "useful_evidence": 3,
    "disposition_unchanged": 4,
    "no_progress": 5,
}


class ReceiptMatch(Contract):
    """One classified receipt as it is offered to the selector.

    `admitted_evidence` and `moved_disposition` carry the receipt's own two
    proven deltas rather than a restatement of its label, so the negative
    authority of a match is re-derived here exactly as the receipt derives it.
    """

    receipt_id: Digest
    match_class: MatchClass
    outcome_class: OutcomeClass
    public_tool_id: Identifier
    selected_capability: Identifier
    arguments_hash: Digest
    reason_code: Reason
    admitted_evidence: bool = False
    moved_disposition: bool = False

    @property
    def blocking(self) -> bool:
        """Only an exact disposition-negative match carries that authority.

        The label is not enough on its own: the strict no-progress class must
        still record that nothing was admitted and nothing moved, and the
        disposition-neutral class must still record a real admitted fact that
        moved nothing. Every other class, exact or not, carries none.
        """
        if self.match_class != "exact":
            return False
        if self.outcome_class not in BLOCKING_OUTCOMES:
            return False
        if self.moved_disposition:
            return False
        if self.outcome_class == "no_progress":
            return not self.admitted_evidence
        return self.admitted_evidence

    @property
    def execution_key(self) -> tuple[str, str]:
        return self.selected_capability, self.arguments_hash

    @property
    def reference(self) -> ReceiptReference:
        """Bounded core projection. The private capability is not included."""
        return ReceiptReference(
            receipt_id=self.receipt_id,
            match_class=self.match_class,
            outcome_class=self.outcome_class,
            public_tool_id=self.public_tool_id,
            arguments_hash=self.arguments_hash,
            reason_code=self.reason_code,
        )


class HistoryLookupResult(Contract):
    """Explicit history status. An absent history never looks like a decision."""

    status: HistoryStatus = "not_requested"
    matches: Annotated[
        tuple[ReceiptMatch, ...], Field(max_length=MAX_RECEIPT_REFERENCES)
    ] = ()
    truncated: bool = False
    #: The store's read budget stopped before the namespace was exhausted, so
    #: receipts were left unread. The history is incomplete rather than absent
    #: or empty, and an incomplete history is only ever reported as explicit
    #: `unavailable`.
    retrieval_truncated: bool = False

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.status == "matched") != bool(self.matches):
            raise ValueError("history status does not match reference availability")
        if self.truncated and self.status != "matched":
            raise ValueError("truncation requires matched history")
        if self.retrieval_truncated and self.status != "unavailable":
            raise ValueError("a truncated retrieval is explicit unavailable history")
        identifiers = tuple(match.receipt_id for match in self.matches)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate receipt identities")
        return self

    @property
    def receipt_ids(self) -> tuple[str, ...]:
        return tuple(match.receipt_id for match in self.matches)

    @property
    def references(self) -> tuple[ReceiptReference, ...]:
        return tuple(match.reference for match in self.matches)

    @property
    def blocked_executions(self) -> frozenset[tuple[str, str]]:
        return frozenset(
            match.execution_key for match in self.matches if match.blocking
        )

    def blocking_match(self, execution_key: tuple[str, str]) -> ReceiptMatch | None:
        return next(
            (
                match
                for match in self.matches
                if match.blocking and match.execution_key == execution_key
            ),
            None,
        )


def _rank(match: ReceiptMatch) -> tuple[int, str]:
    if match.match_class == "exact":
        return _EXACT_RANK.get(match.outcome_class, 2), match.receipt_id
    return _COMPATIBLE_RANK.get(match.outcome_class, 6), match.receipt_id


class HistoryMatcher:
    """Classifies stored receipts against one current decision context."""

    def __init__(
        self,
        repository: ReceiptRepository,
        *,
        max_references: int = MAX_RECEIPT_REFERENCES,
    ):
        self.repository = repository
        self.max_references = min(max_references, MAX_RECEIPT_REFERENCES)

    def classify(
        self, receipt: DecisionReceipt, context: DecisionContext
    ) -> MatchClass | None:
        """Return the match class, or None when the receipt is audit-only."""
        if receipt.outcome_class == "superseded":
            return None
        if receipt.problem_fingerprint != context.problem_fingerprint:
            return None
        if receipt.versions != context.versions:
            return None
        if receipt.decision_context_hash == context.decision_context_hash:
            return "exact"
        return "compatible"

    def _retrieve(
        self, namespace: ReceiptNamespace, problem_fingerprint: str
    ) -> ReceiptPage:
        """Read through the bounded surface when the store offers one."""
        if isinstance(self.repository, BoundedReceiptRepository):
            return self.repository.find_page(namespace, problem_fingerprint)
        return ReceiptPage(
            receipts=self.repository.find(namespace, problem_fingerprint)
        )

    def lookup(
        self,
        namespace: ReceiptNamespace,
        context: DecisionContext,
        *,
        requested: bool,
    ) -> HistoryLookupResult:
        if not requested:
            return HistoryLookupResult()
        try:
            page = self._retrieve(namespace, context.problem_fingerprint)
        except ReceiptLookupError:
            return HistoryLookupResult(status="unavailable")
        if page.truncated:
            # The budget stopped with receipts unread, so an exact no-progress
            # blocker may be in the remainder. A partial history is refused
            # whole rather than classified as the whole of what exists.
            return HistoryLookupResult(status="unavailable", retrieval_truncated=True)
        matches: list[ReceiptMatch] = []
        for row in page.receipts:
            if not isinstance(row, DecisionReceipt):
                raise RuntimeFault(Reason.RECEIPT_INVALID)
            try:
                receipt = DecisionReceipt.model_validate(row)
            except ValidationError as error:
                raise RuntimeFault(Reason.RECEIPT_INVALID) from error
            if receipt.namespace != namespace:
                raise RuntimeFault(Reason.RECEIPT_INVALID)
            match_class = self.classify(receipt, context)
            if match_class is None:
                continue
            matches.append(
                ReceiptMatch(
                    receipt_id=receipt.receipt_id,
                    match_class=match_class,
                    outcome_class=receipt.outcome_class,
                    public_tool_id=receipt.public_tool_id,
                    selected_capability=receipt.selected_capability,
                    arguments_hash=receipt.arguments_hash,
                    reason_code=receipt.reason_code,
                    admitted_evidence=receipt.delta.changed,
                    moved_disposition=receipt.delta.progressed,
                )
            )
        if not matches:
            # The namespace was read to exhaustion and nothing classified in.
            return HistoryLookupResult(status="empty")
        ordered = sorted(matches, key=_rank)
        kept = tuple(ordered[: self.max_references])
        return HistoryLookupResult(
            status="matched", matches=kept, truncated=len(ordered) > len(kept)
        )
