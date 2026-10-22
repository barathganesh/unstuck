"""Provenance spine and evidence inspector projections.

FR10.1 through FR10.4 are the rules. The spine always carries every step, so a
step that did not happen is visibly skipped rather than silently missing: that
is how the direct path is distinguishable from a re-decided one by text alone.

FR10.3 is enforced by construction. A historical receipt is rendered as a
reference and never as an answer. A confidence is rendered as a confidence and
never as correctness. A no-progress outcome is rendered as the absence of
admissible evidence and never as a cause. And an unavailable measurement
becomes a `DisplayValue` that cannot hold a number at all, so it can never
reach a template as zero.

Every panel passes the shared redactor before it leaves this module.
"""

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import JsonValue

from unstuck.events import Measurement, Redactor
from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.contracts import (
    DisplayValue,
    EvidencePanel,
    ModeLabel,
    NodeKind,
    NodeState,
    RunView,
    SpineNode,
)
from unstuck.operator.ledger import ScenarioBinding
from unstuck.storage import Snapshot

#: The spine's fixed order. Every run shows every step, present or skipped.
NODE_ORDER: tuple[tuple[str, NodeKind, str], ...] = (
    ("task", "task", "Immutable task"),
    ("decision-first", "decision_first", "First tool choice"),
    ("history-lookup", "history_lookup", "Decision Receipt history"),
    ("diagnostic", "diagnostic", "Diagnostic read"),
    ("decision-receipt", "receipt", "Decision Receipt"),
    ("blocked-repeat", "blocked_repeat", "Blocked repeat"),
    ("decision-final", "decision_final", "Second tool choice"),
    ("wait", "wait", "Bounded wait"),
    ("repair", "repair", "Deterministic repair"),
    ("verification", "verification", "Verification"),
    ("result", "result", "Structured result"),
)

RunPath = Literal["direct", "re_decided", "no_cascade"]

PATH_TEXT: dict[RunPath, str] = {
    "direct": "direct",
    "re_decided": "re decided",
    "no_cascade": "no cascade",
}

STATUS_TEXT: dict[str, str] = {
    "created": "Created",
    "running": "Running",
    "completed": "Completed with rows",
    "empty_verified": "Verified empty",
    "pending": "Pending after a bounded wait",
    "unresolved": "Unresolved",
    "budget_exhausted": "Stopped on a budget",
    "failed": "Failed",
}


def display(measurement: Measurement | None) -> DisplayValue:
    """Render a measurement. An absent one never becomes a number."""
    if measurement is None:
        return DisplayValue.for_availability("not_provided")
    if measurement.availability == "measured" and measurement.value is not None:
        return DisplayValue.measured(measurement.value)
    return DisplayValue.for_availability(measurement.availability)


def _probability(value: float | None) -> DisplayValue:
    if value is None:
        return DisplayValue.for_availability("not_provided")
    return DisplayValue.measured(value)


def _result_measure(snapshot: Snapshot, field: str) -> Measurement | None:
    result = snapshot.result
    if not result or field not in result:
        return None
    raw = result[field]
    if raw is None:
        return None
    return Measurement.model_validate(raw)


def receipt_payloads(snapshot: Snapshot) -> tuple[dict[str, JsonValue], ...]:
    """Every Decision Receipt this run wrote, in the order it wrote them.

    The receipts are read from the producer's own `decision_receipt_written`
    events rather than recomputed, so this surface cannot disagree with the
    deterministic verifier that created them.
    """
    return tuple(
        dict(event.payload)
        for event in snapshot.events
        if event.type == "decision_receipt_written"
    )


def _receipt_detail(receipts: tuple[dict[str, JsonValue], ...], present: bool) -> str:
    if not present:
        return (
            "No Decision Receipt was written: no diagnostic call reached the "
            "deterministic verifier."
        )
    last = receipts[-1]
    outcome = str(last.get("outcome_class", "unknown"))
    admitted = bool(last.get("admitted_evidence"))
    blocking = bool(last.get("blocking"))
    stored = bool(last.get("stored"))
    return (
        f"The verifier wrote {len(receipts)} receipt(s) from observed facts. "
        f"The last records {outcome}: it "
        f"{'admitted a new fact' if admitted else 'admitted nothing new'} and "
        f"{'may refuse an identical future repetition' if blocking else 'refuses nothing'}"
        f". Persisted for a later call: {'yes' if stored else 'no'}."
    )


def refused_repetitions(snapshot: Snapshot) -> tuple[dict[str, JsonValue], ...]:
    """Every identical execution the controller refused before it ran."""
    return tuple(
        dict(event.payload)
        for event in snapshot.events
        if event.type == "repeat_blocked"
    )


def _blocked_detail(
    refusals: tuple[dict[str, JsonValue], ...], selection: object, present: bool
) -> str:
    if present:
        last = refusals[-1]
        return (
            f"Repeating {last.get('public_tool_id')} was refused before it ran: "
            f"receipt {str(last.get('receipt_id', ''))[:12]} records that the "
            "identical capability and arguments produced no admissible evidence "
            "under this same context."
        )
    unproductive = getattr(selection, "blocked_tool_id", None)
    if unproductive is not None:
        return (
            f"Nothing was prohibited. {unproductive} ran and left the problem "
            "where it was, which opened the second decision on its own; only an "
            "exact no-progress receipt ever refuses a repetition."
        )
    return "Nothing was blocked: no exact receipt matched this context."


def _path(snapshot: Snapshot) -> RunPath:
    if snapshot.selection is None:
        return "no_cascade"
    if snapshot.incident.counters.decider_calls >= 2:
        return "re_decided"
    return "direct"


def spine_for(snapshot: Snapshot) -> tuple[SpineNode, ...]:
    """Build the ordered spine. Absent steps are present as skipped nodes."""
    counters = snapshot.incident.counters
    selection = snapshot.selection
    verification = snapshot.verification
    evidence_ids = tuple(record.evidence_id for record in snapshot.evidence)
    receipts = receipt_payloads(snapshot)
    refusals = refused_repetitions(snapshot)
    terminal = snapshot.state.status not in {"created", "running"}
    present: dict[str, bool] = {
        "task": True,
        "decision_first": selection is not None,
        "history_lookup": counters.history_lookups > 0,
        "diagnostic": counters.diagnostic_reads > 0,
        "receipt": bool(receipts),
        # A prohibition, not merely an unproductive read: the producer records
        # `repeat_blocked` only for an exact repetition it refused. A read that
        # ran and achieved nothing opened the second decision without any
        # prohibition, and this node stays skipped and says so.
        "blocked_repeat": bool(refusals),
        "decision_final": counters.decider_calls >= 2,
        "wait": counters.wait_checks > 0,
        "repair": counters.repair_attempts > 0,
        "verification": verification is not None,
        "result": terminal,
    }
    details = _details(snapshot, present)
    states = _states(snapshot, present)
    nodes: list[SpineNode] = []
    for index, (node_id, kind, label) in enumerate(NODE_ORDER, start=1):
        is_present = present[kind]
        nodes.append(
            SpineNode(
                node_id=node_id,
                order=index,
                kind=kind,
                label=label,
                state=states[kind] if is_present else "skipped",
                detail=details[kind],
                present=is_present,
                evidence_ids=evidence_ids
                if kind == "diagnostic" and is_present
                else (),
            )
        )
    return tuple(nodes)


def _details(snapshot: Snapshot, present: Mapping[str, bool]) -> dict[str, str]:
    counters = snapshot.incident.counters
    receipts = receipt_payloads(snapshot)
    selection = snapshot.selection
    # The first decision's own record is preserved by the run store, so the
    # first choice shown here is D1's and never D2's.
    first = snapshot.first_selection or selection
    history = snapshot.history
    verification = snapshot.verification
    chosen = first.selected_id if first else None
    confidence = first.confidence if first else None
    confidence_text = (
        f"reported confidence {confidence:g}"
        if confidence is not None
        else "no confidence reported"
    )
    return {
        "task": (
            "Synthetic scenario; immutable task bound to one canonical hash "
            f"and the literal initial join mode. Task hash {snapshot.state.task_hash[:12]}."
        ),
        "decision_first": (
            f"Selector chose {chosen} with {confidence_text}. A confidence "
            "gates whether a choice may execute; it states nothing about the outcome."
            if present["decision_first"]
            else "No tool choice was needed: the first report verified without a cascade."
        ),
        "history_lookup": _history_detail(history, present["history_lookup"]),
        "diagnostic": (
            f"Executed {counters.diagnostic_reads} diagnostic read(s); the "
            "controller admitted the structured evidence below."
            if present["diagnostic"]
            else "No diagnostic read was executed."
        ),
        "receipt": _receipt_detail(receipts, present["receipt"]),
        "blocked_repeat": _blocked_detail(
            refused_repetitions(snapshot), selection, present["blocked_repeat"]
        ),
        "decision_final": (
            "A second decision ran over the remaining candidates and chose "
            f"{selection.selected_id if selection else 'another read'}. "
            "It is final."
            if present["decision_final"]
            else "No second decision: nothing blocked the first choice."
        ),
        "wait": (
            "One bounded wait and recheck ran against explicit pending evidence."
            if present["wait"]
            else "No wait was taken."
        ),
        "repair": (
            "One allowlisted join-key normalization was applied, then executed "
            "and verified through the same locked boundary."
            if present["repair"]
            else "No repair was applied."
        ),
        "verification": _verification_detail(verification, present["verification"]),
        "result": (
            f"{STATUS_TEXT[snapshot.state.status]}; reason "
            f"{snapshot.state.reason_code}."
            if present["result"]
            else "The run has not reached a terminal result."
        ),
    }


def _history_detail(history: object, present: bool) -> str:
    if not present:
        return (
            "History was not requested for this arm, so no receipt reference "
            "was retrieved."
        )
    status = getattr(history, "status", "empty")
    identifiers = tuple(getattr(history, "receipt_ids", ()) or ())
    if status == "matched":
        return (
            f"Retrieved {len(identifiers)} bounded receipt reference(s) for this "
            "problem and context. A reference is evidence about a past call, "
            "never a reply to this one."
        )
    if status == "unavailable":
        return (
            "The receipt index could not be read, so this call recorded no "
            "history reference and did not proceed as another arm."
        )
    return "No receipt reference matched this problem and context."


def _verification_detail(verification: object, present: bool) -> str:
    if not present:
        return "No verification ran."
    result = getattr(verification, "result", None)
    status = getattr(result, "status", "failed")
    reason = getattr(result, "reason_code", "unknown")
    if status == "passed":
        return (
            "Deterministic verification passed on scope, shape, source linkage "
            "and totals. The evidence identifiers are listed below."
        )
    return f"Deterministic verification failed: {reason}."


def _states(snapshot: Snapshot, present: Mapping[str, bool]) -> dict[str, NodeState]:
    verification = snapshot.verification
    verified = verification is not None and verification.result.status == "passed"
    status = snapshot.state.status
    outcomes: dict[str, NodeState] = {
        "completed": "verified",
        "empty_verified": "verified",
        "pending": "pending",
        "unresolved": "unresolved",
        "budget_exhausted": "unresolved",
        "failed": "error",
    }
    result_state: NodeState = outcomes.get(status, "informational")
    return {
        "task": "informational",
        "decision_first": "informational",
        "history_lookup": "informational",
        "diagnostic": "informational",
        "receipt": "informational",
        "blocked_repeat": "blocked",
        "decision_final": "informational",
        "wait": "pending",
        "repair": "informational",
        "verification": "verified" if verified else "error",
        "result": result_state,
    }


def run_view(
    snapshot: Snapshot,
    mode: ModeLabel,
    *,
    redactor: Redactor | None = None,
    binding: ScenarioBinding | None = None,
) -> RunView:
    """Project one run into everything Run Detail shows.

    `binding` is the run's recorded scenario authority when one exists. It is
    read from the persisted ledger rather than from the request, so the view
    reports the scenario the stored run was created from.
    """
    cleaner = redactor or Redactor()
    counters = snapshot.incident.counters
    selection = snapshot.selection
    first = snapshot.first_selection or selection
    history = snapshot.history
    verification = snapshot.verification
    path = _path(snapshot)
    status = snapshot.state.status
    outcome = (
        f"{STATUS_TEXT[status]} after the {PATH_TEXT[path]} path. "
        "Synthetic, offline evidence."
    )
    references = tuple(getattr(history, "receipt_ids", ()) or ()) if history else ()
    report = snapshot.verified_report()
    rows = tuple(report.rows) if report is not None else ()
    raw_refs = snapshot.result.get("artifact_refs") if snapshot.result else None
    artifacts: list[str] = (
        [str(item) for item in raw_refs] if isinstance(raw_refs, list) else []
    )
    return RunView(
        run_id=snapshot.state.run_id,
        scenario=binding.scenario if binding is not None else None,
        fixture_identity=binding.fixture_identity if binding is not None else None,
        status=status,
        reason_code=snapshot.state.reason_code,
        outcome_text=str(cleaner.clean(outcome)),
        mode=mode,
        baseline_id="decider_memory" if history is not None else "decider",
        path=path,
        task_hash=snapshot.state.task_hash,
        base_query_hash=snapshot.query.base_query_hash,
        query_hash=snapshot.query.query_hash,
        duration=display(_result_measure(snapshot, "duration_ms")),
        cost=display(_result_measure(snapshot, "cost")),
        confidence=_probability(first.confidence if first else None),
        selector_calls=counters.decider_calls,
        diagnostic_reads=counters.diagnostic_reads,
        wait_checks=counters.wait_checks,
        repair_attempts=counters.repair_attempts,
        history_status=getattr(history, "status", "not_requested")
        if history is not None
        else "not_requested",
        receipt_references=references,
        history_truncated=bool(getattr(history, "truncated", False)),
        first_choice=first.selected_id if first else None,
        final_choice=selection.selected_id
        if selection and selection.accepted
        else None,
        blocked_tool_id=selection.blocked_tool_id if selection else None,
        blocked_reason_code=selection.blocked_reason if selection else None,
        verification_status=verification.result.status if verification else "not_run",
        rows=rows,
        artifact_refs=tuple(str(item) for item in artifacts),
        spine=spine_for(snapshot),
    )


def evidence_panel(
    snapshot: Snapshot, node_id: str, *, redactor: Redactor | None = None
) -> EvidencePanel:
    """Scope the evidence, raw event, and validation tabs to one node."""
    cleaner = redactor or Redactor()
    node = next((item for item in spine_for(snapshot) if item.node_id == node_id), None)
    if node is None:
        raise OperatorFault(OperatorReason.NODE_NOT_FOUND, node_id)
    evidence: dict[str, JsonValue] = {}
    raw: dict[str, JsonValue] | None = None
    validation: dict[str, JsonValue] = {
        "node": node.kind,
        "present": node.present,
        "state": node.state,
        "task_hash": snapshot.state.task_hash,
        "query_hash": snapshot.query.query_hash,
    }
    if node.kind == "diagnostic" and node.present:
        evidence = {
            "records": [_record(record, cleaner) for record in snapshot.evidence]
        }
    if node.kind == "receipt" and node.present:
        receipts = receipt_payloads(snapshot)
        evidence = {"receipts": [cleaner.object(dict(item)) for item in receipts]}
        last = receipts[-1]
        validation |= {
            "outcome_class": last.get("outcome_class"),
            "reason_code": last.get("reason_code"),
            "admitted_evidence": last.get("admitted_evidence"),
            "blocking": last.get("blocking"),
            "evidence_before_hash": last.get("evidence_before_hash"),
            "evidence_after_hash": last.get("evidence_after_hash"),
            "disposition_before_hash": last.get("disposition_before_hash"),
            "disposition_after_hash": last.get("disposition_after_hash"),
            "decision_context_hash": last.get("decision_context_hash"),
        }
    if node.kind == "verification" and snapshot.verification is not None:
        binding = snapshot.verification
        validation |= {
            "status": binding.result.status,
            "reason_code": binding.result.reason_code,
            "report_id": str(binding.report_id),
            "dataset_version": binding.dataset_version,
        }
    if node.kind == "result" and snapshot.result is not None:
        evidence = {"result": cleaner.object(dict(snapshot.result))}
    event = next(
        (item for item in reversed(snapshot.events) if _matches(item.type, node.kind)),
        None,
    )
    if event is not None:
        raw = {
            "type": event.type,
            "sequence": event.sequence,
            "input_hash": event.input_hash,
            "output_hash": event.output_hash,
            "payload": cleaner.object(dict(event.payload)),
        }
    return EvidencePanel(
        node_id=node_id, evidence=evidence, validation=validation, raw_event=raw
    )


def _record(record: Any, cleaner: Redactor) -> JsonValue:
    body = record.payload.model_dump(mode="json")
    return {
        "evidence_id": str(record.evidence_id),
        "kind": record.kind,
        "tool_id": record.tool_id,
        "dataset_version": record.dataset_version,
        "payload": cleaner.clean(body),
    }


#: The producer's own event vocabulary, by node. These are the exact names the
#: controller records (`unstuck.controller._STATES` and its `_event` calls), so
#: a panel resolves the event that actually happened rather than a name this
#: module invented. The returned event is the last match, so a node that ran
#: twice shows its most recent one.
_EVENT_KINDS: dict[str, tuple[str, ...]] = {
    "task": ("incident_opened", "run_started"),
    "decision_first": ("decider_first_returned", "decider_first_requested"),
    "history_lookup": ("decision_history_returned", "decision_history_requested"),
    "diagnostic": ("probe_returned", "probe_started", "query_observed"),
    "receipt": ("decision_receipt_written",),
    "blocked_repeat": ("repeat_blocked",),
    "decision_final": ("decider_final_returned", "decider_final_requested"),
    "wait": ("wait_finished", "wait_started"),
    "repair": ("repair_applied", "repair_requested"),
    "verification": ("verification_finished",),
    "result": ("run_finished",),
}

#: Every event name this projection claims the producer emits.
PROJECTED_EVENTS: frozenset[str] = frozenset(
    name for names in _EVENT_KINDS.values() for name in names
)


def _matches(event_type: str, kind: str) -> bool:
    return event_type in _EVENT_KINDS.get(kind, ())


__all__ = [
    "NODE_ORDER",
    "PATH_TEXT",
    "PROJECTED_EVENTS",
    "STATUS_TEXT",
    "display",
    "evidence_panel",
    "receipt_payloads",
    "refused_repetitions",
    "run_view",
    "spine_for",
]
