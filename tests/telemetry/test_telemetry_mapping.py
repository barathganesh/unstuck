"""Projection of one durable event onto one telemetry record.

Every test builds the durable `EventRecord` the controller would have written
and asserts what telemetry reads out of it. Nothing is inferred: a field the
event does not carry, or carries in the wrong shape, must come back absent.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_mapping.py -q
"""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import JsonValue

from unstuck.domain import Counters
from unstuck.events import EventRecord, Measurement
from unstuck.telemetry import (
    CorrelationContext,
    InfrastructureDimensions,
    correlation_for,
    execution_key,
    map_event,
)

QUERY_HASH = "c" * 64
STATE_HASH = "d" * 64
CONTEXT_HASH = "e" * 64
FINGERPRINT = "f" * 64
RECEIPT_ID = "1" * 64
SCHEMA = "inspect_schema_keys"
SCOPE = "inspect_scope"


def event(
    kind: str,
    payload: dict[str, JsonValue] | None = None,
    *,
    sequence: int = 1,
    budgets: Counters | None = None,
    duration: Measurement | None = None,
    run_id: object | None = None,
    incident_id: object | None = None,
) -> EventRecord:
    return EventRecord(
        run_id=run_id or uuid4(),
        incident_id=incident_id or uuid4(),
        sequence=sequence,
        timestamp=datetime.now(UTC),
        type=kind,
        input_hash=QUERY_HASH,
        output_hash=STATE_HASH,
        budgets=budgets or Counters(),
        duration_ms=duration or Measurement(),
        payload=payload or {},
    )


def test_contract_an_event_with_no_builder_still_produces_a_correlated_record():
    """A complete timeline matters more than a rich record for every event."""
    source = event("agent_turn_started", sequence=3)
    mapped = map_event(source)
    assert mapped.kind == "agent_turn_started"
    assert mapped.correlation.run_id == source.run_id
    assert mapped.correlation.incident_id == source.incident_id
    assert mapped.correlation.event_id == source.event_id
    assert mapped.correlation.sequence == 3
    assert mapped.correlation.timestamp == source.timestamp
    assert (mapped.decision, mapped.tool, mapped.receipt, mapped.outcome) == (
        None,
        None,
        None,
        None,
    )


def test_the_supplied_correlation_identity_binds_to_the_durable_event():
    context = CorrelationContext(
        experiment_id="exp-1",
        case_id="case-01",
        arm="decider_memory",
        call_index=2,
        trace_id="0af7651916cd43dd8448eb211c80319c",
        span_id="b7ad6b7169203331",
        parent_span_id="00f067aa0ba902b7",
        runtime_session_id="runtime-session-9",
        aws_request_id="aws-request-9",
        request_id=uuid4(),
        idempotency_identity_hash=FINGERPRINT,
    )
    bound = correlation_for(event("run_started"), context)
    assert bound.experiment_id == "exp-1"
    assert bound.arm == "decider_memory"
    assert bound.call_index == 2
    assert bound.trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert bound.parent_span_id == "00f067aa0ba902b7"
    assert bound.identity_bearing
    # The hashed twins stay empty until the public projection fills them.
    assert bound.trace_id_hash is None and bound.aws_request_id_hash is None


def test_an_absent_or_malformed_measurement_is_never_read_as_zero():
    absent = map_event(event("decider_first_returned", {}))
    assert absent.measurements.selector_latency_ms.availability == "not_measured"
    assert absent.measurements.selector_latency_ms.value is None

    unprovided = map_event(
        event(
            "decider_first_returned",
            {"measured_latency_ms": {"availability": "not_provided", "value": None}},
        )
    )
    assert unprovided.measurements.selector_latency_ms.availability == "not_provided"
    assert unprovided.measurements.selector_latency_ms.value is None

    broken = map_event(
        event(
            "decider_first_returned",
            {"measured_latency_ms": {"availability": "measured", "value": "fast"}},
        )
    )
    assert broken.measurements.selector_latency_ms.availability == "invalid"
    assert broken.measurements.selector_latency_ms.value is None

    measured = map_event(
        event(
            "decider_first_returned",
            {"measured_latency_ms": {"availability": "measured", "value": 12.0}},
            duration=Measurement(availability="measured", value=4.0),
        )
    )
    assert measured.measurements.selector_latency_ms.value == 12.0
    assert measured.measurements.stage_duration_ms.value == 4.0


def test_a_measurement_with_an_unrecognized_provenance_claims_nothing():
    """A provenance this schema has never heard of is not a measurement."""
    unknown = map_event(
        event(
            "decider_first_returned",
            {"measured_latency_ms": {"availability": "estimated", "value": 9.0}},
        )
    )
    assert unknown.measurements.selector_latency_ms.availability == "not_measured"
    assert unknown.measurements.selector_latency_ms.value is None
    negative = map_event(
        event(
            "decider_first_returned",
            {"measured_latency_ms": {"availability": "measured", "value": -1.0}},
        )
    )
    assert negative.measurements.selector_latency_ms.availability == "invalid"
    flagged = map_event(
        event(
            "decider_first_returned",
            {"measured_latency_ms": {"availability": "measured", "value": True}},
        )
    )
    assert flagged.measurements.selector_latency_ms.availability == "invalid"


def test_a_malformed_distribution_is_dropped_whole_rather_than_half_read():
    """Half a distribution is not evidence about what the Decider reported."""
    for broken in (
        [SCHEMA],
        [{"id": SCHEMA, "probability": True}],
        [{"id": 7, "probability": 1.0}],
        [{"id": SCHEMA, "probability": "high"}],
        [{"id": SCHEMA, "probability": 1.0}, {"id": SCOPE, "probability": 2.0}],
        "not-a-list",
    ):
        mapped = map_event(event("decider_first_returned", {"probabilities": broken}))
        assert mapped.decision is not None
        assert mapped.decision.probabilities is None


def test_a_malformed_evidence_identity_is_skipped_rather_than_invented():
    mapped = map_event(
        event(
            "decision_receipt_written",
            {
                "public_tool_id": SCHEMA,
                "arguments_hash": QUERY_HASH,
                "outcome_class": "useful_evidence",
                "reason_code": "populated_initial",
                "evidence_before_hash": STATE_HASH,
                "evidence_after_hash": CONTEXT_HASH,
                "evidence_ids": [str(uuid4()), "not-a-uuid", 7],
                "admitted_evidence": True,
            },
        )
    )
    assert mapped.tool is not None
    assert len(mapped.tool.evidence_ids) == 1


def test_a_payload_field_of_the_wrong_shape_becomes_an_absent_field():
    mapped = map_event(
        event(
            "decider_first_returned",
            {
                "pass_index": "first",
                "decision_id": "not-a-uuid",
                "confidence": 4.0,
                "accepted": "yes",
                "state_hash": "short",
                "candidate_ids": [SCHEMA, 7, None],
                "probabilities": [{"id": SCHEMA}],
                "validation_status": "maybe",
            },
        )
    )
    decision = mapped.decision
    assert decision is not None
    assert decision.pass_index is None
    assert decision.decision_id is None
    assert decision.confidence is None
    assert decision.accepted is None
    assert decision.selector_input_hash is None
    assert decision.validation_status is None
    assert decision.candidate_public_ids == (SCHEMA,)
    assert decision.probabilities is None


def test_a_selector_request_records_what_was_offered_and_what_remained():
    mapped = map_event(
        event(
            "decider_first_requested",
            {
                "pass_index": 1,
                "state_hash": STATE_HASH,
                "decision_context_hash": CONTEXT_HASH,
                "problem_fingerprint": FINGERPRINT,
                "history_status": "matched",
                "available_tool_ids": [SCHEMA, SCOPE],
                "remaining_selector_calls": 2,
                "remaining_diagnostic_reads": 2,
            },
        )
    )
    decision = mapped.decision
    outcome = mapped.outcome
    assert decision is not None and outcome is not None
    assert decision.pass_index == 1
    assert decision.selector_input_hash == STATE_HASH
    assert decision.decision_context_hash == CONTEXT_HASH
    assert decision.problem_fingerprint == FINGERPRINT
    assert decision.candidate_public_ids == (SCHEMA, SCOPE)
    assert [item.public_tool_id for item in decision.candidate_availability] == [
        SCHEMA,
        SCOPE,
    ]
    assert all(item.available for item in decision.candidate_availability)
    assert decision.remaining_selector_calls == 2
    assert outcome.d2_triggered is False


def test_a_second_selector_request_is_recorded_as_the_second_decision_opening():
    mapped = map_event(
        event(
            "decider_final_requested",
            {"pass_index": 2, "available_tool_ids": [SCHEMA]},
        )
    )
    assert mapped.outcome is not None and mapped.outcome.d2_triggered is True
    assert mapped.decision is not None and mapped.decision.pass_index == 2


def test_a_selector_answer_records_the_reported_distribution_as_reported():
    mapped = map_event(
        event(
            "decider_first_returned",
            {
                "pass_index": 1,
                "decision_id": str(uuid4()),
                "model_call_id": str(uuid4()),
                "selected_id": SCHEMA,
                "confidence": 0.95,
                "accepted": True,
                "validation_status": "valid",
                "model_version": "scripted-v1",
                "candidate_ids": [SCHEMA, SCOPE, "request_review"],
                "probabilities": [
                    {"id": SCHEMA, "probability": 1.0},
                    {"id": SCOPE, "probability": 0.0},
                    {"id": "request_review", "probability": 0.0},
                ],
                "token_usage": {"availability": "not_provided", "value": None},
                "measured_cost": {"availability": "not_measured", "value": None},
            },
        )
    )
    decision = mapped.decision
    assert decision is not None
    assert decision.selected_public_tool_id == SCHEMA
    assert decision.confidence == 0.95
    assert decision.accepted is True
    assert decision.validation_status == "valid"
    assert decision.model_revision == "scripted-v1"
    assert decision.probabilities is not None
    assert [item.id for item in decision.probabilities] == [
        SCHEMA,
        SCOPE,
        "request_review",
    ]
    assert mapped.measurements.total_tokens.availability == "not_provided"
    assert mapped.measurements.cost.availability == "not_measured"


def test_security_a_tool_record_names_a_public_identity_and_an_opaque_key():
    mapped = map_event(
        event(
            "probe_started",
            {
                "tool_id": SCHEMA,
                "pass_index": 1,
                "diagnostic_call_id": str(uuid4()),
            },
            budgets=Counters(diagnostic_reads=1),
        )
    )
    tool = mapped.tool
    assert tool is not None
    assert tool.public_tool_id == SCHEMA
    assert tool.arguments_hash == QUERY_HASH
    assert tool.execution_key == execution_key(SCHEMA, QUERY_HASH)
    assert tool.read_index == 1 and tool.attempt_index == 1
    # The private canonical capability never travels, so a reader can compare
    # executions without learning the execution surface.
    assert "selected_capability" not in mapped.model_dump_json()


def test_a_written_receipt_records_both_proven_deltas_separately():
    mapped = map_event(
        event(
            "decision_receipt_written",
            {
                "receipt_id": RECEIPT_ID,
                "decision_id": str(uuid4()),
                "model_call_id": str(uuid4()),
                "diagnostic_call_id": str(uuid4()),
                "problem_fingerprint": FINGERPRINT,
                "decision_context_hash": CONTEXT_HASH,
                "namespace_hash": STATE_HASH,
                "versions_hash": QUERY_HASH,
                "public_tool_id": SCOPE,
                "arguments_hash": QUERY_HASH,
                "outcome_class": "no_progress",
                "reason_code": "insufficient_evidence",
                "evidence_before_hash": STATE_HASH,
                "evidence_after_hash": STATE_HASH,
                "evidence_ids": [],
                "disposition_before_hash": CONTEXT_HASH,
                "disposition_after_hash": CONTEXT_HASH,
                "admitted_evidence": False,
                "blocking": True,
                "stored": True,
            },
        )
    )
    tool, receipt = mapped.tool, mapped.receipt
    assert tool is not None and receipt is not None
    assert tool.admitted_evidence is False
    assert tool.disposition_moved is False
    assert tool.evidence_ids == ()
    assert receipt.receipt_id == RECEIPT_ID
    assert receipt.outcome_class == "no_progress"
    assert receipt.blocking_eligible is True
    assert receipt.namespace_hash == STATE_HASH
    assert receipt.versions_hash == QUERY_HASH
    assert receipt.created is True and receipt.stored is True


def test_a_refused_repetition_records_the_block_and_its_matching_receipt():
    mapped = map_event(
        event(
            "repeat_blocked",
            {
                "public_tool_id": SCOPE,
                "arguments_hash": QUERY_HASH,
                "receipt_id": RECEIPT_ID,
                "outcome_class": "no_progress",
                "reason_code": "insufficient_evidence",
                "blocked_class": "exact_repeat",
            },
        )
    )
    outcome, receipt = mapped.outcome, mapped.receipt
    assert outcome is not None and receipt is not None
    assert outcome.blocked_public_tool_id == SCOPE
    assert outcome.blocked_reason_code == "insufficient_evidence"
    assert outcome.blocked_class == "exact_repeat"
    assert receipt.match_class == "exact" and receipt.blocking_eligible is True


def test_a_terminal_event_records_the_result_as_a_count_and_a_digest():
    mapped = map_event(
        event(
            "run_finished",
            {
                "status": "completed",
                "reason_code": "populated_after_repair",
                "disposition": "CONTINUE",
                "rows": [{"country_code": "DE", "units": 3}],
                "verification_status": "passed",
                "verification_id": str(uuid4()),
                "pass_count": 2,
                "selection_pass": 2,
                "final_choice": SCHEMA,
                "final_confidence": 0.95,
                "history_status": "matched",
                "receipt_ids": [RECEIPT_ID],
                "history_truncated": False,
                "blocked_tool_id": SCOPE,
                "blocked_reason_code": "insufficient_evidence",
                "duration_ms": {"availability": "measured", "value": 40.0},
                "cost": {"availability": "not_measured", "value": None},
            },
            budgets=Counters(diagnostic_reads=2, decider_calls=2),
        )
    )
    outcome, decision = mapped.outcome, mapped.decision
    assert outcome is not None and decision is not None
    assert outcome.terminal_status == "completed"
    assert outcome.terminal_reason == "populated_after_repair"
    assert outcome.disposition == "CONTINUE"
    assert outcome.row_count == 1
    assert outcome.row_digest is not None and len(outcome.row_digest) == 64
    assert outcome.verification_status == "passed"
    assert outcome.d2_triggered is True
    assert outcome.counters == Counters(diagnostic_reads=2, decider_calls=2)
    assert decision.history_receipt_ids == (RECEIPT_ID,)
    assert mapped.measurements.end_to_end_ms.value == 40.0
    assert mapped.measurements.cost.availability == "not_measured"
    # The rows themselves are never recorded, only their count and digest.
    assert "country_code" not in mapped.model_dump_json()


def test_the_retry_state_is_recorded_only_where_a_caller_supplies_it():
    plain = map_event(event("run_started"))
    assert plain.outcome is None
    resumed = map_event(event("run_started"), retry_state="resumed")
    assert resumed.outcome is not None
    assert resumed.outcome.retry_state == "resumed"
    duplicate = map_event(
        event("run_finished", {"status": "completed"}), retry_state="duplicate"
    )
    assert duplicate.outcome is not None
    assert duplicate.outcome.retry_state == "duplicate"
    assert duplicate.outcome.terminal_status == "completed"


def test_the_hosting_dimensions_travel_from_configuration_and_default_to_absent():
    plain = map_event(event("run_started"))
    assert plain.infrastructure.source == "not_supplied"
    assert not plain.infrastructure.identity_bearing
    configured = map_event(
        event("run_started"),
        infrastructure=InfrastructureDimensions(
            source="runtime_configuration", region="eu-west-1"
        ),
    )
    assert configured.infrastructure.region == "eu-west-1"
    assert configured.infrastructure.identity_bearing


def test_an_unresolved_selection_records_why_no_choice_was_accepted():
    mapped = map_event(
        event("selection_unresolved", {"reason_code": "final_selection_rejected"})
    )
    assert mapped.outcome is not None
    assert mapped.outcome.terminal_reason == "final_selection_rejected"
    assert mapped.outcome.d2_refusal_reason == "final_selection_rejected"


def test_a_budget_stop_records_the_allowance_that_ran_out():
    mapped = map_event(event("budget_stop", {"reason_code": "run_deadline"}))
    assert mapped.outcome is not None
    assert mapped.outcome.terminal_reason == "run_deadline"
    assert mapped.outcome.d2_refusal_reason is None


def test_the_wait_repair_and_verification_path_is_each_recorded_once():
    assert map_event(event("wait_started")).outcome.wait_started is True
    assert map_event(event("repair_requested")).outcome.repair_requested is True
    assert map_event(event("repair_applied")).outcome.repair_applied is True
    verified = map_event(
        event(
            "verification_finished",
            {
                "status": "passed",
                "verification_id": str(uuid4()),
                "evidence_ids": [str(uuid4())],
                "reason_code": "verified",
            },
        )
    )
    assert verified.outcome is not None
    assert verified.outcome.verification_status == "passed"
    assert verified.tool is not None and len(verified.tool.evidence_ids) == 1
