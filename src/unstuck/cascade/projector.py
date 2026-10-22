"""The bounded current-plus-history selector input.

Every new first decision receives the complete current authoritative identity,
the static Tool Capability Cards, the runtime availability of each card, the
remaining budgets, and a bounded set of labeled Decision Receipt references.
The selector then chooses fresh. Nothing in this payload is a decision: the
history is explicitly labeled historical evidence.

The payload is capped. Truncation removes verbose observation text first, then
drops the oldest observations, and is always marked. Scope, versions, budgets,
candidate descriptions, and the blocked-execution reason are never dropped; if
they alone cannot fit, the projection fails rather than lie.
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from unstuck.cascade.cards import CardBinding, ToolCapabilityCard
from unstuck.cascade.identity import BoundVersions, DecisionContext
from unstuck.cascade.matching import HistoryLookupResult
from unstuck.cascade.receipts import OutcomeClass
from unstuck.cascade.serialization import (
    SELECTOR_INPUT_MAX_CHARS,
    TOOL_SUMMARY_MAX_CHARS,
    bounded_text,
    canonical_digest,
    canonical_text,
)
from unstuck.decision import DecisionState, OptionProbability, PublicEvidence
from unstuck.domain import (
    Contract,
    Digest,
    Identifier,
    Probability,
    Reason,
    RuntimeFault,
)
from unstuck.events import PersistenceBoundary, Redactor

#: The one typed question. The text is spelled twice because a Literal
#: annotation must be written out; the annotation on `QUESTION` makes any drift
#: between the two a type error rather than a runtime surprise.
Question = Literal[
    "Which available diagnostic read would best distinguish the remaining "
    "explanations for this empty result, considering both the current scenario "
    "and the attached verified Decision Receipts?"
]
QUESTION: Question = (
    "Which available diagnostic read would best distinguish the remaining "
    "explanations for this empty result, considering both the current scenario "
    "and the attached verified Decision Receipts?"
)


class EvidenceSummary(Contract):
    """One bounded observation. Verbose text is capped, never silently cut."""

    evidence_id: Identifier
    kind: Identifier
    public_tool_id: Identifier
    dataset_version: Identifier
    arguments_hash: Digest
    summary: Annotated[str, Field(max_length=TOOL_SUMMARY_MAX_CHARS)]
    truncated: bool = False


class SelectorBudgets(Contract):
    """What remains. Counted before the call, including failures."""

    remaining_selector_calls: Annotated[int, Field(ge=0, le=2)]
    remaining_diagnostic_reads: Annotated[int, Field(ge=0, le=2)]
    remaining_ms: Annotated[int, Field(ge=0, le=120000)]


class BlockedExecution(Contract):
    """One execution the controller has refused, with its verifier-owned proof.

    For a failed execution the receipt named here is the one this call just
    admitted, so the second decision receives the new verifier-owned receipt
    as labeled same-call history. For an exact repetition it is the matching
    historical receipt that refused the execution, before any selector call:
    the execution is precluded from the offer rather than chosen and refused.
    """

    public_tool_id: Identifier
    selected_capability: Identifier
    arguments_hash: Digest
    receipt_id: Digest
    outcome_class: OutcomeClass
    reason_code: Reason
    blocked_class: Literal["exact_repeat", "failed_execution"]


class FirstChoice(Contract):
    """The first decision's own record, as evidence for the second decision.

    This is the D1 record, not an answer. It states what the first decision
    chose, how confident it reported being, whether the application accepted
    it, and which inputs it was bound to. Nothing here selects a tool.
    """

    decision_id: UUID
    pass_index: Literal[1] = 1
    selected_id: Identifier | None
    confidence: Probability | None
    probabilities: Annotated[tuple[OptionProbability, ...], Field(max_length=4)] | None
    validation_status: Literal["valid", "invalid", "error"]
    accepted: bool
    selector_input_hash: Digest
    decision_context_hash: Digest


class SelectorInput(Contract):
    """The complete bounded payload for one Decider call."""

    pass_index: Literal[1, 2]
    task_hash: Digest
    query_hash: Digest
    base_query_hash: Digest
    dataset_id: Identifier
    versions: BoundVersions
    problem_fingerprint: Digest
    decision_context_hash: Digest
    cards: Annotated[tuple[ToolCapabilityCard, ...], Field(min_length=1, max_length=3)]
    available_tool_ids: Annotated[
        tuple[Identifier, ...], Field(min_length=1, max_length=3)
    ]
    history: HistoryLookupResult
    budgets: SelectorBudgets
    observations: Annotated[tuple[EvidenceSummary, ...], Field(max_length=4)] = ()
    blocked: Annotated[tuple[BlockedExecution, ...], Field(max_length=2)] = ()
    #: Exact repetitions removed from this offer before the call, each with the
    #: historical receipt that proves it. The decision identity above is still
    #: the full original context; only the offer is narrower.
    precluded: Annotated[tuple[BlockedExecution, ...], Field(max_length=2)] = ()
    first: FirstChoice | None = None
    question: Question = QUESTION
    review_option: Literal["request_review"] = "request_review"
    truncated: bool = False

    @model_validator(mode="after")
    def bounded(self) -> Self:
        published = tuple(card.public_tool_id for card in self.cards)
        if len(set(published)) != len(published):
            raise ValueError("duplicate published cards")
        if len(set(self.available_tool_ids)) != len(self.available_tool_ids):
            raise ValueError("duplicate available identities")
        if not set(self.available_tool_ids) <= set(published):
            raise ValueError("available tool is not published")
        if "request_review" in self.available_tool_ids:
            raise ValueError("review option is not a candidate")
        if bool(self.blocked) != (self.pass_index == 2):
            raise ValueError("a second decision requires exactly one blocked reason")
        if (self.first is not None) != (self.pass_index == 2):
            raise ValueError("a second decision carries exactly one first record")
        if any(item.public_tool_id in self.available_tool_ids for item in self.blocked):
            raise ValueError("a blocked execution cannot remain available")
        if any(
            item.public_tool_id in self.available_tool_ids for item in self.precluded
        ):
            raise ValueError("a precluded execution cannot remain available")
        if any(item.blocked_class != "exact_repeat" for item in self.precluded):
            raise ValueError("only an exact repetition is precluded")
        canonical_text(self, SELECTOR_INPUT_MAX_CHARS)
        return self

    @model_serializer(mode="wrap")
    def _without_empty_preclusion(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        """Omit an empty preclusion, so an unblocked payload is byte-identical.

        The canonical dump is both the input hash and the model's state text.
        A call with nothing precluded therefore hashes and reads exactly as it
        did before preclusion existed.
        """
        data: dict[str, object] = handler(self)
        if not self.precluded:
            data.pop("precluded", None)
        return data

    @property
    def option_ids(self) -> tuple[str, ...]:
        return self.available_tool_ids + ("request_review",)

    @property
    def input_hash(self) -> str:
        return canonical_digest(self)


def _summary(record: PublicEvidence) -> EvidenceSummary:
    facts = (
        record.facts
        if isinstance(record.facts, dict)
        else record.facts.model_dump(mode="json")
    )
    text, truncated = bounded_text(canonical_text(facts, TOOL_SUMMARY_MAX_CHARS * 8))
    return EvidenceSummary(
        evidence_id=str(record.evidence_id),
        kind=record.kind,
        public_tool_id=record.tool_id,
        dataset_version=record.dataset_version,
        arguments_hash=record.arguments_hash,
        summary=text,
        truncated=truncated,
    )


def project_selector_input(
    *,
    pass_index: Literal[1, 2],
    context: DecisionContext,
    cards: tuple[CardBinding, ...],
    state: DecisionState,
    history: HistoryLookupResult,
    budgets: SelectorBudgets,
    blocked: tuple[BlockedExecution, ...] = (),
    precluded: tuple[BlockedExecution, ...] = (),
    first: FirstChoice | None = None,
    max_chars: int = SELECTOR_INPUT_MAX_CHARS,
    redactor: Redactor | None = None,
) -> SelectorInput:
    """Build one bounded selector payload from the current authoritative state.

    For a second decision the state is the refreshed authoritative state the
    controller projected after the first read was verified, so the payload,
    its context identity, and the receipt it leads to all describe the current
    situation rather than the one the first decision faced.

    `precluded` names exact repetitions removed before the call. They leave the
    offer, keeping the remaining candidates in their original order, and the
    context identity is unchanged.
    """
    boundary = PersistenceBoundary(redactor or Redactor())
    refused = {item.public_tool_id for item in blocked + precluded}
    available = tuple(
        identifier
        for identifier in context.available_tool_ids
        if identifier not in refused
    )
    if not available:
        raise RuntimeFault(Reason.NO_READ)
    try:
        summaries = tuple(_summary(record) for record in state.bounded_evidence)
    except ValueError as error:
        raise RuntimeFault(Reason.CONFLICT) from error
    boundary.validate(summaries, Reason.RECEIPT_INVALID)
    boundary.validate(history, Reason.RECEIPT_INVALID)
    payload: dict[str, object] = {
        "pass_index": pass_index,
        "task_hash": context.task_hash,
        "query_hash": context.query_hash,
        "base_query_hash": context.base_query_hash,
        "dataset_id": context.dataset_id,
        "versions": context.versions,
        "problem_fingerprint": context.problem_fingerprint,
        "decision_context_hash": context.decision_context_hash,
        "cards": tuple(binding.card for binding in cards),
        "available_tool_ids": available,
        "history": history,
        "budgets": budgets,
        "blocked": blocked,
        "precluded": precluded,
        "first": first,
    }
    dropped = state.truncated or any(item.truncated for item in summaries)
    kept = summaries
    while True:
        candidate = SelectorInput.model_validate(
            payload
            | {
                "observations": kept,
                "truncated": dropped or len(kept) != len(summaries),
            }
        )
        if len(canonical_text(candidate, SELECTOR_INPUT_MAX_CHARS)) <= max_chars:
            return candidate
        if not kept:
            raise RuntimeFault(Reason.INVARIANT)
        kept = kept[1:]
