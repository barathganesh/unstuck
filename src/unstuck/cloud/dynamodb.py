"""DynamoDB run-state adapter behind the existing `RunRepository` protocol.

`docs/spec/design.md` sections 11 and 16 fix the shape. One table holds
`RUN#<run_id>` partitions: a `META` item carrying the run body and the
attributes every condition reads, plus one `EVENT#<zero-padded seq>` item per
event. The `META` body deliberately excludes events, so a long run cannot grow
one item past its limit; `load_run` rebuilds the snapshot from both.

Every write is conditional and every conditional failure is final. The lease is
acquired by one conditional update, an event is written with
`attribute_not_exists(SK)`, and a run body is updated only while the caller
still holds the lease at the expected `state_version`. A failed condition
raises the storage reason code: there is no retry, no cache, and no expiry
rule anywhere in this module.

The snapshot transition arithmetic is reused from
`unstuck.storage.LocalRunRepository` rather than reimplemented, so there is one
definition of the snapshot invariants. The reused object never initializes the
parent's filesystem state and refuses its locking entry point outright.
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from botocore.exceptions import ClientError
from pydantic import JsonValue, ValidationError

from unstuck.clock import Clock, SystemClock
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.keys import EVENT_SORT_PREFIX, DynamoKey
from unstuck.cloud.session import (
    ACQUIRE_CONDITION,
    EVENT_CONDITION,
    WRITE_CONDITION,
    LeaseTerms,
    lease_outcome,
)
from unstuck.decision import DecisionHistory, SelectionAuthority
from unstuck.domain import (
    EvidenceRecord,
    Incident,
    QuerySpec,
    Reason,
    ReportVerification,
    RunState,
    RuntimeFault,
    Settings,
    TaskSpec,
    canonical_hash,
)
from unstuck.events import EventRecord, PersistenceBoundary, Redactor
from unstuck.storage import LocalRunRepository, Snapshot, recorded_selections

AttributeValue = dict[str, str]
Item = dict[str, AttributeValue]

META_ABSENT_CONDITION = "attribute_not_exists(SK)"
EVENT_QUERY = "PK = :pk AND begins_with(SK, :prefix)"
ACQUIRE_EXPRESSION = (
    "SET body = :body, #status = :status, lease_owner = :owner, "
    "state_version = :version"
)
WRITE_EXPRESSION = "SET body = :body, #status = :status, state_version = :version"
STATUS_NAMES = {"#status": "status"}

#: Codes that mean a condition was not met. None of them is retried.
CONDITION_CODES = frozenset(
    {
        "ConditionalCheckFailedException",
        "TransactionCanceledException",
        "TransactionConflictException",
    }
)


class DynamoClient(Protocol):
    """The narrow surface this adapter uses. Always injected, never built."""

    def put_item(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def get_item(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def update_item(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def query(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def transact_write_items(self, **kwargs: Any) -> Mapping[str, Any]: ...


def error_code(error: ClientError) -> str:
    response: Mapping[str, Any] = getattr(error, "response", {}) or {}
    details: Mapping[str, Any] = response.get("Error", {}) or {}
    return str(details.get("Code", ""))


def condition_failed(error: ClientError) -> bool:
    return error_code(error) in CONDITION_CODES


def derive_run_id(idempotency_key: str) -> UUID:
    """The run identity is the idempotency key, so a duplicate resumes."""
    if not idempotency_key or len(idempotency_key) > 128:
        raise RuntimeFault(Reason.INVARIANT)
    return uuid5(NAMESPACE_URL, canonical_hash(idempotency_key))


def initial_snapshot(
    task: TaskSpec, dataset_version: str, idempotency_key: str
) -> Snapshot:
    """The `created` snapshot a first invocation writes. No lease yet."""
    run_id = derive_run_id(idempotency_key)
    query = QuerySpec(task=task, dataset_version=dataset_version)
    return Snapshot(
        authority_version="2",
        selection=None,
        verification=None,
        state=RunState(run_id=run_id, task=task, dataset_version=dataset_version),
        query=query,
        incident=Incident.open(run_id, query),
    )


def snapshot_body(snapshot: Snapshot) -> str:
    """The canonical `META` body: everything except the event log."""
    body = snapshot.model_dump(mode="json")
    body.pop("events", None)
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def meta_item(snapshot: Snapshot) -> Item:
    """The `META` item, carrying every attribute a condition reads."""
    state = snapshot.state
    item: Item = {
        **DynamoKey.run_meta(state.run_id).item,
        "body": {"S": snapshot_body(snapshot)},
        "task_hash": {"S": state.task_hash},
        "status": {"S": state.status},
        "state_version": {"N": str(state.state_version)},
        "dataset_version": {"S": state.dataset_version},
    }
    if state.lease_owner is not None:
        item["lease_owner"] = {"S": str(state.lease_owner)}
    return item


def event_item(event: EventRecord) -> Item:
    return {
        **DynamoKey.run_event(event.run_id, event.sequence).item,
        "body": {"S": event.model_dump_json()},
    }


def snapshot_from_items(
    meta: Mapping[str, Any], events: Sequence[Mapping[str, Any]]
) -> Snapshot:
    """Rebuild one snapshot from its `META` body and its event items."""
    try:
        body = json.loads(meta["body"]["S"])
        records = [json.loads(item["body"]["S"]) for item in events]
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeFault(Reason.STORAGE_ERROR) from error
    records.sort(key=lambda record: record.get("sequence", 0))
    try:
        # The stored body is JSON, so it is revalidated as JSON: the strict
        # contracts accept a serialized UUID or datetime only on that path.
        return Snapshot.model_validate_json(json.dumps(body | {"events": records}))
    except (TypeError, ValueError, ValidationError) as error:
        raise RuntimeFault(Reason.STORAGE_ERROR) from error


class _Transitions(LocalRunRepository):
    """Snapshot transition arithmetic only. No filesystem path is reachable.

    The parent's filesystem state is intentionally never initialized and its
    locking entry point is overridden to refuse, so the only usable methods are
    the pure ones that need nothing but the clock and the redactor.
    """

    def __init__(self, clock: Clock, redactor: Redactor) -> None:
        self.clock = clock
        self.redactor = redactor

    @contextmanager
    def _locked(self, run_id: UUID) -> Iterator[Path]:
        raise RuntimeFault(Reason.STORAGE_ERROR)
        yield Path()  # pragma: no cover - unreachable refusal guard

    def append(
        self,
        snapshot: Snapshot,
        event_type: str,
        payload: dict[str, JsonValue],
        **changes: object,
    ) -> Snapshot:
        return self._append(snapshot, event_type, payload, **changes)

    def terminal(
        self, snapshot: Snapshot, reason: Reason, payload: dict[str, JsonValue]
    ) -> Snapshot:
        return self._terminal(snapshot, reason, payload)

    def lease_expired(self, state: RunState) -> bool:
        return self._lease_expired(state)


class DynamoDbRunRepository:
    """`RunRepository` over DynamoDB. Conditional writes, no retry, no cache."""

    def __init__(
        self,
        client: DynamoClient,
        config: CloudResourceConfig,
        *,
        clock: Clock | None = None,
        redactor: Redactor | None = None,
        terms: LeaseTerms | None = None,
    ):
        self.client = client
        self.config = config
        self.clock = clock or SystemClock()
        self.redactor = redactor or Redactor()
        self.terms = terms or LeaseTerms()
        self.transitions = _Transitions(self.clock, self.redactor)
        self.boundary = PersistenceBoundary(self.redactor)

    @property
    def table(self) -> str:
        return self.config.run_table_name

    def create_run(
        self, task: TaskSpec, dataset_version: str, idempotency_key: str
    ) -> Snapshot:
        self.boundary.validate((task, dataset_version), Reason.TASK_MISMATCH)
        snapshot = initial_snapshot(task, dataset_version, idempotency_key)
        try:
            self.client.put_item(
                TableName=self.table,
                Item=meta_item(snapshot),
                ConditionExpression=META_ABSENT_CONDITION,
            )
        except ClientError as error:
            if not condition_failed(error):
                raise RuntimeFault(Reason.STORAGE_ERROR) from error
            existing = self.load_run(snapshot.state.run_id)
            if (
                existing.state.task_hash != task.task_hash
                or existing.state.dataset_version != dataset_version
            ):
                raise RuntimeFault(Reason.TASK_MISMATCH) from error
            return existing
        return snapshot

    def load_run(self, run_id: UUID) -> Snapshot:
        key = DynamoKey.run_meta(run_id)
        try:
            meta = self.client.get_item(
                TableName=self.table, Key=key.item, ConsistentRead=True
            ).get("Item")
            if not meta:
                raise RuntimeFault(Reason.STORAGE_ERROR)
            events = self.client.query(
                TableName=self.table,
                KeyConditionExpression=EVENT_QUERY,
                ExpressionAttributeValues={
                    ":pk": {"S": key.partition},
                    ":prefix": {"S": EVENT_SORT_PREFIX},
                },
                ConsistentRead=True,
            )
        except ClientError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        return snapshot_from_items(meta, list(events.get("Items", ())))

    def acquire(
        self, run_id: UUID, owner: UUID, *, run_seconds: float = 120.0
    ) -> Snapshot | None:
        self.boundary.validate(owner)
        seconds = Settings(run_seconds=run_seconds).run_seconds
        snapshot = self.load_run(run_id)
        state = snapshot.state
        decision = lease_outcome(
            status=state.status,
            lease_owner=state.lease_owner,
            expired=state.lease_owner is not None
            and self.transitions.lease_expired(state),
        )
        if decision.action == "reject":
            return None
        if decision.action == "fail_expired":
            final = self.transitions.terminal(snapshot, Reason.LEASE_EXPIRED, {})
            self._commit_write(final, state.lease_owner, state.state_version)
            return None
        leased = Snapshot.model_validate(
            snapshot.model_dump()
            | {
                "state": RunState.model_validate(
                    state.model_dump()
                    | {
                        "status": "running",
                        "lease_owner": owner,
                        "lease_expires_at": self.clock.monotonic()
                        + self.terms.lease_seconds,
                        "lease_expires_utc": self.clock.utcnow()
                        + timedelta(seconds=self.terms.lease_seconds),
                        "lease_clock_epoch": self.clock.epoch,
                        "run_deadline_at": self.clock.monotonic() + seconds,
                    }
                )
            }
        )
        leased = self.transitions.append(
            leased, "run_started", {"mode": "offline", "provider": "mock"}
        )
        self._commit_acquire(leased, owner)
        return leased

    def record(
        self,
        snapshot: Snapshot,
        owner: UUID,
        event_type: str,
        payload: dict[str, JsonValue],
        *,
        query: QuerySpec | None = None,
        incident: Incident | None = None,
        evidence: tuple[EvidenceRecord, ...] | None = None,
        selection: SelectionAuthority | None = None,
        history: DecisionHistory | None = None,
        verification: ReportVerification | None = None,
    ) -> Snapshot:
        if event_type == "run_finished":
            raise RuntimeFault(Reason.INVARIANT)
        self.boundary.validate(event_type)
        self.boundary.validate(snapshot)
        self.boundary.validate(
            (query, incident, evidence, verification), Reason.CONFLICT
        )
        self.boundary.validate(selection, Reason.FIRST_REJECTED)
        self.boundary.validate(history, Reason.RECEIPT_INVALID)
        self._authorize(snapshot, owner)
        changed = Snapshot.model_validate(
            snapshot.model_dump()
            | {
                "query": query or snapshot.query,
                "incident": incident or snapshot.incident,
                "evidence": snapshot.evidence if evidence is None else evidence,
                "history": history or snapshot.history,
                "verification": verification
                or (
                    None
                    if query is not None or event_type == "query_observed"
                    else snapshot.verification
                ),
            }
            # The durable record keeps the first and the latest decision apart
            # wherever a run is stored, through the one shared router.
            | recorded_selections(snapshot, selection)
        )
        updated = self.transitions.append(changed, event_type, payload)
        self._commit_write(updated, owner, snapshot.state.state_version)
        return updated

    def finish(
        self,
        snapshot: Snapshot,
        owner: UUID,
        reason: Reason,
        payload: dict[str, JsonValue],
    ) -> Snapshot:
        self._authorize(snapshot, owner)
        final = self.transitions.terminal(snapshot, reason, payload)
        self._commit_write(final, owner, snapshot.state.state_version)
        return final

    def _authorize(self, snapshot: Snapshot, owner: UUID) -> None:
        state = snapshot.state
        if (
            state.status != "running"
            or state.lease_owner != owner
            or state.lease_clock_epoch != self.clock.epoch
            or self.transitions.lease_expired(state)
        ):
            raise RuntimeFault(Reason.STORAGE_ERROR)

    def _check_deadline(self, snapshot: Snapshot) -> None:
        if snapshot.state.status in {"completed", "empty_verified"} and (
            snapshot.state.run_deadline_at is None
            or self.clock.monotonic() >= snapshot.state.run_deadline_at
        ):
            raise RuntimeFault(Reason.DEADLINE)

    def _commit_acquire(self, snapshot: Snapshot, owner: UUID) -> None:
        self._transact(
            snapshot,
            update={
                "TableName": self.table,
                "Key": DynamoKey.run_meta(snapshot.state.run_id).item,
                "UpdateExpression": ACQUIRE_EXPRESSION,
                "ConditionExpression": ACQUIRE_CONDITION,
                "ExpressionAttributeNames": dict(STATUS_NAMES),
                "ExpressionAttributeValues": {
                    ":body": {"S": snapshot_body(snapshot)},
                    ":status": {"S": snapshot.state.status},
                    ":owner": {"S": str(owner)},
                    ":version": {"N": str(snapshot.state.state_version)},
                    ":created": {"S": "created"},
                },
            },
        )

    def _commit_write(
        self, snapshot: Snapshot, owner: UUID | None, expected: int
    ) -> None:
        if owner is None:
            raise RuntimeFault(Reason.STORAGE_ERROR)
        self._transact(
            snapshot,
            update={
                "TableName": self.table,
                "Key": DynamoKey.run_meta(snapshot.state.run_id).item,
                "UpdateExpression": WRITE_EXPRESSION,
                "ConditionExpression": WRITE_CONDITION,
                "ExpressionAttributeNames": dict(STATUS_NAMES),
                "ExpressionAttributeValues": {
                    ":body": {"S": snapshot_body(snapshot)},
                    ":status": {"S": snapshot.state.status},
                    ":version": {"N": str(snapshot.state.state_version)},
                    ":owner": {"S": str(owner)},
                    ":expected": {"N": str(expected)},
                },
            },
        )

    def _transact(self, snapshot: Snapshot, *, update: dict[str, Any]) -> None:
        Snapshot.model_validate(snapshot)
        self.boundary.validate(snapshot)
        self._check_deadline(snapshot)
        if not snapshot.events:
            raise RuntimeFault(Reason.INVARIANT)
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {"Update": update},
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": event_item(snapshot.events[-1]),
                            "ConditionExpression": EVENT_CONDITION,
                        }
                    },
                ]
            )
        except ClientError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error


__all__ = [
    "ACQUIRE_EXPRESSION",
    "CONDITION_CODES",
    "DynamoClient",
    "DynamoDbRunRepository",
    "Item",
    "META_ABSENT_CONDITION",
    "WRITE_EXPRESSION",
    "condition_failed",
    "derive_run_id",
    "error_code",
    "event_item",
    "initial_snapshot",
    "meta_item",
    "snapshot_body",
    "snapshot_from_items",
]
