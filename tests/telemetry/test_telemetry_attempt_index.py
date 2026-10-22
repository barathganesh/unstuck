"""Attempt numbering: the decision pass is not the number of the read.

The defect pinned here is that `attempt_index` was copied from the selection
`pass_index`. In the cross-call refused-repetition path the first decision's
choice is blocked before it runs, so it consumes no diagnostic read; the second
decision then performs the first actual read. Copying the pass labeled that
read attempt two beside read one, which overcounts attempted diagnostics and
attributes first-read latency and errors to a second execution.

The index now comes from the durable diagnostic-read counter, recorded
explicitly by the controller at the moment the allowance is charged. The
decision pass is preserved separately, because both facts are real: the read is
attempt one, on decision pass two.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_attempt_index.py -q
"""

from datetime import UTC, datetime
from uuid import uuid4

from unstuck.cascade.offline import ScriptedChoice
from unstuck.cascade.repository import LocalReceiptRepository
from unstuck.domain import Counters
from unstuck.events import EventRecord, Measurement
from unstuck.telemetry import map_event

from .conftest import PIPELINE, SCHEMA, SCOPE, compose

QUERY_HASH = "c" * 64


def probe(payload: dict[str, object], budgets: Counters) -> EventRecord:
    return EventRecord(
        run_id=uuid4(),
        incident_id=uuid4(),
        sequence=1,
        timestamp=datetime.now(UTC),
        type="probe_started",
        input_hash=QUERY_HASH,
        output_hash=QUERY_HASH,
        budgets=budgets,
        duration_ms=Measurement(),
        payload=payload,  # type: ignore[arg-type]
    )


def test_the_recorded_read_index_decides_the_attempt_not_the_decision_pass():
    """Regression: a pass-two first read used to be labeled attempt two."""
    mapped = map_event(
        probe(
            {
                "tool_id": SCHEMA,
                "pass_index": 2,
                "read_index": 1,
                "attempt_index": 1,
                "diagnostic_call_id": str(uuid4()),
            },
            Counters(diagnostic_reads=1),
        )
    )
    tool = mapped.tool
    assert tool is not None
    assert tool.pass_index == 2
    assert tool.attempt_index == 1 and tool.read_index == 1


def test_a_same_call_second_read_is_attempt_two_on_pass_two():
    mapped = map_event(
        probe(
            {"tool_id": SCHEMA, "pass_index": 2, "read_index": 2, "attempt_index": 2},
            Counters(diagnostic_reads=2),
        )
    )
    tool = mapped.tool
    assert tool is not None
    assert tool.pass_index == 2
    assert tool.attempt_index == 2 and tool.read_index == 2


def test_an_event_without_the_recorded_index_falls_back_to_the_charged_counter():
    """The counter is the same durable fact, read when the field is absent."""
    mapped = map_event(
        probe({"tool_id": SCHEMA, "pass_index": 2}, Counters(diagnostic_reads=1))
    )
    tool = mapped.tool
    assert tool is not None
    assert tool.attempt_index == 1 and tool.read_index == 1
    # Nothing charged means nothing claimed, rather than a plausible one.
    absent = map_event(probe({"tool_id": SCHEMA, "pass_index": 1}, Counters()))
    assert absent.tool is not None
    assert absent.tool.attempt_index is None and absent.tool.read_index is None


def test_contract_a_cross_call_blocked_first_decision_consumes_no_attempt(tmp_path):
    """The real composed path: a precluded repetition, then the first real read."""
    receipts = LocalReceiptRepository()
    with compose(
        tmp_path,
        choices=[
            ScriptedChoice(selected_id=SCOPE),
            ScriptedChoice(selected_id=PIPELINE),
        ],
        key="call-1",
        receipts=receipts,
        seed=(SCOPE,),
    ) as first:
        assert first.execute()["status"] == "unresolved"

    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-2",
        receipts=receipts,
        seed=(SCOPE,),
    ) as second:
        result = second.execute()

    assert result["status"] == "completed"
    assert result["history_status"] == "matched"
    kinds = second.kinds()
    assert "repeat_blocked" in kinds and kinds.count("probe_started") == 1
    # The repetition is precluded before the only selector request.
    assert kinds.index("repeat_blocked") < kinds.index("decider_first_requested")
    assert kinds.count("decider_first_requested") == 1
    assert "decider_final_requested" not in kinds

    # The refused execution consumed no read, so it claims no attempt at all.
    refusal = second.record("repeat_blocked")
    assert refusal.tool is not None
    assert refusal.tool.attempt_index is None
    assert refusal.tool.read_index is None

    executed = second.record("probe_started")
    assert executed.tool is not None
    assert executed.tool.public_tool_id == SCHEMA
    # Attempt and read one, on the one fresh first decision.
    assert executed.tool.attempt_index == 1
    assert executed.tool.read_index == 1
    assert executed.tool.pass_index == 1
    assert second.record("decider_first_requested").decision.pass_index == 1

    finished = second.record("run_finished")
    assert finished.outcome is not None and finished.outcome.counters is not None
    assert finished.outcome.counters.diagnostic_reads == 1
    assert finished.outcome.counters.decider_calls == 1


def test_contract_a_same_call_second_decision_numbers_its_reads_one_then_two(
    tmp_path,
):
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE), ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        seed=(SCOPE,),
    ) as composed:
        assert composed.execute()["status"] == "completed"
    reads = composed.records("probe_started")
    assert len(reads) == 2
    assert [item.tool.read_index for item in reads] == [1, 2]
    assert [item.tool.attempt_index for item in reads] == [1, 2]
    assert [item.tool.pass_index for item in reads] == [1, 2]
