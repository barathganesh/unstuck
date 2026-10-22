"""Adapter tests for the DynamoDB run-state repository. Offline and stubbed.

Every request below is asserted against `botocore.stub.Stubber`. A stub that
returns success is not evidence of a deployed grant: what these tests prove is
the request shape and the conditional algebra, not AWS behaviour. PF6 remains
Pending.
"""

import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from unstuck.clock import FakeClock
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.dynamodb import (
    DynamoDbRunRepository,
    derive_run_id,
    event_item,
    initial_snapshot,
    meta_item,
)
from unstuck.cloud.session import (
    ACQUIRE_CONDITION,
    EVENT_CONDITION,
    WRITE_CONDITION,
)
from unstuck.domain import Reason, RunState, RuntimeFault, TaskSpec
from unstuck.storage import RunRepository, Snapshot

from .conftest import SYNTHETIC_RUN_TABLE, StubbedClient

IDEMPOTENCY_KEY = "offline-case-01"


def repository(
    stub: StubbedClient, config: CloudResourceConfig, clock: FakeClock
) -> DynamoDbRunRepository:
    return DynamoDbRunRepository(stub.client, config, clock=clock)


def created(task: TaskSpec) -> Snapshot:
    return initial_snapshot(task, "v17", IDEMPOTENCY_KEY)


def leased(
    snapshot: Snapshot, owner: UUID, clock: FakeClock, *, age: float
) -> Snapshot:
    """A running snapshot whose lease expired `age` seconds ago in UTC."""
    state = RunState.model_validate(
        snapshot.state.model_dump()
        | {
            "status": "running",
            "lease_owner": owner,
            "lease_expires_at": clock.monotonic() - age,
            "lease_expires_utc": clock.utcnow() - timedelta(seconds=age),
            "lease_clock_epoch": uuid4(),
            "run_deadline_at": clock.monotonic() + 120.0,
        }
    )
    return Snapshot.model_validate(snapshot.model_dump() | {"state": state})


def meta_response(snapshot: Snapshot) -> dict[str, Any]:
    return {"Item": meta_item(snapshot)}


def events_response(snapshot: Snapshot) -> dict[str, Any]:
    return {"Items": [event_item(event) for event in snapshot.events]}


def test_run_repository_satisfies_the_existing_protocol_offline(
    dynamo: StubbedClient, config: CloudResourceConfig, clock: FakeClock
) -> None:
    adapter: RunRepository = repository(dynamo, config, clock)
    assert adapter is not None
    assert derive_run_id(IDEMPOTENCY_KEY) == derive_run_id(IDEMPOTENCY_KEY)
    assert derive_run_id(IDEMPOTENCY_KEY) != derive_run_id("offline-case-02")


def test_create_run_writes_meta_once_conditionally_offline(
    dynamo: StubbedClient,
    config: CloudResourceConfig,
    clock: FakeClock,
    task: TaskSpec,
) -> None:
    dynamo.stubber.add_response("put_item", {})
    snapshot = repository(dynamo, config, clock).create_run(
        task, "v17", IDEMPOTENCY_KEY
    )
    request = dynamo.only("PutItem")
    assert request["TableName"] == SYNTHETIC_RUN_TABLE
    assert request["ConditionExpression"] == "attribute_not_exists(SK)"
    assert request["Item"]["PK"] == {"S": f"RUN#{snapshot.state.run_id}"}
    assert request["Item"]["SK"] == {"S": "META"}
    assert request["Item"]["status"] == {"S": "created"}
    assert request["Item"]["state_version"] == {"N": "0"}
    assert request["Item"]["task_hash"] == {"S": task.task_hash}
    assert "lease_owner" not in request["Item"]
    assert "events" not in json.loads(request["Item"]["body"]["S"])
    dynamo.stubber.assert_no_pending_responses()


def test_create_run_resumes_a_duplicate_without_retrying_offline(
    dynamo: StubbedClient,
    config: CloudResourceConfig,
    clock: FakeClock,
    task: TaskSpec,
) -> None:
    existing = created(task)
    dynamo.stubber.add_client_error(
        "put_item", service_error_code="ConditionalCheckFailedException"
    )
    dynamo.stubber.add_response("get_item", meta_response(existing))
    dynamo.stubber.add_response("query", events_response(existing))
    resumed = repository(dynamo, config, clock).create_run(task, "v17", IDEMPOTENCY_KEY)
    assert resumed == existing
    assert len(dynamo.params("PutItem")) == 1
    dynamo.stubber.assert_no_pending_responses()


def test_create_run_refuses_a_conflicting_task_offline(
    dynamo: StubbedClient,
    config: CloudResourceConfig,
    clock: FakeClock,
    task: TaskSpec,
) -> None:
    other = TaskSpec(dataset_id="ds-18", order_date_utc=task.order_date_utc)
    dynamo.stubber.add_client_error(
        "put_item", service_error_code="ConditionalCheckFailedException"
    )
    dynamo.stubber.add_response("get_item", meta_response(created(other)))
    dynamo.stubber.add_response("query", {"Items": []})
    with pytest.raises(RuntimeFault) as raised:
        repository(dynamo, config, clock).create_run(task, "v17", IDEMPOTENCY_KEY)
    assert raised.value.reason is Reason.TASK_MISMATCH


def test_acquire_uses_the_design_lease_condition_offline(
    dynamo: StubbedClient,
    config: CloudResourceConfig,
    clock: FakeClock,
    task: TaskSpec,
) -> None:
    owner = uuid4()
    dynamo.stubber.add_response("get_item", meta_response(created(task)))
    dynamo.stubber.add_response("query", {"Items": []})
    dynamo.stubber.add_response("transact_write_items", {})
    snapshot = repository(dynamo, config, clock).acquire(
        derive_run_id(IDEMPOTENCY_KEY), owner
    )
    assert snapshot is not None
    assert snapshot.state.status == "running"
    assert snapshot.state.lease_owner == owner
    assert snapshot.state.lease_expires_at == clock.monotonic() + 150.0
    assert snapshot.events[-1].type == "run_started"
    items = dynamo.only("TransactWriteItems")["TransactItems"]
    update, put = items[0]["Update"], items[1]["Put"]
    assert update["ConditionExpression"] == ACQUIRE_CONDITION
    assert update["ExpressionAttributeNames"]["#status"] == "status"
    assert update["ExpressionAttributeValues"][":created"] == {"S": "created"}
    assert put["ConditionExpression"] == EVENT_CONDITION
    assert put["Item"]["SK"] == {"S": "EVENT#000001"}
    dynamo.stubber.assert_no_pending_responses()


def test_acquire_refuses_a_duplicate_invocation_without_writing_offline(
    dynamo: StubbedClient,
    config: CloudResourceConfig,
    clock: FakeClock,
    task: TaskSpec,
) -> None:
    held = leased(created(task), uuid4(), clock, age=-100.0)
    dynamo.stubber.add_response("get_item", meta_response(held))
    dynamo.stubber.add_response("query", events_response(held))
    assert (
        repository(dynamo, config, clock).acquire(
            derive_run_id(IDEMPOTENCY_KEY), uuid4()
        )
        is None
    )
    assert dynamo.params("TransactWriteItems") == []
    assert dynamo.params("UpdateItem") == []


def test_expired_non_terminal_lease_only_writes_failed_offline(
    dynamo: StubbedClient,
    config: CloudResourceConfig,
    clock: FakeClock,
    task: TaskSpec,
) -> None:
    owner = uuid4()
    expired = leased(created(task), owner, clock, age=10.0)
    dynamo.stubber.add_response("get_item", meta_response(expired))
    dynamo.stubber.add_response("query", events_response(expired))
    dynamo.stubber.add_response("transact_write_items", {})
    assert (
        repository(dynamo, config, clock).acquire(
            derive_run_id(IDEMPOTENCY_KEY), uuid4()
        )
        is None
    )
    items = dynamo.only("TransactWriteItems")["TransactItems"]
    update, put = items[0]["Update"], items[1]["Put"]
    assert update["ConditionExpression"] == WRITE_CONDITION
    assert update["ExpressionAttributeValues"][":owner"] == {"S": str(owner)}
    assert update["ExpressionAttributeValues"][":status"] == {"S": "failed"}
    body = json.loads(update["ExpressionAttributeValues"][":body"]["S"])
    assert body["state"]["reason_code"] == "lease_expired_incomplete"
    assert put["Item"]["SK"] == {"S": "EVENT#000001"}
    dynamo.stubber.assert_no_pending_responses()


def test_record_writes_conditional_on_lease_and_state_version_offline(
    dynamo: StubbedClient,
    config: CloudResourceConfig,
    clock: FakeClock,
    task: TaskSpec,
) -> None:
    owner = uuid4()
    dynamo.stubber.add_response("get_item", meta_response(created(task)))
    dynamo.stubber.add_response("query", {"Items": []})
    dynamo.stubber.add_response("transact_write_items", {})
    adapter = repository(dynamo, config, clock)
    running = adapter.acquire(derive_run_id(IDEMPOTENCY_KEY), owner)
    assert running is not None
    dynamo.stubber.add_response("transact_write_items", {})
    updated = adapter.record(running, owner, "query_observed", {"mode": "offline"})
    assert updated.state.state_version == running.state.state_version + 1
    update, put = (
        dynamo.params("TransactWriteItems")[1]["TransactItems"][0]["Update"],
        dynamo.params("TransactWriteItems")[1]["TransactItems"][1]["Put"],
    )
    assert update["ConditionExpression"] == WRITE_CONDITION
    assert update["ExpressionAttributeValues"][":owner"] == {"S": str(owner)}
    assert update["ExpressionAttributeValues"][":expected"] == {
        "N": str(running.state.state_version)
    }
    assert put["ConditionExpression"] == EVENT_CONDITION
    assert put["Item"]["SK"] == {"S": "EVENT#000002"}
    dynamo.stubber.assert_no_pending_responses()


def test_finish_maps_the_terminal_reason_without_mutation_offline(
    dynamo: StubbedClient,
    config: CloudResourceConfig,
    clock: FakeClock,
    task: TaskSpec,
) -> None:
    owner = uuid4()
    dynamo.stubber.add_response("get_item", meta_response(created(task)))
    dynamo.stubber.add_response("query", {"Items": []})
    dynamo.stubber.add_response("transact_write_items", {})
    adapter = repository(dynamo, config, clock)
    running = adapter.acquire(derive_run_id(IDEMPOTENCY_KEY), owner)
    assert running is not None
    dynamo.stubber.add_response("transact_write_items", {})
    final = adapter.finish(running, owner, Reason.INSUFFICIENT, {})
    assert final.state.reason_code is Reason.INSUFFICIENT
    assert final.state.status == "unresolved"
    assert final.incident.state == "UNRESOLVED"
    assert final.result is not None
    assert final.result["reason_code"] == "insufficient_evidence"
    update = dynamo.params("TransactWriteItems")[1]["TransactItems"][0]["Update"]
    assert update["ExpressionAttributeValues"][":status"] == {"S": "unresolved"}
    dynamo.stubber.assert_no_pending_responses()


def test_a_conditional_failure_raises_storage_error_without_retry_offline(
    dynamo: StubbedClient,
    config: CloudResourceConfig,
    clock: FakeClock,
    task: TaskSpec,
) -> None:
    owner = uuid4()
    dynamo.stubber.add_response("get_item", meta_response(created(task)))
    dynamo.stubber.add_response("query", {"Items": []})
    dynamo.stubber.add_response("transact_write_items", {})
    adapter = repository(dynamo, config, clock)
    running = adapter.acquire(derive_run_id(IDEMPOTENCY_KEY), owner)
    assert running is not None
    dynamo.stubber.add_client_error(
        "transact_write_items", service_error_code="TransactionCanceledException"
    )
    with pytest.raises(RuntimeFault) as raised:
        adapter.record(running, owner, "query_observed", {"mode": "offline"})
    assert raised.value.reason is Reason.STORAGE_ERROR
    assert len(dynamo.params("TransactWriteItems")) == 2
    dynamo.stubber.assert_no_pending_responses()


def test_the_adapter_never_touches_the_filesystem_offline(
    dynamo: StubbedClient, config: CloudResourceConfig, clock: FakeClock
) -> None:
    adapter = repository(dynamo, config, clock)
    with pytest.raises(RuntimeFault) as raised:
        adapter.transitions.load_run(derive_run_id(IDEMPOTENCY_KEY))
    assert raised.value.reason is Reason.STORAGE_ERROR
