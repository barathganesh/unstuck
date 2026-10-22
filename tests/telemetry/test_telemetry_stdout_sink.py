"""The public line sink: one compact sanitized line per record, or nothing.

This is the sink a hosted container attaches so its telemetry reaches the
platform log. The line is the public projection, so the deployment identity the
private record carries is hashed or dropped before anything is written, and the
typed last check runs over the projection before it is serialized, so a value
that reached an unexpected field is refused rather than written.

Refusal is the point of the negative cases below. A line that would still carry
private identity is not written, and a sink that refuses is counted by the
recorder and never reaches the run.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_stdout_sink.py -q
"""

import json
from datetime import UTC, datetime
from io import StringIO
from uuid import uuid4

import pytest

from unstuck.domain import Counters, Reason, RuntimeFault
from unstuck.events import EventRecord, Measurement
from unstuck.telemetry import collector
from unstuck.telemetry.collector import (
    PUBLIC_LINE_PREFIX,
    PublicLineSink,
    TelemetryRecorder,
    public_line_payload,
)
from unstuck.telemetry.contracts import (
    CorrelationContext,
    InfrastructureDimensions,
    TelemetryCompleteness,
    TelemetryRecord,
    unsafe_public_fields,
)
from unstuck.telemetry.mapping import map_event

QUERY_HASH = "c" * 64
SCHEMA = "inspect_schema_keys"

#: One synthetic deployment identity. Every value here is invented for this
#: test and names no account, Region, runtime, endpoint or log group that
#: exists.
ACCOUNT = "000000000000"
REGION = "aa-example-1"
RUNTIME = "example_controller"
ENDPOINT = "example_private"
LOG_GROUP = "/example/agentcore/runtimes/example-DEFAULT"
PRINCIPAL = "arn:aws:iam::000000000000:role/ExampleRuntimeRole"

HOSTING = InfrastructureDimensions(
    source="runtime_configuration",
    account_id=ACCOUNT,
    region=REGION,
    runtime_id=RUNTIME,
    endpoint_id=ENDPOINT,
    log_group=LOG_GROUP,
    principal_arn=PRINCIPAL,
)

CONTEXT = CorrelationContext(
    case_id="example-case",
    runtime_session_id="unstuck-run-" + str(uuid4()),
    trace_id="a" * 32,
    span_id="b" * 16,
    aws_request_id=str(uuid4()),
)


def event(kind: str, sequence: int = 1, run_id: object | None = None) -> EventRecord:
    return EventRecord(
        run_id=run_id or uuid4(),
        incident_id=uuid4(),
        sequence=sequence,
        timestamp=datetime.now(UTC),
        type=kind,
        input_hash=QUERY_HASH,
        output_hash=QUERY_HASH,
        budgets=Counters(),
        duration_ms=Measurement(),
        payload={"tool_id": SCHEMA, "pass_index": 1},
    )


def private_record(sequence: int = 1, run_id: object | None = None) -> TelemetryRecord:
    record = map_event(
        event("probe_started", sequence, run_id),
        context=CONTEXT,
        infrastructure=HOSTING,
    )
    assert record.visibility == "private"
    return record


def lines(stream: StringIO) -> list[dict[str, object]]:
    """Every written line, prefix required and stripped before validation."""
    written = []
    for line in stream.getvalue().splitlines():
        payload = public_line_payload(line)
        assert payload is not None, line
        written.append(json.loads(payload))
    return written


def test_the_sink_writes_one_compact_public_line_per_record_offline() -> None:
    """One record, one line, already projected onto its public form."""
    stream = StringIO()
    sink = PublicLineSink(stream)

    sink.emit(private_record())

    text = stream.getvalue()
    assert text.endswith("\n")
    assert text.count("\n") == 1
    assert text.startswith(PUBLIC_LINE_PREFIX)
    body = text[len(PUBLIC_LINE_PREFIX) :].rstrip("\n")
    assert body.startswith("{")
    assert ", " not in body and '": ' not in body
    (written,) = lines(stream)
    assert written["visibility"] == "public"
    assert written["kind"] == "probe_started"
    assert sink.written == 1


def test_a_written_line_carries_hashed_correlation_and_no_raw_identity_offline() -> (
    None
):
    """The correlation survives as hashes, so a reader can still group by it."""
    stream = StringIO()

    PublicLineSink(stream).emit(private_record())

    (written,) = lines(stream)
    correlation = written["correlation"]
    assert isinstance(correlation, dict)
    for name in (
        "case_id",
        "runtime_session_id",
        "trace_id",
        "span_id",
        "aws_request_id",
    ):
        assert name not in correlation, name
        assert correlation[f"{name}_hash"]
    for raw in (
        str(CONTEXT.case_id),
        str(CONTEXT.runtime_session_id),
        str(CONTEXT.trace_id),
        str(CONTEXT.span_id),
        str(CONTEXT.aws_request_id),
    ):
        assert raw not in stream.getvalue(), raw


def test_a_written_line_carries_no_raw_hosting_identity_offline() -> None:
    """Account, Region, runtime, endpoint, log group and principal are gone."""
    stream = StringIO()

    PublicLineSink(stream).emit(private_record())

    text = stream.getvalue()
    for raw in (ACCOUNT, REGION, RUNTIME, ENDPOINT, LOG_GROUP, PRINCIPAL):
        assert raw not in text, raw
    (written,) = lines(stream)
    infrastructure = written["infrastructure"]
    assert isinstance(infrastructure, dict)
    assert infrastructure["source"] == "runtime_configuration"
    assert infrastructure["identity_hash"]
    for name in (
        "account_id",
        "region",
        "runtime_id",
        "endpoint_id",
        "agent_id",
        "log_group",
        "model_id",
        "principal_arn",
    ):
        assert name not in infrastructure, name
    payload = public_line_payload(text.rstrip("\n"))
    assert payload is not None
    reread = TelemetryRecord.model_validate_json(payload)
    assert reread.visibility == "public"
    assert unsafe_public_fields(reread) == ()


def test_the_typed_last_check_refuses_a_bypassed_projection_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with sanitization bypassed, a leaking line is never written."""
    stream = StringIO()
    monkeypatch.setattr(collector, "sanitize", lambda record: record)

    with pytest.raises(RuntimeFault) as refused:
        PublicLineSink(stream).emit(private_record())

    assert refused.value.reason is Reason.INVARIANT
    assert stream.getvalue() == ""


def test_an_oversized_line_is_refused_rather_than_truncated_offline() -> None:
    """A line past the declared bound is refused, never written in part."""
    stream = StringIO()
    sink = PublicLineSink(stream, max_line_bytes=16)

    with pytest.raises(RuntimeFault) as refused:
        sink.emit(private_record())

    assert refused.value.reason is Reason.STORAGE_ERROR
    assert stream.getvalue() == ""
    assert sink.written == 0


def test_a_refusing_sink_is_counted_and_never_reaches_the_run_offline() -> None:
    """Transport refusal is the recorder's problem, never the run's."""
    stream = StringIO()
    recorder = TelemetryRecorder(
        PublicLineSink(stream, max_line_bytes=16),
        context=CONTEXT,
        infrastructure=HOSTING,
    )

    assert recorder.record(event("probe_started")) is None

    assert recorder.health.dropped == 1
    assert recorder.health.emitted == 0
    assert stream.getvalue() == ""


def test_the_completeness_summary_is_written_as_its_own_final_line_offline() -> None:
    """The summary travels on the same stream, so the record reconciles."""
    stream = StringIO()
    run_id = uuid4()
    recorder = TelemetryRecorder(
        PublicLineSink(stream), context=CONTEXT, infrastructure=HOSTING
    )

    recorder.record(event("probe_started", 1, run_id))
    recorder.record(event("probe_finished", 2, run_id))
    summary = recorder.finalize()

    assert summary is not None and summary.finalized
    written = lines(stream)
    assert len(written) == 3
    assert [entry.get("kind") for entry in written[:2]] == [
        "probe_started",
        "probe_finished",
    ]
    final = written[-1]
    assert "kind" not in final
    assert final["run_id"] == str(run_id)
    assert final["first_sequence"] == 1
    assert final["final_sequence"] == 2
    assert final["expected_event_count"] == 2
    assert final["emitted"] == 2
    assert final["refused"] == 0
    assert final["dropped"] == 0
    assert final["finalized"] is True


def test_the_summary_line_is_checked_for_identity_like_every_line_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The summary is a line on the same stream and gets the same last check."""
    stream = StringIO()
    sink = PublicLineSink(stream)
    summary = TelemetryCompleteness(
        run_id=uuid4(),
        first_sequence=1,
        final_sequence=1,
        expected_event_count=1,
        emitted=1,
        refused=0,
        dropped=0,
        finalized=True,
    )
    monkeypatch.setattr(collector, "unsafe_public_fields", lambda value: (PRINCIPAL,))

    with pytest.raises(RuntimeFault) as refused:
        sink.finalize(summary)

    assert refused.value.reason is Reason.INVARIANT
    assert stream.getvalue() == ""


def test_the_sink_defaults_to_the_process_stdout_offline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no stream supplied the line reaches stdout, which is the log."""
    PublicLineSink().emit(private_record())

    captured = capsys.readouterr()
    assert captured.err == ""
    (line,) = captured.out.splitlines()
    payload = public_line_payload(line)
    assert payload is not None
    written = json.loads(payload)
    assert written["visibility"] == "public"


def test_every_written_line_including_the_summary_carries_the_prefix_offline() -> None:
    """The prefix is the selector, so the summary line is not an exception."""
    stream = StringIO()
    run_id = uuid4()
    recorder = TelemetryRecorder(
        PublicLineSink(stream), context=CONTEXT, infrastructure=HOSTING
    )

    recorder.record(event("probe_started", 1, run_id))
    summary = recorder.finalize()

    assert summary is not None
    written = stream.getvalue().splitlines()
    assert len(written) == 2
    for line in written:
        assert line.startswith(PUBLIC_LINE_PREFIX), line
        payload = public_line_payload(line)
        assert payload is not None and json.loads(payload)
    assert "kind" not in json.loads(str(public_line_payload(written[-1])))


def test_the_prefix_carries_no_deployment_identity_offline() -> None:
    """A filter built on the prefix is correct in every environment."""
    assert PUBLIC_LINE_PREFIX == "UNSTUCK_TELEMETRY_V1 "
    assert PUBLIC_LINE_PREFIX.endswith(" ")
    for raw in (ACCOUNT, REGION, RUNTIME, ENDPOINT, LOG_GROUP, PRINCIPAL):
        assert raw not in PUBLIC_LINE_PREFIX, raw


def test_unrelated_stdout_is_not_accepted_as_telemetry_offline() -> None:
    """A line without the prefix is answered with nothing, never parsed."""
    for foreign in (
        '{"visibility":"public","kind":"probe_started"}',
        "INFO starting server on the configured port",
        " UNSTUCK_TELEMETRY_V1 {}",
        "UNSTUCK_TELEMETRY_V2 {}",
        "",
    ):
        assert public_line_payload(foreign) is None, foreign


def test_the_byte_bound_counts_the_prefix_as_part_of_the_line_offline() -> None:
    """The bound describes the whole written line, prefix included."""
    record = private_record()
    stream = StringIO()
    PublicLineSink(stream).emit(record)
    (line,) = stream.getvalue().splitlines()
    body_bytes = len(line.encode("utf-8")) - len(PUBLIC_LINE_PREFIX.encode("utf-8"))

    # The payload alone fits inside this bound; the prefixed line does not.
    with pytest.raises(RuntimeFault) as refused:
        PublicLineSink(StringIO(), max_line_bytes=body_bytes).emit(record)

    assert refused.value.reason is Reason.STORAGE_ERROR
    accepted = PublicLineSink(StringIO(), max_line_bytes=len(line.encode("utf-8")))
    accepted.emit(record)
    assert accepted.written == 1
