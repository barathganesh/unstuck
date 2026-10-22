"""Tests for the evidence export and the read-only benchmark view. Offline."""

import csv
import io
import json

import pytest
from pydantic import ValidationError

from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.benchmark import (
    ARMS,
    RETENTION_ARMS,
    ArmRow,
    BenchmarkView,
    benchmark_view,
    empty_arm,
)
from unstuck.operator.contracts import ExportRequest
from unstuck.operator.export import (
    CSV_COLUMNS,
    EXPORT_MEDIA_TYPES,
    export_bytes,
    manifest,
)
from unstuck.storage import Snapshot

from .conftest import SENTINEL_MARKERS

BOUND_FIELDS = (
    "task_hash",
    "base_query_hash",
    "query_hash",
    "status",
    "reason_code",
    "first_choice",
    "receipt_references",
    "blocked_tool_id",
    "repair_attempts",
    "verification_status",
    "diagnostic_reads",
    "selector_calls",
    "duration_availability",
    "cost_availability",
)


def request_for(
    run: Snapshot, fmt: str, experiment: str | None = None
) -> ExportRequest:
    return ExportRequest(run_id=run.state.run_id, format=fmt, experiment_id=experiment)


def test_the_manifest_binds_every_declared_field_offline(
    completed_run: Snapshot,
) -> None:
    record = manifest(completed_run, request_for(completed_run, "json"))
    for field in BOUND_FIELDS:
        assert field in record, field
    assert record["classification"] == "synthetic"
    assert record["live_checks"] == "Pending"
    assert record["mode"] == "offline"
    assert record["provider"] == "mock"


@pytest.mark.parametrize("fmt", ["jsonl", "json", "csv"])
def test_every_format_is_labelled_explicitly_in_its_payload_offline(
    completed_run: Snapshot, fmt: str
) -> None:
    body = export_bytes(completed_run, request_for(completed_run, fmt)).decode()
    assert fmt in body
    assert EXPORT_MEDIA_TYPES[fmt]
    if fmt == "json":
        assert json.loads(body)["manifest"]["format"] == "json"
    elif fmt == "jsonl":
        first = json.loads(body.splitlines()[0])
        assert first["record"] == "manifest"
        assert first["format"] == "jsonl"
        assert all(json.loads(line) for line in body.splitlines())
    else:
        rows = list(csv.DictReader(io.StringIO(body)))
        assert list(rows[0]) == list(CSV_COLUMNS)
        assert rows[0]["format"] == "csv"


def test_the_export_preserves_the_active_experiment_identifier_offline(
    completed_run: Snapshot,
) -> None:
    body = export_bytes(
        completed_run, request_for(completed_run, "json", "exp-07")
    ).decode()
    assert json.loads(body)["manifest"]["experiment_id"] == "exp-07"
    csv_body = export_bytes(
        completed_run, request_for(completed_run, "csv", "exp-07")
    ).decode()
    assert "exp-07" in csv_body


def test_an_absent_measurement_is_empty_in_csv_never_zero_offline(
    completed_run: Snapshot,
) -> None:
    rows = list(
        csv.DictReader(
            io.StringIO(
                export_bytes(completed_run, request_for(completed_run, "csv")).decode()
            )
        )
    )
    row = rows[0]
    assert row["cost_availability"] == "not_measured"
    assert row["cost"] == ""
    assert row["cost"] != "0"


def test_the_export_carries_no_sentinel_marker_offline(
    completed_run: Snapshot,
) -> None:
    for fmt in ("jsonl", "json", "csv"):
        rendered = (
            export_bytes(completed_run, request_for(completed_run, fmt))
            .decode()
            .lower()
        )
        for marker in SENTINEL_MARKERS:
            assert marker not in rendered, (fmt, marker)


def test_an_unknown_export_format_is_refused_offline(
    completed_run: Snapshot,
) -> None:
    with pytest.raises(ValidationError):
        request_for(completed_run, "xlsx")
    forged = request_for(completed_run, "json").model_copy(update={"format": "xlsx"})
    with pytest.raises(OperatorFault) as raised:
        export_bytes(completed_run, forged)
    assert raised.value.reason is OperatorReason.EXPORT_FORMAT_REJECTED


def test_the_benchmark_view_starts_empty_and_measures_nothing_offline() -> None:
    view = benchmark_view()
    assert isinstance(view, BenchmarkView)
    assert view.corpus_status == "empty"
    assert view.episodes_total == 0
    assert view.retention_enabled is False
    assert view.retention_decision == "pending"
    assert [row.arm for row in view.arms] == list(ARMS)
    for row in view.arms:
        assert row.verified_success.value is None
        assert row.verified_success.text == "Not measured"
    assert "Synthetic" in view.notice
    with pytest.raises(OperatorFault) as raised:
        view.require_retention()
    assert raised.value.reason is OperatorReason.RETENTION_UNDECIDABLE


def test_the_four_arm_names_are_the_design_arms_offline() -> None:
    assert ARMS == ("fixed", "llm", "decider", "decider_memory")
    assert RETENTION_ARMS == ("llm", "decider", "decider_memory")
    rendered = benchmark_view().model_dump_json().lower()
    for stale in ("jev", "decider_jev", "bma"):
        assert stale not in rendered, stale


def test_retention_turns_on_only_with_every_required_arm_offline() -> None:
    """Three measured arms are a partial corpus, never a complete one."""
    from unstuck.operator.benchmark import read_export

    from .test_benchmark import EPISODES, export_body

    partial = benchmark_view(read_export(export_body(("decider", "llm"))))
    assert partial.corpus_status == "partial"
    assert partial.retention_enabled is False

    without_fixed = benchmark_view(read_export(export_body(RETENTION_ARMS)))
    assert without_fixed.corpus_status == "partial"
    assert without_fixed.retention_enabled is False
    assert any("fixed" in gap for gap in without_fixed.gaps)

    complete = benchmark_view(read_export(export_body(ARMS)))
    assert complete.corpus_status == "complete"
    assert complete.retention_enabled is True
    assert complete.episodes_total == len(ARMS) * EPISODES
    complete.require_retention()


def test_the_benchmark_view_refuses_an_incoherent_corpus_offline() -> None:
    row = ArmRow.model_validate(empty_arm("decider").model_dump() | {"episodes": 10})
    with pytest.raises(ValidationError):
        BenchmarkView(corpus_status="empty", episodes_total=0, arms=(row,), notice="x")
    with pytest.raises(ValidationError):
        BenchmarkView(
            corpus_status="empty",
            episodes_total=0,
            arms=(),
            retention_enabled=True,
            notice="x",
        )
    with pytest.raises(ValidationError):
        BenchmarkView(
            corpus_status="empty",
            episodes_total=0,
            arms=(),
            retention_decision="keep",
            notice="x",
        )
