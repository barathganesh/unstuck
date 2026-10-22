"""Deterministic offline Decision Receipt scenarios.

Two scenarios run entirely in process against scripted fakes and the synthetic
fixture:

* `cold` proves a cold namespace: the first decision chooses, the read makes no
  progress, a receipt is written, and one same-call second decision resolves it.
* `history` proves cross-call memory: call one records a no-progress receipt,
  call two retrieves it, precludes the identical repetition before any selector
  call, and resolves through one fresh first decision over what remains.

Everything here is synthetic and offline. No measurement taken against these
fakes is a latency, cost, or accuracy claim, and no live gate can be satisfied
from this module.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue

from unstuck.cascade.engine import CascadeEngine, CascadeRequest, CascadeResult
from unstuck.cascade.identity import CallIdentity
from unstuck.cascade.offline import (
    ScriptedChoice,
    ScriptedDecider,
    ScriptedExecutor,
    ScriptedOutcome,
)
from unstuck.cascade.policy import Arm, CascadePolicy
from unstuck.cascade.receipts import ReceiptNamespace
from unstuck.cascade.repository import LocalReceiptRepository
from unstuck.cascade.verifier import ReceiptVerifier
from unstuck.clock import SystemClock
from unstuck.decision import DecisionState, default_catalog
from unstuck.domain import Counters, QuerySpec, TaskSpec
from unstuck.fixtures import load_fixture

FIRST_CHOICE = "inspect_schema_keys"
SECOND_CHOICE = "inspect_pipeline"


def _state() -> DecisionState:
    fixture = load_fixture()
    task = TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )
    query = QuerySpec(task=task, dataset_version=fixture.dataset_version)
    return DecisionState(
        task=task,
        query=query,
        task_hash=task.task_hash,
        query_hash=query.query_hash,
        dataset_version=fixture.dataset_version,
        candidates=tuple(probe.public for probe in default_catalog()),
        budgets=Counters(),
        remaining_ms=120000,
    )


def _request(state: DecisionState, arm: Arm, key: str) -> CascadeRequest:
    return CascadeRequest(
        cascade_id=uuid4(),
        idempotency_key=key,
        namespace=ReceiptNamespace(arm=arm, case_id="scenario-01"),
        call=CallIdentity(
            run_id=uuid4(),
            request_id=uuid4(),
            session_id=uuid4(),
            trace_id=uuid4(),
            lease_owner=uuid4(),
            observed_at=datetime.now(UTC),
        ),
        state=state,
    )


def _engine(
    repository: LocalReceiptRepository,
    policy: CascadePolicy,
    choices: list[ScriptedChoice],
) -> CascadeEngine:
    clock = SystemClock()
    return CascadeEngine(
        decider=ScriptedDecider(choices),
        repository=repository,
        verifier=ReceiptVerifier(clock),
        clock=clock,
        policy=policy,
        provider_seconds=20.0,
    )


def _authority(
    state: DecisionState, outcomes: dict[str, ScriptedOutcome]
) -> ScriptedExecutor:
    """One scripted execution authority for one scenario invocation."""
    return ScriptedExecutor(outcomes=outcomes, state=state)


def _summary(name: str, result: CascadeResult) -> dict[str, JsonValue]:
    record = result.final or result.first
    return {
        "scenario": name,
        # Content-addressed identity is stable across runs; receipt identities
        # are call linked and therefore differ per invocation by design.
        "decision_context_hash": record.decision_context_hash if record else None,
        "mode": "offline",
        "provider": "mock",
        "classification": "synthetic",
        "baseline_id": result.baseline_id,
        "path": result.path,
        "selected_tool_id": result.selected_tool_id,
        "unresolved_reason": (
            result.unresolved_reason.value if result.unresolved_reason else None
        ),
        "history_status": result.history.status,
        "receipt_references": list(result.history.receipt_ids),
        "blocked": [
            {
                "public_tool_id": item.public_tool_id,
                "blocked_class": item.blocked_class,
                "reason_code": item.reason_code.value,
            }
            for item in result.blocked
        ],
        "precluded": [
            {
                "public_tool_id": item.public_tool_id,
                "blocked_class": item.blocked_class,
                "reason_code": item.reason_code.value,
            }
            for item in result.precluded
        ],
        "created_receipts": len(result.created_receipt_ids),
        "selector_calls": result.selector_calls,
        "diagnostic_reads": result.diagnostic_reads,
        "latency_claim": "unmeasured",
    }


async def cold_scenario() -> dict[str, JsonValue]:
    """A cold namespace resolved by one same-call second decision."""
    repository = LocalReceiptRepository()
    policy = CascadePolicy(baseline_id="decider_memory", model_revision="scripted-v1")
    engine = _engine(
        repository,
        policy,
        [
            ScriptedChoice(selected_id=FIRST_CHOICE),
            ScriptedChoice(selected_id=SECOND_CHOICE),
        ],
    )
    state = _state()
    result = await engine.resolve(
        _request(state, "decider_memory", "cold-1"),
        executor=_authority(state, {FIRST_CHOICE: ScriptedOutcome(changed=False)}),
    )
    return _summary("cold", result) | {"expected_path": "rechosen"}


async def history_scenario() -> dict[str, JsonValue]:
    """A second call that precludes the identical repetition before deciding."""
    repository = LocalReceiptRepository()
    policy = CascadePolicy(baseline_id="decider_memory", model_revision="scripted-v1")
    state = _state()
    first = _engine(repository, policy, [ScriptedChoice(selected_id=FIRST_CHOICE)])
    cold = await first.resolve(
        _request(state, "decider_memory", "history-1"),
        executor=_authority(state, {FIRST_CHOICE: ScriptedOutcome(changed=False)}),
    )
    # The first choice is no longer offered, so the one decision is the other.
    second = _engine(repository, policy, [ScriptedChoice(selected_id=SECOND_CHOICE)])
    result = await second.resolve(
        _request(state, "decider_memory", "history-2"),
        executor=_authority(state, {}),
    )
    return _summary("history", result) | {
        "expected_path": "direct",
        "first_call_path": cold.path,
        "first_call_receipts": len(cold.created_receipt_ids),
        "stored_receipts": repository.count(
            ReceiptNamespace(arm="decider_memory", case_id="scenario-01")
        ),
    }


async def run_scenarios() -> dict[str, JsonValue]:
    cold = await cold_scenario()
    history = await history_scenario()
    passed = (
        cold["path"] == "rechosen"
        and cold["history_status"] == "empty"
        and cold["diagnostic_reads"] == 2
        and history["path"] == "direct"
        and history["history_status"] == "matched"
        and history["selector_calls"] == 1
        and history["diagnostic_reads"] == 1
        and history["stored_receipts"] == 2
    )
    return {
        "status": "passed" if passed else "failed",
        "mode": "offline",
        "live_checks": "Pending",
        "scenarios": [cold, history],
    }


def main() -> int:
    result = asyncio.run(run_scenarios())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())


ScenarioName = Literal["cold", "history"]
