"""Step 6: history matching, ordering, truncation, and dominance tests.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_matching.py -q
"""

from uuid import uuid4

import pytest

from unstuck.cascade.identity import BoundVersions, DecisionContext
from unstuck.cascade.matching import HistoryMatcher
from unstuck.cascade.receipts import DecisionReceipt, ReceiptNamespace
from unstuck.cascade.repository import ReceiptLookupError
from unstuck.domain import Reason, RuntimeFault

DIGEST = "a" * 64


class FailingRepository:
    """A store that cannot be read. This is history_unavailable, not empty."""

    def put(self, receipt, token):
        raise ReceiptLookupError("write path unavailable")

    def find(self, namespace, problem_fingerprint):
        raise ReceiptLookupError("read path unavailable")

    def supersede(self, namespace, receipt_id, token):
        raise ReceiptLookupError("write path unavailable")


class ForeignRepository:
    """A store that returns a row from another namespace."""

    def __init__(self, receipt):
        self.receipt = receipt

    def put(self, receipt, token):
        return True

    def find(self, namespace, problem_fingerprint):
        return (self.receipt,)

    def supersede(self, namespace, receipt_id, token):
        return False


def store(repository, verifier, receipts):
    for receipt in receipts:
        assert repository.put(receipt, verifier.token) is True
    return HistoryMatcher(repository)


def test_matching_exact_identity_requires_every_current_binding(
    repository, verifier, make_receipt, stalled_report, context, namespace
):
    receipt = make_receipt(stalled_report)
    matcher = store(repository, verifier, [receipt])
    result = matcher.lookup(namespace, context, requested=True)
    assert result.status == "matched"
    assert [match.match_class for match in result.matches] == ["exact"]
    assert result.matches[0].receipt_id == receipt.receipt_id
    assert result.blocked_executions == frozenset({receipt.execution_key})
    assert matcher.classify(receipt, context) == "exact"


def test_matching_compatible_history_is_advisory_and_never_blocks(
    repository, verifier, make_receipt, stalled_report, context, namespace
):
    receipt = make_receipt(stalled_report)
    moved_on = DecisionContext.model_validate(
        context.model_dump() | {"evidence_hashes": ("b" * 64,)}
    )
    assert moved_on.problem_fingerprint == context.problem_fingerprint
    assert moved_on.decision_context_hash != context.decision_context_hash
    matcher = store(repository, verifier, [receipt])
    assert matcher.classify(receipt, moved_on) == "compatible"
    result = matcher.lookup(namespace, moved_on, requested=True)
    assert result.status == "matched"
    assert result.matches[0].match_class == "compatible"
    assert not result.matches[0].blocking
    assert result.blocked_executions == frozenset()
    assert result.blocking_match(receipt.execution_key) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_revision", "scripted-v2"),
        ("catalog_version", "cards-v2"),
        ("tool_contract_version", "tools-v2"),
        ("data_schema_version", "schema-v2"),
        ("dataset_version", "v18"),
        ("policy_hash", DIGEST),
        ("calibration_hash", DIGEST),
    ],
)
def test_matching_changed_versions_are_audit_only(
    repository, verifier, make_receipt, stalled_report, context, namespace, field, value
):
    receipt = make_receipt(stalled_report)
    matcher = store(repository, verifier, [receipt])
    changed = DecisionContext.model_validate(
        context.model_dump()
        | {
            "versions": BoundVersions.model_validate(
                context.versions.model_dump() | {field: value}
            )
        }
    )
    assert matcher.classify(receipt, changed) is None
    assert matcher.lookup(namespace, changed, requested=True).status == "empty"
    # The row is retained; only the offer to the selector is withheld.
    assert repository.count(namespace) == 1


def test_matching_ignores_a_different_problem_and_invalidated_rows(
    repository,
    verifier,
    make_receipt,
    stalled_report,
    make_context,
    make_state,
    context,
    namespace,
):
    receipt = make_receipt(stalled_report)
    matcher = store(repository, verifier, [receipt])
    other_problem = make_context(make_state(country_codes=("FR",)))
    assert matcher.classify(receipt, other_problem) is None
    superseded = DecisionReceipt.model_validate(receipt.model_dump())
    assert matcher.classify(superseded, context) == "exact"
    repository.supersede(namespace, receipt.receipt_id, verifier.token)
    assert matcher.lookup(namespace, context, requested=True).status == "empty"


def test_matching_order_is_deterministic_and_timestamp_independent(
    repository, verifier, make_receipt, make_report, context, namespace
):
    exact_stall = make_receipt(
        make_report(capability="inspect_schema_keys", changed=False)
    )
    exact_useful = make_receipt(make_report(capability="inspect_pipeline"))
    moved_on = DecisionContext.model_validate(
        context.model_dump() | {"evidence_hashes": ("c" * 64,)}
    )
    compatible_useful = make_receipt(
        make_report(capability="inspect_scope"), context=moved_on
    )
    compatible_stall = make_receipt(
        make_report(capability="inspect_pipeline", changed=False), context=moved_on
    )
    verifier.clock.advance(30)
    matcher = store(
        repository,
        verifier,
        [compatible_stall, compatible_useful, exact_useful, exact_stall],
    )
    result = matcher.lookup(namespace, context, requested=True)
    assert [(match.match_class, match.outcome_class) for match in result.matches] == [
        ("exact", "no_progress"),
        ("exact", "useful_evidence"),
        ("compatible", "useful_evidence"),
        ("compatible", "no_progress"),
    ]
    assert not result.truncated
    # The same corpus read twice produces the identical order.
    assert matcher.lookup(namespace, context, requested=True) == result


def test_matching_truncation_is_explicit(
    repository, verifier, make_receipt, make_report, context, namespace
):
    receipts = [
        make_receipt(make_report(capability=capability, changed=False))
        for capability in ("inspect_pipeline", "inspect_schema_keys", "inspect_scope")
    ]
    receipts += [
        make_receipt(make_report(capability="inspect_scope")) for _ in range(2)
    ]
    matcher = store(repository, verifier, receipts)
    assert repository.count(namespace) == 5
    result = matcher.lookup(namespace, context, requested=True)
    assert len(result.matches) == 4
    assert result.truncated
    assert all(match.match_class == "exact" for match in result.matches)
    narrow = HistoryMatcher(repository, max_references=2)
    tight = narrow.lookup(namespace, context, requested=True)
    assert len(tight.matches) == 2 and tight.truncated
    assert tight.matches == result.matches[:2]


def test_matching_empty_history_is_normal_and_lookup_error_is_explicit(
    repository, context, namespace
):
    matcher = HistoryMatcher(repository)
    empty = matcher.lookup(namespace, context, requested=True)
    assert empty.status == "empty"
    assert empty.matches == () and empty.references == () and not empty.truncated
    unavailable = HistoryMatcher(FailingRepository()).lookup(
        namespace, context, requested=True
    )
    assert unavailable.status == "unavailable"
    assert unavailable.matches == ()
    not_requested = matcher.lookup(namespace, context, requested=False)
    assert not_requested.status == "not_requested"


def test_matching_only_an_exact_no_progress_receipt_blocks(
    repository, verifier, make_receipt, make_report, context, namespace
):
    useful = make_receipt(make_report(capability="inspect_pipeline"))
    provider = make_receipt(
        make_report(capability="inspect_schema_keys", status="provider_error")
    )
    invalid = make_receipt(
        make_report(capability="inspect_scope", status="invalid_evidence")
    )
    matcher = store(repository, verifier, [useful, provider, invalid])
    result = matcher.lookup(namespace, context, requested=True)
    assert len(result.matches) == 3
    assert all(match.match_class == "exact" for match in result.matches)
    assert result.blocked_executions == frozenset()
    stall = make_receipt(make_report(capability="inspect_pipeline", changed=False))
    repository.put(stall, verifier.token)
    blocked = matcher.lookup(namespace, context, requested=True)
    assert blocked.blocked_executions == frozenset({stall.execution_key})
    assert blocked.blocking_match(stall.execution_key).receipt_id == stall.receipt_id
    # An exact invalid-evidence receipt for another capability still never blocks.
    assert blocked.blocking_match(invalid.execution_key) is None
    assert blocked.blocking_match(provider.execution_key) is None


def test_matching_is_invariant_to_aliases_and_operational_identity(
    repository, verifier, make_receipt, make_report, context, namespace
):
    aliased = make_receipt(
        make_report(
            capability="probe_pipeline",
            public_tool_id="inspect-pipeline",
            changed=False,
        )
    )
    assert aliased.selected_capability == "inspect_pipeline"
    canonical = make_receipt(make_report(capability="inspect_pipeline", changed=False))
    assert aliased.execution_key == canonical.execution_key
    matcher = store(repository, verifier, [aliased])
    result = matcher.lookup(namespace, context, requested=True)
    assert result.blocked_executions == frozenset({canonical.execution_key})
    # A new decision and model call for the same facts is still the same context.
    repeated = make_receipt(
        make_report(capability="inspect_pipeline", changed=False),
        decision_id=uuid4(),
        model_call_id=uuid4(),
    )
    assert repeated.decision_context_hash == aliased.decision_context_hash
    assert matcher.classify(repeated, context) == "exact"


def test_matching_different_arguments_do_not_block_the_same_capability(
    repository, verifier, make_receipt, make_report, context, namespace
):
    stall = make_receipt(
        make_report(capability="inspect_pipeline", changed=False, arguments_hash=DIGEST)
    )
    matcher = store(repository, verifier, [stall])
    result = matcher.lookup(namespace, context, requested=True)
    assert result.matches[0].match_class == "exact"
    assert result.blocked_executions == frozenset({("inspect_pipeline", DIGEST)})
    assert result.blocking_match(("inspect_pipeline", "b" * 64)) is None


def test_matching_refuses_a_row_from_another_namespace(
    verifier, make_receipt, stalled_report, context, namespace
):
    foreign = make_receipt(
        stalled_report,
        namespace=ReceiptNamespace(arm="decider", case_id="case-07"),
    )
    matcher = HistoryMatcher(ForeignRepository(foreign))
    with pytest.raises(RuntimeFault) as error:
        matcher.lookup(namespace, context, requested=True)
    assert error.value.reason is Reason.RECEIPT_INVALID
    broken = HistoryMatcher(ForeignRepository({"receipt_id": DIGEST}))
    with pytest.raises(RuntimeFault):
        broken.lookup(namespace, context, requested=True)
