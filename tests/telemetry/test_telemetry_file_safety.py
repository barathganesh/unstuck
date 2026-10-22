"""Private file safety: descriptors, not pathnames.

Two defects are pinned here. `os.open(..., 0o600)` applies its mode only when
it creates a file, so appending to a target that already existed as `0644` left
private telemetry world-readable. And the reader used `Path.read_text()`, which
resolves the pathname again, so replacing the target, or its parent directory,
after the sink was constructed redirected the read.

The sink now pins the parent directory once, opens the target through that
descriptor with `O_NOFOLLOW`, and verifies the opened descriptor by `fstat`
rather than by name: a regular file, owned by this process, with exactly one
link, on the same device and inode as the first open, and private to its owner.
A broader mode is tightened only after that ownership and link check passes.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_file_safety.py -q
"""

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from unstuck.domain import Counters, Reason, RuntimeFault
from unstuck.events import EventRecord, Measurement
from unstuck.telemetry import (
    PRIVATE_MODE,
    SUMMARY_SUFFIX,
    JsonlSink,
    TelemetryRecorder,
    map_event,
)

QUERY_HASH = "c" * 64
SCHEMA = "inspect_schema_keys"


def event(sequence: int = 1, run_id: object | None = None) -> EventRecord:
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


def mode_of(path) -> str:
    return oct(path.stat().st_mode)[-3:]


def test_security_an_existing_world_readable_file_is_made_private_on_append(
    tmp_path,
):
    """Regression: the private mode used to apply only on creation."""
    path = tmp_path / "telemetry.jsonl"
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o644)
    assert mode_of(path) == "644"

    sink = JsonlSink(path)
    TelemetryRecorder(sink).record(event())
    assert mode_of(path) == "600"
    assert sink.written == 1
    assert sink.read().kinds == ("probe_started",)


def test_security_an_existing_file_with_a_second_link_is_refused(tmp_path):
    """A hard link means another name reaches the same private bytes."""
    path = tmp_path / "telemetry.jsonl"
    path.write_text("", encoding="utf-8")
    os.link(path, tmp_path / "second-name.jsonl")
    sink = JsonlSink(path)
    with pytest.raises(RuntimeFault) as error:
        sink.emit(map_event(event()))
    assert error.value.reason is Reason.STORAGE_ERROR
    # Refused, and the broader mode is left exactly as it was found: this
    # never rewrites the permissions of a file reachable under another name.
    assert mode_of(path) == mode_of(tmp_path / "second-name.jsonl")


def test_security_a_replaced_target_is_detected_by_its_inode_not_its_name(
    tmp_path,
):
    """Regression: a read used to follow whatever the pathname then named."""
    path = tmp_path / "telemetry.jsonl"
    sink = JsonlSink(path)
    recorder = TelemetryRecorder(sink)
    recorder.record(event())
    assert sink.read().kinds == ("probe_started",)

    # Replace the target with a different file under the same name.
    other = tmp_path / "other.jsonl"
    other.write_text("", encoding="utf-8")
    os.chmod(other, PRIVATE_MODE)
    os.replace(other, path)
    with pytest.raises(RuntimeFault) as error:
        sink.read()
    assert error.value.reason is Reason.STORAGE_ERROR
    # An append is refused on the same inode check, and the run is untouched:
    # the recorder counts the drop rather than raising.
    with pytest.raises(RuntimeFault) as error:
        sink.emit(map_event(event()))
    assert error.value.reason is Reason.STORAGE_ERROR
    assert recorder.record(event(sequence=2)) is None
    assert recorder.health.dropped == 1


def test_security_a_target_replaced_by_a_symlink_is_never_followed(tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    path = tmp_path / "telemetry.jsonl"
    sink = JsonlSink(path)
    TelemetryRecorder(sink).record(event())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(RuntimeFault) as error:
        sink.read()
    assert error.value.reason is Reason.STORAGE_ERROR
    assert outside.read_text(encoding="utf-8") == ""


def test_security_a_symlinked_parent_directory_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(RuntimeFault) as error:
        JsonlSink(linked / "telemetry.jsonl")
    assert error.value.reason is Reason.STORAGE_ERROR
    # The real directory is accepted, so the refusal is about the link alone.
    assert JsonlSink(real / "telemetry.jsonl").name == "telemetry.jsonl"


def test_security_a_replaced_parent_directory_cannot_redirect_a_write(tmp_path):
    """The directory is pinned, so the pathname no longer decides anything."""
    original = tmp_path / "evidence"
    original.mkdir()
    sink = JsonlSink(original / "telemetry.jsonl")
    recorder = TelemetryRecorder(sink)
    run_id = uuid4()
    recorder.record(event(1, run_id))

    decoy = tmp_path / "decoy"
    decoy.mkdir()
    original.rename(tmp_path / "moved")
    decoy.rename(original)
    recorder.record(event(2, run_id))
    # Both records went to the pinned directory, not to the decoy now standing
    # at the original path.
    assert not (original / "telemetry.jsonl").exists()
    assert len(sink.read().records) == 2


def test_security_a_missing_directory_an_empty_name_and_a_directory_are_refused(
    tmp_path,
):
    with pytest.raises(RuntimeFault) as error:
        JsonlSink(tmp_path / "absent" / "telemetry.jsonl")
    assert error.value.reason is Reason.STORAGE_ERROR
    with pytest.raises(RuntimeFault) as error:
        JsonlSink(Path("/"))
    assert error.value.reason is Reason.STORAGE_ERROR
    # A name that resolves to a directory is refused on the `fstat` check,
    # because the descriptor is what is verified rather than the pathname.
    nested = tmp_path / "nested"
    nested.mkdir()
    with pytest.raises(RuntimeFault) as error:
        JsonlSink(nested).read()
    assert error.value.reason is Reason.STORAGE_ERROR


def test_the_completeness_summary_is_written_private_beside_the_records(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    sink = JsonlSink(path)
    recorder = TelemetryRecorder(sink)
    run_id = uuid4()
    recorder.record(event(1, run_id))
    recorder.finalize()
    summary = tmp_path / ("telemetry.jsonl" + SUMMARY_SUFFIX)
    assert summary.exists() and mode_of(summary) == "600"
    assert sink.read().completeness is not None


def test_security_an_existing_world_readable_summary_is_made_private(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    summary = tmp_path / ("telemetry.jsonl" + SUMMARY_SUFFIX)
    summary.write_text("{}", encoding="utf-8")
    os.chmod(summary, 0o644)
    recorder = TelemetryRecorder(JsonlSink(path))
    recorder.record(event())
    assert recorder.finalize() is not None
    assert mode_of(summary) == "600"


def test_security_a_summary_path_replaced_by_a_symlink_is_never_followed(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    outside = tmp_path / "outside.json"
    outside.write_text("", encoding="utf-8")
    summary = tmp_path / ("telemetry.jsonl" + SUMMARY_SUFFIX)
    summary.symlink_to(outside)
    sink = JsonlSink(path)
    recorder = TelemetryRecorder(sink)
    recorder.record(event())
    # The recorder never raises into the run, so the refusal shows up as no
    # summary being persisted at all.
    assert recorder.finalize() is None
    assert outside.read_text(encoding="utf-8") == ""
    assert sink.read().completeness is None


def test_a_malformed_summary_sidecar_leaves_the_timeline_unreconciled(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    sink = JsonlSink(path)
    TelemetryRecorder(sink).record(event())
    (tmp_path / ("telemetry.jsonl" + SUMMARY_SUFFIX)).write_text(
        "{not json}", encoding="utf-8"
    )
    timeline = sink.read()
    assert timeline.completeness is None and not timeline.reconciled


def test_a_closed_sink_refuses_every_later_operation(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    sink = JsonlSink(path)
    recorder = TelemetryRecorder(sink)
    recorder.record(event())
    sink.close()
    sink.close()
    with pytest.raises(RuntimeFault) as error:
        sink.read()
    assert error.value.reason is Reason.STORAGE_ERROR
    with pytest.raises(RuntimeFault) as error:
        sink.emit(map_event(event()))
    assert error.value.reason is Reason.STORAGE_ERROR
    # And the run is still untouched: the recorder counts it and carries on.
    assert recorder.finalize() is None
    assert recorder.record(event(sequence=2)) is None
    assert recorder.health.dropped == 1


def test_a_sink_with_nowhere_to_persist_a_summary_still_reports_one(tmp_path):
    """`finalize` is optional on a sink, so its absence is not a failure."""

    class EmitOnly:
        def __init__(self) -> None:
            self.records: list[object] = []

        def emit(self, record: object) -> None:
            self.records.append(record)

    recorder = TelemetryRecorder(EmitOnly())  # type: ignore[arg-type]
    recorder.record(event())
    recorded = recorder.finalize()
    assert recorded is not None and recorded.finalized
    assert recorded.emitted == 1
