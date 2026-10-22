"""Step 12: the remaining contract-invariant and fail-closed branches.

The team posture requires complete branch coverage for call budgets, cascade
bounds, evaluator isolation, and terminal-state mapping. This file closes the
invariant guards that the behavioural files reach only on their happy paths.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_invariants.py -q
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_same_call import request

from unstuck.cascade.cards import (
    CardBinding,
    capability_cards,
    card_for,
    card_index,
)
from unstuck.cascade.identity import DecisionContext
from unstuck.cascade.integration import (
    context_for,
    core_measurement,
    selection_authority,
)
from unstuck.cascade.matching import HistoryLookupResult, HistoryMatcher, ReceiptMatch
from unstuck.cascade.offline import ScriptedChoice, ScriptedDecider, ScriptedExecutor
from unstuck.cascade.policy import CascadePolicy
from unstuck.cascade.projector import SelectorInput
from unstuck.cascade.receipts import (
    DecisionReceipt,
    EvidenceDelta,
    receipt_identity,
)
from unstuck.cascade.selection import SelectionRecord, acceptable, normalize
from unstuck.cascade.serialization import StageMeasurement
from unstuck.decision import OptionProbability, PublicEvidence, PublicProbe
from unstuck.domain import Reason, RuntimeFault
from unstuck.events import Measurement

DIGEST = "a" * 64


def record(**changes) -> SelectionRecord:
    return SelectionRecord.model_validate(
        {
            "pass_index": 1,
            "decision_id": uuid4(),
            "model_call_id": uuid4(),
            "model_revision": "scripted-v1",
            "candidate_ids": ("inspect_scope", "request_review"),
            "validation_status": "valid",
            "accepted": True,
            "selected_id": "inspect_scope",
            "confidence": 0.95,
            "selector_input_hash": DIGEST,
            "decision_context_hash": DIGEST,
        }
        | changes
    )


def test_invariant_selection_record_rejects_incoherent_acceptance():
    valid = record()
    assert valid.accepted and valid.rejection is None
    assert len(valid.record_hash) == 64
    assert record(accepted=False, rejection=Reason.FIRST_REJECTED).rejection is (
        Reason.FIRST_REJECTED
    )
    for changes in (
        {"accepted": True, "rejection": Reason.FIRST_REJECTED},
        {"accepted": False, "rejection": None},
        {"selected_id": None},
        {"selected_id": "request_review"},
        {"confidence": None},
    ):
        with pytest.raises(ValidationError):
            record(**changes)


def test_invariant_acceptance_recomputes_from_the_offered_options(
    context, cards, state
):
    from test_projector import project

    payload = project(context, cards, state)
    options = payload.option_ids

    def raw(**changes):
        from unstuck.cascade.selection import RawSelection

        return RawSelection.model_validate(
            {
                "model_revision": "scripted-v1",
                "decision_id": uuid4(),
                "model_call_id": uuid4(),
                "pass_index": 1,
                "candidate_ids": options,
                "selected_id": "inspect_scope",
                "confidence": 0.95,
                "validation_status": "valid",
                "probabilities": tuple(
                    OptionProbability(
                        id=identifier, probability=float(identifier == "inspect_scope")
                    )
                    for identifier in options
                ),
            }
            | changes
        )

    assert acceptable(raw(), payload, 0.8)
    assert normalize(raw(), payload, threshold=0.8).accepted
    # A probability set that does not sum to one is not a usable distribution.
    assert not acceptable(
        raw(
            probabilities=tuple(
                OptionProbability(id=identifier, probability=0.9)
                for identifier in options
            )
        ),
        payload,
        0.8,
    )
    # A narrowed option set rejects a distribution over the wider one.
    narrowed = SelectorInput.model_validate(
        payload.model_dump() | {"available_tool_ids": ("inspect_scope",)}
    )
    assert not acceptable(
        raw(candidate_ids=("inspect_scope", "request_review")),
        narrowed,
        0.8,
    )
    assert not acceptable(raw(), narrowed, 0.8)
    assert not acceptable(raw(probabilities=None), payload, 0.8)
    assert not acceptable(raw(validation_status="error"), payload, 0.8)
    assert not acceptable(raw(confidence=None), payload, 0.8)
    assert not acceptable(raw(selected_id="request_review"), payload, 0.8)


def test_invariant_decision_context_rejects_duplicate_members(context):
    assert len(context.versions.versions_hash) == 64
    duplicate_candidate = context.candidates[0].model_dump()
    for changes in (
        {"candidates": (duplicate_candidate, duplicate_candidate)},
        {"evidence_hashes": (DIGEST, DIGEST)},
        {"used_capabilities": ("inspect_scope", "inspect_scope")},
    ):
        with pytest.raises(ValidationError):
            DecisionContext.model_validate(context.model_dump() | changes)


def test_invariant_card_catalog_rejects_a_duplicate_label(cards):
    assert len(cards[0].card.card_hash) == 64
    assert card_for("inspect-scope", cards).public_tool_id == "inspect_scope"
    assert card_for("inspect_scope").public_tool_id == "inspect_scope"
    with pytest.raises(ValueError):
        card_for("invented_tool", cards)
    clashing = cards + (
        CardBinding(card=cards[0].card, capability="inspect_scope", aliases=()),
    )
    with pytest.raises(ValueError):
        card_index(clashing)
    aliased = (
        CardBinding(
            card=cards[0].card,
            capability="inspect_pipeline",
            aliases=("inspect_scope",),
        ),
        cards[2],
    )
    with pytest.raises(ValueError):
        card_index(aliased)


def test_invariant_receipt_identity_and_timestamp_are_strict(
    make_receipt, useful_report, stalled_report
):
    receipt = make_receipt(useful_report)
    with pytest.raises(ValueError):
        receipt_identity({"receipt_id": DIGEST})
    with pytest.raises(ValidationError):
        DecisionReceipt.model_validate(
            receipt.model_dump() | {"observed_at": datetime(2026, 10, 1)}
        )
    # A verified outcome must name the diagnostic call it came from.
    stalled = make_receipt(stalled_report)
    payload = stalled.model_dump() | {"diagnostic_call_id": None}
    with pytest.raises(ValidationError):
        DecisionReceipt.model_validate(
            payload | {"receipt_id": receipt_identity(payload)}
        )


def test_invariant_superseded_receipts_are_never_offered(
    repository, verifier, make_receipt, stalled_report, context, namespace
):
    stalled = make_receipt(stalled_report)
    payload = stalled.model_dump() | {
        "outcome_class": "superseded",
        "reason_code": Reason.RECEIPT_INVALID,
    }
    superseded = DecisionReceipt.model_validate(
        payload | {"receipt_id": receipt_identity(payload)}
    )
    assert repository.put(superseded, verifier.token) is True
    matcher = HistoryMatcher(repository)
    assert matcher.classify(superseded, context) is None
    assert matcher.lookup(namespace, context, requested=True).status == "empty"


def test_invariant_history_result_truncation_requires_matches():
    match = ReceiptMatch(
        receipt_id=DIGEST,
        match_class="exact",
        outcome_class="no_progress",
        public_tool_id="inspect_scope",
        selected_capability="inspect_scope",
        arguments_hash=DIGEST,
        reason_code=Reason.INSUFFICIENT,
    )
    assert HistoryLookupResult(status="matched", matches=(match,), truncated=True)
    for changes in (
        {"status": "unavailable", "matches": (), "truncated": True},
        {"status": "not_requested", "matches": (), "truncated": True},
    ):
        with pytest.raises(ValidationError):
            HistoryLookupResult.model_validate(changes)


def test_invariant_matcher_refuses_a_row_that_no_longer_validates(
    repository, verifier, make_receipt, stalled_report, context, namespace
):
    class CorruptRepository:
        def __init__(self, receipt):
            self.receipt = receipt

        def put(self, receipt, token):
            return True

        def find(self, namespace, problem_fingerprint):
            return (self.receipt,)

        def supersede(self, namespace, receipt_id, token):
            return False

    receipt = make_receipt(stalled_report)
    corrupt = receipt.model_copy(update={"receipt_id": DIGEST})
    matcher = HistoryMatcher(CorruptRepository(corrupt))
    with pytest.raises(RuntimeFault) as error:
        matcher.lookup(namespace, context, requested=True)
    assert error.value.reason is Reason.RECEIPT_INVALID


def test_invariant_selector_input_rejects_duplicate_cards_and_review_option(
    context, cards, state
):
    from test_projector import project

    payload = project(context, cards, state).model_dump()
    first = payload["cards"][0]
    with pytest.raises(ValidationError):
        SelectorInput.model_validate(payload | {"cards": (first, first)})
    with pytest.raises(ValidationError):
        SelectorInput.model_validate(
            payload | {"available_tool_ids": ("inspect_scope", "inspect_scope")}
        )
    # Publishing the review option as a card still cannot make it a candidate.
    review_card = first | {"public_tool_id": "request_review"}
    with pytest.raises(ValidationError):
        SelectorInput.model_validate(
            payload
            | {
                "cards": (*payload["cards"][:2], review_card),
                "available_tool_ids": ("inspect_pipeline", "request_review"),
            }
        )


def test_invariant_projector_refuses_a_non_serializable_observation(
    make_state, make_context, cards
):
    from test_projector import project

    poisoned = PublicEvidence(
        evidence_id=uuid4(),
        kind="pipeline",
        tool_id="inspect_pipeline",
        dataset_version="v17",
        arguments_hash=DIGEST,
        observed_at=datetime(2026, 10, 1, tzinfo=UTC).isoformat(),
        facts={"ratio": float("nan")},
    )
    state = make_state(bounded_evidence=(poisoned,))
    with pytest.raises(RuntimeFault) as error:
        project(make_context(state), cards, state)
    assert error.value.reason is Reason.CONFLICT


def test_invariant_core_measurement_narrows_every_availability():
    assert core_measurement(StageMeasurement()) == Measurement()
    assert core_measurement(StageMeasurement(availability="not_called")) == Measurement(
        availability="not_called"
    )
    assert core_measurement(
        StageMeasurement(availability="not_provided")
    ) == Measurement(availability="not_provided")
    assert core_measurement(
        StageMeasurement(availability="provider_error")
    ) == Measurement(availability="not_measured")


def test_invariant_selection_authority_requires_a_decision_record():
    class Empty:
        first = None
        final = None

    with pytest.raises(RuntimeFault) as error:
        selection_authority(Empty())
    assert error.value.reason is Reason.FIRST_REJECTED


def test_invariant_context_for_refuses_an_unpublished_candidate(
    make_state, policy, cards
):
    unknown = PublicProbe(
        id="inspect_future",
        description="An option with no published card",
        applies_when="never, because this option has no published card",
        observable_facts=("future",),
    )
    state = make_state()
    drifted = type(state).model_validate(
        state.model_dump() | {"candidates": (unknown.model_dump(),)}
    )
    with pytest.raises(RuntimeFault) as error:
        context_for(drifted, policy, cards)
    assert error.value.reason is Reason.INVARIANT
    # A repeated used capability is not a valid bounded context either.
    with pytest.raises(RuntimeFault) as error:
        context_for(
            state,
            policy,
            cards,
            used_capabilities=("inspect_scope", "inspect_scope"),
        )
    assert error.value.reason is Reason.INVARIANT
    assert len(capability_cards()) == 3
    assert context_for(state, policy, cards).candidates


def test_invariant_a_malformed_selector_answer_is_refused_on_both_passes(
    repository, verifier, clock, state, policy
):
    """The whole orchestrator is the only selection seam.

    There is deliberately no adapter that answers with a first-pass choice
    alone, so a malformed or stale answer is refused inside the one bounded
    path that also admits receipts and opens the second decision.
    """
    from test_bounds import worker_engine

    class WrongShape(ScriptedDecider):
        async def select(self, payload):
            return {"selected_id": "inspect_scope"}  # type: ignore[return-value]

    worker = worker_engine(
        policy,
        repository,
        verifier,
        clock,
        WrongShape([ScriptedChoice(selected_id="inspect_scope")]),
        ScriptedExecutor(),
    )
    result = asyncio.run(worker.resolve(request(state, key="malformed")))
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.FIRST_REJECTED
    assert result.consumed.decider_calls == 1
    assert result.created_receipt_ids == ()

    stale = worker_engine(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider(
            [ScriptedChoice(selected_id="inspect_scope")],
            model_revision="scripted-v2",
        ),
        ScriptedExecutor(),
    )
    drifted = asyncio.run(stale.resolve(request(state, key="stale-revision")))
    assert drifted.path == "unresolved"
    assert drifted.unresolved_reason is Reason.FIRST_REJECTED
    assert drifted.consumed.diagnostic_reads == 0


def test_invariant_resolve_refuses_a_non_request(
    repository, verifier, clock, state, policy
):
    from test_bounds import worker_engine

    worker = worker_engine(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id="inspect_scope")]),
        ScriptedExecutor(),
    )
    with pytest.raises(RuntimeFault) as error:
        asyncio.run(worker.resolve({"cascade_id": str(uuid4())}))
    assert error.value.reason is Reason.INVARIANT
    assert asyncio.run(worker.resolve(request(state))).path == "direct"


def test_invariant_scripted_fakes_and_scenario_entry_point_run_offline(capsys):
    import json

    from unstuck.cascade import scenarios

    assert scenarios.main() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "passed"
    assert printed["live_checks"] == "Pending"


def test_invariant_scripted_fakes_honour_a_scripted_delay(
    repository, verifier, clock, state, policy
):
    from test_bounds import worker_engine

    from unstuck.cascade.offline import ScriptedOutcome

    worker = worker_engine(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider(
            [ScriptedChoice(selected_id="inspect_scope", delay_seconds=0.001)]
        ),
        ScriptedExecutor(default=ScriptedOutcome(delay_seconds=0.001)),
    )
    result = asyncio.run(worker.resolve(request(state)))
    assert result.path == "direct"
    assert result.consumed.diagnostic_reads == 1


def test_invariant_a_late_diagnostic_result_is_refused(
    repository, verifier, state, policy
):
    from test_bounds import worker_engine

    from unstuck.clock import FakeClock

    clock = FakeClock()

    class LateExecutor(ScriptedExecutor):
        async def execute(self, payload):
            clock.advance(25)
            return await super().execute(payload)

    worker = worker_engine(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id="inspect_scope")]),
        LateExecutor(),
    )
    result = asyncio.run(worker.resolve(request(state)))
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.TOOL_ERROR
    assert result.consumed.diagnostic_reads == 1
    assert result.created_receipt_ids == ()


def test_invariant_a_context_drift_during_the_decision_is_refused(
    repository, verifier, clock, state
):
    from test_bounds import worker_engine

    policy = CascadePolicy(baseline_id="decider_memory", model_revision="scripted-v1")
    drifted = CascadePolicy(
        baseline_id="decider_memory",
        model_revision="scripted-v1",
        calibration={"first_confidence_min": 0.5, "final_confidence_min": 0.5},
    )
    worker = worker_engine(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id="inspect_scope")]),
        ScriptedExecutor(),
    )

    class DriftingDecider(ScriptedDecider):
        async def select(self, payload):
            raw = await super().select(payload)
            # The bound policy moves while the answer is in flight.
            worker.policy = drifted
            return raw

    worker.decider = DriftingDecider([ScriptedChoice(selected_id="inspect_scope")])
    result = asyncio.run(worker.resolve(request(state)))
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.CONFLICT
    assert result.consumed.decider_calls == 1
    assert result.consumed.diagnostic_reads == 0


def test_invariant_evidence_delta_requires_matching_identities():
    with pytest.raises(ValidationError):
        EvidenceDelta(
            evidence_before_hash=DIGEST,
            evidence_after_hash="b" * 64,
            evidence_ids=(),
        )
    delta = EvidenceDelta(
        evidence_before_hash=DIGEST,
        evidence_after_hash="b" * 64,
        evidence_ids=(uuid4(),),
    )
    assert delta.changed


def test_invariant_a_delta_proves_both_of_its_claims():
    """Admitted evidence and moved disposition are two separate claims."""
    base = {
        "evidence_before_hash": DIGEST,
        "evidence_after_hash": "b" * 64,
        "evidence_ids": (uuid4(),),
    }
    admitted = EvidenceDelta.model_validate(
        base | {"disposition_before_hash": DIGEST, "disposition_after_hash": DIGEST}
    )
    assert admitted.changed and not admitted.progressed
    moved = EvidenceDelta.model_validate(
        base | {"disposition_before_hash": DIGEST, "disposition_after_hash": "c" * 64}
    )
    assert moved.changed and moved.progressed
    # An absent disposition delta defaults to the evidence delta.
    assert EvidenceDelta.model_validate(base).progressed
    for changes in (
        # One disposition hash without the other states nothing.
        {"disposition_before_hash": DIGEST},
        {"disposition_after_hash": DIGEST},
        # A disposition cannot move without evidence to move it.
        {
            "evidence_after_hash": DIGEST,
            "evidence_ids": (),
            "disposition_before_hash": DIGEST,
            "disposition_after_hash": "c" * 64,
        },
    ):
        with pytest.raises(ValidationError):
            EvidenceDelta.model_validate(base | changes)


def test_invariant_stalled_is_the_conjunction_and_nothing_weaker():
    """`stalled` is true only when both proven negatives hold."""
    cases = {
        (True, True): False,
        (True, False): False,
        (False, False): True,
    }
    for (changed, progressed), stalled in cases.items():
        delta = EvidenceDelta(
            evidence_before_hash=DIGEST,
            evidence_after_hash=("b" * 64) if changed else DIGEST,
            evidence_ids=(uuid4(),) if changed else (),
            disposition_before_hash=DIGEST,
            disposition_after_hash=("c" * 64) if progressed else DIGEST,
        )
        assert delta.changed is changed
        assert delta.progressed is progressed
        assert delta.stalled is stalled
    # An absent disposition delta inherits the evidence claim on both sides.
    assert EvidenceDelta(
        evidence_before_hash=DIGEST, evidence_after_hash=DIGEST
    ).stalled


def test_invariant_blocking_rereads_both_hashes_and_never_the_label_alone(
    verifier, namespace, context
):
    """Execution authority is re-derived from the delta, not from the class."""
    payload = {
        "namespace": namespace,
        "decision_id": uuid4(),
        "model_call_id": uuid4(),
        "diagnostic_call_id": uuid4(),
        "problem_fingerprint": context.problem_fingerprint,
        "decision_context_hash": context.decision_context_hash,
        "selected_capability": "inspect_scope",
        "public_tool_id": "inspect_scope",
        "arguments_hash": DIGEST,
        "delta": EvidenceDelta(
            evidence_before_hash=DIGEST,
            evidence_after_hash=DIGEST,
            disposition_before_hash=DIGEST,
            disposition_after_hash=DIGEST,
        ),
        "outcome_class": "no_progress",
        "reason_code": Reason.INSUFFICIENT,
        "versions": context.versions,
    }
    stalled = DecisionReceipt.model_validate(
        payload
        | {
            "receipt_id": receipt_identity(payload),
            "observed_at": datetime(2026, 10, 1, 12, tzinfo=UTC),
        }
    )
    assert stalled.blocking is True
    # A no-progress label over a moved evidence hash is refused outright, so no
    # receipt that names admitted evidence can ever reach the blocking check.
    for delta in (
        {
            "evidence_before_hash": DIGEST,
            "evidence_after_hash": "b" * 64,
            "evidence_ids": (uuid4(),),
            "disposition_before_hash": DIGEST,
            "disposition_after_hash": DIGEST,
        },
        {
            "evidence_before_hash": DIGEST,
            "evidence_after_hash": "b" * 64,
            "evidence_ids": (uuid4(),),
        },
    ):
        forged = stalled.model_dump() | {"delta": delta}
        with pytest.raises(ValidationError):
            DecisionReceipt.model_validate(
                forged | {"receipt_id": receipt_identity(forged)}
            )


def test_invariant_a_failed_read_cannot_claim_admitted_evidence(
    verifier, namespace, context
):
    """Only an admissible read may name evidence in its receipt."""
    payload = {
        "namespace": namespace,
        "decision_id": uuid4(),
        "model_call_id": uuid4(),
        "diagnostic_call_id": uuid4(),
        "problem_fingerprint": context.problem_fingerprint,
        "decision_context_hash": context.decision_context_hash,
        "selected_capability": "inspect_scope",
        "public_tool_id": "inspect_scope",
        "arguments_hash": DIGEST,
        "delta": EvidenceDelta(
            evidence_before_hash=DIGEST,
            evidence_after_hash="b" * 64,
            evidence_ids=(uuid4(),),
            disposition_before_hash=DIGEST,
            disposition_after_hash=DIGEST,
        ),
        "outcome_class": "invalid_evidence",
        "reason_code": Reason.CONFLICT,
        "versions": context.versions,
    }
    with pytest.raises(ValidationError):
        DecisionReceipt.model_validate(
            payload
            | {
                "receipt_id": receipt_identity(payload),
                "observed_at": datetime(2026, 10, 1, 12, tzinfo=UTC),
            }
        )


def refreshing(authority, repository, verifier, clock, policy):
    """One cascade whose first read makes no progress, so a refresh happens."""
    from test_bounds import worker_engine

    from unstuck.cascade.offline import ScriptedOutcome

    return worker_engine(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider(
            [
                ScriptedChoice(selected_id="inspect_schema_keys"),
                ScriptedChoice(selected_id="inspect_pipeline"),
            ]
        ),
        authority,
        None,
    ), ScriptedOutcome(changed=False)


def test_invariant_a_refused_state_projection_fails_closed(
    repository, verifier, clock, state, policy, make_state
):
    """The refreshed state is the authority's, and it is validated as one."""
    from unstuck.cascade.offline import ScriptedOutcome

    class Projects(ScriptedExecutor):
        """Projects whatever the case declares, instead of a real state."""

        projection: object = None

        def project(self):
            if isinstance(self.projection, RuntimeFault):
                raise self.projection
            return self.projection

    cases = {
        "not_a_state": (object(), Reason.INVARIANT),
        # The problem itself moved, so this is a different incident.
        "moved_problem": (make_state(country_codes=("IT",)), Reason.TASK_MISMATCH),
        # Nothing is left to choose, so the read's own reason stands.
        "no_candidate": (RuntimeFault(Reason.NO_READ), Reason.INSUFFICIENT),
    }
    for projection, expected in cases.values():
        authority = Projects(default=ScriptedOutcome(changed=False), state=state)
        authority.projection = projection
        worker, _ = refreshing(authority, type(repository)(), verifier, clock, policy)
        result = asyncio.run(worker.resolve(request(state)))
        assert result.path == "unresolved"
        assert result.unresolved_reason is expected
        assert result.consumed.decider_calls == 1
        assert result.consumed.diagnostic_reads == 1
        assert len(result.created_receipt_ids) == 1
