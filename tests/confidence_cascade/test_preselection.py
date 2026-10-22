"""Pre-selection exact-receipt blocking: the proven dead end is removed first.

An exact current-context receipt whose own deltas prove the identical
capability and arguments moved no supported disposition precludes that one
execution before any selector call. The first decision is then made fresh over
what remains. The receipt selects, ranks and scores nothing: it only removes
the one execution it proves, and only while its context is the current one.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_preselection.py -q
"""

import asyncio

import pytest
from test_same_call import engine, request, run

from unstuck.cascade.choice import choice_request
from unstuck.cascade.integration import context_for
from unstuck.cascade.offline import (
    ScriptedChoice,
    ScriptedDecider,
    ScriptedExecutor,
    ScriptedOutcome,
)
from unstuck.cascade.policy import CascadePolicy
from unstuck.cascade.projector import BlockedExecution
from unstuck.cascade.serialization import canonical_text
from unstuck.domain import Counters, Reason

SCHEMA = "inspect_schema_keys"
PIPELINE = "inspect_pipeline"
SCOPE = "inspect_scope"
ALL = (PIPELINE, SCHEMA, SCOPE)
STALL = ScriptedOutcome(changed=False)
NEUTRAL = ScriptedOutcome(changed=True, moved=False)


class Recorder:
    """Records the journal calls in the order the cascade makes them."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []
        self.precluded_items: list[BlockedExecution] = []

    def history(self, result, problem_fingerprint):
        self.entries.append(("history", result.status))

    def precluded(self, execution):
        self.precluded_items.append(execution)
        self.entries.append(("precluded", execution.public_tool_id))

    def requested(self, payload):
        self.entries.append(("requested", str(payload.pass_index)))

    def returned(self, record, result, blocked):
        self.entries.append(("returned", str(record.pass_index)))

    def refused(self, execution):
        self.entries.append(("refused", execution.blocked_class))

    def admitted(self, receipt, stored):
        self.entries.append(("admitted", receipt.outcome_class))


def learn(
    repository, verifier, clock, state, policy, *, tool=SCHEMA, outcome=STALL, cold=True
):
    """Run 1: one read of `tool` that moves nothing, on a cold namespace."""
    decider = ScriptedDecider([ScriptedChoice(selected_id=tool)])
    executor = ScriptedExecutor(outcomes={tool: outcome})
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, arm=policy.baseline_id, key=f"learn-{tool}"),
    )
    assert result.history.status == ("empty" if cold else "matched")
    assert len(result.created_receipt_ids) == 1
    return result


def resolve(policy, repository, verifier, clock, decider, executor, task, journal):
    worker = engine(policy, repository, verifier, clock, decider, executor)
    return asyncio.run(worker.resolve(task, journal=journal))


@pytest.mark.parametrize("outcome", [STALL, NEUTRAL], ids=["no_progress", "neutral"])
def test_an_exact_blocker_is_precluded_before_the_one_fresh_first_decision(
    repository, verifier, clock, state, policy, outcome
):
    learned = learn(repository, verifier, clock, state, policy, outcome=outcome)
    recorder = Recorder()
    decider = ScriptedDecider([ScriptedChoice(selected_id=PIPELINE)])
    executor = ScriptedExecutor()
    result = resolve(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, key="call-2"),
        recorder,
    )
    # The preclusion is journaled before the only selector request.
    assert recorder.entries[:4] == [
        ("history", "matched"),
        ("precluded", SCHEMA),
        ("requested", "1"),
        ("returned", "1"),
    ]
    assert ("refused", "exact_repeat") not in recorder.entries
    precluded = recorder.precluded_items[0]
    assert precluded.blocked_class == "exact_repeat"
    assert precluded.receipt_id == learned.created_receipt_ids[0]
    assert precluded.outcome_class == learned.outcome_class
    assert precluded.reason_code is Reason.INSUFFICIENT
    assert precluded.arguments_hash == state.query_hash
    # One model call over the remaining options, in their original order.
    assert len(decider.requests) == 1
    offered = decider.requests[0]
    assert offered.pass_index == 1
    assert offered.available_tool_ids == (PIPELINE, SCOPE)
    assert offered.option_ids == (PIPELINE, SCOPE, "request_review")
    assert offered.precluded == (precluded,)
    assert offered.blocked == () and offered.first is None
    # The selector input keeps the full original decision identity.
    full = context_for(state, policy)
    assert offered.decision_context_hash == full.decision_context_hash
    assert offered.problem_fingerprint == full.problem_fingerprint
    assert offered.history.receipt_ids == learned.created_receipt_ids
    # The preclusion cost nothing; the receipt chose nothing.
    assert offered.budgets.remaining_selector_calls == 2
    assert offered.budgets.remaining_diagnostic_reads == 2
    assert result.path == "direct"
    assert result.selected_tool_id == PIPELINE
    assert result.precluded == (precluded,)
    assert result.blocked == ()
    assert result.final is None
    assert result.consumed == Counters(
        history_lookups=1, decider_calls=1, diagnostic_reads=1
    )
    assert [item.public_tool_id for item in executor.requests] == [PIPELINE]


def test_the_receipt_never_selects_the_winner(
    repository, verifier, clock, state, policy
):
    learn(repository, verifier, clock, state, policy)
    decider = ScriptedDecider([ScriptedChoice(selected_id=SCOPE)])
    executor = ScriptedExecutor()
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, key="call-2"),
    )
    # The decider's own fresh choice is what executes, with its own
    # distribution recorded as reported.
    assert result.selected_tool_id == SCOPE
    assert result.first is not None
    assert result.first.candidate_ids == (PIPELINE, SCOPE, "request_review")
    assert [(item.id, item.probability) for item in result.first.probabilities] == [
        (PIPELINE, 0.0),
        (SCOPE, 1.0),
        ("request_review", 0.0),
    ]
    assert [item.public_tool_id for item in executor.requests] == [SCOPE]


def test_a_precluded_choice_is_refused_as_an_unoffered_option(
    repository, verifier, clock, state, policy
):
    learn(repository, verifier, clock, state, policy)
    decider = ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)])
    executor = ScriptedExecutor()
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, key="call-2"),
    )
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.FIRST_REJECTED
    assert result.consumed.decider_calls == 1
    assert executor.requests == []


def test_the_new_read_receipt_links_the_original_context_and_its_decision(
    repository, verifier, clock, state, policy
):
    learned = learn(repository, verifier, clock, state, policy)
    decider = ScriptedDecider([ScriptedChoice(selected_id=PIPELINE)])
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        ScriptedExecutor(),
        request(state, key="call-2"),
    )
    (written,) = result.created_receipt_ids
    stored = {
        receipt.receipt_id: receipt
        for receipt in repository.find(
            request(state).namespace, context_for(state, policy).problem_fingerprint
        )
    }
    old = stored[learned.created_receipt_ids[0]]
    new = stored[written]
    assert new.decision_context_hash == old.decision_context_hash
    assert new.decision_id == result.first.decision_id
    assert new.model_call_id == result.first.model_call_id
    assert new.public_tool_id == PIPELINE


def test_every_candidate_precluded_ends_without_any_model_call(
    repository, verifier, clock, make_state, policy
):
    state = make_state(candidate_count=1)
    learned = learn(repository, verifier, clock, state, policy, tool=PIPELINE)
    recorder = Recorder()
    decider = ScriptedDecider([ScriptedChoice(selected_id=PIPELINE)])
    executor = ScriptedExecutor()
    result = resolve(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, key="call-2"),
        recorder,
    )
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.REPEAT_BLOCKED
    assert decider.requests == [] and executor.requests == []
    assert result.consumed == Counters(history_lookups=1)
    assert result.first is None and result.blocked == ()
    assert [item.receipt_id for item in result.precluded] == list(
        learned.created_receipt_ids
    )
    assert recorder.entries == [("history", "matched"), ("precluded", PIPELINE)]


def test_three_exact_blockers_preclude_all_three_reads(
    repository, verifier, clock, state, policy
):
    for index, tool in enumerate(ALL):
        learn(repository, verifier, clock, state, policy, tool=tool, cold=index == 0)
    decider = ScriptedDecider([ScriptedChoice(selected_id=PIPELINE)])
    executor = ScriptedExecutor()
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, key="call-4"),
    )
    assert result.unresolved_reason is Reason.REPEAT_BLOCKED
    assert {item.public_tool_id for item in result.precluded} == set(ALL)
    assert decider.requests == [] and executor.requests == []
    assert result.consumed.decider_calls == 0


def test_a_blocker_for_one_tool_never_precludes_another(
    repository, verifier, clock, state, policy
):
    learn(repository, verifier, clock, state, policy, tool=SCOPE)
    decider = ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)])
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        ScriptedExecutor(),
        request(state, key="call-2"),
    )
    assert decider.requests[0].available_tool_ids == (PIPELINE, SCHEMA)
    assert [item.public_tool_id for item in result.precluded] == [SCOPE]
    assert result.selected_tool_id == SCHEMA


@pytest.mark.parametrize(
    "outcome",
    [
        ScriptedOutcome(),
        ScriptedOutcome(
            status="provider_error", changed=False, reason_code=Reason.TOOL_ERROR
        ),
        ScriptedOutcome(
            status="invalid_evidence", changed=False, reason_code=Reason.CONFLICT
        ),
    ],
    ids=["useful", "provider_error", "invalid_evidence"],
)
def test_non_blocking_receipts_never_preclude(
    repository, verifier, clock, state, policy, outcome
):
    decider = ScriptedDecider(
        [ScriptedChoice(selected_id=SCHEMA), ScriptedChoice(selected_id=PIPELINE)]
    )
    run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        ScriptedExecutor(outcomes={SCHEMA: outcome}),
        request(state, key="call-1"),
    )
    recorder = Recorder()
    later = ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)])
    result = resolve(
        policy,
        repository,
        verifier,
        clock,
        later,
        ScriptedExecutor(),
        request(state, key="call-2"),
        recorder,
    )
    assert result.history.status == "matched"
    assert result.precluded == ()
    assert recorder.precluded_items == []
    assert later.requests[0].available_tool_ids == ALL
    assert later.requests[0].precluded == ()
    assert result.selected_tool_id == SCHEMA


@pytest.mark.parametrize(
    "changes",
    [
        {"dataset_version": "v18"},
        {"available": (SCHEMA, SCOPE)},
        {"country_codes": ("DE",)},
    ],
    ids=["stale_version", "changed_availability", "changed_problem"],
)
def test_a_compatible_or_stale_receipt_never_precludes(
    repository, verifier, clock, make_state, policy, changes
):
    learn(repository, verifier, clock, make_state(), policy)
    changed = make_state(**changes)
    decider = ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)])
    executor = ScriptedExecutor()
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(changed, key="call-2"),
    )
    assert result.precluded == ()
    assert SCHEMA in decider.requests[0].available_tool_ids
    assert result.selected_tool_id == SCHEMA
    assert [item.public_tool_id for item in executor.requests] == [SCHEMA]


def test_a_compatible_match_in_history_precludes_nothing(
    repository, verifier, clock, make_state, policy
):
    """Same versions, different current evidence: compatible, never exact."""
    learn(repository, verifier, clock, make_state(), policy)
    moved = make_state()
    decider = ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)])
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        ScriptedExecutor(),
        request(moved, key="call-2", used_capabilities=(PIPELINE,)),
    )
    assert result.history.matches[0].match_class == "compatible"
    assert result.precluded == ()
    assert SCHEMA in decider.requests[0].available_tool_ids


def test_the_one_pass_arm_never_precludes(repository, verifier, clock, state):
    memory = CascadePolicy(baseline_id="decider_memory", model_revision="scripted-v1")
    learn(repository, verifier, clock, state, memory)
    one_pass = CascadePolicy(baseline_id="decider", model_revision="scripted-v1")
    decider = ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)])
    result = run(
        one_pass,
        repository,
        verifier,
        clock,
        decider,
        ScriptedExecutor(),
        request(state, arm="decider", key="call-2"),
    )
    assert result.history.status == "not_requested"
    assert result.precluded == ()
    assert decider.requests[0].available_tool_ids == ALL


def test_without_a_blocker_the_selector_input_and_model_text_are_unchanged(
    repository, verifier, clock, state, policy
):
    decider = ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)])
    run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        ScriptedExecutor(),
        request(state, key="call-1"),
    )
    payload = decider.requests[0]
    assert payload.precluded == ()
    assert "precluded" not in payload.model_dump()
    assert '"precluded"' not in canonical_text(payload)
    assert '"precluded"' not in choice_request(payload).state_text


def test_with_a_blocker_the_filtered_offer_is_explicit_in_the_model_text(
    repository, verifier, clock, state, policy
):
    learn(repository, verifier, clock, state, policy)
    decider = ScriptedDecider([ScriptedChoice(selected_id=PIPELINE)])
    run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        ScriptedExecutor(),
        request(state, key="call-2"),
    )
    payload = decider.requests[0]
    assert payload.model_dump(mode="json")["precluded"][0]["public_tool_id"] == SCHEMA
    rendered = choice_request(payload)
    assert rendered.option_ids == (PIPELINE, SCOPE, "request_review")


def test_one_read_budget_precluded_then_no_progress_d1_stops_without_d2(
    repository, verifier, clock, state, policy
):
    learn(repository, verifier, clock, state, policy)
    decider = ScriptedDecider(
        [ScriptedChoice(selected_id=PIPELINE), ScriptedChoice(selected_id=SCOPE)]
    )
    executor = ScriptedExecutor(outcomes={PIPELINE: STALL})
    worker = engine(policy, repository, verifier, clock, decider, executor)
    worker.max_diagnostic_reads = 1
    result = asyncio.run(worker.resolve(request(state, key="call-2")))
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.DIAGNOSTICS_EXHAUSTED
    assert len(decider.requests) == 1
    assert result.consumed.decider_calls == 1
    assert result.consumed.diagnostic_reads == 1


def test_precluded_then_no_progress_d1_opens_one_d2_without_the_dead_end(
    repository, verifier, clock, state, policy
):
    """The post-D1 same-call path is unchanged and still bounded at two."""
    learn(repository, verifier, clock, state, policy)
    decider = ScriptedDecider(
        [ScriptedChoice(selected_id=PIPELINE), ScriptedChoice(selected_id=SCOPE)]
    )
    executor = ScriptedExecutor(outcomes={PIPELINE: STALL})
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, key="call-2"),
    )
    assert result.path == "rechosen"
    assert result.consumed.decider_calls == 2
    assert result.consumed.diagnostic_reads == 2
    assert [item.blocked_class for item in result.blocked] == ["failed_execution"]
    assert [item.public_tool_id for item in result.precluded] == [SCHEMA]
    final = decider.requests[1]
    assert final.pass_index == 2
    assert PIPELINE not in final.available_tool_ids
    # D2 reasons over the refreshed context, and the preclusion matched
    # against the call's original context still stands: the proven dead end
    # is never re-offered within the call.
    assert final.decision_context_hash != decider.requests[0].decision_context_hash
    assert final.precluded == result.precluded
    assert final.available_tool_ids == (SCOPE,)
    assert [item.public_tool_id for item in executor.requests] == [PIPELINE, SCOPE]


def test_a_dead_end_and_a_failed_d1_leave_nothing_and_ask_no_d2(
    repository, verifier, clock, make_state, policy
):
    state = make_state(candidate_count=2)
    learn(repository, verifier, clock, state, policy, tool=SCHEMA)
    decider = ScriptedDecider(
        [ScriptedChoice(selected_id=PIPELINE), ScriptedChoice(selected_id=SCHEMA)]
    )
    executor = ScriptedExecutor(outcomes={PIPELINE: STALL})
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, key="call-2"),
    )
    assert result.path == "unresolved"
    assert result.unresolved_reason is Reason.INSUFFICIENT
    assert len(decider.requests) == 1
    assert [item.public_tool_id for item in executor.requests] == [PIPELINE]


def test_preclusion_never_raises_the_two_call_bound(
    repository, verifier, clock, state, policy
):
    learn(repository, verifier, clock, state, policy)
    decider = ScriptedDecider(
        [ScriptedChoice(selected_id=PIPELINE), ScriptedChoice(selected_id=SCOPE)]
    )
    executor = ScriptedExecutor(default=STALL)
    result = run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        executor,
        request(state, key="call-2"),
    )
    assert result.path == "unresolved"
    assert result.consumed.decider_calls == 2
    assert result.consumed.diagnostic_reads == 2
    assert len(decider.requests) == 2


def test_a_selector_input_refuses_an_available_precluded_tool(
    repository, verifier, clock, state, policy
):
    learn(repository, verifier, clock, state, policy)
    decider = ScriptedDecider([ScriptedChoice(selected_id=PIPELINE)])
    run(
        policy,
        repository,
        verifier,
        clock,
        decider,
        ScriptedExecutor(),
        request(state, key="call-2"),
    )
    payload = decider.requests[0]
    document = payload.model_dump()
    with pytest.raises(ValueError, match="precluded"):
        type(payload).model_validate(
            document | {"available_tool_ids": (PIPELINE, SCHEMA, SCOPE)}
        )
    failed = payload.precluded[0].model_copy(
        update={"blocked_class": "failed_execution"}
    )
    with pytest.raises(ValueError, match="precluded"):
        type(payload).model_validate(document | {"precluded": (failed,)})
