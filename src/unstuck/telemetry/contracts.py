"""Versioned safe telemetry contracts for one run.

Telemetry is observation only. Nothing here selects a tool, scores a candidate,
blocks an execution, authorizes a repair, or decides an outcome. Every field is
a projection of a durable event the controller already wrote, and that durable
event stays the only authority: event identity and order, the selection record,
and the Decision Receipt are never recomputed here.

Four rules shape the schema.

Missing is never zero. Every quantity carries its own availability, so an
unmeasured latency, a provider that reported no token count, and a provider that
was never called are three different recorded facts rather than one zero.

Environment identity is never a constant. The infrastructure dimensions are
typed here and supplied by validated runtime configuration. This module defines
no account, Region, runtime, endpoint, log group, or model value, and a record
that carries one must say that configuration supplied it.

The private capability never appears. A tool is named by its public identifier
and by an opaque execution key derived from public values alone, so the private
execution surface stays inside the controller and the cascade.

Public is a validated projection, not a rendering choice. A record marked public
has already had its environment identity hashed or removed, and the refusal
happens in this contract, before persistence or export, rather than at display.

Opacity is a property of the schema, never of a value. A field is exempt from
the public scan because its declared type admits nothing but a hash or a trace
identity, not because the string it happens to hold looks like one. A free
identifier or configuration value is scanned however opaque it looks, which is
what stops a credential or an external user identity from publishing merely
because it is the width of a digest.
"""

import re
from datetime import datetime, timedelta
from types import UnionType
from typing import Annotated, Literal, Self, Union, cast, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue, field_validator, model_validator

from unstuck.cascade.selection import Provider
from unstuck.cascade.serialization import canonical_digest
from unstuck.decision import OptionProbability
from unstuck.domain import Contract, Counters, Digest, Identifier, Probability

TelemetryVersion = Literal["telemetry-1"]
Visibility = Literal["private", "public"]
Arm = Literal["decider", "decider_memory"]

#: The telemetry envelope's own version. `Contract.schema_version` versions the
#: project's contract convention; this versions the telemetry field set, so the
#: two move independently.
TELEMETRY_VERSION: TelemetryVersion = "telemetry-1"

#: Explicit provenance for every quantity. `not_supplied` is the honest state of
#: a field this leaf types but no caller has supplied yet, and it is distinct
#: from a provider that was asked and reported nothing (`not_provided`) and from
#: a provider that was never called at all (`not_called`).
Availability = Literal[
    "measured",
    "not_measured",
    "not_provided",
    "not_called",
    "invalid",
    "provider_error",
    "not_supplied",
]

#: One runtime configuration value. Free-form because a log group name or a
#: principal ARN is not an identifier, and bounded because telemetry is bounded.
ConfigValue = Annotated[str, Field(min_length=1, max_length=512)]

#: An OpenTelemetry trace or span identity, as lowercase hex of its own width.
TraceIdentity = Annotated[
    str, Field(min_length=8, max_length=64, pattern=r"^[a-f0-9]+$")
]

#: Values a public record may never carry. Credentials, evaluator truth, hidden
#: causes and expected answers, raw principal and account identity, and local
#: filesystem paths are each refused by shape rather than by field name, so a
#: value that reaches an unexpected field is still caught.
_PUBLIC_FORBIDDEN = re.compile(
    r"(?i)arn:"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|-----BEGIN"
    r"|bearer\s"
    r"|authorization"
    r"|secret|password|credential|api[_-]?key|access[_-]?key"
    r"|evaluator"
    r"|hidden[_\s-]*(?:cause|category|scenario|answer)"
    r"|expected[_\s-]*(?:rows|action|answer)"
    r"|/users/|/home/|/var/folders/|[A-Za-z]:\\"
    r"|(?<!\d)\d{12}(?!\d)"
)

#: The lowercase-hex constraints of the two opaque string types this schema
#: declares, `Digest` and `TraceIdentity`. A field is opaque because it is
#: typed as one of them, so nothing can become opaque by holding a value of the
#: right shape: a twelve digit account identifier in a free identifier or
#: configuration field is scanned, and so is a sixty-four character credential.
_OPAQUE_PATTERNS = frozenset({r"^[a-f0-9]{64}$", r"^[a-f0-9]+$"})


def _opaque_annotation(annotation: object) -> bool:
    """Whether a declared field type admits nothing but an opaque identity.

    A minted `UUID` is opaque because the type parses nothing else. A string is
    opaque only when its annotation carries one of the lowercase-hex patterns
    above, which is what makes this a statement about the schema rather than
    about the value that arrived.
    """
    if annotation is UUID:
        return True
    if get_origin(annotation) in {Union, UnionType}:
        return any(
            _opaque_annotation(option)
            for option in get_args(annotation)
            if option is not type(None)
        )
    if get_origin(annotation) is not Annotated:
        return False
    return any(
        getattr(constraint, "pattern", None) in _OPAQUE_PATTERNS
        for item in get_args(annotation)[1:]
        for constraint in getattr(item, "metadata", (item,))
    )


def _item_annotation(annotation: object) -> object:
    """The declared element type of a sequence field, or nothing."""
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) in {Union, UnionType}:
        for option in get_args(annotation):
            if option is not type(None):
                return _item_annotation(option)
        return None
    arguments = get_args(annotation)
    return arguments[0] if arguments else None


def unsafe_public_values(value: JsonValue) -> tuple[str, ...]:
    """Every string in an untyped projection that carries private identity.

    The walk inspects string leaves only. Counts, flags, and timestamps cannot
    carry a credential or an ARN, and treating them as text would make an epoch
    or a row count look like an account identifier.

    This function has no field types to read, so it grants no exemption at all:
    every string leaf is scanned. `unsafe_public_fields` is the typed walk that
    knows which locations hold an opaque identity, and it is the one a record
    and a timeline are checked with.
    """
    if isinstance(value, str):
        return (value,) if _PUBLIC_FORBIDDEN.search(value) else ()
    if isinstance(value, dict):
        return tuple(
            found for item in value.values() for found in unsafe_public_values(item)
        )
    if isinstance(value, list):
        return tuple(found for item in value for found in unsafe_public_values(item))
    return ()


def unsafe_public_fields(value: object, annotation: object = None) -> tuple[str, ...]:
    """Every value in a typed projection that still carries private identity.

    The walk follows the declared types rather than a serialized copy, so a
    hash, a trace identity and a minted identifier are skipped because the
    schema says that is what they are. Every other string is scanned, however
    opaque it looks, and a value is never preserved for its shape alone.
    """
    if _opaque_annotation(annotation):
        return ()
    if isinstance(value, BaseModel):
        return tuple(
            found
            for name, field in type(value).model_fields.items()
            for found in unsafe_public_fields(
                getattr(value, name), field.rebuild_annotation()
            )
        )
    if isinstance(value, (list, tuple)):
        item = _item_annotation(annotation)
        return tuple(
            found for entry in value for found in unsafe_public_fields(entry, item)
        )
    if isinstance(value, dict):
        return tuple(
            found
            for key, item in value.items()
            for found in unsafe_public_fields(key) + unsafe_public_fields(item)
        )
    if isinstance(value, str):
        return (value,) if _PUBLIC_FORBIDDEN.search(value) else ()
    return ()


def public_digest(value: str) -> str:
    """Stable hash of one identity value, preserving equality across fields.

    The hash is taken over the value alone, so a parent span identity still
    matches the span identity it refers to once both are hashed. Nothing is
    salted per field, because a public export has to keep its links readable.
    """
    return canonical_digest(["telemetry-public", value])


class TelemetryMeasurement(Contract):
    """One quantity with its own provenance. An absent value is never zero."""

    availability: Availability = "not_measured"
    value: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None

    @model_validator(mode="after")
    def explicit_absence(self) -> Self:
        if (self.availability == "measured") != (self.value is not None):
            raise ValueError("measurement availability does not match value")
        return self


NOT_MEASURED = TelemetryMeasurement()
NOT_SUPPLIED = TelemetryMeasurement(availability="not_supplied")
NOT_CALLED = TelemetryMeasurement(availability="not_called")


def measurement_of(value: object) -> TelemetryMeasurement:
    """Widen a core or cascade measurement without inventing a value.

    Both `unstuck.events.Measurement` and `StageMeasurement` are accepted by
    shape. Anything whose availability is not one this schema knows becomes
    `not_measured`, which is the one answer that claims nothing.
    """
    availability = getattr(value, "availability", None)
    amount = getattr(value, "value", None)
    if availability not in get_args(Availability):
        return NOT_MEASURED
    if availability != "measured":
        return TelemetryMeasurement(availability=cast(Availability, availability))
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return TelemetryMeasurement(availability="invalid")
    return TelemetryMeasurement(availability="measured", value=float(amount))


class PricingProvenance(Contract):
    """Where a cost figure came from, or that it came from nowhere yet.

    The currency and the unit belong here rather than beside the amount, for
    the same reason the date and the source do: a cost with no provenance has
    no currency either, and a bare number with an assumed currency is the kind
    of unlabeled claim this schema exists to prevent.
    """

    availability: Literal["measured", "not_supplied", "unavailable"] = "not_supplied"
    pricing_date: str | None = None
    source: ConfigValue | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    unit: Identifier | None = None

    @model_validator(mode="after")
    def explicit_absence(self) -> Self:
        if self.availability == "measured" and (
            self.pricing_date is None or self.source is None
        ):
            raise ValueError("a measured cost states its pricing date and source")
        if self.availability != "measured" and any(
            value is not None
            for value in (self.pricing_date, self.source, self.currency, self.unit)
        ):
            raise ValueError("an unavailable cost claims no pricing provenance")
        return self


class CorrelationContext(Contract):
    """The correlation identity a caller supplies for one run.

    Every field is optional and defaults to absent. An offline run supplies
    almost none of them, and recording that honestly is the point: a hosted run
    later supplies `runtime_session_id`, `trace_id`, and the AWS request
    identity from its own runtime, and nothing here is a constant.
    """

    experiment_id: Identifier | None = None
    case_id: Identifier | None = None
    arm: Arm | None = None
    call_index: Annotated[int, Field(ge=1, le=10000)] | None = None
    idempotency_identity_hash: Digest | None = None
    request_id: UUID | None = None
    runtime_session_id: ConfigValue | None = None
    trace_id: TraceIdentity | None = None
    span_id: TraceIdentity | None = None
    parent_span_id: TraceIdentity | None = None
    aws_request_id: ConfigValue | None = None


class Correlation(CorrelationContext):
    """The complete correlation identity of one recorded event.

    The durable event supplies `event_id`, `sequence`, and `timestamp`, so the
    telemetry timeline is ordered by the authority that wrote it rather than by
    a clock telemetry reads for itself.

    The hashed twins exist for the public projection. A public record carries
    the hash and not the raw value, and because the hash is taken over the value
    alone the links between them survive.

    `experiment_id` and `case_id` are hashed too. The schema types them as free
    identifiers, so nothing here can prove a given value is a synthetic label
    rather than a customer, tenant, user, or case name, and a label that cannot
    be proven synthetic is treated as identity.
    """

    run_id: UUID
    incident_id: UUID | None = None
    event_id: UUID
    sequence: Annotated[int, Field(ge=1)]
    timestamp: datetime
    experiment_id_hash: Digest | None = None
    case_id_hash: Digest | None = None
    request_id_hash: Digest | None = None
    runtime_session_id_hash: Digest | None = None
    trace_id_hash: Digest | None = None
    span_id_hash: Digest | None = None
    parent_span_id_hash: Digest | None = None
    aws_request_id_hash: Digest | None = None

    @field_validator("timestamp")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("UTC timestamp required")
        return value

    @property
    def identity_bearing(self) -> bool:
        """Whether this correlation still carries raw environment identity."""
        return any(
            value is not None
            for value in (
                self.experiment_id,
                self.case_id,
                self.request_id,
                self.runtime_session_id,
                self.trace_id,
                self.span_id,
                self.parent_span_id,
                self.aws_request_id,
            )
        )


class CandidateAvailability(Contract):
    """One offered option as telemetry saw it. Public identity only."""

    public_tool_id: Identifier
    available: bool


class HistoryReference(Contract):
    """One retrieved Decision Receipt as the bounded lookup classified it.

    This is the unit a multi-receipt audit reads. Keeping each receipt's match
    class, outcome class, public tool, reason and blocking eligibility together
    is what makes the lookup reconstructable: parallel arrays can drift, and a
    first-outcome-only projection cannot say which outcome belonged to which
    receipt or why a receipt was exact rather than compatible.

    `blocking_eligible` repeats the verifier's own classification. Telemetry
    neither computes nor changes it.
    """

    receipt_id: Digest
    match_class: Literal["exact", "compatible"]
    outcome_class: Literal[
        "useful_evidence",
        "disposition_unchanged",
        "no_progress",
        "provider_error",
        "invalid_evidence",
        "superseded",
    ]
    public_tool_id: Identifier | None = None
    reason_code: Identifier | None = None
    blocking_eligible: bool | None = None
    admitted_evidence: bool | None = None
    binding_hash: Digest | None = None
    arguments_hash: Digest | None = None
    decision_context_hash: Digest | None = None
    versions_hash: Digest | None = None


class DecisionTelemetry(Contract):
    """What one Decider call was asked and what it answered.

    Authority stays with the selection record the controller persisted. These
    are the same facts, projected: the probabilities and the confidence are
    recorded as reported, and `accepted` is the application's own recomputed
    verdict rather than anything the provider claimed.
    """

    pass_index: Literal[1, 2] | None = None
    decision_id: UUID | None = None
    model_call_id: UUID | None = None
    model_revision: Identifier | None = None
    model_revision_hash: Digest | None = None
    catalog_version: Identifier | None = None
    tool_contract_version: Identifier | None = None
    data_schema_version: Identifier | None = None
    policy_hash: Digest | None = None
    calibration_hash: Digest | None = None
    candidate_public_ids: Annotated[tuple[Identifier, ...], Field(max_length=4)] = ()
    candidate_availability: Annotated[
        tuple[CandidateAvailability, ...], Field(max_length=4)
    ] = ()
    probabilities: (
        Annotated[tuple[OptionProbability, ...], Field(max_length=4)] | None
    ) = None
    confidence: Probability | None = None
    validation_status: Literal["valid", "invalid", "error"] | None = None
    accepted: bool | None = None
    selected_public_tool_id: Identifier | None = None
    selector_input_hash: Digest | None = None
    decision_context_hash: Digest | None = None
    problem_fingerprint: Digest | None = None
    history_status: (
        Literal["not_requested", "unavailable", "empty", "matched"] | None
    ) = None
    history_receipt_ids: Annotated[tuple[Digest, ...], Field(max_length=4)] = ()
    history_references: Annotated[
        tuple[HistoryReference, ...], Field(max_length=4)
    ] = ()
    history_reference_count: Annotated[int, Field(ge=0, le=4)] | None = None
    history_truncated: bool | None = None
    history_retrieval_truncated: bool | None = None
    remaining_selector_calls: Annotated[int, Field(ge=0, le=2)] | None = None
    remaining_diagnostic_reads: Annotated[int, Field(ge=0, le=2)] | None = None

    @model_validator(mode="after")
    def history_corresponds(self) -> Self:
        if self.model_revision is not None and self.model_revision_hash is not None:
            raise ValueError("a decision carries its model revision or its hash")
        identities = tuple(item.receipt_id for item in self.history_references)
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate telemetry history receipt identity")
        if (
            self.history_receipt_ids
            and identities
            and identities != (self.history_receipt_ids)
        ):
            raise ValueError("telemetry history references do not match their ids")
        if (
            self.history_reference_count is not None
            and identities
            and self.history_reference_count != len(identities)
        ):
            raise ValueError("telemetry history reference count mismatch")
        return self

    @property
    def history_match_classes(self) -> tuple[Literal["exact", "compatible"], ...]:
        """The match classes of the retrieved receipts, in retrieval order.

        A read-only view over `history_references`. It is derived rather than
        stored, so it cannot drift away from the receipt it describes.
        """
        return tuple(item.match_class for item in self.history_references)


class ToolTelemetry(Contract):
    """One executed or refused diagnostic read, named only in public terms.

    `execution_key` is the opaque identity a receipt match is about. It is
    derived from the public tool identity and the bound argument hash, so two
    telemetry records can be compared for the same execution without the
    private capability appearing anywhere.
    """

    diagnostic_call_id: UUID | None = None
    public_tool_id: Identifier | None = None
    execution_key: Digest | None = None
    arguments_hash: Digest | None = None
    pass_index: Literal[1, 2] | None = None
    attempt_index: Annotated[int, Field(ge=1, le=2)] | None = None
    read_index: Annotated[int, Field(ge=1, le=2)] | None = None
    provider_status: (
        Literal["evidence", "provider_error", "invalid_evidence"] | None
    ) = None
    provider_error_class: Identifier | None = None
    evidence_ids: Annotated[tuple[UUID, ...], Field(max_length=16)] = ()
    evidence_before_hash: Digest | None = None
    evidence_after_hash: Digest | None = None
    admitted_evidence: bool | None = None
    disposition_before_hash: Digest | None = None
    disposition_after_hash: Digest | None = None
    disposition_moved: bool | None = None


def execution_key(public_tool_id: str, arguments_hash: str) -> str:
    """Opaque identity of one execution, built from public values alone."""
    return canonical_digest(
        {
            "kind": "execution",
            "public_tool_id": public_tool_id,
            "arguments_hash": arguments_hash,
        }
    )


class ReceiptTelemetry(Contract):
    """One Decision Receipt as observed. Eligibility is reported, never decided.

    `blocking_eligible` repeats what the verifier already proved about the
    receipt. Telemetry cannot make a receipt blocking and cannot stop one from
    blocking; it records which it was.
    """

    receipt_id: Digest | None = None
    match_class: Literal["exact", "compatible"] | None = None
    outcome_class: (
        Literal[
            "useful_evidence",
            "disposition_unchanged",
            "no_progress",
            "provider_error",
            "invalid_evidence",
            "superseded",
        ]
        | None
    ) = None
    reason_code: Identifier | None = None
    blocking_eligible: bool | None = None
    namespace_hash: Digest | None = None
    versions_hash: Digest | None = None
    created: bool | None = None
    stored: bool | None = None


class OutcomeTelemetry(Contract):
    """The run's own path and terminal state, as the durable events recorded it."""

    d2_triggered: bool | None = None
    d2_refusal_reason: Identifier | None = None
    blocked_public_tool_id: Identifier | None = None
    blocked_reason_code: Identifier | None = None
    blocked_class: Literal["exact_repeat", "failed_execution"] | None = None
    wait_started: bool | None = None
    wait_finished: bool | None = None
    repair_requested: bool | None = None
    repair_applied: bool | None = None
    verification_status: Literal["passed", "failed", "not_run"] | None = None
    verification_id: UUID | None = None
    terminal_status: (
        Literal[
            "completed",
            "empty_verified",
            "pending",
            "unresolved",
            "budget_exhausted",
            "failed",
        ]
        | None
    ) = None
    terminal_reason: Identifier | None = None
    disposition: Identifier | None = None
    row_count: Annotated[int, Field(ge=0)] | None = None
    row_digest: Digest | None = None
    counters: Counters | None = None
    retry_state: Literal["new", "duplicate", "resumed"] | None = None


class Measurements(Contract):
    """Every quantity of one recorded event, each with its own provenance."""

    stage_duration_ms: TelemetryMeasurement = NOT_MEASURED
    selector_latency_ms: TelemetryMeasurement = NOT_MEASURED
    tool_latency_ms: TelemetryMeasurement = NOT_MEASURED
    receipt_lookup_ms: TelemetryMeasurement = NOT_MEASURED
    receipt_storage_ms: TelemetryMeasurement = NOT_MEASURED
    end_to_end_ms: TelemetryMeasurement = NOT_MEASURED
    input_tokens: TelemetryMeasurement = NOT_SUPPLIED
    output_tokens: TelemetryMeasurement = NOT_SUPPLIED
    total_tokens: TelemetryMeasurement = NOT_SUPPLIED
    cost: TelemetryMeasurement = NOT_MEASURED
    pricing: PricingProvenance = PricingProvenance()
    cpu_percent: TelemetryMeasurement = NOT_SUPPLIED
    memory_mib: TelemetryMeasurement = NOT_SUPPLIED
    throttle_count: TelemetryMeasurement = NOT_SUPPLIED
    error_count: TelemetryMeasurement = NOT_SUPPLIED


class InfrastructureDimensions(Contract):
    """Typed hosting dimensions, supplied by runtime configuration only.

    This contract ships no value. Every field defaults to absent, and a record
    that carries one must declare `runtime_configuration` as its source, so a
    hardcoded account, Region, runtime, endpoint, log group, or model value
    cannot reach telemetry by looking like a default.
    """

    source: Literal["runtime_configuration", "not_supplied"] = "not_supplied"
    account_id: ConfigValue | None = None
    region: ConfigValue | None = None
    runtime_id: ConfigValue | None = None
    endpoint_id: ConfigValue | None = None
    agent_id: ConfigValue | None = None
    log_group: ConfigValue | None = None
    model_id: ConfigValue | None = None
    principal_arn: ConfigValue | None = None
    identity_hash: Digest | None = None

    @property
    def supplied(self) -> tuple[str | None, ...]:
        return (
            self.account_id,
            self.region,
            self.runtime_id,
            self.endpoint_id,
            self.agent_id,
            self.log_group,
            self.model_id,
            self.principal_arn,
        )

    @property
    def identity_bearing(self) -> bool:
        return any(value is not None for value in self.supplied)

    @model_validator(mode="after")
    def configured(self) -> Self:
        configured = self.source == "runtime_configuration"
        if self.identity_bearing and not configured:
            raise ValueError("a hosting dimension requires a configuration source")
        if self.identity_hash is not None and not configured:
            raise ValueError("a hashed hosting identity requires its source")
        return self


class TelemetryRecord(Contract):
    """One safe telemetry event, private or already sanitized for the public.

    `kind` is the durable event type, so the record is traceable back to the
    event that authorized it. The one exception is `telemetry_dropped`, which is
    telemetry's own explicit account of a record it refused to emit, and which
    carries correlation and a reason and nothing else.
    """

    telemetry_version: TelemetryVersion = TELEMETRY_VERSION
    visibility: Visibility = "private"
    kind: Identifier
    stage: Identifier | None = None
    correlation: Correlation
    decision: DecisionTelemetry | None = None
    tool: ToolTelemetry | None = None
    receipt: ReceiptTelemetry | None = None
    outcome: OutcomeTelemetry | None = None
    measurements: Measurements = Measurements()
    infrastructure: InfrastructureDimensions = InfrastructureDimensions()
    mode: Literal["offline"] = "offline"
    #: Which provider answered this run's selector calls. It defaults to
    #: `mock`, so an unlabelled record never reads as a real one, and it is
    #: stamped per recorder rather than per event because it describes the
    #: deployment's composition rather than one event's content.
    provider: Provider = "mock"
    classification: Literal["synthetic"] = "synthetic"
    drop_reason: Identifier | None = None

    @model_validator(mode="after")
    def safe(self) -> Self:
        if (self.drop_reason is not None) != (self.kind == "telemetry_dropped"):
            raise ValueError("a drop record states exactly its reason")
        if self.kind == "telemetry_dropped" and any(
            part is not None
            for part in (self.decision, self.tool, self.receipt, self.outcome)
        ):
            raise ValueError("a drop record carries no observed payload")
        if self.visibility != "public":
            return self
        if self.correlation.identity_bearing:
            raise ValueError("public telemetry carries hashed correlation only")
        if self.infrastructure.identity_bearing:
            raise ValueError("public telemetry carries no raw hosting identity")
        unsafe = unsafe_public_fields(self)
        if unsafe:
            raise ValueError("public telemetry retains private identity")
        return self


#: The most records one run's timeline may carry. The bound is the contract's,
#: and a reader enforces it before it parses rather than after.
MAX_TIMELINE_RECORDS = 200


class TelemetryCompleteness(Contract):
    """Telemetry's own account of how complete one run's record is.

    This is the fact a later process cannot recover for itself. The recorder's
    counters live in memory, so a file reopened in a fresh process has no way
    to tell a complete record from one a transient sink failure punched a hole
    in. Persisting this summary beside the records is what makes the difference
    readable, and `finalized` is what says the run reached its end rather than
    stopping mid-write.

    Every count is telemetry's own, never the run's: `expected_event_count` is
    how many durable events were handed to the recorder, and the sequence
    bounds are the first and last of those events.
    """

    telemetry_version: TelemetryVersion = TELEMETRY_VERSION
    run_id: UUID
    first_sequence: Annotated[int, Field(ge=1)]
    final_sequence: Annotated[int, Field(ge=1)]
    expected_event_count: Annotated[int, Field(ge=1, le=MAX_TIMELINE_RECORDS)]
    emitted: Annotated[int, Field(ge=0, le=MAX_TIMELINE_RECORDS)]
    refused: Annotated[int, Field(ge=0, le=MAX_TIMELINE_RECORDS)]
    dropped: Annotated[int, Field(ge=0, le=MAX_TIMELINE_RECORDS)]
    finalized: bool = False

    @model_validator(mode="after")
    def accounted(self) -> Self:
        if self.final_sequence < self.first_sequence:
            raise ValueError("completeness sequence bounds are inverted")
        if self.final_sequence - self.first_sequence + 1 != self.expected_event_count:
            raise ValueError("completeness sequence span is not contiguous")
        # One emit attempt is made per durable event, whether the record itself
        # or the drop marker that replaces a refused one, so the two outcomes
        # of that attempt have to account for every event exactly once.
        if self.emitted + self.dropped != self.expected_event_count:
            raise ValueError("completeness counts do not account for every event")
        if self.refused > self.expected_event_count:
            raise ValueError("more refusals than events")
        return self

    @property
    def complete(self) -> bool:
        """Whether every durable event was recorded with its own payload."""
        return self.refused == 0 and self.dropped == 0


class TelemetryTimeline(Contract):
    """One run's ordered telemetry, sufficient to reconstruct its causal path.

    Order is the durable sequence, not arrival order, and it is validated here:
    a timeline with a gap, a repeat, or a record from another run is refused
    rather than read as a different causal story. Contiguity is part of that
    refusal, because a dropped durable event leaves a hole rather than a
    reordering, and a hole is exactly what makes a partial file look usable.

    `completeness` is the persisted summary the records are reconciled against.
    It is optional, because a timeline assembled in the process that recorded
    it does not need one to be trusted, and its absence is what makes a reopened
    file unreconciled rather than silently complete.
    """

    telemetry_version: TelemetryVersion = TELEMETRY_VERSION
    visibility: Visibility = "private"
    run_id: UUID
    records: Annotated[
        tuple[TelemetryRecord, ...], Field(max_length=MAX_TIMELINE_RECORDS)
    ] = ()
    completeness: TelemetryCompleteness | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        sequences = [record.correlation.sequence for record in self.records]
        if sequences != sorted(set(sequences)):
            raise ValueError("telemetry sequence is not strictly increasing")
        if any(
            later - earlier != 1
            for earlier, later in zip(sequences, sequences[1:], strict=False)
        ):
            raise ValueError("telemetry sequence has a gap")
        if any(record.correlation.run_id != self.run_id for record in self.records):
            raise ValueError("telemetry run identity mismatch")
        if any(record.visibility != self.visibility for record in self.records):
            raise ValueError("telemetry visibility mismatch")
        identities = [record.correlation.event_id for record in self.records]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate telemetry event identity")
        if self.completeness is not None and self.completeness.run_id != self.run_id:
            raise ValueError("completeness run identity mismatch")
        return self

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(record.kind for record in self.records)

    @property
    def reconciled(self) -> bool:
        """Whether a persisted summary accounts for exactly these records.

        An unreconciled timeline is not a safe public artifact: either no
        summary was persisted, the run never finalized, a record was dropped in
        transport, or the file holds a different span of sequences than the one
        telemetry recorded.
        """
        summary = self.completeness
        if summary is None or not summary.finalized or summary.dropped != 0:
            return False
        if len(self.records) != summary.emitted:
            return False
        sequences = [record.correlation.sequence for record in self.records]
        return bool(sequences) and (
            sequences[0] == summary.first_sequence
            and sequences[-1] == summary.final_sequence
        )
