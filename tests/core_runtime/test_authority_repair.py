"""One bounded selection authority, refreshed second decisions, truthful deltas.

Every test here drives the real controller over the real run repository, the
real DuckDB evidence engine, the real bounded orchestrator, and the real
receipt repository. Nothing simulates a selector or a store, so a second
authority path, a stale second decision, a false no-progress claim, a
process-local duplicate cache, a collapsed selection record, a shared mutable
run binding, or a swallowed storage failure is visible here as a failure.

The Decider and the ordering of its answers are scripted offline, so no
latency, cost, or accuracy figure here is a measurement.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/core_runtime/test_authority_repair.py -q
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from test_receipt_controller import (
    PIPELINE,
    SCHEMA,
    SCOPE,
    compose,
    policy_for,
    receipts_for,
)

from unstuck.cascade.engine import CascadeEngine
from unstuck.cascade.matching import HistoryMatcher
from unstuck.cascade.offline import ScriptedChoice, ScriptedDecider
from unstuck.cascade.receipts import ReceiptNamespace
from unstuck.cascade.repository import LocalReceiptRepository
from unstuck.cascade.selection import RawSelection
from unstuck.cascade.verifier import ReceiptVerifier
from unstuck.clock import FakeClock
from unstuck.controller import Controller, CoreSelector, RunExecution
from unstuck.decision import DecisionState, OptionProbability, default_catalog
from unstuck.domain import (
    EvidenceRecord,
    Incident,
    Reason,
    RuntimeFault,
    Settings,
    TaskSpec,
)
from unstuck.fixtures import load_fixture
from unstuck.query import EvidenceEngine
from unstuck.storage import LocalRunRepository, Snapshot

CASE = "case-01"


def payload_state(composed) -> DecisionState:
    """The authoritative state the controller projects for its own run."""
    return composed.controller.decision_state()


def task_for(fixture) -> TaskSpec:
    return TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )


# Defect 1: one bounded cascade algorithm behind every active composition.


def test_the_controller_exposes_exactly_one_selection_algorithm():
    assert not hasattr(Controller, "_select_once")
    assert not hasattr(Controller, "_lookup_history")
    assert not hasattr(Controller, "decider")


def test_every_active_entry_point_runs_the_bounded_cascade(tmp_path):
    """The CLI, the operator service, and the runtime all reach the cascade."""
    from unstuck.cli import skeleton
    from unstuck.operator.app import OfflineRunService
    from unstuck.operator.contracts import RunRequest

    cli = asyncio.run(skeleton(tmp_path / "cli"))
    assert cli["status"] == "completed"
    cli_events = (tmp_path / "cli" / str(cli["run_id"]) / "events.jsonl").read_text()
    assert "decision_receipt_written" in cli_events

    clock = FakeClock()
    runs = LocalRunRepository(tmp_path / "operator", clock=clock)
    service = OfflineRunService(runs, clock)
    request = RunRequest(scenario="repairable-join", idempotency_key="operator-01")
    snapshot = asyncio.run(service.start(request))
    assert snapshot.state.status == "completed"
    assert "decision_receipt_written" in [event.type for event in snapshot.events]


def test_the_composed_controller_opens_a_second_decision_on_the_active_path(tmp_path):
    """An unproductive first read opens exactly one second decision."""
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE), ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        arm="decider",
        seed=(SCOPE,),
    ) as composed:
        result = composed.execute()
    assert result["status"] == "completed"
    assert result["pass_count"] == 2
    assert result["history_status"] == "not_requested"
    assert composed.final().incident.counters.history_lookups == 0
    assert len(composed.decider.requests) == 2


# Defect 2: the second decision reasons over refreshed authoritative state.


def test_the_second_decision_receives_the_post_first_read_state_and_record(tmp_path):
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE), ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        seed=(SCOPE,),
    ) as composed:
        assert composed.execute()["status"] == "completed"
    first, final = composed.decider.requests
    probe = composed.payload("probe_returned")
    admitted = str(probe["evidence_id"])
    observed = tuple(item.evidence_id for item in final.observations)
    assert admitted not in tuple(item.evidence_id for item in first.observations)
    assert admitted in observed
    # The refreshed context identifies the post-first-read state.
    assert final.decision_context_hash != first.decision_context_hash
    assert SCOPE not in final.available_tool_ids
    # The first record travels with the second request; it is not replayed.
    assert final.first is not None
    assert final.first.selected_id == SCOPE
    assert final.first.accepted is True
    assert final.first.decision_context_hash == first.decision_context_hash
    assert final.blocked[0].public_tool_id == SCOPE
    receipts = {
        receipt.public_tool_id: receipt for receipt in receipts_for(composed.receipts)
    }
    # Each pass binds its own pre-decision context.
    assert receipts[SCOPE].decision_context_hash == first.decision_context_hash
    assert receipts[SCHEMA].decision_context_hash == final.decision_context_hash


def test_the_recorded_events_carry_both_decision_context_hashes(tmp_path):
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE), ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        seed=(SCOPE,),
    ) as composed:
        composed.execute()
    opened = composed.payload("decider_first_requested")["decision_context_hash"]
    closed = composed.payload("decider_final_requested")["decision_context_hash"]
    assert opened != closed
    written = [
        event.payload["decision_context_hash"]
        for event in composed.final().events
        if event.type == "decision_receipt_written"
    ]
    assert written == [opened, closed]


# Defect 3: admissible evidence can never become a blocking no-progress claim.


def test_a_no_progress_receipt_names_no_evidence_because_it_admitted_none(tmp_path):
    """No progress is both negatives, and it names nothing because of it.

    The run already holds this capability's facts, so the read re-observes them
    under a fresh record identity and observation time. The canonical evidence
    set does not move, no gate moves, and only then is the claim no progress.
    """
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE)],
        key="call-1",
        seed=(SCOPE,),
    ) as composed:
        result = composed.execute()
    assert result["status"] == "unresolved"
    receipt = receipts_for(composed.receipts)[0]
    assert receipt.outcome_class == "no_progress"
    # The provider minted a fresh operational record, which is not a new fact.
    observed = str(composed.payload("probe_returned")["evidence_id"])
    assert observed not in {str(item) for item in receipt.delta.evidence_ids}
    assert receipt.delta.evidence_ids == ()
    assert receipt.delta.evidence_before_hash == receipt.delta.evidence_after_hash
    assert receipt.delta.changed is False
    # The disposition claim is proven separately and agrees.
    assert receipt.delta.disposition_before_hash == receipt.delta.disposition_after_hash
    assert receipt.delta.progressed is False
    assert receipt.delta.stalled is True
    assert receipt.blocking is True
    written = composed.payload("decision_receipt_written")
    assert written["admitted_evidence"] is False
    assert written["evidence_ids"] == []
    assert written["blocking"] is True


def test_the_evidence_delta_is_the_admissible_evidence_not_the_disposition(tmp_path):
    """A useful read proves both deltas; neither is derived from the other."""
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        assert composed.execute()["status"] == "completed"
    receipt = receipts_for(composed.receipts)[0]
    assert receipt.outcome_class == "useful_evidence"
    assert receipt.delta.changed and receipt.delta.progressed
    assert receipt.delta.evidence_ids
    assert receipt.delta.disposition_before_hash != receipt.delta.disposition_after_hash
    assert receipt.delta.evidence_before_hash != receipt.delta.disposition_before_hash


def test_a_failed_read_admits_nothing_and_claims_nothing(tmp_path):
    with compose(
        tmp_path,
        choices=[
            ScriptedChoice(selected_id=SCHEMA),
            ScriptedChoice(selected_id=PIPELINE),
        ],
        key="call-1",
    ) as composed:
        original = composed.controller.engine.diagnose
        attempts: list[str] = []

        def failing(capability: str, query):
            attempts.append(capability)
            if len(attempts) == 1:
                raise RuntimeFault(Reason.TOOL_ERROR)
            return original(capability, query)

        composed.controller.engine.diagnose = failing  # type: ignore[method-assign]
        composed.execute()
    failed = next(
        receipt
        for receipt in receipts_for(composed.receipts)
        if receipt.public_tool_id == SCHEMA
    )
    assert failed.outcome_class == "provider_error"
    assert failed.delta.changed is False and failed.delta.progressed is False
    assert failed.delta.evidence_ids == ()
    assert failed.blocking is False


# Defect 4: the run repository is the only durable idempotency authority.


def test_the_bounded_orchestrator_holds_no_duplicate_cache():
    assert not hasattr(CascadeEngine, "_ledger")
    cascade = CascadeEngine(
        decider=ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)]),
        repository=LocalReceiptRepository(),
        verifier=ReceiptVerifier(FakeClock()),
        clock=FakeClock(),
        policy=policy_for("decider"),
    )
    assert not hasattr(cascade, "executor")
    assert "_ledger" not in vars(cascade)


def test_a_completed_duplicate_returns_its_stored_terminal_result(tmp_path):
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        first = composed.execute()
        again = asyncio.run(composed.controller.run(composed.run_id))
    assert first["status"] == "completed"
    assert again == first
    assert len(composed.decider.requests) == 1
    assert composed.types().count("run_finished") == 1


def test_a_duplicate_whose_record_cannot_be_read_is_refused(tmp_path):
    """An unreadable record is never an answer, and never an execution."""
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        assert composed.execute()["status"] == "completed"
        unreadable = composed.controller.repository

        def refuse(run_id):
            raise RuntimeFault(Reason.STORAGE_ERROR)

        unreadable.load_run = refuse  # type: ignore[method-assign]
        again = asyncio.run(composed.controller.run(composed.run_id))
    assert again == {"run_id": str(composed.run_id), "status": "lease_rejected"}
    assert len(composed.decider.requests) == 1


def test_a_duplicate_of_a_live_lease_never_executes_again(tmp_path):
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        other = uuid4()
        assert composed.runs.acquire(composed.run_id, other) is not None
        refused = asyncio.run(composed.controller.run(composed.run_id))
    assert refused == {"run_id": str(composed.run_id), "status": "lease_rejected"}
    assert not composed.decider.requests


def test_a_fresh_process_resolves_a_completed_duplicate_from_storage(tmp_path):
    """Durable idempotency survives a composition with no process state."""
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        stored = composed.execute()
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as replacement:
        again = asyncio.run(replacement.controller.run(replacement.run_id))
    assert stored["status"] == "completed"
    assert again == stored
    assert not replacement.decider.requests


# Defect 5: the first and final selection records stay separate.


def test_the_terminal_result_keeps_the_first_and_the_final_choice(tmp_path):
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE), ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        seed=(SCOPE,),
    ) as composed:
        result = composed.execute()
    final = composed.final()
    assert result["first_choice"] == SCOPE
    assert result["final_choice"] == SCHEMA
    assert result["selection_pass"] == 2
    assert final.first_selection is not None and final.selection is not None
    assert final.first_selection.pass_index == 1
    assert final.first_selection.selected_id == SCOPE
    assert final.selection.pass_index == 2
    assert final.selection.selected_id == SCHEMA
    # The first record is immutable: the second decision never replaces it.
    assert final.first_selection.decision_id != final.selection.decision_id
    returned = [
        event.payload["selected_id"]
        for event in final.events
        if event.type in {"decider_first_returned", "decider_final_returned"}
    ]
    assert returned == [SCOPE, SCHEMA]


def test_a_single_pass_run_reports_the_same_choice_twice(tmp_path):
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        result = composed.execute()
    final = composed.final()
    assert result["first_choice"] == result["final_choice"] == SCHEMA
    assert final.first_selection == final.selection


# Defect 6: run execution state never crosses concurrent runs.


class BarrierDecider:
    """Holds every first decision until both concurrent runs have arrived."""

    def __init__(self, parties: int, selected: str = SCHEMA):
        self.barrier = asyncio.Barrier(parties)
        self.selected = selected
        self.requests: list[object] = []

    async def select(self, request) -> RawSelection:
        self.requests.append(request)
        if request.pass_index == 1:
            await self.barrier.wait()
        options = request.option_ids
        return RawSelection(
            model_revision="scripted-v1",
            decision_id=uuid4(),
            model_call_id=uuid4(),
            pass_index=request.pass_index,
            candidate_ids=options,
            selected_id=self.selected,
            confidence=0.95,
            probabilities=tuple(
                OptionProbability(
                    id=identifier, probability=float(identifier == self.selected)
                )
                for identifier in options
            ),
            validation_status="valid",
        )


def test_two_concurrent_runs_keep_their_own_execution_authority(tmp_path):
    fixture = load_fixture()
    clock = FakeClock()
    runs = LocalRunRepository(tmp_path, clock=clock)
    decider = BarrierDecider(2)
    receipts = LocalReceiptRepository()
    # One shared orchestrator serves both runs at the same time.
    shared = CascadeEngine(
        decider=decider,
        repository=receipts,
        verifier=ReceiptVerifier(clock),
        clock=clock,
        policy=policy_for("decider_memory"),
        matcher=HistoryMatcher(receipts),
    )
    task = task_for(fixture)
    engines = [EvidenceEngine(fixture) for _ in range(2)]
    try:
        cases = []
        for index, evidence in enumerate(engines):
            namespace = ReceiptNamespace(arm="decider_memory", case_id=f"case-0{index}")
            snapshot = runs.create_run(task, fixture.dataset_version, f"run-{index}")
            cases.append(
                (
                    snapshot.state.run_id,
                    namespace,
                    Controller(
                        runs,
                        evidence,
                        clock=clock,
                        catalog=default_catalog(),
                        settings=Settings(baseline_id="decider_memory"),
                        cascade=shared,
                        namespace=namespace,
                        idempotency_key=f"run-{index}",
                    ),
                )
            )

        async def both():
            return await asyncio.gather(
                *(controller.run(run_id) for run_id, _, controller in cases)
            )

        results = asyncio.run(both())
    finally:
        for evidence in engines:
            evidence.close()
    assert [result["status"] for result in results] == ["completed", "completed"]
    assert len(decider.requests) == 2
    owners = set()
    for (run_id, namespace, _), result in zip(cases, results, strict=True):
        assert result["run_id"] == str(run_id)
        final = runs.load_run(run_id)
        assert final.state.status == "completed"
        assert {event.run_id for event in final.events} == {run_id}
        assert [event.type for event in final.events].count("probe_returned") == 1
        assert receipts.count(namespace) == 1
        owners.add(final.state.lease_owner)
    assert len(owners) == 2


def test_one_controller_refuses_a_second_invocation_of_a_lease_it_holds(tmp_path):
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        controller = composed.controller
        assert controller.begin(composed.run_id)
        refused = asyncio.run(controller.run(composed.run_id))
    assert refused == {"run_id": str(composed.run_id), "status": "lease_rejected"}


def test_one_controller_serves_one_invocation_at_a_time(tmp_path):
    """A controller's writer authority belongs to the run it is serving."""
    fixture = load_fixture()
    clock = FakeClock()
    runs = LocalRunRepository(tmp_path, clock=clock)
    receipts = LocalReceiptRepository()
    reentered: list[dict[str, object]] = []

    class Reentrant(ScriptedDecider):
        """Re-enters the controller while its own decision is in flight."""

        controller: "Controller | None" = None
        run_id: UUID | None = None

        async def select(self, request):
            if self.controller is not None and self.run_id is not None:
                reentered.append(await self.controller.run(self.run_id))
            return await super().select(request)

    decider = Reentrant([ScriptedChoice(selected_id=SCHEMA)])
    cascade = CascadeEngine(
        decider=decider,
        repository=receipts,
        verifier=ReceiptVerifier(clock),
        clock=clock,
        policy=policy_for("decider"),
        matcher=HistoryMatcher(receipts),
    )
    snapshot = runs.create_run(task_for(fixture), fixture.dataset_version, "reentry-1")
    evidence = EvidenceEngine(fixture)
    try:
        controller = Controller(
            runs,
            evidence,
            clock=clock,
            catalog=default_catalog(),
            cascade=cascade,
            namespace=ReceiptNamespace(arm="decider", case_id=CASE),
            idempotency_key="reentry-1",
        )
        decider.controller = controller
        decider.run_id = snapshot.state.run_id
        result = asyncio.run(controller.run(snapshot.state.run_id))
    finally:
        evidence.close()
    assert result["status"] == "completed"
    assert reentered == [
        {"run_id": str(snapshot.state.run_id), "status": "lease_rejected"}
    ]
    assert len(decider.requests) == 1
    final = runs.load_run(snapshot.state.run_id)
    assert [event.type for event in final.events].count("run_started") == 1
    assert [event.type for event in final.events].count("run_finished") == 1


def test_the_execution_authority_is_bound_to_one_controller(tmp_path):
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        authority = RunExecution(composed.controller)
        assert authority.controller is composed.controller
        with pytest.raises(TypeError):
            RunExecution()  # type: ignore[call-arg]
        with pytest.raises(RuntimeFault) as error:
            authority.project()
    assert error.value.reason is Reason.INVARIANT


# Defect 7: a durable write failure stays a storage failure.


def test_a_refused_durable_write_ends_as_a_storage_failure(tmp_path):
    class Refusing(LocalRunRepository):
        """Refuses the durable write of the first selector attempt."""

        def record(self, snapshot, owner, event_type, payload, **changes):
            if event_type == "decider_first_requested":
                raise RuntimeFault(Reason.STORAGE_ERROR)
            return super().record(snapshot, owner, event_type, payload, **changes)

    fixture = load_fixture()
    clock = FakeClock()
    runs = Refusing(tmp_path, clock=clock)
    receipts = LocalReceiptRepository()
    decider = ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)])
    cascade = CascadeEngine(
        decider=decider,
        repository=receipts,
        verifier=ReceiptVerifier(clock),
        clock=clock,
        policy=policy_for("decider"),
        matcher=HistoryMatcher(receipts),
    )
    snapshot = runs.create_run(task_for(fixture), fixture.dataset_version, "storage-1")
    evidence = EvidenceEngine(fixture)
    try:
        controller = Controller(
            runs,
            evidence,
            clock=clock,
            catalog=default_catalog(),
            settings=Settings(baseline_id="decider"),
            cascade=cascade,
            namespace=ReceiptNamespace(arm="decider", case_id=CASE),
            idempotency_key="storage-1",
        )
        result = asyncio.run(controller.run(snapshot.state.run_id))
    finally:
        evidence.close()
    assert result == {
        "run_id": str(snapshot.state.run_id),
        "status": "failed",
        "reason_code": "storage_error",
        "mode": "offline",
        "provider": "mock",
        "persistence": "unavailable",
    }
    assert not decider.requests
    assert receipts.count(ReceiptNamespace(arm="decider", case_id=CASE)) == 0


def test_an_initial_evidence_snapshot_still_requires_a_cascade(tmp_path):
    """Composition stays explicit: no authority means no controller."""
    fixture = load_fixture()
    clock = FakeClock()
    runs = LocalRunRepository(tmp_path, clock=clock)
    evidence = EvidenceEngine(fixture)
    try:
        with pytest.raises(RuntimeFault) as error:
            Controller(runs, evidence, clock=clock)
        assert error.value.reason is Reason.INVARIANT
    finally:
        evidence.close()


def test_the_snapshot_refuses_a_second_pass_without_a_first_record(tmp_path):
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE), ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        seed=(SCOPE,),
    ) as composed:
        composed.execute()
    final = composed.final()
    with pytest.raises(ValueError, match="first selection"):
        Snapshot.model_validate(final.model_dump() | {"first_selection": None})
    assert isinstance(final.incident, Incident)
    assert all(isinstance(record, EvidenceRecord) for record in final.evidence)


def test_the_core_selector_adapter_adds_no_authority_of_its_own(tmp_path):
    """The adapter carries one question and one answer, and nothing else."""
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        assert composed.execute()["status"] == "completed"
        payload = composed.decider.requests[0]
        projected = payload_state(composed)
        offered = DecisionState.model_validate(
            projected.model_dump()
            | {"candidates": tuple(probe.public for probe in default_catalog())}
        )
        narrowed = DecisionState.model_validate(
            projected.model_dump() | {"candidates": (default_catalog()[0].public,)}
        )

    class Projects:
        """Projects whatever the case declares as the current state."""

        def __init__(self, state: DecisionState):
            self.state = state

        def project(self) -> DecisionState:
            return self.state

    class Refuses:
        """Answers with something that is not a decision at all."""

        async def select(self, request: DecisionState) -> object:
            return {"selected_id": SCHEMA}

    # An authority that no longer offers what the orchestrator asked about.
    with pytest.raises(RuntimeFault) as error:
        asyncio.run(
            CoreSelector(Refuses(), Projects(narrowed), "scripted-v1").select(payload)  # type: ignore[arg-type]
        )
    assert error.value.reason is Reason.CONFLICT
    # An answer the core contract does not accept is a rejected selection.
    with pytest.raises(RuntimeFault) as error:
        asyncio.run(
            CoreSelector(Refuses(), Projects(offered), "scripted-v1").select(payload)  # type: ignore[arg-type]
        )
    assert error.value.reason is Reason.FIRST_REJECTED
