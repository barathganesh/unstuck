"""Evidence novelty and decision progress are two independent dimensions.

The measured defect this suite pins: a real diagnostic read always admits a
canonical fact the run did not hold on its first execution, so the receipt was
classified `useful_evidence` even when the supported disposition did not move,
and `useful_evidence` can never block. The exact repeated blind-spot choice was
therefore unblockable through the real evidence path.

The correction keeps both dimensions separate and adds one outcome class for
the combination that had nowhere to go: `disposition_unchanged` means a valid,
admissible read admitted a fact **and** moved no supported disposition. Only
that class and the existing strict `no_progress` may ever be eligible to refuse
an exact repetition, and eligibility is never the label on its own.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_disposition_outcome.py -q
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_same_call import engine, request, run

from unstuck.cascade.identity import BoundVersions
from unstuck.cascade.matching import HistoryMatcher, ReceiptMatch
from unstuck.cascade.offline import (
    ScriptedChoice,
    ScriptedDecider,
    ScriptedExecutor,
    ScriptedOutcome,
)
from unstuck.cascade.policy import Calibration, CascadePolicy
from unstuck.cascade.receipts import (
    BLOCKING_OUTCOMES,
    EVIDENCE_OUTCOMES,
    OUTCOME_REASONS,
    DecisionReceipt,
    EvidenceDelta,
    receipt_identity,
)
from unstuck.cascade.serialization import canonical_digest
from unstuck.domain import Counters, Reason

#: The tool the scripted selector repeats. One capability, one argument set.
REPEATED = "inspect_schema_keys"
OTHER = "inspect_pipeline"

HELD = canonical_digest(["admissible", 0])
ADMITTED = canonical_digest(["admissible", 1])
OPENED = canonical_digest(["disposition", 0])
MOVED = canonical_digest(["disposition", 1])


def delta(*, changed: bool, moved: bool) -> EvidenceDelta:
    """One proven pair of deltas. The two dimensions are set independently."""
    return EvidenceDelta(
        evidence_before_hash=HELD,
        evidence_after_hash=ADMITTED if changed else HELD,
        evidence_ids=(uuid4(),) if changed else (),
        disposition_before_hash=OPENED,
        disposition_after_hash=MOVED if moved else OPENED,
    )


def report(make_report, *, changed: bool, moved: bool, **changes):
    reason = Reason.POPULATED_INITIAL if moved else Reason.INSUFFICIENT
    return make_report(
        capability=REPEATED,
        delta=delta(changed=changed, moved=moved),
        reason_code=changes.pop("reason_code", reason),
        **changes,
    )


# ---------------------------------------------------------------------------
# Admission: what the verifier derives from the two observed deltas
# ---------------------------------------------------------------------------


def test_an_admitted_fact_that_moved_a_gate_is_useful_evidence_and_never_blocks(
    verifier, make_report, make_receipt
):
    admitted = report(make_report, changed=True, moved=True)
    assert verifier.classify(admitted) == "useful_evidence"
    receipt = make_receipt(admitted)
    assert receipt.outcome_class == "useful_evidence"
    assert receipt.delta.changed is True
    assert receipt.delta.progressed is True
    assert receipt.blocking is False


def test_an_admitted_fact_that_moved_no_gate_is_disposition_unchanged(
    verifier, make_report, make_receipt
):
    """The combination the previous experiment could never express."""
    neutral = report(make_report, changed=True, moved=False)
    assert verifier.classify(neutral) == "disposition_unchanged"
    receipt = make_receipt(neutral)
    assert receipt.outcome_class == "disposition_unchanged"
    assert receipt.delta.changed is True
    assert receipt.delta.progressed is False
    assert receipt.delta.stalled is False
    assert receipt.reason_code is Reason.INSUFFICIENT
    assert receipt.blocking is True


def test_a_read_that_admitted_nothing_is_still_strict_no_progress(
    verifier, make_report, make_receipt
):
    stalled = report(make_report, changed=False, moved=False)
    assert verifier.classify(stalled) == "no_progress"
    receipt = make_receipt(stalled)
    assert receipt.outcome_class == "no_progress"
    assert receipt.delta.stalled is True
    assert receipt.blocking is True


@pytest.mark.parametrize(
    ("status", "reason"),
    [("provider_error", Reason.TOOL_ERROR), ("invalid_evidence", Reason.CONFLICT)],
)
def test_a_failed_or_inadmissible_read_is_never_relabelled(
    verifier, make_report, make_receipt, status, reason
):
    """The correction touches neither failure class and neither becomes blocking."""
    failed = make_report(
        capability=REPEATED,
        status=status,
        changed=False,
        reason_code=reason,
        diagnostic_call_id=None if status == "provider_error" else uuid4(),
    )
    assert verifier.classify(failed) == status
    receipt = make_receipt(failed)
    assert receipt.outcome_class == status
    assert receipt.blocking is False


def test_the_blocking_set_is_exactly_the_two_disposition_negative_classes():
    assert BLOCKING_OUTCOMES == frozenset({"no_progress", "disposition_unchanged"})
    assert EVIDENCE_OUTCOMES == frozenset({"useful_evidence", "disposition_unchanged"})
    assert "useful_evidence" not in BLOCKING_OUTCOMES
    assert "provider_error" not in BLOCKING_OUTCOMES
    assert "invalid_evidence" not in BLOCKING_OUTCOMES
    assert "superseded" not in BLOCKING_OUTCOMES


def test_disposition_unchanged_admits_only_the_insufficient_reason():
    assert OUTCOME_REASONS["disposition_unchanged"] == frozenset({Reason.INSUFFICIENT})


def test_a_disposition_unchanged_report_refuses_a_progress_reason(make_report):
    with pytest.raises(ValidationError):
        report(
            make_report, changed=True, moved=False, reason_code=Reason.POPULATED_INITIAL
        )


def test_a_useful_evidence_report_refuses_a_stalled_disposition(make_report):
    """A moved gate cannot be reported with the disposition-neutral reason."""
    with pytest.raises(ValidationError):
        report(make_report, changed=True, moved=True, reason_code=Reason.INSUFFICIENT)


# ---------------------------------------------------------------------------
# Eligibility is re-derived from proven facts, never taken from the label
# ---------------------------------------------------------------------------


def _fields(namespace, context, *, outcome, reason, moved_delta, **changes):
    return {
        "namespace": namespace,
        "decision_id": uuid4(),
        "model_call_id": uuid4(),
        "diagnostic_call_id": uuid4(),
        "problem_fingerprint": context.problem_fingerprint,
        "decision_context_hash": context.decision_context_hash,
        "selected_capability": REPEATED,
        "public_tool_id": REPEATED,
        "arguments_hash": canonical_digest(["arguments"]),
        "delta": moved_delta,
        "outcome_class": outcome,
        "reason_code": reason,
        "versions": context.versions,
    } | changes


def _receipt(fields, *, observed_at=None):
    return DecisionReceipt.model_validate(
        fields
        | {
            "receipt_id": receipt_identity(fields),
            "observed_at": observed_at or datetime(2026, 10, 21, 12, tzinfo=UTC),
        }
    )


def test_a_disposition_unchanged_label_over_a_moved_gate_is_refused(namespace, context):
    """The label alone buys nothing: the proven hashes are re-read."""
    with pytest.raises(ValidationError):
        _receipt(
            _fields(
                namespace,
                context,
                outcome="disposition_unchanged",
                reason=Reason.INSUFFICIENT,
                moved_delta=delta(changed=True, moved=True),
            )
        )


def test_a_disposition_unchanged_label_over_an_empty_read_is_refused(
    namespace, context
):
    with pytest.raises(ValidationError):
        _receipt(
            _fields(
                namespace,
                context,
                outcome="disposition_unchanged",
                reason=Reason.INSUFFICIENT,
                moved_delta=delta(changed=False, moved=False),
            )
        )


def test_a_stored_legacy_useful_evidence_receipt_validates_and_never_blocks(
    namespace, context
):
    """Backward compatibility, in the fail-safe direction only.

    A receipt written before the correction carries `useful_evidence` with no
    moved gate. It still reads back, and it stays non-blocking, so a stored row
    can never acquire execution authority it was not written with.
    """
    legacy = _receipt(
        _fields(
            namespace,
            context,
            outcome="useful_evidence",
            reason=Reason.INSUFFICIENT,
            moved_delta=delta(changed=True, moved=False),
        )
    )
    assert legacy.outcome_class == "useful_evidence"
    assert legacy.delta.changed is True
    assert legacy.delta.progressed is False
    assert legacy.blocking is False


def test_receipt_identity_still_excludes_audit_time_and_operational_identity(
    namespace, context
):
    """Timestamps are not semantic identity, and a new instant is not a new row."""
    fields = _fields(
        namespace,
        context,
        outcome="disposition_unchanged",
        reason=Reason.INSUFFICIENT,
        moved_delta=delta(changed=True, moved=False),
    )
    early = _receipt(fields)
    late = _receipt(
        fields, observed_at=datetime(2026, 10, 21, 12, tzinfo=UTC) + timedelta(days=9)
    )
    assert early.receipt_id == late.receipt_id
    assert early.blocking is True and late.blocking is True


def test_a_fresh_evidence_identity_does_not_change_blocking_eligibility(
    namespace, context
):
    """A re-minted evidence id is operational, so it is not what is proven."""
    first = delta(changed=True, moved=False)
    second = delta(changed=True, moved=False)
    assert first.evidence_ids != second.evidence_ids
    for proven in (first, second):
        receipt = _receipt(
            _fields(
                namespace,
                context,
                outcome="disposition_unchanged",
                reason=Reason.INSUFFICIENT,
                moved_delta=proven,
            )
        )
        assert receipt.blocking is True


# ---------------------------------------------------------------------------
# Matching: eligible is not blocked without an exact current-context match
# ---------------------------------------------------------------------------


def _match(**changes) -> ReceiptMatch:
    return ReceiptMatch.model_validate(
        {
            "receipt_id": canonical_digest(["receipt"]),
            "match_class": "exact",
            "outcome_class": "disposition_unchanged",
            "public_tool_id": REPEATED,
            "selected_capability": REPEATED,
            "arguments_hash": canonical_digest(["arguments"]),
            "reason_code": Reason.INSUFFICIENT,
            "admitted_evidence": True,
            "moved_disposition": False,
        }
        | changes
    )


def test_an_exact_disposition_unchanged_match_carries_negative_authority():
    assert _match().blocking is True


def test_a_compatible_disposition_unchanged_match_is_advisory_only():
    assert _match(match_class="compatible").blocking is False


def test_a_match_that_records_a_moved_gate_is_never_blocking():
    assert _match(moved_disposition=True).blocking is False


def test_a_match_that_records_no_admitted_fact_is_not_disposition_unchanged():
    """The two blocking classes are not interchangeable."""
    assert _match(admitted_evidence=False).blocking is False
    assert (
        _match(
            outcome_class="no_progress",
            admitted_evidence=False,
            moved_disposition=False,
        ).blocking
        is True
    )
    assert _match(outcome_class="no_progress").blocking is False


def test_an_exact_useful_evidence_match_is_never_blocking():
    assert (
        _match(outcome_class="useful_evidence", moved_disposition=True).blocking
        is False
    )


def test_a_changed_decision_context_makes_an_eligible_receipt_advisory(
    repository, verifier, namespace, context, make_context, make_state, make_report
):
    """Current authoritative facts dominate history."""
    neutral = verifier.create(
        namespace=namespace,
        decision_id=uuid4(),
        model_call_id=uuid4(),
        context=context,
        report=report(make_report, changed=True, moved=False),
    )
    repository.put(neutral, verifier.token)
    matcher = HistoryMatcher(repository)
    assert matcher.classify(neutral, context) == "exact"

    moved_on = make_context(make_state(), evidence_hashes=(canonical_digest(["new"]),))
    assert moved_on.decision_context_hash != context.decision_context_hash
    assert matcher.classify(neutral, moved_on) == "compatible"
    advisory = matcher.lookup(namespace, moved_on, requested=True)
    assert advisory.status == "matched"
    assert advisory.blocked_executions == frozenset()


@pytest.mark.parametrize(
    "moved",
    [
        {"dataset_version": "v18"},
        {"model_revision": "other-revision"},
        {"policy_hash": canonical_digest(["other-policy"])},
        {"calibration_hash": canonical_digest(["other-calibration"])},
        {"catalog_version": "catalog-v9"},
        {"tool_contract_version": "tools-v9"},
        {"data_schema_version": "schema-v9"},
    ],
)
def test_a_moved_bound_version_removes_an_eligible_receipt_from_the_history(
    repository, verifier, namespace, context, make_report, moved
):
    """Model, catalog, dataset, tool, policy and calibration all bind."""
    neutral = verifier.create(
        namespace=namespace,
        decision_id=uuid4(),
        model_call_id=uuid4(),
        context=context,
        report=report(make_report, changed=True, moved=False),
    )
    repository.put(neutral, verifier.token)
    rebound = context.model_copy(
        update={
            "versions": BoundVersions.model_validate(
                context.versions.model_dump() | moved
            )
        }
    )
    matcher = HistoryMatcher(repository)
    assert matcher.classify(neutral, rebound) is None
    assert matcher.lookup(namespace, rebound, requested=True).status == "empty"


def test_a_block_names_the_identical_capability_and_arguments_only(
    repository, verifier, namespace, context, make_report
):
    neutral = verifier.create(
        namespace=namespace,
        decision_id=uuid4(),
        model_call_id=uuid4(),
        context=context,
        report=report(make_report, changed=True, moved=False),
    )
    repository.put(neutral, verifier.token)
    found = HistoryMatcher(repository).lookup(namespace, context, requested=True)
    assert found.blocked_executions == frozenset({(REPEATED, neutral.arguments_hash)})
    assert found.blocking_match((REPEATED, neutral.arguments_hash)) is not None
    assert found.blocking_match((OTHER, neutral.arguments_hash)) is None
    assert found.blocking_match((REPEATED, canonical_digest(["other"]))) is None


# ---------------------------------------------------------------------------
# Cross-call orchestration: one refusal, no read spent, at most one re-decision
# ---------------------------------------------------------------------------

NEUTRAL = ScriptedOutcome(changed=True, moved=False)
PRODUCTIVE = ScriptedOutcome(changed=True, moved=True)


def _call(
    repository,
    verifier,
    clock,
    state,
    policy,
    *,
    first_tool=REPEATED,
    second_tool=OTHER,
    outcome=NEUTRAL,
    key="call-1",
):
    decider = ScriptedDecider(
        [
            ScriptedChoice(selected_id=first_tool),
            ScriptedChoice(selected_id=second_tool),
        ]
    )
    executor = ScriptedExecutor(outcomes={first_tool: outcome}, default=PRODUCTIVE)
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, arm=policy.baseline_id, key=key),
    )
    return result, decider, executor


def test_call_2_precludes_the_exact_repeated_neutral_read_before_one_fresh_choice(
    repository, verifier, clock, state, policy
):
    """The mechanism the previous experiment could not reach."""
    first, _, first_executor = _call(
        repository, verifier, clock, state, policy, key="call-1"
    )
    # Call 1 reads the repeated tool, admits a real fact, moves no gate, and
    # its one same-call second read is what finally resolves the incident.
    assert first.reports[0].outcome_class == "disposition_unchanged"
    assert first.reports[0].public_tool_id == REPEATED
    assert first.reports[1].outcome_class == "useful_evidence"
    assert len(first_executor.requests) == 2
    assert repository.count(request(state).namespace) == 2

    second, decider, executor = _call(
        repository,
        verifier,
        clock,
        state,
        policy,
        first_tool=OTHER,
        outcome=PRODUCTIVE,
        key="call-2",
    )
    assert second.history.status == "matched"
    assert decider.requests[0].history.status == "matched"
    # The preclusion happened before any selector call or read, and D1 still
    # chose for itself among what remained.
    assert second.precluded[0].blocked_class == "exact_repeat"
    assert second.precluded[0].outcome_class == "disposition_unchanged"
    assert second.precluded[0].public_tool_id == REPEATED
    assert second.precluded[0].reason_code is Reason.INSUFFICIENT
    assert REPEATED not in decider.requests[0].available_tool_ids
    assert REPEATED not in {item.public_tool_id for item in executor.requests}
    assert second.path == "direct"
    assert second.blocked == ()
    assert second.selected_tool_id == OTHER
    assert second.consumed.decider_calls == 1
    assert second.consumed.diagnostic_reads == 1
    assert len(decider.requests) == 1
    assert len(executor.requests) == 1


def test_the_preclusion_spends_no_call_or_read_and_leaves_both_for_d1(
    repository, verifier, clock, state, policy
):
    _call(repository, verifier, clock, state, policy, key="call-1")
    second, decider, executor = _call(
        repository,
        verifier,
        clock,
        state,
        policy,
        first_tool=OTHER,
        outcome=PRODUCTIVE,
        key="call-2",
    )
    assert decider.requests[0].budgets.remaining_selector_calls == 2
    assert decider.requests[0].budgets.remaining_diagnostic_reads == 2
    assert second.consumed == Counters(
        decider_calls=1, diagnostic_reads=1, history_lookups=1
    )
    assert len(executor.requests) == 1
    assert executor.requests[0].pass_index == 1


def test_a_useful_and_moved_first_call_never_refuses_the_repetition(
    repository, verifier, clock, state, policy
):
    first, _, _ = _call(
        repository, verifier, clock, state, policy, outcome=PRODUCTIVE, key="call-1"
    )
    assert first.outcome_class == "useful_evidence"
    assert first.path == "direct"
    second, decider, executor = _call(
        repository, verifier, clock, state, policy, outcome=PRODUCTIVE, key="call-2"
    )
    assert second.history.status == "matched"
    assert second.blocked == ()
    assert second.path == "direct"
    assert REPEATED in {item.public_tool_id for item in executor.requests}
    assert len(decider.requests) == 1


def test_the_memoryless_arm_keeps_no_receipt_and_never_refuses(
    repository, verifier, clock, state
):
    plain = CascadePolicy(baseline_id="decider", model_revision="scripted-v1")
    _call(repository, verifier, clock, state, plain, key="call-1")
    second, _, executor = _call(repository, verifier, clock, state, plain, key="call-2")
    assert second.history.status == "not_requested"
    assert second.blocked[0].blocked_class == "failed_execution"
    assert REPEATED in {item.public_tool_id for item in executor.requests}


def test_a_changed_current_context_reopens_the_previously_neutral_read(
    repository, verifier, clock, state, make_state, policy
):
    """A changed context is advisory history, so the read is eligible again."""
    _call(repository, verifier, clock, state, policy, key="call-1")
    rebound = make_state(dataset_version="v18")
    second, _, executor = _call(
        repository, verifier, clock, rebound, policy, key="call-2"
    )
    assert second.blocked[0].blocked_class == "failed_execution"
    assert REPEATED in {item.public_tool_id for item in executor.requests}


def test_history_never_selects_scores_or_decides_truth(
    repository, verifier, clock, state, policy
):
    _call(repository, verifier, clock, state, policy, key="call-1")
    second, decider, _ = _call(
        repository,
        verifier,
        clock,
        state,
        policy,
        first_tool=OTHER,
        outcome=PRODUCTIVE,
        key="call-2",
    )
    offered = decider.requests[0]
    # The blocking match resolves to the private capability inside the
    # application, and the projection handed to the selector drops it.
    blocking = [match for match in offered.history.matches if match.blocking]
    assert [match.selected_capability for match in blocking] == [REPEATED]
    for reference in offered.history.references:
        dumped = reference.model_dump()
        assert "selected_capability" not in dumped
        assert "probability" not in dumped and "confidence" not in dumped
    # D1 answered for itself; the recorded choice is the selector's own.
    assert second.first is not None
    assert second.first.selected_id == OTHER
    assert second.first.accepted is True
    # The preclusion authorized no repair and determined no factual outcome.
    assert second.precluded[0].reason_code is Reason.INSUFFICIENT
    assert second.consumed.repair_attempts == 0
    assert second.outcome_class == "useful_evidence"


def test_two_selector_calls_and_two_reads_remain_the_hard_bound(
    repository, verifier, clock, state, policy
):
    _call(repository, verifier, clock, state, policy, key="call-1")
    second, decider, executor = _call(
        repository, verifier, clock, state, policy, second_tool=OTHER, key="call-2"
    )
    assert second.consumed.decider_calls <= 2
    assert second.consumed.diagnostic_reads <= 2
    assert len(decider.requests) <= 2
    assert len(executor.requests) <= 2


def test_choosing_a_precluded_read_anyway_is_refused_and_never_re_asked(
    repository, verifier, clock, state, policy
):
    """A precluded read is not offered; naming it anyway ends the incident."""
    _call(repository, verifier, clock, state, policy, key="call-1")
    decider = ScriptedDecider(
        [ScriptedChoice(selected_id=REPEATED), ScriptedChoice(selected_id=REPEATED)]
    )
    executor = ScriptedExecutor(default=NEUTRAL)
    second = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, arm=policy.baseline_id, key="call-2"),
    )
    # The repeated tool was precluded before D1, so naming it is an unoffered
    # answer: it is refused, spends no read, and no second decision is asked.
    assert second.precluded[0].blocked_class == "exact_repeat"
    assert REPEATED not in decider.requests[0].available_tool_ids
    assert second.consumed.decider_calls == 1
    assert len(decider.requests) == 1
    assert second.consumed.diagnostic_reads == 0
    assert executor.requests == []
    assert second.path == "unresolved"
    assert second.unresolved_reason is Reason.FIRST_REJECTED


def test_a_low_confidence_answer_is_still_refused_under_the_correction(
    repository, verifier, clock, state
):
    """Blocking eligibility buys no acceptance anywhere."""
    strict = CascadePolicy(
        baseline_id="decider_memory",
        model_revision="scripted-v1",
        calibration=Calibration(),
    )
    decider = ScriptedDecider([ScriptedChoice(selected_id=REPEATED, confidence=0.10)])
    executor = ScriptedExecutor(default=NEUTRAL)
    worker = engine(strict, repository, verifier, clock, decider, executor)
    result = asyncio.run(
        worker.resolve(request(state, arm="decider_memory", key="call-1"))
    )
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.FIRST_REJECTED
    assert executor.requests == []
