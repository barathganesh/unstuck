"""The owner's telemetry deployment inputs, typed and never defaulted.

Account and Region are the owner's Isengard deployment inputs. So are the
agent, runtime and endpoint identity, the runtime ARN, the model identity, and
the log group and log stream unified telemetry writes to. Every one of them is
declared here with no default at all, because a default would be a guess about
somebody else's deployment and would work exactly once.

What follows from that is the whole point of this module. A configuration with
a hole in it is a usable object: it validates, it projects the documented
variables it can, and it reports precisely which inputs are still missing. What
it cannot do is pass the live telemetry gate, because a live claim about
latency, token usage, session counts or error rates requires observed records
from a deployment that exists, and an unconfigured field means there is nothing
to have observed.

This module performs no AWS call, constructs no client, reads no ambient
credential or profile, touches no process environment, and opens no socket. It
validates values a caller supplies and projects them into typed form.

The documented variable projection is a map, not a mutation. A deployment
applies it; nothing here sets it. A variable whose inputs are missing is
reported absent rather than projected empty, and the two header variables and
the resource attributes are declared for public redaction because they carry
the log group, the log stream and the service identity.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from unstuck.cloud.config import (
    AgentCoreName,
    AgentRuntimeArn,
    AwsAccountId,
    AwsPartition,
    AwsRegion,
    LogGroupName,
    LogStreamName,
    ModelIdentifier,
    SafeIdentifier,
    ServiceName,
)
from unstuck.domain import Contract
from unstuck.telemetry.contracts import InfrastructureDimensions, PricingProvenance
from unstuck.telemetry.runtime import (
    LIVE_IDENTITY_FIELDS,
    OTEL_VARIABLES,
    VARIABLE_TREATMENT,
    ConfiguredTelemetry,
    ConfigVariable,
    LiveEvidenceGate,
    OtelConfigMap,
    TransactionSearch,
    VariableName,
    scoped_digest,
)

#: The deployment inputs a live telemetry claim needs. Shared with the coverage
#: matrix and the preflight, so all three mean the same set.
REQUIRED_LIVE_FIELDS: tuple[str, ...] = LIVE_IDENTITY_FIELDS

#: The AWS Distro for OpenTelemetry wiring an instrumented AgentCore runtime
#: uses. These name a library and a wire format, not a deployment: no account,
#: Region, runtime, endpoint, model, or log group appears in any of them, so
#: they are the one part of the projection that is the same everywhere.
OTEL_DISTRO = "aws_distro"
OTEL_CONFIGURATOR = "aws_configurator"


class TelemetryRuntimeConfig(Contract):
    """Every telemetry deployment input, each one optional and never defaulted.

    `pricing_source`, `pricing_date` and `currency` travel together: a currency
    with no source and date is an unlabelled cost claim, which this project
    does not make.
    """

    account_id: AwsAccountId | None = None
    region: AwsRegion | None = None
    partition: AwsPartition | None = None
    agent_id: AgentCoreName | None = None
    runtime_id: AgentCoreName | None = None
    endpoint_name: AgentCoreName | None = None
    runtime_arn: AgentRuntimeArn | None = None
    model_id: ModelIdentifier | None = None
    model_revision: SafeIdentifier | None = None
    log_group: LogGroupName | None = None
    log_stream: LogStreamName | None = None
    unified_telemetry: bool = False
    observability_enabled: bool = False
    transaction_search: TransactionSearch = "unknown"
    service_name: ServiceName | None = None
    service_stage: SafeIdentifier | None = None
    classification: Literal["synthetic", "live"] | None = None
    live_evidence_gate: LiveEvidenceGate = "pending"
    pricing_source: ServiceName | None = None
    pricing_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.currency is not None or self.pricing_date is not None) and (
            self.pricing_source is None
        ):
            raise ValueError("a priced bundle names its pricing source")
        if self.pricing_source is not None and self.pricing_date is None:
            raise ValueError("a priced bundle states its pricing date")
        self._arn_agrees()
        return self

    def _arn_agrees(self) -> None:
        """Check the runtime ARN against the separately declared identity.

        The ARN is split on its own separator rather than matched against a
        second pattern, so this module spells no ARN of its own. A field nobody
        supplied is not compared: an incomplete configuration is valid, and it
        is the live gate that refuses it, not the contract.
        """
        if self.runtime_arn is None:
            return
        parts = self.runtime_arn.split(":")
        declared = {
            "partition": (self.partition, parts[1]),
            "region": (self.region, parts[3]),
            "account_id": (self.account_id, parts[4]),
            "runtime_id": (self.runtime_id, parts[5].split("/", 1)[1]),
        }
        for supplied, embedded in declared.values():
            if supplied is not None and supplied != embedded:
                raise ValueError("the runtime ARN disagrees with the declared identity")

    @property
    def missing_live_fields(self) -> tuple[str, ...]:
        """Which live telemetry inputs are still unsupplied, in a fixed order."""
        return tuple(
            name for name in REQUIRED_LIVE_FIELDS if getattr(self, name) is None
        )

    @property
    def inputs_complete(self) -> bool:
        """Whether every live telemetry deployment input has been supplied.

        This is a statement about the configuration and nothing else. It is
        deliberately not called readiness: `live_evidence_unmet` is what says
        whether a live claim could stand, and a complete form is one of several
        conditions it names.
        """
        return not self.missing_live_fields

    @property
    def live_evidence_unmet(self) -> tuple[str, ...]:
        """Every condition a live telemetry claim still fails, in a fixed order.

        `observed_records` is always present. No live record has been observed
        anywhere in this repository, so no configuration can clear this gate,
        and listing the condition rather than omitting it is what keeps the
        preflight's `pending` legible instead of looking like an oversight.
        """
        unmet: list[str] = []
        if self.missing_live_fields:
            unmet.append("deployment_inputs")
        if not self.observability_enabled:
            unmet.append("observability_enabled")
        if self.classification != "live":
            unmet.append("live_classification")
        if self.live_evidence_gate != "established":
            unmet.append("live_evidence_gate")
        unmet.append("observed_records")
        return tuple(unmet)

    @property
    def config_hash(self) -> str:
        """The identity of this exact configuration, hashed over every field."""
        return scoped_digest("telemetry-config", self.model_dump_json())

    def pricing(self) -> PricingProvenance:
        """The cost provenance, or the honest statement that there is none."""
        if self.pricing_source is None:
            return PricingProvenance()
        return PricingProvenance(
            availability="measured",
            pricing_date=self.pricing_date,
            source=self.pricing_source,
            currency=self.currency,
        )

    def dimensions(self) -> InfrastructureDimensions:
        """The hosting dimensions, declaring configuration as their source.

        A configuration that supplied nothing yields dimensions that supplied
        nothing, which is what keeps `not_supplied` distinguishable from a
        deployment whose values happen to be absent from this record.
        """
        supplied = (
            self.account_id,
            self.region,
            self.runtime_id,
            self.endpoint_name,
            self.agent_id,
            self.log_group,
            self.model_id,
        )
        if all(value is None for value in supplied):
            return InfrastructureDimensions()
        return InfrastructureDimensions(
            source="runtime_configuration",
            account_id=self.account_id,
            region=self.region,
            runtime_id=self.runtime_id,
            endpoint_id=self.endpoint_name,
            agent_id=self.agent_id,
            log_group=self.log_group,
            model_id=self.model_id,
        )

    def variables(self) -> OtelConfigMap:
        """Project the documented telemetry variables a deployment applies.

        Nothing is read from or written to the process. A variable whose inputs
        are missing is reported absent; it is never projected as an empty
        string, because a collector cannot tell those apart on arrival.
        """
        headers = (
            f"x-aws-log-group={self.log_group},x-aws-log-stream={self.log_stream}"
            if self.log_group is not None and self.log_stream is not None
            else None
        )
        attributes = (
            ",".join(
                part
                for part in (
                    f"service.name={self.service_name}",
                    None
                    if self.service_stage is None
                    else f"deployment.stage={self.service_stage}",
                    None
                    if self.classification is None
                    else f"unstuck.classification={self.classification}",
                )
                if part is not None
            )
            if self.service_name is not None
            else None
        )
        projected: dict[VariableName, str | None] = {
            "AGENT_OBSERVABILITY_ENABLED": _flag(self.observability_enabled),
            "OTEL_EXPORTER_OTLP_LOGS_HEADERS": headers,
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS": headers,
            "OTEL_PYTHON_CONFIGURATOR": OTEL_CONFIGURATOR,
            "OTEL_PYTHON_DISTRO": OTEL_DISTRO,
            "OTEL_RESOURCE_ATTRIBUTES": attributes,
            "UNIFIED_TRACES_DESTINATION_ENABLED": _flag(self.unified_telemetry),
        }
        return OtelConfigMap(
            variables=tuple(
                ConfigVariable(name=name, value=value)
                for name in OTEL_VARIABLES
                if (value := projected[name]) is not None
            ),
            absent=tuple(name for name in OTEL_VARIABLES if projected[name] is None),
        )

    def configured(self) -> ConfiguredTelemetry:
        """The typed projection a bundle carries. No value is invented here."""
        return ConfiguredTelemetry(
            config_hash=self.config_hash,
            dimensions=self.dimensions(),
            variables=self.variables(),
            observability_enabled=self.observability_enabled,
            unified_telemetry=self.unified_telemetry,
            transaction_search=self.transaction_search,
            service_name=self.service_name,
            service_stage=self.service_stage,
            classification=self.classification or "not_supplied",
            live_evidence_gate=self.live_evidence_gate,
            pricing=self.pricing(),
            missing_live_fields=self.missing_live_fields,
        )

    @property
    def public_withheld(self) -> tuple[str, ...]:
        """The documented variables a public bundle withholds, by name."""
        return tuple(
            sorted(
                name
                for name, treatment in VARIABLE_TREATMENT.items()
                if treatment == "redact"
            )
        )


def _flag(value: bool) -> str:
    """The documented spelling of a telemetry flag. Never an empty string."""
    return "true" if value else "false"
