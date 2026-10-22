from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
JoinMode = Literal["literal", "strip_country_prefix"]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
#: Which provider answered one call. `mock` is a scripted stand-in and is the
#: default everywhere, so an unlabelled answer never reads as a real one;
#: `decider` is a real Decider 2B checkpoint reached through the Choice seam.
#: The value is evidence about the call and never authority: acceptance,
#: selection, repair, and the terminal outcome are all recomputed from the
#: offered options, the thresholds, and validated structured evidence.
#:
#: It lives here rather than beside the selector because the run record, the
#: durable events, and the terminal result all have to carry it, and those
#: contracts sit below the cascade.
Provider = Literal["mock", "decider"]
PROVIDERS: tuple[Provider, ...] = ("mock", "decider")


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    schema_version: Literal["1"] = "1"


def canonical_hash(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class TaskSpec(Contract):
    dataset_id: Identifier
    order_date_utc: date
    country_codes: Annotated[tuple[CountryCode, ...], Field(max_length=250)] = ()
    metric: Literal["units"] = "units"
    user_goal: Annotated[str, Field(min_length=1, max_length=512)] = (
        "Summarize order units by country"
    )

    @field_validator("country_codes")
    @classmethod
    def canonical_countries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @property
    def task_hash(self) -> str:
        return canonical_hash(self)


class QuerySpec(Contract):
    task: TaskSpec
    dataset_version: Identifier
    join_key_mode: JoinMode = "literal"

    @property
    def task_hash(self) -> str:
        return self.task.task_hash

    @property
    def query_hash(self) -> str:
        return canonical_hash(self)

    @property
    def base_query_hash(self) -> str:
        return canonical_hash(
            QuerySpec(task=self.task, dataset_version=self.dataset_version)
        )


class Counters(Contract):
    diagnostic_reads: Annotated[int, Field(ge=0, le=2)] = 0
    decider_calls: Annotated[int, Field(ge=0, le=2)] = 0
    history_lookups: Annotated[int, Field(ge=0, le=2)] = 0
    repair_attempts: Annotated[int, Field(ge=0, le=1)] = 0
    report_queries: Annotated[int, Field(ge=0, le=3)] = 0
    wait_checks: Annotated[int, Field(ge=0, le=1)] = 0
    agent_turns: Annotated[int, Field(ge=0, le=8)] = 0


class Settings(Contract):
    mode: Literal["offline"] = "offline"
    baseline_id: Literal["decider", "decider_memory"] = "decider"
    model_revision: Identifier = "scripted-v1"
    first_confidence_min: Probability = 0.80
    final_confidence_min: Probability = 0.80
    calibration: Literal["bootstrap_unmeasured"] = "bootstrap_unmeasured"
    run_seconds: Annotated[float, Field(gt=0, le=120, allow_inf_nan=False)] = 120.0
    #: The ceiling is the run ceiling halved, because D1 and D2 are the only
    #: two Decider calls a run may make, so two calls at the widest
    #: configurable budget still fit inside one shared run deadline. The
    #: default stays the fast offline value: raising a ceiling is not raising
    #: a default, and every offline path keeps the budget it had.
    provider_seconds: Annotated[float, Field(gt=0, le=60, allow_inf_nan=False)] = 20.0
    wait_seconds: Annotated[float, Field(ge=0, le=3, allow_inf_nan=False)] = 3.0


class Row(Contract):
    country_code: CountryCode
    units: Annotated[int, Field(ge=0)]


class SourceRow(Row):
    order_id: Identifier
    order_date_utc: date


class QueryFacts(Contract):
    kind: Literal["query"] = "query"
    dataset_version: Identifier
    query_hash: Digest
    rows: Annotated[tuple[Row, ...], Field(max_length=250)] = ()
    source_rows: Annotated[tuple[SourceRow, ...], Field(max_length=1000)] = ()
    encoding_proven: bool = False
    source_complete: bool = False


class PipelineFacts(Contract):
    kind: Literal["pipeline"] = "pipeline"
    dataset_version: Identifier
    job_state: Literal["pending", "complete", "failed"]
    scope_complete: bool
    watermark_utc: datetime


class SchemaFacts(Contract):
    kind: Literal["schema_keys"] = "schema_keys"
    dataset_version: Identifier
    columns: tuple[
        Literal["orders.country_id:VARCHAR", "countries.country_id:VARCHAR"], ...
    ] = ("orders.country_id:VARCHAR", "countries.country_id:VARCHAR")
    orders_key_encoding: Literal["plain", "country_prefix", "unknown"]
    countries_key_encoding: Literal["plain", "country_prefix", "unknown"]
    approved_join_modes: Annotated[tuple[JoinMode, ...], Field(max_length=2)] = (
        "literal",
    )
    normalized_dimension_keys_unique: bool
    metadata_complete: bool
    scope_covered: bool
    partition_complete: bool
    orders_examples: Annotated[
        tuple[Annotated[str, Field(max_length=128)], ...], Field(max_length=5)
    ] = ()
    countries_examples: Annotated[
        tuple[Annotated[str, Field(max_length=128)], ...], Field(max_length=5)
    ] = ()


class ScopeFacts(Contract):
    kind: Literal["scope"] = "scope"
    dataset_version: Identifier
    query_matches_task_scope: bool
    metadata_complete: bool
    scope_covered: bool


class EvidenceRecord(Contract):
    evidence_id: UUID = Field(default_factory=uuid4)
    kind: Literal["query", "pipeline", "schema_keys", "scope", "wait"]
    tool_id: Identifier
    arguments_hash: Digest
    dataset_id: Identifier
    dataset_version: Identifier
    observed_at: datetime
    payload: Annotated[
        QueryFacts | PipelineFacts | SchemaFacts | ScopeFacts,
        Field(discriminator="kind"),
    ]

    @field_validator("observed_at")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("UTC timestamp required")
        return value

    @model_validator(mode="after")
    def matching_payload(self) -> Self:
        kind = "pipeline" if self.kind == "wait" else self.kind
        if (
            self.payload.kind != kind
            or self.dataset_version != self.payload.dataset_version
        ):
            raise ValueError("evidence_conflict")
        return self


class Reason(StrEnum):
    POPULATED_INITIAL = "populated_initial"
    POPULATED_AFTER_WAIT = "populated_after_wait"
    POPULATED_AFTER_REPAIR = "populated_after_repair"
    EMPTY_INITIAL = "empty_initial"
    EMPTY_AFTER_WAIT = "empty_after_wait"
    EMPTY_AFTER_REPAIR = "empty_after_repair"
    STILL_PENDING = "still_pending_after_wait"
    FIRST_REJECTED = "first_selection_rejected"
    FINAL_REJECTED = "final_selection_rejected"
    HISTORY_UNAVAILABLE = "history_unavailable"
    RECEIPT_INVALID = "receipt_invalid"
    REPEAT_BLOCKED = "repeat_blocked_no_alternative"
    ENRICHMENT_UNAVAILABLE = "enrichment_unavailable"
    ENRICHMENT_INVALID = "enrichment_invalid"
    ENRICHMENT_STALE = "enrichment_stale"
    NO_READ = "no_supported_read"
    INSUFFICIENT = "insufficient_evidence"
    CONFLICT = "evidence_conflict"
    DIAGNOSTICS_EXHAUSTED = "diagnostic_reads_exhausted"
    REPAIR_FAILED = "repair_failed"
    UNSUPPORTED_REPAIR = "unsupported_repair"
    DECIDER_ERROR = "provider_error_decider"
    BEDROCK_ERROR = "provider_error_bedrock"
    TOOL_ERROR = "provider_error_tool"
    DEADLINE = "run_deadline"
    REPORTS_EXHAUSTED = "report_queries_exhausted"
    TURNS_EXHAUSTED = "agent_turns_exhausted"
    TASK_MISMATCH = "task_mismatch"
    STORAGE_ERROR = "storage_error"
    LEASE_EXPIRED = "lease_expired_incomplete"
    INVARIANT = "internal_invariant"


def terminal_mapping(reason: Reason) -> tuple[str, str | None, str]:
    reason = Reason(reason)
    if reason in {
        Reason.POPULATED_INITIAL,
        Reason.POPULATED_AFTER_WAIT,
        Reason.POPULATED_AFTER_REPAIR,
    }:
        return "DONE", "CONTINUE", "completed"
    if reason in {
        Reason.EMPTY_INITIAL,
        Reason.EMPTY_AFTER_WAIT,
        Reason.EMPTY_AFTER_REPAIR,
    }:
        return "DONE", "EMPTY", "empty_verified"
    if reason == Reason.STILL_PENDING:
        return "PENDING", "WAIT", "pending"
    if reason in {Reason.DEADLINE, Reason.REPORTS_EXHAUSTED, Reason.TURNS_EXHAUSTED}:
        return "BUDGET_STOP", "BUDGET_STOP", "budget_exhausted"
    if reason in {
        Reason.TASK_MISMATCH,
        Reason.STORAGE_ERROR,
        Reason.LEASE_EXPIRED,
        Reason.INVARIANT,
    }:
        return "FAILED", None, "failed"
    if reason in {
        Reason.DECIDER_ERROR,
        Reason.BEDROCK_ERROR,
        Reason.TOOL_ERROR,
    }:
        return "UNRESOLVED", "PROVIDER_ERROR", "unresolved"
    return "UNRESOLVED", "UNRESOLVED", "unresolved"


class RuntimeFault(Exception):
    def __init__(self, reason: Reason):
        self.reason = reason
        super().__init__(reason.value)


class Incident(Contract):
    incident_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    fingerprint: Digest
    task_hash: Digest
    base_query_hash: Digest
    state: Literal[
        "OBSERVE",
        "DECISION_HISTORY",
        "DECIDER_FIRST",
        "DIRECT_PATH",
        "RUN_PROBE",
        "RECEIPT_CHECK",
        "DECIDER_FINAL",
        "RUN_PROBE_FINAL",
        "WAIT",
        "REPAIR",
        "VERIFY",
        "DONE",
        "PENDING",
        "UNRESOLVED",
        "BUDGET_STOP",
        "FAILED",
    ] = "OBSERVE"
    used_probes: Annotated[tuple[Identifier, ...], Field(max_length=2)] = ()
    counters: Counters = Field(default_factory=Counters)

    @classmethod
    def open(cls, run_id: UUID, query: QuerySpec) -> Incident:
        return cls(
            run_id=run_id,
            fingerprint=canonical_hash(
                [str(run_id), query.task.dataset_id, query.task_hash]
            ),
            task_hash=query.task_hash,
            base_query_hash=query.base_query_hash,
        )


class VerificationResult(Contract):
    verification_id: UUID = Field(default_factory=uuid4)
    status: Literal["passed", "failed"]
    reason_code: Literal[
        "verified",
        "scope_mismatch",
        "source_mismatch",
        "encoding_unproven",
        "evidence_conflict",
        "shape_mismatch",
    ]
    evidence_ids: Annotated[tuple[UUID, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.status == "passed") != (self.reason_code == "verified"):
            raise ValueError("verification status mismatch")
        return self


class ReportVerification(Contract):
    report_id: UUID
    dataset_id: Identifier
    dataset_version: Identifier
    query_hash: Digest
    result: VerificationResult


class RunState(Contract):
    run_id: UUID
    task: TaskSpec
    dataset_version: Identifier
    status: Literal[
        "created",
        "running",
        "completed",
        "empty_verified",
        "pending",
        "unresolved",
        "budget_exhausted",
        "failed",
    ] = "created"
    reason_code: Reason | None = None
    state_version: Annotated[int, Field(ge=0)] = 0
    lease_owner: UUID | None = None
    lease_expires_at: Annotated[float, Field(allow_inf_nan=False)] | None = None
    lease_expires_utc: datetime | None = None
    lease_clock_epoch: UUID | None = None
    run_deadline_at: Annotated[float, Field(allow_inf_nan=False)] | None = None
    mode: Literal["offline"] = "offline"
    #: Which provider answered this run's selector calls, observed from the
    #: normalized selection the run recorded. It stays `mock` until a real
    #: answer is recorded, so a run that reached no selection, or one served
    #: by a scripted stand-in, reads exactly as it did before.
    provider: Provider = "mock"

    @model_validator(mode="after")
    def lease_coherent(self) -> Self:
        fields = (
            self.lease_owner,
            self.lease_expires_at,
            self.lease_expires_utc,
            self.lease_clock_epoch,
            self.run_deadline_at,
        )
        if any(value is None for value in fields) and any(
            value is not None for value in fields
        ):
            raise ValueError("incomplete lease authority")
        if (
            self.lease_expires_utc is not None
            and self.lease_expires_utc.utcoffset() != timedelta(0)
        ):
            raise ValueError("UTC lease timestamp required")
        if (self.status == "created") != (self.lease_owner is None):
            raise ValueError("lease status mismatch")
        return self

    @property
    def task_hash(self) -> str:
        return self.task.task_hash
