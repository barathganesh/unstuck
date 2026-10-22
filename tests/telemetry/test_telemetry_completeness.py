"""Completeness: a partial record must not read as the whole causal story.

The defect pinned here is that a timeline accepted a gap. Sequence validation
refused reordering and repeats but allowed `(1, 3)`, and the only account of
what telemetry had actually managed to write lived in the recorder's in-memory
counters. So one transient sink failure followed by a successful append left a
file that a later process could read as complete evidence, with the event that
opened the second decision, recorded the receipt, or fixed the terminal outcome
simply missing.

Two things close it. The timeline now requires contiguous durable sequences,
and the recorder persists its own completeness summary beside the records, so a
reopened file either reconciles against that summary or is refused at the
export gate rather than published.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_completeness.py -q
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from unstuck.cascade.offline import ScriptedChoice
from unstuck.domain import Counters, Reason, RuntimeFault
from unstuck.events import EventRecord, Measurement
from unstuck.telemetry import (
    JsonlSink,
    MemorySink,
    TelemetryCompleteness,
    TelemetryRecord,
    TelemetryRecorder,
    TelemetryTimeline,
    public_export,
)

from .conftest import SCHEMA, compose

QUERY_HASH = "c" * 64


def event(sequence: int, run_id: object | None = None) -> EventRecord:
    return EventRecord(
        run_id=run_id or uuid4(),
        incident_id=uuid4(),
        sequence=sequence,
        timestamp=datetime.now(UTC),
        type="probe_started",
        input_hash=QUERY_HASH,
        output_hash=QUERY_HASH,
        budgets=Counters(diagnostic_reads=1),
        duration_ms=Measurement(),
        payload={"tool_id": SCHEMA, "pass_index": 1, "read_index": 1},
    )


def record(run_id: object, sequence: int) -> TelemetryRecord:
    return TelemetryRecord(
        kind="probe_started",
        correlation={
            "run_id": run_id,
            "event_id": uuid4(),
            "sequence": sequence,
            "timestamp": datetime.now(UTC),
        },
    )


def summary(run_id: object, **changes: object) -> TelemetryCompleteness:
    base: dict[str, object] = {
        "run_id": run_id,
        "first_sequence": 1,
        "final_sequence": 3,
        "expected_event_count": 3,
        "emitted": 3,
        "refused": 0,
        "dropped": 0,
        "finalized": True,
    }
    return TelemetryCompleteness.model_validate(base | changes)


class SometimesUnwritable:
    """A sink that fails on one nominated record, the way a full disk would."""

    def __init__(self, failing: int) -> None:
        self.failing = failing
        self.records: list[TelemetryRecord] = []
        self.completeness: TelemetryCompleteness | None = None

    def emit(self, record: TelemetryRecord) -> None:
        if record.correlation.sequence == self.failing:
            raise OSError("scripted sink failure")
        self.records.append(record)

    def finalize(self, completeness: TelemetryCompleteness) -> None:
        self.completeness = completeness


def test_contract_a_timeline_refuses_a_gap_a_leading_gap_and_a_trailing_gap():
    """Regression: `(1, 3)` used to validate as a complete causal record."""
    run_id = uuid4()
    for sequences in ((1, 3), (1, 2, 4), (1, 3, 4), (2, 4)):
        with pytest.raises(ValidationError):
            TelemetryTimeline(
                run_id=run_id,
                records=tuple(record(run_id, item) for item in sequences),
            )
    whole = TelemetryTimeline(
        run_id=run_id, records=tuple(record(run_id, item) for item in (1, 2, 3))
    )
    assert [item.correlation.sequence for item in whole.records] == [1, 2, 3]
    # A timeline that starts partway through is contiguous and legal; what the
    # completeness summary adds is whether that is the whole run.
    assert TelemetryTimeline(
        run_id=run_id, records=tuple(record(run_id, item) for item in (4, 5))
    ).records


def test_contract_a_completeness_summary_accounts_for_every_observed_event():
    run_id = uuid4()
    assert summary(run_id).complete
    assert summary(run_id, refused=1).complete is False
    assert summary(run_id, emitted=2, dropped=1).complete is False
    # The sequence span has to match the count, so a gap cannot be summarized
    # away as a shorter run.
    with pytest.raises(ValidationError):
        summary(run_id, final_sequence=4)
    with pytest.raises(ValidationError):
        summary(run_id, first_sequence=4, final_sequence=1)
    # Every event is accounted for by exactly one emit attempt.
    with pytest.raises(ValidationError):
        summary(run_id, emitted=2)
    with pytest.raises(ValidationError):
        summary(run_id, refused=4)


def test_contract_a_summary_from_another_run_is_refused_outright():
    run_id = uuid4()
    with pytest.raises(ValidationError):
        TelemetryTimeline(
            run_id=run_id,
            records=tuple(record(run_id, item) for item in (1, 2, 3)),
            completeness=summary(uuid4()),
        )


def test_an_unreconciled_timeline_is_refused_at_the_export_gate():
    run_id = uuid4()
    records = tuple(record(run_id, item) for item in (1, 2, 3))
    for completeness in (
        None,
        summary(run_id, finalized=False),
        summary(run_id, emitted=2, dropped=1),
        summary(run_id, first_sequence=2, final_sequence=4),
    ):
        timeline = TelemetryTimeline(
            run_id=run_id, records=records, completeness=completeness
        )
        assert not timeline.reconciled
        with pytest.raises(RuntimeFault) as error:
            public_export(timeline)
        assert error.value.reason is Reason.STORAGE_ERROR
    reconciled = TelemetryTimeline(
        run_id=run_id, records=records, completeness=summary(run_id)
    )
    assert reconciled.reconciled
    public = public_export(reconciled)
    assert public.visibility == "public"
    assert public.completeness == summary(run_id)


def test_the_recorder_summarizes_the_durable_stream_it_was_handed():
    sink = MemorySink()
    recorder = TelemetryRecorder(sink)
    run_id = uuid4()
    for sequence in (1, 2, 3):
        recorder.record(event(sequence, run_id))
    assert recorder.health.observed == 3
    assert recorder.health.contiguous
    recorded = recorder.finalize()
    assert recorded is not None and recorded.finalized
    assert recorded.expected_event_count == 3
    assert recorded.first_sequence == 1 and recorded.final_sequence == 3
    assert recorded.emitted == 3 and recorded.complete
    assert sink.timeline(run_id).reconciled


def test_a_transient_sink_failure_leaves_the_record_unexportable():
    """The one failure that used to be invisible after a restart."""
    sink = SometimesUnwritable(failing=2)
    recorder = TelemetryRecorder(sink)
    run_id = uuid4()
    for sequence in (1, 2, 3):
        recorder.record(event(sequence, run_id))
    assert recorder.health.dropped == 1
    assert not recorder.health.complete
    # The surviving records have a hole in them, so they are not a timeline at
    # all: the contract refuses the sequence gap outright.
    with pytest.raises(ValidationError):
        TelemetryTimeline(run_id=run_id, records=tuple(sink.records))
    recorded = recorder.finalize()
    assert recorded is not None
    assert recorded.dropped == 1 and not recorded.complete
    assert recorded.expected_event_count == 3 and recorded.emitted == 2


def test_an_event_telemetry_cannot_map_at_all_leaves_no_summary():
    """Fail closed: unbalanced counts mean no summary rather than a false one."""

    class Broken:
        type = "probe_started"
        sequence = 1
        run_id = uuid4()

    recorder = TelemetryRecorder(MemorySink())
    assert recorder.record(Broken()) is None  # type: ignore[arg-type]
    assert recorder.health.refused == 1
    assert recorder.health.observed == 1
    assert recorder.health.emitted == 0 and recorder.health.dropped == 0
    assert recorder.completeness() is None
    assert recorder.finalize() is None


def test_a_refused_record_still_reconciles_because_its_marker_is_recorded():
    """A recorded gap is a fact, not an absence, so the export still holds."""

    class Refusing(TelemetryRecorder):
        def __init__(self, sink: MemorySink) -> None:
            super().__init__(sink)
            self.boundary = _AlwaysUnsafe()

    sink = MemorySink()
    recorder = Refusing(sink)
    run_id = uuid4()
    for sequence in (1, 2):
        recorder.record(event(sequence, run_id))
    recorded = recorder.finalize()
    assert recorded is not None
    assert recorded.refused == 2 and recorded.emitted == 2
    assert not recorded.complete
    timeline = sink.timeline(run_id)
    assert timeline.kinds == ("telemetry_dropped", "telemetry_dropped")
    assert timeline.reconciled
    assert public_export(timeline).kinds == timeline.kinds


class _AlwaysUnsafe:
    """Stands in for a boundary that refuses every record."""

    def validate(self, value: object, reason: Reason = Reason.INVARIANT) -> None:
        raise RuntimeFault(reason)


def test_a_file_reopened_without_a_summary_is_not_exportable(tmp_path):
    """A later process has no counters, so an unsummarized file is refused."""
    path = tmp_path / "telemetry.jsonl"
    recorder = TelemetryRecorder(JsonlSink(path))
    run_id = uuid4()
    for sequence in (1, 2):
        recorder.record(event(sequence, run_id))

    reopened = JsonlSink(path)
    timeline = reopened.read()
    assert timeline.completeness is None and not timeline.reconciled
    with pytest.raises(RuntimeFault) as error:
        public_export(timeline)
    assert error.value.reason is Reason.STORAGE_ERROR

    recorder.finalize()
    summarized = JsonlSink(path).read()
    assert summarized.completeness is not None
    assert summarized.completeness.run_id == run_id
    assert summarized.reconciled
    assert public_export(summarized).visibility == "public"


def test_a_real_run_persists_a_finalized_summary_beside_its_records(tmp_path):
    path = tmp_path / "evidence" / "telemetry.jsonl"
    path.parent.mkdir()
    sink = JsonlSink(path)
    with compose(
        tmp_path / "runs",
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        sink=sink,  # type: ignore[arg-type]
    ) as composed:
        assert composed.execute()["status"] == "completed"
        expected = len(composed.types())

    reopened = JsonlSink(path).read()
    assert len(reopened.records) == expected
    assert reopened.completeness is not None
    assert reopened.completeness.expected_event_count == expected
    assert reopened.completeness.finalized and reopened.completeness.complete
    assert reopened.reconciled
    # The sequences the controller wrote are contiguous, which is what the
    # timeline now requires rather than merely increasing.
    sequences = [item.correlation.sequence for item in reopened.records]
    assert sequences == list(range(sequences[0], sequences[0] + expected))
    assert public_export(reopened).visibility == "public"


def test_a_truncated_file_no_longer_reconciles_with_its_summary(tmp_path):
    """Losing the tail of a finalized file is detected rather than exported."""
    path = tmp_path / "telemetry.jsonl"
    recorder = TelemetryRecorder(JsonlSink(path))
    run_id = uuid4()
    for sequence in (1, 2, 3):
        recorder.record(event(sequence, run_id))
    recorder.finalize()

    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    truncated = JsonlSink(path).read()
    assert len(truncated.records) == 2
    assert truncated.completeness is not None
    assert not truncated.reconciled
    with pytest.raises(RuntimeFault) as error:
        public_export(truncated)
    assert error.value.reason is Reason.STORAGE_ERROR
