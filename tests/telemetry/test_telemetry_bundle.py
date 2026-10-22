"""The evidence bundle: joins, manifest, redaction, coverage. Written first.

The bundle is the artifact this project wants to keep: one run's application
telemetry joined to the OpenTelemetry spans, the CloudTrail records, the
CloudWatch metrics and the model-invocation records that some other process
exported, with every join basis named and every gap reported.

The defects pinned here are the ones that make an evidence bundle worse than no
bundle at all. A silently dropped unmatched record turns an incomplete join
into a clean-looking one. A manifest that does not bind the content it
describes lets a part be swapped after the fact. A coverage matrix that reports
an absent value as zero turns "we never measured this" into "this was zero". A
public projection that keeps an account, a principal, a runtime, an endpoint, a
log group, a request identity or a session identity publishes the owner's
topology. And a bundle built from an unreconciled timeline presents a partial
causal story as the whole one.

Nothing here publishes, uploads, or submits anything.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_bundle.py -q
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from tests.telemetry.placeholders import (
    SYNTHETIC_ACCOUNT,
    SYNTHETIC_LOG_GROUP,
    SYNTHETIC_RUNTIME_ARN,
)
from tests.telemetry.test_telemetry_runtime_config import full
from tests.telemetry.test_telemetry_signals import (
    REQUEST,
    SESSION,
    SPAN,
    TRACE,
    WINDOW_END,
    WINDOW_START,
    invocation,
    metric,
    signals,
    source,
    span,
)
from unstuck.cloud.telemetry import TelemetryRuntimeConfig
from unstuck.domain import Reason, RuntimeFault
from unstuck.telemetry.bundle import (
    JOIN_BASES,
    SIGNAL_KINDS,
    EvidenceBundle,
    build_bundle,
    bundle_findings,
    hackathon_export,
    public_bundle,
)
from unstuck.telemetry.contracts import (
    Correlation,
    TelemetryCompleteness,
    TelemetryRecord,
    TelemetryTimeline,
    public_digest,
)
from unstuck.telemetry.runtime import ConfiguredTelemetry
from unstuck.telemetry.signals import ExternalSignals

RUN = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ONE = UUID("22222222-2222-4222-8222-222222222222")
EVENT_TWO = UUID("33333333-3333-4333-8333-333333333333")
IDEMPOTENCY = "e" * 64
AT = datetime(2026, 10, 20, 12, 10, tzinfo=UTC)


def record(
    *, sequence: int, event_id: UUID, kind: str, correlated: bool = True
) -> TelemetryRecord:
    return TelemetryRecord(
        kind=kind,
        correlation=Correlation(
            run_id=RUN,
            event_id=event_id,
            sequence=sequence,
            timestamp=AT,
            idempotency_identity_hash=IDEMPOTENCY,
            runtime_session_id=SESSION if correlated else None,
            trace_id=TRACE if correlated else None,
            span_id=SPAN if correlated else None,
            aws_request_id=REQUEST if correlated else None,
        ),
    )


def timeline(*, correlated: bool = True, reconciled: bool = True) -> TelemetryTimeline:
    records = (
        record(
            sequence=1, event_id=EVENT_ONE, kind="run_started", correlated=correlated
        ),
        record(
            sequence=2, event_id=EVENT_TWO, kind="run_completed", correlated=correlated
        ),
    )
    return TelemetryTimeline(
        run_id=RUN,
        records=records,
        completeness=(
            TelemetryCompleteness(
                run_id=RUN,
                first_sequence=1,
                final_sequence=2,
                expected_event_count=2,
                emitted=2,
                refused=0,
                dropped=0,
                finalized=True,
            )
            if reconciled
            else None
        ),
    )


def bundle(
    *,
    configuration: ConfiguredTelemetry | None = None,
    timeline_: TelemetryTimeline | None = None,
    signals_: ExternalSignals | None = None,
) -> EvidenceBundle:
    return build_bundle(
        configuration=configuration or full().configured(),
        timeline=timeline_ or timeline(),
        signals=signals_ or signals(),
    )


def test_a_bundle_binds_its_own_parts_by_content() -> None:
    built = bundle()
    assert bundle_findings(built) == ()
    assert built.verified is True
    assert len(built.bundle_hash) == 64
    assert {output.name for output in built.manifest.outputs} == {
        "configuration",
        "timeline",
        "signals",
        "joins",
        "gaps",
        "coverage",
    }


def test_a_swapped_part_breaks_the_manifest_binding() -> None:
    built = bundle()
    with pytest.raises(ValidationError):
        type(built).model_validate(
            built.model_dump() | {"timeline": timeline(correlated=False).model_dump()}
        )


def test_the_manifest_records_the_capture_window_and_config_hash() -> None:
    manifest = bundle().manifest
    assert manifest.capture_window_start == WINDOW_START
    assert manifest.capture_window_end == WINDOW_END
    assert manifest.config_hash == full().config_hash
    assert len(manifest.redaction_policy_hash) == 64


def test_the_manifest_counts_every_record_kind_including_the_empty_ones() -> None:
    counts = {item.kind: item.count for item in bundle().manifest.record_counts}
    assert counts["application_timeline"] == 2
    for kind in SIGNAL_KINDS:
        assert kind in counts


def test_every_join_basis_is_named_explicitly() -> None:
    built = bundle()
    assert {item.basis for item in built.manifest.join_counts} == set(JOIN_BASES)
    joined = {edge.basis for edge in built.joins}
    assert {
        "aws_request_id",
        "trace_id",
        "span_id",
        "runtime_session_id",
        "application_correlation_hash",
    } >= joined
    assert "aws_request_id" in joined


def test_a_join_links_the_application_record_to_every_matching_signal() -> None:
    edge = next(item for item in bundle().joins if item.basis == "aws_request_id")
    assert edge.key_hash == public_digest(REQUEST)
    assert edge.application_sequences == (1, 2)
    assert edge.span_indices == (0,)
    assert edge.trail_indices == (0,)
    assert edge.invocation_indices == (0,)
    assert edge.degree == 5


def test_a_session_join_reaches_the_metric_records() -> None:
    edge = next(item for item in bundle().joins if item.basis == "runtime_session_id")
    assert edge.metric_indices == (0,)


def test_an_unmatched_record_is_preserved_and_reported_never_dropped() -> None:
    orphan = span(
        trace_id="f" * 32,
        span_id="f" * 16,
        aws_request_id=None,
        runtime_session_id=None,
        parent_span_id=None,
    )
    built = bundle(signals_=signals(spans=(span(), orphan)))
    assert len(built.signals.spans) == 2
    gap = next(item for item in built.gaps if item.kind == "otel_spans")
    assert gap.total == 2
    assert gap.joined == 1
    assert gap.unmatched == 1
    assert gap.unmatched_indices == (1,)


def test_a_timeline_with_no_correlation_joins_nothing_and_says_so() -> None:
    built = bundle(timeline_=timeline(correlated=False))
    for gap in built.gaps:
        if gap.total:
            assert gap.joined == 0
            assert gap.unmatched == gap.total
    assert built.verified is True


def test_an_unreconciled_timeline_is_refused_rather_than_bundled() -> None:
    with pytest.raises(RuntimeFault) as raised:
        bundle(timeline_=timeline(reconciled=False))
    assert raised.value.reason is Reason.STORAGE_ERROR


def test_a_window_that_excludes_the_timeline_is_refused() -> None:
    narrow = signals(
        capture_window_start=datetime(2026, 10, 20, 12, 20, tzinfo=UTC),
        capture_window_end=WINDOW_END,
        spans=(),
        trail=(),
        metrics=(),
        invocations=(),
    )
    with pytest.raises(RuntimeFault) as raised:
        bundle(signals_=narrow)
    assert raised.value.reason is Reason.INVARIANT


def test_the_coverage_matrix_separates_absent_from_zero() -> None:
    built = bundle(
        configuration=TelemetryRuntimeConfig().configured(),
        signals_=signals(spans=(), trail=(), metrics=(), invocations=()),
    )
    statuses = {entry.field: entry.status for entry in built.coverage.entries}
    assert statuses["account_id"] == "not_configured"
    assert statuses["log_group"] == "not_configured"
    assert statuses["otel_spans"] == "pending_live_gate"
    assert statuses["cloudwatch_metrics"] == "pending_live_gate"
    assert "captured" not in {statuses[name] for name in ("account_id", "otel_spans")}
    assert all(entry.detail for entry in built.coverage.entries)


def test_the_coverage_matrix_reports_a_supplied_empty_export_as_unavailable() -> None:
    """`unavailable` is earned by a declared source, not by configuration.

    A configured deployment that exported nothing still has to say which
    export returned nothing, because a kind with no source at all is a query
    nobody ran rather than a query that came back empty.
    """
    built = bundle(
        signals_=signals(
            spans=(),
            trail=(),
            metrics=(),
            invocations=(),
            sources=(source("otel_spans", count=0),),
        )
    )
    statuses = {entry.field: entry.status for entry in built.coverage.entries}
    assert statuses["account_id"] == "captured"
    assert statuses["otel_spans"] == "unavailable"
    assert statuses["cloudtrail"] == "not_configured"


def test_the_coverage_matrix_marks_the_public_withholding_as_redacted() -> None:
    public = public_bundle(bundle())
    statuses = {entry.field: entry.status for entry in public.coverage.entries}
    assert statuses["account_id"] == "redacted"
    assert statuses["log_group"] == "redacted"
    assert statuses["OTEL_EXPORTER_OTLP_LOGS_HEADERS"] == "redacted"
    assert statuses["otel_spans"] == "captured"


def test_every_coverage_status_is_one_of_the_declared_five() -> None:
    declared = {
        "captured",
        "unavailable",
        "redacted",
        "not_configured",
        "pending_live_gate",
    }
    for built in (bundle(), public_bundle(bundle())):
        assert {entry.status for entry in built.coverage.entries} <= declared


def test_the_public_bundle_removes_every_environment_identity() -> None:
    public = public_bundle(bundle())
    body = public.model_dump_json()
    for raw in (
        SYNTHETIC_ACCOUNT,
        SYNTHETIC_LOG_GROUP,
        SYNTHETIC_RUNTIME_ARN,
        TRACE,
        SPAN,
        SESSION,
        REQUEST,
    ):
        assert raw not in body
    assert public.visibility == "public"
    assert public.configuration.dimensions.identity_bearing is False
    assert public.signals.identity_bearing is False
    assert bundle_findings(public) == ()


def test_the_public_bundle_preserves_the_causal_joins() -> None:
    private = bundle()
    public = public_bundle(private)
    assert [edge.basis for edge in public.joins] == [
        edge.basis for edge in private.joins
    ]
    assert [edge.key_hash for edge in public.joins] == [
        edge.key_hash for edge in private.joins
    ]
    assert [edge.degree for edge in public.joins] == [
        edge.degree for edge in private.joins
    ]
    assert public.gaps == private.gaps


def test_the_public_bundle_preserves_the_decision_and_outcome_evidence() -> None:
    public = public_bundle(bundle())
    assert public.timeline.kinds == ("run_started", "run_completed")
    assert public.signals.invocations[0].latency_ms.value == pytest.approx(250.0)
    assert public.signals.invocations[0].input_tokens.value == pytest.approx(120.0)
    assert public.signals.trail[0].event_name == "InvokeAgentRuntime"
    assert public.signals.metrics[0].metric_name == "SessionCount"


def test_the_public_bundle_is_independently_verifiable_by_content() -> None:
    public = public_bundle(bundle())
    rebuilt = type(public).model_validate(public.model_dump())
    assert rebuilt.bundle_hash == public.bundle_hash
    assert rebuilt.verified is True


def test_a_public_projection_is_idempotent() -> None:
    once = public_bundle(bundle())
    assert public_bundle(once).bundle_hash == once.bundle_hash


def test_the_export_gate_refuses_an_unverified_bundle() -> None:
    built = bundle()
    broken = type(built).model_construct(
        **(built.model_dump() | {"joins": ()}),
    )
    with pytest.raises(RuntimeFault):
        hackathon_export(broken)


def test_the_export_gate_returns_the_public_projection() -> None:
    exported = hackathon_export(bundle())
    assert exported.visibility == "public"
    assert exported.verified is True
    assert exported.bundle_hash == public_bundle(bundle()).bundle_hash


def test_an_offline_bundle_is_labelled_synthetic_and_never_live() -> None:
    built = bundle()
    assert all(item.classification == "synthetic" for item in built.timeline.records)
    assert all(item.mode == "offline" for item in built.timeline.records)
    assert built.coverage.entry("pricing").status == "not_configured"


def test_a_priced_bundle_states_its_pricing_provenance() -> None:
    priced = TelemetryRuntimeConfig.model_validate(
        full().model_dump()
        | {
            "pricing_source": "published-price-list",
            "pricing_date": "2026-10-20",
            "currency": "USD",
        }
    )
    built = bundle(configuration=priced.configured())
    assert built.manifest.pricing.availability == "measured"
    assert built.manifest.pricing.source == "published-price-list"
    assert built.coverage.entry("pricing").status == "captured"


def test_the_bundle_never_claims_a_metric_it_did_not_observe() -> None:
    built = bundle(
        signals_=signals(metrics=(metric(value={"availability": "invalid"}),))
    )
    assert built.signals.metrics[0].value.value is None
    assert built.coverage.entry("cloudwatch_metrics").status == "captured"


def test_an_invocation_without_tokens_does_not_become_zero() -> None:
    built = bundle(
        signals_=signals(
            invocations=(
                invocation(
                    input_tokens={"availability": "not_provided"},
                    output_tokens={"availability": "not_provided"},
                ),
            )
        )
    )
    observed = built.signals.invocations[0]
    assert observed.input_tokens.availability == "not_provided"
    assert observed.total_tokens.availability == "not_supplied"


def test_a_trail_only_bundle_still_builds_and_reports_its_gaps() -> None:
    built = bundle(signals_=signals(spans=(), metrics=(), invocations=()))
    assert built.verified is True
    assert built.coverage.entry("otel_spans").status == "not_configured"
    assert built.coverage.entry("cloudtrail").status == "captured"
    assert built.signals.provenance_complete is False
