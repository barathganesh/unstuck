"""The typed Decider seam and its normalized record.

Decider 2B is the only model tool selector. It answers one typed question per
call and returns a candidate distribution. Nothing it returns is authority: the
application recomputes acceptance from the offered options, the configured
threshold, and the probability shape. Provider text is not part of the contract
at all, so there is no prose that could reach an authority field.
"""

from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, model_validator

from unstuck.cascade.policy import (
    READ_ONLY_COST_CLASSES,
    READ_ONLY_INPUT_SCHEMAS,
    AcceptanceTier,
)
from unstuck.cascade.projector import FirstChoice, SelectorInput
from unstuck.cascade.serialization import StageMeasurement, canonical_digest
from unstuck.decision import OptionProbability
from unstuck.domain import (
    PROVIDERS as PROVIDERS,
)
from unstuck.domain import (
    Contract,
    Digest,
    Identifier,
    Probability,
    Reason,
    RuntimeFault,
)
from unstuck.domain import (
    Provider as Provider,
)

Probabilities = Annotated[tuple[OptionProbability, ...], Field(max_length=4)] | None
ValidationStatus = Literal["valid", "invalid", "error"]
REVIEW_OPTION = "request_review"
# `Provider` and `PROVIDERS` are re-exported above under the names the selector
# seam has always used. The definition lives in `unstuck.domain` because the
# run record, the durable events, and the terminal result carry the identity
# too, and those contracts sit below the cascade. It remains evidence about the
# call and never authority: acceptance is still recomputed from the offered
# options, the threshold and the probability shape.


class RawSelection(Contract):
    """One normalized Decider response or typed failure."""

    model_revision: Identifier
    decision_id: UUID
    model_call_id: UUID
    pass_index: Literal[1, 2]
    candidate_ids: Annotated[tuple[Identifier, ...], Field(max_length=4)]
    validation_status: ValidationStatus
    provider: Provider = "mock"
    selected_id: Identifier | None = None
    probabilities: Probabilities = None
    confidence: Probability | None = None
    latency_ms: StageMeasurement = StageMeasurement()
    cost: StageMeasurement = StageMeasurement()
    token_usage: StageMeasurement = StageMeasurement(availability="not_provided")


class SelectionRecord(Contract):
    """The application's normalized view of one selector call."""

    pass_index: Literal[1, 2]
    decision_id: UUID
    model_call_id: UUID
    model_revision: Identifier
    candidate_ids: Annotated[tuple[Identifier, ...], Field(max_length=4)]
    validation_status: ValidationStatus
    accepted: bool
    provider: Provider = "mock"
    selector_input_hash: Digest
    decision_context_hash: Digest
    selected_id: Identifier | None = None
    probabilities: Probabilities = None
    confidence: Probability | None = None
    rejection: Reason | None = None
    latency_ms: StageMeasurement = StageMeasurement()
    cost: StageMeasurement = StageMeasurement()
    token_usage: StageMeasurement = StageMeasurement(availability="not_provided")

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.accepted != (self.rejection is None):
            raise ValueError("an accepted selection carries no rejection reason")
        if self.accepted and (
            self.selected_id in {None, REVIEW_OPTION} or self.confidence is None
        ):
            raise ValueError("accepted selection missing choice or confidence")
        return self

    @property
    def record_hash(self) -> str:
        return canonical_digest(self)


class Decider(Protocol):
    async def select(self, request: SelectorInput) -> RawSelection: ...


def rejection_for(pass_index: int) -> Reason:
    return Reason.FIRST_REJECTED if pass_index == 1 else Reason.FINAL_REJECTED


def first_choice(record: SelectionRecord) -> FirstChoice:
    """Project the accepted first record for the one second decision.

    The projection is evidence about the first call and carries no authority:
    the second decision still chooses from the current candidates itself.
    """
    if record.pass_index != 1:
        raise RuntimeFault(Reason.INVARIANT)
    return FirstChoice(
        decision_id=record.decision_id,
        selected_id=record.selected_id,
        confidence=record.confidence,
        probabilities=record.probabilities,
        validation_status=record.validation_status,
        accepted=record.accepted,
        selector_input_hash=record.selector_input_hash,
        decision_context_hash=record.decision_context_hash,
    )


def read_only_surface(request: SelectorInput) -> bool:
    """Whether every executable option offered is an argument-free local read.

    This is the precondition the relaxed tier's reasoning rests on, re-derived
    from the published cards rather than assumed from the contract's current
    literals. An option with no published card, or one whose published surface
    leaves either declared set, makes the surface unsafe for that tier.
    """
    published = {card.public_tool_id: card for card in request.cards}
    for identifier in request.option_ids[:-1]:
        card = published.get(identifier)
        if card is None:
            return False
        if card.input_schema not in READ_ONLY_INPUT_SCHEMAS:
            return False
        if card.cost_class not in READ_ONLY_COST_CLASSES:
            return False
    return True


def acceptable(
    raw: RawSelection,
    request: SelectorInput,
    threshold: float,
    *,
    tier: AcceptanceTier = "strict_confidence",
) -> bool:
    """Recompute acceptance from the offered options alone.

    Structural validity is a hard precondition in every tier and no tier
    weakens any clause of it: the answer must be valid, name one currently
    offered executable option, carry a distribution over exactly the offered
    options that sums to one, and carry a finite confidence. The review option
    is excluded by membership, so it is never acceptable under any tier.

    The tier decides only what happens to an answer that already passed all of
    that. `strict_confidence` additionally requires the configured floor.
    `read_only_structural` requires instead that every executable option
    offered is an argument-free local read, and then accepts whatever
    confidence was reported. Model confidence can never supply a missing fact
    in either tier; in the relaxed tier it is recorded telemetry only.
    """
    # `SelectorInput` already guarantees the options are distinct and exclude
    # the review option, so acceptance only has to compare against them.
    options = request.option_ids
    if raw.validation_status != "valid" or raw.confidence is None:
        return False
    if raw.candidate_ids != options or raw.selected_id not in options[:-1]:
        return False
    if (
        raw.probabilities is None
        or tuple(item.id for item in raw.probabilities) != options
    ):
        return False
    if abs(sum(item.probability for item in raw.probabilities) - 1.0) > 0.001:
        return False
    if tier == "read_only_structural":
        return read_only_surface(request)
    return raw.confidence >= threshold


def normalize(
    raw: RawSelection,
    request: SelectorInput,
    *,
    threshold: float,
    tier: AcceptanceTier = "strict_confidence",
) -> SelectionRecord:
    """Normalize one response into the record the controller keeps."""
    accepted = acceptable(raw, request, threshold, tier=tier)
    return SelectionRecord(
        pass_index=request.pass_index,
        decision_id=raw.decision_id,
        model_call_id=raw.model_call_id,
        model_revision=raw.model_revision,
        candidate_ids=raw.candidate_ids,
        selected_id=raw.selected_id,
        probabilities=raw.probabilities,
        confidence=raw.confidence,
        validation_status=raw.validation_status,
        accepted=accepted,
        # Recorded as reported, so the durable trace says which provider
        # answered. It gates nothing: acceptance was already recomputed above.
        provider=raw.provider,
        rejection=None if accepted else rejection_for(request.pass_index),
        selector_input_hash=request.input_hash,
        decision_context_hash=request.decision_context_hash,
        latency_ms=raw.latency_ms,
        cost=raw.cost,
        token_usage=raw.token_usage,
    )
