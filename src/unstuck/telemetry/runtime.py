"""The typed shape of configured telemetry, and the documented variable map.

AgentCore Runtime instruments a deployed agent itself and exports
OpenTelemetry-compatible telemetry, and the deployment turns that on through a
fixed set of documented variables. This module holds the typed shape of that
configuration: the variable names, the declared public treatment of each one,
and the projection a bundle carries.

Three rules shape it.

Nothing here is a deployment value. The hosting dimensions arrive as
`InfrastructureDimensions`, which already refuses an identity that does not
declare `runtime_configuration` as its source, and the variable values arrive
as supplied strings. This module defines no account, Region, partition,
runtime, endpoint, model, log group, or ARN, so no deployment is built into it.

Nothing here reads or writes the ambient process. The projection is a typed map
a deployment applies; it never calls `os.getenv`, never mutates `os.environ`,
and has no slot for an access key, a signing secret, or a session credential.
The credential names are listed anyway, and refused by the contract, because
defence in depth is cheap and a future caller building this map by hand is not
reading this docstring.

An absent variable stays absent. A variable whose inputs were not configured is
recorded in `absent` rather than projected as an empty string, because a
collector cannot tell those apart once they arrive, and an empty resource
attribute is a claim that the service has no name rather than the fact that
nobody said.

The public treatment is declared per variable, not decided at display time. The
two header variables and the resource attributes carry the log group, the log
stream and the service identity, so a public projection moves them to
`redacted` and keeps the rest. That is the same shape as the correlation and
hosting treatments in `unstuck.telemetry.sanitizer`: a fixed table, readable
and reviewable, rather than a judgement.

Identity hashes are domain separated. An account identifier and a principal
label that happen to be the same string hash differently, because the domain
takes part in the hash. A hash here is a stable correlation within one bundle
and is never an authentication, an authorization, or a proof of possession.
"""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from unstuck.cascade.serialization import canonical_digest
from unstuck.domain import Contract, Digest, Identifier
from unstuck.events import Redactor
from unstuck.telemetry.contracts import (
    ConfigValue,
    InfrastructureDimensions,
    PricingProvenance,
    Visibility,
)
from unstuck.telemetry.sanitizer import public_identity, public_infrastructure

#: The documented variables an AgentCore deployment sets to enable and route
#: its OpenTelemetry telemetry. The tuple is the whole accounted-for set: a
#: projection states each one either as a value, as absent, or as redacted.
VariableName = Literal[
    "AGENT_OBSERVABILITY_ENABLED",
    "OTEL_PYTHON_DISTRO",
    "OTEL_PYTHON_CONFIGURATOR",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_RESOURCE_ATTRIBUTES",
    "UNIFIED_TRACES_DESTINATION_ENABLED",
]

OTEL_VARIABLES: tuple[VariableName, ...] = (
    "AGENT_OBSERVABILITY_ENABLED",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_PYTHON_CONFIGURATOR",
    "OTEL_PYTHON_DISTRO",
    "OTEL_RESOURCE_ATTRIBUTES",
    "UNIFIED_TRACES_DESTINATION_ENABLED",
)

#: Names a telemetry configuration map may never carry. The typed name above
#: already admits none of them; this is the second refusal, so a map built by a
#: caller that bypasses the literal still cannot carry a credential.
FORBIDDEN_VARIABLES: frozenset[str] = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_CREDENTIAL_EXPIRATION",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "OTEL_EXPORTER_OTLP_HEADERS",
    }
)

Treatment = Literal["keep", "redact"]

#: The declared public treatment of each documented variable. The three
#: redacted ones carry the configured log group, log stream and service
#: identity; the rest carry a protocol, a distro name, or a flag.
VARIABLE_TREATMENT: dict[str, Treatment] = {
    "AGENT_OBSERVABILITY_ENABLED": "keep",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS": "redact",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "keep",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "redact",
    "OTEL_PYTHON_CONFIGURATOR": "keep",
    "OTEL_PYTHON_DISTRO": "keep",
    "OTEL_RESOURCE_ATTRIBUTES": "redact",
    "UNIFIED_TRACES_DESTINATION_ENABLED": "keep",
}

#: How a transaction search setting is recorded. `unknown` is the honest state
#: of a deployment nobody has told us about, and it is not `disabled`.
TransactionSearch = Literal["enabled", "disabled", "unknown"]

#: The owner's explicit live evidence gate. `pending` is the only state this
#: repository ever produces, because no live record has been observed. The gate
#: exists as a separate field rather than as something derived from the
#: configuration, because "every input is supplied" is a statement about a form
#: somebody filled in and `established` is a statement about evidence somebody
#: checked.
LiveEvidenceGate = Literal["pending", "established"]

#: The deployment inputs a live telemetry claim needs, named once here so the
#: deployment contract, the coverage matrix and the preflight all mean the same
#: set. Every one is the owner's own Isengard input: none has a default, none
#: is a product constant, and an absent one keeps the live gate blocked.
LIVE_IDENTITY_FIELDS: tuple[str, ...] = (
    "account_id",
    "agent_id",
    "endpoint_name",
    "log_group",
    "log_stream",
    "model_id",
    "partition",
    "region",
    "runtime_arn",
    "runtime_id",
)

_REDACTOR = Redactor()


def _safe_value(value: str) -> str:
    """Refuse a configuration value carrying a credential or authority marker.

    The same vocabulary `unstuck.cloud.guards.SecretGuard` refuses, applied one
    value at a time, so a header string assembled from configuration cannot
    smuggle an authorization value into a telemetry bundle.
    """
    if _REDACTOR.keys.search(value) or _REDACTOR.authority_markers.search(value):
        raise ValueError("a telemetry variable carries no sensitive marker")
    if _REDACTOR.clean(value) != value:
        raise ValueError("a telemetry variable carries no sensitive marker")
    return value


VariableValue = Annotated[
    str, Field(min_length=1, max_length=1024), AfterValidator(_safe_value)
]

#: A variable name as it is carried in a projection. Typed as a bounded
#: identifier rather than as the closed documented set, so the refusal of a
#: credential variable is a check that runs rather than one the field type has
#: already made unreachable. `OtelConfigMap` enforces membership in the
#: documented set, which it has to account for anyway.
VariableField = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_]+$")
]


def scoped_digest(domain: str, value: str) -> str:
    """Stable, domain-separated hash of one identity value.

    The domain takes part in the hash, so an account identifier and a principal
    label that read the same do not hash the same. The result is a correlation
    handle for one bundle. It is not an authentication, it proves nothing about
    possession, and it must never be accepted in place of one.
    """
    return canonical_digest(
        {"kind": "telemetry-identity", "domain": domain, "value": value}
    )


class ConfigVariable(Contract):
    """One documented telemetry variable and the value a deployment supplies.

    The name is typed as a bounded identifier rather than as the closed
    documented set, so the refusal of a credential variable below is the check
    that actually runs rather than one the field type has already made
    unreachable. Membership in the documented set is enforced one level up, by
    `OtelConfigMap`, which has to account for every documented name anyway.
    """

    name: VariableField
    value: VariableValue

    @model_validator(mode="after")
    def permitted(self) -> Self:
        if self.name in FORBIDDEN_VARIABLES:
            raise ValueError("a credential variable is not telemetry configuration")
        return self


class OtelConfigMap(Contract):
    """Every documented variable, accounted for exactly once.

    A variable is either projected with its value, absent because its inputs
    were not configured, or redacted because a public projection withholds it.
    The three sets partition the documented names, which is what stops a
    missing variable from being indistinguishable from one nobody looked at.
    """

    variables: Annotated[tuple[ConfigVariable, ...], Field(max_length=8)] = ()
    absent: Annotated[tuple[VariableField, ...], Field(max_length=8)] = ()
    redacted: Annotated[tuple[VariableField, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def accounted_for(self) -> Self:
        named = tuple(item.name for item in self.variables)
        every = named + self.absent + self.redacted
        if len(set(every)) != len(every):
            raise ValueError("a telemetry variable is accounted for twice")
        if set(every) != set(OTEL_VARIABLES):
            raise ValueError("a telemetry projection accounts for every variable")
        if named != tuple(sorted(named)):
            raise ValueError("telemetry variables are ordered by name")
        if self.absent != tuple(sorted(self.absent)):
            raise ValueError("absent telemetry variables are ordered by name")
        if self.redacted != tuple(sorted(self.redacted)):
            raise ValueError("redacted telemetry variables are ordered by name")
        if any(VARIABLE_TREATMENT[name] != "redact" for name in self.redacted):
            raise ValueError("only a withheld variable is redacted")
        return self

    @property
    def accounted(self) -> set[str]:
        return (
            {item.name for item in self.variables}
            | set(self.absent)
            | set(self.redacted)
        )

    @property
    def mapping(self) -> dict[str, str]:
        """The projected variables a deployment applies, by name."""
        return {item.name: item.value for item in self.variables}

    def value(self, name: str) -> str | None:
        """The projected value of one variable, or nothing when it has none."""
        return self.mapping.get(name)

    @property
    def config_map_hash(self) -> str:
        return canonical_digest(self)

    def public(self) -> "OtelConfigMap":
        """Withhold every variable the treatment table marks for redaction."""
        withheld = tuple(
            sorted(
                {
                    item.name
                    for item in self.variables
                    if VARIABLE_TREATMENT[item.name] == "redact"
                }
                | set(self.redacted)
            )
        )
        return OtelConfigMap(
            variables=tuple(
                item
                for item in self.variables
                if VARIABLE_TREATMENT[item.name] == "keep"
            ),
            absent=self.absent,
            redacted=withheld,
        )


class ConfiguredTelemetry(Contract):
    """The telemetry configuration one bundle was produced under.

    This is the projection a bundle carries, not the deployment inputs
    themselves. `unstuck.cloud.telemetry.TelemetryRuntimeConfig` owns the
    inputs and builds this; keeping the two apart is what lets the telemetry
    package stay independent of the cloud package.

    `missing_live_fields` is the honest account of what a live claim still
    needs. It travels with the bundle because a reader cannot recover it: an
    empty signal set and an unconfigured deployment look the same otherwise.

    `service_stage` is hashed rather than kept in a public projection. It is
    typed as a free identifier, so nothing here can prove a given value names a
    synthetic stage rather than the owner's production environment, and a stage
    name is one of the things a demonstration audience has no reason to be
    told.
    """

    visibility: Visibility = "private"
    config_hash: Digest
    dimensions: InfrastructureDimensions = InfrastructureDimensions()
    variables: OtelConfigMap
    observability_enabled: bool = False
    unified_telemetry: bool = False
    transaction_search: TransactionSearch = "unknown"
    service_name: ConfigValue | None = None
    service_stage: Identifier | None = None
    service_stage_hash: Digest | None = None
    classification: Literal["synthetic", "live", "not_supplied"] = "not_supplied"
    live_evidence_gate: LiveEvidenceGate = "pending"
    pricing: PricingProvenance = PricingProvenance()
    missing_live_fields: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.service_stage is not None and self.service_stage_hash is not None:
            raise ValueError("a configuration carries its stage or its stage hash")
        if self.visibility != "public":
            return self
        if self.dimensions.identity_bearing:
            raise ValueError("a public telemetry configuration carries no identity")
        if self.service_name is not None:
            raise ValueError("a public telemetry configuration names no service")
        if self.service_stage is not None:
            raise ValueError("a public telemetry configuration names no stage")
        # A variable that was never configured cannot be withheld, so the test
        # is that no identity-bearing variable is projected. Which of them are
        # `redacted` and which are `absent` is then a statement about what the
        # deployment supplied, not about what the projection kept.
        if any(
            VARIABLE_TREATMENT[item.name] == "redact"
            for item in self.variables.variables
        ):
            raise ValueError("a public telemetry configuration withholds its headers")
        return self

    @property
    def inputs_complete(self) -> bool:
        """Whether every deployment input a live telemetry claim needs is set."""
        return not self.missing_live_fields

    @property
    def live_ready(self) -> bool:
        """Whether a live telemetry claim is established, not merely configured.

        Complete deployment inputs are necessary and nowhere near sufficient.
        Telemetry has to be switched on, the deployment has to label what it
        records as live, and the owner's live evidence gate has to be
        explicitly established. An offline synthetic run satisfies the first
        condition and none of the other three, which is exactly why the first
        one on its own is not readiness.
        """
        return (
            self.inputs_complete
            and self.observability_enabled
            and self.classification == "live"
            and self.live_evidence_gate == "established"
        )

    def public(self) -> "ConfiguredTelemetry":
        """Hash the hosting identity and the stage, withhold the headers.

        Idempotent: a configuration that is already public is returned
        unchanged rather than hashed a second time.
        """
        if self.visibility == "public":
            return self
        return ConfiguredTelemetry(
            visibility="public",
            config_hash=self.config_hash,
            dimensions=public_infrastructure(self.dimensions),
            variables=self.variables.public(),
            observability_enabled=self.observability_enabled,
            unified_telemetry=self.unified_telemetry,
            transaction_search=self.transaction_search,
            service_name=None,
            service_stage=None,
            service_stage_hash=public_identity(
                "service_stage", self.service_stage, domain="deployment"
            ),
            classification=self.classification,
            live_evidence_gate=self.live_evidence_gate,
            pricing=self.pricing,
            missing_live_fields=self.missing_live_fields,
        )
