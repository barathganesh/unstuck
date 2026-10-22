"""Retrieved history: which outcome belonged to which receipt, and why.

The defect pinned here is that history telemetry flattened the lookup. The
durable event wrote receipt identities, match classes and outcome classes as
separate arrays; the projection kept the identities and the match classes and
reduced the outcomes to the first one, with no per-receipt public tool, reason
code, blocking eligibility or binding. A multi-receipt audit therefore could
not say which outcome belonged to which receipt, or why a receipt was exact
rather than compatible, and the arrays could drift apart unchecked.

The second defect pinned here is that the schema declared catalog and
tool-contract version fields the selector-request projection never populated,
so the record could not say which catalog, contract, schema, policy and
calibration the decision was bound to.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_history_reconstruction.py -q
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from unstuck.cascade.offline import ScriptedChoice
from unstuck.cascade.repository import LocalReceiptRepository
from unstuck.domain import Counters
from unstuck.events import EventRecord, Measurement
from unstuck.telemetry import (
    DecisionTelemetry,
    HistoryReference,
    execution_key,
    map_event,
)

from .conftest import PIPELINE, SCHEMA, SCOPE, compose

QUERY_HASH = "c" * 64
CONTEXT_HASH = "e" * 64
FINGERPRINT = "f" * 64
FIRST = "1" * 64
SECOND = "2" * 64
VERSIONS = "3" * 64


def reference(receipt_id: str, **changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "receipt_id": receipt_id,
        "match_class": "exact",
        "outcome_class": "no_progress",
        "public_tool_id": SCOPE,
        "reason_code": "insufficient_evidence",
        "blocking_eligible": True,
        "admitted_evidence": False,
        "arguments_hash": QUERY_HASH,
        "binding_hash": execution_key(SCOPE, QUERY_HASH),
    }
    return base | changes


def event(kind: str, payload: dict[str, object]) -> EventRecord:
    return EventRecord(
        run_id=uuid4(),
        incident_id=uuid4(),
        sequence=1,
        timestamp=datetime.now(UTC),
        type=kind,
        input_hash=QUERY_HASH,
        output_hash=QUERY_HASH,
        budgets=Counters(),
        duration_ms=Measurement(),
        payload=payload,  # type: ignore[arg-type]
    )


def test_each_retrieved_receipt_keeps_its_own_outcome_tool_reason_and_binding():
    """Regression: only the first outcome used to survive the projection."""
    mapped = map_event(
        event(
            "decision_history_returned",
            {
                "history_status": "matched",
                "receipt_ids": [FIRST, SECOND],
                "reference_count": 2,
                "history_references": [
                    reference(FIRST),
                    reference(
                        SECOND,
                        match_class="compatible",
                        outcome_class="useful_evidence",
                        public_tool_id=PIPELINE,
                        reason_code="populated_after_repair",
                        blocking_eligible=False,
                        admitted_evidence=True,
                        binding_hash=execution_key(PIPELINE, QUERY_HASH),
                    ),
                ],
            },
        )
    )
    decision = mapped.decision
    assert decision is not None
    assert decision.history_receipt_ids == (FIRST, SECOND)
    assert decision.history_reference_count == 2
    blocker, evidence = decision.history_references
    assert blocker.receipt_id == FIRST
    assert blocker.match_class == "exact"
    assert blocker.outcome_class == "no_progress"
    assert blocker.public_tool_id == SCOPE
    assert blocker.reason_code == "insufficient_evidence"
    assert blocker.blocking_eligible is True
    assert blocker.binding_hash == execution_key(SCOPE, QUERY_HASH)
    # The second receipt's own outcome survives rather than the first one's.
    assert evidence.receipt_id == SECOND
    assert evidence.match_class == "compatible"
    assert evidence.outcome_class == "useful_evidence"
    assert evidence.public_tool_id == PIPELINE
    assert evidence.blocking_eligible is False
    assert evidence.admitted_evidence is True
    assert evidence.binding_hash != blocker.binding_hash
    # The derived view over the list cannot drift away from it.
    assert decision.history_match_classes == ("exact", "compatible")


def test_the_top_ranked_match_is_named_from_one_reference_not_two_arrays():
    """Every field of the single receipt describes the same receipt."""
    mapped = map_event(
        event(
            "decision_history_returned",
            {
                "history_status": "matched",
                "receipt_ids": [FIRST, SECOND],
                "history_references": [
                    reference(FIRST),
                    reference(
                        SECOND,
                        match_class="compatible",
                        outcome_class="useful_evidence",
                        blocking_eligible=False,
                    ),
                ],
            },
        )
    )
    receipt = mapped.receipt
    assert receipt is not None
    assert receipt.receipt_id == FIRST
    assert receipt.match_class == "exact"
    assert receipt.outcome_class == "no_progress"
    assert receipt.reason_code == "insufficient_evidence"
    assert receipt.blocking_eligible is True


def test_contract_a_reference_list_refuses_duplicates_and_mismatched_ids():
    duplicate = HistoryReference.model_validate(reference(FIRST))
    with pytest.raises(ValidationError):
        DecisionTelemetry(history_references=(duplicate, duplicate))
    with pytest.raises(ValidationError):
        DecisionTelemetry(
            history_receipt_ids=(SECOND,), history_references=(duplicate,)
        )
    with pytest.raises(ValidationError):
        DecisionTelemetry(history_reference_count=2, history_references=(duplicate,))
    with pytest.raises(ValidationError):
        DecisionTelemetry(
            history_references=tuple(
                HistoryReference.model_validate(reference(str(index) * 64))
                for index in range(5)
            )
        )
    agreed = DecisionTelemetry(
        history_receipt_ids=(FIRST,),
        history_references=(duplicate,),
        history_reference_count=1,
        history_status="matched",
    )
    assert agreed.history_match_classes == ("exact",)


def test_one_malformed_entry_refuses_the_whole_list_rather_than_shortening_it():
    """A shorter history reads as a smaller lookup than the one that happened."""
    mapped = map_event(
        event(
            "decision_history_returned",
            {
                "history_status": "matched",
                "receipt_ids": [FIRST, SECOND],
                "history_references": [
                    reference(FIRST),
                    reference(SECOND, match_class="invented_class"),
                ],
            },
        )
    )
    assert mapped.decision is not None
    assert mapped.decision.history_references == ()
    assert mapped.receipt is not None and mapped.receipt.receipt_id is None
    # A list holding anything but entries, and a field of the wrong shape, are
    # both an absent history rather than a partial one.
    for payload in ({"history_references": [FIRST]}, {"history_references": FIRST}):
        other = map_event(event("decision_history_returned", payload))
        assert other.decision is not None
        assert other.decision.history_references == ()


def test_a_selector_request_records_the_versions_the_decision_was_bound_to():
    """Regression: the declared version fields were never populated."""
    mapped = map_event(
        event(
            "decider_first_requested",
            {
                "pass_index": 1,
                "state_hash": QUERY_HASH,
                "decision_context_hash": CONTEXT_HASH,
                "problem_fingerprint": FINGERPRINT,
                "history_status": "matched",
                "receipt_ids": [FIRST],
                "reference_count": 1,
                "history_references": [
                    reference(
                        FIRST,
                        decision_context_hash=CONTEXT_HASH,
                        versions_hash=VERSIONS,
                    )
                ],
                "available_tool_ids": [SCHEMA],
                "model_revision": "scripted-v1",
                "catalog_version": "catalog-1",
                "tool_contract_version": "contract-1",
                "data_schema_version": "schema-1",
                "policy_hash": FIRST,
                "calibration_hash": SECOND,
            },
        )
    )
    decision = mapped.decision
    assert decision is not None
    assert decision.model_revision == "scripted-v1"
    assert decision.catalog_version == "catalog-1"
    assert decision.tool_contract_version == "contract-1"
    assert decision.data_schema_version == "schema-1"
    assert decision.policy_hash == FIRST
    assert decision.calibration_hash == SECOND
    # The history D1 was handed, with the binding each receipt was classified
    # against, is on the same event that says what the call was bound to.
    bound = decision.history_references[0]
    assert bound.decision_context_hash == CONTEXT_HASH
    assert bound.versions_hash == VERSIONS


def test_contract_a_real_cross_call_lookup_is_reconstructable_receipt_by_receipt(
    tmp_path,
):
    receipts = LocalReceiptRepository()
    with compose(
        tmp_path,
        choices=[
            ScriptedChoice(selected_id=SCOPE),
            ScriptedChoice(selected_id=PIPELINE),
        ],
        key="call-1",
        receipts=receipts,
        seed=(SCOPE,),
    ) as first:
        assert first.execute()["status"] == "unresolved"

    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE), ScriptedChoice(selected_id=SCHEMA)],
        key="call-2",
        receipts=receipts,
        seed=(SCOPE,),
    ) as second:
        assert second.execute()["history_status"] == "matched"

    history = second.record("decision_history_returned")
    assert history.decision is not None
    references = history.decision.history_references
    assert references
    assert len(references) == len(history.decision.history_receipt_ids)
    assert tuple(item.receipt_id for item in references) == (
        history.decision.history_receipt_ids
    )
    # The receipt that can refuse a repetition is ranked first, and it carries
    # its own reason, tool and binding rather than borrowing another's.
    blocker = references[0]
    assert blocker.match_class == "exact"
    assert blocker.outcome_class == "no_progress"
    assert blocker.blocking_eligible is True
    assert blocker.public_tool_id == SCOPE
    assert blocker.reason_code
    assert blocker.binding_hash == execution_key(SCOPE, blocker.arguments_hash or "")

    refusal = second.record("repeat_blocked")
    assert refusal.tool is not None and refusal.receipt is not None
    assert refusal.receipt.receipt_id == blocker.receipt_id
    # The opaque binding the refusal was about is the one the lookup recorded.
    assert refusal.tool.execution_key == blocker.binding_hash

    requested = second.record("decider_first_requested")
    assert requested.decision is not None
    assert requested.decision.catalog_version is not None
    assert requested.decision.tool_contract_version is not None
    assert requested.decision.data_schema_version is not None
    assert requested.decision.policy_hash is not None
    assert requested.decision.calibration_hash is not None
    bound = requested.decision.history_references[0]
    assert bound.receipt_id == blocker.receipt_id
    assert bound.versions_hash is not None
    assert bound.decision_context_hash == requested.decision.decision_context_hash
