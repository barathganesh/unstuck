"""Step 6b: the matcher consumes a bounded retrieval without gaining authority.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_receipt_pagination.py -q

Retrieval truncation and reference truncation are two different facts. A read
budget that stopped before the namespace was exhausted leaves receipts unread,
so an exact no-progress blocker may be sitting in the remainder. That is an
incomplete history rather than a readable one, and the matcher reports it as
explicit `history_unavailable` with `retrieval_truncated` recorded, never as a
cold start and never as a no-match. Reference truncation is the other fact: the
whole namespace was read and classified matches were dropped to stay inside the
reference bound, which keeps a complete history and stays `matched`.

Matching authority is unchanged: the matcher still classifies against the
current decision context and only an exact no-progress match blocks anything.
"""

import pytest
from pydantic import ValidationError

from unstuck.cascade.matching import HistoryLookupResult, HistoryMatcher
from unstuck.cascade.repository import (
    BoundedReceiptRepository,
    LocalReceiptRepository,
    ReceiptLookupError,
    ReceiptPage,
    ReceiptReadBudget,
)


class BoundedStub:
    """A store whose retrieval stopped before the namespace was exhausted."""

    def __init__(self, receipts=(), *, truncated=True):
        self.receipts = tuple(receipts)
        self.truncated = truncated
        self.calls = 0

    def put(self, receipt, token):
        return True

    def find(self, namespace, problem_fingerprint):
        raise AssertionError("a bounded repository is read through find_page")

    def find_page(self, namespace, problem_fingerprint, *, budget=None):
        self.calls += 1
        return ReceiptPage(
            receipts=self.receipts,
            truncated=self.truncated,
            pages_read=2,
            items_examined=8,
            consumed_reads=1.0,
        )

    def supersede(self, namespace, receipt_id, token):
        return False


class UnboundedStub:
    """A store that offers only the original unbounded protocol."""

    def __init__(self, receipts=()):
        self.receipts = tuple(receipts)

    def put(self, receipt, token):
        return True

    def find(self, namespace, problem_fingerprint):
        return self.receipts

    def supersede(self, namespace, receipt_id, token):
        return False


def test_the_local_repository_is_a_bounded_repository(
    repository: LocalReceiptRepository,
) -> None:
    assert isinstance(repository, BoundedReceiptRepository)
    assert not isinstance(UnboundedStub(), BoundedReceiptRepository)


def test_the_local_repository_returns_a_deterministic_bounded_page(
    repository, verifier, make_receipt, useful_report, namespace
) -> None:
    receipts = [make_receipt(useful_report) for _ in range(3)]
    for receipt in receipts:
        assert repository.put(receipt, verifier.token) is True
    fingerprint = receipts[0].problem_fingerprint
    budget = ReceiptReadBudget(page_size=2, max_pages=1, max_items=2)
    found = repository.find_page(namespace, fingerprint, budget=budget)
    expected = tuple(sorted(receipts, key=lambda item: item.receipt_id))[:2]
    assert found.receipts == expected
    assert found.truncated is True
    assert found.items_examined == 3
    whole = repository.find_page(namespace, fingerprint)
    assert whole.truncated is False
    assert whole.receipts == tuple(sorted(receipts, key=lambda item: item.receipt_id))
    assert repository.find(namespace, fingerprint) == whole.receipts


def test_a_truncated_retrieval_is_history_unavailable_with_no_match(context, namespace):
    """An unread remainder is an absent history, never a cold start."""
    matcher = HistoryMatcher(BoundedStub())
    result = matcher.lookup(namespace, context, requested=True)
    assert result.status == "unavailable"
    assert result.retrieval_truncated is True
    assert result.truncated is False
    assert result.matches == ()
    assert result.receipt_ids == ()


def test_a_truncated_retrieval_is_history_unavailable_despite_a_match(
    verifier, make_receipt, useful_report, context, namespace
):
    """A partial page is refused whole; a read match does not complete it."""
    receipt = make_receipt(useful_report)
    matcher = HistoryMatcher(BoundedStub([receipt]))
    result = matcher.lookup(namespace, context, requested=True)
    assert result.status == "unavailable"
    assert result.retrieval_truncated is True
    assert result.matches == ()
    assert result.receipt_ids == ()


@pytest.mark.parametrize("status", ["not_requested", "empty", "matched"])
def test_a_truncated_retrieval_cannot_claim_a_readable_history(status):
    with pytest.raises(ValidationError):
        HistoryLookupResult.model_validate(
            {"status": status, "retrieval_truncated": True}
        )


def test_an_exhausted_retrieval_reports_no_retrieval_truncation(
    make_receipt, useful_report, context, namespace
):
    receipt = make_receipt(useful_report)
    matcher = HistoryMatcher(BoundedStub([receipt], truncated=False))
    result = matcher.lookup(namespace, context, requested=True)
    assert result.retrieval_truncated is False


def test_an_unbounded_repository_still_reads_through_find(
    make_receipt, useful_report, context, namespace
):
    receipt = make_receipt(useful_report)
    matcher = HistoryMatcher(UnboundedStub([receipt]))
    result = matcher.lookup(namespace, context, requested=True)
    assert result.status == "matched"
    assert result.retrieval_truncated is False


def test_a_bounded_lookup_failure_is_history_unavailable(context, namespace):
    class Failing(BoundedStub):
        def find_page(self, namespace, problem_fingerprint, *, budget=None):
            raise ReceiptLookupError("read path unavailable")

    result = HistoryMatcher(Failing()).lookup(namespace, context, requested=True)
    assert result.status == "unavailable"
    assert result.retrieval_truncated is False


def test_a_truncated_retrieval_offers_no_match_and_an_exhausted_one_does(
    verifier, make_receipt, stalled_report, context, namespace
):
    """Only a complete read may offer a blocker; truncation offers nothing."""
    receipt = make_receipt(stalled_report)
    execution = (receipt.selected_capability, receipt.arguments_hash)
    result = HistoryMatcher(BoundedStub([receipt])).lookup(
        namespace, context, requested=True
    )
    assert result.retrieval_truncated is True
    assert result.blocked_executions == frozenset()
    assert result.blocking_match(execution) is None
    exhausted = HistoryMatcher(BoundedStub([receipt], truncated=False)).lookup(
        namespace, context, requested=True
    )
    blocking = exhausted.blocking_match(execution)
    assert blocking is not None
    assert blocking.match_class == "exact"
    assert exhausted.blocked_executions == frozenset({execution})


@pytest.mark.parametrize(
    "refused",
    [
        {"page_size": 0},
        {"max_pages": 0},
        {"max_items": 0},
        {"max_items": 1},
    ],
)
def test_an_incoherent_read_budget_is_refused(refused):
    with pytest.raises(ValueError):
        ReceiptReadBudget(**{"page_size": 2, "max_pages": 2, "max_items": 4} | refused)
