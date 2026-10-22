"""The sink seam and the recorder's emission policy.

Two failures are told apart here. A record telemetry refuses is not emitted and
the refusal is recorded, which is fail closed. A sink that cannot be written is
counted and the run is untouched, because an unwritable file is not a reason to
change a verified result.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_collector.py -q
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from unstuck.domain import Counters, Reason, RuntimeFault
from unstuck.events import EventRecord, Measurement
from unstuck.telemetry import (
    CorrelationContext,
    InfrastructureDimensions,
    JsonlSink,
    MemorySink,
    NullSink,
    TelemetryRecord,
    TelemetryRecorder,
    map_event,
)

QUERY_HASH = "c" * 64
SCHEMA = "inspect_schema_keys"


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


def _probe_record() -> TelemetryRecord:
    return map_event(event("probe_started"))


class Unwritable:
    """A sink that always fails, the way a full disk or a bad path would."""

    def __init__(self) -> None:
        self.attempts = 0

    def emit(self, record: TelemetryRecord) -> None:
        self.attempts += 1
        raise OSError("scripted sink failure")


def test_contract_the_default_sink_does_nothing_and_telemetry_stays_inactive():
    """Telemetry is opt-in, so an unconfigured controller does no telemetry work."""
    recorder = TelemetryRecorder()
    assert isinstance(recorder.sink, NullSink)
    assert not recorder.active
    assert recorder.record(event("probe_started")) is None
    assert recorder.health.emitted == 0
    assert recorder.health.complete
    # The null sink is reachable directly and still does nothing.
    assert NullSink().emit(_probe_record()) is None


def test_a_supplied_sink_receives_one_record_per_durable_event():
    sink = MemorySink()
    recorder = TelemetryRecorder(sink)
    assert recorder.active
    run_id = uuid4()
    for index, kind in enumerate(("run_started", "probe_started", "probe_returned"), 1):
        recorder.record(event(kind, index, run_id), stage="RUN_PROBE")
    assert [record.kind for record in sink.records] == [
        "run_started",
        "probe_started",
        "probe_returned",
    ]
    assert recorder.health.emitted == 3
    assert recorder.health.complete
    timeline = sink.timeline(run_id)
    assert timeline.run_id == run_id
    assert timeline.kinds == (
        "run_started",
        "probe_started",
        "probe_returned",
    )
    assert all(record.stage == "RUN_PROBE" for record in timeline.records)


def test_the_supplied_correlation_and_hosting_values_reach_every_record():
    sink = MemorySink()
    recorder = TelemetryRecorder(
        sink,
        context=CorrelationContext(case_id="case-01", arm="decider"),
        infrastructure=InfrastructureDimensions(
            source="runtime_configuration", region="eu-west-1"
        ),
    )
    recorder.record(event("probe_started"))
    recorded = sink.records[0]
    assert recorded.correlation.case_id == "case-01"
    assert recorded.correlation.arm == "decider"
    assert recorded.infrastructure.region == "eu-west-1"


def test_security_a_record_the_persistence_boundary_refuses_is_never_emitted():
    """Fail closed, and say so. The refusal is a recorded fact, not a gap."""

    class Refusing(TelemetryRecorder):
        def __init__(self, sink: MemorySink) -> None:
            super().__init__(sink)
            self.boundary = _AlwaysUnsafe()

    sink = MemorySink()
    recorder = Refusing(sink)
    assert recorder.record(event("probe_started")) is None
    assert recorder.health.refused == 1
    assert not recorder.health.complete
    assert [record.kind for record in sink.records] == ["telemetry_dropped"]
    marker = sink.records[0]
    assert marker.drop_reason == "boundary_refused"
    assert (marker.decision, marker.tool, marker.receipt, marker.outcome) == (
        None,
        None,
        None,
        None,
    )
    # The correlation of the refused event survives, so the gap is locatable.
    assert marker.correlation.sequence == 1


class _AlwaysUnsafe:
    """Stands in for a boundary that refuses every record."""

    def validate(self, value: object, reason: Reason = Reason.INVARIANT) -> None:
        raise RuntimeFault(reason)


def test_a_sink_that_cannot_be_written_is_counted_and_never_raises():
    sink = Unwritable()
    recorder = TelemetryRecorder(sink)
    assert recorder.record(event("probe_started")) is None
    assert sink.attempts == 1
    assert recorder.health.dropped == 1
    assert recorder.health.emitted == 0
    assert not recorder.health.complete


def test_an_event_that_cannot_be_mapped_is_refused_rather_than_guessed():
    class Broken:
        type = "probe_started"

    sink = MemorySink()
    recorder = TelemetryRecorder(sink)
    assert recorder.record(Broken()) is None  # type: ignore[arg-type]
    assert recorder.health.refused == 1
    assert [record.kind for record in sink.records] == []


def test_the_private_writer_appends_one_line_per_record_and_reads_back(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    sink = JsonlSink(path)
    recorder = TelemetryRecorder(sink)
    run_id = uuid4()
    for index, kind in enumerate(("run_started", "probe_started"), 1):
        recorder.record(event(kind, index, run_id))
    assert sink.written == 2
    assert path.read_text(encoding="utf-8").count("\n") == 2
    assert oct(path.stat().st_mode)[-3:] == "600"
    timeline = sink.read()
    assert timeline.run_id == run_id
    assert timeline.kinds == ("run_started", "probe_started")
    assert timeline.visibility == "private"


def test_the_private_writer_refuses_a_symlink_or_a_missing_directory(tmp_path):
    target = tmp_path / "real.jsonl"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(RuntimeFault) as error:
        JsonlSink(link)
    assert error.value.reason is Reason.STORAGE_ERROR
    with pytest.raises(RuntimeFault) as error:
        JsonlSink(tmp_path / "absent" / "telemetry.jsonl")
    assert error.value.reason is Reason.STORAGE_ERROR


def test_an_empty_memory_or_file_sink_has_no_timeline_to_offer(tmp_path):
    with pytest.raises(RuntimeFault) as error:
        MemorySink().timeline()
    assert error.value.reason is Reason.INVARIANT
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeFault) as error:
        JsonlSink(path).read()
    assert error.value.reason is Reason.STORAGE_ERROR


def test_an_unreadable_private_file_is_a_storage_fault_not_a_partial_timeline(
    tmp_path,
):
    """A file that cannot be read is refused whole rather than read in part."""
    path = tmp_path / "telemetry.jsonl"
    sink = JsonlSink(path)
    recorder = TelemetryRecorder(sink)
    recorder.record(event("probe_started"))
    path.write_bytes(b"\xff\xfe not valid utf-8\n")
    with pytest.raises(RuntimeFault) as error:
        sink.read()
    assert error.value.reason is Reason.STORAGE_ERROR
    path.unlink()
    with pytest.raises(RuntimeFault) as error:
        sink.read()
    assert error.value.reason is Reason.STORAGE_ERROR
