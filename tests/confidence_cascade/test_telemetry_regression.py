"""Cascade regression: the telemetry schema still describes what D1, D2 and the
verifier actually produce.

Telemetry is a projection of the cascade's own facts, so the two can drift: a
new outcome class, match class, reason, blocked class, history status, incident
state, or terminal status would widen the cascade without widening the record
that is supposed to describe it. These tests pin every closed taxonomy the
cascade owns against the telemetry field that has to carry it, so the drift
fails here rather than silently producing telemetry that cannot say what
happened.

The second property guarded here is the execution identity. The cascade matches
receipts on the private capability; telemetry names an opaque key built from the
public identity. Both must partition executions the same way, or two different
reads would look like one in the record.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_telemetry_regression.py -q
"""

from datetime import UTC, datetime
from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from unstuck.cascade.cards import capability_cards
from unstuck.cascade.matching import HistoryStatus, MatchClass, ReceiptMatch
from unstuck.cascade.projector import BlockedExecution
from unstuck.cascade.receipts import OUTCOME_REASONS, RECEIPT_REASONS, OutcomeClass
from unstuck.domain import Counters, Incident, Reason, terminal_mapping
from unstuck.telemetry import (
    Correlation,
    DecisionTelemetry,
    HistoryReference,
    OutcomeTelemetry,
    ReceiptTelemetry,
    TelemetryRecord,
    ToolTelemetry,
    execution_key,
)

BEFORE_HASH = "1" * 64
AFTER_HASH = "2" * 64


def correlation() -> Correlation:
    return Correlation(
        run_id=uuid4(), event_id=uuid4(), sequence=1, timestamp=datetime.now(UTC)
    )


def test_every_verifier_outcome_class_is_recordable():
    for outcome in get_args(OutcomeClass):
        assert ReceiptTelemetry(outcome_class=outcome).outcome_class == outcome
    assert set(OUTCOME_REASONS) == set(get_args(OutcomeClass))
    with pytest.raises(ValidationError):
        ReceiptTelemetry(outcome_class="invented_outcome")


def test_every_verifier_reason_is_recordable_as_a_reason_code():
    for reason in sorted(RECEIPT_REASONS):
        assert ReceiptTelemetry(reason_code=reason.value).reason_code == reason.value
    # Every terminal reason the core can report fits the terminal field too.
    for reason in Reason:
        recorded = OutcomeTelemetry(terminal_reason=reason.value)
        assert recorded.terminal_reason == reason.value
        _, disposition, status = terminal_mapping(reason)
        mapped = OutcomeTelemetry(
            terminal_status=status,
            terminal_reason=reason.value,
            disposition=disposition,
        )
        assert mapped.terminal_status == status
        assert mapped.disposition == disposition


def test_every_match_class_and_history_status_is_recordable():
    for match in get_args(MatchClass):
        assert ReceiptTelemetry(match_class=match).match_class == match
    for status in get_args(HistoryStatus):
        assert DecisionTelemetry(history_status=status).history_status == status
    with pytest.raises(ValidationError):
        DecisionTelemetry(history_status="invented_status")


def test_every_blocked_class_the_cascade_reports_is_recordable():
    for blocked in ("exact_repeat", "failed_execution"):
        refusal = BlockedExecution(
            public_tool_id="inspect_scope",
            selected_capability="inspect_scope",
            arguments_hash=BEFORE_HASH,
            receipt_id=AFTER_HASH,
            outcome_class="no_progress",
            reason_code=Reason.INSUFFICIENT,
            blocked_class=blocked,
        )
        recorded = OutcomeTelemetry(
            blocked_public_tool_id=refusal.public_tool_id,
            blocked_reason_code=refusal.reason_code.value,
            blocked_class=refusal.blocked_class,
        )
        assert recorded.blocked_class == blocked
        # The private capability is never a telemetry field at all.
        assert "selected_capability" not in recorded.model_dump()


def test_a_classified_receipt_match_projects_into_telemetry_without_loss():
    match = ReceiptMatch(
        receipt_id=AFTER_HASH,
        match_class="exact",
        outcome_class="no_progress",
        public_tool_id="inspect_scope",
        selected_capability="inspect_scope",
        arguments_hash=BEFORE_HASH,
        reason_code=Reason.INSUFFICIENT,
        admitted_evidence=False,
    )
    recorded = ReceiptTelemetry(
        receipt_id=match.receipt_id,
        match_class=match.match_class,
        outcome_class=match.outcome_class,
        reason_code=match.reason_code.value,
        blocking_eligible=match.blocking,
    )
    assert recorded.blocking_eligible is match.blocking is True
    assert recorded.receipt_id == match.receipt_id
    assert "inspect_scope" not in recorded.model_dump_json()


def test_a_whole_lookup_projects_one_structured_reference_per_receipt():
    """Each receipt keeps its own classification rather than joining an array.

    Parallel arrays of identities, match classes and outcome classes cannot
    say which outcome belonged to which receipt, and can drift apart. This
    pins the cascade's own per-receipt facts against the telemetry field that
    has to carry them, including the eligibility the verifier decided.
    """
    matches = (
        ReceiptMatch(
            receipt_id=AFTER_HASH,
            match_class="exact",
            outcome_class="no_progress",
            public_tool_id="inspect_scope",
            selected_capability="inspect_scope",
            arguments_hash=BEFORE_HASH,
            reason_code=Reason.INSUFFICIENT,
        ),
        ReceiptMatch(
            receipt_id="3" * 64,
            match_class="compatible",
            outcome_class="useful_evidence",
            public_tool_id="inspect_pipeline",
            selected_capability="inspect_pipeline",
            arguments_hash=BEFORE_HASH,
            reason_code=Reason.POPULATED_AFTER_REPAIR,
            admitted_evidence=True,
        ),
    )
    recorded = DecisionTelemetry(
        history_status="matched",
        history_receipt_ids=tuple(match.receipt_id for match in matches),
        history_reference_count=len(matches),
        history_references=tuple(
            HistoryReference(
                receipt_id=match.receipt_id,
                match_class=match.match_class,
                outcome_class=match.outcome_class,
                public_tool_id=match.public_tool_id,
                reason_code=match.reason_code.value,
                blocking_eligible=match.blocking,
                admitted_evidence=match.admitted_evidence,
                arguments_hash=match.arguments_hash,
                binding_hash=execution_key(match.public_tool_id, match.arguments_hash),
            )
            for match in matches
        ),
    )
    assert recorded.history_match_classes == ("exact", "compatible")
    assert [item.outcome_class for item in recorded.history_references] == [
        "no_progress",
        "useful_evidence",
    ]
    assert [item.blocking_eligible for item in recorded.history_references] == [
        match.blocking for match in matches
    ]
    # The bindings partition the two retrieved executions, and the private
    # canonical capability still never travels.
    bindings = {item.binding_hash for item in recorded.history_references}
    assert len(bindings) == 2
    assert "selected_capability" not in recorded.model_dump_json()
    # A reference list that disagrees with its own identities is refused.
    with pytest.raises(ValidationError):
        DecisionTelemetry(
            history_receipt_ids=(AFTER_HASH,),
            history_references=recorded.history_references,
        )


def test_the_opaque_execution_key_partitions_executions_as_the_cascade_does():
    """The public key and the private key must separate the same executions."""
    cards = capability_cards()
    private = {(binding.capability, BEFORE_HASH) for binding in cards}
    public = {
        execution_key(binding.card.public_tool_id, BEFORE_HASH) for binding in cards
    }
    assert len(public) == len(private) == len(cards)
    # The same read under different bound arguments is a different execution in
    # both schemes, which is what stops a repair from reusing a stale receipt.
    repaired = {
        execution_key(binding.card.public_tool_id, AFTER_HASH) for binding in cards
    }
    assert public.isdisjoint(repaired)


def test_the_incident_states_the_controller_moves_through_are_recordable():
    """Every stage label the controller can set fits the telemetry stage field."""
    states = get_args(Incident.model_fields["state"].annotation)
    assert states
    for stage in states:
        assert (
            TelemetryRecord(
                kind="probe_started", stage=stage, correlation=correlation()
            ).stage
            == stage
        )


def test_the_budget_ledger_round_trips_into_the_terminal_record():
    counters = Counters(
        diagnostic_reads=2,
        decider_calls=2,
        history_lookups=1,
        repair_attempts=1,
        report_queries=3,
        wait_checks=1,
        agent_turns=1,
    )
    recorded = OutcomeTelemetry(counters=counters)
    assert recorded.counters == counters


def test_a_tool_record_states_both_proven_deltas_without_collapsing_them():
    """A read that admitted a fact but moved no gate stays distinguishable."""
    admitted = ToolTelemetry(
        public_tool_id="inspect_scope",
        evidence_before_hash=BEFORE_HASH,
        evidence_after_hash=AFTER_HASH,
        admitted_evidence=True,
        disposition_before_hash=BEFORE_HASH,
        disposition_after_hash=BEFORE_HASH,
        disposition_moved=False,
    )
    assert admitted.admitted_evidence is True
    assert admitted.disposition_moved is False
    stalled = ToolTelemetry(
        public_tool_id="inspect_scope",
        evidence_before_hash=BEFORE_HASH,
        evidence_after_hash=BEFORE_HASH,
        admitted_evidence=False,
        disposition_moved=False,
    )
    assert stalled.admitted_evidence is False
    assert admitted != stalled
