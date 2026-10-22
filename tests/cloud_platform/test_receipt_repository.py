"""Adapter tests for the DynamoDB Decision Receipt repository. Offline.

The adapter persists and retrieves what the verifier already owns. It derives
no outcome class, selects no tool, authorizes no repair, and holds no expiry
rule. Every assertion below is a request shape or a refusal, never evidence of
a deployed grant: PF3 and PF6 remain Pending.
"""

import ast
import inspect
import json
from collections.abc import Callable

import pytest

from unstuck.cascade.receipts import DecisionReceipt, ReceiptNamespace
from unstuck.cascade.repository import (
    ReceiptLookupError,
    ReceiptRepository,
    ReceiptWriteToken,
)
from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.keys import ReceiptIndexKey
from unstuck.cloud.receipts import (
    RECEIPT_ID_INDEX,
    DynamoDbReceiptRepository,
    receipt_item,
)
from unstuck.domain import Reason, RuntimeFault

from .conftest import SYNTHETIC_RECEIPT_TABLE, StubbedClient

MakeReceipt = Callable[..., DecisionReceipt]


def repository(
    stub: StubbedClient, config: CloudResourceConfig
) -> DynamoDbReceiptRepository:
    return DynamoDbReceiptRepository(stub.client, config)


def test_receipt_repository_satisfies_the_existing_protocol_offline(
    receipt_dynamo: StubbedClient, config: CloudResourceConfig
) -> None:
    adapter: ReceiptRepository = repository(receipt_dynamo, config)
    assert adapter is not None
    assert repository(receipt_dynamo, config).frozen is False


def test_put_requires_a_verifier_write_token_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    make_receipt: MakeReceipt,
) -> None:
    receipt = make_receipt()
    adapter = repository(receipt_dynamo, config)
    for forged in (ReceiptWriteToken(authority=object()), "token", None):
        with pytest.raises(RuntimeFault) as raised:
            adapter.put(receipt, forged)
        assert raised.value.reason is Reason.RECEIPT_INVALID
    assert receipt_dynamo.params("PutItem") == []


def test_put_writes_one_conditional_namespace_scoped_item_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    write_token: ReceiptWriteToken,
    namespace: ReceiptNamespace,
    make_receipt: MakeReceipt,
) -> None:
    receipt = make_receipt()
    receipt_dynamo.stubber.add_response("put_item", {})
    assert repository(receipt_dynamo, config).put(receipt, write_token) is True
    request = receipt_dynamo.only("PutItem")
    assert request["TableName"] == SYNTHETIC_RECEIPT_TABLE
    assert request["ConditionExpression"] == "attribute_not_exists(SK)"
    assert request["Item"]["PK"] == {"S": f"PROBLEM#{receipt.problem_fingerprint}"}
    assert request["Item"]["SK"] == {
        "S": f"{ReceiptIndexKey.namespace_prefix(namespace)}{receipt.receipt_id}"
    }
    assert request["Item"]["outcome_class"] == {"S": receipt.outcome_class}
    assert request["Item"]["decision_context_hash"] == {
        "S": receipt.decision_context_hash
    }
    assert json.loads(request["Item"]["body"]["S"])["receipt_id"] == receipt.receipt_id
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_put_is_idempotent_and_returns_false_on_a_duplicate_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    write_token: ReceiptWriteToken,
    make_receipt: MakeReceipt,
) -> None:
    receipt = make_receipt()
    receipt_dynamo.stubber.add_client_error(
        "put_item", service_error_code="ConditionalCheckFailedException"
    )
    assert repository(receipt_dynamo, config).put(receipt, write_token) is False
    assert len(receipt_dynamo.params("PutItem")) == 1
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_a_frozen_corpus_refuses_a_receipt_write_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    write_token: ReceiptWriteToken,
    make_receipt: MakeReceipt,
) -> None:
    adapter = repository(receipt_dynamo, config)
    adapter.freeze()
    assert adapter.frozen is True
    with pytest.raises(RuntimeFault) as raised:
        adapter.put(make_receipt(), write_token)
    assert raised.value.reason is Reason.RECEIPT_INVALID
    assert receipt_dynamo.params("PutItem") == []


def test_find_is_scoped_to_one_namespace_and_fingerprint_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    namespace: ReceiptNamespace,
    make_receipt: MakeReceipt,
) -> None:
    receipt = make_receipt()
    receipt_dynamo.stubber.add_response("query", {"Items": [receipt_item(receipt)]})
    found = repository(receipt_dynamo, config).find(
        namespace, receipt.problem_fingerprint
    )
    assert found == (receipt,)
    request = receipt_dynamo.only("Query")
    assert request["TableName"] == SYNTHETIC_RECEIPT_TABLE
    assert request["KeyConditionExpression"] == "PK = :pk AND begins_with(SK, :prefix)"
    assert request["ExpressionAttributeValues"][":pk"] == {
        "S": f"PROBLEM#{receipt.problem_fingerprint}"
    }
    assert request["ExpressionAttributeValues"][":prefix"] == {
        "S": ReceiptIndexKey.namespace_prefix(namespace)
    }
    assert request["FilterExpression"] == "attribute_not_exists(superseded)"
    assert request["ConsistentRead"] is True
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_find_refuses_a_row_from_a_foreign_namespace_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    namespace: ReceiptNamespace,
    other_namespace: ReceiptNamespace,
    make_receipt: MakeReceipt,
) -> None:
    foreign = make_receipt(receipt_namespace=other_namespace)
    receipt_dynamo.stubber.add_response("query", {"Items": [receipt_item(foreign)]})
    with pytest.raises(CloudFault) as raised:
        repository(receipt_dynamo, config).find(namespace, foreign.problem_fingerprint)
    assert raised.value.reason is CloudReason.NAMESPACE_DENIED


def test_find_fails_closed_when_the_index_is_unreadable_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    namespace: ReceiptNamespace,
) -> None:
    adapter = repository(receipt_dynamo, config)
    receipt_dynamo.stubber.add_client_error(
        "query", service_error_code="ProvisionedThroughputExceededException"
    )
    with pytest.raises(ReceiptLookupError):
        adapter.find(namespace, "a" * 64)
    receipt_dynamo.stubber.add_response("query", {"Items": [{"body": {"S": "{"}}]})
    with pytest.raises(ReceiptLookupError):
        adapter.find(namespace, "a" * 64)
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_supersede_is_a_write_side_tombstone_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    write_token: ReceiptWriteToken,
    namespace: ReceiptNamespace,
    make_receipt: MakeReceipt,
) -> None:
    receipt = make_receipt()
    adapter = repository(receipt_dynamo, config)
    partition = {"S": f"PROBLEM#{receipt.problem_fingerprint}"}
    sort = f"{ReceiptIndexKey.namespace_prefix(namespace)}{receipt.receipt_id}"
    receipt_dynamo.stubber.add_response("query", {"Items": [{"PK": partition}]})
    receipt_dynamo.stubber.add_response("update_item", {})
    assert adapter.supersede(namespace, receipt.receipt_id, write_token) is True
    located = receipt_dynamo.only("Query")
    assert located["IndexName"] == RECEIPT_ID_INDEX
    assert located["KeyConditionExpression"] == "SK = :sk"
    assert located["ExpressionAttributeValues"][":sk"] == {"S": sort}
    request = receipt_dynamo.only("UpdateItem")
    assert request["UpdateExpression"] == "SET superseded = :superseded"
    assert request["ConditionExpression"] == "attribute_exists(SK)"
    assert request["Key"] == {"PK": partition, "SK": {"S": sort}}
    assert request["ExpressionAttributeValues"] == {":superseded": {"BOOL": True}}
    receipt_dynamo.stubber.add_response("query", {"Items": []})
    assert adapter.supersede(namespace, receipt.receipt_id, write_token) is False
    with pytest.raises(RuntimeFault):
        adapter.supersede(namespace, receipt.receipt_id, ReceiptWriteToken(object()))
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_the_adapter_never_derives_outcome_selects_or_repairs_offline() -> None:
    surface = {
        name for name in dir(DynamoDbReceiptRepository) if not name.startswith("_")
    }
    assert surface == {
        "find",
        "find_page",
        "freeze",
        "frozen",
        "put",
        "supersede",
        "table",
    }
    module = inspect.getmodule(DynamoDbReceiptRepository)
    assert module is not None
    tree = ast.parse(inspect.getsource(module))
    identifiers: set[str] = set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.alias):
            identifiers.add(node.name)
            identifiers.add(node.asname or node.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "unstuck.cascade.verifier" not in imported
    assert not any("verifier" in name.lower() for name in imported | identifiers)
    for forbidden in ("classify", "select", "repair", "expire", "ttl", "cache"):
        matches = {name for name in identifiers if forbidden in name.lower()}
        assert matches == set(), (forbidden, matches)
