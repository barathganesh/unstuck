"""Offline composition for the telemetry tests.

Every test here runs the real controller over the real run repository, the real
DuckDB evidence engine, the real bounded orchestrator, and the real receipt
store. Telemetry is attached as a sink and nothing else is substituted, so a
telemetry assertion is an assertion about the durable record the controller
actually wrote.

The Decider and the order of its answers are scripted offline. No latency, cost,
or token figure observed in these tests is a measurement of anything.
"""

import asyncio
import socket
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
from unstuck.domain import Settings, TaskSpec
from unstuck.events import EventRecord
from unstuck.fixtures import Fixture, load_fixture
from unstuck.query import EvidenceEngine
from unstuck.storage import LocalRunRepository, Snapshot
from unstuck.telemetry import (
    CorrelationContext,
    InfrastructureDimensions,
    MemorySink,
    TelemetryRecord,
    TelemetryTimeline,
)

CASE = "case-01"
REVISION = "scripted-v1"
SCHEMA = "inspect_schema_keys"
PIPELINE = "inspect_pipeline"
SCOPE = "inspect_scope"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network is forbidden in telemetry tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "bind", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@dataclass
class Composed:
    """One composed call plus the telemetry it produced."""

    controller: Controller
    runs: LocalRunRepository
    run: Snapshot
    decider: ScriptedDecider
    receipts: LocalReceiptRepository
    sink: MemorySink

    @property
    def run_id(self) -> UUID:
        return self.run.state.run_id

    def execute(self) -> dict[str, object]:
        return asyncio.run(self.controller.run(self.run_id))

    def final(self) -> Snapshot:
        return self.runs.load_run(self.run_id)

    def events(self) -> tuple[EventRecord, ...]:
        return self.final().events

    def types(self) -> list[str]:
        return [event.type for event in self.events()]

    def timeline(self) -> TelemetryTimeline:
        return self.sink.timeline(self.run_id)

    def kinds(self) -> list[str]:
        return [record.kind for record in self.timeline().records]

    def record(self, kind: str) -> TelemetryRecord:
        return next(record for record in self.timeline().records if record.kind == kind)

    def records(self, kind: str) -> list[TelemetryRecord]:
        return [record for record in self.timeline().records if record.kind == kind]


@contextmanager
def compose(
    root: Path,
    *,
    choices: Sequence[ScriptedChoice],
    key: str,
    arm: str = "decider_memory",
    receipts: LocalReceiptRepository | None = None,
    fixture: Fixture | None = None,
    sink: MemorySink | None = None,
    attach: bool = True,
    correlation: CorrelationContext | None = None,
    infrastructure: InfrastructureDimensions | None = None,
    seed: Sequence[str] = (),
) -> Iterator[Composed]:
    """Wire one real controller onto one real orchestrator with a sink attached.

    `seed` names capabilities whose facts the run already holds, which is the
    one way a real read re-observes known facts and proves no progress.

    `attach` is what makes the control case possible. With it false the
    controller is built with no sink at all, which is the run telemetry has to
    be proven not to change.
    """
    loaded = fixture or load_fixture()
    clock = FakeClock()
    store = LocalReceiptRepository() if receipts is None else receipts
    decider = ScriptedDecider(list(choices))
    engine = CascadeEngine(
        decider=decider,
        repository=store,
        verifier=ReceiptVerifier(clock),
        clock=clock,
        policy=CascadePolicy(baseline_id=arm, model_revision=REVISION),
        matcher=HistoryMatcher(store),
    )
    runs = LocalRunRepository(root, clock=clock)
    task = TaskSpec(dataset_id=loaded.dataset_id, order_date_utc=loaded.order_date_utc)
    run = runs.create_run(task, loaded.dataset_version, key)
    evidence = EvidenceEngine(loaded)
    collected = MemorySink() if sink is None else sink
    try:
        controller = Controller(
            runs,
            evidence,
            clock=clock,
            catalog=default_catalog(),
            settings=Settings(baseline_id=arm),
            cascade=engine,
            namespace=ReceiptNamespace(arm=arm, case_id=CASE),
            idempotency_key=key,
            telemetry=collected if attach else None,
            correlation=correlation,
            infrastructure=infrastructure,
        )
        controller.initial_evidence = tuple(
            evidence.diagnose(capability, run.query) for capability in seed
        )
        yield Composed(controller, runs, run, decider, store, collected)
    finally:
        evidence.close()


def pending_fixture() -> Fixture:
    """A fixture whose partition has not arrived, so the wait path is reachable."""
    loaded = load_fixture()
    return Fixture.model_validate(
        loaded.model_dump()
        | {
            "job_state": "pending",
            "arrival_after_seconds": 3.0,
            "countries_key_encoding": "plain",
            "countries": tuple(
                country.model_dump() | {"country_id": country.country_code}
                for country in loaded.countries
            ),
        }
    )
