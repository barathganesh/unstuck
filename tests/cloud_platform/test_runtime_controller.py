"""Lease ownership across the runtime-to-controller seam. Offline.

`ControllerRuntime` is the one layer that acquires a run when it serves an
invocation. The controller it drives therefore continues from the already
leased snapshot through `Controller.resume` instead of acquiring the same run a
second time. These tests compose the real runtime surface with the real
controller, the real run repository, and the real DuckDB evidence engine, so a
second acquisition would be visible as a refusal rather than hidden behind an
injected callback.

No AWS account, credential, socket, or live provider is reachable here.
"""

import asyncio
from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from unstuck.clock import FakeClock
from unstuck.cloud.runtime import (
    INVOCATIONS_PATH,
    ControllerRuntime,
)
from unstuck.controller import Controller
from unstuck.decision import Decision, DecisionState, OptionProbability
from unstuck.domain import TaskSpec
from unstuck.fixtures import load_fixture
from unstuck.query import EvidenceEngine
from unstuck.storage import LocalRunRepository, Snapshot

HEADERS = {
    "x-amzn-bedrock-agentcore-runtime-caller": "arn:aws:sts::000000000000:role/r"
}


class AcceptingIdentity:
    """Offline stand-in for the verifier PF6 will supply."""

    def verify(self, headers: Mapping[str, str]) -> str:
        if not headers:
            raise AssertionError("an unauthenticated invocation must never be served")
        return "offline-principal"


class SelectsSchema:
    """A scripted one-pass selector. Not a provider and not a measurement."""

    def __init__(self) -> None:
        self.calls: list[DecisionState] = []

    async def select(self, state: DecisionState) -> Decision:
        self.calls.append(state)
        identifiers = tuple(probe.id for probe in state.candidates) + (
            "request_review",
        )
        chosen = "inspect_schema_keys"
        return Decision(
            candidate_ids=identifiers,
            selected_id=chosen,
            confidence=0.95,
            probabilities=tuple(
                OptionProbability(id=name, probability=float(name == chosen))
                for name in identifiers
            ),
            validation_status="valid",
        )


@pytest.fixture
def composed(tmp_path):
    fixture = load_fixture()
    clock = FakeClock()
    runs = LocalRunRepository(tmp_path, clock=clock)
    evidence = EvidenceEngine(fixture)
    decider = SelectsSchema()
    controller = Controller(runs, evidence, decider, clock=clock)
    task = TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )
    try:
        yield controller, runs, task, fixture.dataset_version, decider
    finally:
        evidence.close()


def surface_for(
    runs: LocalRunRepository, task: TaskSpec, version: str, controller: Controller
) -> ControllerRuntime:
    def execute(snapshot: Snapshot, owner: UUID) -> dict[str, JsonValue]:
        return asyncio.run(controller.resume(snapshot, owner))

    return ControllerRuntime(
        runs, task, version, execute=execute, verifier=AcceptingIdentity()
    )


def test_the_runtime_is_the_only_layer_that_acquires_the_run_lease_offline(composed):
    controller, runs, task, version, decider = composed
    surface = surface_for(runs, task, version, controller)
    status, body = surface.handle(
        "POST", INVOCATIONS_PATH, HEADERS, {"idempotency_key": "cloud-case-01"}
    )
    assert status == 200
    assert body["status"] == "accepted"
    result = body["result"]
    assert isinstance(result, dict)
    assert result["status"] == "completed"
    assert result["reason_code"] == "populated_after_repair"
    assert len(decider.calls) == 1
    final = runs.load_run(UUID(str(body["run_id"])))
    types = [event.type for event in final.events]
    # One acquisition means exactly one lease event and one terminal event.
    assert types.count("run_started") == 1
    assert types.count("run_finished") == 1
    assert final.state.status == "completed"
    assert final.state.lease_owner is not None


def test_a_second_acquisition_of_a_live_lease_is_refused_offline(composed):
    controller, runs, task, version, decider = composed
    created = runs.create_run(task, version, "cloud-case-02")
    run_id = created.state.run_id
    owner = uuid4()
    leased = runs.acquire(run_id, owner)
    assert leased is not None
    # The controller must not acquire a run another layer already holds.
    assert asyncio.run(controller.run(run_id)) == {
        "run_id": str(run_id),
        "status": "lease_rejected",
    }
    assert not decider.calls
    # Continuing from the held lease is the composition path.
    result = asyncio.run(controller.resume(leased, owner))
    assert result["status"] == "completed"
    assert len(decider.calls) == 1
    final = runs.load_run(run_id)
    assert [event.type for event in final.events].count("run_started") == 1


def test_resuming_a_lease_the_controller_does_not_hold_is_refused_offline(composed):
    controller, runs, task, version, decider = composed
    created = runs.create_run(task, version, "cloud-case-03")
    run_id = created.state.run_id
    # A snapshot that was never leased carries no writer authority.
    assert asyncio.run(controller.resume(created, uuid4())) == {
        "run_id": str(run_id),
        "status": "lease_rejected",
    }
    leased = runs.acquire(run_id, uuid4())
    assert leased is not None
    assert asyncio.run(controller.resume(leased, uuid4())) == {
        "run_id": str(run_id),
        "status": "lease_rejected",
    }
    assert not decider.calls
    final = runs.load_run(run_id)
    assert [event.type for event in final.events] == ["run_started"]


def test_a_completed_duplicate_invocation_returns_its_stored_result_offline(composed):
    """The run store is the durable idempotency authority, not this layer."""
    controller, runs, task, version, decider = composed
    surface = surface_for(runs, task, version, controller)
    first = surface.handle(
        "POST", INVOCATIONS_PATH, HEADERS, {"idempotency_key": "cloud-case-04"}
    )
    assert first[1]["status"] == "accepted"
    second = surface.handle(
        "POST", INVOCATIONS_PATH, HEADERS, {"idempotency_key": "cloud-case-04"}
    )
    assert second[0] == 200
    assert second[1]["status"] == "duplicate"
    assert second[1]["reason_code"] is None
    assert second[1]["run_id"] == first[1]["run_id"]
    assert second[1]["result"] == first[1]["result"]
    assert len(decider.calls) == 1
    final = runs.load_run(UUID(str(first[1]["run_id"])))
    assert [event.type for event in final.events].count("run_started") == 1
    assert [event.type for event in final.events].count("run_finished") == 1


def test_an_in_flight_duplicate_invocation_acquires_nothing_offline(composed):
    controller, runs, task, version, decider = composed
    surface = surface_for(runs, task, version, controller)
    created = runs.create_run(task, version, "cloud-case-05")
    run_id = created.state.run_id
    # Another writer owns the run; the duplicate neither acquires nor executes.
    assert runs.acquire(run_id, uuid4()) is not None
    status, body = surface.handle(
        "POST", INVOCATIONS_PATH, HEADERS, {"idempotency_key": "cloud-case-05"}
    )
    assert status == 200
    assert body["status"] == "in_flight"
    assert body["run_id"] == str(run_id)
    assert body["result"] is None
    assert not decider.calls
    final = runs.load_run(run_id)
    assert [event.type for event in final.events] == ["run_started"]


def test_a_fresh_runtime_resolves_a_completed_duplicate_from_storage_offline(composed):
    """No process-local state is involved in duplicate resolution."""
    controller, runs, task, version, decider = composed
    first = surface_for(runs, task, version, controller).handle(
        "POST", INVOCATIONS_PATH, HEADERS, {"idempotency_key": "cloud-case-06"}
    )
    assert first[1]["status"] == "accepted"
    again = surface_for(runs, task, version, controller).handle(
        "POST", INVOCATIONS_PATH, HEADERS, {"idempotency_key": "cloud-case-06"}
    )
    assert again[1]["status"] == "duplicate"
    assert again[1]["result"] == first[1]["result"]
    assert len(decider.calls) == 1
