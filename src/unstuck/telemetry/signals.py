"""Typed import of telemetry some other process already exported.

This module reads local JSON files and nothing else. It makes no AWS call,
constructs no client, installs no exporter, runs no CloudWatch query, loads no
credential, and opens no socket. A signal arrives as a file an operator
exported with their own tooling, and this module's whole job is to refuse it or
turn it into a typed record.

What a record may carry, and what it may not.

OpenTelemetry spans carry trace, span, parent, session and AWS request identity,
a span kind and name, timestamps, a status, the `gen_ai` tool and model fields,
and usage counts. They do not carry prompt text, completion text, or tool
bodies: AgentCore treats model and tool content as requiring an explicit
privacy decision, this project never captures it, and the schema has nowhere to
put it, so an export that contains one is refused rather than stripped.

CloudTrail records carry the request identity, the event source and name, the
time, the caller's class and a hash of its identity, an error code, and a
resource type and hash. The caller's principal ARN and source address are
refused by shape wherever they appear, because that identity has no place in
evidence this project will show an audience.

CloudWatch metric records carry a namespace, a metric name, a hash of the
dimension set, a statistic, a period, a timestamp, a unit, a value with its own
availability, and the window the query covered. The window is part of the
record because a value read over the wrong window is not the same fact.

Model invocation records carry the request identity, the model revision, the
latency, the usage counts, an error class, content hashes, and the masking
status those hashes were produced under. Raw prompts and completions are never
persisted, and a record that captured nothing claims no hash.

Missing is never zero, here as everywhere. Every quantity is a
`TelemetryMeasurement`, so a provider that reported no token count, a span still
open, and a metric the query could not resolve stay three different facts.

Every record states when it was observed, in UTC, and every record is checked
against the declared capture window. A span states it in Unix nanoseconds and
is compared in nanoseconds; the other three state a UTC timestamp directly. A
window that does not cover a record it summarizes is the thing that lets a
matching correlation identifier attach evidence from some other interval.

Every nonempty kind names the export it came from. A record with no declared
`SignalSource` cannot be traced to a file anyone can hash, so it is refused.
A supplied export that contained nothing declares a zero-count source, which is
a different fact from no export at all: the first says the query returned
nothing, the second says nobody looked.

Reading is bounded and descriptor-based. The file is opened without following a
symlink, verified as a regular file from its own descriptor, and refused on size
before anything is read, so an oversized export costs one `fstat` rather than
one allocation of itself. Every refusal is a reason-coded fault.
"""

import hashlib
import json
import os
import re
import stat
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from unstuck.cascade.serialization import canonical_digest
from unstuck.domain import Contract, Digest, Identifier, Reason, RuntimeFault
from unstuck.telemetry.contracts import (
    NOT_SUPPLIED,
    ConfigValue,
    TelemetryMeasurement,
    TraceIdentity,
    Visibility,
    public_digest,
    unsafe_public_fields,
)
from unstuck.telemetry.sanitizer import public_identity

#: The most records one imported file may deliver. Telemetry is bounded, and a
#: longer export is a different artifact rather than a larger one.
MAX_SIGNAL_RECORDS = 500

#: The most bytes one imported file may carry. Checked from the opened
#: descriptor before anything is read.
MAX_SIGNAL_FILE_BYTES = 2_000_000

SignalKind = Literal[
    "otel_spans", "cloudtrail", "cloudwatch_metrics", "model_invocations"
]
SourceKind = Literal[
    "otel_spans",
    "cloudtrail",
    "cloudwatch_metrics",
    "model_invocations",
    "application_timeline",
]

SIGNAL_KIND_ORDER: tuple[SignalKind, ...] = (
    "otel_spans",
    "cloudtrail",
    "cloudwatch_metrics",
    "model_invocations",
)

#: Identity this module refuses wherever it appears in free text: an ARN, a
#: dotted-quad address, and a local filesystem path. Refusing by shape rather
#: than by field name is what catches a value that reached an unexpected field.
_RAW_IDENTITY = re.compile(
    r"(?i)arn:"
    r"|(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])"
    r"|/users/|/home/|/var/folders/|^/|[A-Za-z]:\\"
)

#: One nanosecond epoch bound. Wide enough for any real capture window and
#: narrow enough that a millisecond value in a nanosecond field is refused.
_MAX_UNIX_NANO = 4_000_000_000_000_000_000

#: The Unix epoch, as the one UTC moment nanosecond conversion is relative to.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _no_raw_identity(value: str) -> str:
    if _RAW_IDENTITY.search(value):
        raise ValueError("an imported signal carries no raw principal or address")
    return value


SafeText = Annotated[
    str, Field(min_length=1, max_length=256), AfterValidator(_no_raw_identity)
]
UnixNano = Annotated[int, Field(ge=0, le=_MAX_UNIX_NANO)]
Namespace = Annotated[
    str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9/_.#:-]+$")
]

MetricStatistic = Literal[
    "Sum", "Average", "Maximum", "Minimum", "SampleCount", "p50", "p90", "p99"
]
MetricUnit = Literal[
    "Count", "Count/Second", "Milliseconds", "Seconds", "Bytes", "Percent", "None"
]
CallerClass = Literal[
    "assumed_role", "iam_role", "iam_user", "federated", "service", "unknown"
]
MaskingStatus = Literal["masked", "hashed", "not_captured"]


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp required")
    return value


def unix_nano(moment: datetime) -> int:
    """One UTC moment as Unix nanoseconds, exactly.

    The conversion is integer microseconds scaled by a thousand rather than a
    float multiplication of `timestamp()`, because a window bound that rounds
    is a window bound a record just outside it can compare equal to.
    """
    return (moment - _EPOCH) // timedelta(microseconds=1) * 1000


def from_unix_nano(value: int) -> datetime:
    """Unix nanoseconds as a UTC moment, truncated to the microsecond.

    The truncation is for reading only. Window validation compares whole
    nanoseconds, so nothing is ever decided on the truncated value.
    """
    return _EPOCH + timedelta(microseconds=value // 1000)


SourceLabel = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
]


class SignalSource(Contract):
    """One imported file, by name and content. Never by local path.

    The label is the file's own name. The directory it was read from is
    deliberately not recorded: a bundle that names a local path publishes the
    exporter's filesystem layout, and the content hash is what makes the source
    verifiable anyway.

    A public source carries `label_hash` instead. An operator's filename is
    free text that can carry a case, a customer, a tenant or a date, so it is
    hashed under its own domain rather than shown, and the hash is enough to
    tell two exports apart.

    `record_count` is declared even when it is zero, because a supplied export
    that returned nothing is a different fact from no export at all.
    """

    label: SourceLabel | None = None
    label_hash: Digest | None = None
    kind: SourceKind
    content_hash: Digest
    record_count: Annotated[int, Field(ge=0, le=MAX_SIGNAL_RECORDS)]

    @model_validator(mode="after")
    def named(self) -> Self:
        if (self.label is None) == (self.label_hash is None):
            raise ValueError("a signal source carries its label or its label hash")
        return self

    @property
    def identity_bearing(self) -> bool:
        return self.label is not None

    def public(self) -> "SignalSource":
        """Replace the filename with its domain-separated hash."""
        if self.label is None:
            return self
        return SignalSource(
            label=None,
            label_hash=public_identity("source_label", self.label, domain="signals"),
            kind=self.kind,
            content_hash=self.content_hash,
            record_count=self.record_count,
        )


def source_order(source: SignalSource) -> tuple[str, str, str]:
    """The canonical sort key of one declared source.

    Sources are ordered rather than merely unique so the same set of exports
    hashes one way. A public source sorts by its label hash, which is why the
    public projection reorders rather than copying the private order.
    """
    return (source.kind, source.label or "", source.label_hash or "")


class OtelSpanRecord(Contract):
    """One exported span. Model and tool content has nowhere to live here."""

    trace_id: TraceIdentity | None = None
    span_id: TraceIdentity | None = None
    parent_span_id: TraceIdentity | None = None
    runtime_session_id: ConfigValue | None = None
    aws_request_id: ConfigValue | None = None
    trace_id_hash: Digest | None = None
    span_id_hash: Digest | None = None
    parent_span_id_hash: Digest | None = None
    runtime_session_id_hash: Digest | None = None
    aws_request_id_hash: Digest | None = None
    span_kind: Literal["internal", "server", "client", "producer", "consumer"]
    span_name: SafeText
    start_unix_nano: UnixNano
    end_unix_nano: UnixNano | None = None
    status_code: Literal["unset", "ok", "error"]
    status_class: Identifier | None = None
    gen_ai_system: Identifier | None = None
    gen_ai_operation_name: Identifier | None = None
    gen_ai_request_model: ConfigValue | None = None
    gen_ai_request_model_hash: Digest | None = None
    gen_ai_tool_name: Identifier | None = None
    input_tokens: TelemetryMeasurement = NOT_SUPPLIED
    output_tokens: TelemetryMeasurement = NOT_SUPPLIED
    total_tokens: TelemetryMeasurement = NOT_SUPPLIED

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_unix_nano is not None and self.end_unix_nano < self.start_unix_nano:
            raise ValueError("a span cannot end before it starts")
        if self.status_code == "error" and self.status_class is None:
            raise ValueError("a failed span states its error class")
        if (
            self.gen_ai_request_model is not None
            and self.gen_ai_request_model_hash is not None
        ):
            raise ValueError("a span carries its model identity or its hash")
        if self.identity_bearing and any(
            value is not None
            for value in (
                self.trace_id_hash,
                self.span_id_hash,
                self.parent_span_id_hash,
                self.runtime_session_id_hash,
                self.aws_request_id_hash,
            )
        ):
            raise ValueError("a span carries its raw identity or its hash, not both")
        return self

    @property
    def identity_bearing(self) -> bool:
        return any(
            value is not None
            for value in (
                self.trace_id,
                self.span_id,
                self.parent_span_id,
                self.runtime_session_id,
                self.aws_request_id,
            )
        )

    @property
    def duration_ms(self) -> TelemetryMeasurement:
        """How long the span took, or that it has not ended. Never zero."""
        if self.end_unix_nano is None:
            return TelemetryMeasurement()
        return TelemetryMeasurement(
            availability="measured",
            value=(self.end_unix_nano - self.start_unix_nano) / 1_000_000,
        )

    @property
    def observed_at(self) -> datetime:
        """When this span was observed, in UTC: the moment it started."""
        return from_unix_nano(self.start_unix_nano)

    @property
    def public_projectable(self) -> bool:
        """Whether anything here still has to be hashed or dropped."""
        return self.identity_bearing or self.gen_ai_request_model is not None

    def public(self) -> "OtelSpanRecord":
        """Hash every raw identity and the model identity, drop the raw value."""
        if not self.public_projectable:
            return self
        return OtelSpanRecord.model_validate(
            self.model_dump()
            | {
                "trace_id": None,
                "span_id": None,
                "parent_span_id": None,
                "runtime_session_id": None,
                "aws_request_id": None,
                "gen_ai_request_model": None,
                "trace_id_hash": _hashed(self.trace_id),
                "span_id_hash": _hashed(self.span_id),
                "parent_span_id_hash": _hashed(self.parent_span_id),
                "runtime_session_id_hash": _hashed(self.runtime_session_id),
                "aws_request_id_hash": _hashed(self.aws_request_id),
                "gen_ai_request_model_hash": public_identity(
                    "gen_ai_request_model", self.gen_ai_request_model, domain="model"
                ),
            }
        )


class CloudTrailRecord(Contract):
    """One exported control-plane record. The caller is a class and a hash."""

    aws_request_id: ConfigValue | None = None
    aws_request_id_hash: Digest | None = None
    event_source: SafeText
    event_name: Identifier
    event_time: datetime
    caller_class: CallerClass
    caller_identity_hash: Digest | None = None
    error_code: Identifier | None = None
    resource_type: Identifier | None = None
    resource_hash: Digest | None = None

    _utc_event_time = field_validator("event_time")(_utc)

    @model_validator(mode="after")
    def identified(self) -> Self:
        if self.aws_request_id is not None and self.aws_request_id_hash is not None:
            raise ValueError("a trail record carries its request id or its hash")
        return self

    @property
    def observed_at(self) -> datetime:
        """When this record was observed, in UTC: the event's own time."""
        return self.event_time

    @property
    def identity_bearing(self) -> bool:
        return self.aws_request_id is not None

    def public(self) -> "CloudTrailRecord":
        if not self.identity_bearing:
            return self
        return CloudTrailRecord.model_validate(
            self.model_dump()
            | {
                "aws_request_id": None,
                "aws_request_id_hash": _hashed(self.aws_request_id),
            }
        )


class CloudWatchMetricRecord(Contract):
    """One exported metric reading, with the window the query covered."""

    namespace: Namespace
    metric_name: Identifier
    dimensions_hash: Digest
    statistic: MetricStatistic
    period_seconds: Annotated[int, Field(ge=1, le=86_400)]
    timestamp: datetime
    unit: MetricUnit
    value: TelemetryMeasurement = TelemetryMeasurement()
    runtime_session_id: ConfigValue | None = None
    runtime_session_id_hash: Digest | None = None
    query_window_start: datetime
    query_window_end: datetime

    _utc_fields = field_validator(
        "timestamp", "query_window_start", "query_window_end"
    )(_utc)

    @model_validator(mode="after")
    def within_its_window(self) -> Self:
        if self.query_window_end < self.query_window_start:
            raise ValueError("a metric query window is ordered")
        if not (self.query_window_start <= self.timestamp <= self.query_window_end):
            raise ValueError("a metric reading lies inside the window it was read over")
        if (
            self.runtime_session_id is not None
            and self.runtime_session_id_hash is not None
        ):
            raise ValueError("a metric carries its session id or its hash")
        return self

    @property
    def observed_at(self) -> datetime:
        """When this reading was observed, in UTC: the datapoint's timestamp."""
        return self.timestamp

    @property
    def identity_bearing(self) -> bool:
        return self.runtime_session_id is not None

    def public(self) -> "CloudWatchMetricRecord":
        if not self.identity_bearing:
            return self
        return CloudWatchMetricRecord.model_validate(
            self.model_dump()
            | {
                "runtime_session_id": None,
                "runtime_session_id_hash": _hashed(self.runtime_session_id),
            }
        )


class ModelInvocationRecord(Contract):
    """One exported model invocation. No prompt or completion text, ever.

    `observed_at` has no default. An invocation with no observation time cannot
    be placed inside or outside the capture window the bundle declares, so the
    counts and joins that summarize it would be unbound in time.
    """

    observed_at: datetime
    aws_request_id: ConfigValue | None = None
    trace_id: TraceIdentity | None = None
    span_id: TraceIdentity | None = None
    runtime_session_id: ConfigValue | None = None
    aws_request_id_hash: Digest | None = None
    trace_id_hash: Digest | None = None
    span_id_hash: Digest | None = None
    runtime_session_id_hash: Digest | None = None
    model_revision: ConfigValue | None = None
    model_revision_hash: Digest | None = None
    latency_ms: TelemetryMeasurement = TelemetryMeasurement()
    input_tokens: TelemetryMeasurement = NOT_SUPPLIED
    output_tokens: TelemetryMeasurement = NOT_SUPPLIED
    total_tokens: TelemetryMeasurement = NOT_SUPPLIED
    error_class: Identifier | None = None
    input_hash: Digest | None = None
    output_hash: Digest | None = None
    masking_status: MaskingStatus = "not_captured"

    _utc_observed_at = field_validator("observed_at")(_utc)

    @model_validator(mode="after")
    def captured(self) -> Self:
        if self.masking_status == "not_captured" and (
            self.input_hash is not None or self.output_hash is not None
        ):
            raise ValueError("an uncaptured invocation claims no content hash")
        if self.model_revision is not None and self.model_revision_hash is not None:
            raise ValueError("an invocation carries its model revision or its hash")
        if self.identity_bearing and any(
            value is not None
            for value in (
                self.aws_request_id_hash,
                self.trace_id_hash,
                self.span_id_hash,
                self.runtime_session_id_hash,
            )
        ):
            raise ValueError("an invocation carries its raw identity or its hash")
        return self

    @property
    def identity_bearing(self) -> bool:
        return any(
            value is not None
            for value in (
                self.aws_request_id,
                self.trace_id,
                self.span_id,
                self.runtime_session_id,
            )
        )

    @property
    def public_projectable(self) -> bool:
        """Whether anything here still has to be hashed or dropped."""
        return self.identity_bearing or self.model_revision is not None

    def public(self) -> "ModelInvocationRecord":
        if not self.public_projectable:
            return self
        return ModelInvocationRecord.model_validate(
            self.model_dump()
            | {
                "aws_request_id": None,
                "trace_id": None,
                "span_id": None,
                "runtime_session_id": None,
                "model_revision": None,
                "aws_request_id_hash": _hashed(self.aws_request_id),
                "trace_id_hash": _hashed(self.trace_id),
                "span_id_hash": _hashed(self.span_id),
                "runtime_session_id_hash": _hashed(self.runtime_session_id),
                "model_revision_hash": public_identity(
                    "model_revision", self.model_revision, domain="model"
                ),
            }
        )


def _hashed(value: str | None) -> str | None:
    """The public twin of one raw identity, or nothing when there was none.

    `public_digest` is deliberately reused rather than a domain-separated hash:
    the application timeline hashes its own correlation the same way, so a
    public span and the public record it belongs to still join.
    """
    return None if value is None else public_digest(value)


class ExternalSignals(Contract):
    """Every imported signal for one capture window, with its own sources.

    The declared sources are reconciled against the records they delivered. A
    source that claims more records than arrived is the thing that makes a
    partial export look whole, so it is refused here rather than discovered
    later in the join coverage.

    Reconciliation runs in both directions. A source that over-claims is
    refused, and so is a kind that delivered records while naming no source at
    all: a record that cannot be traced to a file anyone can hash has no
    provenance, and a manifest built over it can only describe itself.

    Every record is inside the declared window, across all four kinds. Spans
    are compared in Unix nanoseconds, the other three by their UTC observation
    time, and the comparison is inclusive at both bounds.
    """

    signals_version: Literal["telemetry-signals-1"] = "telemetry-signals-1"
    visibility: Visibility = "private"
    capture_window_start: datetime
    capture_window_end: datetime
    spans: Annotated[
        tuple[OtelSpanRecord, ...], Field(max_length=MAX_SIGNAL_RECORDS)
    ] = ()
    trail: Annotated[
        tuple[CloudTrailRecord, ...], Field(max_length=MAX_SIGNAL_RECORDS)
    ] = ()
    metrics: Annotated[
        tuple[CloudWatchMetricRecord, ...], Field(max_length=MAX_SIGNAL_RECORDS)
    ] = ()
    invocations: Annotated[
        tuple[ModelInvocationRecord, ...], Field(max_length=MAX_SIGNAL_RECORDS)
    ] = ()
    sources: Annotated[tuple[SignalSource, ...], Field(max_length=32)] = ()

    _utc_window = field_validator("capture_window_start", "capture_window_end")(_utc)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.capture_window_end < self.capture_window_start:
            raise ValueError("a capture window is ordered")
        self._inside_window()
        self._reconciled()
        if self.visibility != "public":
            return self
        if self.identity_bearing:
            raise ValueError("public signals carry hashed identity only")
        if any(source.identity_bearing for source in self.sources):
            raise ValueError("public signal sources carry a hashed label only")
        if unsafe_public_fields(self):
            raise ValueError("public signals retain private identity")
        return self

    def _inside_window(self) -> None:
        """Refuse any record the declared window does not cover."""
        start = unix_nano(self.capture_window_start)
        end = unix_nano(self.capture_window_end)
        for record in self.spans:
            if not start <= record.start_unix_nano <= end:
                raise ValueError("a span starts outside the capture window")
            if record.end_unix_nano is not None and not (
                start <= record.end_unix_nano <= end
            ):
                raise ValueError("a span ends outside the capture window")
        for entry in self.trail:
            if not self._within(entry.observed_at):
                raise ValueError("a trail record lies outside the capture window")
        for metric in self.metrics:
            if not self._within(metric.observed_at):
                raise ValueError("a metric reading lies outside the capture window")
        for call in self.invocations:
            if not self._within(call.observed_at):
                raise ValueError("an invocation lies outside the capture window")

    def _reconciled(self) -> None:
        """Refuse a source set that does not account for the records present."""
        keys = [source_order(source) for source in self.sources]
        if len(set(keys)) != len(keys):
            raise ValueError("a signal source is declared twice")
        if keys != sorted(keys):
            raise ValueError("signal sources are ordered by kind and label")
        counts = self.counts
        supplied = self.supplied_kinds
        for kind in SIGNAL_KIND_ORDER:
            if kind not in supplied:
                if counts[kind]:
                    raise ValueError("an imported record names no source manifest")
                continue
            declared = sum(
                source.record_count for source in self.sources if source.kind == kind
            )
            if declared != counts[kind]:
                raise ValueError("a signal source count does not reconcile")

    def _within(self, moment: datetime) -> bool:
        return self.capture_window_start <= moment <= self.capture_window_end

    @property
    def counts(self) -> dict[str, int]:
        """How many records arrived per kind, including the empty kinds."""
        return {
            "otel_spans": len(self.spans),
            "cloudtrail": len(self.trail),
            "cloudwatch_metrics": len(self.metrics),
            "model_invocations": len(self.invocations),
        }

    @property
    def supplied_kinds(self) -> frozenset[str]:
        """Which kinds declared an export, whatever that export delivered.

        A kind in this set with a count of zero is a query that returned
        nothing. A kind outside it is a query nobody ran, and the two are never
        reported as the same thing.
        """
        return frozenset(source.kind for source in self.sources)

    @property
    def provenance_complete(self) -> bool:
        """Whether every imported kind names the export it came from.

        Completeness is per kind rather than per record, because a kind with no
        source at all is the case a count cannot reveal: zero records and no
        export read identically once the summary is written.
        """
        return all(kind in self.supplied_kinds for kind in SIGNAL_KIND_ORDER)

    @property
    def identity_bearing(self) -> bool:
        """Whether any record anywhere still carries a raw identity.

        Each kind is tested separately rather than over one merged sequence:
        the four record types share the property but not a base class, so a
        merged walk would lose the type that makes the property readable.
        """
        return (
            any(record.identity_bearing for record in self.spans)
            or any(record.identity_bearing for record in self.trail)
            or any(record.identity_bearing for record in self.metrics)
            or any(record.identity_bearing for record in self.invocations)
        )

    @property
    def signals_hash(self) -> str:
        return canonical_digest(self)

    def public(self) -> "ExternalSignals":
        """Hash every raw identity across every kind, preserving the joins.

        The sources are reordered rather than copied, because a public source
        sorts by its label hash and the private order was by filename.
        """
        if self.visibility == "public":
            return self
        return ExternalSignals(
            visibility="public",
            capture_window_start=self.capture_window_start,
            capture_window_end=self.capture_window_end,
            spans=tuple(record.public() for record in self.spans),
            trail=tuple(record.public() for record in self.trail),
            metrics=tuple(record.public() for record in self.metrics),
            invocations=tuple(record.public() for record in self.invocations),
            sources=tuple(
                sorted((source.public() for source in self.sources), key=source_order)
            ),
        )


#: One reader per kind, so every record is validated in the JSON mode it was
#: exported in rather than coerced from a Python object.
_SPAN_LIST = TypeAdapter(list[OtelSpanRecord])
_TRAIL_LIST = TypeAdapter(list[CloudTrailRecord])
_METRIC_LIST = TypeAdapter(list[CloudWatchMetricRecord])
_INVOCATION_LIST = TypeAdapter(list[ModelInvocationRecord])


def read_signal_file(
    path: Path, kind: SignalKind, *, max_bytes: int = MAX_SIGNAL_FILE_BYTES
) -> tuple[SignalSource, bytes]:
    """Read one exported JSON file within its declared bounds.

    The file is opened without following a symlink and verified as a regular
    file from its own descriptor, and its size is checked before any content is
    read. The returned source names the file and binds its content by hash; the
    directory it came from is deliberately not recorded.

    The payload is returned as the bytes that were read rather than as parsed
    objects. The records are validated from JSON, which is the form they were
    exported in: these contracts are strict, and a timestamp that arrived as a
    JSON string is a timestamp, where the same string handed to a Python-mode
    validator is not.
    """
    target = Path(path)
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise RuntimeFault(Reason.STORAGE_ERROR) from error
    try:
        handle = os.fdopen(descriptor, "rb")
    except OSError as error:
        os.close(descriptor)
        raise RuntimeFault(Reason.STORAGE_ERROR) from error
    with handle:
        try:
            status = os.fstat(handle.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_size > max_bytes:
                raise RuntimeFault(Reason.STORAGE_ERROR)
            body = handle.read(max_bytes + 1)
        except OSError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
    if len(body) > max_bytes:
        raise RuntimeFault(Reason.STORAGE_ERROR)
    try:
        parsed = json.loads(body)
    except (UnicodeError, ValueError) as error:
        raise RuntimeFault(Reason.STORAGE_ERROR) from error
    if not isinstance(parsed, list) or len(parsed) > MAX_SIGNAL_RECORDS:
        raise RuntimeFault(Reason.STORAGE_ERROR)
    try:
        source = SignalSource(
            label=target.name,
            kind=kind,
            content_hash=hashlib.sha256(body).hexdigest(),
            record_count=len(parsed),
        )
    except ValidationError as error:
        raise RuntimeFault(Reason.STORAGE_ERROR) from error
    return source, body


def import_signals(
    entries: Sequence[tuple[SignalKind, Path]],
    *,
    capture_window_start: datetime,
    capture_window_end: datetime,
) -> ExternalSignals:
    """Import every declared export into one typed, reconciled signal set.

    Every refusal is reason-coded: a file this module cannot read, a payload it
    cannot parse, and a record the contract rejects all fail closed rather than
    delivering a partial signal set that looks complete.
    """
    spans: list[OtelSpanRecord] = []
    trail: list[CloudTrailRecord] = []
    metrics: list[CloudWatchMetricRecord] = []
    invocations: list[ModelInvocationRecord] = []
    sources: list[SignalSource] = []
    for kind, path in entries:
        source, body = read_signal_file(path, kind)
        try:
            if kind == "otel_spans":
                spans.extend(_SPAN_LIST.validate_json(body))
            elif kind == "cloudtrail":
                trail.extend(_TRAIL_LIST.validate_json(body))
            elif kind == "cloudwatch_metrics":
                metrics.extend(_METRIC_LIST.validate_json(body))
            else:
                invocations.extend(_INVOCATION_LIST.validate_json(body))
        except ValidationError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        sources.append(source)
    try:
        return ExternalSignals(
            capture_window_start=capture_window_start,
            capture_window_end=capture_window_end,
            spans=tuple(spans),
            trail=tuple(trail),
            metrics=tuple(metrics),
            invocations=tuple(invocations),
            sources=tuple(sorted(sources, key=source_order)),
        )
    except ValidationError as error:
        raise RuntimeFault(Reason.STORAGE_ERROR) from error
