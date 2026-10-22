"""Risk-tiered acceptance for a read-only diagnostic choice.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest \
  tests/confidence_cascade/test_acceptance_tier.py -q

Written before the tier exists. The defect they pin: acceptance of a Decider
answer is gated by one scalar confidence floor, and that floor is doing two
unrelated jobs at once. It is the product's uncalibrated risk default, and it is
also the only thing standing between a structurally valid answer and an
argument-free local read. The whole offerable surface is read-only diagnostics,
so the second job does not need a confidence number at all, and conflating the
two is what stopped every real selection in the previous pilot before any
diagnostic ran.

The tier changes exactly one thing: what happens to a *structurally valid*
answer whose reported confidence is low. Every structural refusal still
refuses, the review option is still never acceptable, and the tier is refused
outright unless every executable option offered is read-only and argument-free.
Confidence remains recorded telemetry; it is never execution authority in this
tier, and nothing here claims it is calibrated in either tier.
"""

from uuid import uuid4

import pytest

from unstuck.cascade.cards import ToolCapabilityCard
from unstuck.cascade.choice import choice_request, validate_choice
from unstuck.cascade.matching import HistoryLookupResult
from unstuck.cascade.policy import (
    ACCEPTANCE_TIERS,
    READ_ONLY_COST_CLASSES,
    READ_ONLY_INPUT_SCHEMAS,
    STRICT_CONFIDENCE_MIN,
    Calibration,
    CascadePolicy,
)
from unstuck.cascade.projector import SelectorBudgets, project_selector_input
from unstuck.cascade.selection import (
    REVIEW_OPTION,
    RawSelection,
    acceptable,
    normalize,
)
from unstuck.domain import Reason, RuntimeFault

REVISION = "hobson-v19"
TIMEOUT_MS = 20000.0
#: Every real answer the pilot checkpoint gave sat in this band, well under the
#: product default. Recorded here as an observation about that run only.
LOW_CONFIDENCE = 0.2273


def budgets() -> SelectorBudgets:
    return SelectorBudgets(
        remaining_selector_calls=2, remaining_diagnostic_reads=2, remaining_ms=120000
    )


def request_for(context, cards, state, **changes):
    return project_selector_input(
        pass_index=1,
        context=context,
        cards=cards,
        state=state,
        history=HistoryLookupResult(status="empty"),
        budgets=budgets(),
        **changes,
    )


def raw_for(request, *, choice=None, **changes):
    """One structurally valid answer, with any field replaced by `changes`.

    `confidence` travels through `changes` rather than as its own parameter so
    a refusal case can replace it alongside the other fields it replaces.
    """
    options = request.option_ids
    selected = options[0] if choice is None else choice
    if selected in options:
        share = (1.0 - 0.4) / (len(options) - 1)
        weights = {option: 0.4 if option == selected else share for option in options}
    else:
        # An answer naming something outside the offered set still carries a
        # well formed distribution over the offered set, so membership is the
        # only clause that refuses it.
        weights = dict.fromkeys(options, 1.0 / len(options))
    probabilities = tuple(
        {"id": option, "probability": weights[option]} for option in options
    )
    return RawSelection.model_validate(
        {
            "model_revision": REVISION,
            "decision_id": uuid4(),
            "model_call_id": uuid4(),
            "pass_index": request.pass_index,
            "candidate_ids": options,
            "validation_status": "valid",
            "provider": "decider",
            "selected_id": selected,
            "probabilities": probabilities,
            "confidence": LOW_CONFIDENCE,
        }
        | changes
    )


def test_the_two_tiers_are_the_declared_closed_set():
    assert ACCEPTANCE_TIERS == ("strict_confidence", "read_only_structural")
    assert STRICT_CONFIDENCE_MIN == 0.80


def test_the_product_default_is_the_strict_tier_at_the_uncalibrated_floor():
    calibration = Calibration()
    assert calibration.acceptance_tier == "strict_confidence"
    assert calibration.first_confidence_min == STRICT_CONFIDENCE_MIN
    assert calibration.final_confidence_min == STRICT_CONFIDENCE_MIN
    assert calibration.label == "bootstrap_unmeasured"


def test_the_strict_tier_still_refuses_a_low_confidence_answer(context, cards, state):
    request = request_for(context, cards, state)
    raw = raw_for(request)
    assert acceptable(raw, request, 0.80, tier="strict_confidence") is False
    record = normalize(raw, request, threshold=0.80, tier="strict_confidence")
    assert record.accepted is False
    assert record.rejection is Reason.FIRST_REJECTED
    assert record.confidence == pytest.approx(LOW_CONFIDENCE)


def test_the_read_only_tier_accepts_a_structurally_valid_low_confidence_read(
    context, cards, state
):
    request = request_for(context, cards, state)
    raw = raw_for(request)
    assert acceptable(raw, request, 0.80, tier="read_only_structural") is True
    record = normalize(raw, request, threshold=0.80, tier="read_only_structural")
    assert record.accepted is True
    assert record.rejection is None
    assert record.selected_id == request.option_ids[0]
    # Confidence survives as recorded telemetry; it gated nothing.
    assert record.confidence == pytest.approx(LOW_CONFIDENCE)


def test_the_read_only_tier_ignores_the_threshold_entirely(context, cards, state):
    request = request_for(context, cards, state)
    raw = raw_for(request, confidence=0.0)
    for threshold in (0.0, 0.32, 0.80, 1.0):
        assert acceptable(raw, request, threshold, tier="read_only_structural") is True


@pytest.mark.parametrize("tier", ACCEPTANCE_TIERS)
def test_the_review_option_is_never_acceptable_in_any_tier(context, cards, state, tier):
    request = request_for(context, cards, state)
    raw = raw_for(request, choice=REVIEW_OPTION, confidence=1.0)
    assert acceptable(raw, request, 0.0, tier=tier) is False


@pytest.mark.parametrize("tier", ACCEPTANCE_TIERS)
@pytest.mark.parametrize(
    "case",
    [
        "malformed_answer",
        "provider_error",
        "unavailable_option",
        "unknown_capability",
        "wrong_option_set",
        "reordered_option_set",
        "probabilities_do_not_sum",
        "probabilities_wrong_keys",
        "probabilities_absent",
        "confidence_absent",
        "no_choice",
    ],
)
def test_every_structural_refusal_still_refuses_in_both_tiers(
    context, cards, state, make_context, tier, case
):
    request = request_for(context, cards, state)
    options = request.option_ids
    changes: dict[str, object] = {"confidence": 1.0}
    choice = None
    if case == "malformed_answer":
        changes = {
            "validation_status": "invalid",
            "selected_id": None,
            "probabilities": None,
            "confidence": None,
        }
    elif case == "provider_error":
        changes = {
            "validation_status": "error",
            "selected_id": None,
            "probabilities": None,
            "confidence": None,
        }
    elif case == "unavailable_option":
        # Offered only the first two tools, yet the answer names the third.
        narrowed = make_context(
            state,
            candidates=tuple(
                {
                    "public_tool_id": probe.id,
                    "description": probe.description,
                    "available": index < 2,
                }
                for index, probe in enumerate(state.candidates)
            ),
        )
        request = request_for(narrowed, cards, state)
        choice = options[2]
        changes = {"candidate_ids": request.option_ids}
    elif case == "unknown_capability":
        choice = "run_arbitrary_sql"
    elif case == "wrong_option_set":
        changes = {"candidate_ids": options[:-1]}
    elif case == "reordered_option_set":
        changes = {"candidate_ids": (options[1], options[0]) + options[2:]}
    elif case == "probabilities_do_not_sum":
        changes = {
            "probabilities": tuple(
                {"id": option, "probability": 0.9} for option in options
            )
        }
    elif case == "probabilities_wrong_keys":
        changes = {
            "probabilities": tuple(
                {"id": option, "probability": 1.0 / len(options)}
                for option in reversed(options)
            )
        }
    elif case == "probabilities_absent":
        changes = {"probabilities": None}
    elif case == "confidence_absent":
        changes = {"confidence": None}
    else:
        changes = {"selected_id": None}
    raw = raw_for(request, choice=choice, **changes)
    assert acceptable(raw, request, 0.0, tier=tier) is False
    record = normalize(raw, request, threshold=0.0, tier=tier)
    assert record.accepted is False
    assert record.rejection is Reason.FIRST_REJECTED


@pytest.mark.parametrize("tier", ACCEPTANCE_TIERS)
def test_a_wrong_revision_is_refused_at_the_validator_in_both_tiers(
    context, cards, state, tier
):
    payload = choice_request(request_for(context, cards, state))
    options = payload.option_ids
    share = 1.0 / len(options)
    answer = {
        "model_revision": "some-other-revision",
        "model_call_id": uuid4(),
        "choice": options[0],
        "probabilities": dict.fromkeys(options, share),
        "confidence": LOW_CONFIDENCE,
        "latency_ms": 1400.0,
    }
    from unstuck.cascade.choice import ProviderChoice

    outcome = validate_choice(
        ProviderChoice.model_validate(answer),
        payload,
        expected_revision=REVISION,
        timeout_ms=TIMEOUT_MS,
    )
    assert outcome.validation_status == "invalid"
    assert outcome.selected_id is None
    assert outcome.confidence is None


def test_the_read_only_tier_declares_its_own_safety_precondition():
    assert READ_ONLY_INPUT_SCHEMAS == frozenset({"no_arguments"})
    assert READ_ONLY_COST_CLASSES == frozenset({"local_read"})


def test_the_read_only_tier_refuses_an_option_surface_that_is_not_read_only(
    context, cards, state
):
    """The tier's reasoning only holds while every option is an argument-free
    local read, so the guard is structural rather than a comment. A card whose
    published surface leaves that set cannot be accepted under the tier at any
    confidence, and the strict tier is unaffected by the surface at all.
    """
    request = request_for(context, cards, state)
    offered = request.cards[0]
    # `model_construct` bypasses validation deliberately: the shipped contract
    # pins both fields to one literal, and this proves the tier would refuse a
    # widened contract rather than inherit it.
    widened = ToolCapabilityCard.model_construct(
        **offered.model_dump() | {"input_schema": "free_text", "cost_class": "mutating"}
    )
    unsafe = request.model_copy(update={"cards": (widened,) + request.cards[1:]})
    raw = raw_for(unsafe, choice=offered.public_tool_id, confidence=1.0)
    assert acceptable(raw, unsafe, 0.0, tier="read_only_structural") is False
    assert acceptable(raw, unsafe, 0.0, tier="strict_confidence") is True


def test_an_option_with_no_published_card_cannot_be_accepted_under_the_tier(
    context, cards, state
):
    request = request_for(context, cards, state)
    stripped = request.model_copy(update={"cards": request.cards[:1]})
    raw = raw_for(stripped, choice=request.option_ids[1], confidence=1.0)
    assert acceptable(raw, stripped, 0.0, tier="read_only_structural") is False
    # The same payload cannot even be offered to a provider.
    with pytest.raises(RuntimeFault) as captured:
        choice_request(stripped)
    assert captured.value.reason == Reason.INVARIANT


def test_the_tier_is_frozen_into_the_bound_versions_of_every_receipt():
    """A tier change is a changed current context, never a silent reuse."""
    strict = CascadePolicy(baseline_id="decider_memory", model_revision=REVISION)
    relaxed = CascadePolicy(
        baseline_id="decider_memory",
        model_revision=REVISION,
        calibration=Calibration(acceptance_tier="read_only_structural"),
    )
    assert strict.acceptance_tier == "strict_confidence"
    assert relaxed.acceptance_tier == "read_only_structural"
    assert relaxed.calibration.calibration_hash != strict.calibration.calibration_hash
    assert relaxed.policy_hash != strict.policy_hash
    assert relaxed.versions("v17") != strict.versions("v17")
