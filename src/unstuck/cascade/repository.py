"""Local receipt storage with verifier-only writes and namespace isolation.

The repository stores nothing it was not handed by the deterministic verifier.
Writes are conditional and idempotent: the same verified fact written twice is
one row, and an existing row is never overwritten. Each arm and case is a
separate namespace, so a held-out run starts empty and Call 1 can only ever be
read by a later call of the same arm and case.

Freezing the store is the mechanism for held-out evaluation: once frozen, no
further receipt may be admitted.

Retrieval is bounded and explicit. `find_page` spends a declared read budget,
returns the receipts in one deterministic order, and states whether the budget
stopped the walk before the namespace was exhausted. That truncation signal is
what keeps a bounded read from looking like an empty namespace: a caller that
reads a truncated page knows its history is incomplete rather than absent.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from unstuck.cascade.receipts import DecisionReceipt, ReceiptNamespace
from unstuck.domain import Reason, RuntimeFault
from unstuck.events import PersistenceBoundary, Redactor

#: The largest page a single receipt query may request.
MAX_RECEIPT_PAGE_SIZE = 100


class ReceiptLookupError(Exception):
    """The store could not be read. The caller reports history_unavailable."""


@dataclass(frozen=True)
class ReceiptReadBudget:
    """The read budget one receipt lookup may spend.

    `page_size` bounds a single request, `max_pages` bounds how far the walk
    follows a continuation, and `max_items` bounds the receipts it keeps. The
    budget is declared rather than discovered, so lookup work and read cost do
    not grow with namespace history.
    """

    page_size: int = 25
    max_pages: int = 4
    max_items: int = 50

    def __post_init__(self) -> None:
        if not 1 <= self.page_size <= MAX_RECEIPT_PAGE_SIZE:
            raise ValueError("a receipt page size is between one and one hundred")
        if self.max_pages < 1:
            raise ValueError("a receipt lookup reads at least one page")
        if self.max_items < 1:
            raise ValueError("a receipt lookup keeps at least one receipt")
        if self.max_items < self.page_size:
            raise ValueError("the item budget cannot be smaller than one page")


#: The default budget every receipt lookup spends unless a caller narrows it.
RECEIPT_READ_BUDGET = ReceiptReadBudget()


@dataclass(frozen=True)
class ReceiptPage:
    """One bounded retrieval, with its truncation and read evidence.

    `truncated` is true when the budget stopped the walk or dropped a receipt,
    so an incomplete history is never reported as an exhausted one. The read
    counters are evidence about the retrieval and carry no decision authority.
    """

    receipts: tuple[DecisionReceipt, ...] = ()
    truncated: bool = False
    pages_read: int = 0
    items_examined: int = 0
    consumed_reads: float = 0.0


class _Authority:
    """Private write capability held only by the deterministic verifier."""

    __slots__ = ()


_AUTHORITY = _Authority()


@dataclass(frozen=True)
class ReceiptWriteToken:
    """Proof that a receipt came from the verifier rather than a model."""

    authority: object

    def valid(self) -> bool:
        return self.authority is _AUTHORITY


def mint_write_token() -> ReceiptWriteToken:
    """Internal: used by `ReceiptVerifier` only."""
    return ReceiptWriteToken(_AUTHORITY)


class ReceiptRepository(Protocol):
    def put(self, receipt: DecisionReceipt, token: ReceiptWriteToken) -> bool: ...
    def find(
        self, namespace: ReceiptNamespace, problem_fingerprint: str
    ) -> tuple[DecisionReceipt, ...]: ...
    def supersede(
        self, namespace: ReceiptNamespace, receipt_id: str, token: ReceiptWriteToken
    ) -> bool: ...


@runtime_checkable
class BoundedReceiptRepository(Protocol):
    """A store that retrieves receipts under a declared read budget."""

    def find_page(
        self,
        namespace: ReceiptNamespace,
        problem_fingerprint: str,
        *,
        budget: ReceiptReadBudget | None = ...,
    ) -> ReceiptPage: ...


class LocalReceiptRepository:
    """Offline, in-process receipt store. No network, cache, or expiry rule."""

    def __init__(self, *, redactor: Redactor | None = None):
        self.boundary = PersistenceBoundary(redactor or Redactor())
        self._rows: dict[tuple[str, str], dict[str, DecisionReceipt]] = {}
        self._tombstones: dict[tuple[str, str], set[str]] = {}
        self._frozen = False

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
            receipt = DecisionReceipt.model_validate(receipt)
        except ValidationError as error:
            raise RuntimeFault(Reason.RECEIPT_INVALID) from error
        self.boundary.validate(receipt, Reason.RECEIPT_INVALID)
        rows = self._rows.setdefault(receipt.namespace.key, {})
        if receipt.receipt_id in rows:
            return False
        rows[receipt.receipt_id] = receipt
        return True

    def find(
        self, namespace: ReceiptNamespace, problem_fingerprint: str
    ) -> tuple[DecisionReceipt, ...]:
        """Every live receipt of one namespace, in receipt identity order.

        Identity order is the order the DynamoDB adapter's sort key already
        produces, so a truncated page holds the same prefix in both stores.
        """
        tombstones = self._tombstones.get(namespace.key, set())
        return tuple(
            sorted(
                (
                    receipt
                    for receipt in self._rows.get(namespace.key, {}).values()
                    if receipt.problem_fingerprint == problem_fingerprint
                    and receipt.receipt_id not in tombstones
                ),
                key=lambda receipt: receipt.receipt_id,
            )
        )

    def find_page(
        self,
        namespace: ReceiptNamespace,
        problem_fingerprint: str,
        *,
        budget: ReceiptReadBudget | None = None,
    ) -> ReceiptPage:
        """Bound the in-process read the same way the remote walk is bounded.

        An in-process store has no page to pay for, so this spends no reads.
        It still honours the budget, because a caller that narrows the budget
        must get the same truncated prefix from either store.
        """
        spent = budget or RECEIPT_READ_BUDGET
        live = self.find(namespace, problem_fingerprint)
        reachable = min(len(live), spent.page_size * spent.max_pages)
        kept = live[: min(reachable, spent.max_items)]
        return ReceiptPage(
            receipts=kept,
            truncated=len(kept) < len(live),
            pages_read=max(1, -(-reachable // spent.page_size)),
            items_examined=len(live),
        )

    def supersede(
        self, namespace: ReceiptNamespace, receipt_id: str, token: ReceiptWriteToken
    ) -> bool:
        """Invalidate one receipt. Invalidation is a write, not a read effect."""
        if not isinstance(token, ReceiptWriteToken) or not token.valid():
            raise RuntimeFault(Reason.RECEIPT_INVALID)
        if receipt_id not in self._rows.get(namespace.key, {}):
            return False
        self._tombstones.setdefault(namespace.key, set()).add(receipt_id)
        return True

    def namespaces(self) -> Iterator[tuple[str, str]]:
        return iter(tuple(self._rows))

    def count(self, namespace: ReceiptNamespace) -> int:
        tombstones = self._tombstones.get(namespace.key, set())
        return len(
            [
                identifier
                for identifier in self._rows.get(namespace.key, {})
                if identifier not in tombstones
            ]
        )
