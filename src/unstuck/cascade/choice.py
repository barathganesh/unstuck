"""The typed Choice payload a real Decider 2B call is made from, and its validator.

Decider 2B answers one Choice question per call: pick one of N named options.
The provider's Choice primitive carries a description per option, and that
description is where each option is read back from, so an option offered with
no description is offered as an opaque identifier. This module builds the
payload from the offered Tool Capability Cards instead, in exact option order,
and validates one response against the offered options alone.

Nothing here decides anything and nothing here is authority. The criteria are
derived only from each card's own published description and observable facts,
so a renamed catalog publishes renamed criteria and no private capability,
expected outcome or evaluator fact can reach a model input. The review option
carries a fixed criterion stating that it reads nothing; it is never a
candidate for execution, and `selection.acceptable` refuses it on membership.
"""

import math
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, model_validator

from unstuck.cascade.cards import ToolCapabilityCard
from unstuck.cascade.projector import QUESTION, Question, SelectorInput
from unstuck.cascade.selection import REVIEW_OPTION, Probabilities, ValidationStatus
from unstuck.cascade.serialization import SELECTOR_INPUT_MAX_CHARS, canonical_text
from unstuck.decision import OptionProbability
from unstuck.domain import (
    Contract,
    Identifier,
    Probability,
    Reason,
    RuntimeFault,
)

#: The published card description is capped at 512 characters, its condition at
#: 256, and at most 8 observable facts travel with it, so one rendered
#: criterion is bounded too.
CRITERION_MAX_CHARS = 1280
#: The fixed lead-in that marks the card's published condition as a condition
#: rather than as more description. The question asked is which read would best
#: distinguish the remaining explanations, and a criterion that only says what
#: a tool reads does not answer it.
APPLIES_WHEN_PREFIX = "Useful when"
#: Tolerance on the probability sum, as the acceptance rule applies it.
SUM_TOLERANCE = 0.001
#: The fixed criterion for the review option. It names no tool, observes
#: nothing, and says so, so a selector cannot read it as a cheap diagnostic.
REVIEW_CRITERION = (
    "Escalate to a human reviewer. This option reads nothing and observes no "
    "fact; choosing it ends the run without a diagnostic result."
)


def criterion_for(card: ToolCapabilityCard) -> str:
    """Render one option's criterion from that card's published surface alone.

    Three parts, in one order: what the read does, the facts it admits, and
    last the published condition under which admitting them distinguishes
    something. All three come from this card and nothing else, so a renamed
    catalog publishes renamed criteria and no private capability, expected
    outcome or evaluator fact can reach a model input.

    The condition goes last on purpose, and the reason is the provider's own
    prompt rather than a preference. One option is rendered as exactly one
    numbered line, and a pointer head scores that option from the hidden state
    at the line's last token, so what the line ends on is what the head has
    most recently read. Ending on the facts ends on the part that says least
    about which read would distinguish anything. The order is applied
    identically to every option, so it changes what is said inside a line and
    never the order the options are offered in.
    """
    facts = ", ".join(card.observable_facts)
    return (
        f"{card.description}. "
        f"Observes: {facts}. "
        f"{APPLIES_WHEN_PREFIX} {card.applies_when}."
    )


class ChoiceOption(Contract):
    """One rendered option: its identifier, its criterion, and whether it reads."""

    option_id: Identifier
    criterion: Annotated[str, Field(min_length=1, max_length=CRITERION_MAX_CHARS)]
    executable: bool


class ChoiceRequest(Contract):
    """The complete bounded Choice payload for one Decider call."""

    question: Question = QUESTION
    state_text: Annotated[str, Field(min_length=1, max_length=SELECTOR_INPUT_MAX_CHARS)]
    options: Annotated[tuple[ChoiceOption, ...], Field(min_length=2, max_length=4)]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        identifiers = tuple(option.option_id for option in self.options)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate option identities")
        if identifiers[-1] != REVIEW_OPTION:
            raise ValueError("the review option is always offered last")
        if any(option.executable for option in self.options[-1:]):
            raise ValueError("the review option is not executable")
        if not all(option.executable for option in self.options[:-1]):
            raise ValueError("every candidate option is executable")
        return self

    @property
    def option_ids(self) -> tuple[str, ...]:
        return tuple(option.option_id for option in self.options)

    @property
    def criteria(self) -> dict[str, str]:
        """Option identifier to criterion, in exact offered order."""
        return {option.option_id: option.criterion for option in self.options}


class ProviderChoice(Contract):
    """One raw Choice answer exactly as a provider returned it.

    Nothing here is trusted. The fields are deliberately permissive so a
    malformed answer is recorded and refused rather than rejected at the parse
    boundary, where the failure class would be lost.
    """

    model_revision: Identifier
    model_call_id: UUID
    choice: Annotated[str, Field(min_length=1, max_length=128)]
    probabilities: Annotated[dict[str, float], Field(min_length=1, max_length=8)]
    confidence: float
    latency_ms: Annotated[float, Field(ge=0)]


class ChoiceOutcome(Contract):
    """One validated answer, or a typed refusal carrying no values."""

    validation_status: ValidationStatus
    selected_id: Identifier | None = None
    probabilities: Probabilities = None
    confidence: Probability | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        values = (self.selected_id, self.probabilities, self.confidence)
        if (self.validation_status == "valid") != all(
            value is not None for value in values
        ):
            raise ValueError("only a valid outcome carries a choice")
        return self


def choice_request(
    request: SelectorInput, *, max_chars: int = SELECTOR_INPUT_MAX_CHARS
) -> ChoiceRequest:
    """Build the Choice payload for one selector input. Fails closed.

    An option with no published card would have to be offered with a null
    criterion, which is an opaque identifier from the provider's side, so the
    projection refuses rather than offer it.
    """
    published = {card.public_tool_id: card for card in request.cards}
    options: list[ChoiceOption] = []
    for identifier in request.option_ids:
        if identifier == REVIEW_OPTION:
            options.append(
                ChoiceOption(
                    option_id=identifier,
                    criterion=REVIEW_CRITERION,
                    executable=False,
                )
            )
            continue
        card = published.get(identifier)
        if card is None:
            raise RuntimeFault(Reason.INVARIANT)
        options.append(
            ChoiceOption(
                option_id=identifier,
                criterion=criterion_for(card),
                executable=True,
            )
        )
    try:
        state_text = canonical_text(request, max_chars)
    except ValueError as error:
        raise RuntimeFault(Reason.INVARIANT) from error
    try:
        return ChoiceRequest(
            question=request.question,
            state_text=state_text,
            options=tuple(options),
        )
    except ValueError as error:
        raise RuntimeFault(Reason.INVARIANT) from error


def _well_formed(
    answer: ProviderChoice,
    request: ChoiceRequest,
    *,
    expected_revision: str,
    expected_call_id: UUID | None,
) -> bool:
    options = request.option_ids
    if answer.model_revision != expected_revision:
        return False
    if expected_call_id is not None and answer.model_call_id != expected_call_id:
        return False
    if answer.choice not in options:
        return False
    if set(answer.probabilities) != set(options):
        return False
    values = [answer.probabilities[option] for option in options]
    if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in values):
        return False
    if abs(sum(values) - 1.0) > SUM_TOLERANCE:
        return False
    return math.isfinite(answer.confidence) and 0.0 <= answer.confidence <= 1.0


def validate_choice(
    answer: ProviderChoice | None,
    request: ChoiceRequest,
    *,
    expected_revision: str,
    timeout_ms: float,
    expected_call_id: UUID | None = None,
) -> ChoiceOutcome:
    """Validate one response against the offered options alone.

    A transport failure and an answer that arrived after the provider budget
    are both `error`: nothing was learned about the model's shape. A malformed
    or out-of-contract answer is `invalid`, which is a measurement about the
    answer. Neither carries a value forward.
    """
    if answer is None:
        return ChoiceOutcome(validation_status="error")
    if answer.latency_ms > timeout_ms:
        return ChoiceOutcome(validation_status="error")
    if not _well_formed(
        answer,
        request,
        expected_revision=expected_revision,
        expected_call_id=expected_call_id,
    ):
        return ChoiceOutcome(validation_status="invalid")
    return ChoiceOutcome(
        validation_status="valid",
        selected_id=answer.choice,
        probabilities=tuple(
            OptionProbability(id=option, probability=answer.probabilities[option])
            for option in request.option_ids
        ),
        confidence=answer.confidence,
    )
