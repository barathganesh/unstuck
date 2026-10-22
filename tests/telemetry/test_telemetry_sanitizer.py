"""Deterministic private to public sanitization.

The private record is the complete safe evidence. The public record is what a
hackathon audience may read. These tests pin the difference: what survives, what
becomes a hash, what is removed, and that the result is still enough to
reconstruct the run.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_sanitizer.py -q
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from unstuck.telemetry import (
    CORRELATION_TREATMENT,
    INFRASTRUCTURE_TREATMENT,
    Correlation,
    CorrelationContext,
    DecisionTelemetry,
    HistoryReference,
    InfrastructureDimensions,
    OutcomeTelemetry,
    ReceiptTelemetry,
    TelemetryRecord,
    TelemetryTimeline,
    ToolTelemetry,
    public_correlation,
    public_digest,
    public_findings,
    public_infrastructure,
    sanitize,
    sanitize_timeline,
)

DIGEST = "a" * 64
OTHER = "b" * 64
SCHEMA = "inspect_schema_keys"
SCOPE = "inspect_scope"

HOSTED = InfrastructureDimensions(
    source="runtime_configuration",
    account_id="123456789012",
    region="us-east-1",
    runtime_id="unstuck-runtime-EXAMPLE",
    endpoint_id="DEFAULT",
    agent_id="unstuck-agent",
    log_group="/aws/bedrock-agentcore/runtimes/unstuck-runtime-EXAMPLE",
    model_id="anthropic.claude-example-v1",
    principal_arn="arn:aws:iam::123456789012:role/unstuck-runtime",
)

CONTEXT = CorrelationContext(
    experiment_id="exp-1",
    case_id="case-01",
    arm="decider_memory",
    call_index=2,
    idempotency_identity_hash=DIGEST,
    request_id=uuid4(),
    runtime_session_id="runtime-session-EXAMPLE",
    trace_id="0af7651916cd43dd8448eb211c80319c",
    span_id="b7ad6b7169203331",
    parent_span_id="00f067aa0ba902b7",
    aws_request_id="d2f0c0a4-EXAMPLE",
)


def correlation(run_id: object | None = None, sequence: int = 1) -> Correlation:
    supplied = CONTEXT.model_dump()
    supplied.pop("schema_version", None)
    return Correlation.model_validate(
        supplied
        | {
            "run_id": run_id or uuid4(),
            "incident_id": uuid4(),
            "event_id": uuid4(),
            "sequence": sequence,
            "timestamp": datetime.now(UTC),
        }
    )


def private(run_id: object | None = None, sequence: int = 1) -> TelemetryRecord:
    return TelemetryRecord(
        kind="decision_receipt_written",
        stage="RUN_PROBE",
        correlation=correlation(run_id, sequence),
        infrastructure=HOSTED,
        decision=DecisionTelemetry(
            pass_index=1,
            decision_id=uuid4(),
            selected_public_tool_id=SCOPE,
            confidence=0.95,
            accepted=True,
            decision_context_hash=DIGEST,
            problem_fingerprint=OTHER,
            history_status="matched",
            history_receipt_ids=(DIGEST,),
            history_references=(
                HistoryReference(
                    receipt_id=DIGEST,
                    match_class="exact",
                    outcome_class="no_progress",
                    public_tool_id=SCOPE,
                    reason_code="insufficient_evidence",
                    blocking_eligible=True,
                    binding_hash=OTHER,
                ),
            ),
        ),
        tool=ToolTelemetry(
            public_tool_id=SCOPE,
            execution_key=DIGEST,
            arguments_hash=OTHER,
            evidence_before_hash=DIGEST,
            evidence_after_hash=DIGEST,
            admitted_evidence=False,
            disposition_moved=False,
            provider_status="evidence",
        ),
        receipt=ReceiptTelemetry(
            receipt_id=DIGEST,
            outcome_class="no_progress",
            reason_code="insufficient_evidence",
            blocking_eligible=True,
            stored=True,
        ),
        outcome=OutcomeTelemetry(
            terminal_status="completed",
            terminal_reason="populated_after_repair",
            verification_status="passed",
            row_count=3,
            row_digest=OTHER,
            d2_triggered=True,
        ),
    )


def test_contract_every_identity_bearing_field_has_one_declared_treatment():
    assert set(CORRELATION_TREATMENT.values()) == {"hash"}
    # Every raw correlation field the contract refuses in public has a declared
    # treatment, so none can survive by being overlooked here.
    assert set(CORRELATION_TREATMENT) == {
        "experiment_id",
        "case_id",
        "request_id",
        "runtime_session_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "aws_request_id",
    }
    assert set(INFRASTRUCTURE_TREATMENT.values()) == {"remove"}
    # The declared infrastructure treatment covers exactly the supplied set.
    assert len(INFRASTRUCTURE_TREATMENT) == len(HOSTED.supplied)


def test_security_sanitization_hashes_the_correlation_and_drops_the_raw_values():
    public = public_correlation(correlation())
    assert not public.identity_bearing
    assert public.request_id is None and public.runtime_session_id is None
    assert public.trace_id is None and public.aws_request_id is None
    assert public.trace_id_hash == public_digest(CONTEXT.trace_id or "")
    assert public.runtime_session_id_hash is not None
    assert public.aws_request_id_hash is not None
    # The schema types the experiment and case labels as free identifiers, so
    # nothing can prove a given value is a synthetic label rather than a
    # customer, tenant, user, or case name. They are hashed, and a public
    # bundle still groups by them because the hash is stable.
    assert public.experiment_id is None and public.case_id is None
    assert public.experiment_id_hash == public_digest("exp-1")
    assert public.case_id_hash == public_digest("case-01")
    assert public.arm == "decider_memory"
    assert public.call_index == 2
    assert public.idempotency_identity_hash == DIGEST


def test_security_a_customer_shaped_case_label_cannot_reach_a_public_record():
    """Regression: the sanitizer used to keep a case label unchanged.

    `customer-acme` matches no forbidden pattern, so nothing but hashing the
    typed field removes it. The contract refuses the raw value too, so the
    refusal holds even if the sanitizer is skipped.
    """
    named = Correlation.model_validate(
        correlation().model_dump() | {"case_id": "customer-acme", "schema_version": "1"}
    )
    public = public_correlation(named)
    assert public.case_id is None
    assert public.case_id_hash == public_digest("customer-acme")
    assert "customer-acme" not in public.model_dump_json()
    with pytest.raises(ValidationError):
        TelemetryRecord(kind="probe_started", visibility="public", correlation=named)


def test_security_the_span_links_survive_being_hashed():
    """A hashed parent still points at the hashed span it refers to."""
    child = public_correlation(correlation())
    parent = public_correlation(
        Correlation.model_validate(
            correlation().model_dump()
            | {"span_id": CONTEXT.parent_span_id, "schema_version": "1"}
        )
    )
    assert child.parent_span_id_hash == parent.span_id_hash


def test_security_the_hosting_dimensions_reduce_to_one_stable_identity_hash():
    public = public_infrastructure(HOSTED)
    assert not public.identity_bearing
    assert public.identity_hash is not None and len(public.identity_hash) == 64
    assert public.source == "runtime_configuration"
    assert public == public_infrastructure(HOSTED)
    moved = InfrastructureDimensions.model_validate(
        HOSTED.model_dump() | {"region": "eu-west-1"}
    )
    assert public_infrastructure(moved).identity_hash != public.identity_hash
    # Nothing supplied means nothing hashed, not the hash of emptiness.
    absent = public_infrastructure(InfrastructureDimensions())
    assert absent.identity_hash is None and absent.source == "not_supplied"


def test_security_a_sanitized_record_carries_no_account_principal_or_path():
    public = sanitize(private())
    rendered = public.model_dump_json()
    assert public.visibility == "public"
    for forbidden in (
        "123456789012",
        "arn:aws:iam",
        "us-east-1",
        "unstuck-runtime-EXAMPLE",
        "/aws/bedrock-agentcore",
        "anthropic.claude-example-v1",
        "runtime-session-EXAMPLE",
        "d2f0c0a4-EXAMPLE",
        "0af7651916cd43dd8448eb211c80319c",
    ):
        assert forbidden not in rendered
    assert (
        public_findings(
            TelemetryTimeline(
                visibility="public",
                run_id=public.correlation.run_id,
                records=(public,),
            )
        )
        == ()
    )


def test_the_public_export_still_reconstructs_why_each_decision_was_made():
    """Sanitization removes identity, not the causal story."""
    public = sanitize(private())
    decision, tool, receipt, outcome = (
        public.decision,
        public.tool,
        public.receipt,
        public.outcome,
    )
    assert decision is not None and tool is not None
    assert receipt is not None and outcome is not None
    # Why the first decision chose what it chose.
    assert decision.pass_index == 1
    assert decision.selected_public_tool_id == SCOPE
    assert decision.confidence == 0.95
    assert decision.history_status == "matched"
    assert decision.history_receipt_ids == (DIGEST,)
    assert decision.decision_context_hash == DIGEST
    # What ran and what the evidence did.
    assert tool.public_tool_id == SCOPE
    assert tool.execution_key == DIGEST
    assert tool.admitted_evidence is False
    assert tool.disposition_moved is False
    # Why the second decision opened, and what was verified.
    assert receipt.outcome_class == "no_progress"
    assert receipt.blocking_eligible is True
    assert outcome.d2_triggered is True
    assert outcome.terminal_status == "completed"
    assert outcome.verification_status == "passed"
    assert outcome.row_count == 3 and outcome.row_digest == OTHER
    # Measurement availability survives, so an unmeasured figure stays unmeasured.
    assert public.measurements.selector_latency_ms.availability == "not_measured"
    assert public.measurements.total_tokens.availability == "not_supplied"


def test_sanitization_is_deterministic_and_idempotent():
    once = sanitize(private())
    assert sanitize(once) == once
    # The same private input always yields the same public output, so a public
    # bundle can be content-addressed by a later leaf.
    fixed = private(run_id=once.correlation.run_id)
    first = sanitize(fixed)
    second = sanitize(fixed)
    assert first.model_dump_json() == second.model_dump_json()


def test_a_whole_timeline_is_sanitized_in_its_recorded_order():
    run_id = uuid4()
    timeline = TelemetryTimeline(
        run_id=run_id,
        records=(private(run_id, 1), private(run_id, 2), private(run_id, 3)),
    )
    public = sanitize_timeline(timeline)
    assert public.visibility == "public"
    assert [item.correlation.sequence for item in public.records] == [1, 2, 3]
    assert all(item.visibility == "public" for item in public.records)
    assert sanitize_timeline(public) == public
    assert public_findings(public) == ()


def test_security_a_minted_identity_is_never_mistaken_for_an_account_identifier():
    """Regression: a run of minted identities must not fail an export at random.

    A UUID's final group is twelve hex characters and is sometimes twelve
    digits, which is also the shape of an account identifier. Sanitizing many
    freshly minted records is what proves the two are told apart rather than
    told apart most of the time.
    """
    for _ in range(300):
        public = sanitize(private())
        assert (
            public_findings(
                TelemetryTimeline(
                    visibility="public",
                    run_id=public.correlation.run_id,
                    records=(public,),
                )
            )
            == ()
        )


def test_security_a_public_record_cannot_be_built_without_sanitizing_it():
    """The contract is the last line, so skipping the sanitizer still fails."""
    unsanitized = private()
    with pytest.raises(ValidationError):
        TelemetryRecord.model_validate(
            unsanitized.model_dump() | {"visibility": "public"}
        )
