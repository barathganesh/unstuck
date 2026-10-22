"""The composed Decision Receipt path through the real controller.

Every test here drives `Controller` over the real run repository, the real
DuckDB evidence engine, the real `CascadeEngine`, and the real receipt
repository. Nothing projects or simulates a selector: the controller reaches
the bounded orchestrator, the orchestrator reaches back into the controller for
every diagnostic read, and the controller persists each attempt under its own
lease before the call it pays for.

The Decider and the ordering of its answers are scripted offline, so no
latency, cost, or accuracy figure here is a measurement.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/core_runtime/test_receipt_controller.py -q
"""

import asyncio
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from unstuck.cascade.engine import CascadeEngine
from unstuck.cascade.execution import DiagnosticRequest
from unstuck.cascade.integration import context_for
from unstuck.cascade.matching import HistoryMatcher
from unstuck.cascade.offline import (
    ScriptedChoice,
    ScriptedDecider,
)
from unstuck.cascade.policy import CascadePolicy
from unstuck.cascade.receipts import DecisionReceipt, ReceiptNamespace
from unstuck.cascade.repository import (
    LocalReceiptRepository,
    ReceiptLookupError,
    ReceiptWriteToken,
)
from unstuck.cascade.verifier import ReceiptVerifier
from unstuck.clock import FakeClock
from unstuck.controller import Controller, RunExecution
from unstuck.decision import DecisionState, default_catalog
from unstuck.domain import (
    Counters,
    QuerySpec,
    Reason,
    RuntimeFault,
    Settings,
    TaskSpec,
)
from unstuck.fixtures import Fixture, load_fixture
from unstuck.query import EvidenceEngine
from unstuck.storage import LocalRunRepository, Snapshot

CASE = "case-01"
REVISION = "scripted-v1"
SCHEMA = "inspect_schema_keys"
PIPELINE = "inspect_pipeline"
SCOPE = "inspect_scope"


def policy_for(arm: str) -> CascadePolicy:
    return CascadePolicy(baseline_id=arm, model_revision=REVISION)


def fingerprint(fixture: Fixture, arm: str = "decider_memory") -> str:
    """The semantic problem identity, built by the production context builder."""
    task = TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )
    query = QuerySpec(task=task, dataset_version=fixture.dataset_version)
    state = DecisionState(
        task=task,
        query=query,
        task_hash=task.task_hash,
        query_hash=query.query_hash,
        dataset_version=fixture.dataset_version,
        candidates=tuple(probe.public for probe in default_catalog()),
        budgets=Counters(),
        remaining_ms=120000,
    )
    return context_for(state, policy_for(arm)).problem_fingerprint


def receipts_for(
    store: LocalReceiptRepository, arm: str = "decider_memory"
) -> tuple[DecisionReceipt, ...]:
    return store.find(
        ReceiptNamespace(arm=arm, case_id=CASE), fingerprint(load_fixture(), arm)
    )


def outcomes_for(
    store: LocalReceiptRepository, arm: str = "decider_memory"
) -> dict[str, str]:
    return {
        receipt.public_tool_id: receipt.outcome_class
        for receipt in receipts_for(store, arm)
    }


@dataclass
class Composed:
    """One fully composed call: controller, storage, and scripted selector."""

    controller: Controller
    runs: LocalRunRepository
    run: Snapshot
    decider: ScriptedDecider
    receipts: LocalReceiptRepository
    cascade: CascadeEngine

    @property
    def run_id(self) -> UUID:
        return self.run.state.run_id

    def execute(self) -> dict[str, object]:
        return asyncio.run(self.controller.run(self.run_id))

    def final(self) -> Snapshot:
        return self.runs.load_run(self.run_id)

    def types(self) -> list[str]:
        return [event.type for event in self.final().events]

    def payload(self, name: str) -> dict[str, object]:
        return next(
            event.payload for event in self.final().events if event.type == name
        )


@contextmanager
def compose(
    root: Path,
    *,
    choices: Sequence[ScriptedChoice],
    key: str,
    arm: str = "decider_memory",
    receipts: LocalReceiptRepository | None = None,
    fixture: Fixture | None = None,
    cascade: CascadeEngine | None = None,
    run_key: str | None = None,
    seed: Sequence[str] = (),
) -> Iterator[Composed]:
    """Wire one real controller onto one real bounded orchestrator.

    `seed` names capabilities whose facts the run already holds before any
    decision. A later read of a seeded capability re-observes exactly those
    facts under a fresh record identity and observation time, which is the one
    way a real read admits nothing new and can prove no progress.
    """
    loaded = fixture or load_fixture()
    clock = FakeClock()
    store = LocalReceiptRepository() if receipts is None else receipts
    decider = ScriptedDecider(list(choices))
    # One orchestrator serves successive runs. It holds no run state: each
    # invocation hands it its own execution authority and journal.
    engine = cascade or CascadeEngine(
        decider=decider,
        repository=store,
        verifier=ReceiptVerifier(clock),
        clock=clock,
        policy=policy_for(arm),
        matcher=HistoryMatcher(store),
    )
    runs = LocalRunRepository(root, clock=clock)
    task = TaskSpec(dataset_id=loaded.dataset_id, order_date_utc=loaded.order_date_utc)
    run = runs.create_run(task, loaded.dataset_version, run_key or key)
    evidence = EvidenceEngine(loaded)
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
        )
        # The engine shares the controller's clock only once the controller is
        # built, so seeded facts are observed after that and stay current.
        controller.initial_evidence = tuple(
            evidence.diagnose(capability, run.query) for capability in seed
        )
        yield Composed(controller, runs, run, decider, store, engine)
    finally:
        evidence.close()


def ordered(types: Sequence[str], expected: Sequence[str]) -> None:
    positions = [types.index(name) for name in expected]
    assert positions == sorted(positions), list(zip(expected, positions, strict=True))


def test_contract_the_real_controller_runs_history_then_fresh_D1_then_a_verified_read(
    tmp_path,
):
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        result = composed.execute()
    final = composed.final()
    assert result["status"] == "completed"
    assert result["reason_code"] == "populated_after_repair"
    assert final.incident.counters == Counters(
        report_queries=2,
        diagnostic_reads=1,
        decider_calls=1,
        history_lookups=1,
        repair_attempts=1,
        agent_turns=1,
    )
    types = composed.types()
    ordered(
        types,
        [
            "decision_history_requested",
            "decision_history_returned",
            "decider_first_requested",
            "decider_first_returned",
            "direct_path_selected",
            "probe_started",
            "probe_returned",
            "decision_receipt_written",
            "repair_applied",
            "run_finished",
        ],
    )
    assert "decider_final_requested" not in types
    assert "repeat_blocked" not in types
    assert result["history_status"] == "empty"
    assert result["receipt_ids"] == []
    assert result["pass_count"] == 1 and result["selection_pass"] == 1
    assert result["first_choice"] == result["final_choice"] == SCHEMA
    assert len(composed.decider.requests) == 1
    assert composed.decider.requests[0].pass_index == 1
    assert composed.decider.requests[0].history.status == "empty"
    receipts = receipts_for(composed.receipts)
    assert len(receipts) == 1
    assert receipts[0].outcome_class == "useful_evidence"
    assert receipts[0].public_tool_id == SCHEMA
    written = composed.payload("decision_receipt_written")
    assert written["receipt_id"] == receipts[0].receipt_id
    assert written["outcome_class"] == "useful_evidence"
    assert written["stored"] is True
    assert written["problem_fingerprint"] == fingerprint(load_fixture())


def test_a_no_progress_D1_read_opens_exactly_one_same_call_second_decision(tmp_path):
    """A repeated observation admits nothing, so it is the no-progress case."""
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE), ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        seed=(SCOPE,),
    ) as composed:
        result = composed.execute()
    final = composed.final()
    assert result["status"] == "completed"
    assert result["reason_code"] == "populated_after_repair"
    assert final.incident.counters == Counters(
        report_queries=2,
        diagnostic_reads=2,
        decider_calls=2,
        history_lookups=1,
        repair_attempts=1,
        agent_turns=1,
    )
    types = composed.types()
    assert types.count("decider_first_requested") == 1
    assert types.count("decider_final_requested") == 1
    assert types.count("probe_started") == 2
    assert types.count("decision_receipt_written") == 2
    assert "repeat_blocked" not in types
    ordered(
        types,
        [
            "decider_first_returned",
            "probe_returned",
            "decision_receipt_written",
            "decider_final_requested",
            "decider_final_returned",
        ],
    )
    assert result["pass_count"] == 2 and result["selection_pass"] == 2
    assert result["blocked_tool_id"] == SCOPE
    assert result["blocked_reason_code"] == "insufficient_evidence"
    # Both records survive: the first decision chose the read that made no
    # progress, and the second decision chose the one that resolved it.
    assert result["first_choice"] == SCOPE
    assert result["final_choice"] == SCHEMA
    assert outcomes_for(composed.receipts) == {
        SCOPE: "no_progress",
        SCHEMA: "useful_evidence",
    }
    stalled = next(
        receipt
        for receipt in receipts_for(composed.receipts)
        if receipt.public_tool_id == SCOPE
    )
    # Both halves of the conjunction hold, which is why this one may block.
    assert stalled.delta.evidence_ids == ()
    assert stalled.delta.evidence_before_hash == stalled.delta.evidence_after_hash
    assert stalled.delta.stalled and stalled.blocking


def test_a_third_selection_never_opens_after_two_unhelpful_reads(tmp_path):
    with compose(
        tmp_path,
        choices=[
            ScriptedChoice(selected_id=SCOPE),
            ScriptedChoice(selected_id=PIPELINE),
        ],
        key="call-1",
        seed=(SCOPE, PIPELINE),
    ) as composed:
        result = composed.execute()
    final = composed.final()
    assert result["status"] == "unresolved"
    assert result["reason_code"] == "insufficient_evidence"
    assert final.incident.counters.decider_calls == 2
    assert final.incident.counters.diagnostic_reads == 2
    assert len(composed.decider.requests) == 2
    assert composed.types().count("decision_receipt_written") == 2
    assert set(outcomes_for(composed.receipts).values()) == {"no_progress"}


def test_call_two_precludes_the_exact_repetition_and_chooses_without_replaying(
    tmp_path,
):
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
        cold = first.execute()
    assert cold["status"] == "unresolved"
    assert cold["history_status"] == "empty"
    blocked = next(
        receipt for receipt in receipts_for(receipts) if receipt.public_tool_id == SCOPE
    )
    assert blocked.outcome_class == "no_progress"
    assert blocked.blocking

    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-2",
        receipts=receipts,
        seed=(SCOPE,),
    ) as second:
        result = second.execute()
    final = second.final()
    assert result["status"] == "completed"
    assert result["history_status"] == "matched"
    assert blocked.receipt_id in result["receipt_ids"]
    # The precluded repetition cost no selector call and no diagnostic read.
    assert final.incident.counters.decider_calls == 1
    assert final.incident.counters.diagnostic_reads == 1
    assert result["blocked_tool_id"] == SCOPE
    assert result["blocked_reason_code"] == "insufficient_evidence"
    assert result["selection_pass"] == 1
    assert result["first_choice"] == SCHEMA
    assert result["final_choice"] == SCHEMA
    types = second.types()
    assert types.count("repeat_blocked") == 1
    assert types.count("probe_started") == 1
    refusal = second.payload("repeat_blocked")
    assert refusal["receipt_id"] == blocked.receipt_id
    assert refusal["public_tool_id"] == SCOPE
    assert refusal["blocked_class"] == "exact_repeat"
    # Decider 2B made the one choice itself; nothing was replayed.
    assert len(second.decider.requests) == 1
    assert second.decider.requests[0].history.status == "matched"
    assert blocked.receipt_id in second.decider.requests[0].history.receipt_ids
    assert SCOPE not in second.decider.requests[0].available_tool_ids


def test_current_facts_dominate_history_when_D1_chooses_an_unblocked_read(tmp_path):
    receipts = LocalReceiptRepository()
    with compose(
        tmp_path,
        choices=[
            ScriptedChoice(selected_id=SCOPE),
            ScriptedChoice(selected_id=PIPELINE),
        ],
        key="call-1",
        receipts=receipts,
    ) as first:
        first.execute()
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-2",
        receipts=receipts,
    ) as second:
        result = second.execute()
    final = second.final()
    assert result["status"] == "completed"
    assert result["history_status"] == "matched"
    assert result["selection_pass"] == 1
    # Call 1's exact neutral read of SCOPE is precluded; D1's own choice runs.
    assert result["blocked_tool_id"] == SCOPE
    assert result["first_choice"] == SCHEMA
    assert final.incident.counters.decider_calls == 1
    assert final.incident.counters.diagnostic_reads == 1
    assert second.types().count("repeat_blocked") == 1
    assert second.payload("repeat_blocked")["stage"] == "pre_selection"
    assert len(second.decider.requests) == 1


def test_a_duplicate_run_identity_resumes_from_the_durable_run_record(tmp_path):
    """One shared orchestrator keeps no duplicate state; the store decides."""
    receipts = LocalReceiptRepository()
    clock = FakeClock()
    decider = ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)])
    shared = CascadeEngine(
        decider=decider,
        repository=receipts,
        verifier=ReceiptVerifier(clock),
        clock=clock,
        policy=policy_for("decider_memory"),
        matcher=HistoryMatcher(receipts),
    )

    def call(key: str, run_key: str | None = None) -> Composed:
        return compose(
            tmp_path,
            choices=[ScriptedChoice(selected_id=SCHEMA)],
            key=key,
            receipts=receipts,
            cascade=shared,
            run_key=run_key,
        )

    with call("call-1") as first:
        stored = first.execute()
    assert stored["status"] == "completed"
    assert len(decider.requests) == 1

    # The same run identity is the same work, whatever the operational labels.
    with call("retry-1", run_key="call-1") as duplicate:
        result = duplicate.execute()
    assert result == stored
    assert len(decider.requests) == 1
    assert duplicate.types().count("decider_first_requested") == 1
    assert duplicate.types().count("probe_started") == 1
    assert len(receipts_for(receipts)) == 1

    # A new run identity invokes a fresh first decision.
    with call("call-2") as fresh:
        assert fresh.execute()["status"] == "completed"
    assert len(decider.requests) == 2
    assert decider.requests[1].pass_index == 1
    assert decider.requests[1].history.status == "matched"


def test_a_duplicate_of_a_finished_run_returns_its_stored_result(tmp_path):
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        stored = composed.execute()
        again = asyncio.run(composed.controller.run(composed.run_id))
    assert stored["status"] == "completed"
    assert again == stored
    assert len(composed.decider.requests) == 1


def test_an_unreadable_receipt_store_ends_explicitly_as_history_unavailable(tmp_path):
    class Unreadable(LocalReceiptRepository):
        def find(self, namespace, problem_fingerprint):
            raise ReceiptLookupError("scripted store failure")

    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        receipts=Unreadable(),
    ) as composed:
        result = composed.execute()
    final = composed.final()
    assert result["status"] == "unresolved"
    assert result["reason_code"] == "history_unavailable"
    assert result["history_status"] == "unavailable"
    assert result["receipt_ids"] == []
    assert final.incident.counters.history_lookups == 1
    assert final.incident.counters.decider_calls == 0
    assert final.incident.counters.diagnostic_reads == 0
    assert not composed.decider.requests


def test_the_one_pass_arm_requests_no_history_and_persists_no_receipt_row(tmp_path):
    receipts = LocalReceiptRepository()
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        arm="decider",
        receipts=receipts,
    ) as composed:
        result = composed.execute()
    final = composed.final()
    assert result["status"] == "completed"
    assert result["baseline_id"] == "decider"
    assert result["history_status"] == "not_requested"
    assert final.history is None
    assert final.incident.counters.history_lookups == 0
    types = composed.types()
    assert "decision_history_requested" not in types
    assert types.count("decision_receipt_written") == 1
    assert composed.payload("decision_receipt_written")["stored"] is False
    assert receipts.count(ReceiptNamespace(arm="decider", case_id=CASE)) == 0


def test_a_provider_failure_on_the_selected_read_consumes_its_allowance(tmp_path):
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA), ScriptedChoice(selected_id=SCOPE)],
        key="call-1",
    ) as composed:
        original = composed.controller.engine.diagnose
        attempts: list[str] = []

        def failing(capability: str, query: QuerySpec):
            attempts.append(capability)
            if len(attempts) == 1:
                raise RuntimeFault(Reason.TOOL_ERROR)
            return original(capability, query)

        composed.controller.engine.diagnose = failing  # type: ignore[method-assign]
        result = composed.execute()
    final = composed.final()
    assert final.incident.counters.diagnostic_reads == 2
    assert final.incident.counters.decider_calls == 2
    assert result["blocked_tool_id"] == SCHEMA
    assert result["blocked_reason_code"] == "provider_error_tool"
    assert outcomes_for(composed.receipts)[SCHEMA] == "provider_error"


def test_a_wait_opening_read_is_useful_current_evidence(tmp_path):
    fixture = load_fixture()
    pending = Fixture.model_validate(
        fixture.model_dump()
        | {
            "job_state": "pending",
            "arrival_after_seconds": 3.0,
            "countries_key_encoding": "plain",
            "countries": tuple(
                country.model_dump() | {"country_id": country.country_code}
                for country in fixture.countries
            ),
        }
    )
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=PIPELINE)],
        key="call-1",
        fixture=pending,
    ) as composed:
        result = composed.execute()
    assert result["reason_code"] == "populated_after_wait"
    assert result["wait_checks"] == 1
    assert composed.final().incident.counters.diagnostic_reads == 1
    assert outcomes_for(composed.receipts)[PIPELINE] == "useful_evidence"


def test_security_no_private_capability_or_truth_marker_reaches_a_persisted_event(
    tmp_path,
):
    receipts = LocalReceiptRepository()
    with compose(
        tmp_path,
        choices=[
            ScriptedChoice(selected_id=SCOPE),
            ScriptedChoice(selected_id=PIPELINE),
        ],
        key="call-1",
        receipts=receipts,
    ) as first:
        first.execute()
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE), ScriptedChoice(selected_id=SCHEMA)],
        key="call-2",
        receipts=receipts,
    ) as second:
        result = second.execute()
    rendered = json.dumps(
        {"result": result, "snapshot": json.loads(second.final().model_dump_json())}
    )
    assert "selected_capability" not in rendered
    for term in (
        "jev",
        "bma",
        "jarvis",
        "hobson",
        "claude",
        "assistant",
        "expected_rows",
        "expected_action",
        "evaluator",
        "authorization",
    ):
        assert term not in rendered.lower()


def controller_for(
    runs: LocalRunRepository, evidence: EvidenceEngine, clock: FakeClock, **changes
) -> Callable[[], Controller]:
    cascade = CascadeEngine(
        decider=ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)]),
        repository=LocalReceiptRepository(),
        verifier=ReceiptVerifier(clock),
        clock=clock,
        policy=policy_for("decider"),
    )
    arguments = {
        "cascade": cascade,
        "namespace": ReceiptNamespace(arm="decider", case_id=CASE),
        "idempotency_key": "unbound",
    } | changes
    return lambda: Controller(runs, evidence, clock=clock, **arguments)


def test_a_composition_without_one_bound_authority_is_refused(tmp_path):
    fixture = load_fixture()
    clock = FakeClock()
    runs = LocalRunRepository(tmp_path, clock=clock)
    evidence = EvidenceEngine(fixture)
    try:
        for changes in (
            {"idempotency_key": None},
            {"namespace": ReceiptNamespace(arm="decider_memory", case_id=CASE)},
            {"receipts": LocalReceiptRepository()},
            {"decider": ScriptedDecider([])},
        ):
            build = controller_for(runs, evidence, clock, **changes)
            with pytest.raises(RuntimeFault) as error:
                build()
            assert error.value.reason is Reason.INVARIANT
        composed = controller_for(runs, evidence, clock)()
        assert isinstance(composed.cascade, CascadeEngine)
        with pytest.raises(RuntimeFault) as error:
            Controller(runs, evidence, clock=clock)
        assert error.value.reason is Reason.INVARIANT
        # The execution authority exists only as one run's, never unbound.
        authority = RunExecution(composed)
        with pytest.raises(RuntimeFault) as error:
            asyncio.run(
                authority.execute(
                    DiagnosticRequest(
                        diagnostic_call_id=uuid4(),
                        pass_index=1,
                        capability=SCHEMA,
                        public_tool_id=SCHEMA,
                        arguments_hash="0" * 64,
                    )
                )
            )
        assert error.value.reason is Reason.INVARIANT
    finally:
        evidence.close()


def test_the_write_token_stays_with_the_verifier_on_the_composed_path(tmp_path):
    receipts = LocalReceiptRepository()
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        receipts=receipts,
    ) as composed:
        composed.execute()
    receipt = receipts_for(receipts)[0]
    with pytest.raises(RuntimeFault) as error:
        receipts.put(receipt, ReceiptWriteToken(object()))
    assert error.value.reason is Reason.RECEIPT_INVALID


def test_an_admitted_read_that_moves_no_disposition_is_refusable(tmp_path):
    """FR5.4 through the real evidence path, not the scenario harness.

    The read below admits a scope fact the run did not hold and opens no
    supported disposition at all. Both dimensions are proven separately, so
    the receipt is neither the strict no-progress class nor useful evidence:
    it is the disposition-neutral class, which is eligible to refuse the
    identical repetition under an identical later context.

    This is the one case a real controller reaches on every first execution,
    and the case the collapsed single-dimension taxonomy could not express.
    """
    incomplete = Fixture.model_validate(
        load_fixture().model_dump() | {"metadata_complete": False}
    )
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE)],
        key="call-1",
        fixture=incomplete,
    ) as composed:
        result = composed.execute()
    assert result["status"] == "unresolved"
    assert composed.final().incident.counters.diagnostic_reads == 1
    receipt = receipts_for(composed.receipts)[0]
    assert receipt.outcome_class == "disposition_unchanged"
    assert receipt.reason_code is Reason.INSUFFICIENT
    # The admitted fact is named, the unmoved gate is proven separately, and
    # neither claim is collapsed into the other.
    assert receipt.delta.changed is True
    assert receipt.delta.progressed is False
    assert receipt.delta.disposition_before_hash == receipt.delta.disposition_after_hash
    # Not the strict no-progress conjunction, which is the point: real
    # evidence was admitted, and the decision still did not move.
    assert receipt.delta.stalled is False
    assert receipt.delta.neutral is True
    assert receipt.blocking is True
    assert [str(item) for item in receipt.delta.evidence_ids] == [
        str(composed.payload("probe_returned")["evidence_id"])
    ]
    assert composed.payload("decision_receipt_written")["blocking"] is True


def test_a_duplicate_observation_is_zero_new_evidence_despite_a_fresh_record(tmp_path):
    """Operational identity is not fact identity, so the repeat still blocks."""
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE)],
        key="call-1",
        seed=(SCOPE,),
    ) as composed:
        # Time moves between the two observations of the same facts.
        composed.controller.clock.advance(1.0)
        composed.execute()
        seeded = composed.controller.initial_evidence[0]
    repeated = next(
        record
        for record in composed.final().evidence
        if record.kind == "scope" and record.evidence_id != seeded.evidence_id
    )
    # The provider answered with its own record identity and its own clock.
    assert repeated.evidence_id != seeded.evidence_id
    assert repeated.observed_at != seeded.observed_at
    # Everything the canonical fact is made of is identical.
    assert repeated.payload == seeded.payload
    assert (repeated.kind, repeated.arguments_hash, repeated.dataset_version) == (
        seeded.kind,
        seeded.arguments_hash,
        seeded.dataset_version,
    )
    receipt = receipts_for(composed.receipts)[0]
    assert receipt.delta.evidence_before_hash == receipt.delta.evidence_after_hash
    assert receipt.delta.evidence_ids == ()
    assert receipt.outcome_class == "no_progress" and receipt.blocking


def test_an_unrepairable_incompatible_schema_read_admits_a_fact_and_opens_nothing(
    tmp_path,
):
    unrepairable = Fixture.model_validate(
        load_fixture().model_dump() | {"approved_join_modes": ("literal",)}
    )
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        fixture=unrepairable,
    ) as composed:
        result = composed.execute()
    assert result["status"] == "unresolved"
    assert outcomes_for(composed.receipts)[SCHEMA] == "disposition_unchanged"
    # Eligibility to refuse a repetition is not authority to repair.
    assert composed.final().incident.counters.repair_attempts == 0
    receipt = receipts_for(composed.receipts)[0]
    assert receipt.delta.changed and not receipt.delta.progressed
    assert receipt.blocking


def test_a_read_whose_evidence_conflicts_is_proven_invalid_evidence(tmp_path):
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA), ScriptedChoice(selected_id=SCOPE)],
        key="call-1",
    ) as composed:
        original = composed.controller.engine.diagnose
        attempts: list[str] = []

        def drifting(capability: str, query: QuerySpec):
            attempts.append(capability)
            if len(attempts) == 1:
                composed.controller.engine.fixture = Fixture.model_validate(
                    composed.controller.engine.fixture.model_dump()
                    | {"dataset_version": "v99"}
                )
            return original(capability, query)

        composed.controller.engine.diagnose = drifting  # type: ignore[method-assign]
        result = composed.execute()
    assert result["status"] == "unresolved"
    assert outcomes_for(composed.receipts)[SCHEMA] == "invalid_evidence"
    assert result["blocked_tool_id"] == SCHEMA
    assert result["blocked_reason_code"] == "evidence_conflict"


def test_a_run_deadline_during_a_read_never_becomes_a_receipt(tmp_path):
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        original = composed.controller.engine.diagnose

        def slow(capability: str, query: QuerySpec):
            record = original(capability, query)
            composed.controller.clock.advance(121.0)
            return record

        composed.controller.engine.diagnose = slow  # type: ignore[method-assign]
        result = composed.execute()
    assert result["status"] == "budget_exhausted"
    assert result["reason_code"] == "run_deadline"
    assert composed.final().incident.counters.diagnostic_reads == 1
    # A read the deadline cut short is counted but never verified.
    assert receipts_for(composed.receipts) == ()
    assert "decision_receipt_written" not in composed.types()


def test_the_read_seam_refuses_a_tool_or_argument_set_it_did_not_publish(tmp_path):
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        assert composed.controller.begin(composed.run_id)
        query_hash = composed.controller.current.query.query_hash
        for changes in (
            {"public_tool_id": "inspect_future"},
            {"capability": SCOPE},
            {"arguments_hash": "0" * 64},
        ):
            request = DiagnosticRequest.model_validate(
                {
                    "diagnostic_call_id": uuid4(),
                    "pass_index": 1,
                    "capability": SCHEMA,
                    "public_tool_id": SCHEMA,
                    "arguments_hash": query_hash,
                }
                | changes
            )
            with pytest.raises(RuntimeFault) as error:
                composed.controller.execute_diagnostic(request)
            assert error.value.reason is Reason.CONFLICT
        assert composed.final().incident.counters.diagnostic_reads == 0


def test_a_read_contradicting_admitted_evidence_is_proven_invalid_evidence(tmp_path):
    with compose(
        tmp_path,
        choices=[
            ScriptedChoice(selected_id=PIPELINE),
            ScriptedChoice(selected_id=SCHEMA),
        ],
        key="call-1",
        seed=(PIPELINE,),
    ) as composed:
        original = composed.controller.engine.diagnose

        def drifting(capability: str, query: QuerySpec):
            if capability == SCHEMA:
                # The partition completeness the pipeline read already proved
                # moves underneath the second read.
                composed.controller.engine.fixture = Fixture.model_validate(
                    composed.controller.engine.fixture.model_dump()
                    | {"scope_complete": False}
                )
            return original(capability, query)

        composed.controller.engine.diagnose = drifting  # type: ignore[method-assign]
        result = composed.execute()
    final = composed.final()
    assert result["status"] == "unresolved"
    assert result["reason_code"] == "evidence_conflict"
    assert final.incident.counters.diagnostic_reads == 2
    assert outcomes_for(composed.receipts) == {
        PIPELINE: "no_progress",
        SCHEMA: "invalid_evidence",
    }
    assert final.incident.counters.repair_attempts == 0


def test_call_two_precludes_the_exact_dead_end_before_any_selector_request(tmp_path):
    """Run 2 removes the proven dead end first; D1 is the one and only choice."""
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
        first.execute()
    blocked = next(
        receipt for receipt in receipts_for(receipts) if receipt.public_tool_id == SCOPE
    )
    assert blocked.blocking

    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-2",
        receipts=receipts,
        seed=(SCOPE,),
    ) as second:
        result = second.execute()
    final = second.final()
    assert result["status"] == "completed"
    assert result["reason_code"] == "populated_after_repair"
    assert final.incident.counters.decider_calls == 1
    assert final.incident.counters.diagnostic_reads == 1
    assert result["selection_pass"] == 1
    assert result["first_choice"] == result["final_choice"] == SCHEMA
    # The terminal record still names the refused dead end and its reason.
    assert result["blocked_tool_id"] == SCOPE
    assert result["blocked_reason_code"] == "insufficient_evidence"
    types = second.types()
    assert types.count("repeat_blocked") == 1
    assert "decider_final_requested" not in types
    ordered(
        types,
        [
            "decision_history_returned",
            "repeat_blocked",
            "decider_first_requested",
            "decider_first_returned",
            "probe_started",
            "decision_receipt_written",
            "run_finished",
        ],
    )
    refusal = second.payload("repeat_blocked")
    assert refusal["receipt_id"] == blocked.receipt_id
    assert refusal["public_tool_id"] == SCOPE
    assert refusal["blocked_class"] == "exact_repeat"
    assert refusal["stage"] == "pre_selection"
    requested = second.payload("decider_first_requested")
    assert SCOPE not in requested["available_tool_ids"]
    assert requested["precluded_tool_ids"] == [SCOPE]
    assert requested["remaining_selector_calls"] == 2
    assert len(second.decider.requests) == 1
    offered = second.decider.requests[0]
    assert SCOPE not in offered.available_tool_ids
    assert offered.decision_context_hash == blocked.decision_context_hash
    assert [item.receipt_id for item in offered.precluded] == [blocked.receipt_id]


def test_without_a_blocker_the_first_request_payload_is_unchanged(tmp_path):
    with compose(
        tmp_path, choices=[ScriptedChoice(selected_id=SCHEMA)], key="call-1"
    ) as composed:
        composed.execute()
    assert "precluded_tool_ids" not in composed.payload("decider_first_requested")
    assert "repeat_blocked" not in composed.types()
