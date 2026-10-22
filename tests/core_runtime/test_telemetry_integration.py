"""Core-runtime regression: attaching telemetry changes nothing about a run.

The telemetry schema and its projections are tested under `tests/telemetry`.
What is guarded here is the core runtime's own property: the durable record, the
terminal result, and the persisted events are identical whether or not a sink is
attached, and a sink that cannot be written is not the run's problem.

The composition is built locally rather than imported from another test module,
so this file adds no cross-module import to a directory that is not a package.

The Decider is scripted offline, so no figure here is a measurement.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/core_runtime/test_telemetry_integration.py -q
"""

import asyncio
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

from unstuck.cascade.engine import CascadeEngine
from unstuck.cascade.matching import HistoryMatcher
from unstuck.cascade.offline import ScriptedChoice, ScriptedDecider
from unstuck.cascade.policy import CascadePolicy
from unstuck.cascade.receipts import ReceiptNamespace
from unstuck.cascade.repository import LocalReceiptRepository
from unstuck.cascade.verifier import ReceiptVerifier
from unstuck.clock import FakeClock
from unstuck.controller import Controller
from unstuck.decision import default_catalog
from unstuck.domain import Counters, QuerySpec, Reason, RuntimeFault, Settings, TaskSpec
from unstuck.fixtures import load_fixture
from unstuck.query import EvidenceEngine
from unstuck.storage import LocalRunRepository, Snapshot
from unstuck.telemetry import MemorySink, TelemetryHealth, TelemetryRecord

SCHEMA = "inspect_schema_keys"
SCOPE = "inspect_scope"


class Unwritable:
    """A sink that always fails, the way a full disk would."""

    def __init__(self) -> None:
        self.attempts = 0

    def emit(self, record: TelemetryRecord) -> None:
        self.attempts += 1
        raise OSError("scripted sink failure")


@dataclass
class Composed:
    """One composed run and the repository holding its durable record."""

    controller: Controller
    runs: LocalRunRepository
    run_id: UUID

    def execute(self) -> dict[str, object]:
        return asyncio.run(self.controller.run(self.run_id))

    def final(self) -> Snapshot:
        return self.runs.load_run(self.run_id)

    def types(self) -> list[str]:
        return [event.type for event in self.final().events]

    @property
    def health(self) -> TelemetryHealth:
        return self.controller.telemetry.health


@contextmanager
def composed(
    root: Path,
    *,
    choices: Sequence[ScriptedChoice],
    sink: object | None = None,
    seed: Sequence[str] = (),
) -> Iterator[Composed]:
    fixture = load_fixture()
    clock = FakeClock()
    store = LocalReceiptRepository()
    runs = LocalRunRepository(root, clock=clock)
    task = TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )
    run = runs.create_run(task, fixture.dataset_version, "call-1")
    evidence = EvidenceEngine(fixture)
    engine = CascadeEngine(
        decider=ScriptedDecider(list(choices)),
        repository=store,
        verifier=ReceiptVerifier(clock),
        clock=clock,
        policy=CascadePolicy(
            baseline_id="decider_memory", model_revision="scripted-v1"
        ),
        matcher=HistoryMatcher(store),
    )
    try:
        controller = Controller(
            runs,
            evidence,
            clock=clock,
            catalog=default_catalog(),
            settings=Settings(baseline_id="decider_memory"),
            cascade=engine,
            namespace=ReceiptNamespace(arm="decider_memory", case_id="case-01"),
            idempotency_key="call-1",
            telemetry=sink,  # type: ignore[arg-type]
        )
        controller.initial_evidence = tuple(
            evidence.diagnose(capability, run.query) for capability in seed
        )
        yield Composed(controller, runs, run.state.run_id)
    finally:
        evidence.close()


def test_the_controller_does_no_telemetry_work_until_a_sink_is_supplied(tmp_path):
    with composed(tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)]) as run:
        assert not run.controller.telemetry.active
        result = run.execute()
    assert result["status"] == "completed"
    assert run.health.emitted == 0
    assert run.health.complete


def test_an_attached_sink_observes_every_durable_event_exactly_once(tmp_path):
    sink = MemorySink()
    with composed(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], sink=sink
    ) as run:
        result = run.execute()
        recorded = run.types()
        final = run.final()
    assert result["status"] == "completed"
    assert [record.kind for record in sink.records] == recorded
    assert [record.correlation.sequence for record in sink.records] == [
        event.sequence for event in final.events
    ]
    assert run.health.emitted == len(recorded)
    assert run.health.complete


def test_a_failing_sink_never_changes_the_terminal_result(tmp_path):
    sink = Unwritable()
    with composed(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE), ScriptedChoice(selected_id=SCHEMA)],
        sink=sink,
        seed=(SCOPE,),
    ) as run:
        result = run.execute()
        recorded = run.types()
        final = run.final()
    assert result["status"] == "completed"
    assert result["reason_code"] == "populated_after_repair"
    assert result["first_choice"] == SCOPE and result["final_choice"] == SCHEMA
    assert final.incident.counters == Counters(
        report_queries=2,
        diagnostic_reads=2,
        decider_calls=2,
        history_lookups=1,
        repair_attempts=1,
        agent_turns=1,
    )
    assert sink.attempts == len(recorded)
    assert run.health.dropped == sink.attempts
    assert run.health.emitted == 0


def test_a_sink_attached_to_a_failing_run_still_records_the_terminal_reason(tmp_path):
    sink = MemorySink()
    with composed(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA), ScriptedChoice(selected_id=SCOPE)],
        sink=sink,
    ) as run:

        def failing(capability: str, query: QuerySpec):
            raise RuntimeFault(Reason.TOOL_ERROR)

        run.controller.engine.diagnose = failing  # type: ignore[method-assign]
        result = run.execute()
    assert result["status"] == "unresolved"
    finished = next(record for record in sink.records if record.kind == "run_finished")
    assert finished.outcome is not None
    assert finished.outcome.terminal_status == "unresolved"
    assert finished.outcome.terminal_reason == result["reason_code"]
    assert run.health.complete


def test_the_run_persists_its_completeness_summary_without_changing_anything(
    tmp_path,
):
    """The controller finalizes telemetry's own account at the last event.

    Those counts live in memory, so without this a reopened record could not
    be told apart from a partial one. Persisting them is still observation:
    the terminal result and the durable events are unchanged.
    """
    sink = MemorySink()
    with composed(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], sink=sink
    ) as run:
        result = run.execute()
        recorded = run.types()
    assert result["status"] == "completed"
    summary = sink.completeness
    assert summary is not None and summary.finalized
    assert summary.expected_event_count == len(recorded)
    assert summary.emitted == len(recorded) and summary.complete
    assert summary.final_sequence - summary.first_sequence + 1 == len(recorded)
    assert sink.timeline(summary.run_id).reconciled


def test_a_failing_sink_persists_no_summary_so_nothing_reads_as_complete(tmp_path):
    sink = Unwritable()
    with composed(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], sink=sink
    ) as run:
        assert run.execute()["status"] == "completed"
    assert run.health.dropped == sink.attempts
    assert not run.health.complete
    # Every event was dropped, so the finalized summary says so rather than
    # being absent or claiming a record that was never written.
    recorded = run.controller.telemetry.completeness()
    assert recorded is not None
    assert recorded.emitted == 0 and recorded.dropped == sink.attempts
    assert not recorded.complete


def test_a_sink_observes_nothing_before_the_lease_is_held(tmp_path):
    """Telemetry follows the durable record, so an unleased run observes nothing."""
    sink = MemorySink()
    with composed(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], sink=sink
    ) as run:
        with pytest.raises(RuntimeFault) as error:
            run.controller.decision_state()
    assert error.value.reason is Reason.INVARIANT
    assert sink.records == []
