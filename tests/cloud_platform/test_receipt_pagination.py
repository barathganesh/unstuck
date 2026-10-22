# Unit command: UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/cloud_platform/test_receipt_pagination.py -q
"""Bounded, paginated, deterministic Decision Receipt retrieval. Offline.

A filtered DynamoDB page can be empty while later pages hold live receipts,
because `Limit` applies before `FilterExpression`. A lookup that stops at the
first page therefore reports a cold start that never happened. These tests fix
the opposite behavior: continue through `LastEvaluatedKey` until the namespace
is exhausted or a declared read budget stops the walk, keep one deterministic
order, and report truncation and the consumed read budget explicitly.

Every request below is a stubbed shape, never a deployed grant: PF3 and PF6
remain Pending.
"""

from collections.abc import Callable
from typing import Any

import pytest

from unstuck.cascade.receipts import DecisionReceipt, ReceiptNamespace
from unstuck.cascade.repository import (
    RECEIPT_READ_BUDGET,
    BoundedReceiptRepository,
    ReceiptLookupError,
    ReceiptPage,
    ReceiptReadBudget,
)
from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.keys import ReceiptIndexKey
from unstuck.cloud.receipts import DynamoDbReceiptRepository, receipt_item

from .conftest import SYNTHETIC_RECEIPT_TABLE, StubbedClient

MakeReceipt = Callable[..., DecisionReceipt]


def repository(
    stub: StubbedClient, config: CloudResourceConfig
) -> DynamoDbReceiptRepository:
    return DynamoDbReceiptRepository(stub.client, config)


def page(
    items: list[dict[str, Any]], *, last_key: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "Items": items,
        "Count": len(items),
        "ScannedCount": max(len(items), 1),
        "ConsumedCapacity": {
            "TableName": SYNTHETIC_RECEIPT_TABLE,
            "CapacityUnits": 0.5,
        },
    }
    if last_key is not None:
        body["LastEvaluatedKey"] = last_key
    return body


def continuation(
    receipt: DecisionReceipt, namespace: ReceiptNamespace
) -> dict[str, Any]:
    key = ReceiptIndexKey.for_receipt(
        namespace, receipt.problem_fingerprint, receipt.receipt_id
    )
    return {"PK": {"S": key.partition}, "SK": {"S": key.sort}}


def test_the_dynamodb_repository_satisfies_the_bounded_protocol_offline(
    receipt_dynamo: StubbedClient, config: CloudResourceConfig
) -> None:
    adapter = repository(receipt_dynamo, config)
    assert isinstance(adapter, BoundedReceiptRepository)
    assert RECEIPT_READ_BUDGET.page_size >= 1
    assert RECEIPT_READ_BUDGET.max_pages >= 2
    assert RECEIPT_READ_BUDGET.max_items >= RECEIPT_READ_BUDGET.page_size


def test_every_receipt_query_declares_an_explicit_page_limit_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    namespace: ReceiptNamespace,
    make_receipt: MakeReceipt,
) -> None:
    receipt = make_receipt()
    receipt_dynamo.stubber.add_response("query", page([receipt_item(receipt)]))
    found = repository(receipt_dynamo, config).find_page(
        namespace, receipt.problem_fingerprint
    )
    assert found.receipts == (receipt,)
    request = receipt_dynamo.only("Query")
    assert request["Limit"] == RECEIPT_READ_BUDGET.page_size
    assert request["ReturnConsumedCapacity"] == "TOTAL"
    assert request["ConsistentRead"] is True
    assert "ExclusiveStartKey" not in request
    assert found.pages_read == 1
    assert found.truncated is False
    assert found.consumed_reads == pytest.approx(0.5)
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_an_empty_filtered_page_is_never_a_cold_start_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    namespace: ReceiptNamespace,
    make_receipt: MakeReceipt,
) -> None:
    """`Limit` applies before the live filter, so page one can be empty."""
    live = make_receipt()
    cursor = continuation(live, namespace)
    receipt_dynamo.stubber.add_response("query", page([], last_key=cursor))
    receipt_dynamo.stubber.add_response("query", page([receipt_item(live)]))
    found = repository(receipt_dynamo, config).find_page(
        namespace, live.problem_fingerprint
    )
    assert found.receipts == (live,)
    assert found.pages_read == 2
    assert found.truncated is False
    requests = receipt_dynamo.params("Query")
    assert len(requests) == 2
    assert "ExclusiveStartKey" not in requests[0]
    assert requests[1]["ExclusiveStartKey"] == cursor
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_the_page_budget_stops_the_walk_and_reports_truncation_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    namespace: ReceiptNamespace,
    make_receipt: MakeReceipt,
) -> None:
    receipt = make_receipt()
    cursor = continuation(receipt, namespace)
    budget = ReceiptReadBudget(page_size=1, max_pages=2, max_items=8)
    for _ in range(budget.max_pages):
        receipt_dynamo.stubber.add_response(
            "query", page([receipt_item(receipt)], last_key=cursor)
        )
    found = repository(receipt_dynamo, config).find_page(
        namespace, receipt.problem_fingerprint, budget=budget
    )
    assert found.pages_read == budget.max_pages
    assert found.truncated is True
    assert len(receipt_dynamo.params("Query")) == budget.max_pages
    assert all(item["Limit"] == 1 for item in receipt_dynamo.params("Query"))
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_the_item_budget_keeps_a_deterministic_prefix_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    namespace: ReceiptNamespace,
    make_receipt: MakeReceipt,
) -> None:
    receipts = [make_receipt() for _ in range(3)]
    ordered = sorted(receipts, key=lambda item: item.receipt_id)
    budget = ReceiptReadBudget(page_size=2, max_pages=4, max_items=2)
    receipt_dynamo.stubber.add_response(
        "query", page([receipt_item(item) for item in reversed(ordered)])
    )
    found = repository(receipt_dynamo, config).find_page(
        namespace, ordered[0].problem_fingerprint, budget=budget
    )
    assert found.receipts == tuple(ordered[:2])
    assert found.truncated is True
    assert found.items_examined >= len(ordered)
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_a_foreign_namespace_row_on_a_later_page_is_refused_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    namespace: ReceiptNamespace,
    other_namespace: ReceiptNamespace,
    make_receipt: MakeReceipt,
) -> None:
    local = make_receipt()
    foreign = make_receipt(receipt_namespace=other_namespace)
    cursor = continuation(local, namespace)
    receipt_dynamo.stubber.add_response(
        "query", page([receipt_item(local)], last_key=cursor)
    )
    receipt_dynamo.stubber.add_response("query", page([receipt_item(foreign)]))
    with pytest.raises(CloudFault) as raised:
        repository(receipt_dynamo, config).find_page(
            namespace, local.problem_fingerprint
        )
    assert raised.value.reason is CloudReason.NAMESPACE_DENIED


def test_a_later_page_failure_fails_closed_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    namespace: ReceiptNamespace,
    make_receipt: MakeReceipt,
) -> None:
    receipt = make_receipt()
    receipt_dynamo.stubber.add_response(
        "query",
        page([receipt_item(receipt)], last_key=continuation(receipt, namespace)),
    )
    receipt_dynamo.stubber.add_client_error(
        "query", service_error_code="ProvisionedThroughputExceededException"
    )
    with pytest.raises(ReceiptLookupError):
        repository(receipt_dynamo, config).find_page(
            namespace, receipt.problem_fingerprint
        )
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_find_returns_the_bounded_page_receipts_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    namespace: ReceiptNamespace,
    make_receipt: MakeReceipt,
) -> None:
    receipt = make_receipt()
    receipt_dynamo.stubber.add_response("query", page([receipt_item(receipt)]))
    assert repository(receipt_dynamo, config).find(
        namespace, receipt.problem_fingerprint
    ) == (receipt,)
    assert receipt_dynamo.only("Query")["Limit"] == RECEIPT_READ_BUDGET.page_size
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_an_empty_namespace_reports_an_empty_page_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    namespace: ReceiptNamespace,
) -> None:
    receipt_dynamo.stubber.add_response("query", page([]))
    found = repository(receipt_dynamo, config).find_page(namespace, "a" * 64)
    assert found == ReceiptPage(
        receipts=(),
        truncated=False,
        pages_read=1,
        items_examined=1,
        consumed_reads=0.5,
    )
    receipt_dynamo.stubber.assert_no_pending_responses()


@pytest.mark.parametrize(
    "refused",
    [
        {"page_size": 0},
        {"page_size": 101},
        {"max_pages": 0},
        {"max_items": 0},
        {"max_items": 2},
    ],
)
def test_an_incoherent_read_budget_is_refused_offline(refused: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ReceiptReadBudget(**{"page_size": 4, "max_pages": 2, "max_items": 8} | refused)
