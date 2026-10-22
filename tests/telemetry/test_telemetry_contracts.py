"""The telemetry schema itself: versioning, missingness, and public invariants.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_contracts.py -q
"""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from unstuck.cascade.serialization import StageMeasurement
from unstuck.events import Measurement
from unstuck.telemetry import (
    NOT_MEASURED,
    NOT_SUPPLIED,
    TELEMETRY_VERSION,
    Correlation,
    DecisionTelemetry,
    InfrastructureDimensions,
    Measurements,
    OutcomeTelemetry,
    PricingProvenance,
    TelemetryMeasurement,
    TelemetryRecord,
    TelemetryTimeline,
    ToolTelemetry,
    execution_key,
    measurement_of,
    public_digest,
    unsafe_public_fields,
    unsafe_public_values,
)

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def correlation(**changes: object) -> Correlation:
    base: dict[str, object] = {
        "run_id": uuid4(),
        "event_id": uuid4(),
        "sequence": 1,
        "timestamp": datetime.now(UTC),
    }
    return Correlation.model_validate(base | changes)


def record(**changes: object) -> TelemetryRecord:
    base: dict[str, object] = {"kind": "probe_started", "correlation": correlation()}
    return TelemetryRecord.model_validate(base | changes)


def test_contract_the_envelope_carries_its_own_version_beside_the_contract_version():
    """Telemetry versions its field set independently of the project contract."""
    built = record()
    assert built.telemetry_version == TELEMETRY_VERSION == "telemetry-1"
    assert built.schema_version == "1"
    assert built.visibility == "private"
    assert built.mode == "offline" and built.provider == "mock"
    assert built.classification == "synthetic"
    with pytest.raises(ValidationError):
        record(telemetry_version="telemetry-2")


def test_contract_an_absent_measurement_is_recorded_as_absent_and_never_as_zero():
    assert NOT_MEASURED.availability == "not_measured" and NOT_MEASURED.value is None
    assert NOT_SUPPLIED.availability == "not_supplied" and NOT_SUPPLIED.value is None
    with pytest.raises(ValidationError):
        TelemetryMeasurement(availability="measured")
    with pytest.raises(ValidationError):
        TelemetryMeasurement(availability="not_measured", value=0.0)
    with pytest.raises(ValidationError):
        TelemetryMeasurement(availability="measured", value=-1.0)
    measured = TelemetryMeasurement(availability="measured", value=0.0)
    assert measured.value == 0.0


def test_contract_every_default_measurement_states_why_it_is_absent():
    """A fresh record claims no quantity at all, in a way a reader can tell."""
    defaults = Measurements()
    for name in (
        "stage_duration_ms",
        "selector_latency_ms",
        "tool_latency_ms",
        "receipt_lookup_ms",
        "receipt_storage_ms",
        "end_to_end_ms",
        "cost",
    ):
        assert getattr(defaults, name).availability == "not_measured"
        assert getattr(defaults, name).value is None
    for name in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cpu_percent",
        "memory_mib",
        "throttle_count",
        "error_count",
    ):
        assert getattr(defaults, name).availability == "not_supplied"
        assert getattr(defaults, name).value is None
    assert defaults.pricing.availability == "not_supplied"
    assert defaults.pricing.pricing_date is None and defaults.pricing.source is None


def test_a_core_or_cascade_measurement_widens_without_inventing_a_value():
    assert measurement_of(Measurement(availability="measured", value=12.5)) == (
        TelemetryMeasurement(availability="measured", value=12.5)
    )
    assert measurement_of(Measurement(availability="not_provided")).value is None
    assert measurement_of(Measurement(availability="not_provided")).availability == (
        "not_provided"
    )
    assert measurement_of(StageMeasurement(availability="provider_error")) == (
        TelemetryMeasurement(availability="provider_error")
    )
    # An unknown provenance claims nothing rather than claiming zero.
    assert measurement_of(object()) == NOT_MEASURED
    assert measurement_of(None) == NOT_MEASURED

    class Claimed:
        """A source that claims a measurement but carries no usable number."""

        availability = "measured"
        value = "fast"

    assert measurement_of(Claimed()) == TelemetryMeasurement(availability="invalid")


def test_a_measured_cost_states_its_pricing_date_and_source():
    with pytest.raises(ValidationError):
        PricingProvenance(availability="measured")
    with pytest.raises(ValidationError):
        PricingProvenance(availability="not_supplied", source="price-list")
    priced = PricingProvenance(
        availability="measured", pricing_date="2026-10-20", source="price-list"
    )
    assert priced.source == "price-list"


def test_security_the_hosting_dimensions_ship_no_value_and_require_configuration():
    """Environment identity is configuration, never a constant in this package."""
    empty = InfrastructureDimensions()
    assert empty.source == "not_supplied"
    assert empty.supplied == (None,) * 8
    assert not empty.identity_bearing
    with pytest.raises(ValidationError):
        InfrastructureDimensions(region="us-east-1")
    with pytest.raises(ValidationError):
        InfrastructureDimensions(identity_hash=DIGEST)
    configured = InfrastructureDimensions(
        source="runtime_configuration", region="us-east-1"
    )
    assert configured.identity_bearing


def test_security_a_public_record_refuses_raw_correlation_identity():
    with pytest.raises(ValidationError):
        record(visibility="public", correlation=correlation(trace_id="abcdef01"))
    with pytest.raises(ValidationError):
        record(visibility="public", correlation=correlation(request_id=uuid4()))
    with pytest.raises(ValidationError):
        record(
            visibility="public",
            correlation=correlation(runtime_session_id="session-9"),
        )
    allowed = record(
        visibility="public",
        correlation=correlation(trace_id_hash=DIGEST, span_id_hash=OTHER_DIGEST),
    )
    assert allowed.correlation.trace_id is None
    assert allowed.correlation.trace_id_hash == DIGEST


def test_security_a_public_record_refuses_raw_hosting_identity():
    with pytest.raises(ValidationError):
        record(
            visibility="public",
            infrastructure=InfrastructureDimensions(
                source="runtime_configuration", account_id="123456789012"
            ),
        )
    hashed = record(
        visibility="public",
        infrastructure=InfrastructureDimensions(
            source="runtime_configuration", identity_hash=DIGEST
        ),
    )
    assert hashed.infrastructure.identity_hash == DIGEST


def test_security_a_public_record_refuses_free_text_that_carries_private_identity():
    """The refusal is in the contract, so it holds even if the sanitizer is skipped."""
    for unsafe in (
        "arn:aws:iam::123456789012:role/unstuck",
        "123456789012",
        "/Users/builder/unstuck/private.jsonl",
        "AKIAIOSFODNN7EXAMPLE",
        "evaluator-truth-table",
        "authorization=Bearer abc",
    ):
        with pytest.raises(ValidationError):
            record(
                visibility="public",
                measurements=Measurements(
                    pricing=PricingProvenance(
                        availability="measured",
                        pricing_date="2026-10-20",
                        source=unsafe,
                    )
                ),
            )
    # A private record may retain the same detail; only the public form refuses.
    assert (
        record(
            measurements=Measurements(
                pricing=PricingProvenance(
                    availability="measured",
                    pricing_date="2026-10-20",
                    source="arn:aws:iam::123456789012:role/unstuck",
                )
            )
        ).visibility
        == "private"
    )


def test_the_untyped_public_scan_grants_no_exemption_for_a_value_shape():
    """An untyped scan has no field types to read, so it exempts nothing.

    A value is never trusted for looking opaque. A digest-width credential and
    a UUID-shaped external user identifier are exactly the shapes that would
    pass a shape test, so the shape test is not the one that runs.
    """
    assert unsafe_public_values(DIGEST) == ()
    # A sixty-four character lowercase hex value is the width of a digest, and
    # this scan still reports the account identifier inside it.
    credential = "f" * 48 + "123456789012" + "abcd"
    assert len(credential) == 64
    assert unsafe_public_values(credential) == (credential,)
    assert unsafe_public_values("123456789012") == ("123456789012",)
    assert unsafe_public_values(123456789012) == ()
    assert unsafe_public_values("a5d0a854-4373-512c-a639-123456789012") == (
        "a5d0a854-4373-512c-a639-123456789012",
    )
    assert unsafe_public_values({"a": ["arn:aws:s3:::bucket"]}) == (
        "arn:aws:s3:::bucket",
    )
    assert unsafe_public_values({"a": {"b": "/home/builder/x"}}) == ("/home/builder/x",)
    assert unsafe_public_values(None) == ()


def test_security_the_typed_public_scan_exempts_a_field_by_its_declared_type():
    """Opacity comes from the schema, so a free field is scanned regardless.

    `trace_id_hash` is typed as a digest and admits nothing else, so it is
    skipped. A pricing source is free text, so a sixty-four character
    credential sitting in it is still reported, and a minted identity in a
    `UUID` field is still skipped: that is the difference between deciding
    opacity by location and deciding it by shape.
    """
    hashed = record(correlation=correlation(trace_id_hash="0" * 64))
    assert unsafe_public_fields(hashed) == ()
    # A minted identity in a UUID-typed field is opaque by its own type, so a
    # run of fresh records cannot fail an export at random.
    for _ in range(300):
        assert unsafe_public_fields(record()) == ()
    credential = "f" * 48 + "123456789012" + "ab" * 2
    assert len(credential) == 64
    leaked = record(
        measurements=Measurements(
            pricing=PricingProvenance(
                availability="measured",
                pricing_date="2026-10-20",
                source=credential,
            )
        )
    )
    assert unsafe_public_fields(leaked) == (credential,)
    with pytest.raises(ValidationError):
        record(visibility="public", measurements=leaked.measurements)


def test_a_public_hash_is_stable_and_preserves_the_links_between_identities():
    parent = public_digest("00112233445566778899aabbccddeeff")
    assert parent == public_digest("00112233445566778899aabbccddeeff")
    assert parent != public_digest("ffeeddccbbaa99887766554433221100")
    assert len(parent) == 64


def test_security_an_execution_key_is_derived_from_public_values_alone():
    key = execution_key("inspect_schema_keys", DIGEST)
    assert key == execution_key("inspect_schema_keys", DIGEST)
    assert key != execution_key("inspect_scope", DIGEST)
    assert key != execution_key("inspect_schema_keys", OTHER_DIGEST)
    assert len(key) == 64
    assert "inspect_schema_keys" not in key


def test_a_drop_record_states_exactly_its_reason_and_carries_no_payload():
    dropped = record(kind="telemetry_dropped", drop_reason="boundary_refused")
    assert dropped.drop_reason == "boundary_refused"
    assert (dropped.decision, dropped.tool, dropped.receipt, dropped.outcome) == (
        None,
        None,
        None,
        None,
    )
    with pytest.raises(ValidationError):
        record(kind="telemetry_dropped")
    with pytest.raises(ValidationError):
        record(drop_reason="boundary_refused")
    with pytest.raises(ValidationError):
        record(
            kind="telemetry_dropped",
            drop_reason="boundary_refused",
            tool=ToolTelemetry(public_tool_id="inspect_scope"),
        )


def test_contract_a_timeline_is_ordered_by_the_durable_sequence():
    run_id = uuid4()

    def step(sequence: int) -> TelemetryRecord:
        return record(
            correlation=correlation(run_id=run_id, sequence=sequence),
            outcome=OutcomeTelemetry(terminal_reason="insufficient_evidence"),
        )

    timeline = TelemetryTimeline(run_id=run_id, records=(step(1), step(2), step(3)))
    assert [item.correlation.sequence for item in timeline.records] == [1, 2, 3]
    with pytest.raises(ValidationError):
        TelemetryTimeline(run_id=run_id, records=(step(2), step(1)))
    with pytest.raises(ValidationError):
        TelemetryTimeline(run_id=run_id, records=(step(1), step(1)))
    with pytest.raises(ValidationError):
        TelemetryTimeline(run_id=uuid4(), records=(step(1),))
    with pytest.raises(ValidationError):
        TelemetryTimeline(visibility="public", run_id=run_id, records=(step(1),))


def test_contract_a_timeline_refuses_two_records_with_one_event_identity():
    run_id = uuid4()
    event_id = uuid4()
    first = record(
        correlation=correlation(run_id=run_id, event_id=event_id, sequence=1)
    )
    second = record(
        correlation=correlation(run_id=run_id, event_id=event_id, sequence=2)
    )
    with pytest.raises(ValidationError):
        TelemetryTimeline(run_id=run_id, records=(first, second))


def test_contract_a_correlation_timestamp_must_be_utc():
    elsewhere = timezone(timedelta(hours=2))
    with pytest.raises(ValidationError):
        correlation(timestamp=datetime.now(elsewhere))
    assert correlation(timestamp=datetime.now(UTC)).sequence == 1


def test_contract_decision_telemetry_bounds_its_history_references():
    with pytest.raises(ValidationError):
        DecisionTelemetry(history_receipt_ids=tuple(DIGEST for _ in range(5)))
    with pytest.raises(ValidationError):
        DecisionTelemetry(confidence=1.5)
    bounded = DecisionTelemetry(
        history_receipt_ids=(DIGEST, OTHER_DIGEST), history_status="matched"
    )
    assert bounded.history_reference_count is None
