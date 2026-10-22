"""Step 6b: the typed Choice seam between a selector payload and a Decider.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_choice.py -q

These are written before the seam exists. The defect they pin is an absence:
nothing turned a `SelectorInput` into a per-option Choice payload, so every
qualification run handed the provider bare opaque identifiers and a null
criterion per option.
"""

from uuid import uuid4

import pytest

from unstuck.cascade.cards import ToolCapabilityCard, capability_cards
from unstuck.cascade.choice import (
    REVIEW_CRITERION,
    ChoiceOutcome,
    ProviderChoice,
    choice_request,
    criterion_for,
    validate_choice,
)
from unstuck.cascade.matching import HistoryLookupResult
from unstuck.cascade.projector import (
    QUESTION,
    SelectorBudgets,
    project_selector_input,
)
from unstuck.cascade.selection import REVIEW_OPTION, RawSelection, acceptable
from unstuck.cascade.serialization import SELECTOR_INPUT_MAX_CHARS
from unstuck.domain import Reason, RuntimeFault

REVISION = "hobson-v19"
TIMEOUT_MS = 20000.0


def budgets() -> SelectorBudgets:
    return SelectorBudgets(
        remaining_selector_calls=2, remaining_diagnostic_reads=2, remaining_ms=120000
    )


def request_for(context, cards, state):
    return project_selector_input(
        pass_index=1,
        context=context,
        cards=cards,
        state=state,
        history=HistoryLookupResult(status="empty"),
        budgets=budgets(),
    )


def answer_for(payload, choice, probabilities, **changes) -> ProviderChoice:
    return ProviderChoice.model_validate(
        {
            "model_revision": REVISION,
            "model_call_id": uuid4(),
            "choice": choice,
            "probabilities": probabilities,
            "confidence": 0.9,
            "latency_ms": 1400.0,
        }
        | changes
    )


def uniform(payload) -> dict[str, float]:
    share = 1.0 / len(payload.option_ids)
    return {option: share for option in payload.option_ids}


def test_criteria_come_from_the_offered_cards_in_exact_option_order(
    context, cards, state
):
    payload = choice_request(request_for(context, cards, state))
    request = request_for(context, cards, state)
    assert payload.option_ids == request.option_ids
    assert payload.question == QUESTION
    published = {card.public_tool_id: card for card in request.cards}
    for option in payload.options[:-1]:
        card = published[option.option_id]
        assert option.criterion == criterion_for(card)
        assert card.description in option.criterion
        for fact in card.observable_facts:
            assert fact in option.criterion
        assert option.executable is True


def test_every_option_carries_a_criterion_and_none_is_null(context, cards, state):
    payload = choice_request(request_for(context, cards, state))
    criteria = payload.criteria
    assert list(criteria) == list(payload.option_ids)
    assert all(isinstance(text, str) and text.strip() for text in criteria.values())


def test_the_review_option_is_last_fixed_and_non_executable(context, cards, state):
    request = request_for(context, cards, state)
    payload = choice_request(request)
    review = payload.options[-1]
    assert review.option_id == REVIEW_OPTION
    assert review.criterion == REVIEW_CRITERION
    assert review.executable is False
    assert all(option.executable for option in payload.options[:-1])
    assert request.available_tool_ids == payload.option_ids[:-1]
    assert REVIEW_OPTION not in request.available_tool_ids


def test_a_review_selection_is_well_formed_yet_never_acceptable(context, cards, state):
    request = request_for(context, cards, state)
    payload = choice_request(request)
    options = payload.option_ids
    probabilities = {option: 0.1 for option in options[:-1]}
    probabilities[REVIEW_OPTION] = 1.0 - 0.1 * (len(options) - 1)
    outcome = validate_choice(
        answer_for(payload, REVIEW_OPTION, probabilities),
        payload,
        expected_revision=REVISION,
        timeout_ms=TIMEOUT_MS,
    )
    assert outcome.validation_status == "valid"
    assert outcome.selected_id == REVIEW_OPTION
    raw = RawSelection(
        model_revision=REVISION,
        decision_id=uuid4(),
        model_call_id=uuid4(),
        pass_index=1,
        candidate_ids=options,
        validation_status=outcome.validation_status,
        selected_id=outcome.selected_id,
        probabilities=outcome.probabilities,
        confidence=outcome.confidence,
    )
    assert acceptable(raw, request, 0.0) is False


def test_an_option_without_a_published_card_fails_closed(context, cards, state):
    request = request_for(context, cards, state)
    stripped = request.model_copy(update={"cards": request.cards[:1]})
    with pytest.raises(RuntimeFault) as captured:
        choice_request(stripped)
    assert captured.value.reason == Reason.INVARIANT


def test_a_renamed_card_yields_only_its_own_published_wording():
    shipped = capability_cards()[1].card
    renamed = ToolCapabilityCard(
        public_tool_id="check_key_shapes",
        description="Read the declared join encodings, their coverage, and key uniqueness",
        applies_when="it is still open whether the two sides agree on key shape",
        observable_facts=shipped.observable_facts,
    )
    text = criterion_for(renamed)
    assert renamed.description in text
    assert renamed.applies_when in text
    assert shipped.description not in text
    assert shipped.applies_when not in text
    assert shipped.public_tool_id not in text


def test_the_state_text_is_bounded_and_overflow_fails_closed(context, cards, state):
    request = request_for(context, cards, state)
    payload = choice_request(request)
    assert 0 < len(payload.state_text) <= SELECTOR_INPUT_MAX_CHARS
    with pytest.raises(RuntimeFault) as captured:
        choice_request(request, max_chars=len(payload.state_text) - 1)
    assert captured.value.reason == Reason.INVARIANT


def test_probabilities_are_returned_in_exact_option_order(context, cards, state):
    payload = choice_request(request_for(context, cards, state))
    options = payload.option_ids
    scrambled = {option: uniform(payload)[option] for option in reversed(options)}
    outcome = validate_choice(
        answer_for(payload, options[0], scrambled),
        payload,
        expected_revision=REVISION,
        timeout_ms=TIMEOUT_MS,
    )
    assert outcome.validation_status == "valid"
    assert outcome.probabilities is not None
    assert tuple(item.id for item in outcome.probabilities) == options


@pytest.mark.parametrize(
    "case",
    [
        "not_a_member",
        "missing_option",
        "extra_option",
        "not_finite",
        "out_of_range",
        "sum_too_low",
        "sum_too_high",
        "confidence_out_of_range",
        "confidence_not_finite",
        "revision_mismatch",
        "call_identity_mismatch",
    ],
)
def test_malformed_answers_are_invalid_rather_than_trusted(context, cards, state, case):
    payload = choice_request(request_for(context, cards, state))
    options = payload.option_ids
    probabilities = uniform(payload)
    choice = options[0]
    changes: dict[str, object] = {}
    expected_call_id = None
    if case == "not_a_member":
        choice = "inspect_everything"
    elif case == "missing_option":
        probabilities = {k: v for k, v in probabilities.items() if k != options[-1]}
    elif case == "extra_option":
        probabilities = probabilities | {"inspect_everything": 0.0}
    elif case == "not_finite":
        probabilities = probabilities | {choice: float("inf")}
    elif case == "out_of_range":
        probabilities = probabilities | {choice: -0.25}
    elif case == "sum_too_low":
        probabilities = {k: v * 0.5 for k, v in probabilities.items()}
    elif case == "sum_too_high":
        probabilities = {k: v * 1.5 for k, v in probabilities.items()}
    elif case == "confidence_out_of_range":
        changes["confidence"] = 1.5
    elif case == "confidence_not_finite":
        changes["confidence"] = float("nan")
    elif case == "revision_mismatch":
        changes["model_revision"] = "some-other-revision"
    else:
        expected_call_id = uuid4()
    outcome = validate_choice(
        answer_for(payload, choice, probabilities, **changes),
        payload,
        expected_revision=REVISION,
        timeout_ms=TIMEOUT_MS,
        expected_call_id=expected_call_id,
    )
    assert outcome.validation_status == "invalid"
    assert outcome.selected_id is None
    assert outcome.probabilities is None
    assert outcome.confidence is None


def test_sum_tolerance_accepts_the_contract_tolerance(context, cards, state):
    payload = choice_request(request_for(context, cards, state))
    options = payload.option_ids
    probabilities = uniform(payload)
    probabilities[options[0]] += 0.0009
    outcome = validate_choice(
        answer_for(payload, options[0], probabilities),
        payload,
        expected_revision=REVISION,
        timeout_ms=TIMEOUT_MS,
    )
    assert outcome.validation_status == "valid"


def test_a_timeout_and_a_provider_failure_are_errors_not_rejections(
    context, cards, state
):
    payload = choice_request(request_for(context, cards, state))
    options = payload.option_ids
    late = validate_choice(
        answer_for(payload, options[0], uniform(payload), latency_ms=TIMEOUT_MS + 1.0),
        payload,
        expected_revision=REVISION,
        timeout_ms=TIMEOUT_MS,
    )
    assert late.validation_status == "error"
    failed = validate_choice(
        None, payload, expected_revision=REVISION, timeout_ms=TIMEOUT_MS
    )
    assert failed == ChoiceOutcome(validation_status="error")
