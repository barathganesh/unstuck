"""Span export: absence keeps its reason, cost exists, correlation survives.

The defect pinned here is that the conversion erased three things. Any
quantity whose availability was not `measured` produced no attribute at all,
so a collector could not tell `not_called` from `not_provided` from
`provider_error` from `invalid` from `not_measured`. Cost had no attribute
mapping, so it disappeared from the span surface entirely. And a sanitized
record's hashed correlation was never exported, so converting a public
timeline lost its runtime-session and trace links.

The fix pairs every quantity with its own availability attribute, maps cost
with its currency, unit, pricing date and source, and exports each hashed
correlation under an explicit `.hash` name rather than in a span context slot
a hash does not belong in.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_span_missingness.py -q
"""

from datetime import UTC, datetime
from typing import get_args
from uuid import uuid4

import pytest

from unstuck.telemetry import (
    AVAILABILITY_SUFFIX,
    MEASUREMENT_FIELDS,
    PUBLIC_WITHHELD,
    Availability,
    Correlation,
    InfrastructureDimensions,
    Measurements,
    PricingProvenance,
    TelemetryMeasurement,
    TelemetryRecord,
    sanitize,
    span_for,
)

DIGEST = "a" * 64
SCOPE = "inspect_scope"

HOSTED = InfrastructureDimensions(
    source="runtime_configuration",
    account_id="123456789012",
    region="us-east-1",
    runtime_id="unstuck-runtime-EXAMPLE",
)


def correlation(**changes: object) -> Correlation:
    base: dict[str, object] = {
        "run_id": uuid4(),
        "event_id": uuid4(),
        "sequence": 1,
        "timestamp": datetime.now(UTC),
    }
    return Correlation.model_validate(base | changes)


def record(**changes: object) -> TelemetryRecord:
    base: dict[str, object] = {
        "kind": "decider_first_returned",
        "correlation": correlation(),
    }
    return TelemetryRecord.model_validate(base | changes)


def test_contract_every_quantity_contributes_its_availability_to_the_span():
    """A fresh record still says, per quantity, why it has no number."""
    span = span_for(record())
    for field, name in MEASUREMENT_FIELDS.items():
        declared = getattr(Measurements(), field).availability
        assert span.attributes[name + AVAILABILITY_SUFFIX] == declared
        assert name not in span.attributes
    # Every quantity the schema declares has a mapped attribute pair.
    assert set(MEASUREMENT_FIELDS) == set(
        name
        for name in Measurements.model_fields
        if name
        not in {
            "schema_version",
            "pricing",
        }
    )


@pytest.mark.parametrize("availability", get_args(Availability))
def test_security_each_availability_state_is_distinguishable_on_the_span(
    availability,
):
    """Regression: four non-measured states used to arrive as one absence."""
    measured = availability == "measured"
    quantity = TelemetryMeasurement(
        availability=availability, value=12.5 if measured else None
    )
    span = span_for(
        record(measurements=Measurements(total_tokens=quantity, cost=quantity))
    )
    for name in ("gen_ai.usage.total_tokens", "unstuck.cost.amount"):
        assert span.attributes[name + AVAILABILITY_SUFFIX] == availability
        assert (name in span.attributes) is measured
        if measured:
            assert span.attributes[name] == 12.5


def test_cost_reaches_the_span_with_its_currency_unit_and_pricing_provenance():
    """Regression: cost had no attribute mapping at all."""
    span = span_for(
        record(
            measurements=Measurements(
                cost=TelemetryMeasurement(availability="measured", value=0.0042),
                pricing=PricingProvenance(
                    availability="measured",
                    pricing_date="2026-10-20",
                    source="offline-price-list",
                    currency="USD",
                    unit="total_call",
                ),
            )
        )
    )
    assert span.attributes["unstuck.cost.amount"] == 0.0042
    assert span.attributes["unstuck.cost.amount" + AVAILABILITY_SUFFIX] == "measured"
    assert span.attributes["unstuck.cost.currency"] == "USD"
    assert span.attributes["unstuck.cost.unit"] == "total_call"
    assert span.attributes["unstuck.cost.pricing.date"] == "2026-10-20"
    assert span.attributes["unstuck.cost.pricing.source"] == "offline-price-list"
    assert span.attributes["unstuck.cost.pricing.availability"] == "measured"


def test_an_unpriced_cost_claims_no_currency_or_source_on_the_span():
    span = span_for(record())
    assert span.attributes["unstuck.cost.pricing.availability"] == "not_supplied"
    for name in (
        "unstuck.cost.currency",
        "unstuck.cost.unit",
        "unstuck.cost.pricing.date",
        "unstuck.cost.pricing.source",
    ):
        assert name not in span.attributes


def test_the_receipt_and_tool_timings_have_their_own_attributes():
    span = span_for(
        record(
            measurements=Measurements(
                receipt_lookup_ms=TelemetryMeasurement(
                    availability="measured", value=3.0
                ),
                receipt_storage_ms=TelemetryMeasurement(availability="provider_error"),
                tool_latency_ms=TelemetryMeasurement(availability="not_called"),
                error_count=TelemetryMeasurement(availability="measured", value=1.0),
            )
        )
    )
    assert span.attributes["unstuck.receipt.lookup.duration_ms"] == 3.0
    assert (
        span.attributes["unstuck.receipt.storage.duration_ms" + AVAILABILITY_SUFFIX]
        == "provider_error"
    )
    assert "unstuck.receipt.storage.duration_ms" not in span.attributes
    assert span.attributes["unstuck.tool.duration_ms" + AVAILABILITY_SUFFIX] == (
        "not_called"
    )
    assert span.attributes["unstuck.error.count"] == 1.0


def test_security_a_public_record_exports_its_hashed_correlation_as_attributes():
    """Regression: a sanitized span used to carry run identity and nothing else."""
    private = record(
        correlation=correlation(
            experiment_id="exp-1",
            case_id="case-01",
            request_id=uuid4(),
            runtime_session_id="runtime-session-EXAMPLE",
            trace_id="0af7651916cd43dd8448eb211c80319c",
            span_id="b7ad6b7169203331",
            parent_span_id="00f067aa0ba902b7",
            aws_request_id="d2f0c0a4-EXAMPLE",
        ),
        infrastructure=HOSTED,
    )
    raw = span_for(private)
    assert raw.trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert raw.attributes["unstuck.case.id"] == "case-01"
    assert raw.withheld

    span = span_for(sanitize(private))
    # The hashes travel as named attributes, never as the span's own context.
    assert (span.trace_id, span.span_id, span.parent_span_id) == (None, None, None)
    for name in (
        "unstuck.experiment.id.hash",
        "unstuck.case.id.hash",
        "unstuck.request.id.hash",
        "unstuck.session.id.hash",
        "unstuck.trace.id.hash",
        "unstuck.span.id.hash",
        "unstuck.parent_span.id.hash",
        "unstuck.aws.request.id.hash",
        "unstuck.hosting.identity.hash",
    ):
        assert name.endswith(".hash")
        assert len(str(span.attributes[name])) == 64
    assert span.withheld == ()
    rendered = span.model_dump_json()
    for forbidden in (
        "case-01",
        "exp-1",
        "123456789012",
        "us-east-1",
        "runtime-session-EXAMPLE",
        "0af7651916cd43dd8448eb211c80319c",
    ):
        assert forbidden not in rendered


def test_contract_the_raw_case_and_request_attributes_are_withheld_from_the_public():
    for name in (
        "unstuck.experiment.id",
        "unstuck.case.id",
        "unstuck.request.id",
    ):
        assert name in PUBLIC_WITHHELD
