"""Step 10: cross-call Decision Receipt behaviour and the exact-repeat guard.

An exact blocker is precluded from the offer before the first decision, so the
one fresh first decision chooses among what remains.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_cross_call.py -q
"""

import asyncio

import pytest
from test_same_call import engine, request, run

from unstuck.cascade.offline import (
    ScriptedChoice,
    ScriptedDecider,
    ScriptedExecutor,
    ScriptedOutcome,
)
from unstuck.cascade.policy import CascadePolicy
from unstuck.domain import Reason

STALL = ScriptedOutcome(changed=False)


def first_call(
    repository,
    verifier,
    clock,
    state,
    policy,
    *,
    tool="inspect_schema_keys",
    outcome=STALL,
    key="call-1",
):
    """Call 1 on a cold namespace."""
    decider = ScriptedDecider([ScriptedChoice(selected_id=tool)])
    executor = ScriptedExecutor(outcomes={tool: outcome})
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, arm=policy.baseline_id, key=key),
    )
    return result, decider, executor


def test_cross_call_cold_namespace_then_precluded_repetition_one_fresh_choice(
    repository, verifier, clock, state, policy
):
    cold, first_decider, _ = first_call(repository, verifier, clock, state, policy)
    assert cold.path == "unresolved"
    assert cold.history.status == "empty"
    assert first_decider.requests[0].history.status == "empty"
    assert len(cold.created_receipt_ids) == 1
    assert repository.count(request(state).namespace) == 1

    # Call 2 is a brand new call over the same canonical state.
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
    assert second.history.status == "matched"
    assert second.history.receipt_ids == cold.created_receipt_ids
    assert second.history.matches[0].match_class == "exact"
    assert second.history.matches[0].outcome_class == "no_progress"
    # The first decision still ran fresh, with the history attached.
    assert len(decider.requests) == 1
    assert decider.requests[0].pass_index == 1
    assert decider.requests[0].history.status == "matched"
    assert second.first is not None and second.first.accepted
    assert second.first.selected_id == "inspect_pipeline"
    # The identical execution was precluded and cost no call and no read.
    assert len(second.precluded) == 1
    assert second.blocked == ()
    assert second.precluded[0].blocked_class == "exact_repeat"
    assert second.precluded[0].outcome_class == "no_progress"
    # The decision sees the verifier's own reason, not a terminal label.
    assert second.precluded[0].reason_code is Reason.INSUFFICIENT
    assert second.precluded[0].receipt_id == cold.created_receipt_ids[0]
    assert decider.requests[0].precluded == second.precluded
    assert second.path == "direct"
    assert second.selected_tool_id == "inspect_pipeline"
    assert second.consumed.decider_calls == 1
    assert second.consumed.diagnostic_reads == 1
    assert len(executor.requests) == 1
    assert executor.requests[0].public_tool_id == "inspect_pipeline"
    assert "inspect_schema_keys" not in decider.requests[0].available_tool_ids


def test_cross_call_a_different_fresh_choice_executes_after_the_preclusion(
    repository, verifier, clock, state, policy
):
    cold, _, _ = first_call(repository, verifier, clock, state, policy)
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_scope")])
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
    assert second.path == "direct"
    assert second.selected_tool_id == "inspect_scope"
    assert second.blocked == ()
    assert [item.public_tool_id for item in second.precluded] == ["inspect_schema_keys"]
    assert second.consumed.decider_calls == 1
    assert second.consumed.diagnostic_reads == 1
    assert second.history.status == "matched"
    # The blocked key left the offer; the receipt stays as labeled evidence.
    assert decider.requests[0].available_tool_ids == (
        "inspect_pipeline",
        "inspect_scope",
    )
    assert cold.created_receipt_ids[0] in decider.requests[0].history.receipt_ids


def test_cross_call_history_never_replays_a_prior_choice(
    repository, verifier, clock, state, policy
):
    # Call 1 records a useful read of one tool.
    useful, _, _ = first_call(
        repository,
        verifier,
        clock,
        state,
        policy,
        tool="inspect_pipeline",
        outcome=ScriptedOutcome(),
    )
    assert useful.path == "direct"
    assert repository.count(request(state).namespace) == 1
    # Call 2 chooses something else. The historical choice is not copied.
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_scope")])
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
    assert second.history.matches[0].outcome_class == "useful_evidence"
    assert second.history.blocked_executions == frozenset()
    assert second.selected_tool_id == "inspect_scope"
    assert second.executed_capability == "inspect_scope"
    assert executor.requests[0].public_tool_id == "inspect_scope"


def test_cross_call_blocked_repetition_without_an_alternative_is_unresolved(
    repository, verifier, clock, make_state, policy
):
    state = make_state(candidate_count=1)
    cold, _, _ = first_call(
        repository, verifier, clock, state, policy, tool="inspect_pipeline"
    )
    assert cold.unresolved_reason is Reason.INSUFFICIENT
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
    assert second.path == "unresolved"
    assert second.unresolved_reason is Reason.REPEAT_BLOCKED
    # Nothing remained to offer, so no selector call was made at all.
    assert second.consumed.decider_calls == 0
    assert decider.requests == []
    assert second.consumed.diagnostic_reads == 0
    assert executor.requests == []
    assert second.created_receipt_ids == ()
    assert len(second.precluded) == 1
    assert second.blocked == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"dataset_version": "v18"},
        {"available": ("inspect_schema_keys", "inspect_scope")},
        {"country_codes": ("DE",)},
    ],
)
def test_cross_call_a_changed_context_makes_the_read_eligible_again(
    repository, verifier, clock, make_state, policy, changes
):
    state = make_state()
    first_call(repository, verifier, clock, state, policy)
    changed = make_state(**changes)
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")])
    executor = ScriptedExecutor()
    second = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(changed, key="call-2"),
    )
    assert second.path == "direct"
    assert second.selected_tool_id == "inspect_schema_keys"
    assert second.blocked == ()
    assert second.history.blocked_executions == frozenset()
    assert executor.requests[0].public_tool_id == "inspect_schema_keys"


@pytest.mark.parametrize(
    "status,reason",
    [("provider_error", Reason.TOOL_ERROR), ("invalid_evidence", Reason.CONFLICT)],
)
def test_cross_call_a_failed_read_never_hard_blocks_a_future_call(
    repository, verifier, clock, state, policy, status, reason
):
    cold, _, _ = first_call(
        repository,
        verifier,
        clock,
        state,
        policy,
        outcome=ScriptedOutcome(status=status, changed=False, reason_code=reason),
    )
    assert cold.path == "rechosen" or cold.path == "unresolved"
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_schema_keys")])
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
    assert second.history.status == "matched"
    assert second.history.blocked_executions == frozenset()
    assert second.path == "direct"
    assert second.selected_tool_id == "inspect_schema_keys"
    assert second.blocked == ()


def test_cross_call_the_one_pass_arm_receives_no_history(
    repository, verifier, clock, state
):
    memory = CascadePolicy(baseline_id="decider_memory", model_revision="scripted-v1")
    first_call(repository, verifier, clock, state, memory)
    assert repository.count(request(state, arm="decider_memory").namespace) == 1
    one_pass = CascadePolicy(baseline_id="decider", model_revision="scripted-v1")
    decider = ScriptedDecider(
        [
            ScriptedChoice(selected_id="inspect_schema_keys"),
            ScriptedChoice(selected_id="inspect_pipeline"),
        ]
    )
    executor = ScriptedExecutor(outcomes={"inspect_schema_keys": STALL})
    result = run(
        policy=one_pass,
        repository=repository,
        verifier=verifier,
        clock=clock,
        decider=decider,
        executor=executor,
        task=request(state, arm="decider", key="call-2"),
    )
    assert result.history.status == "not_requested"
    assert result.history.matches == ()
    assert result.consumed.history_lookups == 0
    assert decider.requests[0].history.status == "not_requested"
    # The same-call second decision is still available to the one-pass arm.
    assert result.path == "rechosen"
    assert result.consumed.decider_calls == 2
    # Nothing was written to the one-pass namespace.
    assert repository.count(request(state, arm="decider").namespace) == 0


def test_cross_call_precluded_repetition_then_two_failed_choices_stop(
    repository, verifier, clock, state, policy
):
    first_call(repository, verifier, clock, state, policy)
    decider = ScriptedDecider(
        [
            ScriptedChoice(selected_id="inspect_pipeline"),
            ScriptedChoice(selected_id="inspect_scope"),
        ]
    )
    executor = ScriptedExecutor(default=STALL)
    second = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, key="call-2"),
    )
    assert second.path == "unresolved"
    assert second.unresolved_reason is Reason.INSUFFICIENT
    # The second choice is final: two calls, two reads, never a third.
    assert second.consumed.decider_calls == 2
    assert second.consumed.diagnostic_reads == 2
    assert len(decider.requests) == 2
    assert len(executor.requests) == 2


def test_cross_call_the_idempotency_key_carries_no_decision_authority(
    repository, verifier, clock, make_state, policy
):
    """This unit answers every invocation it is given, and caches nothing.

    Duplicate invocations are a durable property of the run store, which is
    the only idempotency authority. Two different questions under one caller
    key therefore each decide for themselves rather than one of them being
    answered with the other's result.
    """
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_scope")])
    executor = ScriptedExecutor()
    worker = engine(policy, repository, verifier, clock, decider, executor)
    one = make_state()
    other = make_state(country_codes=("IT",))

    async def both():
        return (
            await worker.resolve(request(one, key="shared-key")),
            await worker.resolve(request(other, key="shared-key")),
        )

    first, second = asyncio.run(both())
    assert first.path == "direct" and second.path == "direct"
    assert first.cascade_id != second.cascade_id
    assert first.created_receipt_ids != second.created_receipt_ids
    assert len(decider.requests) == 2
    assert len(executor.requests) == 2
    assert (
        decider.requests[0].problem_fingerprint
        != decider.requests[1].problem_fingerprint
    )


def test_cross_call_concurrent_invocations_keep_their_own_authority(
    repository, verifier, clock, state, policy
):
    """One instance serves concurrent invocations without mixing them."""
    decider = ScriptedDecider([ScriptedChoice(selected_id="inspect_scope")])
    worker = engine(policy, repository, verifier, clock, decider, ScriptedExecutor())
    authorities = [ScriptedExecutor(state=state), ScriptedExecutor(state=state)]

    async def both():
        return await asyncio.gather(
            *(
                worker.resolve(request(state, key=f"call-{index}"), executor=authority)
                for index, authority in enumerate(authorities)
            )
        )

    results = asyncio.run(both())
    assert {result.path for result in results} == {"direct"}
    assert len({result.cascade_id for result in results}) == 2
    # Each invocation executed through its own authority, exactly once.
    assert [len(authority.requests) for authority in authorities] == [1, 1]
    assert len(decider.requests) == 2
