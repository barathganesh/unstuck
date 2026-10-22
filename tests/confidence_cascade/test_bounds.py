"""Step 12: budgets, failure bounds, leakage, and core integration.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_bounds.py -q
"""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_same_call import Composed, call_identity, engine, request, run

from unstuck.cascade.engine import CASCADE_REASONS, CascadeEngine, CascadeRequest
from unstuck.cascade.integration import (
    core_measurement,
    decision_history,
    selection_authority,
)
from unstuck.cascade.matching import HistoryLookupResult, HistoryMatcher
from unstuck.cascade.offline import (
    ScriptedChoice,
    ScriptedDecider,
    ScriptedExecutor,
    ScriptedOutcome,
)
from unstuck.cascade.policy import CascadePolicy
from unstuck.cascade.repository import LocalReceiptRepository, ReceiptPage
from unstuck.cascade.serialization import (
    MAX_DIAGNOSTIC_READS,
    MAX_SELECTOR_CALLS,
    SELECTOR_INPUT_MAX_CHARS,
    StageMeasurement,
)
from unstuck.clock import FakeClock
from unstuck.decision import DecisionHistory, SelectionAuthority
from unstuck.domain import Counters, Reason, RuntimeFault
from unstuck.events import Measurement

STALL = ScriptedOutcome(changed=False)
FORBIDDEN_TERMS = (
    "jev",
    "bma",
    "jarvis",
    "hobson",
    "claude",
    "assistant",
    "synthetic_person",
    "boto3",
    "requests.get",
)


def worker_engine(policy, repository, verifier, clock, decider, executor, matcher=None):
    return Composed(
        decider=decider,
        executor=executor,
        repository=repository,
        verifier=verifier,
        clock=clock,
        policy=policy,
        matcher=matcher or HistoryMatcher(repository),
    )


def two_pass_decider():
    return ScriptedDecider(
        [
            ScriptedChoice(selected_id="inspect_schema_keys"),
            ScriptedChoice(selected_id="inspect_pipeline"),
        ]
    )


def test_bounds_two_selector_calls_and_two_reads_are_the_hard_maximum(
    repository, verifier, clock, state, policy
):
    assert (MAX_SELECTOR_CALLS, MAX_DIAGNOSTIC_READS) == (2, 2)
    decider = two_pass_decider()
    executor = ScriptedExecutor(default=STALL)
    result = run(policy, repository, verifier, clock, decider, executor, request(state))
    assert result.consumed.decider_calls == MAX_SELECTOR_CALLS
    assert result.consumed.diagnostic_reads == MAX_DIAGNOSTIC_READS
    assert len(decider.requests) == 2 and len(executor.requests) == 2
    # Budgets handed to each pass count down, never up.
    assert [
        payload.budgets.remaining_selector_calls for payload in decider.requests
    ] == [2, 1]
    assert [
        payload.budgets.remaining_diagnostic_reads for payload in decider.requests
    ] == [2, 1]
    assert result.path == "unresolved"
    assert result.unresolved_reason in CASCADE_REASONS


def test_bounds_the_cascade_never_consumes_a_controller_allowance(
    repository, verifier, clock, state, policy
):
    decider = two_pass_decider()
    executor = ScriptedExecutor(default=STALL)
    result = run(policy, repository, verifier, clock, decider, executor, request(state))
    assert result.consumed == Counters(
        decider_calls=2, diagnostic_reads=2, history_lookups=1
    )
    for name in ("wait_checks", "report_queries", "repair_attempts", "agent_turns"):
        assert getattr(result.consumed, name) == 0


def test_bounds_a_blocked_call_uses_zero_reads(
    repository, verifier, clock, state, policy
):
    cold = run(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")]),
        ScriptedExecutor(default=STALL),
        request(state, key="call-1"),
    )
    assert cold.consumed.diagnostic_reads == 1
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_pipeline")])
    executor = ScriptedExecutor()
    second = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, key="call-2"),
    )
    # The precluded repetition cost no selector call and no read.
    assert second.consumed.decider_calls == 1
    assert [item.blocked_class for item in second.precluded] == ["exact_repeat"]
    assert second.consumed.diagnostic_reads == 1
    assert len(executor.requests) == 1
    assert len(second.reports) == 1
    assert len(second.created_receipt_ids) == 1


def test_bounds_an_alias_cannot_reset_a_budget(
    repository, verifier, clock, state, policy
):
    decider = ScriptedDecider(
        [
            ScriptedChoice(selected_id="inspect_schema_keys"),
            # A renamed label is not an available option and not a fresh budget.
            ScriptedChoice(selected_id="probe_schema_keys"),
        ]
    )
    executor = ScriptedExecutor(default=STALL)
    result = run(policy, repository, verifier, clock, decider, executor, request(state))
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.FINAL_REJECTED
    assert result.consumed.decider_calls == 2
    assert result.consumed.diagnostic_reads == 1
    assert len(decider.requests) == 2
    assert "probe_schema_keys" not in decider.requests[1].available_tool_ids


def test_bounds_repeated_invocations_do_not_restore_a_budget(
    make_state, repository, verifier, clock, policy
):
    """The incident's consumed allowances travel with the request state.

    A repeated invocation is bounded by what the incident already spent, not
    by anything this unit remembers: a retry of a question whose allowances
    are gone executes nothing at all, and a new operational identity for the
    same spent incident changes nothing.
    """
    decider = two_pass_decider()
    executor = ScriptedExecutor(default=STALL)
    worker = engine(policy, repository, verifier, clock, decider, executor)
    spent = make_state(budgets=Counters(decider_calls=2, diagnostic_reads=2))

    async def three():
        task = request(spent, key="shared-key")
        return [
            await worker.resolve(task),
            await worker.resolve(request(spent, key="shared-key")),
            await worker.resolve(
                CascadeRequest.model_validate(
                    task.model_dump() | {"call": call_identity(attempt=2)}
                )
            ),
        ]

    results = asyncio.run(three())
    assert all(result.path == "unresolved" for result in results)
    assert all(result.unresolved_reason is Reason.FIRST_REJECTED for result in results)
    assert all(result.consumed == Counters(history_lookups=1) for result in results)
    assert decider.requests == []
    assert executor.requests == []


def test_bounds_whole_run_deadline_precedes_every_provider_call(
    repository, verifier, clock, state, policy
):
    decider = two_pass_decider()
    executor = ScriptedExecutor()
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, remaining_ms=0),
    )
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.DEADLINE
    assert result.consumed.decider_calls == 0
    assert decider.requests == [] and executor.requests == []


def test_bounds_a_late_selector_answer_is_ignored_and_never_retried(
    repository, verifier, state, policy
):
    clock = FakeClock()

    class LateDecider(ScriptedDecider):
        async def select(self, payload):
            clock.advance(25)
            return await super().select(payload)

    decider = LateDecider([ScriptedChoice(selected_id="inspect_schema_keys")])
    executor = ScriptedExecutor()
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, remaining_ms=120000),
    )
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.DECIDER_ERROR
    assert result.consumed.decider_calls == 1
    assert len(decider.requests) == 1
    assert executor.requests == []


def test_bounds_a_mismatched_or_late_diagnostic_result_is_refused(
    repository, verifier, clock, state, policy
):
    class WrongToolExecutor(ScriptedExecutor):
        async def execute(self, payload):
            report = await super().execute(payload)
            return type(report).model_validate(
                report.model_dump()
                | {"capability": "inspect_scope", "public_tool_id": "inspect_scope"}
            )

    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")])
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        WrongToolExecutor(),
        request(state),
    )
    assert result.unresolved_reason is Reason.CONFLICT
    assert result.consumed.diagnostic_reads == 1
    assert result.created_receipt_ids == ()


def test_bounds_the_selector_payload_stays_inside_its_reserved_capacity(
    repository, verifier, clock, make_state, policy
):
    decider = two_pass_decider()
    executor = ScriptedExecutor(default=STALL)
    state = make_state()
    run(policy, repository, verifier, clock, decider, executor, request(state))
    for payload in decider.requests:
        assert len(payload.model_dump_json()) <= SELECTOR_INPUT_MAX_CHARS
        assert payload.question.endswith("Decision Receipts?")
        assert payload.review_option == "request_review"
        assert payload.budgets.remaining_ms <= 120000


def test_security_no_personal_identifier_reaches_cascade_source_or_output(
    repository, verifier, clock, state, policy
):
    root = Path(__file__).parents[2] / "src" / "unstuck" / "cascade"
    sources = sorted(root.rglob("*.py"))
    assert len(sources) >= 12
    for path in sources:
        text = path.read_text().lower()
        for term in FORBIDDEN_TERMS:
            assert term not in text, f"{path} mentions {term}"
    decider = two_pass_decider()
    executor = ScriptedExecutor(default=STALL)
    result = run(policy, repository, verifier, clock, decider, executor, request(state))
    rendered = json.dumps(
        {
            "result": result.model_dump(mode="json"),
            "requests": [
                payload.model_dump(mode="json") for payload in decider.requests
            ],
        }
    ).lower()
    for term in FORBIDDEN_TERMS:
        assert term not in rendered


@pytest.mark.parametrize(
    "marker", ["evaluator_truth", "hidden_cause_b", "authorization_header"]
)
def test_security_no_evaluator_or_credential_value_reaches_a_receipt(
    repository, verifier, clock, state, marker
):
    policy = CascadePolicy(baseline_id="decider_memory", model_revision="scripted-v1")
    task = request(state, namespace={"arm": "decider_memory", "case_id": marker})
    decider = two_pass_decider()
    executor = ScriptedExecutor()
    result = run(policy, repository, verifier, clock, decider, executor, task)
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.RECEIPT_INVALID
    assert result.created_receipt_ids == ()
    assert repository.count(task.namespace) == 0


def test_integration_history_projects_onto_the_core_contract(
    repository,
    verifier,
    clock,
    state,
    policy,
    make_receipt,
    stalled_report,
    namespace,
    context,
):
    receipt = make_receipt(stalled_report)
    repository.put(receipt, verifier.token)
    lookup = HistoryMatcher(repository).lookup(namespace, context, requested=True)
    projected = decision_history(lookup)
    assert isinstance(projected, DecisionHistory)
    assert projected.status == "matched"
    assert projected.receipt_ids == (receipt.receipt_id,)
    assert projected.references[0].outcome_class == "no_progress"
    assert DecisionHistory.model_validate(projected.model_dump()) == projected
    for status in ("not_requested", "empty", "unavailable"):
        plain = decision_history(HistoryLookupResult(status=status))
        assert plain.status == status and plain.references == ()


def test_integration_selection_authority_projects_onto_the_core_contract(
    repository, verifier, clock, state, policy
):
    cold = run(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")]),
        ScriptedExecutor(default=STALL),
        request(state, key="call-1"),
    )
    second = run(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id="inspect_pipeline")]),
        ScriptedExecutor(),
        request(state, key="call-2"),
    )
    authority = selection_authority(second)
    assert isinstance(authority, SelectionAuthority)
    # One fresh decision over the narrowed offer; it names the precluded read.
    assert authority.pass_index == 1
    assert authority.accepted
    assert authority.selected_id == "inspect_pipeline"
    assert authority.history_status == "matched"
    assert authority.receipt_ids == cold.created_receipt_ids
    assert authority.blocked_tool_id == "inspect_schema_keys"
    assert authority.blocked_reason is Reason.INSUFFICIENT
    assert SelectionAuthority.model_validate(authority.model_dump()) == authority
    assert core_measurement(StageMeasurement(availability="invalid")) == Measurement()
    assert core_measurement(
        StageMeasurement(availability="measured", value=2.0)
    ) == Measurement(availability="measured", value=2.0)


def test_integration_the_core_controller_composes_the_whole_orchestrator(tmp_path):
    """Every controller composition is one bounded orchestrator, executably.

    The composed behaviour is proven end to end over the real controller, run
    repository, receipt repository, and ordered calls in
    `tests/core_runtime/test_receipt_controller.py` and
    `tests/core_runtime/test_authority_repair.py`. What this asserts is that
    both compositions reach that one algorithm and that nothing exposes a core
    selection engine which answers with a tool choice while skipping receipt
    admission, exact-repetition blocking, and the second decision.
    """
    import unstuck.cascade as cascade
    from unstuck.cascade import integration
    from unstuck.cli import OfflineDecider
    from unstuck.controller import Controller
    from unstuck.fixtures import load_fixture
    from unstuck.query import EvidenceEngine
    from unstuck.storage import LocalRunRepository

    assert not hasattr(integration, "CascadeDecisionEngine")
    assert "CascadeDecisionEngine" not in cascade.__all__
    assert not [
        name
        for name in dir(integration)
        if name.endswith("DecisionEngine") and not name.startswith("_")
    ]
    runs = LocalRunRepository(tmp_path)
    evidence = EvidenceEngine(load_fixture())
    try:
        # A core selection engine is adapted onto the one orchestrator.
        adapted = Controller(runs, evidence, OfflineDecider())
        assert isinstance(adapted.cascade, CascadeEngine)
        assert adapted.cascade.policy.baseline_id == "decider"
        # There is no second selection algorithm to fall back to.
        assert not hasattr(Controller, "_select_once")
        assert not hasattr(adapted, "decider")
        assert [
            name
            for name in dir(Controller)
            if name.startswith("_select") or name.startswith("_cascade")
        ] == ["_select"]
    finally:
        evidence.close()


def test_integration_offline_scenarios_are_deterministic():
    from unstuck.cascade import scenarios

    first = asyncio.run(scenarios.run_scenarios())
    second = asyncio.run(scenarios.run_scenarios())
    assert first["status"] == "passed"
    assert first["live_checks"] == "Pending"
    cold, history = first["scenarios"]
    assert cold["scenario"] == "cold"
    assert cold["path"] == cold["expected_path"] == "rechosen"
    assert cold["history_status"] == "empty"
    assert (cold["selector_calls"], cold["diagnostic_reads"]) == (2, 2)
    assert cold["blocked"][0]["blocked_class"] == "failed_execution"
    assert history["scenario"] == "history"
    assert history["path"] == history["expected_path"] == "direct"
    assert history["history_status"] == "matched"
    # The precluded repetition cost no selector call and no read.
    assert (history["selector_calls"], history["diagnostic_reads"]) == (1, 1)
    assert history["blocked"] == []
    assert history["precluded"][0]["blocked_class"] == "exact_repeat"
    assert cold["precluded"] == []
    assert history["first_call_path"] == "unresolved"
    assert history["stored_receipts"] == 2
    assert cold["latency_claim"] == history["latency_claim"] == "unmeasured"
    # The decision identity is content addressed, so two runs agree exactly.
    volatile = {"receipt_references", "created_receipts"}
    for before, after in zip(first["scenarios"], second["scenarios"], strict=True):
        assert {key: value for key, value in before.items() if key not in volatile} == {
            key: value for key, value in after.items() if key not in volatile
        }
        assert len(before["decision_context_hash"]) == 64
    # Receipt identities are call linked, so a second invocation writes new rows.
    assert second["scenarios"][1]["receipt_references"] != history["receipt_references"]


def test_integration_package_exports_the_complete_engine_surface():
    import unstuck.cascade as package

    for name in (
        "CascadeEngine",
        "CascadeRequest",
        "CascadeResult",
        "DecisionReceipt",
        "ReceiptVerifier",
        "HistoryMatcher",
        "SelectorInput",
        "ToolCapabilityCard",
        "CascadeJournal",
        "SilentJournal",
        "decision_history",
        "selection_authority",
    ):
        assert name in package.__all__
        assert getattr(package, name) is not None
    assert len(set(package.__all__)) == len(package.__all__)
    for removed in (
        "JevRequest",
        "JevSupplement",
        "CascadeContext",
        "NotCalledReason",
        # A first-pass-only core selector would bypass receipt admission,
        # exact-repetition blocking, and the second decision.
        "CascadeDecisionEngine",
    ):
        assert removed not in package.__all__
        assert not hasattr(package, removed)


def test_bounds_the_incident_allowance_is_inherited_not_restarted(
    repository, verifier, clock, make_state, policy
):
    # The controller already spent one read on this incident.
    spent = make_state(budgets=Counters(diagnostic_reads=1))
    decider = two_pass_decider()
    executor = ScriptedExecutor(default=STALL)
    result = run(policy, repository, verifier, clock, decider, executor, request(spent))
    assert decider.requests[0].budgets.remaining_diagnostic_reads == 1
    assert result.path == "unresolved"
    # One read remained, so the stalled first read exhausts the incident.
    assert result.unresolved_reason is Reason.DIAGNOSTICS_EXHAUSTED
    assert result.consumed.diagnostic_reads == 1
    assert result.consumed.decider_calls == 1
    assert len(executor.requests) == 1
    assert len(decider.requests) == 1


def test_bounds_an_exhausted_incident_executes_no_read_at_all(
    repository, verifier, clock, make_state, policy
):
    spent = make_state(budgets=Counters(diagnostic_reads=2))
    decider = two_pass_decider()
    executor = ScriptedExecutor()
    result = run(policy, repository, verifier, clock, decider, executor, request(spent))
    assert decider.requests[0].budgets.remaining_diagnostic_reads == 0
    assert result.unresolved_reason is Reason.DIAGNOSTICS_EXHAUSTED
    assert result.consumed.diagnostic_reads == 0
    assert executor.requests == []
    assert result.created_receipt_ids == ()


def test_bounds_an_exhausted_selector_allowance_refuses_a_further_call(
    repository, verifier, clock, make_state, policy
):
    spent = make_state(budgets=Counters(decider_calls=2))
    decider = two_pass_decider()
    executor = ScriptedExecutor()
    result = run(policy, repository, verifier, clock, decider, executor, request(spent))
    assert result.unresolved_reason is Reason.FIRST_REJECTED
    assert result.consumed.decider_calls == 0
    assert decider.requests == [] and executor.requests == []


def one_read_engine(policy, repository, verifier, clock, decider, executor):
    return Composed(
        decider=decider,
        executor=executor,
        repository=repository,
        verifier=verifier,
        clock=clock,
        policy=policy,
        matcher=HistoryMatcher(repository),
        max_diagnostic_reads=1,
    )


def test_bounds_a_one_read_budget_ends_an_executed_dead_end_without_a_second_decision(
    repository, verifier, clock, state, policy
):
    decider = two_pass_decider()
    executor = ScriptedExecutor(default=STALL)
    worker = one_read_engine(policy, repository, verifier, clock, decider, executor)
    result = asyncio.run(worker.resolve(request(state)))
    assert decider.requests[0].budgets.remaining_diagnostic_reads == 1
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.DIAGNOSTICS_EXHAUSTED
    assert result.consumed.diagnostic_reads == 1
    assert result.consumed.decider_calls == 1
    assert len(decider.requests) == 1 and len(executor.requests) == 1
    assert len(result.created_receipt_ids) == 1


def test_bounds_a_one_read_budget_keeps_the_read_a_blocked_repetition_did_not_spend(
    repository, verifier, clock, state, policy
):
    cold = asyncio.run(
        one_read_engine(
            policy,
            repository,
            verifier,
            clock,
            ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")]),
            ScriptedExecutor(default=STALL),
        ).resolve(request(state, key="call-1"))
    )
    assert cold.unresolved_reason is Reason.DIAGNOSTICS_EXHAUSTED
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_pipeline")])
    executor = ScriptedExecutor()
    worker = one_read_engine(policy, repository, verifier, clock, decider, executor)
    second = asyncio.run(worker.resolve(request(state, key="call-2")))
    assert second.path == "direct"
    assert second.precluded[0].blocked_class == "exact_repeat"
    assert second.precluded[0].receipt_id == cold.created_receipt_ids[0]
    assert [
        payload.budgets.remaining_diagnostic_reads for payload in decider.requests
    ] == [1]
    assert second.consumed.diagnostic_reads == 1
    assert [item.capability for item in executor.requests] == ["inspect_pipeline"]


def test_bounds_a_lowered_read_budget_below_the_spent_ledger_projects_none_left(
    repository, verifier, clock, make_state, policy
):
    spent = make_state(budgets=Counters(diagnostic_reads=2))
    decider = two_pass_decider()
    executor = ScriptedExecutor()
    worker = one_read_engine(policy, repository, verifier, clock, decider, executor)
    result = asyncio.run(worker.resolve(request(spent)))
    assert decider.requests[0].budgets.remaining_diagnostic_reads == 0
    assert result.unresolved_reason is Reason.DIAGNOSTICS_EXHAUSTED
    assert executor.requests == []


@pytest.mark.parametrize("reads", [-1, 0, MAX_DIAGNOSTIC_READS + 1])
def test_bounds_a_read_budget_outside_one_to_the_absolute_maximum_is_refused(
    repository, verifier, clock, policy, reads
):
    with pytest.raises(RuntimeFault) as raised:
        CascadeEngine(
            decider=two_pass_decider(),
            repository=repository,
            verifier=verifier,
            clock=clock,
            policy=policy,
            max_diagnostic_reads=reads,
        )
    assert raised.value.reason is Reason.INVARIANT


def test_bounds_the_default_read_budget_is_the_absolute_maximum(
    repository, verifier, clock, policy
):
    worker = CascadeEngine(
        decider=two_pass_decider(),
        repository=repository,
        verifier=verifier,
        clock=clock,
        policy=policy,
    )
    assert worker.max_diagnostic_reads == MAX_DIAGNOSTIC_READS == 2


def test_bounds_history_unavailable_is_explicit_and_stops_before_selection(
    verifier, clock, state, policy
):
    from test_matching import FailingRepository

    from unstuck.cascade.matching import HistoryMatcher as Matcher

    store = FailingRepository()
    decider = two_pass_decider()
    executor = ScriptedExecutor()
    worker = worker_engine(
        policy, store, verifier, clock, decider, executor, Matcher(store)
    )
    result = asyncio.run(worker.resolve(request(state)))
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.HISTORY_UNAVAILABLE
    assert result.history.status == "unavailable"
    assert result.consumed.decider_calls == 0
    assert decider.requests == [] and executor.requests == []


class WindowedRepository(LocalReceiptRepository):
    """A store whose read budget stops before the namespace is exhausted.

    The receipts are all stored and all live. What the bounded read returns is
    the first `window` of them, which is how a real paginated walk ends when
    its page or item budget runs out with rows still unread.
    """

    def __init__(self, *, window: int = 0):
        super().__init__()
        self.window = window

    def find_page(self, namespace, problem_fingerprint, *, budget=None):
        live = self.find(namespace, problem_fingerprint)
        kept = live[: self.window]
        return ReceiptPage(
            receipts=kept,
            truncated=len(kept) < len(live),
            pages_read=1,
            items_examined=len(live),
        )


def test_bounds_a_blocker_outside_the_read_window_stops_before_any_call(
    verifier, clock, state, policy
):
    """A receipt the budget never reached cannot be treated as absent.

    Call 1 writes the exact no-progress receipt for `inspect_schema_keys`.
    Call 2's bounded read stops before reaching it, so the blocker that would
    refuse the identical repetition is sitting in the unread remainder. The
    cascade fails closed as `history_unavailable`: the repeated diagnostic
    never executes, and the truncation is recorded rather than discarded.
    """
    store = WindowedRepository(window=0)
    cold = run(
        policy,
        store,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")]),
        ScriptedExecutor(default=STALL),
        request(state, key="call-1"),
    )
    assert cold.history.status == "empty"
    assert cold.history.retrieval_truncated is False
    assert len(cold.created_receipt_ids) == 1
    assert store.count(request(state).namespace) == 1

    decider = two_pass_decider()
    executor = ScriptedExecutor(default=STALL)
    worker = worker_engine(
        policy, store, verifier, clock, decider, executor, HistoryMatcher(store)
    )
    second = asyncio.run(worker.resolve(request(state, key="call-2")))
    assert second.path == "unresolved"
    assert second.unresolved_reason is Reason.HISTORY_UNAVAILABLE
    assert second.history.status == "unavailable"
    assert second.history.retrieval_truncated is True
    assert second.history.matches == ()
    # Nothing was selected, nothing was read, and nothing was admitted.
    assert decider.requests == [] and executor.requests == []
    assert second.consumed == Counters(history_lookups=1)
    assert second.created_receipt_ids == () and second.reports == ()


def test_bounds_an_exhausted_read_window_still_offers_the_blocker(
    verifier, clock, state, policy
):
    """The fail-closed path is truncation, not the presence of a blocker."""
    store = WindowedRepository(window=4)
    run(
        policy,
        store,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")]),
        ScriptedExecutor(default=STALL),
        request(state, key="call-1"),
    )
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_pipeline")])
    executor = ScriptedExecutor()
    worker = worker_engine(
        policy, store, verifier, clock, decider, executor, HistoryMatcher(store)
    )
    second = asyncio.run(worker.resolve(request(state, key="call-2")))
    assert second.history.status == "matched"
    assert second.history.retrieval_truncated is False
    assert second.path == "direct"
    assert second.precluded[0].blocked_class == "exact_repeat"
    assert len(executor.requests) == 1


def test_bounds_no_available_candidate_is_unresolved_without_a_call(
    repository, verifier, clock, make_state, policy
):
    blank = make_state(available=())
    decider = two_pass_decider()
    executor = ScriptedExecutor()
    result = run(policy, repository, verifier, clock, decider, executor, request(blank))
    assert result.unresolved_reason is Reason.NO_READ
    assert result.consumed.decider_calls == 0
    assert decider.requests == [] and executor.requests == []


def test_bounds_a_version_or_shape_mismatch_fails_closed(
    repository, verifier, clock, state, policy
):
    stale = ScriptedDecider(
        [ScriptedChoice(selected_id="inspect_schema_keys")],
        model_revision="scripted-v2",
    )
    stale_result = run(
        policy, repository, verifier, clock, stale, ScriptedExecutor(), request(state)
    )
    assert stale_result.unresolved_reason is Reason.FIRST_REJECTED
    assert stale_result.consumed.decider_calls == 1

    class WrongShapeDecider(ScriptedDecider):
        async def select(self, payload):
            self.requests.append(payload)
            return {"selected_id": "inspect_schema_keys"}

    shaped = run(
        policy,
        type(repository)(),
        verifier,
        clock,
        WrongShapeDecider([ScriptedChoice()]),
        ScriptedExecutor(),
        request(state, key="shape-1"),
    )
    assert shaped.unresolved_reason is Reason.FIRST_REJECTED

    class WrongPassDecider(ScriptedDecider):
        async def select(self, payload):
            raw = await super().select(payload)
            if payload.pass_index == 2:
                return type(raw).model_validate(raw.model_dump() | {"pass_index": 1})
            return raw

    passed = run(
        policy,
        type(repository)(),
        verifier,
        clock,
        WrongPassDecider(
            [
                ScriptedChoice(selected_id="inspect_schema_keys"),
                ScriptedChoice(selected_id="inspect_pipeline"),
            ]
        ),
        ScriptedExecutor(outcomes={"inspect_schema_keys": STALL}),
        request(state, key="pass-1"),
    )
    assert passed.unresolved_reason is Reason.FINAL_REJECTED
    assert passed.consumed.decider_calls == 2


def test_bounds_a_non_report_diagnostic_result_is_a_tool_error(
    repository, verifier, clock, state, policy
):
    class WrongShapeExecutor(ScriptedExecutor):
        async def execute(self, payload):
            self.requests.append(payload)
            return {"status": "evidence"}

    result = run(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")]),
        WrongShapeExecutor(),
        request(state),
    )
    assert result.unresolved_reason is Reason.TOOL_ERROR
    assert result.consumed.diagnostic_reads == 1
    assert result.created_receipt_ids == ()


def test_bounds_a_context_change_mid_flight_refuses_the_authority_change(
    repository, verifier, clock, state
):
    policy = CascadePolicy(baseline_id="decider_memory", model_revision="scripted-v1")
    drifted = CascadePolicy(
        baseline_id="decider_memory",
        model_revision="scripted-v1",
        calibration={"first_confidence_min": 0.5, "final_confidence_min": 0.5},
    )
    executor = ScriptedExecutor()
    worker = worker_engine(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")]),
        executor,
    )

    class DriftingExecutor(ScriptedExecutor):
        async def execute(self, payload):
            report = await super().execute(payload)
            # The bound policy moves after the read but before admission.
            worker.policy = drifted
            return report

    worker.executor = DriftingExecutor()
    result = asyncio.run(worker.resolve(request(state)))
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.CONFLICT
    assert result.consumed.diagnostic_reads == 1
    assert result.created_receipt_ids == ()
    assert repository.count(request(state).namespace) == 0


def _direct(repository, verifier, clock, state, policy):
    return run(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")]),
        ScriptedExecutor(),
        request(state, key="direct-1"),
    )


def _rechosen(repository, verifier, clock, state, policy):
    return run(
        policy,
        repository,
        verifier,
        clock,
        two_pass_decider(),
        ScriptedExecutor(outcomes={"inspect_schema_keys": STALL}),
        request(state, key="rechosen-1"),
    )


def test_bounds_result_mapping_rejects_every_incoherent_combination(
    repository, verifier, clock, state, policy
):
    direct = _direct(repository, verifier, clock, state, policy)
    rechosen = _rechosen(type(repository)(), verifier, clock, state, policy)
    assert direct.path == "direct" and rechosen.path == "rechosen"
    unresolved = run(
        policy,
        type(repository)(),
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(confidence=0.1)]),
        ScriptedExecutor(),
        request(state, key="unresolved-1"),
    )
    assert unresolved.path == "unresolved"
    cases = [
        (unresolved, {"selected_tool_id": "inspect_scope"}),
        (unresolved, {"executed_capability": "inspect_scope"}),
        (unresolved, {"unresolved_reason": Reason.POPULATED_INITIAL}),
        (unresolved, {"unresolved_reason": Reason.LEASE_EXPIRED}),
        (direct, {"selected_tool_id": None}),
        (direct, {"executed_capability": None}),
        (direct, {"unresolved_reason": Reason.INSUFFICIENT}),
        (direct, {"outcome_class": "no_progress"}),
        (direct, {"outcome_class": None}),
        (direct, {"consumed": Counters(decider_calls=2, diagnostic_reads=1)}),
        (direct, {"final": rechosen.final}),
        (rechosen, {"consumed": Counters(decider_calls=1, diagnostic_reads=2)}),
        (rechosen, {"final": None}),
        (rechosen, {"blocked": ()}),
        (direct, {"consumed": Counters(decider_calls=1, diagnostic_reads=0)}),
        (direct, {"created_receipt_ids": ()}),
        (
            rechosen,
            {"created_receipt_ids": (rechosen.created_receipt_ids[0],) * 2},
        ),
        (
            direct,
            {"consumed": Counters(decider_calls=1, diagnostic_reads=2)},
        ),
    ]
    for baseline, changes in cases:
        with pytest.raises(ValidationError):
            type(baseline).model_validate(baseline.model_dump() | changes)
    # The cascade may never report a controller allowance as consumed.
    for name in ("wait_checks", "report_queries", "repair_attempts", "agent_turns"):
        with pytest.raises(ValidationError):
            type(direct).model_validate(
                direct.model_dump()
                | {
                    "consumed": Counters.model_validate(
                        direct.consumed.model_dump() | {name: 1}
                    )
                }
            )
    assert type(direct).model_validate(direct.model_dump()) == direct
    assert direct.selector_calls == 1 and direct.diagnostic_reads == 1
    assert rechosen.selector_calls == 2 and rechosen.diagnostic_reads == 2


def test_integration_one_lookup_reaches_both_the_selector_and_the_authority(
    repository, verifier, clock, make_receipt, stalled_report, state
):
    """There is no second history left to disagree with.

    The orchestrator performs the one bounded lookup, hands it to the selector,
    and reports it on the same result the caller records, so a controller and a
    selector can no longer hold two different histories for one decision.
    """
    policy = CascadePolicy(baseline_id="decider_memory", model_revision="scripted-v1")
    receipt = make_receipt(stalled_report)
    repository.put(receipt, verifier.token)
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_pipeline")])
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        ScriptedExecutor(),
        request(state, key="call-2"),
    )
    assert result.path == "direct"
    assert [item.receipt_id for item in result.precluded] == [receipt.receipt_id]
    assert result.history.status == "matched"
    assert result.history.receipt_ids == (receipt.receipt_id,)
    assert decider.requests[0].history == result.history
    assert decision_history(result.history).receipt_ids == result.history.receipt_ids
    assert selection_authority(result).receipt_ids == result.history.receipt_ids
    assert selection_authority(result).history_status == "matched"
