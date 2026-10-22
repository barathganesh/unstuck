"""Step 2: contract, canonical hash, and version-binding tests.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_contracts.py -q
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from unstuck.cascade.cards import (
    CATALOG_VERSION,
    TOOL_CONTRACT_VERSION,
    ToolCapabilityCard,
    canonical_capability,
    capability_cards,
    card_index,
    catalog_hash,
)
from unstuck.cascade.identity import BoundVersions, CallIdentity, DecisionContext
from unstuck.cascade.matching import HistoryLookupResult, ReceiptMatch
from unstuck.cascade.receipts import (
    RECEIPT_REASONS,
    DecisionReceipt,
    EvidenceDelta,
    receipt_identity,
)
from unstuck.cascade.serialization import (
    MAX_RECEIPT_REFERENCES,
    SELECTOR_INPUT_MAX_CHARS,
    TOOL_SUMMARY_MAX_CHARS,
    StageMeasurement,
    bounded_text,
    canonical_digest,
    canonical_text,
)
from unstuck.decision import ReceiptReference
from unstuck.domain import Reason

DIGEST = "a" * 64


def test_contract_cards_are_static_public_and_version_bound(cards):
    assert len(cards) == 3
    identifiers = [binding.card.public_tool_id for binding in cards]
    assert identifiers == ["inspect_pipeline", "inspect_schema_keys", "inspect_scope"]
    for binding in cards:
        card = binding.card
        assert card.catalog_version == CATALOG_VERSION
        assert card.tool_contract_version == TOOL_CONTRACT_VERSION
        assert card.input_schema == "no_arguments"
        assert card.cost_class == "local_read"
        assert card.observable_facts
        # The internal capability never reaches the public card surface.
        assert "capability" not in card.model_dump()
    # Cards are static: two reads produce the identical catalog identity.
    assert catalog_hash(capability_cards()) == catalog_hash(cards)


def test_contract_card_rejects_unknown_fields_and_private_capability():
    base = {
        "public_tool_id": "inspect_scope",
        "description": "Observe declared scope coverage",
        "applies_when": "it is still open whether the declared scope is covered",
        "observable_facts": ("scope_covered",),
    }
    assert ToolCapabilityCard.model_validate(base).public_tool_id == "inspect_scope"
    for changes in (
        {"capability": "inspect_scope"},
        {"input_schema": "free_form"},
        {"cost_class": "remote_write"},
        {"observable_facts": ()},
        {"description": ""},
        {"applies_when": ""},
        {"public_tool_id": "not a tool id"},
    ):
        with pytest.raises(ValidationError):
            ToolCapabilityCard.model_validate(base | changes)


def test_contract_capability_resolves_through_aliases_only(cards):
    index = card_index(cards)
    assert canonical_capability("inspect_pipeline", cards) == "inspect_pipeline"
    for binding in cards:
        for alias in binding.aliases:
            assert alias in index
            assert canonical_capability(alias, cards) == binding.capability
    with pytest.raises(ValueError):
        canonical_capability("invented_tool", cards)


def test_contract_problem_fingerprint_is_task_and_base_query_lineage(
    context, make_state, make_context
):
    assert context.problem_fingerprint == make_context(make_state()).problem_fingerprint
    # A different requested date is a different problem.
    other = make_context(make_state(country_codes=("DE",)))
    assert other.problem_fingerprint != context.problem_fingerprint
    # Candidate membership changes the decision context, never the problem lineage.
    narrowed = make_context(make_state(candidate_count=1))
    assert narrowed.problem_fingerprint == context.problem_fingerprint
    assert narrowed.decision_context_hash != context.decision_context_hash


def test_contract_decision_context_excludes_operational_identity(context):
    first = CallIdentity(
        run_id=uuid4(),
        request_id=uuid4(),
        session_id=uuid4(),
        trace_id=uuid4(),
        lease_owner=uuid4(),
        observed_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    second = CallIdentity(
        run_id=uuid4(),
        request_id=uuid4(),
        session_id=uuid4(),
        trace_id=uuid4(),
        lease_owner=uuid4(),
        attempt=2,
        observed_at=datetime(2026, 10, 1, 1, tzinfo=UTC),
    )
    assert first != second
    # Operational identity is a separate contract; it never enters the hash.
    assert "run_id" not in context.model_dump()
    assert "observed_at" not in context.model_dump()
    with pytest.raises(ValidationError):
        CallIdentity.model_validate(
            first.model_dump() | {"observed_at": datetime(2026, 10, 1)}
        )


def test_contract_decision_context_ignores_reordering_and_serialization(
    context, make_context, make_state
):
    reordered = DecisionContext.model_validate(
        context.model_dump() | {"candidates": tuple(reversed(context.candidates))}
    )
    assert reordered.decision_context_hash == context.decision_context_hash
    with_evidence = DecisionContext.model_validate(
        context.model_dump() | {"evidence_hashes": ("b" * 64, "c" * 64)}
    )
    shuffled = DecisionContext.model_validate(
        context.model_dump() | {"evidence_hashes": ("c" * 64, "b" * 64)}
    )
    assert with_evidence.decision_context_hash == shuffled.decision_context_hash
    assert with_evidence.decision_context_hash != context.decision_context_hash
    # Availability and used capabilities are authoritative identity.
    unavailable = make_context(make_state(available=("inspect_pipeline",)))
    assert unavailable.decision_context_hash != context.decision_context_hash
    used = DecisionContext.model_validate(
        context.model_dump() | {"used_capabilities": ("inspect_pipeline",)}
    )
    assert used.decision_context_hash != context.decision_context_hash


def test_contract_bound_versions_are_complete_and_change_identity(context, versions):
    assert versions.catalog_version == CATALOG_VERSION
    assert versions.tool_contract_version == TOOL_CONTRACT_VERSION
    for field in (
        "dataset_version",
        "data_schema_version",
        "catalog_version",
        "tool_contract_version",
        "model_revision",
        "policy_hash",
        "calibration_hash",
    ):
        assert field in versions.model_dump()
        changed = BoundVersions.model_validate(
            versions.model_dump()
            | {field: DIGEST if field.endswith("_hash") else "changed-1"}
        )
        assert changed != versions
        assert (
            DecisionContext.model_validate(
                context.model_dump() | {"versions": changed}
            ).decision_context_hash
            != context.decision_context_hash
        )
    with pytest.raises(ValidationError):
        BoundVersions.model_validate(versions.model_dump() | {"extra": 1})


def test_contract_evidence_delta_proves_the_observed_change():
    unchanged = EvidenceDelta(evidence_before_hash=DIGEST, evidence_after_hash=DIGEST)
    assert not unchanged.changed and unchanged.evidence_ids == ()
    identifier = uuid4()
    changed = EvidenceDelta(
        evidence_before_hash=DIGEST,
        evidence_after_hash="b" * 64,
        evidence_ids=(identifier,),
    )
    assert changed.changed and changed.evidence_ids == (identifier,)
    for payload in (
        {
            "evidence_before_hash": DIGEST,
            "evidence_after_hash": DIGEST,
            "evidence_ids": (identifier,),
        },
        {"evidence_before_hash": DIGEST, "evidence_after_hash": "b" * 64},
        {
            "evidence_before_hash": DIGEST,
            "evidence_after_hash": "b" * 64,
            "evidence_ids": (identifier, identifier),
        },
        {"evidence_before_hash": "short", "evidence_after_hash": DIGEST},
    ):
        with pytest.raises(ValidationError):
            EvidenceDelta.model_validate(payload)


def test_contract_receipt_identity_is_canonical_and_audit_time_is_excluded(
    make_receipt, useful_report
):
    receipt = make_receipt(useful_report)
    assert receipt.receipt_id == receipt_identity(receipt)
    assert len(receipt.receipt_id) == 64
    later = DecisionReceipt.model_validate(
        receipt.model_dump()
        | {"observed_at": receipt.observed_at + timedelta(seconds=30)}
    )
    assert later.receipt_id == receipt.receipt_id
    assert later != receipt
    renamed = DecisionReceipt.model_validate(
        receipt.model_dump()
        | {
            "public_tool_id": "inspect_scope",
            "receipt_id": receipt_identity(
                receipt.model_dump() | {"public_tool_id": "inspect_scope"}
            ),
        }
    )
    assert renamed.receipt_id != receipt.receipt_id
    with pytest.raises(ValidationError):
        DecisionReceipt.model_validate(receipt.model_dump() | {"receipt_id": DIGEST})
    with pytest.raises(ValidationError):
        DecisionReceipt.model_validate(receipt.model_dump() | {"confidence": 0.9})


def test_contract_receipt_outcome_taxonomy_is_closed_and_coherent(
    make_receipt, useful_report, stalled_report
):
    useful = make_receipt(useful_report)
    stalled = make_receipt(stalled_report)
    assert useful.outcome_class == "useful_evidence" and not useful.blocking
    assert stalled.outcome_class == "no_progress" and stalled.blocking
    assert stalled.execution_key == ("inspect_schema_keys", stalled.arguments_hash)
    assert {reason for reason in RECEIPT_REASONS} <= set(Reason)
    for changes in (
        {"outcome_class": "probably_useful"},
        {"outcome_class": "useful_evidence"},
        {"reason_code": Reason.LEASE_EXPIRED},
        {"namespace": {"arm": "invented", "case_id": "case-01"}},
        {
            "delta": {
                "evidence_before_hash": DIGEST,
                "evidence_after_hash": "b" * 64,
                "evidence_ids": (uuid4(),),
            }
        },
    ):
        payload = stalled.model_dump() | changes
        with pytest.raises(ValidationError):
            DecisionReceipt.model_validate(
                payload | {"receipt_id": receipt_identity(payload)}
            )


def test_contract_history_result_and_core_reference_projection_agree(
    make_receipt, stalled_report
):
    receipt = make_receipt(stalled_report)
    match = ReceiptMatch(
        receipt_id=receipt.receipt_id,
        match_class="exact",
        outcome_class=receipt.outcome_class,
        public_tool_id=receipt.public_tool_id,
        selected_capability=receipt.selected_capability,
        arguments_hash=receipt.arguments_hash,
        reason_code=receipt.reason_code,
    )
    assert match.blocking
    empty = HistoryLookupResult(status="empty")
    assert empty.matches == () and empty.references == ()
    assert empty.blocked_executions == frozenset()
    matched = HistoryLookupResult(status="matched", matches=(match,))
    assert matched.receipt_ids == (receipt.receipt_id,)
    assert matched.blocked_executions == frozenset({receipt.execution_key})
    reference = matched.references[0]
    assert isinstance(reference, ReceiptReference)
    assert reference.receipt_id == receipt.receipt_id
    assert reference.match_class == "exact"
    assert reference.outcome_class == "no_progress"
    assert "selected_capability" not in reference.model_dump()
    for changes in (
        {"status": "matched", "matches": ()},
        {"status": "empty", "matches": (match,)},
        {"status": "not_requested", "matches": (match,)},
        {"status": "unavailable", "matches": (match,)},
        {"status": "empty", "truncated": True},
        {"status": "matched", "matches": (match, match)},
        {"status": "invented"},
    ):
        with pytest.raises(ValidationError):
            HistoryLookupResult.model_validate(
                {"status": "matched", "matches": (match,)} | changes
            )


def test_contract_canonical_serialization_bounds_and_measurements():
    assert canonical_text({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonical_digest({"b": 1, "a": 2}) == canonical_digest({"a": 2, "b": 1})
    assert len(canonical_digest({"a": 1})) == 64
    with pytest.raises(ValueError):
        canonical_text({"a": float("inf")})
    with pytest.raises(ValueError):
        canonical_text({"a": "x" * 64}, max_chars=16)
    assert bounded_text("abc", 8) == ("abc", False)
    assert bounded_text("a" * 12, 8) == ("a" * 8, True)
    assert (SELECTOR_INPUT_MAX_CHARS, TOOL_SUMMARY_MAX_CHARS) == (8000, 1000)
    assert MAX_RECEIPT_REFERENCES == 4
    assert StageMeasurement().availability == "not_measured"
    assert StageMeasurement(availability="measured", value=1.5).value == 1.5
    for payload in (
        {"availability": "measured"},
        {"availability": "not_called", "value": 0.0},
        {"availability": "invented"},
        {"availability": "measured", "value": -1.0},
    ):
        with pytest.raises(ValidationError):
            StageMeasurement.model_validate(payload)
