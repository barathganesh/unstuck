"""Cross-layer integration over the implemented cloud layers. Offline.

The real core-runtime controller runs against the DynamoDB adapter and the S3
artifact store through in-memory clients that enforce the same conditional
algebra a real table would. That is stronger than a queued stub for a whole
run, because the number of writes is the controller's choice rather than the
test's, and every condition is actually evaluated.

It is still offline. No AWS account, credential, socket, live provider, or
evaluator asset is reachable, so nothing here is evidence for a deployed grant.
"""

import ast
import asyncio
import copy
import importlib
import inspect
import json
from collections.abc import Mapping
from importlib import util
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from unstuck.cascade.receipts import ReceiptNamespace
from unstuck.cascade.repository import ReceiptWriteToken
from unstuck.cli import OfflineDecider, deploy
from unstuck.clock import FakeClock
from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.artifacts import S3ArtifactStore
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.dynamodb import (
    ACQUIRE_EXPRESSION,
    WRITE_EXPRESSION,
    DynamoDbRunRepository,
    derive_run_id,
)
from unstuck.cloud.preflight import SpendGateInputs, evaluate_spend_gate, run_preflight
from unstuck.cloud.receipts import (
    RECEIPT_ID_INDEX,
    TOMBSTONE_EXPRESSION,
    DynamoDbReceiptRepository,
)
from unstuck.cloud.runtime import (
    INVOCATIONS_PATH,
    ControllerRuntime,
    UnverifiedIdentity,
)
from unstuck.cloud.session import ACQUIRE_CONDITION, WRITE_CONDITION
from unstuck.cloud.synth import SynthesisInputs, template_for
from unstuck.controller import Controller
from unstuck.domain import TaskSpec
from unstuck.fixtures import load_fixture
from unstuck.query import EvidenceEngine
from unstuck.storage import Snapshot

from . import test_infrastructure as infrastructure

CLOUD_MODULES = (
    "unstuck.cloud",
    "unstuck.cloud.artifacts",
    "unstuck.cloud.config",
    "unstuck.cloud.dynamodb",
    "unstuck.cloud.guards",
    "unstuck.cloud.keys",
    "unstuck.cloud.preflight",
    "unstuck.cloud.receipts",
    "unstuck.cloud.runtime",
    "unstuck.cloud.session",
    "unstuck.cloud.synth",
    "unstuck.cloud.teardown",
)
#: Modules that can open a socket. Only the injected client layer may import
#: them, and only `botocore`'s exception and session surfaces are used.
OUTBOUND_MODULES = (
    "socket",
    "socketserver",
    "http",
    "http.client",
    "urllib",
    "urllib.request",
    "ssl",
    "asyncio",
    "requests",
    "httpx",
    "boto3",
)


def _error(code: str, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "offline in-memory client"}}, operation
    )


class InMemoryDynamo:
    """A DynamoDB-shaped client that evaluates the conditions it is handed."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.calls: list[str] = []

    @staticmethod
    def _key(table: str, key: Mapping[str, Any]) -> tuple[str, str, str]:
        return table, key["PK"]["S"], key["SK"]["S"]

    def put_item(
        self,
        *,
        TableName: str,
        Item: Mapping[str, Any],
        ConditionExpression: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append("PutItem")
        identity = (TableName, Item["PK"]["S"], Item["SK"]["S"])
        if ConditionExpression == "attribute_not_exists(SK)" and identity in self.items:
            raise _error("ConditionalCheckFailedException", "PutItem")
        self.items[identity] = dict(Item)
        return {}

    def get_item(
        self, *, TableName: str, Key: Mapping[str, Any], ConsistentRead: bool = False
    ) -> dict[str, Any]:
        self.calls.append("GetItem")
        item = self.items.get(self._key(TableName, Key))
        return {"Item": dict(item)} if item is not None else {}

    def query(
        self,
        *,
        TableName: str,
        KeyConditionExpression: str,
        ExpressionAttributeValues: Mapping[str, Any],
        IndexName: str | None = None,
        FilterExpression: str | None = None,
        ConsistentRead: bool = False,
        Limit: int | None = None,
        ExclusiveStartKey: Mapping[str, Any] | None = None,
        ReturnConsumedCapacity: str | None = None,
    ) -> dict[str, Any]:
        """Page the way DynamoDB does: `Limit` applies before the filter.

        A page can therefore come back empty while a later page holds live
        rows, which is the behavior a single-page lookup gets wrong.
        """
        self.calls.append("Query")
        if IndexName is not None:
            assert IndexName == RECEIPT_ID_INDEX
            sort = ExpressionAttributeValues[":sk"]["S"]
            rows = [
                dict(item)
                for (table, _, key), item in self.items.items()
                if table == TableName and key == sort
            ]
        else:
            partition = ExpressionAttributeValues[":pk"]["S"]
            prefix = ExpressionAttributeValues.get(":prefix", {}).get("S", "")
            rows = [
                dict(item)
                for (table, key, sort), item in self.items.items()
                if table == TableName and key == partition and sort.startswith(prefix)
            ]
        rows.sort(key=lambda row: str(row["SK"]["S"]))
        if ExclusiveStartKey is not None:
            resume = str(ExclusiveStartKey["SK"]["S"])
            rows = [row for row in rows if str(row["SK"]["S"]) > resume]
        scanned = rows if Limit is None else rows[:Limit]
        exhausted = len(scanned) == len(rows)
        page = scanned
        if FilterExpression == "attribute_not_exists(superseded)":
            page = [row for row in scanned if "superseded" not in row]
        response: dict[str, Any] = {
            "Items": page,
            "Count": len(page),
            "ScannedCount": len(scanned),
        }
        if ReturnConsumedCapacity == "TOTAL":
            response["ConsumedCapacity"] = {
                "TableName": TableName,
                "CapacityUnits": 0.5 * max(len(scanned), 1),
            }
        if not exhausted and scanned:
            last = scanned[-1]
            response["LastEvaluatedKey"] = {"PK": last["PK"], "SK": last["SK"]}
        return response

    def update_item(
        self,
        *,
        TableName: str,
        Key: Mapping[str, Any],
        UpdateExpression: str,
        ConditionExpression: str | None = None,
        ExpressionAttributeNames: Mapping[str, str] | None = None,
        ExpressionAttributeValues: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append("UpdateItem")
        item = self.items.get(self._key(TableName, Key))
        values = dict(ExpressionAttributeValues or {})
        if item is None:
            raise _error("ConditionalCheckFailedException", "UpdateItem")
        if UpdateExpression == TOMBSTONE_EXPRESSION:
            item["superseded"] = values[":superseded"]
            return {}
        if UpdateExpression == ACQUIRE_EXPRESSION:
            assert ConditionExpression == ACQUIRE_CONDITION
            if "lease_owner" in item or item["status"] != values[":created"]:
                raise _error("ConditionalCheckFailedException", "UpdateItem")
            item["lease_owner"] = values[":owner"]
        elif UpdateExpression == WRITE_EXPRESSION:
            assert ConditionExpression == WRITE_CONDITION
            if (
                item.get("lease_owner") != values[":owner"]
                or item["state_version"] != values[":expected"]
            ):
                raise _error("ConditionalCheckFailedException", "UpdateItem")
        else:  # pragma: no cover - the adapter writes no other expression
            raise AssertionError(UpdateExpression)
        item["body"] = values[":body"]
        item["status"] = values[":status"]
        item["state_version"] = values[":version"]
        return {}

    def transact_write_items(
        self, *, TransactItems: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        self.calls.append("TransactWriteItems")
        restore = copy.deepcopy(self.items)
        try:
            for entry in TransactItems:
                if "Update" in entry:
                    self.update_item(**entry["Update"])
                else:
                    self.put_item(**entry["Put"])
        except ClientError as error:
            self.items = restore
            raise _error(
                "TransactionCanceledException", "TransactWriteItems"
            ) from error
        return {}


class _Meta:
    endpoint_url = "https://s3.us-east-1.amazonaws.com"


class InMemoryS3:
    """An S3-shaped client bound to a TLS endpoint. Bodies stay in memory."""

    def __init__(self) -> None:
        self.meta = _Meta()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.requests: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(dict(kwargs))
        assert kwargs["ServerSideEncryption"] == "AES256"
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = bytes(kwargs["Body"])
        return {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(dict(kwargs))
        payload = self.objects[(kwargs["Bucket"], kwargs["Key"])]

        class Body:
            @staticmethod
            def read() -> bytes:
                return payload

        return {"Body": Body()}


@pytest.fixture
def dynamo_memory() -> InMemoryDynamo:
    return InMemoryDynamo()


@pytest.fixture
def s3_memory() -> InMemoryS3:
    return InMemoryS3()


def test_a_complete_run_is_written_through_the_adapters_under_one_lease_offline(
    dynamo_memory: InMemoryDynamo,
    s3_memory: InMemoryS3,
    config: CloudResourceConfig,
) -> None:
    fixture = load_fixture()
    clock = FakeClock()
    repository = DynamoDbRunRepository(dynamo_memory, config, clock=clock)
    store = S3ArtifactStore(s3_memory, config)
    task = TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )
    snapshot = repository.create_run(task, fixture.dataset_version, "offline-case-01")
    engine = EvidenceEngine(fixture, clock=clock)
    try:
        controller = Controller(repository, engine, OfflineDecider(), clock=clock)
        result = asyncio.run(controller.run(snapshot.state.run_id))
    finally:
        engine.close()
    assert result["status"] in {"completed", "empty_verified", "unresolved", "pending"}
    assert result["mode"] == "offline"

    reloaded = repository.load_run(snapshot.state.run_id)
    assert reloaded.state.status == result["status"]
    assert reloaded.state.reason_code is not None
    assert reloaded.events[0].type == "run_started"
    assert reloaded.events[-1].type == "run_finished"
    assert [event.sequence for event in reloaded.events] == list(
        range(1, len(reloaded.events) + 1)
    )
    owners = {reloaded.state.lease_owner}
    assert len(owners) == 1 and None not in owners

    key = store.put(
        store.run_key(snapshot.state.run_id, "events.jsonl"),
        [event.model_dump(mode="json") for event in reloaded.events],
    )
    assert key.startswith(f"runs/{snapshot.state.run_id}/")
    assert store.get(key)
    assert dynamo_memory.calls.count("TransactWriteItems") >= 2


def test_a_duplicate_invocation_cannot_take_a_second_lease_offline(
    dynamo_memory: InMemoryDynamo, config: CloudResourceConfig
) -> None:
    clock = FakeClock()
    repository = DynamoDbRunRepository(dynamo_memory, config, clock=clock)
    task = TaskSpec(dataset_id="ds-17", order_date_utc=load_fixture().order_date_utc)
    repository.create_run(task, "v17", "offline-case-02")
    run_id = derive_run_id("offline-case-02")
    first = repository.acquire(run_id, uuid4())
    assert isinstance(first, Snapshot)
    assert repository.acquire(run_id, uuid4()) is None
    assert repository.load_run(run_id).state.lease_owner == first.state.lease_owner


def test_a_receipt_round_trips_in_one_namespace_and_is_denied_across_them_offline(
    dynamo_memory: InMemoryDynamo,
    config: CloudResourceConfig,
    write_token: ReceiptWriteToken,
    namespace: ReceiptNamespace,
    other_namespace: ReceiptNamespace,
    make_receipt: Any,
) -> None:
    receipts = DynamoDbReceiptRepository(dynamo_memory, config)
    mine = make_receipt()
    theirs = make_receipt(receipt_namespace=other_namespace)
    assert receipts.put(mine, write_token) is True
    assert receipts.put(mine, write_token) is False
    assert receipts.put(theirs, write_token) is True

    assert receipts.find(namespace, mine.problem_fingerprint) == (mine,)
    assert receipts.find(other_namespace, theirs.problem_fingerprint) == (theirs,)
    assert mine.problem_fingerprint == theirs.problem_fingerprint
    assert receipts.find(namespace, mine.problem_fingerprint) != (theirs,)

    assert receipts.supersede(namespace, mine.receipt_id, write_token) is True
    assert receipts.find(namespace, mine.problem_fingerprint) == ()
    assert receipts.find(other_namespace, theirs.problem_fingerprint) == (theirs,)


def test_an_invocation_without_a_verified_identity_never_admits_a_run_offline(
    dynamo_memory: InMemoryDynamo, config: CloudResourceConfig
) -> None:
    clock = FakeClock()
    repository = DynamoDbRunRepository(dynamo_memory, config, clock=clock)
    surface = ControllerRuntime(
        repository,
        TaskSpec(dataset_id="ds-17", order_date_utc=load_fixture().order_date_utc),
        "v17",
        execute=lambda snapshot, owner: {"status": "unresolved"},
        verifier=UnverifiedIdentity(),
    )
    status, body = surface.handle(
        "POST", INVOCATIONS_PATH, {"authorization": "ignored"}, {"idempotency_key": "x"}
    )
    assert status == 403
    assert body["reason_code"] == CloudReason.IDENTITY_UNVERIFIED.value
    assert body["run_id"] is None
    assert dynamo_memory.items == {}


def test_a_preflight_refusal_blocks_the_deploy_path_offline(
    config: CloudResourceConfig,
    inputs: SynthesisInputs,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = run_preflight(config, SpendGateInputs())
    assert report.offline_passed is True
    assert report.live_passed is False
    assert evaluate_spend_gate(SpendGateInputs()).reason is (
        CloudReason.SPEND_LIMIT_UNSET
    )

    monkeypatch.setattr(
        util,
        "find_spec",
        lambda name: None if name == "infra.stack" else util.find_spec,
    )
    blocked = run_preflight(config, SpendGateInputs())
    assert blocked.offline_passed is False
    assert [check.id for check in blocked.unmet() if check.scope == "offline"] == [
        "synthesis-available"
    ]

    path = tmp_path / "cloud-config.json"
    path.write_text(inputs.model_dump_json(), encoding="utf-8")
    code = deploy(path, tmp_path / "cdk.out", synth_only=True)
    body = json.loads(capsys.readouterr().out)
    assert code == 2
    assert body["reason_code"] == "preflight_required"
    assert body["detail"] == "synthesis-available"


def test_one_synthesized_template_satisfies_every_security_assertion_offline(
    inputs: SynthesisInputs, tmp_path: Path
) -> None:
    template = template_for(inputs, outdir=tmp_path / "cdk.out")
    assertions = [
        getattr(infrastructure, name)
        for name in sorted(dir(infrastructure))
        if name.startswith("test_")
        and set(inspect.signature(getattr(infrastructure, name)).parameters)
        == {"template"}
    ]
    assert len(assertions) >= 9
    for assertion in assertions:
        assertion(template)


def test_no_cloud_module_opens_a_socket_or_reaches_outbound_offline() -> None:
    for name in CLOUD_MODULES:
        module = importlib.import_module(name)
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        offending = {
            item
            for item in imported
            if item in OUTBOUND_MODULES or item.startswith("boto3")
        }
        assert offending == set(), (name, offending)
        assert "botocore.session" not in imported, name


def test_the_unit_builds_no_client_of_its_own_offline() -> None:
    for name in CLOUD_MODULES:
        module = importlib.import_module(name)
        tree = ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        forbidden = {
            "boto3.client",
            "boto3.resource",
            "Session",
            "botocore.session.Session",
            "get_session",
        }
        assert not (forbidden & calls), (name, forbidden & calls)
        assert not any(call.endswith(".create_client") for call in calls), name


def test_the_network_guard_itself_is_active_offline() -> None:
    import socket

    with pytest.raises(AssertionError):
        socket.socket().connect(("127.0.0.1", 9))
    with pytest.raises(AssertionError):
        socket.create_connection(("127.0.0.1", 9))
    with pytest.raises(CloudFault):
        S3ArtifactStore(object(), CloudResourceConfig.model_construct())
