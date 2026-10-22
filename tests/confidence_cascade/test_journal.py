"""The durable-record seam between the cascade and its owning caller.

The cascade owns every decision; the caller owns every durable write. The
journal is the only channel between them, it is called at the moment each
allowance or verified fact becomes final, and a refusal on that channel stops
the cascade instead of hiding an attempt.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_journal.py -q
"""

import asyncio

import pytest
from test_same_call import engine, request

from unstuck.cascade.engine import SilentJournal
from unstuck.cascade.offline import (
    ScriptedChoice,
    ScriptedDecider,
    ScriptedExecutor,
    ScriptedOutcome,
)
from unstuck.cascade.policy import CascadePolicy
from unstuck.domain import Reason, RuntimeFault

STALL = ScriptedOutcome(changed=False)
SCHEMA = "inspect_schema_keys"
PIPELINE = "inspect_pipeline"


class Recorder:
    """Records the journal calls in the order the cascade makes them."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def history(self, result, problem_fingerprint):
        assert len(problem_fingerprint) == 64
        self.entries.append(("history", result.status))

    def requested(self, payload):
        self.entries.append(("requested", str(payload.pass_index)))

    def returned(self, record, result, blocked):
        self.entries.append(("returned", str(record.pass_index)))

    def refused(self, execution):
        self.entries.append(("refused", execution.blocked_class))

    def precluded(self, execution):
        self.entries.append(("precluded", execution.blocked_class))

    def admitted(self, receipt, stored):
        self.entries.append(("admitted", receipt.outcome_class))


class Refusing(Recorder):
    """Fails the durable write of the first selector attempt."""

    def requested(self, payload):
        super().requested(payload)
        raise RuntimeFault(Reason.STORAGE_ERROR)


def two_pass() -> ScriptedDecider:
    return ScriptedDecider(
        [ScriptedChoice(selected_id=SCHEMA), ScriptedChoice(selected_id=PIPELINE)]
    )


def test_journal_records_every_allowance_and_fact_in_cascade_order(
    repository, verifier, clock, state, policy
):
    recorder = Recorder()
    decider = two_pass()
    worker = engine(
        policy,
        repository,
        verifier,
        clock,
        decider,
        ScriptedExecutor(outcomes={SCHEMA: STALL}),
    )
    result = asyncio.run(worker.resolve(request(state, key="call-1"), journal=recorder))
    assert result.path == "rechosen"
    assert recorder.entries == [
        ("history", "empty"),
        ("requested", "1"),
        ("returned", "1"),
        ("admitted", "no_progress"),
        ("refused", "failed_execution"),
        ("requested", "2"),
        ("returned", "2"),
        ("admitted", "useful_evidence"),
    ]


def test_journal_records_an_exact_repetition_before_the_first_request(
    repository, verifier, clock, state, policy
):
    cold = engine(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)]),
        ScriptedExecutor(default=STALL),
    )
    assert asyncio.run(cold.resolve(request(state, key="call-1"))).path == "unresolved"
    recorder = Recorder()
    second = engine(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id=PIPELINE)]),
        ScriptedExecutor(),
    )
    result = asyncio.run(second.resolve(request(state, key="call-2"), journal=recorder))
    assert result.path == "direct"
    assert recorder.entries == [
        ("history", "matched"),
        ("precluded", "exact_repeat"),
        ("requested", "1"),
        ("returned", "1"),
        ("admitted", "useful_evidence"),
    ]


def test_every_invocation_journals_its_own_decisions(
    repository, verifier, clock, state, policy
):
    """This unit caches nothing, so a repeated invocation decides again.

    Duplicate invocations are resolved durably by the run store before a
    cascade is opened at all; see tests/core_runtime and tests/cloud_platform.
    """
    decider = ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)])
    worker = engine(policy, repository, verifier, clock, decider, ScriptedExecutor())
    task = request(state, key="call-1")
    first = asyncio.run(worker.resolve(task, journal=Recorder()))
    assert first.path == "direct"
    recorder = Recorder()
    again = asyncio.run(worker.resolve(task, journal=recorder))
    assert again.path == "direct"
    assert again.cascade_id == task.cascade_id
    assert [name for name, _ in recorder.entries] == [
        "history",
        "requested",
        "returned",
        "admitted",
    ]
    assert len(decider.requests) == 2


def test_a_refused_durable_write_stops_the_cascade_as_a_storage_failure(
    repository, verifier, clock, state, policy
):
    """A failure this unit does not own is raised, never relabelled.

    The journal belongs to the caller, so a refused durable write is the
    caller's storage failure. It reaches the caller as one, and the cascade
    stops before the provider call that attempt would have paid for.
    """
    refusing = Refusing()
    decider = two_pass()
    worker = engine(policy, repository, verifier, clock, decider, ScriptedExecutor())
    with pytest.raises(RuntimeFault) as error:
        asyncio.run(worker.resolve(request(state, key="call-1"), journal=refusing))
    assert error.value.reason is Reason.STORAGE_ERROR
    assert not decider.requests
    assert refusing.entries == [("history", "empty"), ("requested", "1")]
    assert repository.count(request(state, key="call-1").namespace) == 0


@pytest.mark.parametrize("arm", ["decider", "decider_memory"])
def test_the_silent_journal_records_nothing_and_resolves_identically(
    repository, verifier, clock, state, arm
):
    policy = CascadePolicy(baseline_id=arm, model_revision="scripted-v1")
    silent = SilentJournal()
    assert silent.history(None, "0" * 64) is None
    assert silent.requested(None) is None
    assert silent.returned(None, None, ()) is None
    assert silent.refused(None) is None
    assert silent.admitted(None, False) is None
    worker = engine(
        policy,
        repository,
        verifier,
        clock,
        ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)]),
        ScriptedExecutor(),
    )
    result = asyncio.run(worker.resolve(request(state, arm=arm, key="call-1")))
    assert result.path == "direct"
    assert result.selected_tool_id == SCHEMA


def test_the_one_pass_arm_never_opens_the_history_channel(
    repository, verifier, clock, state, one_pass_policy
):
    recorder = Recorder()
    worker = engine(
        repository=repository,
        verifier=verifier,
        clock=clock,
        policy=one_pass_policy,
        decider=ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)]),
        executor=ScriptedExecutor(),
    )
    result = asyncio.run(
        worker.resolve(request(state, arm="decider", key="call-1"), journal=recorder)
    )
    assert result.path == "direct"
    assert [name for name, _ in recorder.entries] == [
        "requested",
        "returned",
        "admitted",
    ]
    assert recorder.entries[-1] == ("admitted", "useful_evidence")
