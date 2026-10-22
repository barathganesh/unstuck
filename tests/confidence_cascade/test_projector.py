"""Step 6: current-plus-history selector input tests.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_projector.py -q
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from unstuck.cascade.matching import HistoryLookupResult, HistoryMatcher, ReceiptMatch
from unstuck.cascade.projector import (
    QUESTION,
    BlockedExecution,
    FirstChoice,
    SelectorBudgets,
    SelectorInput,
    project_selector_input,
)
from unstuck.cascade.serialization import (
    SELECTOR_INPUT_MAX_CHARS,
    TOOL_SUMMARY_MAX_CHARS,
    canonical_text,
)
from unstuck.decision import PublicEvidence
from unstuck.domain import Reason, RuntimeFault

DIGEST = "a" * 64


def budgets(**changes) -> SelectorBudgets:
    return SelectorBudgets.model_validate(
        {
            "remaining_selector_calls": 2,
            "remaining_diagnostic_reads": 2,
            "remaining_ms": 120000,
        }
        | changes
    )


def observation(size: int = 32, tool_id: str = "inspect_pipeline") -> PublicEvidence:
    return PublicEvidence(
        evidence_id=uuid4(),
        kind="pipeline",
        tool_id=tool_id,
        dataset_version="v17",
        arguments_hash=DIGEST,
        observed_at=datetime(2026, 10, 1, tzinfo=UTC).isoformat(),
        facts={"note": "n" * size, "scope_complete": False},
    )


def project(context, cards, state, history=None, **changes):
    return project_selector_input(
        pass_index=1,
        context=context,
        cards=cards,
        state=state,
        history=history or HistoryLookupResult(status="empty"),
        budgets=budgets(),
        **changes,
    )


def test_projector_carries_current_state_cards_availability_and_budgets(
    context, cards, state
):
    request = project(context, cards, state)
    assert request.pass_index == 1
    assert request.question == QUESTION
    assert "Decision Receipt" in request.question
    assert request.review_option == "request_review"
    assert request.task_hash == state.task_hash
    assert request.query_hash == state.query_hash
    assert request.base_query_hash == state.query.base_query_hash
    assert request.versions == context.versions
    assert request.problem_fingerprint == context.problem_fingerprint
    assert request.decision_context_hash == context.decision_context_hash
    assert [card.public_tool_id for card in request.cards] == [
        "inspect_pipeline",
        "inspect_schema_keys",
        "inspect_scope",
    ]
    assert request.available_tool_ids == (
        "inspect_pipeline",
        "inspect_schema_keys",
        "inspect_scope",
    )
    assert request.option_ids == request.available_tool_ids + ("request_review",)
    assert request.budgets == budgets()
    assert request.blocked == ()
    assert not request.truncated
    assert len(canonical_text(request)) <= SELECTOR_INPUT_MAX_CHARS


def test_projector_keeps_cards_static_while_availability_varies(
    make_context, make_state, cards
):
    state = make_state(available=("inspect_scope",))
    context = make_context(state)
    request = project(context, cards, state)
    assert len(request.cards) == 3
    assert request.available_tool_ids == ("inspect_scope",)
    assert request.option_ids == ("inspect_scope", "request_review")
    # The published catalog is identical regardless of runtime availability.
    assert (
        request.cards == project(make_context(make_state()), cards, make_state()).cards
    )
    for card in request.cards:
        assert card.catalog_version == "cards-v1"
        assert "capability" not in card.model_dump()


def test_projector_refuses_a_state_with_no_available_candidate(
    make_context, make_state, cards
):
    state = make_state(available=())
    with pytest.raises(RuntimeFault) as error:
        project(make_context(state), cards, state)
    assert error.value.reason is Reason.NO_READ


def test_projector_labels_history_and_hides_the_private_capability(
    repository, verifier, make_receipt, stalled_report, context, cards, state, namespace
):
    receipt = make_receipt(stalled_report)
    repository.put(receipt, verifier.token)
    history = HistoryMatcher(repository).lookup(namespace, context, requested=True)
    request = project(context, cards, state, history=history)
    assert request.history.status == "matched"
    assert request.history.receipt_ids == (receipt.receipt_id,)
    rendered = canonical_text(request)
    assert receipt.receipt_id in rendered
    assert "no_progress" in rendered and "exact" in rendered
    # The private canonical capability is not part of the reference projection.
    projected = [
        reference.model_dump(mode="json") for reference in request.history.references
    ]
    assert "selected_capability" not in canonical_text(projected)
    assert request.history.references[0].public_tool_id == receipt.public_tool_id
    for status in ("not_requested", "empty", "unavailable"):
        plain = project(
            context, cards, state, history=HistoryLookupResult(status=status)
        )
        assert plain.history.status == status
        assert plain.history.references == ()


def test_projector_caps_each_tool_summary_and_marks_truncation(
    make_context, make_state, cards
):
    state = make_state(bounded_evidence=(observation(size=2400),))
    context = make_context(state)
    request = project(context, cards, state)
    assert len(request.observations) == 1
    summary = request.observations[0]
    assert len(summary.summary) == TOOL_SUMMARY_MAX_CHARS
    assert summary.truncated
    assert summary.public_tool_id == "inspect_pipeline"
    assert summary.dataset_version == "v17"
    assert summary.arguments_hash == DIGEST
    # Scope, versions, budgets, and option descriptions are never dropped.
    assert request.versions == context.versions
    assert request.budgets == budgets()
    assert all(card.description for card in request.cards)
    assert request.available_tool_ids


def test_projector_drops_oldest_observations_before_authority(
    make_context, make_state, cards
):
    state = make_state(
        bounded_evidence=tuple(
            observation(size=120, tool_id="inspect_pipeline") for _ in range(4)
        )
    )
    context = make_context(state)
    full = project(context, cards, state)
    assert len(full.observations) == 4 and not full.truncated
    # A tight but still feasible cap, derived from what the payload with one
    # observation actually measures rather than hardcoded, so a change to the
    # authority floor cannot silently turn this into the fail-closed case the
    # end of this test covers separately.
    one = project(
        context,
        cards,
        state.model_copy(update={"bounded_evidence": state.bounded_evidence[-1:]}),
    )
    tight = project(
        context,
        cards,
        state,
        max_chars=len(canonical_text(one, SELECTOR_INPUT_MAX_CHARS)),
    )
    assert len(tight.observations) < 4
    assert tight.truncated
    assert (
        tight.observations
        == full.observations[len(full.observations) - len(tight.observations) :]
    )
    assert tight.cards == full.cards
    assert tight.budgets == full.budgets
    assert tight.versions == full.versions
    with pytest.raises(RuntimeFault) as error:
        project(context, cards, state, max_chars=200)
    assert error.value.reason is Reason.INVARIANT


def test_projector_pass_two_omits_the_blocked_capability(
    repository, verifier, make_receipt, stalled_report, context, cards, state, namespace
):
    receipt = make_receipt(stalled_report)
    repository.put(receipt, verifier.token)
    history = HistoryMatcher(repository).lookup(namespace, context, requested=True)
    blocked = BlockedExecution(
        public_tool_id=receipt.public_tool_id,
        selected_capability=receipt.selected_capability,
        arguments_hash=receipt.arguments_hash,
        receipt_id=receipt.receipt_id,
        outcome_class=receipt.outcome_class,
        reason_code=Reason.REPEAT_BLOCKED,
        blocked_class="exact_repeat",
    )
    first = FirstChoice(
        decision_id=uuid4(),
        selected_id=receipt.public_tool_id,
        confidence=0.95,
        probabilities=None,
        validation_status="valid",
        accepted=True,
        selector_input_hash="1" * 64,
        decision_context_hash=context.decision_context_hash,
    )
    request = project_selector_input(
        pass_index=2,
        context=context,
        cards=cards,
        state=state,
        history=history,
        budgets=budgets(remaining_selector_calls=1, remaining_diagnostic_reads=1),
        blocked=(blocked,),
        first=first,
    )
    assert request.pass_index == 2
    assert request.blocked == (blocked,)
    # The first record travels with the second decision as evidence about it.
    assert request.first == first
    assert request.first.selected_id == receipt.public_tool_id
    assert "inspect_schema_keys" not in request.available_tool_ids
    assert request.available_tool_ids == ("inspect_pipeline", "inspect_scope")
    # The blocked tool keeps its card and its receipt so D2 can see the reason.
    assert "inspect_schema_keys" in [card.public_tool_id for card in request.cards]
    assert receipt.receipt_id in canonical_text(request)
    assert Reason.REPEAT_BLOCKED.value in canonical_text(request)


def test_projector_contract_rejects_incoherent_pass_and_block_combinations(
    context, cards, state
):
    request = project(context, cards, state)
    payload = request.model_dump()
    blocked = {
        "public_tool_id": "inspect_scope",
        "selected_capability": "inspect_scope",
        "arguments_hash": DIGEST,
        "receipt_id": DIGEST,
        "outcome_class": "no_progress",
        "reason_code": Reason.REPEAT_BLOCKED,
        "blocked_class": "exact_repeat",
    }
    for changes in (
        {"blocked": (blocked,)},
        {"pass_index": 2},
        {"pass_index": 2, "blocked": (blocked,)},
        {"available_tool_ids": ()},
        {"available_tool_ids": ("inspect_future",)},
        {"available_tool_ids": ("inspect_scope", "inspect_scope")},
        {"question": "Pick a tool"},
        {"review_option": "auto_approve"},
        {"pass_index": 3},
        {"extra": 1},
    ):
        with pytest.raises(ValidationError):
            SelectorInput.model_validate(payload | changes)
    assert SelectorInput.model_validate(payload) == request


@pytest.mark.parametrize(
    "marker", ["evaluator_only", "hidden_cause", "authorization: Bearer abc"]
)
def test_security_projector_rejects_evaluator_and_secret_markers(
    make_context, make_state, cards, state, context, marker
):
    poisoned = PublicEvidence(
        evidence_id=uuid4(),
        kind="pipeline",
        tool_id="inspect_pipeline",
        dataset_version="v17",
        arguments_hash=DIGEST,
        observed_at=datetime(2026, 10, 1, tzinfo=UTC).isoformat(),
        facts={"note": marker},
    )
    # The core selection contract already refuses the marker outright.
    with pytest.raises(ValidationError):
        make_state(bounded_evidence=(poisoned,))
    # The projector applies the same boundary to anything handed to it directly.
    tainted = ReceiptMatch(
        receipt_id=DIGEST,
        match_class="exact",
        outcome_class="no_progress",
        public_tool_id=marker.replace(" ", "_").replace(":", "_"),
        selected_capability="inspect_scope",
        arguments_hash=DIGEST,
        reason_code=Reason.INSUFFICIENT,
    )
    with pytest.raises(RuntimeFault) as error:
        project(
            context,
            cards,
            state,
            history=HistoryLookupResult(status="matched", matches=(tainted,)),
        )
    assert error.value.reason is Reason.RECEIPT_INVALID


def test_projector_input_hash_is_canonical_and_order_independent(context, cards, state):
    first = project(context, cards, state)
    second = SelectorInput.model_validate(first.model_dump())
    assert first.input_hash == second.input_hash
    assert len(first.input_hash) == 64
    changed = SelectorInput.model_validate(
        first.model_dump()
        | {"budgets": budgets(remaining_diagnostic_reads=1).model_dump()}
    )
    assert changed.input_hash != first.input_hash
    match = ReceiptMatch(
        receipt_id=DIGEST,
        match_class="compatible",
        outcome_class="useful_evidence",
        public_tool_id="inspect_scope",
        selected_capability="inspect_scope",
        arguments_hash=DIGEST,
        reason_code=Reason.EMPTY_INITIAL,
    )
    with_history = SelectorInput.model_validate(
        first.model_dump()
        | {"history": HistoryLookupResult(status="matched", matches=(match,))}
    )
    assert with_history.input_hash != first.input_hash
