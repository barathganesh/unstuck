import asyncio
import json
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_controller import FakeDecider, replace, setup
from test_receipt_controller import fingerprint

from unstuck.cascade.receipts import ReceiptNamespace
from unstuck.cascade.repository import (
    LocalReceiptRepository,
    ReceiptLookupError,
    ReceiptPage,
)
from unstuck.decision import (
    DecisionHistory,
    DecisionState,
    ReceiptReference,
    SelectionAuthority,
    default_catalog,
)
from unstuck.domain import (
    Counters,
    Incident,
    QuerySpec,
    Reason,
    RuntimeFault,
    Settings,
    TaskSpec,
    terminal_mapping,
)
from unstuck.events import TerminalResult
from unstuck.fixtures import load_fixture
from unstuck.policy import consume

RECEIPT_ID = "a" * 64
ARGUMENTS_HASH = "b" * 64
#: The namespace a controller composes for itself from its own arm.
MEMORY_NAMESPACE = ReceiptNamespace(arm="decider_memory", case_id="local")
OBSOLETE_RESULT_FIELDS = ("jev_invoked", "jev_not_called_reason", "escalation_status")


def reference(**changes):
    return ReceiptReference.model_validate(
        {
            "receipt_id": RECEIPT_ID,
            "match_class": "exact",
            "outcome_class": "no_progress",
            "public_tool_id": "inspect_pipeline",
            "arguments_hash": ARGUMENTS_HASH,
            "reason_code": Reason.INSUFFICIENT,
        }
        | changes
    )


def matched_history(**changes):
    return DecisionHistory.model_validate(
        {"status": "matched", "references": (reference(),)} | changes
    )


def decision_state(**changes):
    query = QuerySpec(
        task=TaskSpec(dataset_id="ds-17", order_date_utc=date(2026, 10, 1)),
        dataset_version="v17",
    )
    return DecisionState.model_validate(
        {
            "task": query.task,
            "query": query,
            "task_hash": query.task_hash,
            "query_hash": query.query_hash,
            "dataset_version": query.dataset_version,
            "candidates": (default_catalog()[0].public,),
            "budgets": Counters(),
            "remaining_ms": 120000,
        }
        | changes
    )


def test_contract_OD02_receipt_reference_is_bounded_and_typed():
    assert reference().receipt_id == RECEIPT_ID
    for changes in (
        {"receipt_id": "not-a-digest"},
        {"arguments_hash": "A" * 64},
        {"match_class": "partial"},
        {"outcome_class": "probably_useful"},
        {"reason_code": "invented_reason"},
        {"extra": 1},
    ):
        with pytest.raises(ValidationError):
            reference(**changes)


def test_contract_OD02_history_status_matches_references():
    assert DecisionHistory().status == "not_requested"
    assert DecisionHistory().references == ()
    assert not DecisionHistory().truncated
    assert matched_history().references[0].receipt_id == RECEIPT_ID
    assert matched_history(truncated=True).truncated
    for changes in (
        {"status": "matched", "references": ()},
        {"status": "not_requested", "references": (reference(),)},
        {"status": "unavailable", "references": (reference(),)},
        {"status": "empty", "references": (reference(),)},
        {"status": "empty", "references": (), "truncated": True},
        {"status": "not_requested", "references": (), "truncated": True},
        {"status": "matched", "references": (reference(), reference())},
        {"status": "invented"},
    ):
        with pytest.raises(ValidationError):
            DecisionHistory.model_validate(
                {"status": "matched", "references": (reference(),)} | changes
            )


def test_contract_OD02_history_reaches_the_bounded_selector_input():
    state = decision_state(history=matched_history())
    assert state.history.status == "matched"
    assert len(state.model_dump_json()) <= 8000
    assert "Decision Receipt" in state.question
    oversized = tuple(reference(receipt_id=format(index, "064x")) for index in range(4))
    assert DecisionHistory(status="matched", references=oversized, truncated=True)
    with pytest.raises(ValidationError):
        DecisionHistory(
            status="matched",
            references=oversized + (reference(receipt_id="f" * 64),),
        )


@pytest.mark.parametrize(
    "marker",
    ["expected_rows_12", "hidden_category_a", "evaluator_only", "authorization: token"],
)
def test_security_OD02_history_rejects_evaluator_or_secret_markers(marker):
    with pytest.raises(ValidationError):
        decision_state(
            history=matched_history(references=(reference(public_tool_id=marker),))
        )


def test_contract_OD02_generic_reason_taxonomy_replaces_jev():
    values = {reason.value for reason in Reason}
    assert "provider_error_jev" not in values
    assert not any("jev" in value for value in values)
    for value in (
        "history_unavailable",
        "receipt_invalid",
        "repeat_blocked_no_alternative",
    ):
        assert value in values
        assert terminal_mapping(Reason(value)) == (
            "UNRESOLVED",
            "UNRESOLVED",
            "unresolved",
        )


def test_budget_OD02_history_lookups_are_counted_and_capped():
    assert Counters().history_lookups == 0
    assert not hasattr(Counters(), "jev_calls")
    counters = Counters()
    for expected in (1, 2):
        counters = consume(counters, "history_lookups")
        assert counters.history_lookups == expected
    with pytest.raises(RuntimeFault, match="history_unavailable"):
        consume(counters, "history_lookups")
    with pytest.raises(RuntimeFault, match="internal_invariant"):
        consume(Counters(), "jev_calls")


def test_contract_OD02_baseline_identifiers_are_generic():
    assert Settings().baseline_id == "decider"
    assert Settings(baseline_id="decider_memory").baseline_id == "decider_memory"
    for invalid in ("decider_jev", "jev", "bma"):
        with pytest.raises(ValidationError):
            Settings(baseline_id=invalid)


def test_contract_OD02_selection_authority_carries_history_and_block():
    authority = SelectionAuthority(
        decision_id=uuid4(),
        selected_id="inspect_pipeline",
        confidence=0.95,
        model_version="scripted-v1",
        accepted=True,
    )
    assert authority.pass_index == 1
    assert authority.history_status == "not_requested"
    assert authority.receipt_ids == ()
    assert authority.blocked_tool_id is None and authority.blocked_reason is None
    blocked = replace(
        authority,
        pass_index=2,
        history_status="matched",
        receipt_ids=(RECEIPT_ID,),
        blocked_tool_id="inspect_schema_keys",
        blocked_reason=Reason.REPEAT_BLOCKED,
    )
    assert blocked.pass_index == 2 and blocked.receipt_ids == (RECEIPT_ID,)
    for changes in (
        {"pass_index": 3},
        {"history_status": "matched", "receipt_ids": ()},
        {"history_status": "not_requested", "receipt_ids": (RECEIPT_ID,)},
        {"receipt_ids": (RECEIPT_ID, RECEIPT_ID), "history_status": "matched"},
        {"blocked_tool_id": "inspect_scope"},
        {"blocked_reason": Reason.REPEAT_BLOCKED},
        {"history_truncated": True},
    ):
        with pytest.raises(ValidationError):
            replace(authority, **changes)


def test_contract_OD02_terminal_result_drops_jev_and_binds_history(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    result = asyncio.run(controller.run(run.state.run_id))
    for field in OBSOLETE_RESULT_FIELDS:
        assert field not in result
        assert field not in TerminalResult.model_fields
    assert result["history_status"] == "not_requested"
    assert result["receipt_ids"] == []
    assert result["history_truncated"] is False
    assert result["selection_pass"] == 1
    assert result["blocked_tool_id"] is None
    assert result["blocked_reason_code"] is None
    assert result["baseline_id"] == "decider"
    assert result["pass_count"] == 1
    validated = TerminalResult.model_validate_json(json.dumps(result))
    assert validated.history_status == "not_requested"
    for changes in (
        {"history_status": "matched"},
        {"receipt_ids": [RECEIPT_ID]},
        {"history_truncated": True},
        {"blocked_tool_id": "inspect_scope"},
        {"blocked_reason_code": "repeat_blocked_no_alternative"},
        {"selection_pass": 3},
        {"receipt_ids": [RECEIPT_ID, RECEIPT_ID], "history_status": "matched"},
        {"baseline_id": "decider_jev"},
    ):
        with pytest.raises(ValidationError):
            TerminalResult.model_validate_json(json.dumps(result | changes))


def test_invariant_OD02_snapshot_binds_history_to_lookups_and_selection(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    asyncio.run(controller.run(run.state.run_id))
    without_lookup = repo.load_run(run.state.run_id)
    assert without_lookup.history is None
    assert without_lookup.incident.counters.history_lookups == 0
    with pytest.raises(ValidationError, match="recorded lookups"):
        replace(without_lookup, history=DecisionHistory())

    second, repo_two, run_two, _ = setup(
        tmp_path / "second", settings=Settings(baseline_id="decider_memory")
    )
    asyncio.run(second.run(run_two.state.run_id))
    with_lookup = repo_two.load_run(run_two.state.run_id)
    assert with_lookup.history == DecisionHistory(status="empty")
    assert with_lookup.incident.counters.history_lookups == 1
    with pytest.raises(ValidationError, match="recorded lookups"):
        replace(with_lookup, history=None)
    with pytest.raises(ValidationError, match="selection history binding mismatch"):
        replace(with_lookup, history=DecisionHistory(status="unavailable"))


def test_OD02_direct_path_requests_no_history_and_no_second_pass(tmp_path):
    controller, repo, run, decider = setup(tmp_path)
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    assert result["reason_code"] == "populated_after_repair"
    assert final.incident.counters == Counters(
        report_queries=2,
        diagnostic_reads=1,
        decider_calls=1,
        repair_attempts=1,
        agent_turns=1,
    )
    assert final.incident.counters.history_lookups == 0
    assert decider.calls[0].history == DecisionHistory()
    types = [event.type for event in final.events]
    for absent in (
        "decision_history_requested",
        "decision_history_returned",
        "decider_final_requested",
        "decider_final_returned",
        "repeat_blocked",
    ):
        assert absent not in types
    # The arm keeps no cross-call memory, but every verified read is still
    # admitted as a receipt under this call's own lease.
    assert types.count("decision_receipt_written") == 1
    assert final.selection is not None
    assert final.selection == final.first_selection
    assert final.selection.pass_index == 1
    assert final.selection.history_status == "not_requested"


def memory_call(tmp_path, key, store, choice="inspect_scope"):
    """One memory-arm call over a shared receipt store. Offline throughout."""
    return setup(
        tmp_path,
        decider=FakeDecider(choice),
        key=key,
        settings=Settings(baseline_id="decider_memory"),
        receipts=store,
    )


def test_OD02_a_retrieved_history_is_recorded_and_projected(tmp_path):
    """The looked-up history is what reaches the selector and the record."""
    store = LocalReceiptRepository()
    first, _, cold_run, cold_decider = memory_call(tmp_path, "cold-call", store)
    cold = asyncio.run(first.run(cold_run.state.run_id))
    stored = store.find(MEMORY_NAMESPACE, fingerprint(load_fixture()))
    # The cold read admitted a canonical fact that opened no supported
    # disposition. That is the disposition-neutral class, and it is exactly
    # what opens the one same-call second decision this selector cannot answer.
    assert len(stored) == 1 and stored[0].outcome_class == "disposition_unchanged"
    assert stored[0].delta.changed and not stored[0].delta.progressed
    # It is eligible to refuse the identical repetition on a later call under
    # an identical context. Eligibility is not a selection or a repair.
    assert stored[0].blocking is True
    assert len(cold_decider.calls) == 2 and cold["pass_count"] == 2

    controller, repo, run, decider = memory_call(
        tmp_path, "warm-call", store, choice="inspect_schema_keys"
    )
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    assert result["status"] == "completed"
    assert result["history_status"] == "matched"
    assert result["receipt_ids"] == [stored[0].receipt_id]
    assert result["history_truncated"] is False
    assert result["baseline_id"] == "decider_memory"
    assert result["pass_count"] == 1 and result["selection_pass"] == 1
    assert final.incident.counters.history_lookups == 1
    assert decider.calls[0].history.status == "matched"
    assert decider.calls[0].history.receipt_ids == (stored[0].receipt_id,)
    types = [event.type for event in final.events]
    assert (
        types.index("decision_history_requested")
        < types.index("decision_history_returned")
        < types.index("decider_first_requested")
    )
    returned = next(
        event for event in final.events if event.type == "decision_history_returned"
    )
    assert returned.payload["history_status"] == "matched"
    assert returned.payload["receipt_ids"] == [stored[0].receipt_id]
    assert returned.payload["match_classes"] == ["exact"]
    assert returned.payload["outcome_classes"] == ["disposition_unchanged"]
    # One structured entry per retrieved receipt beside them, so a
    # multi-receipt lookup is readable receipt by receipt and the arrays are
    # proven to agree with it rather than able to drift.
    assert returned.payload["reference_count"] == 1
    reference = returned.payload["history_references"][0]
    assert reference["receipt_id"] == stored[0].receipt_id
    assert reference["match_class"] == "exact"
    assert reference["outcome_class"] == "disposition_unchanged"
    # Eligible, and still not a block: this warm call chose a different
    # capability, so nothing was refused and one selector call answered it.
    assert reference["blocking_eligible"] is True
    assert reference["public_tool_id"] and reference["binding_hash"]
    assert returned.payload["truncated"] is False
    assert "selected_capability" not in json.dumps(returned.payload)
    requested = next(
        event for event in final.events if event.type == "decision_history_requested"
    )
    assert requested.payload["history_status"] == "matched"
    assert requested.payload["reference_count"] == 1


@pytest.mark.parametrize("status", ["unavailable", "empty"])
def test_OD02_explicit_absent_history_never_becomes_a_cached_decision(tmp_path, status):
    class Unreadable(LocalReceiptRepository):
        """A store that cannot be read. The lookup says so explicitly."""

        def find(self, namespace, problem_fingerprint):
            raise ReceiptLookupError("scripted store failure")

    store = Unreadable() if status == "unavailable" else LocalReceiptRepository()
    controller, repo, run, decider = memory_call(
        tmp_path, "absent-history", store, choice="inspect_schema_keys"
    )
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    assert result["history_status"] == status
    assert result["receipt_ids"] == []
    assert final.incident.counters.history_lookups == 1
    if status == "empty":
        assert result["status"] == "completed"
        assert len(decider.calls) == 1
        assert decider.calls[0].history.status == "empty"
        assert final.selection is not None
        assert final.selection.history_status == "empty"
    else:
        # An unreadable index is never another arm's cold start: the run says
        # so and decides nothing.
        assert result["status"] == "unresolved"
        assert result["reason_code"] == "history_unavailable"
        assert not decider.calls
        assert final.selection is None


def test_OD02_a_truncated_retrieval_is_recorded_and_decides_nothing(tmp_path):
    """A bounded read that left receipts unread is an absent history.

    The budget stopped before the namespace was exhausted, so an exact
    no-progress blocker may be in the remainder. The run says so explicitly,
    records the truncation on both history events, and selects nothing.
    """

    class Truncating(LocalReceiptRepository):
        """A store whose read budget always stops before the namespace ends."""

        def find_page(self, namespace, problem_fingerprint, *, budget=None):
            return ReceiptPage(receipts=(), truncated=True, pages_read=1)

    controller, repo, run, decider = memory_call(
        tmp_path, "truncated-history", Truncating(), choice="inspect_schema_keys"
    )
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    assert result["status"] == "unresolved"
    assert result["reason_code"] == "history_unavailable"
    # Never a cold start and never a no-match.
    assert result["history_status"] == "unavailable"
    assert result["receipt_ids"] == []
    assert result["history_truncated"] is False
    assert final.incident.counters.history_lookups == 1
    assert final.incident.counters.decider_calls == 0
    assert final.incident.counters.diagnostic_reads == 0
    assert not decider.calls
    assert final.selection is None
    assert final.history is not None and final.history.status == "unavailable"
    types = [event.type for event in final.events]
    assert "decider_first_requested" not in types
    assert "probe_started" not in types
    for name in ("decision_history_requested", "decision_history_returned"):
        event = next(item for item in final.events if item.type == name)
        assert event.payload["history_status"] == "unavailable"
        assert event.payload["history_retrieval_truncated"] is True


def test_OD02_an_exhausted_retrieval_records_no_truncation(tmp_path):
    store = LocalReceiptRepository()
    controller, repo, run, _ = memory_call(
        tmp_path, "exhausted-history", store, choice="inspect_schema_keys"
    )
    asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    for name in ("decision_history_requested", "decision_history_returned"):
        event = next(item for item in final.events if item.type == name)
        assert event.payload["history_status"] == "empty"
        assert event.payload["history_retrieval_truncated"] is False


@pytest.mark.parametrize(
    "supplied",
    ["not_a_history", {"status": "matched"}, None, "unvalidated"],
    ids=["text", "mapping", "missing", "unvalidated"],
)
def test_security_OD02_untyped_history_fails_closed(tmp_path, supplied):
    controller, repo, run, decider = setup(tmp_path)
    if supplied == "unvalidated":
        supplied = DecisionHistory.model_construct(status="matched", references=())
    controller.history = supplied
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    assert result["reason_code"] == "receipt_invalid"
    assert result["status"] == "unresolved"
    assert result["history_status"] == "not_requested"
    assert result["receipt_ids"] == []
    assert not decider.calls
    assert final.history is None
    assert final.incident.counters.history_lookups == 0
    assert final.incident.counters.diagnostic_reads == 0


def test_OD02_history_does_not_replace_a_fresh_choice(tmp_path):
    """A matching useful receipt is evidence, never a replayed choice."""
    store = LocalReceiptRepository()
    first, _, cold_run, _ = memory_call(
        tmp_path, "cold-call", store, choice="inspect_schema_keys"
    )
    asyncio.run(first.run(cold_run.state.run_id))
    stored = store.find(MEMORY_NAMESPACE, fingerprint(load_fixture()))
    assert [receipt.outcome_class for receipt in stored] == ["useful_evidence"]

    controller, repo, run, decider = memory_call(tmp_path, "warm-call", store)
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    probes = [
        event.payload["tool_id"]
        for event in final.events
        if event.type == "probe_started"
    ]
    # The selector chose its own read, not the one the receipt describes.
    assert probes == ["inspect_scope"]
    assert result["history_status"] == "matched"
    assert result["first_choice"] == "inspect_scope"
    assert result["status"] == "unresolved"
    assert decider.calls[0].history.receipt_ids == (stored[0].receipt_id,)


def test_OD02_history_binding_is_frozen_before_selection(tmp_path):
    """Retrieval owns the history binding; nothing can swap it mid-decision."""
    store = LocalReceiptRepository()
    first, _, cold_run, _ = memory_call(tmp_path, "cold-call", store)
    asyncio.run(first.run(cold_run.state.run_id))
    stored = store.find(MEMORY_NAMESPACE, fingerprint(load_fixture()))

    controller, repo, run, decider = memory_call(
        tmp_path, "warm-call", store, choice="inspect_schema_keys"
    )
    decider.hook = lambda state: setattr(controller, "history", DecisionHistory())
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    assert result["status"] == "completed"
    assert result["history_status"] == "matched"
    assert result["receipt_ids"] == [stored[0].receipt_id]
    assert decider.calls[0].history.receipt_ids == (stored[0].receipt_id,)
    assert final.history is not None and final.history.status == "matched"
    assert final.selection is not None
    assert final.selection.receipt_ids == (stored[0].receipt_id,)


def test_OD02_incident_states_cover_the_receipt_and_final_seam():
    query = QuerySpec(
        task=TaskSpec(dataset_id="ds-17", order_date_utc=date(2026, 10, 1)),
        dataset_version="v17",
    )
    incident = Incident.open(uuid4(), query)
    for state in (
        "DECISION_HISTORY",
        "RECEIPT_CHECK",
        "DECIDER_FINAL",
        "RUN_PROBE_FINAL",
    ):
        assert replace(incident, state=state).state == state
    with pytest.raises(ValidationError):
        replace(incident, state="JEV_REVIEW")


def test_OD02_runtime_surface_carries_no_obsolete_or_personal_identifier(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    controller.history = matched_history()
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    rendered = json.dumps(
        {
            "result": result,
            "snapshot": json.loads(final.model_dump_json()),
        }
    ).lower()
    for term in ("jev", "escalation", "jarvis", "hobson", "claude", "assistant"):
        assert term not in rendered


def test_OD02_core_source_uses_generic_roles_only():
    root = Path(__file__).parents[2]
    paths = [
        path
        for path in (root / "src" / "unstuck").rglob("*")
        if path.suffix in {".py", ".json"} and "cascade" not in path.parts
    ]
    assert len(paths) >= 14
    for path in paths:
        text = path.read_text().lower()
        for term in ("jev", "jarvis", "hobson", "claude", "synthetic_person", "bma"):
            assert term not in text, f"{path} mentions {term}"


def test_OD02_core_doc_uses_generic_roles_only():
    root = Path(__file__).parents[2]
    text = (root / "docs" / "core-runtime.md").read_text().lower()
    for term in ("jev", "jarvis", "hobson", "claude", "synthetic_person", "bma"):
        assert term not in text


def test_OD02_offline_skeleton_result_is_generic(tmp_path):
    from unstuck.cli import skeleton

    result = asyncio.run(skeleton(tmp_path))
    assert result["status"] == "completed"
    assert result["history_status"] == "not_requested"
    assert result["receipt_ids"] == []
    assert result["baseline_id"] == "decider"
    assert set(OBSOLETE_RESULT_FIELDS).isdisjoint(result)
    events = [
        json.loads(line)
        for line in (tmp_path / result["run_id"] / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    assert events[-1]["budgets"]["history_lookups"] == 0
    assert "jev_calls" not in events[-1]["budgets"]
    assert load_fixture().classification == "synthetic"
