"""Step 8: bounded same-call orchestration for both selector arms.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_same_call.py -q
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from unstuck.cascade.engine import CASCADE_REASONS, CascadeEngine, CascadeRequest
from unstuck.cascade.identity import CallIdentity
from unstuck.cascade.matching import HistoryMatcher
from unstuck.cascade.offline import (
    ScriptedChoice,
    ScriptedDecider,
    ScriptedExecutor,
    ScriptedOutcome,
)
from unstuck.cascade.policy import CascadePolicy
from unstuck.cascade.receipts import ReceiptNamespace
from unstuck.domain import Counters, Reason

ARMS = ["decider", "decider_memory"]


class Composed(CascadeEngine):
    """Test-only composition that carries one scripted execution authority.

    Production callers own the authority and hand it to `resolve` for one
    invocation, which is what keeps concurrent runs apart. These unit tests
    drive one invocation at a time, so the scripted authority is carried here
    and passed through the same per-invocation parameter.

    A scripted authority holds no authoritative state of its own, so the state
    it projects after a read is the task's own state. The real refresh is
    proven against the controller in tests/core_runtime.
    """

    def __init__(self, *, executor, **changes):
        super().__init__(**changes)
        self.executor = executor

    async def resolve(self, request, *, executor=None, journal=None):
        authority = executor or self.executor
        if (
            isinstance(authority, ScriptedExecutor)
            and authority.state is None
            and isinstance(request, CascadeRequest)
        ):
            authority.state = request.state
        return await super().resolve(request, executor=authority, journal=journal)


def engine(policy, repository, verifier, clock, decider, executor) -> CascadeEngine:
    return Composed(
        decider=decider,
        executor=executor,
        repository=repository,
        verifier=verifier,
        clock=clock,
        policy=policy,
        matcher=HistoryMatcher(repository),
    )


def call_identity(**changes) -> CallIdentity:
    return CallIdentity.model_validate(
        {
            "run_id": uuid4(),
            "request_id": uuid4(),
            "session_id": uuid4(),
            "trace_id": uuid4(),
            "lease_owner": uuid4(),
            "observed_at": datetime(2026, 10, 1, 12, tzinfo=UTC),
        }
        | changes
    )


def request(state, *, arm="decider_memory", key="call-1", **changes) -> CascadeRequest:
    return CascadeRequest.model_validate(
        {
            "cascade_id": uuid4(),
            "idempotency_key": key,
            "namespace": ReceiptNamespace(arm=arm, case_id="case-01"),
            "call": call_identity(),
            "state": state,
        }
        | changes
    )


def run(policy, repository, verifier, clock, decider, executor, task):
    worker = engine(policy, repository, verifier, clock, decider, executor)
    return asyncio.run(worker.resolve(task))


@pytest.mark.parametrize("arm", ARMS)
def test_same_call_direct_path_executes_one_read_for_both_arms(
    repository, verifier, clock, state, arm
):
    policy = CascadePolicy(baseline_id=arm, model_revision="scripted-v1")
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")])
    executor = ScriptedExecutor()
    task = request(state, arm=arm)
    result = run(policy, repository, verifier, clock, decider, executor, task)
    assert result.path == "direct"
    assert result.selected_tool_id == "inspect_schema_keys"
    assert result.executed_capability == "inspect_schema_keys"
    assert result.unresolved_reason is None
    assert result.outcome_class == "useful_evidence"
    assert result.first is not None and result.first.accepted
    assert result.final is None
    assert result.blocked == ()
    assert len(result.created_receipt_ids) == 1
    assert len(decider.requests) == 1 and len(executor.requests) == 1
    assert result.consumed == Counters(
        decider_calls=1,
        diagnostic_reads=1,
        history_lookups=1 if arm == "decider_memory" else 0,
    )
    # Only the memory arm keeps a cross-call receipt.
    expected = 1 if arm == "decider_memory" else 0
    assert repository.count(task.namespace) == expected
    assert result.history.status == (
        "empty" if arm == "decider_memory" else "not_requested"
    )


@pytest.mark.parametrize("arm", ARMS)
@pytest.mark.parametrize(
    "status,changed,reason",
    [
        ("evidence", False, Reason.INSUFFICIENT),
        ("provider_error", False, Reason.TOOL_ERROR),
        ("invalid_evidence", False, Reason.CONFLICT),
    ],
)
def test_same_call_second_decision_opens_after_any_unproductive_read(
    repository, verifier, clock, state, arm, status, changed, reason
):
    policy = CascadePolicy(baseline_id=arm, model_revision="scripted-v1")
    decider = ScriptedDecider(
        [
            ScriptedChoice(selected_id="inspect_schema_keys"),
            ScriptedChoice(selected_id="inspect_pipeline"),
        ]
    )
    executor = ScriptedExecutor(
        outcomes={
            "inspect_schema_keys": ScriptedOutcome(
                status=status, changed=changed, reason_code=reason
            )
        }
    )
    result = run(
        policy, repository, verifier, clock, decider, executor, request(state, arm=arm)
    )
    assert result.path == "rechosen"
    assert result.selected_tool_id == "inspect_pipeline"
    assert result.executed_capability == "inspect_pipeline"
    assert result.outcome_class == "useful_evidence"
    assert result.final is not None and result.final.pass_index == 2
    assert result.consumed.decider_calls == 2
    assert result.consumed.diagnostic_reads == 2
    assert len(result.created_receipt_ids) == 2
    # The second decision sees the failed capability as a blocked execution.
    assert len(result.blocked) == 1
    blocked = result.blocked[0]
    assert blocked.public_tool_id == "inspect_schema_keys"
    assert blocked.selected_capability == "inspect_schema_keys"
    assert blocked.blocked_class == "failed_execution"
    assert blocked.reason_code is reason
    assert blocked.receipt_id == result.created_receipt_ids[0]
    second = decider.requests[1]
    assert second.pass_index == 2
    assert second.blocked == (blocked,)
    assert "inspect_schema_keys" not in second.available_tool_ids
    assert "inspect_schema_keys" in [card.public_tool_id for card in second.cards]


def test_same_call_no_remaining_candidate_ends_unresolved(
    repository, verifier, clock, make_state, policy
):
    state = make_state(candidate_count=1)
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_pipeline")])
    executor = ScriptedExecutor(default=ScriptedOutcome(changed=False))
    result = run(policy, repository, verifier, clock, decider, executor, request(state))
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.INSUFFICIENT
    assert result.selected_tool_id is None and result.executed_capability is None
    assert result.outcome_class == "no_progress"
    assert result.consumed.decider_calls == 1
    assert result.consumed.diagnostic_reads == 1
    assert len(decider.requests) == 1
    assert len(result.created_receipt_ids) == 1
    assert result.unresolved_reason in CASCADE_REASONS


@pytest.mark.parametrize(
    "choice,reason",
    [
        (
            ScriptedChoice(selected_id="inspect_schema_keys", confidence=0.79),
            Reason.FIRST_REJECTED,
        ),
        (ScriptedChoice(selected_id="request_review"), Reason.FIRST_REJECTED),
        (
            ScriptedChoice(
                selected_id=None, confidence=None, validation_status="invalid"
            ),
            Reason.FIRST_REJECTED,
        ),
        (ScriptedChoice(selected_id="inspect_future"), Reason.FIRST_REJECTED),
        (
            ScriptedChoice(
                selected_id="inspect_schema_keys",
                candidate_ids=("inspect_schema_keys",),
            ),
            Reason.FIRST_REJECTED,
        ),
    ],
)
def test_same_call_rejected_first_decision_executes_nothing(
    repository, verifier, clock, state, policy, choice, reason
):
    decider = ScriptedDecider([choice])
    executor = ScriptedExecutor()
    result = run(policy, repository, verifier, clock, decider, executor, request(state))
    assert result.path == "unresolved"
    assert result.unresolved_reason is reason
    assert result.consumed.decider_calls == 1
    assert result.consumed.diagnostic_reads == 0
    assert executor.requests == []
    assert result.created_receipt_ids == ()
    assert result.first is not None and not result.first.accepted
    assert result.first.rejection is reason


def test_same_call_rejected_second_decision_fails_closed(
    repository, verifier, clock, state, policy
):
    decider = ScriptedDecider(
        [
            ScriptedChoice(selected_id="inspect_schema_keys"),
            ScriptedChoice(selected_id="inspect_pipeline", confidence=0.5),
        ]
    )
    executor = ScriptedExecutor(
        outcomes={"inspect_schema_keys": ScriptedOutcome(changed=False)}
    )
    result = run(policy, repository, verifier, clock, decider, executor, request(state))
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.FINAL_REJECTED
    assert result.consumed.decider_calls == 2
    assert result.consumed.diagnostic_reads == 1
    assert result.final is not None and not result.final.accepted
    assert len(executor.requests) == 1


def test_same_call_second_read_failure_ends_without_a_third_decision(
    repository, verifier, clock, state, policy
):
    decider = ScriptedDecider(
        [
            ScriptedChoice(selected_id="inspect_schema_keys"),
            ScriptedChoice(selected_id="inspect_pipeline"),
        ]
    )
    executor = ScriptedExecutor(default=ScriptedOutcome(changed=False))
    result = run(policy, repository, verifier, clock, decider, executor, request(state))
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.INSUFFICIENT
    assert result.consumed.decider_calls == 2
    assert result.consumed.diagnostic_reads == 2
    assert len(decider.requests) == 2
    assert len(executor.requests) == 2
    assert len(result.created_receipt_ids) == 2
    assert result.outcome_class == "no_progress"


def test_same_call_selector_transport_failure_consumes_its_attempt(
    repository, verifier, clock, state, policy
):
    decider = ScriptedDecider([ScriptedChoice(transport_error=True)])
    executor = ScriptedExecutor()
    result = run(policy, repository, verifier, clock, decider, executor, request(state))
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.DECIDER_ERROR
    assert result.consumed.decider_calls == 1
    assert result.consumed.diagnostic_reads == 0
    assert result.first is None
    assert executor.requests == []


def test_same_call_diagnostic_transport_failure_consumes_its_read(
    repository, verifier, clock, state, policy
):
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")])
    executor = ScriptedExecutor(
        outcomes={"inspect_schema_keys": ScriptedOutcome(transport_error=True)}
    )
    result = run(policy, repository, verifier, clock, decider, executor, request(state))
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.TOOL_ERROR
    assert result.consumed.diagnostic_reads == 1
    assert result.consumed.decider_calls == 1
    assert result.created_receipt_ids == ()


def test_same_call_arms_share_identical_second_decision_behaviour(
    repository, verifier, clock, state
):
    results = {}
    for arm in ARMS:
        store = type(repository)()
        policy = CascadePolicy(baseline_id=arm, model_revision="scripted-v1")
        decider = ScriptedDecider(
            [
                ScriptedChoice(selected_id="inspect_schema_keys"),
                ScriptedChoice(selected_id="inspect_scope"),
            ]
        )
        executor = ScriptedExecutor(
            outcomes={"inspect_schema_keys": ScriptedOutcome(changed=False)}
        )
        results[arm] = run(
            policy, store, verifier, clock, decider, executor, request(state, arm=arm)
        )
        assert store.count(ReceiptNamespace(arm=arm, case_id="case-01")) == (
            2 if arm == "decider_memory" else 0
        )
    for arm, result in results.items():
        assert result.path == "rechosen", arm
        assert result.selected_tool_id == "inspect_scope"
        assert result.consumed.decider_calls == 2
        assert result.consumed.diagnostic_reads == 2
        assert result.baseline_id == arm
    assert results["decider"].history.status == "not_requested"
    assert results["decider_memory"].history.status == "empty"
