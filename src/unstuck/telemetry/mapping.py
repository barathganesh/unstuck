"""Projection of one durable event onto one telemetry record.

There is no second decision truth. The controller writes a durable
`EventRecord` under its own lease, and this module reads that record. Event
identity, sequence, timestamp and budgets come from the event itself, and every
observed value is read out of the payload the controller already redacted.

Nothing is inferred and nothing is filled in. A payload field of the wrong
shape, or a field the controller did not record, becomes an absent telemetry
field rather than a plausible one, so a telemetry record never claims a fact the
durable event does not carry.
"""

from collections.abc import Callable
from typing import Literal, cast, get_args
from uuid import UUID

from pydantic import JsonValue, ValidationError

from unstuck.cascade.selection import Provider
from unstuck.cascade.serialization import canonical_digest
from unstuck.decision import OptionProbability
from unstuck.domain import Counters
from unstuck.events import EventRecord
from unstuck.telemetry.contracts import (
    Availability,
    CandidateAvailability,
    Correlation,
    CorrelationContext,
    DecisionTelemetry,
    HistoryReference,
    InfrastructureDimensions,
    Measurements,
    OutcomeTelemetry,
    ReceiptTelemetry,
    TelemetryMeasurement,
    TelemetryRecord,
    ToolTelemetry,
    execution_key,
    measurement_of,
)

Payload = dict[str, JsonValue]

_AVAILABILITY = frozenset(get_args(Availability))
_DIGEST_WIDTH = 64
_HISTORY_STATUS = ("not_requested", "unavailable", "empty", "matched")
_OUTCOME_CLASSES = (
    "useful_evidence",
    "disposition_unchanged",
    "no_progress",
    "provider_error",
    "invalid_evidence",
    "superseded",
)
_PROVIDER_STATUS = ("evidence", "provider_error", "invalid_evidence")
_VALIDATION_STATUS = ("valid", "invalid", "error")
_BLOCKED_CLASSES = ("exact_repeat", "failed_execution")
_TERMINAL_STATUS = (
    "completed",
    "empty_verified",
    "pending",
    "unresolved",
    "budget_exhausted",
    "failed",
)
_VERIFICATION_STATUS = ("passed", "failed", "not_run")


def _text(payload: Payload, key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _literal(payload: Payload, key: str, allowed: tuple[str, ...]) -> str | None:
    value = _text(payload, key)
    return value if value in allowed else None


def _digest(payload: Payload, key: str) -> str | None:
    value = _text(payload, key)
    if value is None or len(value) != _DIGEST_WIDTH:
        return None
    return value if all(letter in "0123456789abcdef" for letter in value) else None


def _uuid(payload: Payload, key: str) -> UUID | None:
    value = _text(payload, key)
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _flag(payload: Payload, key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _count(payload: Payload, key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _pass_index(payload: Payload, key: str = "pass_index") -> Literal[1, 2] | None:
    value = _count(payload, key)
    return cast(Literal[1, 2], value) if value in {1, 2} else None


def _probability(payload: Payload, key: str) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0.0 <= float(value) <= 1.0 else None


def _strings(
    payload: Payload, key: str, allowed: tuple[str, ...] = ()
) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        return ()
    found = tuple(item for item in value if isinstance(item, str) and item)
    return found if not allowed else tuple(item for item in found if item in allowed)


def _digests(payload: Payload, key: str) -> tuple[str, ...]:
    return tuple(
        item
        for item in _strings(payload, key)
        if len(item) == _DIGEST_WIDTH
        and all(letter in "0123456789abcdef" for letter in item)
    )


def _uuids(payload: Payload, key: str) -> tuple[UUID, ...]:
    found = []
    for item in _strings(payload, key):
        try:
            found.append(UUID(item))
        except ValueError:
            continue
    return tuple(found)


def _measurement(payload: Payload, key: str) -> TelemetryMeasurement:
    """Read one measurement out of a payload without inventing a value.

    A payload that records no measurement, or records one this schema does not
    recognize, yields `not_measured`. A payload that claims a measurement but
    carries no usable number yields `invalid`, which is a recorded fault rather
    than a zero.
    """
    value = payload.get(key)
    if not isinstance(value, dict):
        return TelemetryMeasurement()
    availability = value.get("availability")
    if not isinstance(availability, str) or availability not in _AVAILABILITY:
        return TelemetryMeasurement()
    if availability != "measured":
        return TelemetryMeasurement(availability=cast(Availability, availability))
    amount = value.get("value")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return TelemetryMeasurement(availability="invalid")
    if float(amount) < 0:
        return TelemetryMeasurement(availability="invalid")
    return TelemetryMeasurement(availability="measured", value=float(amount))


def _probabilities(payload: Payload) -> tuple[OptionProbability, ...] | None:
    value = payload.get("probabilities")
    if not isinstance(value, list):
        return None
    options = []
    for item in value:
        if not isinstance(item, dict):
            return None
        identifier = item.get("id")
        probability = item.get("probability")
        if not isinstance(identifier, str) or isinstance(probability, bool):
            return None
        if not isinstance(probability, (int, float)):
            return None
        try:
            options.append(
                OptionProbability(id=identifier, probability=float(probability))
            )
        except ValueError:
            return None
    return tuple(options)


def _candidates(identifiers: tuple[str, ...]) -> tuple[CandidateAvailability, ...]:
    return tuple(
        CandidateAvailability(public_tool_id=identifier, available=True)
        for identifier in identifiers
    )


def _references(payload: Payload) -> tuple[HistoryReference, ...]:
    """Read the durable per-receipt history list, or nothing at all.

    One malformed entry refuses the whole list rather than yielding a shorter
    one, because a partial history reads as a smaller lookup than the one that
    actually happened. The list is bounded by the contract, so an over-long
    one is refused there.
    """
    value = payload.get("history_references")
    if not isinstance(value, list):
        return ()
    found = []
    for item in value:
        if not isinstance(item, dict):
            return ()
        try:
            found.append(HistoryReference.model_validate(item))
        except ValidationError:
            return ()
    return tuple(found)


def _history(payload: Payload) -> dict[str, object]:
    return {
        "history_status": _literal(payload, "history_status", _HISTORY_STATUS),
        "history_receipt_ids": _digests(payload, "receipt_ids"),
        "history_references": _references(payload),
        "history_reference_count": _count(payload, "reference_count"),
        "history_truncated": _flag(payload, "truncated"),
        "history_retrieval_truncated": _flag(payload, "history_retrieval_truncated"),
        "problem_fingerprint": _digest(payload, "problem_fingerprint"),
    }


def _versions(payload: Payload) -> dict[str, object]:
    """The bound version and policy provenance of one selector call.

    Each value is read out of the durable event. The schema declares these
    fields, so leaving them absent when the event carries them would make the
    record unable to say which catalog, contract, schema, policy and
    calibration the decision was bound to.
    """
    return {
        "model_revision": _text(payload, "model_revision"),
        "catalog_version": _text(payload, "catalog_version"),
        "tool_contract_version": _text(payload, "tool_contract_version"),
        "data_schema_version": _text(payload, "data_schema_version"),
        "policy_hash": _digest(payload, "policy_hash"),
        "calibration_hash": _digest(payload, "calibration_hash"),
    }


def _top_receipt(references: tuple[HistoryReference, ...]) -> ReceiptTelemetry:
    """The top-ranked match on its own, with everything that classified it.

    The matcher ranks the exact no-progress match first when there is one, so
    this is the receipt that could refuse a repetition. Each field comes from
    that one reference rather than from the head of a separate array, so the
    outcome, the match class and the reason all describe the same receipt.
    """
    first = next(iter(references), None)
    if first is None:
        return ReceiptTelemetry()
    return ReceiptTelemetry(
        receipt_id=first.receipt_id,
        match_class=first.match_class,
        outcome_class=first.outcome_class,
        reason_code=first.reason_code,
        blocking_eligible=first.blocking_eligible,
        versions_hash=first.versions_hash,
    )


def _history_requested(payload: Payload, event: EventRecord) -> dict[str, object]:
    return {"decision": DecisionTelemetry.model_validate(_history(payload))}


def _history_returned(payload: Payload, event: EventRecord) -> dict[str, object]:
    """Record the whole bounded lookup, plus its top-ranked match on its own.

    Every matched receipt is recorded under `decision` as one structured
    reference carrying its own identity, match class, outcome class, public
    tool, reason, blocking eligibility and binding, so a multi-receipt lookup
    can be read back receipt by receipt.
    """
    references = _references(payload)
    return {
        "decision": DecisionTelemetry.model_validate(_history(payload)),
        "receipt": _top_receipt(references),
    }


def _selector_requested(payload: Payload, event: EventRecord) -> dict[str, object]:
    offered = _strings(payload, "available_tool_ids")
    pass_index = _pass_index(payload)
    return {
        "decision": DecisionTelemetry.model_validate(
            _history(payload)
            | _versions(payload)
            | {
                "pass_index": pass_index,
                "selector_input_hash": _digest(payload, "state_hash"),
                "decision_context_hash": _digest(payload, "decision_context_hash"),
                "candidate_public_ids": offered,
                "candidate_availability": _candidates(offered),
                "remaining_selector_calls": _count(payload, "remaining_selector_calls"),
                "remaining_diagnostic_reads": _count(
                    payload, "remaining_diagnostic_reads"
                ),
            }
        ),
        "outcome": OutcomeTelemetry(d2_triggered=pass_index == 2),
    }


def _selector_returned(payload: Payload, event: EventRecord) -> dict[str, object]:
    return {
        "decision": DecisionTelemetry.model_validate(
            {
                "pass_index": _pass_index(payload),
                "decision_id": _uuid(payload, "decision_id"),
                "model_call_id": _uuid(payload, "model_call_id"),
                "model_revision": _text(payload, "model_version"),
                "selected_public_tool_id": _text(payload, "selected_id"),
                "confidence": _probability(payload, "confidence"),
                "probabilities": _probabilities(payload),
                "accepted": _flag(payload, "accepted"),
                "validation_status": _literal(
                    payload, "validation_status", _VALIDATION_STATUS
                ),
                "selector_input_hash": _digest(payload, "state_hash"),
                "decision_context_hash": _digest(payload, "decision_context_hash"),
                "candidate_public_ids": _strings(payload, "candidate_ids"),
            }
        ),
        # The payload names the usage field `token_usage` exactly, because the
        # redactor treats that one label as measured metadata and would redact
        # any other label carrying the word.
        "measurements": {
            "selector_latency_ms": _measurement(payload, "measured_latency_ms"),
            "cost": _measurement(payload, "measured_cost"),
            "total_tokens": _measurement(payload, "token_usage"),
        },
    }


def _direct_path(payload: Payload, event: EventRecord) -> dict[str, object]:
    selected = _text(payload, "tool_id")
    return {
        "tool": ToolTelemetry.model_validate(
            {
                "public_tool_id": selected,
                "pass_index": _pass_index(payload),
                "arguments_hash": event.input_hash,
                "execution_key": (
                    execution_key(selected, event.input_hash)
                    if selected is not None
                    else None
                ),
            }
        ),
        "outcome": OutcomeTelemetry(d2_triggered=False),
    }


def _read_index(payload: Payload, event: EventRecord) -> Literal[1, 2] | None:
    """Which diagnostic read this is, from the durable read counter alone.

    The decision pass is not the attempt number. A first decision whose choice
    is refused as an exact repetition consumes no read, so the second
    decision's first execution is read one on decision pass two. Taking the
    index from the pass would report it as attempt two and overcount every
    attempted diagnostic in a cross-call refused run.

    The controller records the index explicitly; `event.budgets` is the same
    counter after the attempt was charged, and is read only as a fallback for
    an event written before the field existed.
    """
    explicit = _count(payload, "read_index") or _count(payload, "attempt_index")
    charged = explicit if explicit is not None else event.budgets.diagnostic_reads
    return cast(Literal[1, 2], charged) if charged in {1, 2} else None


def _probe_started(payload: Payload, event: EventRecord) -> dict[str, object]:
    selected = _text(payload, "tool_id")
    reads = _read_index(payload, event)
    return {
        "tool": ToolTelemetry.model_validate(
            {
                "diagnostic_call_id": _uuid(payload, "diagnostic_call_id"),
                "public_tool_id": selected,
                "pass_index": _pass_index(payload),
                "attempt_index": reads,
                "read_index": reads,
                "arguments_hash": event.input_hash,
                "execution_key": (
                    execution_key(selected, event.input_hash)
                    if selected is not None
                    else None
                ),
            }
        )
    }


def _probe_returned(payload: Payload, event: EventRecord) -> dict[str, object]:
    selected = _text(payload, "tool_id")
    observed = _uuid(payload, "evidence_id")
    return {
        "tool": ToolTelemetry.model_validate(
            {
                "public_tool_id": selected,
                "pass_index": _pass_index(payload),
                "arguments_hash": event.input_hash,
                "execution_key": (
                    execution_key(selected, event.input_hash)
                    if selected is not None
                    else None
                ),
                "evidence_ids": () if observed is None else (observed,),
                "provider_status": "evidence",
            }
        )
    }


def _repeat_blocked(payload: Payload, event: EventRecord) -> dict[str, object]:
    selected = _text(payload, "public_tool_id")
    arguments = _digest(payload, "arguments_hash")
    return {
        "tool": ToolTelemetry.model_validate(
            {
                "public_tool_id": selected,
                "arguments_hash": arguments,
                "execution_key": (
                    execution_key(selected, arguments)
                    if selected is not None and arguments is not None
                    else None
                ),
            }
        ),
        "receipt": ReceiptTelemetry.model_validate(
            {
                "receipt_id": _digest(payload, "receipt_id"),
                "outcome_class": _literal(payload, "outcome_class", _OUTCOME_CLASSES),
                "reason_code": _text(payload, "reason_code"),
                "match_class": "exact",
                "blocking_eligible": True,
            }
        ),
        "outcome": OutcomeTelemetry.model_validate(
            {
                "blocked_public_tool_id": selected,
                "blocked_reason_code": _text(payload, "reason_code"),
                "blocked_class": _literal(payload, "blocked_class", _BLOCKED_CLASSES),
            }
        ),
    }


def _receipt_written(payload: Payload, event: EventRecord) -> dict[str, object]:
    selected = _text(payload, "public_tool_id")
    arguments = _digest(payload, "arguments_hash")
    before = _digest(payload, "disposition_before_hash")
    after = _digest(payload, "disposition_after_hash")
    return {
        "tool": ToolTelemetry.model_validate(
            {
                "diagnostic_call_id": _uuid(payload, "diagnostic_call_id"),
                "public_tool_id": selected,
                "arguments_hash": arguments,
                "execution_key": (
                    execution_key(selected, arguments)
                    if selected is not None and arguments is not None
                    else None
                ),
                "evidence_ids": _uuids(payload, "evidence_ids"),
                "evidence_before_hash": _digest(payload, "evidence_before_hash"),
                "evidence_after_hash": _digest(payload, "evidence_after_hash"),
                "admitted_evidence": _flag(payload, "admitted_evidence"),
                "disposition_before_hash": before,
                "disposition_after_hash": after,
                "disposition_moved": (
                    None if before is None or after is None else before != after
                ),
            }
        ),
        "receipt": ReceiptTelemetry.model_validate(
            {
                "receipt_id": _digest(payload, "receipt_id"),
                "outcome_class": _literal(payload, "outcome_class", _OUTCOME_CLASSES),
                "reason_code": _text(payload, "reason_code"),
                "blocking_eligible": _flag(payload, "blocking"),
                "namespace_hash": _digest(payload, "namespace_hash"),
                "versions_hash": _digest(payload, "versions_hash"),
                "created": True,
                "stored": _flag(payload, "stored"),
            }
        ),
        "decision": DecisionTelemetry.model_validate(
            {
                "decision_id": _uuid(payload, "decision_id"),
                "model_call_id": _uuid(payload, "model_call_id"),
                "decision_context_hash": _digest(payload, "decision_context_hash"),
                "problem_fingerprint": _digest(payload, "problem_fingerprint"),
            }
        ),
    }


def _wait_started(payload: Payload, event: EventRecord) -> dict[str, object]:
    return {"outcome": OutcomeTelemetry(wait_started=True)}


def _wait_finished(payload: Payload, event: EventRecord) -> dict[str, object]:
    observed = _uuid(payload, "evidence_id")
    return {
        "outcome": OutcomeTelemetry(wait_finished=True),
        "tool": ToolTelemetry(evidence_ids=() if observed is None else (observed,)),
    }


def _repair_requested(payload: Payload, event: EventRecord) -> dict[str, object]:
    return {"outcome": OutcomeTelemetry(repair_requested=True)}


def _repair_applied(payload: Payload, event: EventRecord) -> dict[str, object]:
    return {"outcome": OutcomeTelemetry(repair_applied=True)}


def _verification(payload: Payload, event: EventRecord) -> dict[str, object]:
    return {
        "outcome": OutcomeTelemetry.model_validate(
            {
                "verification_status": _literal(
                    payload, "status", _VERIFICATION_STATUS
                ),
                "verification_id": _uuid(payload, "verification_id"),
            }
        ),
        "tool": ToolTelemetry(evidence_ids=_uuids(payload, "evidence_ids")),
    }


def _unresolved(payload: Payload, event: EventRecord) -> dict[str, object]:
    reason = _text(payload, "reason_code")
    return {
        "outcome": OutcomeTelemetry.model_validate(
            {"terminal_reason": reason, "d2_refusal_reason": reason}
        )
    }


def _budget_stop(payload: Payload, event: EventRecord) -> dict[str, object]:
    return {
        "outcome": OutcomeTelemetry.model_validate(
            {"terminal_reason": _text(payload, "reason_code")}
        )
    }


def _run_finished(payload: Payload, event: EventRecord) -> dict[str, object]:
    rows = payload.get("rows")
    counted = len(rows) if isinstance(rows, list) else None
    return {
        "outcome": OutcomeTelemetry.model_validate(
            {
                "terminal_status": _literal(payload, "status", _TERMINAL_STATUS),
                "terminal_reason": _text(payload, "reason_code"),
                "disposition": _text(payload, "disposition"),
                "row_count": counted,
                "row_digest": canonical_digest(rows) if counted is not None else None,
                "verification_status": _literal(
                    payload, "verification_status", _VERIFICATION_STATUS
                ),
                "verification_id": _uuid(payload, "verification_id"),
                "blocked_public_tool_id": _text(payload, "blocked_tool_id"),
                "blocked_reason_code": _text(payload, "blocked_reason_code"),
                "d2_triggered": _count(payload, "pass_count") == 2,
                "counters": Counters.model_validate(event.budgets),
            }
        ),
        "decision": DecisionTelemetry.model_validate(
            {
                "pass_index": _pass_index(payload, "selection_pass"),
                "model_revision": _text(payload, "model_version"),
                "selected_public_tool_id": _text(payload, "final_choice"),
                "confidence": _probability(payload, "final_confidence"),
                "history_status": _literal(payload, "history_status", _HISTORY_STATUS),
                "history_receipt_ids": _digests(payload, "receipt_ids"),
                "history_truncated": _flag(payload, "history_truncated"),
            }
        ),
        "measurements": {
            "end_to_end_ms": _measurement(payload, "duration_ms"),
            "cost": _measurement(payload, "cost"),
        },
    }


#: One builder per durable event type. An event with no builder still produces a
#: correlated record, which is what keeps the timeline complete.
_BUILDERS: dict[str, Callable[[Payload, EventRecord], dict[str, object]]] = {
    "decision_history_requested": _history_requested,
    "decision_history_returned": _history_returned,
    "decider_first_requested": _selector_requested,
    "decider_final_requested": _selector_requested,
    "decider_first_returned": _selector_returned,
    "decider_final_returned": _selector_returned,
    "direct_path_selected": _direct_path,
    "probe_started": _probe_started,
    "probe_returned": _probe_returned,
    "repeat_blocked": _repeat_blocked,
    "decision_receipt_written": _receipt_written,
    "wait_started": _wait_started,
    "wait_finished": _wait_finished,
    "repair_requested": _repair_requested,
    "repair_applied": _repair_applied,
    "verification_finished": _verification,
    "selection_unresolved": _unresolved,
    "budget_stop": _budget_stop,
    "run_finished": _run_finished,
}


def correlation_for(
    event: EventRecord, context: CorrelationContext | None = None
) -> Correlation:
    """Bind the supplied correlation identity to one durable event."""
    supplied = (context or CorrelationContext()).model_dump()
    supplied.pop("schema_version", None)
    return Correlation.model_validate(
        supplied
        | {
            "run_id": event.run_id,
            "incident_id": event.incident_id,
            "event_id": event.event_id,
            "sequence": event.sequence,
            "timestamp": event.timestamp,
        }
    )


def map_event(
    event: EventRecord,
    *,
    context: CorrelationContext | None = None,
    infrastructure: InfrastructureDimensions | None = None,
    stage: str | None = None,
    retry_state: Literal["new", "duplicate", "resumed"] | None = None,
    provider: Provider = "mock",
) -> TelemetryRecord:
    """Project one durable event onto one private telemetry record.

    `provider` names which provider answered this run's selector calls. It is
    a label the caller supplies rather than a value read from the event,
    because the durable event's own provider field is the core runtime's
    offline marker; it defaults to `mock`, so an unlabelled record never reads
    as a real one.
    """
    validated = EventRecord.model_validate(event)
    builder = _BUILDERS.get(validated.type)
    parts = builder(validated.payload, validated) if builder is not None else {}
    measurements = Measurements.model_validate(
        {"stage_duration_ms": measurement_of(validated.duration_ms)}
        | cast(dict[str, object], parts.pop("measurements", {}))
    )
    observed = parts.pop("outcome", None)
    outcome = cast(OutcomeTelemetry | None, observed)
    if retry_state is not None:
        outcome = OutcomeTelemetry.model_validate(
            (outcome or OutcomeTelemetry()).model_dump() | {"retry_state": retry_state}
        )
    return TelemetryRecord.model_validate(
        {
            "kind": validated.type,
            "stage": stage,
            "correlation": correlation_for(validated, context),
            "measurements": measurements,
            "infrastructure": infrastructure or InfrastructureDimensions(),
            "outcome": outcome,
            "provider": provider,
        }
        | parts
    )
