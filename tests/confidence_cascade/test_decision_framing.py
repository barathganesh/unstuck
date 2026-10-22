"""How the second decision is framed, and what it is allowed to be framed from.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest \
  tests/confidence_cascade/test_decision_framing.py -q

Written before the framing exists. The defect they pin: an offered criterion is
`f"{card.description}. Observes: {facts}."`, which states what a diagnostic
reads and never states the condition under which reading it distinguishes
anything. The question asked is which read would best distinguish the remaining
explanations for an empty result, and no option answers that question, so the
choice falls back on description similarity. That is why the previous pilot's
checkpoint chose the same tool on every first pass and the same other tool on
every second pass, across every case class.

The fix is a condition sentence published on the card itself, so the criteria
stay derived from the public catalog plus runtime-safe current facts. These
tests pin what that sentence may not contain - another tool's identity, a case
class, a category or scenario token, an expected status or action, or any
evaluator fact - and pin that a second decision's payload carries the verified
first result and the exact refused reason inside the 8,000 character bound.
"""

import json
from uuid import uuid4

import pytest

from unstuck.cascade.cards import ToolCapabilityCard, capability_cards
from unstuck.cascade.choice import (
    APPLIES_WHEN_PREFIX,
    REVIEW_CRITERION,
    choice_request,
    criterion_for,
)
from unstuck.cascade.matching import HistoryLookupResult, ReceiptMatch
from unstuck.cascade.projector import (
    BlockedExecution,
    SelectorBudgets,
    project_selector_input,
)
from unstuck.cascade.selection import REVIEW_OPTION, SelectionRecord, first_choice
from unstuck.cascade.serialization import SELECTOR_INPUT_MAX_CHARS
from unstuck.decision import OptionProbability, default_catalog
from unstuck.domain import Reason

#: Tokens only the evaluator may ever hold. None may appear in a model input.
FORBIDDEN_TOKENS = (
    "kappa-11",
    "kappa-27",
    "kappa-43",
    "kappa-58",
    "repair",
    "wait",
    "valid_empty",
    "accept_empty",
    "healthy",
    "empty_verified",
    "expected",
    "correct",
    "acceptable",
    "answer",
    "truth",
    "holdout",
    "label",
)
#: The option identifiers and canonical capabilities a criterion may not name.
TOOL_TOKENS = tuple(probe.public.id for probe in default_catalog()) + tuple(
    probe.capability for probe in default_catalog()
)


def budgets(**changes) -> SelectorBudgets:
    return SelectorBudgets.model_validate(
        {
            "remaining_selector_calls": 2,
            "remaining_diagnostic_reads": 2,
            "remaining_ms": 120000,
        }
        | changes
    )


def test_every_card_publishes_its_own_condition_sentence():
    for binding in capability_cards():
        condition = binding.card.applies_when
        assert isinstance(condition, str)
        assert condition.strip()
        assert len(condition) <= 256


def test_the_condition_is_rendered_into_the_criterion_as_condition_language(
    context, cards, state
):
    payload = choice_request(
        project_selector_input(
            pass_index=1,
            context=context,
            cards=cards,
            state=state,
            history=HistoryLookupResult(status="empty"),
            budgets=budgets(),
        )
    )
    published = {binding.card.public_tool_id: binding.card for binding in cards}
    for option in payload.options[:-1]:
        card = published[option.option_id]
        assert option.criterion == criterion_for(card)
        assert card.description in option.criterion
        assert APPLIES_WHEN_PREFIX in option.criterion
        assert card.applies_when in option.criterion
        for fact in card.observable_facts:
            assert fact in option.criterion


def test_the_condition_is_the_last_thing_each_option_line_says():
    """The frozen part order, pinned so it cannot drift back silently.

    One option is one numbered line in the provider's prompt and a pointer head
    scores it from that line's last token, so the criterion ends on the
    condition rather than on the facts. This is a property of the rendering,
    not of the option list: the offered order is untouched.
    """
    for binding in capability_cards():
        card = binding.card
        text = criterion_for(card)
        assert text.startswith(f"{card.description}.")
        assert text.endswith(f"{APPLIES_WHEN_PREFIX} {card.applies_when}.")
        assert text.index("Observes:") < text.index(APPLIES_WHEN_PREFIX)


def test_each_condition_distinguishes_its_own_option_from_the_others():
    """No two options may offer the same condition, or the sentence adds nothing."""
    conditions = [binding.card.applies_when for binding in capability_cards()]
    assert len(set(conditions)) == len(conditions)
    assert REVIEW_CRITERION not in conditions


def test_a_condition_never_names_another_tool_or_the_review_option():
    for binding in capability_cards():
        rendered = criterion_for(binding.card).lower()
        own = binding.card.public_tool_id.lower()
        for token in TOOL_TOKENS + (REVIEW_OPTION,):
            if token.lower() == own:
                continue
            assert token.lower() not in rendered


def test_a_condition_never_carries_a_case_class_category_or_evaluator_token():
    for binding in capability_cards():
        condition = binding.card.applies_when.lower()
        for token in FORBIDDEN_TOKENS:
            assert token not in condition


def test_the_criterion_is_derived_from_the_card_alone(context, cards, state):
    """A renamed catalog publishes renamed criteria and nothing shipped leaks."""
    shipped = capability_cards()[1].card
    renamed = ToolCapabilityCard(
        public_tool_id="check_key_shapes",
        description="Read the declared join encodings, their coverage, and key uniqueness",
        applies_when=(
            "the result is empty and it is not yet established whether the two "
            "sides of the join declare the same key encoding"
        ),
        observable_facts=shipped.observable_facts,
    )
    text = criterion_for(renamed)
    assert renamed.description in text
    assert renamed.applies_when in text
    assert shipped.description not in text
    assert shipped.applies_when not in text
    assert shipped.public_tool_id not in text


def test_the_condition_enters_the_published_catalog_identity():
    shipped = capability_cards()[0].card
    altered = shipped.model_copy(
        update={"applies_when": "a different published condition"}
    )
    assert altered.card_hash != shipped.card_hash


def second_decision(context, cards, state, *, blocked_class, outcome_class, reason):
    options = tuple(probe.id for probe in state.candidates) + (REVIEW_OPTION,)
    shares = (0.4, 0.3, 0.2, 0.1)
    first = SelectionRecord(
        pass_index=1,
        decision_id=uuid4(),
        model_call_id=uuid4(),
        model_revision="hobson-v19",
        candidate_ids=options,
        validation_status="valid",
        accepted=True,
        provider="decider",
        selector_input_hash="a" * 64,
        decision_context_hash=context.decision_context_hash,
        selected_id=state.candidates[0].id,
        probabilities=tuple(
            OptionProbability(id=option, probability=share)
            for option, share in zip(options, shares, strict=True)
        ),
        confidence=0.2273,
    )
    blocked = (
        BlockedExecution(
            public_tool_id=state.candidates[0].id,
            selected_capability=state.candidates[0].id,
            arguments_hash=state.query_hash,
            receipt_id="b" * 64,
            outcome_class=outcome_class,
            reason_code=reason,
            blocked_class=blocked_class,
        ),
    )
    history = HistoryLookupResult(
        status="matched",
        matches=(
            ReceiptMatch(
                receipt_id="b" * 64,
                match_class="exact",
                outcome_class=outcome_class,
                public_tool_id=state.candidates[0].id,
                selected_capability=state.candidates[0].id,
                arguments_hash=state.query_hash,
                reason_code=reason,
                admitted_evidence=True,
                moved_disposition=False,
            ),
        ),
    )
    return project_selector_input(
        pass_index=2,
        context=context,
        cards=cards,
        state=state,
        history=history,
        budgets=budgets(remaining_selector_calls=1, remaining_diagnostic_reads=2),
        blocked=blocked,
        first=first_choice(first),
    )


@pytest.mark.parametrize(
    ("blocked_class", "outcome_class", "reason"),
    [
        ("exact_repeat", "disposition_unchanged", Reason.INSUFFICIENT),
        ("failed_execution", "disposition_unchanged", Reason.INSUFFICIENT),
    ],
)
def test_the_second_decision_state_text_carries_the_first_result_and_the_reason(
    context, cards, state, blocked_class, outcome_class, reason
):
    request = second_decision(
        context,
        cards,
        state,
        blocked_class=blocked_class,
        outcome_class=outcome_class,
        reason=reason,
    )
    payload = choice_request(request)
    text = payload.state_text
    decoded = json.loads(text)
    # The verified first result, as the D1 record rather than as an answer.
    assert decoded["first"]["selected_id"] == state.candidates[0].id
    assert decoded["first"]["accepted"] is True
    assert decoded["first"]["validation_status"] == "valid"
    # The exact refused execution, with the verifier's own outcome and reason.
    refused = decoded["blocked"]
    assert len(refused) == 1
    assert refused[0]["public_tool_id"] == state.candidates[0].id
    assert refused[0]["outcome_class"] == outcome_class
    assert refused[0]["reason_code"] == reason.value
    assert refused[0]["blocked_class"] == blocked_class


def test_the_refused_tool_is_not_offered_again_on_the_second_decision(
    context, cards, state
):
    request = second_decision(
        context,
        cards,
        state,
        blocked_class="exact_repeat",
        outcome_class="disposition_unchanged",
        reason=Reason.INSUFFICIENT,
    )
    payload = choice_request(request)
    assert state.candidates[0].id not in payload.option_ids
    assert payload.option_ids[-1] == REVIEW_OPTION
    assert len(payload.option_ids) == 3


def test_the_second_decision_payload_stays_inside_the_declared_bound(
    context, cards, state
):
    request = second_decision(
        context,
        cards,
        state,
        blocked_class="exact_repeat",
        outcome_class="disposition_unchanged",
        reason=Reason.INSUFFICIENT,
    )
    payload = choice_request(request)
    rendered = payload.state_text + "".join(
        option.criterion for option in payload.options
    )
    assert len(payload.state_text) <= SELECTOR_INPUT_MAX_CHARS
    assert len(rendered) < SELECTOR_INPUT_MAX_CHARS


def test_no_model_input_carries_an_evaluator_token(context, cards, state):
    request = second_decision(
        context,
        cards,
        state,
        blocked_class="exact_repeat",
        outcome_class="disposition_unchanged",
        reason=Reason.INSUFFICIENT,
    )
    payload = choice_request(request)
    rendered = (
        payload.state_text
        + payload.question
        + "".join(option.criterion for option in payload.options)
    ).lower()
    for token in ("kappa-", "expected_status", "expected_action", "acceptable_first"):
        assert token not in rendered


def test_the_offered_order_is_the_catalog_order_and_is_not_permuted(
    context, cards, state
):
    request = project_selector_input(
        pass_index=1,
        context=context,
        cards=cards,
        state=state,
        history=HistoryLookupResult(status="empty"),
        budgets=budgets(),
    )
    payload = choice_request(request)
    published = tuple(binding.card.public_tool_id for binding in cards)
    assert payload.option_ids[:-1] == published
    assert payload.option_ids == request.option_ids
