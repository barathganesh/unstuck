"""What the evidence bundle binds, and what it refuses to take on trust.

Five defects are pinned here, each one a way a bundle could read as more than
it is.

The gap report used to sit outside the hash boundary. `gaps` was not a manifest
output and was never recomputed, while publication summed it into a published
unmatched-record count, so the joined-versus-unmatched claim of a public
artifact could change with no hash moving at all.

The manifest used to describe rather than bind. Its source list, record counts,
join counts, pricing provenance, capture window and redaction policy hash were
accepted as written, so a manifest could report a different window or a
different source set over perfectly valid parts, and a duplicated count entry
could inflate a published tally.

Provenance used to be optional. A nonempty kind validated with no
`SignalSource` at all, so the manifest could not say which export produced its
records, and an empty kind was reported `unavailable` on the strength of the
deployment configuration rather than on any export having been supplied.

Two of the four clocks used to escape the capture window. Span start and end
were never compared against it, and a model invocation carried no observation
time to compare, so a matching correlation identifier could attach evidence
from some other interval.

And public projection used to keep three kinds of deployment identity: the
service stage, the model identity on spans and invocations, and the operator's
own export filenames. Each is now removed or hashed, and each has a unique
sentinel here so a leak names the field it came from.

Nothing here publishes, uploads, or submits anything.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_evidence_binding.py -q
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tests.telemetry.placeholders import (
    MODEL_REVISION_SENTINEL,
    MODEL_SENTINEL,
    SOURCE_LABEL_SENTINEL,
    STAGE_SENTINEL,
)
from tests.telemetry.test_telemetry_bundle import bundle, timeline
from tests.telemetry.test_telemetry_runtime_config import full
from tests.telemetry.test_telemetry_signals import (
    DIGEST,
    OBSERVED_AT,
    OUTSIDE_WINDOW,
    SPAN_END_NANO,
    SPAN_START_NANO,
    WINDOW_END,
    WINDOW_END_NANO,
    WINDOW_START,
    WINDOW_START_NANO,
    invocation,
    metric,
    signals,
    source,
    span,
    trail,
)
from unstuck.cloud.telemetry import TelemetryRuntimeConfig
from unstuck.domain import Reason, RuntimeFault
from unstuck.telemetry.bundle import (
    OUTPUT_NAMES,
    RECORD_KINDS,
    EvidenceBundle,
    SignalGap,
    coverage_matrix,
    evidence_manifest,
    hackathon_export,
    public_bundle,
    redaction_policy_hash,
)
from unstuck.telemetry.signals import (
    ExternalSignals,
    SignalSource,
    from_unix_nano,
    unix_nano,
)

OTHER_DIGEST = "7" * 64


def rebound(built: EvidenceBundle, **parts: object) -> EvidenceBundle:
    """Reassemble one bundle around changed parts, with a matching manifest.

    The manifest is rebuilt rather than copied, so every part hash still
    matches. That isolates the refusal under test: a cruder tamper would be
    caught by the hash comparison before the recomputation ever ran.
    """
    fields: dict[str, object] = {
        "configuration": built.configuration,
        "timeline": built.timeline,
        "signals": built.signals,
        "joins": built.joins,
        "gaps": built.gaps,
        "coverage": built.coverage,
    }
    fields |= parts
    return EvidenceBundle(
        manifest=evidence_manifest(visibility=built.visibility, **fields),  # type: ignore[arg-type]
        **fields,  # type: ignore[arg-type]
    )


def with_manifest(built: EvidenceBundle, **changes: object) -> EvidenceBundle:
    """One bundle whose manifest claims something its parts do not."""
    return type(built).model_validate(
        built.model_dump() | {"manifest": built.manifest.model_dump() | changes}
    )


def replaced_gap(
    built: EvidenceBundle, kind: str, **changes: object
) -> tuple[SignalGap, ...]:
    """The gap report with one kind's entry replaced by a self-consistent lie."""
    return tuple(
        SignalGap.model_validate(gap.model_dump() | changes)
        if gap.kind == kind
        else gap
        for gap in built.gaps
    )


# --- The gap report inside the hash boundary --------------------------------


def test_the_manifest_binds_the_gap_report_as_an_output() -> None:
    """Regression: `gaps` was absent from the manifest's output list."""
    built = bundle()
    assert "gaps" in OUTPUT_NAMES
    assert tuple(item.name for item in built.manifest.outputs) == OUTPUT_NAMES
    assert len(built.manifest.output("gaps")) == 64


def test_a_changed_gap_report_changes_the_bundle_hash() -> None:
    """Regression: the published unmatched count could move with no hash moving."""
    built = bundle()
    orphan = span(
        trace_id="f" * 32,
        span_id="f" * 16,
        aws_request_id=None,
        runtime_session_id=None,
        parent_span_id=None,
    )
    other = bundle(signals_=signals(spans=(span(), orphan)))
    assert built.gaps != other.gaps
    assert built.manifest.output("gaps") != other.manifest.output("gaps")
    assert built.bundle_hash != other.bundle_hash


def test_a_gap_report_that_contradicts_the_records_is_refused() -> None:
    """Regression: `bound()` never recomputed the report it carried."""
    built = bundle()
    tampered = replaced_gap(
        built, "otel_spans", joined=0, unmatched=1, unmatched_indices=(0,)
    )
    with pytest.raises(ValidationError, match="does not match its own records"):
        rebound(built, gaps=tampered)


def test_a_gap_report_that_contradicts_the_timeline_is_refused() -> None:
    built = bundle()
    tampered = replaced_gap(
        built, "application_timeline", joined=0, unmatched=2, unmatched_indices=(1, 2)
    )
    with pytest.raises(ValidationError):
        rebound(built, gaps=tampered)


def test_a_gap_report_naming_a_record_the_bundle_lacks_is_refused() -> None:
    """An index is only not identity because it points at a record present.

    The total is left correct on purpose, so the refusal is the dangling index
    rather than the arithmetic a miscount would already have caught.
    """
    built = bundle()
    tampered = replaced_gap(
        built, "otel_spans", total=1, joined=0, unmatched=1, unmatched_indices=(3,)
    )
    with pytest.raises(ValidationError, match="names a record the bundle lacks"):
        rebound(built, gaps=tampered)


def test_a_join_edge_naming_a_record_the_bundle_lacks_is_refused() -> None:
    built = bundle()
    edges = tuple(
        type(edge).model_validate(edge.model_dump() | {"span_indices": (9,)})
        if edge.span_indices
        else edge
        for edge in built.joins
    )
    with pytest.raises(ValidationError, match="names a record the bundle lacks"):
        rebound(built, joins=edges)


def test_a_join_edge_naming_an_absent_application_record_is_refused() -> None:
    built = bundle()
    edges = tuple(
        type(edge).model_validate(
            edge.model_dump() | {"application_sequences": (1, 2, 9)}
        )
        if edge.application_sequences
        else edge
        for edge in built.joins
    )
    with pytest.raises(ValidationError, match="application record the bundle lacks"):
        rebound(built, joins=edges)


def test_a_gap_report_that_miscounts_an_imported_kind_is_refused() -> None:
    built = bundle()
    tampered = replaced_gap(
        built, "otel_spans", total=2, joined=1, unmatched=1, unmatched_indices=(1,)
    )
    with pytest.raises(ValidationError, match="miscounts an imported kind"):
        rebound(built, gaps=tampered)


def test_a_gap_report_that_miscounts_the_timeline_is_refused() -> None:
    built = bundle()
    tampered = replaced_gap(
        built,
        "application_timeline",
        total=3,
        joined=0,
        unmatched=3,
        unmatched_indices=(1, 2, 3),
    )
    with pytest.raises(ValidationError, match="application record the bundle lacks"):
        rebound(built, gaps=tampered)


def test_a_gap_report_over_a_miscounted_timeline_total_is_refused() -> None:
    built = bundle()
    tampered = replaced_gap(
        built,
        "application_timeline",
        total=1,
        joined=0,
        unmatched=1,
        unmatched_indices=(1,),
    )
    with pytest.raises(ValidationError, match="miscounts the application timeline"):
        rebound(built, gaps=tampered)


# --- The manifest reconciled against its own parts --------------------------


def test_the_manifest_is_exactly_the_one_its_parts_produce() -> None:
    built = bundle()
    assert built.manifest == evidence_manifest(
        configuration=built.configuration,
        timeline=built.timeline,
        signals=built.signals,
        joins=built.joins,
        gaps=built.gaps,
        coverage=built.coverage,
        visibility=built.visibility,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("sources", ()),
        ("pricing", {"availability": "unavailable"}),
        ("redaction_policy_hash", OTHER_DIGEST),
        ("capture_window_start", datetime(2026, 10, 20, 11, 0, tzinfo=UTC)),
        ("capture_window_end", OUTSIDE_WINDOW),
        ("telemetry_version", "telemetry-1"),
    ),
    ids=(
        "sources",
        "pricing",
        "redaction-policy",
        "window-start",
        "window-end",
        "telemetry-version",
    ),
)
def test_a_manifest_summary_that_its_parts_do_not_produce_is_refused(
    field: str, value: object
) -> None:
    """Regression: every one of these was accepted as written.

    The telemetry version case is the control: it is the one value that already
    agrees with its part, so it must still validate, which proves the matrix is
    refusing a difference rather than refusing any rebuild at all.
    """
    built = bundle()
    if field == "telemetry_version":
        assert with_manifest(built, **{field: value}).verified is True
        return
    with pytest.raises(ValidationError):
        with_manifest(built, **{field: value})


def test_a_manifest_record_count_that_contradicts_the_signals_is_refused() -> None:
    built = bundle()
    inflated = tuple(
        item.model_dump() | ({"count": 9} if item.kind == "otel_spans" else {})
        for item in built.manifest.record_counts
    )
    with pytest.raises(ValidationError):
        with_manifest(built, record_counts=inflated)


def test_a_duplicated_count_or_basis_cannot_inflate_a_tally() -> None:
    """Regression: the manifest compared these as sets, so duplicates passed."""
    built = bundle()
    manifest = built.manifest
    for field, value in (
        ("record_counts", manifest.record_counts + (manifest.record_counts[0],)),
        ("join_counts", manifest.join_counts + (manifest.join_counts[0],)),
        ("outputs", manifest.outputs + (manifest.outputs[0],)),
    ):
        with pytest.raises(ValidationError):
            type(manifest).model_validate(manifest.model_dump() | {field: value})


def test_a_manifest_list_out_of_canonical_order_is_refused() -> None:
    """The same content must hash one way, so order is part of the contract."""
    manifest = bundle().manifest
    for field, value in (
        ("record_counts", tuple(reversed(manifest.record_counts))),
        ("join_counts", tuple(reversed(manifest.join_counts))),
        ("outputs", tuple(reversed(manifest.outputs))),
    ):
        with pytest.raises(ValidationError):
            type(manifest).model_validate(manifest.model_dump() | {field: value})
    assert tuple(item.kind for item in manifest.record_counts) == RECORD_KINDS


def test_a_manifest_source_declared_twice_is_refused() -> None:
    manifest = bundle().manifest
    with pytest.raises(ValidationError, match="declares a signal source twice"):
        type(manifest).model_validate(
            manifest.model_dump() | {"sources": manifest.sources[:1] * 2}
        )


def test_a_manifest_source_list_out_of_canonical_order_is_refused() -> None:
    manifest = bundle().manifest
    with pytest.raises(ValidationError, match="ordered by kind and label"):
        type(manifest).model_validate(
            manifest.model_dump() | {"sources": tuple(reversed(manifest.sources))}
        )


def test_a_manifest_with_an_inverted_capture_window_is_refused() -> None:
    manifest = bundle().manifest
    with pytest.raises(ValidationError, match="capture window is ordered"):
        type(manifest).model_validate(
            manifest.model_dump()
            | {
                "capture_window_start": WINDOW_END,
                "capture_window_end": WINDOW_START,
            }
        )


def test_the_manifest_sources_are_the_signal_sources_in_canonical_order() -> None:
    built = bundle()
    assert built.manifest.sources == built.signals.sources
    assert [item.kind for item in built.manifest.sources] == sorted(
        item.kind for item in built.manifest.sources
    )


def test_the_manifest_redaction_policy_hash_is_the_current_policy() -> None:
    assert bundle().manifest.redaction_policy_hash == redaction_policy_hash()


# --- Source provenance ------------------------------------------------------


def test_a_bundle_cannot_be_built_over_records_with_no_source() -> None:
    """The refusal is at the signal contract, so no bundle path reaches it."""
    with pytest.raises(ValidationError):
        ExternalSignals(
            capture_window_start=WINDOW_START,
            capture_window_end=WINDOW_END,
            spans=(span(),),
        )


def test_a_supplied_empty_export_is_unavailable_and_an_absent_one_is_not() -> None:
    """Regression: `unavailable` came from configuration, not from a source."""
    empty = {"spans": (), "trail": (), "metrics": (), "invocations": ()}
    supplied = bundle(
        signals_=signals(
            **empty,
            sources=tuple(source(kind, count=0) for kind in sorted(RECORD_KINDS[:4])),
        )
    )
    absent = bundle(signals_=signals(**empty, sources=()))
    for kind in RECORD_KINDS[:4]:
        assert supplied.coverage.entry(kind).status == "unavailable"
        assert absent.coverage.entry(kind).status == "not_configured"


def test_an_unconfigured_deployment_with_no_export_is_pending_its_live_gate() -> None:
    built = bundle(
        configuration=TelemetryRuntimeConfig().configured(),
        signals_=signals(spans=(), trail=(), metrics=(), invocations=(), sources=()),
    )
    for kind in RECORD_KINDS[:4]:
        assert built.coverage.entry(kind).status == "pending_live_gate"


def test_a_captured_kind_says_it_came_from_a_declared_source() -> None:
    entry = bundle().coverage.entry("otel_spans")
    assert entry.status == "captured"
    assert "declared source" in entry.detail


# --- The capture window over every clock ------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    (
        (WINDOW_START_NANO - 1, SPAN_END_NANO),
        (SPAN_START_NANO, WINDOW_END_NANO + 1),
        (WINDOW_START_NANO - 1, WINDOW_START_NANO),
        (WINDOW_END_NANO, WINDOW_END_NANO + 1),
    ),
    ids=("before", "after", "crossing-the-start", "crossing-the-end"),
)
def test_a_span_outside_the_capture_window_is_refused(start: int, end: int) -> None:
    """Regression: span nanoseconds were never compared against the window."""
    with pytest.raises(ValidationError):
        signals(spans=(span(start_unix_nano=start, end_unix_nano=end),))


def test_a_span_exactly_on_either_bound_is_accepted() -> None:
    """The comparison is inclusive, so a boundary span is evidence not an error."""
    accepted = signals(
        spans=(span(start_unix_nano=WINDOW_START_NANO, end_unix_nano=WINDOW_END_NANO),)
    )
    assert accepted.spans[0].observed_at == WINDOW_START


def test_a_span_states_its_observation_time_in_utc() -> None:
    assert span().observed_at == OBSERVED_AT
    assert from_unix_nano(unix_nano(OBSERVED_AT)) == OBSERVED_AT


def test_an_invocation_with_no_observation_time_is_refused() -> None:
    """Regression: `ModelInvocationRecord` carried no clock at all."""
    body = invocation().model_dump()
    body.pop("observed_at")
    body.pop("schema_version", None)
    with pytest.raises(ValidationError):
        type(invocation()).model_validate(body)


@pytest.mark.parametrize(
    "moment",
    (
        OUTSIDE_WINDOW,
        datetime(2026, 10, 20, 11, 59, 59, tzinfo=UTC),
    ),
    ids=("after", "before"),
)
def test_an_invocation_outside_the_capture_window_is_refused(
    moment: datetime,
) -> None:
    with pytest.raises(ValidationError):
        signals(invocations=(invocation(observed_at=moment),))


def test_an_invocation_observation_time_must_be_utc() -> None:
    with pytest.raises(ValidationError):
        invocation(observed_at=OBSERVED_AT.replace(tzinfo=None))


def test_every_kind_states_the_same_observation_time_vocabulary() -> None:
    assert trail().observed_at == WINDOW_START
    assert metric().observed_at == WINDOW_START
    assert invocation().observed_at == OBSERVED_AT
    assert span().observed_at == OBSERVED_AT


# --- Public projection of the remaining identity ----------------------------


def sentinel_bundle() -> EvidenceBundle:
    """One bundle whose stage, model and filenames are unique sentinels."""
    configuration = TelemetryRuntimeConfig.model_validate(
        full().model_dump() | {"service_stage": STAGE_SENTINEL}
    )
    labelled = signals(
        spans=(span(gen_ai_request_model=MODEL_SENTINEL),),
        invocations=(invocation(model_revision=MODEL_REVISION_SENTINEL),),
        sources=(
            source("cloudtrail", count=1),
            source("cloudwatch_metrics", count=1),
            source("model_invocations", count=1),
            source("otel_spans", count=1, label=SOURCE_LABEL_SENTINEL),
        ),
    )
    return bundle(configuration=configuration.configured(), signals_=labelled)


@pytest.mark.parametrize(
    "sentinel",
    (STAGE_SENTINEL, MODEL_SENTINEL, MODEL_REVISION_SENTINEL, SOURCE_LABEL_SENTINEL),
    ids=("stage", "span-model", "invocation-model", "source-label"),
)
def test_security_no_deployment_sentinel_survives_public_projection(
    sentinel: str,
) -> None:
    """Regression: stage, model and filename each published as written."""
    private = sentinel_bundle()
    assert sentinel in private.model_dump_json()
    for public in (public_bundle(private), hackathon_export(private)):
        assert sentinel not in public.model_dump_json()


def test_the_public_projection_hashes_the_stage_rather_than_dropping_it() -> None:
    public = public_bundle(sentinel_bundle())
    assert public.configuration.service_stage is None
    assert public.configuration.service_stage_hash is not None
    assert len(public.configuration.service_stage_hash) == 64


def test_the_public_projection_hashes_every_model_identity() -> None:
    public = public_bundle(sentinel_bundle())
    assert public.signals.spans[0].gen_ai_request_model is None
    assert public.signals.spans[0].gen_ai_request_model_hash is not None
    assert public.signals.invocations[0].model_revision is None
    assert public.signals.invocations[0].model_revision_hash is not None


def test_the_public_projection_replaces_every_source_label_with_a_hash() -> None:
    public = public_bundle(sentinel_bundle())
    for item in public.signals.sources:
        assert item.label is None
        assert item.label_hash is not None
        assert item.identity_bearing is False
    assert public.manifest.sources == public.signals.sources


def test_a_source_label_hash_is_domain_separated_from_a_correlation_hash() -> None:
    """A filename is a label, not a join key, so it must not match one."""
    label = "synthetic-spans.json"
    hashed = SignalSource(
        label=label, kind="otel_spans", content_hash=DIGEST, record_count=1
    ).public()
    assert hashed.label_hash is not None
    assert hashed.label_hash != DIGEST
    other = SignalSource(
        label=label, kind="cloudtrail", content_hash=DIGEST, record_count=1
    ).public()
    # The same filename hashes the same whatever kind declared it, so two
    # exports of one file stay comparable.
    assert other.label_hash == hashed.label_hash


def test_a_source_carrying_both_a_label_and_its_hash_is_refused() -> None:
    with pytest.raises(ValidationError):
        SignalSource(
            label="spans.json",
            label_hash=DIGEST,
            kind="otel_spans",
            content_hash=DIGEST,
            record_count=1,
        )
    with pytest.raises(ValidationError):
        SignalSource(kind="otel_spans", content_hash=DIGEST, record_count=1)


def test_a_configuration_carrying_both_a_stage_and_its_hash_is_refused() -> None:
    configured = sentinel_bundle().configuration
    with pytest.raises(ValidationError, match="stage or its stage hash"):
        type(configured).model_validate(
            configured.model_dump() | {"service_stage_hash": DIGEST}
        )


def test_a_public_configuration_naming_its_stage_is_refused() -> None:
    public = sentinel_bundle().configuration.public()
    with pytest.raises(ValidationError):
        type(public).model_validate(
            public.model_dump()
            | {"service_stage": STAGE_SENTINEL, "service_stage_hash": None}
        )


def test_a_record_carrying_both_a_model_identity_and_its_hash_is_refused() -> None:
    with pytest.raises(ValidationError):
        span(gen_ai_request_model_hash=DIGEST)
    with pytest.raises(ValidationError):
        invocation(model_revision_hash=DIGEST)


def test_the_public_projection_of_the_sentinel_bundle_still_verifies() -> None:
    public = public_bundle(sentinel_bundle())
    assert public.verified is True
    assert public.visibility == "public"
    assert public_bundle(public).bundle_hash == public.bundle_hash


def test_the_redaction_policy_hash_covers_the_new_identity_treatments() -> None:
    """The policy hash is what proves two bundles were sanitized alike."""
    from unstuck.telemetry.sanitizer import IDENTITY_TREATMENT

    assert set(IDENTITY_TREATMENT) == {
        "gen_ai_request_model",
        "model_revision",
        "service_name",
        "service_stage",
        "source_label",
    }
    assert redaction_policy_hash() == bundle().manifest.redaction_policy_hash
    assert len(redaction_policy_hash()) == 64


def test_a_bundle_over_an_unreconciled_timeline_is_still_refused() -> None:
    """The new checks do not replace the completeness refusal."""
    with pytest.raises(RuntimeFault) as raised:
        bundle(timeline_=timeline(reconciled=False))
    assert raised.value.reason is Reason.STORAGE_ERROR


def test_the_coverage_matrix_still_derives_from_the_validated_parts() -> None:
    built = bundle()
    assert built.coverage == coverage_matrix(
        configuration=built.configuration,
        timeline=built.timeline,
        signals=built.signals,
    )
