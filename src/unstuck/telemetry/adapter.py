"""The dependency-free adapter boundary for OpenTelemetry and CloudWatch.

This leaf adds no OpenTelemetry package and makes no AWS SDK call. What it
defines is the boundary a later leaf implements against: the name of every
telemetry field in OpenTelemetry semantic terms, and the typed shape of one
exportable span.

Why a mapping table rather than an exporter. AgentCore Runtime instruments a
deployed agent itself and CloudWatch correlates its spans and logs by trace,
session and span identity, so what this application has to supply is its own
custom span and log attributes under names the collector already understands.
Naming them here, with no transport, keeps the field set reviewable and keeps
this leaf offline.

What the boundary refuses to carry. Model and tool content is explicitly out of
scope: AgentCore treats prompt and tool payloads as requiring an explicit
privacy control, and this application never records them at all, so there is no
attribute here that could carry one. Every attribute below is an identity, a
hash, a public identifier, a count, an availability, or a timing.

Missingness survives the conversion. Every quantity contributes its own
availability attribute whether or not it was measured, and the numeric value
only when it was, so a collector can tell `not_called` from `provider_error`
from `not_provided` instead of seeing one absent metric. Omitting the
availability too would have made all five states look alike on arrival, which
is exactly what the record's own missingness guarantee exists to prevent.

Hashed correlation is exported as an attribute, never as span context. A
sanitized record carries hashes in place of the raw trace and span identities,
and a hash is not a trace identity: putting one in the span's own context slots
would produce a span that claims a trace it is not part of. Each hash is named
with an explicit `.hash` suffix instead.
"""

from typing import Literal

from unstuck.domain import Contract, Identifier
from unstuck.telemetry.contracts import (
    TelemetryRecord,
    TelemetryVersion,
)

Scope = Literal["unstuck.telemetry"]

#: Any value an attribute bag may carry. A collector accepts scalars only, so a
#: nested structure is not exportable and is never built here.
AttributeValue = str | int | float | bool

#: The instrumentation scope a later leaf registers these attributes under.
SCOPE_NAME: Scope = "unstuck.telemetry"

#: OpenTelemetry semantic names for the correlation identity. The three on the
#: left of each pair are span context rather than attributes, so an exporter
#: sets them on the span itself instead of in its attribute bag.
CONTEXT_FIELDS: dict[str, str] = {
    "trace_id": "trace_id",
    "span_id": "span_id",
    "parent_span_id": "parent_span_id",
}

#: Every remaining telemetry field, by its dotted attribute name. The prefix
#: separates this application's own attributes from the runtime's.
ATTRIBUTE_FIELDS: dict[str, str] = {
    "correlation.run_id": "unstuck.run.id",
    "correlation.incident_id": "unstuck.incident.id",
    "correlation.event_id": "unstuck.event.id",
    "correlation.sequence": "unstuck.event.sequence",
    "correlation.experiment_id": "unstuck.experiment.id",
    "correlation.case_id": "unstuck.case.id",
    "correlation.arm": "unstuck.arm",
    "correlation.call_index": "unstuck.call.index",
    "correlation.idempotency_identity_hash": "unstuck.idempotency.hash",
    "correlation.request_id": "unstuck.request.id",
    "correlation.runtime_session_id": "aws.bedrock.agentcore.session.id",
    "correlation.aws_request_id": "aws.request_id",
    "correlation.experiment_id_hash": "unstuck.experiment.id.hash",
    "correlation.case_id_hash": "unstuck.case.id.hash",
    "correlation.request_id_hash": "unstuck.request.id.hash",
    "correlation.runtime_session_id_hash": "unstuck.session.id.hash",
    "correlation.trace_id_hash": "unstuck.trace.id.hash",
    "correlation.span_id_hash": "unstuck.span.id.hash",
    "correlation.parent_span_id_hash": "unstuck.parent_span.id.hash",
    "correlation.aws_request_id_hash": "unstuck.aws.request.id.hash",
    "decision.pass_index": "unstuck.decision.pass",
    "decision.decision_id": "unstuck.decision.id",
    "decision.model_call_id": "unstuck.decision.model_call.id",
    "decision.model_revision": "gen_ai.request.model",
    "decision.confidence": "unstuck.decision.confidence",
    "decision.validation_status": "unstuck.decision.validation",
    "decision.accepted": "unstuck.decision.accepted",
    "decision.selected_public_tool_id": "gen_ai.tool.name",
    "decision.selector_input_hash": "unstuck.decision.input.hash",
    "decision.decision_context_hash": "unstuck.decision.context.hash",
    "decision.problem_fingerprint": "unstuck.problem.fingerprint",
    "decision.catalog_version": "unstuck.catalog.version",
    "decision.tool_contract_version": "unstuck.tool_contract.version",
    "decision.data_schema_version": "unstuck.data_schema.version",
    "decision.policy_hash": "unstuck.policy.hash",
    "decision.calibration_hash": "unstuck.calibration.hash",
    "decision.history_status": "unstuck.history.status",
    "decision.history_truncated": "unstuck.history.truncated",
    "decision.history_retrieval_truncated": "unstuck.history.retrieval.truncated",
    "decision.history_reference_count": "unstuck.history.reference.count",
    "tool.diagnostic_call_id": "unstuck.tool.call.id",
    "tool.public_tool_id": "gen_ai.tool.name",
    "tool.execution_key": "unstuck.tool.execution.key",
    "tool.arguments_hash": "unstuck.tool.arguments.hash",
    "tool.attempt_index": "unstuck.tool.attempt",
    "tool.read_index": "unstuck.tool.read",
    "tool.provider_status": "unstuck.tool.status",
    "tool.provider_error_class": "error.type",
    "tool.admitted_evidence": "unstuck.evidence.admitted",
    "tool.disposition_moved": "unstuck.disposition.moved",
    "receipt.receipt_id": "unstuck.receipt.id",
    "receipt.outcome_class": "unstuck.receipt.outcome",
    "receipt.match_class": "unstuck.receipt.match",
    "receipt.blocking_eligible": "unstuck.receipt.blocking",
    "outcome.terminal_status": "unstuck.run.status",
    "outcome.terminal_reason": "unstuck.run.reason",
    "outcome.verification_status": "unstuck.verification.status",
    "outcome.row_count": "unstuck.result.row_count",
    "outcome.row_digest": "unstuck.result.digest",
    "outcome.d2_triggered": "unstuck.decision.second.triggered",
    "outcome.blocked_class": "unstuck.blocked.class",
    "outcome.blocked_reason_code": "unstuck.blocked.reason",
    "measurements.pricing.availability": "unstuck.cost.pricing.availability",
    "measurements.pricing.pricing_date": "unstuck.cost.pricing.date",
    "measurements.pricing.source": "unstuck.cost.pricing.source",
    "measurements.pricing.currency": "unstuck.cost.currency",
    "measurements.pricing.unit": "unstuck.cost.unit",
    "infrastructure.region": "cloud.region",
    "infrastructure.account_id": "cloud.account.id",
    "infrastructure.runtime_id": "aws.bedrock.agentcore.runtime.id",
    "infrastructure.endpoint_id": "aws.bedrock.agentcore.endpoint.id",
    "infrastructure.agent_id": "aws.bedrock.agentcore.agent.id",
    "infrastructure.model_id": "gen_ai.request.model",
    "infrastructure.log_group": "aws.log.group.names",
    "infrastructure.identity_hash": "unstuck.hosting.identity.hash",
}

#: Every quantity, by the base name of its attribute pair. Each one always
#: contributes `<name>.availability`, and contributes `<name>` itself only when
#: the quantity was measured, so absence, a provider that reported nothing, a
#: provider that was never called, and an invalid figure stay four facts.
MEASUREMENT_FIELDS: dict[str, str] = {
    "stage_duration_ms": "unstuck.stage.duration_ms",
    "selector_latency_ms": "gen_ai.client.operation.duration",
    "tool_latency_ms": "unstuck.tool.duration_ms",
    "receipt_lookup_ms": "unstuck.receipt.lookup.duration_ms",
    "receipt_storage_ms": "unstuck.receipt.storage.duration_ms",
    "end_to_end_ms": "unstuck.run.duration_ms",
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "total_tokens": "gen_ai.usage.total_tokens",
    "cost": "unstuck.cost.amount",
    "cpu_percent": "unstuck.resource.cpu_percent",
    "memory_mib": "unstuck.resource.memory_mib",
    "throttle_count": "unstuck.throttle.count",
    "error_count": "unstuck.error.count",
}

#: The suffix every availability attribute carries. Spelled once, so the pair
#: of names for one quantity cannot drift apart.
AVAILABILITY_SUFFIX = ".availability"

#: Attribute names a public export may never set, because their value is raw
#: environment or case identity. A later leaf exports the hashed hosting
#: identity and the hashed correlation attributes in their place.
PUBLIC_WITHHELD: frozenset[str] = frozenset(
    {
        "cloud.account.id",
        "aws.bedrock.agentcore.runtime.id",
        "aws.bedrock.agentcore.endpoint.id",
        "aws.bedrock.agentcore.agent.id",
        "aws.log.group.names",
        "aws.request_id",
        "aws.bedrock.agentcore.session.id",
        "cloud.region",
        "unstuck.experiment.id",
        "unstuck.case.id",
        "unstuck.request.id",
    }
)


class SpanDescriptor(Contract):
    """One telemetry record described as a span, with no transport attached.

    A later leaf turns this into a real span. Nothing here opens a connection,
    loads a provider, or reads an environment variable, so this contract is
    usable offline and in a test.
    """

    scope: Scope = SCOPE_NAME
    telemetry_version: TelemetryVersion
    name: Identifier
    kind: Literal["internal"] = "internal"
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    attributes: dict[str, AttributeValue] = {}

    @property
    def withheld(self) -> tuple[str, ...]:
        """Attributes set here that a public export must not carry."""
        return tuple(sorted(set(self.attributes) & PUBLIC_WITHHELD))


def _leaf(record: TelemetryRecord, path: str) -> AttributeValue | None:
    part: object = record
    for name in path.split("."):
        if part is None:
            return None
        part = getattr(part, name, None)
    if part is None:
        return None
    if isinstance(part, (str, int, float, bool)):
        return part
    return str(part)


def span_for(record: TelemetryRecord) -> SpanDescriptor:
    """Describe one telemetry record as an exportable span.

    An unsupplied dimension is omitted rather than exported as an empty string,
    because a collector cannot tell those apart once they arrive. A quantity is
    different: its availability is always exported, and its numeric value only
    when it was measured, so absence keeps its own reason on the span.

    The span's trace, span, and parent context come from the raw identities
    alone. A sanitized record has none, and its hashes travel as attributes
    rather than being presented as the trace it is no longer carrying.
    """
    validated = TelemetryRecord.model_validate(record)
    attributes: dict[str, AttributeValue] = {}
    for path, name in ATTRIBUTE_FIELDS.items():
        value = _leaf(validated, path)
        if value is None or name in attributes:
            continue
        attributes[name] = value
    for field, name in MEASUREMENT_FIELDS.items():
        measurement = getattr(validated.measurements, field)
        attributes[name + AVAILABILITY_SUFFIX] = measurement.availability
        if measurement.availability == "measured" and measurement.value is not None:
            attributes[name] = float(measurement.value)
    return SpanDescriptor(
        telemetry_version=validated.telemetry_version,
        name=validated.kind,
        trace_id=validated.correlation.trace_id,
        span_id=validated.correlation.span_id,
        parent_span_id=validated.correlation.parent_span_id,
        attributes=attributes,
    )
