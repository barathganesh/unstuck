"""Every refusal the telemetry evidence contracts make. Written first. Offline.

The happy paths are covered elsewhere. This file is the other half: each
invariant these contracts declare is exercised by constructing the thing it
forbids and checking that the construction fails. An invariant nobody has seen
refuse is an invariant nobody knows is wired up.

The refusals group into five kinds. Identity carried twice, raw and hashed,
which would let a reader choose which one to believe. A set that does not
account for everything it claims to, which is how a partial export looks whole.
An ordering that is not canonical, which would make the same content hash two
ways. A window that does not cover its own evidence. And a public artifact that
still carries something private.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_evidence_invariants.py -q
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tests.telemetry.placeholders import SYNTHETIC_ACCOUNT
from tests.telemetry.test_telemetry_bundle import bundle, timeline
from tests.telemetry.test_telemetry_runtime_config import full
from tests.telemetry.test_telemetry_signals import (
    DIGEST,
    REQUEST,
    SESSION,
    SPAN,
    TRACE,
    WINDOW_END,
    WINDOW_START,
    invocation,
    metric,
    signals,
    span,
    trail,
)
from unstuck.cloud.telemetry import TelemetryRuntimeConfig
from unstuck.domain import Reason, RuntimeFault
from unstuck.telemetry.bundle import (
    JOIN_BASES,
    CoverageMatrix,
    EvidenceBundle,
    FieldCoverage,
    JoinCount,
    JoinEdge,
    SignalGap,
    bundle_findings,
    coverage_matrix,
    evidence_manifest,
    hackathon_export,
    public_bundle,
)
from unstuck.telemetry.contracts import InfrastructureDimensions
from unstuck.telemetry.runtime import (
    OTEL_VARIABLES,
    ConfiguredTelemetry,
    ConfigVariable,
    OtelConfigMap,
)
from unstuck.telemetry.signals import ExternalSignals, SignalSource

OTHER_DIGEST = "9" * 64


def one_variable(name: str, value: str) -> OtelConfigMap:
    return OtelConfigMap(
        variables=(ConfigVariable(name=name, value=value),),
        absent=tuple(sorted(item for item in OTEL_VARIABLES if item != name)),
    )


# --- The documented variable projection -------------------------------------


def test_a_credential_variable_is_refused_by_the_variable_itself() -> None:
    with pytest.raises(ValidationError):
        ConfigVariable(name="AWS_ACCESS_KEY_ID", value="offline-placeholder")
    with pytest.raises(ValidationError):
        ConfigVariable(name="AWS_SESSION_TOKEN", value="offline-placeholder")


def test_a_value_matching_a_credential_shape_is_refused() -> None:
    with pytest.raises(ValidationError):
        ConfigVariable(
            name="OTEL_RESOURCE_ATTRIBUTES",
            value="service.name=AKIAABCDEFGHIJKLMNOP",
        )


def test_an_unordered_projection_is_refused() -> None:
    with pytest.raises(ValidationError):
        OtelConfigMap(
            variables=(
                ConfigVariable(name="OTEL_PYTHON_DISTRO", value="aws_distro"),
                ConfigVariable(name="AGENT_OBSERVABILITY_ENABLED", value="true"),
            ),
            absent=tuple(
                sorted(
                    name
                    for name in OTEL_VARIABLES
                    if name not in {"OTEL_PYTHON_DISTRO", "AGENT_OBSERVABILITY_ENABLED"}
                )
            ),
        )


def test_an_unordered_absent_or_redacted_set_is_refused() -> None:
    with pytest.raises(ValidationError):
        OtelConfigMap(absent=tuple(reversed(OTEL_VARIABLES)))
    kept = one_variable("AGENT_OBSERVABILITY_ENABLED", "true")
    with pytest.raises(ValidationError):
        OtelConfigMap(
            variables=kept.variables,
            absent=("OTEL_PYTHON_DISTRO",),
            redacted=(
                "OTEL_RESOURCE_ATTRIBUTES",
                "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
                "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
                "OTEL_EXPORTER_OTLP_PROTOCOL",
                "OTEL_PYTHON_CONFIGURATOR",
                "UNIFIED_TRACES_DESTINATION_ENABLED",
            ),
        )


def test_only_a_withheld_variable_may_be_redacted() -> None:
    with pytest.raises(ValidationError):
        OtelConfigMap(
            redacted=("OTEL_PYTHON_DISTRO",),
            absent=tuple(
                sorted(name for name in OTEL_VARIABLES if name != "OTEL_PYTHON_DISTRO")
            ),
        )


def test_the_projection_hash_is_stable_and_content_addressed() -> None:
    first = full().configured().variables
    assert first.config_map_hash == full().configured().variables.config_map_hash
    assert first.config_map_hash != first.public().config_map_hash


# --- The configured telemetry projection ------------------------------------


def configured(**changes: object) -> ConfiguredTelemetry:
    return ConfiguredTelemetry.model_validate(
        full().configured().model_dump() | changes
    )


def test_a_public_configuration_carrying_raw_hosting_identity_is_refused() -> None:
    with pytest.raises(ValidationError):
        configured(
            visibility="public",
            dimensions=InfrastructureDimensions(
                source="runtime_configuration", account_id=SYNTHETIC_ACCOUNT
            ),
        )


def test_a_public_configuration_naming_its_service_is_refused() -> None:
    public = full().configured().public()
    with pytest.raises(ValidationError):
        ConfiguredTelemetry.model_validate(
            public.model_dump() | {"service_name": "unstuck-synthetic-controller"}
        )


def test_a_public_configuration_projecting_a_header_is_refused() -> None:
    public = full().configured().public()
    with pytest.raises(ValidationError):
        ConfiguredTelemetry.model_validate(
            public.model_dump()
            | {
                "variables": one_variable(
                    "OTEL_EXPORTER_OTLP_LOGS_HEADERS", "x-aws-log-group=anything"
                )
            }
        )


def test_a_pricing_source_without_a_date_is_refused() -> None:
    with pytest.raises(ValidationError):
        TelemetryRuntimeConfig(pricing_source="published-price-list")


def test_the_withheld_variable_names_are_readable_from_the_configuration() -> None:
    assert full().public_withheld == (
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_RESOURCE_ATTRIBUTES",
    )


# --- Imported signal records ------------------------------------------------


def test_a_failed_span_without_an_error_class_is_refused() -> None:
    with pytest.raises(ValidationError):
        span(status_code="error")


@pytest.mark.parametrize(
    ("factory", "changes"),
    (
        (span, {"trace_id_hash": DIGEST}),
        (trail, {"aws_request_id_hash": DIGEST}),
        (metric, {"runtime_session_id_hash": DIGEST}),
        (invocation, {"trace_id_hash": DIGEST}),
    ),
    ids=("span", "trail", "metric", "invocation"),
)
def test_a_record_carrying_both_a_raw_identity_and_its_hash_is_refused(
    factory: object, changes: dict[str, str]
) -> None:
    with pytest.raises(ValidationError):
        factory(**changes)  # type: ignore[operator]


def test_a_metric_whose_query_window_is_inverted_is_refused() -> None:
    with pytest.raises(ValidationError):
        metric(
            query_window_start=WINDOW_END,
            query_window_end=WINDOW_START,
            timestamp=WINDOW_START,
        )


def test_a_record_with_no_raw_identity_projects_to_itself() -> None:
    hashed = signals().public()
    for record in (
        hashed.spans[0],
        hashed.trail[0],
        hashed.metrics[0],
        hashed.invocations[0],
    ):
        assert record.public() is record
        assert record.identity_bearing is False


def test_a_metric_outside_the_capture_window_is_refused() -> None:
    with pytest.raises(ValidationError):
        signals(
            metrics=(
                metric(
                    timestamp=datetime(2026, 10, 20, 13, 0, tzinfo=UTC),
                    query_window_start=datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
                    query_window_end=datetime(2026, 10, 20, 14, 0, tzinfo=UTC),
                ),
            )
        )


def test_a_source_declared_twice_is_refused() -> None:
    source = {
        "label": "spans.json",
        "kind": "otel_spans",
        "content_hash": DIGEST,
        "record_count": 1,
    }
    with pytest.raises(ValidationError):
        signals(sources=(source, source))


def test_public_signals_carrying_a_raw_identity_are_refused() -> None:
    with pytest.raises(ValidationError):
        signals(visibility="public")


def test_public_signals_retaining_a_private_value_are_refused() -> None:
    with pytest.raises(ValidationError, match="retain private identity"):
        ExternalSignals(
            visibility="public",
            capture_window_start=WINDOW_START,
            capture_window_end=WINDOW_END,
            metrics=(
                metric(
                    namespace="/home/operator/metrics",
                    runtime_session_id=None,
                    runtime_session_id_hash=DIGEST,
                ),
            ),
            sources=(
                SignalSource(
                    label_hash=DIGEST,
                    kind="cloudwatch_metrics",
                    content_hash=DIGEST,
                    record_count=1,
                ),
            ),
        )


def test_public_signals_carrying_a_raw_source_label_are_refused() -> None:
    """A filename is identity, so a public source carries only its hash."""
    with pytest.raises(ValidationError, match="hashed label only"):
        ExternalSignals(
            visibility="public",
            capture_window_start=WINDOW_START,
            capture_window_end=WINDOW_END,
            metrics=(metric(runtime_session_id=None, runtime_session_id_hash=DIGEST),),
            sources=(
                SignalSource(
                    label="synthetic-metrics.json",
                    kind="cloudwatch_metrics",
                    content_hash=DIGEST,
                    record_count=1,
                ),
            ),
        )


def test_an_unordered_source_set_is_refused() -> None:
    """The same set of exports must hash one way, so order is part of it."""
    ordered = signals().sources
    with pytest.raises(ValidationError, match="ordered by kind and label"):
        signals(sources=tuple(reversed(ordered)))


def test_an_already_public_source_projects_to_itself() -> None:
    hashed = SignalSource(
        label_hash=DIGEST, kind="otel_spans", content_hash=DIGEST, record_count=1
    )
    assert hashed.public() is hashed
    assert hashed.identity_bearing is False


def test_the_signals_hash_is_stable_and_content_addressed() -> None:
    assert signals().signals_hash == signals().signals_hash
    assert signals().signals_hash != signals().public().signals_hash


# --- Joins, gaps, coverage and the manifest ---------------------------------


def test_a_join_edge_reaching_nothing_is_refused() -> None:
    with pytest.raises(ValidationError):
        JoinEdge(basis="trace_id", key_hash=DIGEST)


def test_a_join_edge_with_unordered_members_is_refused() -> None:
    with pytest.raises(ValidationError):
        JoinEdge(basis="trace_id", key_hash=DIGEST, span_indices=(1, 0))
    with pytest.raises(ValidationError):
        JoinEdge(basis="trace_id", key_hash=DIGEST, span_indices=(0, 0))


def test_a_gap_report_that_does_not_account_for_every_record_is_refused() -> None:
    with pytest.raises(ValidationError):
        SignalGap(kind="otel_spans", total=2, joined=1, unmatched=0)
    with pytest.raises(ValidationError):
        SignalGap(kind="otel_spans", total=2, joined=1, unmatched=1)
    with pytest.raises(ValidationError):
        SignalGap(
            kind="otel_spans",
            total=3,
            joined=1,
            unmatched=2,
            unmatched_indices=(1, 1),
        )


def test_a_coverage_matrix_with_a_repeated_or_unordered_field_is_refused() -> None:
    first = FieldCoverage(
        field="account_id",
        group="runtime_configuration",
        status="captured",
        detail="x",
    )
    second = FieldCoverage(
        field="region", group="runtime_configuration", status="captured", detail="x"
    )
    with pytest.raises(ValidationError):
        CoverageMatrix(entries=(first, first))
    with pytest.raises(ValidationError):
        CoverageMatrix(entries=(second, first))


def test_asking_the_coverage_matrix_for_an_unknown_field_is_a_fault() -> None:
    matrix = coverage_matrix(
        configuration=full().configured(), timeline=timeline(), signals=signals()
    )
    with pytest.raises(RuntimeFault) as raised:
        matrix.entry("no_such_field")
    assert raised.value.reason is Reason.INVARIANT


def test_a_basis_cannot_cross_more_sources_than_it_has_edges() -> None:
    with pytest.raises(ValidationError):
        JoinCount(basis="trace_id", edges=1, cross_source_edges=2)


def test_a_manifest_missing_an_output_basis_or_kind_is_refused() -> None:
    manifest = bundle().manifest
    for change in (
        {"outputs": manifest.outputs[:-1]},
        {"join_counts": manifest.join_counts[:-1]},
        {"record_counts": manifest.record_counts[:-1]},
    ):
        with pytest.raises(ValidationError):
            type(manifest).model_validate(manifest.model_dump() | change)
    assert {item.basis for item in manifest.join_counts} == set(JOIN_BASES)


def test_asking_the_manifest_for_an_unknown_output_is_a_fault() -> None:
    with pytest.raises(RuntimeFault) as raised:
        bundle().manifest.output("no_such_part")
    assert raised.value.reason is Reason.INVARIANT


def test_a_bundle_whose_manifest_binds_another_configuration_is_refused() -> None:
    built = bundle()
    other = TelemetryRuntimeConfig().configured()
    with pytest.raises(ValidationError):
        type(built).model_validate(
            built.model_dump() | {"configuration": other.model_dump()}
        )


def test_a_bundle_whose_parts_disagree_on_visibility_is_refused() -> None:
    built = bundle()
    with pytest.raises(ValidationError):
        type(built).model_validate(
            built.model_dump() | {"signals": signals().public().model_dump()}
        )


def test_a_public_bundle_carrying_a_raw_signal_identity_is_refused() -> None:
    exported = hackathon_export(bundle())
    with pytest.raises(ValidationError):
        type(exported).model_validate(
            exported.model_dump() | {"signals": signals().model_dump()}
        )


def test_a_public_bundle_carrying_raw_hosting_identity_is_refused() -> None:
    exported = hackathon_export(bundle())
    with pytest.raises(ValidationError):
        type(exported).model_validate(
            exported.model_dump()
            | {
                "configuration": full().configured().model_dump()
                | {"visibility": "public"}
            }
        )


def unreconciled() -> EvidenceBundle:
    """A bundle whose manifest binds it correctly over a partial record.

    `build_bundle` refuses this, which is the point: it is assembled here with
    the same manifest builder so the completeness check can be seen refusing
    something that is otherwise entirely well formed.
    """
    partial = timeline(reconciled=False)
    external = signals()
    configuration = full().configured()
    coverage = coverage_matrix(
        configuration=configuration, timeline=partial, signals=external
    )
    joins = bundle().joins
    gaps = bundle().gaps
    return EvidenceBundle(
        manifest=evidence_manifest(
            configuration=configuration,
            timeline=partial,
            signals=external,
            joins=joins,
            gaps=gaps,
            coverage=coverage,
            visibility="private",
        ),
        configuration=configuration,
        timeline=partial,
        signals=external,
        joins=joins,
        gaps=gaps,
        coverage=coverage,
    )


def test_a_well_formed_bundle_over_a_partial_record_is_not_verified() -> None:
    built = unreconciled()
    assert built.verified is False
    assert bundle_findings(built) == ("application timeline is unreconciled",)


def test_the_export_gate_refuses_a_bundle_over_a_partial_record() -> None:
    with pytest.raises(RuntimeFault) as raised:
        hackathon_export(unreconciled())
    assert raised.value.reason is Reason.STORAGE_ERROR


def test_a_bundle_that_does_not_validate_reports_one_finding() -> None:
    built = bundle()
    broken = type(built).model_construct(**(built.model_dump() | {"joins": ()}))
    findings = bundle_findings(broken)
    assert len(findings) == 1
    assert "does not validate" in findings[0]


def test_a_public_bundle_reports_no_finding() -> None:
    assert bundle_findings(public_bundle(bundle())) == ()


def test_the_raw_identities_are_absent_from_every_public_part() -> None:
    body = hackathon_export(bundle()).model_dump_json()
    for raw in (TRACE, SPAN, SESSION, REQUEST, SYNTHETIC_ACCOUNT):
        assert raw not in body


def test_a_public_bundle_retaining_a_private_value_is_refused() -> None:
    exported = hackathon_export(bundle())
    leaked = CoverageMatrix(
        entries=tuple(
            entry
            if entry.field != "pricing"
            else FieldCoverage(
                field="pricing",
                group="pricing",
                status="not_configured",
                detail="Priced from /home/operator/prices.json, which is a path.",
            )
            for entry in exported.coverage.entries
        )
    )
    with pytest.raises(ValidationError, match="retains private identity"):
        EvidenceBundle(
            manifest=evidence_manifest(
                configuration=exported.configuration,
                timeline=exported.timeline,
                signals=exported.signals,
                joins=exported.joins,
                gaps=exported.gaps,
                coverage=leaked,
                visibility="public",
            ),
            configuration=exported.configuration,
            timeline=exported.timeline,
            signals=exported.signals,
            joins=exported.joins,
            gaps=exported.gaps,
            coverage=leaked,
        )


def public_unreconciled() -> EvidenceBundle:
    """A public bundle whose application record is partial. Never exportable."""
    exported = hackathon_export(bundle())
    from unstuck.telemetry.sanitizer import sanitize_timeline

    partial = sanitize_timeline(timeline(reconciled=False))
    coverage = coverage_matrix(
        configuration=exported.configuration,
        timeline=partial,
        signals=exported.signals,
    )
    return EvidenceBundle(
        manifest=evidence_manifest(
            configuration=exported.configuration,
            timeline=partial,
            signals=exported.signals,
            joins=exported.joins,
            gaps=exported.gaps,
            coverage=coverage,
            visibility="public",
        ),
        configuration=exported.configuration,
        timeline=partial,
        signals=exported.signals,
        joins=exported.joins,
        gaps=exported.gaps,
        coverage=coverage,
    )


def test_a_public_bundle_over_a_partial_record_is_not_verified() -> None:
    assert public_unreconciled().verified is False
