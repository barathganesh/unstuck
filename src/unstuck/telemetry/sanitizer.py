"""Deterministic private to public sanitization.

The private record is the complete safe evidence of one run. The public record
is what a hackathon audience may read, and the difference between them is
declared here field by field rather than decided at display time: sanitization
runs before export, and the public contract refuses anything that still carries
private identity even if this module were bypassed.

Three treatments exist and every identity-bearing field has exactly one.

`keep` is for a value that is already opaque or already synthetic: a hash, a
public tool identifier, a reason code, a count, a timing, an availability. These
are what make the public export sufficient to reconstruct why the first decision
chose what it chose, what ran, what evidence changed, why the second decision
opened, and what the verified result was.

`hash` is for a value that is needed as a correlation but would expose identity
in the clear: the experiment and case labels, the runtime session, the trace and
span identities, the application and AWS request identities, and the hosting
dimensions as one combined identity. The hash is stable and unsalted, so links
between hashed values survive and the same run hashes the same way twice. A
public bundle can still group its records by experiment or case, by the hash.

`remove` is for a value with no public purpose at all: the raw account, the
principal ARN, the Region, the runtime, endpoint, agent, log group and model
values, and the service name. They are dropped, not hashed into a per-field
twin, because their only public use is the one combined identity hash.

Three tables declare those treatments: one for the correlation, one for the
hosting dimensions, and one for the remaining identity-bearing free text, which
is the deployment stage, the model identity carried on a decision or on an
imported span or invocation, and the filename of an exported signal. The last
table exists
because those three used to survive public projection as written, and a stage
name, a model revision and an operator's filename each say something about a
deployment that an audience has no reason to be told.

Credentials, evaluator truth, hidden causes, expected answers, customer and user
identifiers, raw prompt text, unrestricted tool payloads, local paths, and
private capability strings are not sanitized at all: they never enter a
telemetry record in the first place. The typed schema has no field that could
hold one, the recorder validates every record against the shared persistence
boundary before any sink sees it, and `unsafe_public_values` is the last check.
"""

from typing import Literal

from unstuck.cascade.serialization import canonical_digest
from unstuck.domain import Reason, RuntimeFault
from unstuck.telemetry.contracts import (
    Correlation,
    DecisionTelemetry,
    InfrastructureDimensions,
    TelemetryRecord,
    TelemetryTimeline,
    public_digest,
    unsafe_public_fields,
)

Treatment = Literal["keep", "hash", "remove"]

#: The declared public treatment of every correlation field. Anything absent
#: from this map is kept, which is safe because the schema types everything
#: else as an opaque identity, a public identifier, or a closed enumeration.
#: The experiment and case labels are hashed rather than kept: the schema types
#: them as free identifiers, so no check here can prove a given value is a
#: synthetic label rather than a customer, tenant, user, or case name.
CORRELATION_TREATMENT: dict[str, Treatment] = {
    "experiment_id": "hash",
    "case_id": "hash",
    "request_id": "hash",
    "runtime_session_id": "hash",
    "trace_id": "hash",
    "span_id": "hash",
    "parent_span_id": "hash",
    "aws_request_id": "hash",
}

#: The declared public treatment of every identity-bearing field that is
#: neither a correlation nor a hosting dimension. A deployment stage, a model
#: identity and an exported filename each name something about the owner's
#: deployment, and each is typed as free text, so no check can prove a given
#: value is harmless. The service name has no public purpose at all and is
#: removed; the rest are hashed so a reader can still group by them.
IDENTITY_TREATMENT: dict[str, Treatment] = {
    "gen_ai_request_model": "hash",
    "model_revision": "hash",
    "service_name": "remove",
    "service_stage": "hash",
    "source_label": "hash",
}

#: The declared public treatment of every hosting dimension. All are removed
#: individually and contribute to one combined identity hash instead.
INFRASTRUCTURE_TREATMENT: dict[str, Treatment] = {
    "account_id": "remove",
    "region": "remove",
    "runtime_id": "remove",
    "endpoint_id": "remove",
    "agent_id": "remove",
    "log_group": "remove",
    "model_id": "remove",
    "principal_arn": "remove",
}


def public_identity(field: str, value: str | None, *, domain: str) -> str | None:
    """Project one declared identity field onto its public form.

    The treatment is read from `IDENTITY_TREATMENT` rather than restated by the
    caller, so moving a field to `remove` changes what is projected without a
    second edit somewhere else. An absent value stays absent: nothing supplied
    is never hashed into the digest of emptiness.

    `domain` is required, because none of these fields is a join key. A stage
    name, a model identity and an exported filename are labels, and a label
    must not hash to the same digest as a correlation identity that happens to
    read the same: the correlation is a link, and only a link is meant to match
    anything. Two labels in the same domain do still compare equal, which is
    what keeps a public bundle groupable by model or by export.
    """
    if value is None or IDENTITY_TREATMENT[field] == "remove":
        return None
    return canonical_digest(
        {"kind": "telemetry-identity-label", "domain": domain, "value": value}
    )


def public_correlation(correlation: Correlation) -> Correlation:
    """Hash the environment-bearing correlation and drop its raw values."""
    fields = correlation.model_dump()
    fields.pop("schema_version", None)
    for name, treatment in CORRELATION_TREATMENT.items():
        value = fields.get(name)
        fields[name] = None
        if treatment != "hash" or value is None:
            continue
        fields[f"{name}_hash"] = public_digest(str(value))
    return Correlation.model_validate(fields)


def public_decision(decision: DecisionTelemetry) -> DecisionTelemetry:
    """Hash the model identity one Decider call names and drop the raw value.

    The decision is where the application states the model it asked, so the
    same treatment an imported span and an exported invocation already apply is
    applied here. The domain is the model domain, so a public bundle still
    groups its decisions by revision without naming it.
    """
    if decision.model_revision is None:
        return decision
    fields = decision.model_dump()
    fields.pop("schema_version", None)
    return DecisionTelemetry.model_validate(
        fields
        | {
            "model_revision": None,
            "model_revision_hash": public_identity(
                "model_revision", decision.model_revision, domain="model"
            ),
        }
    )


def public_infrastructure(
    infrastructure: InfrastructureDimensions,
) -> InfrastructureDimensions:
    """Reduce the hosting dimensions to one stable identity hash.

    The hash covers the supplied dimensions together, so two runs on the same
    hosting compare equal and a run on different hosting compares different,
    without any one value being readable. Nothing supplied means nothing hashed:
    an absent hosting identity stays absent rather than becoming the hash of
    emptiness.
    """
    if not infrastructure.identity_bearing:
        return InfrastructureDimensions(source=infrastructure.source)
    identity = canonical_digest(
        {
            name: getattr(infrastructure, name)
            for name in sorted(INFRASTRUCTURE_TREATMENT)
        }
    )
    return InfrastructureDimensions(
        source="runtime_configuration", identity_hash=identity
    )


def sanitize(record: TelemetryRecord) -> TelemetryRecord:
    """Project one private record onto its public form.

    The function is total and deterministic: the same private record always
    yields the same public record, and a record that is already public is
    returned unchanged rather than hashed a second time.
    """
    validated = TelemetryRecord.model_validate(record)
    if validated.visibility == "public":
        return validated
    fields = validated.model_dump()
    fields.pop("schema_version", None)
    return TelemetryRecord.model_validate(
        fields
        | {
            "visibility": "public",
            "correlation": public_correlation(validated.correlation),
            "decision": (
                None
                if validated.decision is None
                else public_decision(validated.decision)
            ),
            "infrastructure": public_infrastructure(validated.infrastructure),
        }
    )


def sanitize_timeline(timeline: TelemetryTimeline) -> TelemetryTimeline:
    """Project a whole run's timeline, preserving its recorded order.

    The persisted completeness summary travels with the projection. It carries
    only telemetry's own counts and sequence bounds, so it is public as it
    stands, and dropping it would make every exported timeline unreconciled.
    """
    validated = TelemetryTimeline.model_validate(timeline)
    if validated.visibility == "public":
        return validated
    return TelemetryTimeline(
        visibility="public",
        run_id=validated.run_id,
        records=tuple(sanitize(record) for record in validated.records),
        completeness=validated.completeness,
    )


def public_export(timeline: TelemetryTimeline) -> TelemetryTimeline:
    """The export gate: sanitize one timeline, or refuse to export it at all.

    Sanitization alone cannot tell a complete record from a partial one. A file
    reopened in a later process carries no health counters, so a timeline is
    exportable only when a finalized completeness summary accounts for exactly
    the records present. Anything else is refused as a storage fault rather than
    published as if it were the whole causal story.

    A refused record still reconciles. Its drop marker is one of the records
    the summary counts, so the gap is an exported fact rather than an absence.
    """
    validated = TelemetryTimeline.model_validate(timeline)
    if not validated.reconciled:
        raise RuntimeFault(Reason.STORAGE_ERROR)
    return sanitize_timeline(validated)


def public_findings(timeline: TelemetryTimeline) -> tuple[str, ...]:
    """Every value in an exported timeline that still carries private identity.

    The public contract already refuses such a record, so this returning
    anything means a caller built a public timeline by some other route. It is
    the check an export gate runs rather than a sanitization step.
    """
    return unsafe_public_fields(TelemetryTimeline.model_validate(timeline))
