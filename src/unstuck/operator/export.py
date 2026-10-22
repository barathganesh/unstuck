"""Evidence export in JSONL, JSON, and CSV with explicit labels.

AC2.1.3 is the rule: the export binds task, state, choices, evidence, budgets,
provider versions, timing, cost availability, and verification. The format is
chosen explicitly by the caller and labelled in the payload, so a reader never
has to infer it from a file extension.

Redaction runs before any byte is produced, and an unavailable measurement is
exported as its availability rather than as a number.
"""

import csv
import io
import json
from typing import Any

from pydantic import JsonValue

from unstuck.events import Redactor
from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.contracts import ExportRequest
from unstuck.operator.ledger import ScenarioBinding
from unstuck.operator.modes import resolve_mode
from unstuck.operator.projection import run_view, spine_for
from unstuck.storage import Snapshot

EXPORT_MEDIA_TYPES: dict[str, str] = {
    "jsonl": "application/x-ndjson",
    "json": "application/json",
    "csv": "text/csv",
}

#: The flat columns a CSV reader gets. Each is a declared label, not a guess.
CSV_COLUMNS: tuple[str, ...] = (
    "format",
    "classification",
    "mode",
    "provider",
    "run_id",
    "scenario",
    "fixture_identity",
    "status",
    "reason_code",
    "path",
    "task_hash",
    "base_query_hash",
    "query_hash",
    "baseline_id",
    "selector_calls",
    "diagnostic_reads",
    "wait_checks",
    "repair_attempts",
    "history_status",
    "receipt_references",
    "history_truncated",
    "first_choice",
    "final_choice",
    "blocked_tool_id",
    "blocked_reason_code",
    "confidence_availability",
    "confidence",
    "duration_availability",
    "duration_ms",
    "cost_availability",
    "cost",
    "verification_status",
    "experiment_id",
)


def manifest(
    snapshot: Snapshot,
    request: ExportRequest,
    *,
    binding: ScenarioBinding | None = None,
) -> dict[str, JsonValue]:
    """The flat, explicitly labelled record every format carries.

    The scenario and its canonical fixture identity are part of the record: an
    exported run states which synthetic inputs produced it, and an absent
    binding is exported as absent rather than as a guess.
    """
    view = run_view(snapshot, resolve_mode("offline"), binding=binding)
    return {
        "format": request.format,
        "classification": "synthetic",
        "mode": view.mode.effective,
        "provider": view.mode.provider,
        "run_id": str(view.run_id),
        "scenario": view.scenario,
        "fixture_identity": view.fixture_identity,
        "status": view.status,
        "reason_code": view.reason_code,
        "path": view.path,
        "task_hash": view.task_hash,
        "base_query_hash": view.base_query_hash,
        "query_hash": view.query_hash,
        "baseline_id": view.baseline_id,
        "selector_calls": view.selector_calls,
        "diagnostic_reads": view.diagnostic_reads,
        "wait_checks": view.wait_checks,
        "repair_attempts": view.repair_attempts,
        "history_status": view.history_status,
        "receipt_references": list(view.receipt_references),
        "history_truncated": view.history_truncated,
        "first_choice": view.first_choice,
        "final_choice": view.final_choice,
        "blocked_tool_id": view.blocked_tool_id,
        "blocked_reason_code": view.blocked_reason_code,
        "confidence_availability": view.confidence.availability,
        "confidence": view.confidence.value,
        "duration_availability": view.duration.availability,
        "duration_ms": view.duration.value,
        "cost_availability": view.cost.availability,
        "cost": view.cost.value,
        "verification_status": view.verification_status,
        "artifact_refs": list(view.artifact_refs),
        "experiment_id": request.experiment_id,
        "live_checks": "Pending",
    }


def export_bytes(
    snapshot: Snapshot,
    request: ExportRequest,
    *,
    redactor: Redactor | None = None,
    binding: ScenarioBinding | None = None,
) -> bytes:
    """Produce the export. Redaction runs before any byte exists."""
    cleaner = redactor or Redactor()
    if request.format not in EXPORT_MEDIA_TYPES:
        raise OperatorFault(OperatorReason.EXPORT_FORMAT_REJECTED, request.format)
    record = cleaner.object(manifest(snapshot, request, binding=binding))
    if request.format == "json":
        body: dict[str, Any] = {
            "manifest": record,
            "spine": [
                cleaner.object(node.model_dump(mode="json", exclude={"schema_version"}))
                for node in spine_for(snapshot)
            ],
            "events": [
                cleaner.object(
                    event.model_dump(mode="json", exclude={"schema_version"})
                )
                for event in snapshot.events
            ],
        }
        return json.dumps(body, sort_keys=True, indent=2).encode()
    if request.format == "jsonl":
        lines = [json.dumps({"record": "manifest"} | record, sort_keys=True)]
        for node in spine_for(snapshot):
            lines.append(
                json.dumps(
                    {"record": "spine"}
                    | cleaner.object(
                        node.model_dump(mode="json", exclude={"schema_version"})
                    ),
                    sort_keys=True,
                )
            )
        for event in snapshot.events:
            lines.append(
                json.dumps(
                    {"record": "event"}
                    | cleaner.object(
                        event.model_dump(mode="json", exclude={"schema_version"})
                    ),
                    sort_keys=True,
                )
            )
        return ("\n".join(lines) + "\n").encode()
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    writer.writerow({column: _cell(record.get(column)) for column in CSV_COLUMNS})
    return buffer.getvalue().encode()


def _cell(value: object) -> str:
    """Render one CSV cell. An absent value is empty, never zero."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    return str(value)


__all__ = ["CSV_COLUMNS", "EXPORT_MEDIA_TYPES", "export_bytes", "manifest"]
