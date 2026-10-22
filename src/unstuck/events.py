import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from types import UnionType
from typing import Annotated, Literal, Self, Union, cast, get_args, get_origin
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, JsonValue, model_validator

from unstuck.domain import (
    Contract,
    Counters,
    Digest,
    Identifier,
    Probability,
    Provider,
    Reason,
    Row,
    RuntimeFault,
    terminal_mapping,
)


class Measurement(Contract):
    availability: Literal["measured", "not_measured", "not_provided", "not_called"] = (
        "not_measured"
    )
    value: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None

    @model_validator(mode="after")
    def explicit_absence(self) -> Self:
        if (self.availability == "measured") != (self.value is not None):
            raise ValueError("measurement availability does not match value")
        return self


class TerminalResult(Contract):
    run_id: UUID
    status: Literal[
        "completed",
        "empty_verified",
        "pending",
        "unresolved",
        "budget_exhausted",
        "failed",
    ]
    reason_code: Reason
    disposition: (
        Literal[
            "CONTINUE", "EMPTY", "WAIT", "UNRESOLVED", "BUDGET_STOP", "PROVIDER_ERROR"
        ]
        | None
    )
    mode: Literal["offline"] = "offline"
    #: Which provider answered this run's selector calls, observed from the
    #: normalized selection the run recorded rather than from any deployment
    #: label or mode. A run that reached no selection, or one served by a
    #: scripted stand-in, keeps the `mock` default it has always carried.
    provider: Provider = "mock"
    rows: list[Row]
    evidence_ids: list[UUID]
    dataset_version: Identifier
    task_hash: Digest
    base_query_hash: Digest
    query_hash: Digest
    diagnostic_reads: Annotated[int, Field(ge=0, le=2)]
    wait_checks: Annotated[int, Field(ge=0, le=1)]
    repair_attempts: Annotated[int, Field(ge=0, le=1)]
    history_status: Literal["not_requested", "unavailable", "empty", "matched"]
    receipt_ids: Annotated[list[Digest], Field(max_length=4)]
    history_truncated: bool
    pass_count: Annotated[int, Field(ge=0, le=2)]
    selection_pass: Literal[1, 2] | None
    first_choice: Identifier | None
    final_choice: Identifier | None
    first_confidence: Probability | None
    final_confidence: Probability | None
    confidence_available: bool
    model_version: Identifier | None
    blocked_tool_id: Identifier | None
    blocked_reason_code: Reason | None
    verification_status: Literal["passed", "failed", "not_run"]
    report_evidence_id: UUID | None
    verification_id: UUID | None
    cost: Measurement | None
    duration_ms: Measurement
    artifact_refs: list[str]
    baseline_id: Literal["decider", "decider_memory"] = "decider"
    classification: Literal["synthetic"] = "synthetic"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        _, disposition, status = terminal_mapping(self.reason_code)
        if (self.status, self.disposition) != (status, disposition):
            raise ValueError("terminal reason mismatch")
        if self.confidence_available != (self.first_confidence is not None):
            raise ValueError("confidence availability mismatch")
        if (self.history_status == "matched") != bool(self.receipt_ids):
            raise ValueError("history status does not match receipt availability")
        if self.history_truncated and self.history_status != "matched":
            raise ValueError("truncation requires matched history")
        if len(set(self.receipt_ids)) != len(self.receipt_ids):
            raise ValueError("duplicate receipt identities")
        if (self.blocked_tool_id is None) != (self.blocked_reason_code is None):
            raise ValueError("blocked repetition requires tool and reason")
        if (self.verification_status == "not_run") != (
            self.report_evidence_id is None and self.verification_id is None
        ):
            raise ValueError("verification identity missing")
        if self.verification_status != "not_run" and (
            self.report_evidence_id is None
            or self.verification_id is None
            or self.report_evidence_id not in self.evidence_ids
        ):
            raise ValueError("report identity missing")
        if self.status in {"completed", "empty_verified"} and (
            self.verification_status != "passed"
            or (self.status == "completed") != bool(self.rows)
        ):
            raise ValueError("successful result requires verification")
        return self


#: Labels that carry a sensitive keyword but name only measured metadata. They
#: are matched as whole labels, so `token_usage` stays readable while
#: `aws_session_token` does not.
BENIGN_LABELS = frozenset({"token_usage", "token_usage_available"})

#: One sensitive label in a serialized payload, with its value. The label may
#: carry a prefix or suffix (`aws_secret_access_key`) and may be quoted as a
#: JSON or Python mapping key, so the bare `keyword=value` form is only one of
#: the shapes a stringified body actually takes.
_KEYED_SECRET = re.compile(
    r"""(?i)['"]?(?P<label>[\w-]*"""
    r"""(?:password|secret|token|credential|api[_-]?key|access[_-]?key[_-]?id)"""
    r"""[\w-]*)['"]?\s*[:=]\s*['"]?[^'"\s,;}\]]+['"]?"""
)

#: A PEM private-key block of any key type, header to footer.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)


def _redact_keyed(match: re.Match[str]) -> str:
    if match.group("label").lower() in BENIGN_LABELS:
        return match.group(0)
    return "[REDACTED]"


class Redactor:
    def __init__(self, values: tuple[str, ...] = ()):
        self.values = tuple(value for value in values if value)
        self.keys = re.compile(
            r"authorization|secret|token|password|api[_-]?key|credential|environment",
            re.I,
        )
        self.patterns = (
            re.compile(r"(?i)authorization\s*[:=]\s*[^\r\n]+"),
            re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
            _PRIVATE_KEY_BLOCK,
            re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
        )
        self.keyed_pattern = _KEYED_SECRET
        self.authority_markers = re.compile(
            r"hidden[\s_/-]*(?:category|scenario|cause)|expected[\s_-]*(?:rows|action)"
            r"|evaluator|private[\s_-]*dispatch|environment[\s_-]*values|authorization",
            re.I,
        )

    def safe_authority(self, value: JsonValue) -> bool:
        if self.clean(value) != value:
            return False
        if isinstance(value, dict):
            return all(
                self.safe_authority(key) and self.safe_authority(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return all(self.safe_authority(item) for item in value)
        return not isinstance(value, str) or not self.authority_markers.search(value)

    def clean(self, value: JsonValue) -> JsonValue:
        if isinstance(value, dict):
            return {
                str(self.clean(key)): "[REDACTED]"
                if self.keys.search(key)
                and key not in {"token_usage", "token_usage_available"}
                else self.clean(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            for secret in self.values:
                value = value.replace(secret, "[REDACTED]")
            for pattern in self.patterns:
                value = pattern.sub("[REDACTED]", value)
            value = self.keyed_pattern.sub(_redact_keyed, value)
            value = self.authority_markers.sub("[REDACTED]", value)
        return value

    def object(self, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        cleaned = self.clean(value)
        assert isinstance(cleaned, dict)
        return cleaned


@dataclass(frozen=True)
class DisplayOnly:
    pass


@dataclass(frozen=True)
class PersistedAs:
    model: type[BaseModel]


class PersistenceBoundary:
    def __init__(self, redactor: Redactor):
        self.redactor = redactor

    def validate(self, value: object, reason: Reason = Reason.INVARIANT) -> None:
        if not self._safe(value):
            raise RuntimeFault(reason)

    def _safe(self, value: object, annotation: object = None) -> bool:
        if get_origin(annotation) is Annotated:
            annotation, *metadata = get_args(annotation)
            for role in metadata:
                if isinstance(role, DisplayOnly):
                    return self._display(value)
                if isinstance(role, PersistedAs) and value is not None:
                    return self._safe(role.model.model_validate_json(json.dumps(value)))
        if get_origin(annotation) is Literal:
            return value in get_args(annotation)
        return self._value_safe(value, annotation)

    def _display(self, value: object) -> bool:
        if self.redactor.clean(cast(JsonValue, value)) == value:
            return True
        try:
            projection = TerminalResult.model_validate_json(json.dumps(value))
        except (TypeError, ValueError):
            return False
        return self._safe(projection)

    def _value_safe(self, value: object, annotation: object) -> bool:
        if get_origin(annotation) in {Union, UnionType}:
            options = get_args(annotation)
            if all(
                option is type(None) or get_origin(option) is Literal
                for option in options
            ):
                return any(self._safe(value, option) for option in options)
        if isinstance(value, BaseModel):
            return all(
                self._safe(getattr(value, name), field.rebuild_annotation())
                for name, field in type(value).model_fields.items()
            )
        if isinstance(value, Enum):
            return True
        if isinstance(value, (str, UUID)):
            return self.redactor.safe_authority(str(value))
        if isinstance(value, dict):
            arguments = get_args(annotation)
            key_type, value_type = arguments if len(arguments) == 2 else (None, None)
            return all(
                self._safe(key, key_type) and self._safe(item, value_type)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            arguments = get_args(annotation)
            item_type = arguments[0] if arguments else None
            return all(self._safe(item, item_type) for item in value)
        return value is None or isinstance(value, (bool, int, float, date))


class EventRecord(Contract):
    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    incident_id: UUID | None
    sequence: Annotated[int, Field(ge=1)]
    timestamp: datetime
    component: Identifier = "controller"
    type: Identifier
    input_hash: Digest
    output_hash: Digest
    mode: Literal["offline"] = "offline"
    #: Which provider had answered by the time this event was written. Events
    #: recorded before any selection keep the `mock` default, which is the
    #: honest value: nothing had answered yet.
    provider: Provider = "mock"
    model_version: Identifier | None = None
    budgets: Counters
    evidence_ids: Annotated[tuple[UUID, ...], Field(max_length=16)] = ()
    duration_ms: Measurement = Field(default_factory=Measurement)
    payload: Annotated[dict[str, JsonValue], DisplayOnly()]

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if len(
            json.dumps(self.payload)
        ) > 32000 or self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("invalid event bounds or UTC timestamp")
        return self


def project_events(events: tuple[EventRecord, ...]) -> dict[str, JsonValue] | None:
    finals = [event for event in events if event.type == "run_finished"]
    if len(finals) > 1:
        raise RuntimeFault(Reason.INVARIANT)
    return finals[0].payload if finals else None
