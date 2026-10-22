"""DynamoDB Decision Receipt adapter behind the `ReceiptRepository` protocol.

The adapter stores nothing it was not handed by the deterministic verifier and
reads nothing outside the requested namespace. It derives no outcome class,
selects no tool, authorizes no repair, and carries no time window: a receipt is
never a cache and `observed_at` is never an expiry rule.

Namespace isolation is a key property, not a filter. Receipts live in
`PROBLEM#<problem_fingerprint>` partitions whose sort keys begin with the arm
and case, so a query bounded by that prefix cannot reach another namespace even
when the problem fingerprint matches. A row that arrives from a foreign
namespace anyway is refused rather than returned.

Retrieval is bounded and complete. DynamoDB applies `Limit` before
`FilterExpression`, so a page that excludes every tombstoned row can come back
empty while later pages hold live receipts: a lookup that stopped at the first
page would report a cold start that never happened. `find_page` therefore
requests an explicit page size, follows `LastEvaluatedKey` until the namespace
is exhausted or the declared read budget stops the walk, and reports truncation
and consumed capacity so the caller records an incomplete history as
incomplete. The walk stays on the base table with `ConsistentRead`, because
Call 2 of a pair must read the receipt Call 1 wrote; a secondary index would
only ever be eventually consistent and would turn that read into a cold start.

A write is conditional and idempotent: the same verified fact written twice is
one row and the second `put` reports `False`. Invalidation is a write-side
tombstone, never a read-side effect. Freezing refuses further admission, which
is the mechanism held-out evaluation relies on.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from botocore.exceptions import ClientError
from pydantic import ValidationError

from unstuck.cascade.receipts import DecisionReceipt, ReceiptNamespace
from unstuck.cascade.repository import (
    RECEIPT_READ_BUDGET,
    ReceiptLookupError,
    ReceiptPage,
    ReceiptReadBudget,
    ReceiptWriteToken,
)
from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.dynamodb import Item, condition_failed
from unstuck.cloud.keys import ReceiptIndexKey
from unstuck.domain import Reason, RuntimeFault
from unstuck.events import PersistenceBoundary, Redactor

RECEIPT_ABSENT_CONDITION = "attribute_not_exists(SK)"
RECEIPT_PRESENT_CONDITION = "attribute_exists(SK)"
RECEIPT_QUERY = "PK = :pk AND begins_with(SK, :prefix)"
LIVE_FILTER = "attribute_not_exists(superseded)"
TOMBSTONE_EXPRESSION = "SET superseded = :superseded"

#: Secondary index over the globally unique receipt sort key. `supersede` is
#: handed a namespace and a receipt id but not a problem fingerprint, so it
#: resolves the owning partition through this index before it writes.
RECEIPT_ID_INDEX = "receipt-sort-key-index"
RECEIPT_ID_QUERY = "SK = :sk"


class ReceiptIndexClient(Protocol):
    """The narrow surface this adapter uses. Always injected, never built."""

    def put_item(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def update_item(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def query(self, **kwargs: Any) -> Mapping[str, Any]: ...


def receipt_item(receipt: DecisionReceipt) -> Item:
    """The index item. Every attribute is verifier-owned already."""
    key = ReceiptIndexKey.for_receipt(
        receipt.namespace, receipt.problem_fingerprint, receipt.receipt_id
    )
    return {
        **key.item,
        "body": {"S": receipt.model_dump_json()},
        "receipt_id": {"S": receipt.receipt_id},
        "decision_context_hash": {"S": receipt.decision_context_hash},
        "outcome_class": {"S": receipt.outcome_class},
        "versions_hash": {"S": receipt.versions.versions_hash},
        "arm": {"S": receipt.namespace.arm},
        "case_id": {"S": receipt.namespace.case_id},
    }


def receipt_from_item(item: Mapping[str, Any]) -> DecisionReceipt:
    """Rebuild one receipt, or fail closed so the caller reports no history."""
    try:
        return DecisionReceipt.model_validate_json(item["body"]["S"])
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise ReceiptLookupError("receipt row is unreadable") from error


class DynamoDbReceiptRepository:
    """`ReceiptRepository` over DynamoDB. No retry, no cache, no expiry."""

    def __init__(
        self,
        client: ReceiptIndexClient,
        config: CloudResourceConfig,
        *,
        redactor: Redactor | None = None,
    ):
        self._client = client
        self._config = config
        self._boundary = PersistenceBoundary(redactor or Redactor())
        self._frozen = False

    @property
    def table(self) -> str:
        return self._config.receipt_table_name

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> None:
        """Freeze the corpus before any held-out call."""
        self._frozen = True

    def put(self, receipt: DecisionReceipt, token: ReceiptWriteToken) -> bool:
        if not isinstance(token, ReceiptWriteToken) or not token.valid():
            raise RuntimeFault(Reason.RECEIPT_INVALID)
        if self._frozen:
            raise RuntimeFault(Reason.RECEIPT_INVALID)
        if not isinstance(receipt, DecisionReceipt):
            raise RuntimeFault(Reason.RECEIPT_INVALID)
        try:
            validated = DecisionReceipt.model_validate(receipt)
        except ValidationError as error:
            raise RuntimeFault(Reason.RECEIPT_INVALID) from error
        self._boundary.validate(validated, Reason.RECEIPT_INVALID)
        try:
            self._client.put_item(
                TableName=self.table,
                Item=receipt_item(validated),
                ConditionExpression=RECEIPT_ABSENT_CONDITION,
            )
        except ClientError as error:
            if condition_failed(error):
                return False
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        return True

    def find(
        self, namespace: ReceiptNamespace, problem_fingerprint: str
    ) -> tuple[DecisionReceipt, ...]:
        return self.find_page(namespace, problem_fingerprint).receipts

    def find_page(
        self,
        namespace: ReceiptNamespace,
        problem_fingerprint: str,
        *,
        budget: ReceiptReadBudget | None = None,
    ) -> ReceiptPage:
        """Walk one namespace under the read budget and report what it cost."""
        spent = budget or RECEIPT_READ_BUDGET
        values = {
            ":pk": {"S": ReceiptIndexKey.partition_for(problem_fingerprint)},
            ":prefix": {"S": ReceiptIndexKey.namespace_prefix(namespace)},
        }
        collected: list[DecisionReceipt] = []
        start_key: Mapping[str, Any] | None = None
        pages = 0
        examined = 0
        reads = 0.0
        truncated = False
        while True:
            request: dict[str, Any] = {
                "TableName": self.table,
                "KeyConditionExpression": RECEIPT_QUERY,
                "FilterExpression": LIVE_FILTER,
                "ExpressionAttributeValues": values,
                "ConsistentRead": True,
                "Limit": spent.page_size,
                "ReturnConsumedCapacity": "TOTAL",
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = dict(start_key)
            try:
                response = self._client.query(**request)
            except ClientError as error:
                raise ReceiptLookupError("receipt index is unavailable") from error
            pages += 1
            rows: Sequence[Mapping[str, Any]] = list(response.get("Items", ()))
            examined += int(response.get("ScannedCount", len(rows)))
            capacity = response.get("ConsumedCapacity") or {}
            reads += float(capacity.get("CapacityUnits", 0.0))
            for row in rows:
                found = receipt_from_item(row)
                if found.namespace != namespace:
                    raise CloudFault(CloudReason.NAMESPACE_DENIED)
                collected.append(found)
            start_key = response.get("LastEvaluatedKey") or None
            if start_key is None:
                break
            if pages >= spent.max_pages or len(collected) >= spent.max_items:
                truncated = True
                break
        # The sort key carries the receipt id last, so identity order is the
        # order DynamoDB already returned. Sorting makes it explicit and keeps
        # the kept prefix identical to the local store's.
        ordered = sorted(collected, key=lambda receipt: receipt.receipt_id)
        kept = tuple(ordered[: spent.max_items])
        return ReceiptPage(
            receipts=kept,
            truncated=truncated or len(kept) < len(ordered),
            pages_read=pages,
            items_examined=examined,
            consumed_reads=reads,
        )

    def supersede(
        self, namespace: ReceiptNamespace, receipt_id: str, token: ReceiptWriteToken
    ) -> bool:
        """Invalidate one receipt. Invalidation is a write, not a read effect."""
        if not isinstance(token, ReceiptWriteToken) or not token.valid():
            raise RuntimeFault(Reason.RECEIPT_INVALID)
        sort = f"{ReceiptIndexKey.namespace_prefix(namespace)}{receipt_id}"
        try:
            located = self._client.query(
                TableName=self.table,
                IndexName=RECEIPT_ID_INDEX,
                KeyConditionExpression=RECEIPT_ID_QUERY,
                ExpressionAttributeValues={":sk": {"S": sort}},
                ConsistentRead=False,
            )
        except ClientError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        rows: Sequence[Mapping[str, Any]] = list(located.get("Items", ()))
        if not rows:
            return False
        try:
            self._client.update_item(
                TableName=self.table,
                Key={"PK": rows[0]["PK"], "SK": {"S": sort}},
                UpdateExpression=TOMBSTONE_EXPRESSION,
                ConditionExpression=RECEIPT_PRESENT_CONDITION,
                ExpressionAttributeValues={":superseded": {"BOOL": True}},
            )
        except (ClientError, KeyError) as error:
            if isinstance(error, ClientError) and condition_failed(error):
                return False
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        return True


__all__ = [
    "LIVE_FILTER",
    "RECEIPT_ABSENT_CONDITION",
    "RECEIPT_ID_INDEX",
    "RECEIPT_ID_QUERY",
    "RECEIPT_PRESENT_CONDITION",
    "RECEIPT_QUERY",
    "RECEIPT_READ_BUDGET",
    "TOMBSTONE_EXPRESSION",
    "DynamoDbReceiptRepository",
    "ReceiptIndexClient",
    "receipt_from_item",
    "receipt_item",
]
