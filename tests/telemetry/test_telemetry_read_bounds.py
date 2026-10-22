"""Bounded reads: the limit runs before the allocation, not after it.

The defect pinned here is that the reader called `read_text().splitlines()`,
which loaded the whole file and built every line string before any record was
validated, and the 200-record bound only ran when the timeline was constructed
after all of them had already been parsed. A malformed or oversized evidence
file could therefore consume unbounded memory and CPU before being rejected,
and a malformed line raised a raw validation error rather than the sink's own
reason-coded storage fault.

The read now streams under three declared bounds: total file bytes, checked
from the opened descriptor before anything is read; bytes per line, checked
before the line is parsed; and record count, checked before the next line is
parsed. Each limit is exercised at its exact value and one over.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_read_bounds.py -q
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from unstuck.domain import Counters, Reason, RuntimeFault
from unstuck.events import EventRecord, Measurement
from unstuck.telemetry import (
    MAX_FILE_BYTES,
    MAX_LINE_BYTES,
    MAX_TIMELINE_RECORDS,
    JsonlSink,
    map_event,
)

QUERY_HASH = "c" * 64
SCHEMA = "inspect_schema_keys"


def line(sequence: int, run_id: object) -> str:
    """One well-formed record line, as the sink itself would have written it."""
    source = EventRecord(
        run_id=run_id,
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
    return map_event(source).model_dump_json()


def write(path, lines: list[str]) -> None:
    path.write_text("".join(item + "\n" for item in lines), encoding="utf-8")


def test_contract_the_declared_bounds_are_the_ones_the_reader_uses():
    """The limits are the contract's, not an implementation detail."""
    assert MAX_TIMELINE_RECORDS == 200
    assert MAX_LINE_BYTES == 65_536
    assert MAX_FILE_BYTES == MAX_TIMELINE_RECORDS * (MAX_LINE_BYTES + 1)


def test_the_record_bound_accepts_its_exact_limit_and_refuses_one_more(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    run_id = uuid4()
    sink = JsonlSink(path, max_records=3)
    write(path, [line(index, run_id) for index in range(1, 4)])
    assert len(sink.read().records) == 3
    write(path, [line(index, run_id) for index in range(1, 5)])
    with pytest.raises(RuntimeFault) as error:
        sink.read()
    assert error.value.reason is Reason.STORAGE_ERROR


def test_the_default_record_bound_is_two_hundred(tmp_path):
    """The contract's own bound, exercised at the limit and one over."""
    path = tmp_path / "telemetry.jsonl"
    run_id = uuid4()
    sink = JsonlSink(path)
    write(path, [line(index, run_id) for index in range(1, MAX_TIMELINE_RECORDS + 1)])
    assert len(sink.read().records) == MAX_TIMELINE_RECORDS
    write(path, [line(index, run_id) for index in range(1, MAX_TIMELINE_RECORDS + 2)])
    with pytest.raises(RuntimeFault) as error:
        sink.read()
    assert error.value.reason is Reason.STORAGE_ERROR


def test_the_line_bound_accepts_its_exact_limit_and_refuses_one_more(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    run_id = uuid4()
    body = line(1, run_id)
    sink = JsonlSink(path, max_line_bytes=len(body))
    write(path, [body])
    assert len(sink.read().records) == 1
    # One byte over the bound is refused before the line is parsed, so the
    # content being otherwise valid JSON makes no difference.
    tight = JsonlSink(path, max_line_bytes=len(body) - 1)
    with pytest.raises(RuntimeFault) as error:
        tight.read()
    assert error.value.reason is Reason.STORAGE_ERROR


def test_a_single_oversized_line_is_refused_without_being_read_whole(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    sink = JsonlSink(path, max_line_bytes=64, max_file_bytes=1_000_000)
    path.write_text("x" * 500_000 + "\n", encoding="utf-8")
    with pytest.raises(RuntimeFault) as error:
        sink.read()
    assert error.value.reason is Reason.STORAGE_ERROR


def test_the_file_bound_accepts_its_exact_limit_and_refuses_one_more(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    run_id = uuid4()
    body = line(1, run_id) + "\n"
    path.write_text(body, encoding="utf-8")
    exact = JsonlSink(path, max_file_bytes=len(body.encode()))
    assert len(exact.read().records) == 1
    over = JsonlSink(path, max_file_bytes=len(body.encode()) - 1)
    with pytest.raises(RuntimeFault) as error:
        over.read()
    assert error.value.reason is Reason.STORAGE_ERROR


def test_malformed_json_becomes_a_reason_coded_storage_fault(tmp_path):
    """Regression: a raw validation error used to reach the caller."""
    path = tmp_path / "telemetry.jsonl"
    run_id = uuid4()
    sink = JsonlSink(path)
    for body in (
        "{not json at all}",
        '{"kind": "probe_started"}',
        "[]",
        "null",
        '{"kind": "probe_started", "correlation": {"sequence": 0}}',
    ):
        write(path, [line(1, run_id), body])
        with pytest.raises(RuntimeFault) as error:
            sink.read()
        assert error.value.reason is Reason.STORAGE_ERROR
        assert error.value.__cause__ is not None


def test_invalid_utf8_and_an_empty_file_are_storage_faults(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    sink = JsonlSink(path)
    path.write_bytes(b"\xff\xfe not valid utf-8\n")
    with pytest.raises(RuntimeFault) as error:
        sink.read()
    assert error.value.reason is Reason.STORAGE_ERROR
    path.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeFault) as error:
        sink.read()
    assert error.value.reason is Reason.STORAGE_ERROR


def test_blank_lines_are_skipped_without_counting_against_the_record_bound(
    tmp_path,
):
    path = tmp_path / "telemetry.jsonl"
    run_id = uuid4()
    sink = JsonlSink(path, max_records=2)
    path.write_text(
        "\n".join(["", line(1, run_id), "", "   ", line(2, run_id), ""]) + "\n",
        encoding="utf-8",
    )
    assert len(sink.read().records) == 2


def test_an_oversized_summary_sidecar_is_ignored_rather_than_trusted(tmp_path):
    """An unreadable summary leaves the timeline unreconciled, never complete."""
    path = tmp_path / "telemetry.jsonl"
    run_id = uuid4()
    write(path, [line(1, run_id)])
    bound = len(line(1, run_id)) + 1
    (tmp_path / "telemetry.jsonl.summary.json").write_text(
        "x" * (bound + 1), encoding="utf-8"
    )
    timeline = JsonlSink(path, max_line_bytes=bound).read()
    assert timeline.completeness is None
    assert not timeline.reconciled
