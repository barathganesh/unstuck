"""External signal import, written first. Offline, local files only.

This leaf imports evidence that some other process already exported. It makes
no AWS call, builds no client, installs no exporter, and opens no socket: a
signal arrives as a local JSON file and is validated into a typed record or
refused.

Six refusals are pinned here. A record carrying a raw principal ARN or a
source address is refused, because that identity has no place in evidence this
project will show an audience. A record carrying prompt, completion, or tool
body text is refused by the schema having nowhere to put it. A declared source
whose record count disagrees with the records it delivered is refused, because
a count that cannot be reconciled is the thing that makes a partial export look
whole. A record whose kind declares no source at all is refused, because
provenance that is optional is provenance nobody has. A record outside the
declared capture window is refused on every one of the four clocks, spans
included. And a file over its declared bound is refused from its own descriptor
before it is read, so an oversized export costs one `fstat` rather than one
allocation of itself.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_signals.py -q
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.telemetry.placeholders import (
    SYNTHETIC_ACCOUNT,
    SYNTHETIC_MODEL_REVISION,
)
from unstuck.domain import Reason, RuntimeFault
from unstuck.telemetry.signals import (
    MAX_SIGNAL_RECORDS,
    SIGNAL_KIND_ORDER,
    CloudTrailRecord,
    CloudWatchMetricRecord,
    ExternalSignals,
    ModelInvocationRecord,
    OtelSpanRecord,
    import_signals,
    read_signal_file,
    unix_nano,
)

TRACE = "a" * 32
SPAN = "b" * 16
PARENT = "c" * 16
SESSION = "synthetic-session-0001"
REQUEST = "synthetic-request-0001"
DIGEST = "d" * 64
WINDOW_START = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 10, 20, 12, 10, tzinfo=UTC)
OUTSIDE_WINDOW = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)

#: The capture window and the span fixture in the Unix nanoseconds a span is
#: exported in. The fixture used to start at nanosecond 1,000,000,000, which is
#: 1970, and was accepted into a 2026 window because spans were never compared
#: against it at all.
WINDOW_START_NANO = unix_nano(WINDOW_START)
WINDOW_END_NANO = unix_nano(WINDOW_END)
SPAN_START_NANO = unix_nano(OBSERVED_AT)
SPAN_END_NANO = SPAN_START_NANO + 250_000_000

#: One deterministic synthetic source filename per imported kind. A synthetic
#: fixture declares its provenance like any other export, because a nonempty
#: kind with no source is refused.
SOURCE_LABELS: dict[str, str] = {
    "otel_spans": "synthetic-spans.json",
    "cloudtrail": "synthetic-trail.json",
    "cloudwatch_metrics": "synthetic-metrics.json",
    "model_invocations": "synthetic-calls.json",
}

#: Which fixture field delivers each imported kind.
SOURCE_FIELDS: dict[str, str] = {
    "otel_spans": "spans",
    "cloudtrail": "trail",
    "cloudwatch_metrics": "metrics",
    "model_invocations": "invocations",
}


def span(**changes: object) -> OtelSpanRecord:
    return OtelSpanRecord.model_validate(
        {
            "trace_id": TRACE,
            "span_id": SPAN,
            "parent_span_id": PARENT,
            "runtime_session_id": SESSION,
            "aws_request_id": REQUEST,
            "span_kind": "client",
            "span_name": "invoke_model",
            "start_unix_nano": SPAN_START_NANO,
            "end_unix_nano": SPAN_END_NANO,
            "status_code": "ok",
            "gen_ai_system": "synthetic",
            "gen_ai_operation_name": "chat",
            "gen_ai_request_model": SYNTHETIC_MODEL_REVISION,
        }
        | changes
    )


def trail(**changes: object) -> CloudTrailRecord:
    return CloudTrailRecord.model_validate(
        {
            "aws_request_id": REQUEST,
            "event_source": "synthetic-service.example",
            "event_name": "InvokeAgentRuntime",
            "event_time": WINDOW_START,
            "caller_class": "assumed_role",
            "caller_identity_hash": DIGEST,
            "resource_type": "AgentRuntime",
            "resource_hash": DIGEST,
        }
        | changes
    )


def metric(**changes: object) -> CloudWatchMetricRecord:
    return CloudWatchMetricRecord.model_validate(
        {
            "namespace": "synthetic/observability",
            "metric_name": "SessionCount",
            "dimensions_hash": DIGEST,
            "statistic": "Sum",
            "period_seconds": 60,
            "timestamp": WINDOW_START,
            "unit": "Count",
            "value": {"availability": "measured", "value": 1.0},
            "runtime_session_id": SESSION,
            "query_window_start": WINDOW_START,
            "query_window_end": WINDOW_END,
        }
        | changes
    )


def invocation(**changes: object) -> ModelInvocationRecord:
    return ModelInvocationRecord.model_validate(
        {
            "observed_at": OBSERVED_AT,
            "aws_request_id": REQUEST,
            "trace_id": TRACE,
            "span_id": SPAN,
            "runtime_session_id": SESSION,
            "model_revision": SYNTHETIC_MODEL_REVISION,
            "latency_ms": {"availability": "measured", "value": 250.0},
            "input_tokens": {"availability": "measured", "value": 120.0},
            "output_tokens": {"availability": "measured", "value": 40.0},
            "input_hash": DIGEST,
            "output_hash": DIGEST,
            "masking_status": "hashed",
        }
        | changes
    )


def source(kind: str, *, count: int, label: str | None = None) -> dict[str, object]:
    """One deterministic synthetic source manifest for an imported kind."""
    return {
        "label": SOURCE_LABELS[kind] if label is None else label,
        "kind": kind,
        "content_hash": DIGEST,
        "record_count": count,
    }


def derived_sources(fields: dict[str, object]) -> tuple[dict[str, object], ...]:
    """One source per kind that delivered a record, in canonical order.

    A kind that delivered nothing gets no source at all, which is the honest
    statement that no export was supplied for it. A test that means "a supplied
    export returned zero records" declares a zero-count source explicitly, and
    the two are different coverage answers.
    """
    return tuple(
        source(kind, count=len(fields[SOURCE_FIELDS[kind]]))  # type: ignore[arg-type]
        for kind in sorted(SIGNAL_KIND_ORDER)
        if fields[SOURCE_FIELDS[kind]]
    )


def signals(**changes: object) -> ExternalSignals:
    fields: dict[str, object] = {
        "capture_window_start": WINDOW_START,
        "capture_window_end": WINDOW_END,
        "spans": (span(),),
        "trail": (trail(),),
        "metrics": (metric(),),
        "invocations": (invocation(),),
    }
    fields |= changes
    fields.setdefault("sources", derived_sources(fields))
    return ExternalSignals.model_validate(fields)


def test_a_span_without_an_end_reports_no_duration_rather_than_zero() -> None:
    assert span().duration_ms.availability == "measured"
    assert span().duration_ms.value == pytest.approx(250.0)
    open_span = span(end_unix_nano=None, status_code="unset")
    assert open_span.duration_ms.availability == "not_measured"
    assert open_span.duration_ms.value is None


def test_a_span_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(ValidationError):
        span(end_unix_nano=1)


def test_a_span_has_nowhere_to_put_prompt_or_completion_text() -> None:
    for field in ("gen_ai_prompt", "gen_ai_completion", "tool_input", "tool_output"):
        with pytest.raises(ValidationError):
            span(**{field: "whatever the model said"})


def test_an_unsupplied_token_count_is_not_supplied_rather_than_zero() -> None:
    assert span().input_tokens.availability == "not_supplied"
    assert span().input_tokens.value is None


@pytest.mark.parametrize(
    "value",
    (
        f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:role/offline-operator",
        "203.0.113.7",
    ),
)
def test_a_trail_record_refuses_a_raw_principal_or_address(value: str) -> None:
    with pytest.raises(ValidationError):
        trail(event_source=value)


def test_a_trail_record_names_a_caller_class_and_never_a_principal() -> None:
    assert trail().caller_class == "assumed_role"
    with pytest.raises(ValidationError):
        trail(caller_arn=f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:role/offline-operator")
    with pytest.raises(ValidationError):
        trail(source_ip_address="203.0.113.7")


def test_an_unmeasured_metric_carries_its_availability() -> None:
    absent = metric(value={"availability": "not_provided"})
    assert absent.value.availability == "not_provided"
    assert absent.value.value is None


def test_a_metric_timestamp_outside_its_own_query_window_is_refused() -> None:
    with pytest.raises(ValidationError):
        metric(timestamp=datetime(2026, 10, 20, 13, 0, tzinfo=UTC))


def test_a_model_record_that_captured_nothing_claims_no_content_hash() -> None:
    with pytest.raises(ValidationError):
        invocation(masking_status="not_captured")
    uncaptured = invocation(
        masking_status="not_captured", input_hash=None, output_hash=None
    )
    assert uncaptured.masking_status == "not_captured"


def test_a_model_record_has_nowhere_to_put_raw_text() -> None:
    for field in ("prompt", "completion", "input_text", "output_text"):
        with pytest.raises(ValidationError):
            invocation(**{field: "whatever the model said"})


def test_the_declared_window_must_be_ordered_and_in_utc() -> None:
    with pytest.raises(ValidationError):
        signals(capture_window_end=WINDOW_START.replace(hour=11))
    with pytest.raises(ValidationError):
        signals(capture_window_start=WINDOW_START.replace(tzinfo=None))


def test_a_record_outside_the_declared_window_is_refused() -> None:
    with pytest.raises(ValidationError):
        signals(trail=(trail(event_time=OUTSIDE_WINDOW),))


def spans_only(**changes: object) -> ExternalSignals:
    """A signal set carrying spans alone, so one kind's sources stand alone."""
    return signals(trail=(), metrics=(), invocations=(), **changes)


def test_a_declared_source_count_must_match_the_records_delivered() -> None:
    with pytest.raises(ValidationError):
        spans_only(sources=(source("otel_spans", count=2),))
    ok = spans_only(sources=(source("otel_spans", count=1),))
    assert ok.sources[0].record_count == 1


def test_a_source_label_is_a_name_and_never_a_local_path() -> None:
    with pytest.raises(ValidationError):
        spans_only(
            sources=(
                source("otel_spans", count=1, label="/var/folders/export/spans.json"),
            )
        )


def test_a_nonempty_kind_with_no_declared_source_is_refused() -> None:
    """Regression: a record used to validate with no provenance at all.

    Each kind is removed from the source set in turn, so no single kind is
    carrying the refusal for the others.
    """
    full = derived_sources(
        {
            "spans": (span(),),
            "trail": (trail(),),
            "metrics": (metric(),),
            "invocations": (invocation(),),
        }
    )
    with pytest.raises(ValidationError):
        signals(sources=())
    for kind in SIGNAL_KIND_ORDER:
        with pytest.raises(ValidationError):
            signals(sources=tuple(item for item in full if item["kind"] != kind))


def test_a_supplied_empty_export_declares_a_zero_count_source() -> None:
    """An export that returned nothing is not an export nobody ran."""
    supplied = spans_only(spans=(), sources=(source("otel_spans", count=0),))
    assert supplied.counts["otel_spans"] == 0
    assert "otel_spans" in supplied.supplied_kinds
    absent = spans_only(spans=(), sources=())
    assert absent.counts["otel_spans"] == 0
    assert "otel_spans" not in absent.supplied_kinds


def test_provenance_is_complete_only_when_every_kind_names_a_source() -> None:
    assert signals().provenance_complete is True
    assert spans_only().provenance_complete is False
    every = signals(
        spans=(),
        trail=(),
        metrics=(),
        invocations=(),
        sources=tuple(source(kind, count=0) for kind in sorted(SIGNAL_KIND_ORDER)),
    )
    assert every.provenance_complete is True


def test_the_signals_counts_are_readable_per_kind() -> None:
    assert signals().counts == {
        "otel_spans": 1,
        "cloudtrail": 1,
        "cloudwatch_metrics": 1,
        "model_invocations": 1,
    }


def test_a_public_projection_hashes_every_raw_signal_identity() -> None:
    public = signals().public()
    assert public.visibility == "public"
    assert public.identity_bearing is False
    assert public.spans[0].trace_id is None
    assert public.spans[0].trace_id_hash is not None
    assert public.spans[0].span_id_hash != public.spans[0].trace_id_hash
    assert public.invocations[0].aws_request_id is None
    assert (
        public.trail[0].aws_request_id_hash == public.invocations[0].aws_request_id_hash
    )
    body = public.model_dump_json()
    for raw in (TRACE, SPAN, SESSION, REQUEST):
        assert raw not in body


def test_a_public_projection_is_idempotent() -> None:
    once = signals().public()
    assert once.public() == once


def test_a_public_projection_keeps_its_measurements_and_classes() -> None:
    public = signals().public()
    assert public.invocations[0].latency_ms.value == pytest.approx(250.0)
    assert public.metrics[0].value.availability == "measured"
    assert public.trail[0].caller_class == "assumed_role"


def test_importing_reads_a_local_file_and_records_its_hash(tmp_path: Path) -> None:
    path = tmp_path / "spans.json"
    path.write_text(json.dumps([span().model_dump(mode="json")]), encoding="utf-8")
    source, body = read_signal_file(path, "otel_spans")
    assert source.label == "spans.json"
    assert source.kind == "otel_spans"
    assert source.record_count == 1
    assert len(source.content_hash) == 64
    assert json.loads(body) == [span().model_dump(mode="json")]


def test_importing_refuses_a_file_over_its_declared_bound(tmp_path: Path) -> None:
    path = tmp_path / "spans.json"
    path.write_text(json.dumps([span().model_dump(mode="json")]), encoding="utf-8")
    with pytest.raises(RuntimeFault) as raised:
        read_signal_file(path, "otel_spans", max_bytes=8)
    assert raised.value.reason is Reason.STORAGE_ERROR


def test_importing_refuses_a_symlinked_export(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps([]), encoding="utf-8")
    link = tmp_path / "spans.json"
    link.symlink_to(real)
    with pytest.raises(RuntimeFault) as raised:
        read_signal_file(link, "otel_spans")
    assert raised.value.reason is Reason.STORAGE_ERROR


def test_importing_refuses_a_directory_and_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RuntimeFault):
        read_signal_file(tmp_path, "otel_spans")
    with pytest.raises(RuntimeFault):
        read_signal_file(tmp_path / "absent.json", "otel_spans")


def test_importing_refuses_malformed_json_with_a_reason_code(tmp_path: Path) -> None:
    path = tmp_path / "spans.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeFault) as raised:
        read_signal_file(path, "otel_spans")
    assert raised.value.reason is Reason.STORAGE_ERROR


def test_importing_refuses_more_records_than_the_declared_bound(tmp_path: Path) -> None:
    path = tmp_path / "spans.json"
    body = [span().model_dump(mode="json")] * (MAX_SIGNAL_RECORDS + 1)
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(RuntimeFault):
        read_signal_file(path, "otel_spans")


def test_importing_builds_typed_signals_whose_sources_reconcile(tmp_path: Path) -> None:
    (tmp_path / "spans.json").write_text(
        json.dumps([span().model_dump(mode="json")]), encoding="utf-8"
    )
    (tmp_path / "trail.json").write_text(
        json.dumps([trail().model_dump(mode="json")]), encoding="utf-8"
    )
    imported = import_signals(
        (
            ("otel_spans", tmp_path / "spans.json"),
            ("cloudtrail", tmp_path / "trail.json"),
        ),
        capture_window_start=WINDOW_START,
        capture_window_end=WINDOW_END,
    )
    assert imported.counts["otel_spans"] == 1
    assert imported.counts["cloudtrail"] == 1
    assert imported.counts["cloudwatch_metrics"] == 0
    assert {source.label for source in imported.sources} == {
        "spans.json",
        "trail.json",
    }


def test_importing_refuses_a_record_the_contract_rejects(tmp_path: Path) -> None:
    path = tmp_path / "spans.json"
    path.write_text(json.dumps([{"trace_id": TRACE}]), encoding="utf-8")
    with pytest.raises(RuntimeFault):
        import_signals(
            (("otel_spans", path),),
            capture_window_start=WINDOW_START,
            capture_window_end=WINDOW_END,
        )


def test_importing_covers_every_kind_it_declares(tmp_path: Path) -> None:
    for name, body in (
        ("spans.json", [span()]),
        ("trail.json", [trail()]),
        ("metrics.json", [metric()]),
        ("calls.json", [invocation()]),
    ):
        (tmp_path / name).write_text(
            json.dumps([record.model_dump(mode="json") for record in body]),
            encoding="utf-8",
        )
    imported = import_signals(
        (
            ("otel_spans", tmp_path / "spans.json"),
            ("cloudtrail", tmp_path / "trail.json"),
            ("cloudwatch_metrics", tmp_path / "metrics.json"),
            ("model_invocations", tmp_path / "calls.json"),
        ),
        capture_window_start=WINDOW_START,
        capture_window_end=WINDOW_END,
    )
    assert imported.counts == {
        "otel_spans": 1,
        "cloudtrail": 1,
        "cloudwatch_metrics": 1,
        "model_invocations": 1,
    }
    assert len(imported.sources) == 4


def test_importing_refuses_a_window_that_excludes_its_own_records(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trail.json"
    path.write_text(json.dumps([trail().model_dump(mode="json")]), encoding="utf-8")
    with pytest.raises(RuntimeFault) as raised:
        import_signals(
            (("cloudtrail", path),),
            capture_window_start=datetime(2026, 10, 20, 13, 0, tzinfo=UTC),
            capture_window_end=datetime(2026, 10, 20, 14, 0, tzinfo=UTC),
        )
    assert raised.value.reason is Reason.STORAGE_ERROR


def test_importing_refuses_a_file_whose_name_is_not_a_label(tmp_path: Path) -> None:
    path = tmp_path / "exported spans!.json"
    path.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(RuntimeFault) as raised:
        read_signal_file(path, "otel_spans")
    assert raised.value.reason is Reason.STORAGE_ERROR
