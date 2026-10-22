"""Offline deployment wiring for the private AgentCore Runtime.

Every AgentCore fact this module relies on is read from a contract installed in
this environment, never from memory:

* `AWS::BedrockAgentCore::Runtime` and `AWS::BedrockAgentCore::RuntimeEndpoint`
  are the `cloudformationResource` types declared by `aws_cdk`'s own
  `aws_bedrockagentcore.CfnRuntime` and `CfnRuntimeEndpoint`.
* The property names, enum values, string patterns and collection bounds come
  from the `botocore` service models `bedrock-agentcore-control` (control plane)
  and `bedrock-agentcore` (data plane).
* `AuthorizerConfiguration` is a union whose only member is
  `customJWTAuthorizer`. There is no unauthenticated, public, or API-key
  alternative in the contract, so omitting the authorizer selects the service's
  own AWS SigV4 authorization rather than an open endpoint. That is why this
  unit sets no authorizer at all, and why a synthesized runtime carrying an
  `AuthorizerConfiguration` is a finding.

`tests/cloud_platform/test_agentcore_deployment.py` asserts each of those
against the installed packages, so a version bump that changes one fails here
instead of reaching a deploy.

What this module does not do is deploy. It builds a CloudFormation template in
process, hashes it, describes the change set an owner-approved deploy would
create, and lists what is still missing. It performs no AWS API call, creates,
updates or deletes nothing, pushes no image, reads no ambient credential or
profile, resolves no CloudFormation context lookup, and spends nothing. Account
and Region arrive as validated inputs; no literal of either appears here.

The live identity of a deployment is an output, never a default. The agent and
runtime identifiers, the endpoint, the runtime ARN, the model revision, the
image digest, and the log group and stream are all supplied by the owner or read
back from a deploy that happened. PF6, PF8 and PF9 stay Pending, and
`deployment_blockers` is the machine-readable form of that.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.config import (
    _REGION,
    AwsAccountId,
    AwsRegion,
    CloudResourceConfig,
    SafeIdentifier,
    sensitive_text,
)
from unstuck.cloud.runtime import AGENTCORE_REQUEST_HEADERS, RUNTIME_IDENTITY_VARIABLES
from unstuck.cloud.telemetry import TelemetryRuntimeConfig
from unstuck.domain import Contract, Digest, canonical_hash

if TYPE_CHECKING:  # pragma: no cover - imports for typing only
    from aws_cdk import App

    from infra.stack import UnstuckStack
    from unstuck.cloud.synth import SynthesisInputs

#: `RuntimeContainerUri` in the installed `bedrock-agentcore-control` model,
#: transcribed so a drift is visible as a failing assertion rather than as a
#: silent widening. This unit accepts a strict subset of it; see
#: `PRIVATE_IMAGE_URI`.
RUNTIME_CONTAINER_URI_PATTERN = (
    r"(([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com(\.cn)?|public\.ecr\.aws)"
    r"/((?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*)"
    r"(?::([^:@]{1,300}))?(?:@(.+))?"
)

#: The subset this unit admits: an image in the deployment account's own private
#: registry, pinned by manifest digest. A mutable tag is refused because the
#: thing that was reviewed and the thing that runs must be the same bytes, and
#: `public.ecr.aws` is refused because a restricted runtime does not take its
#: code from a public registry.
PRIVATE_IMAGE_URI = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com"
    r"/(?P<repository>(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*)"
    r"@(?P<digest>sha256:[0-9a-f]{64})$"
)

#: `NetworkMode` and `ServerProtocol`, as the installed control-plane model
#: enumerates them. Nothing outside these sets is ever synthesized.
AGENTCORE_NETWORK_MODES: tuple[str, ...] = ("PUBLIC", "VPC")
AGENTCORE_SERVER_PROTOCOLS: tuple[str, ...] = ("MCP", "HTTP", "A2A", "AGUI")

#: The one data-plane action that reaches a deployed runtime, and the only
#: AgentCore action any role in this topology is granted.
INVOKE_ACTION = "bedrock-agentcore:InvokeAgentRuntime"

#: The SpringClean tag every resource in the owner's personal test account must
#: carry. It is a tag, not an account: no account literal enters this file.
SPRINGCLEAN_TAG: tuple[str, str] = ("auto-delete", "no")

#: Modelled bounds. `EnvironmentVariablesMap` is capped at 50 entries with keys
#: of at most 100 characters and values of at most 5,000;
#: `LifecycleConfiguration` timeouts run from 60 to 1,209,600 seconds;
#: `RequestHeaderAllowlist` holds 1 to 20 names.
MAX_ENVIRONMENT_VARIABLES = 50
MAX_ENVIRONMENT_KEY = 100
MAX_ENVIRONMENT_VALUE = 5000
MIN_LIFECYCLE_SECONDS = 60
MAX_LIFECYCLE_SECONDS = 1209600
MAX_REQUEST_HEADERS = 20
MAX_TAGS = 50

#: The fixed order of the live test. The denial check sits before the first
#: upload on purpose: an anonymous 200 on restricted content is a leak, and
#: observing the denial first is the only way to learn that before the content
#: exists. Reordering these is a security change, not a convenience.
LIVE_TEST_ORDER: tuple[str, ...] = (
    "deploy-empty-private-origin",
    "attach-auth-gate",
    "verify-anonymous-denial",
    "upload-restricted-content",
    "invoke-as-the-operator",
    "capture-correlation",
    "rollback-and-teardown",
)


def _tag_text(value: str) -> str:
    if sensitive_text(value):
        raise ValueError("a deployment tag carries no sensitive marker")
    return value


def _environment_value(value: str) -> str:
    if sensitive_text(value):
        raise ValueError("an injected variable carries no sensitive marker")
    return value


#: `AgentRuntimeName` and `EndpointName` in the installed control-plane model
#: are both `[a-zA-Z][a-zA-Z0-9_]{0,47}`: a leading letter, then letters, digits
#: and underscores only. A hyphen is refused by the service, so it is refused
#: here rather than at deploy time.
AgentCoreResourceName = Annotated[
    str,
    Field(min_length=1, max_length=48, pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$"),
    AfterValidator(_tag_text),
]
ImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
SubnetId = Annotated[str, Field(pattern=r"^subnet-[0-9a-zA-Z]{8,17}$")]
SecurityGroupId = Annotated[str, Field(pattern=r"^sg-[0-9a-zA-Z]{8,17}$")]
HeaderName = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,255}$")
]
EnvironmentName = Annotated[
    str,
    Field(min_length=1, max_length=MAX_ENVIRONMENT_KEY, pattern=r"^[A-Za-z0-9_]+$"),
]


def _private_image(value: str) -> str:
    """Admit only a digest-pinned image in a private registry."""
    if PRIVATE_IMAGE_URI.fullmatch(value) is None:
        raise ValueError(
            "the container image is a digest-pinned private ECR URI of the form "
            "<account>.dkr.ecr.<region>.amazonaws.com/<repository>@sha256:<digest>"
        )
    return _tag_text(value)


ContainerImageUri = Annotated[
    str, Field(min_length=1, max_length=1024), AfterValidator(_private_image)
]


class DeploymentTag(Contract):
    """One tag, within the modelled `TagKey` and `TagValue` bounds."""

    key: Annotated[
        str,
        Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9\s._:/=+@-]+$"),
        AfterValidator(_tag_text),
    ]
    value: Annotated[
        str,
        Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9\s._:/=+@-]+$"),
        AfterValidator(_tag_text),
    ]


def springclean_tags() -> tuple[DeploymentTag, ...]:
    """The minimum tag set: the required SpringClean retention tag."""
    key, value = SPRINGCLEAN_TAG
    return (DeploymentTag(key=key, value=value),)


class VpcAttachment(Contract):
    """The subnets and security groups a `VPC` network mode attaches to."""

    subnets: Annotated[tuple[SubnetId, ...], Field(min_length=1, max_length=16)]
    security_groups: Annotated[
        tuple[SecurityGroupId, ...], Field(min_length=1, max_length=16)
    ]

    @model_validator(mode="after")
    def distinct(self) -> Self:
        for collection in (self.subnets, self.security_groups):
            if len(set(collection)) != len(collection):
                raise ValueError("a VPC attachment names each resource once")
        return self


class AgentCoreRuntimeInputs(Contract):
    """Every AgentCore Runtime deployment input, with no live default.

    The protocol is closed rather than configurable. The controller container
    serves `POST /invocations` over HTTP, so `HTTP` is the only server protocol
    this artifact can honour; offering `MCP` would describe a container that
    does not exist.

    `network_mode` is required and has no default, because the two values mean
    different blast radii and neither is a safe guess about somebody else's
    account. `PUBLIC` additionally requires the owner's recorded approval: the
    installed contract documents no semantics for it, so this unit treats it as
    the owner's risk decision rather than inferring that it is egress-only.
    Inbound authorization is unaffected either way, since the contract offers no
    unauthenticated inbound path.
    """

    runtime_name: AgentCoreResourceName
    endpoint_name: AgentCoreResourceName
    image_uri: ContainerImageUri
    network_mode: Literal["PUBLIC", "VPC"]
    server_protocol: Literal["HTTP"] = "HTTP"
    vpc: VpcAttachment | None = None
    owner_approved_public_egress: bool = False
    request_header_allowlist: Annotated[
        tuple[HeaderName, ...], Field(min_length=1, max_length=MAX_REQUEST_HEADERS)
    ] = AGENTCORE_REQUEST_HEADERS
    idle_session_timeout_seconds: (
        Annotated[int, Field(ge=MIN_LIFECYCLE_SECONDS, le=MAX_LIFECYCLE_SECONDS)] | None
    ) = None
    max_lifetime_seconds: (
        Annotated[int, Field(ge=MIN_LIFECYCLE_SECONDS, le=MAX_LIFECYCLE_SECONDS)] | None
    ) = None
    tags: Annotated[tuple[DeploymentTag, ...], Field(min_length=1, max_length=MAX_TAGS)]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.runtime_name == self.endpoint_name:
            raise ValueError("the runtime and its endpoint are named distinctly")
        if self.network_mode == "VPC" and self.vpc is None:
            raise ValueError("a VPC network mode names its subnets and security groups")
        if self.network_mode == "PUBLIC":
            if self.vpc is not None:
                raise ValueError("a PUBLIC network mode attaches to no VPC")
            if not self.owner_approved_public_egress:
                raise ValueError(
                    "a PUBLIC network mode needs the owner's recorded approval"
                )
        if self.max_lifetime_seconds is not None and (
            self.idle_session_timeout_seconds is not None
            and self.idle_session_timeout_seconds > self.max_lifetime_seconds
        ):
            raise ValueError("an idle timeout never outlives the maximum lifetime")
        keys = [tag.key for tag in self.tags]
        if len(set(keys)) != len(keys):
            raise ValueError("a deployment names each tag key once")
        key, value = SPRINGCLEAN_TAG
        if not any(tag.key == key and tag.value == value for tag in self.tags):
            raise ValueError(f"every resource carries the {key}={value} tag")
        allowlist = self.request_header_allowlist
        if len(set(allowlist)) != len(allowlist):
            raise ValueError("a request header allowlist names each header once")
        return self

    @property
    def _image_parts(self) -> re.Match[str]:
        matched = PRIVATE_IMAGE_URI.fullmatch(self.image_uri)
        if matched is None:  # pragma: no cover - the validator already refused
            raise CloudFault(CloudReason.CONFIG_INVALID)
        return matched

    @property
    def image_account_id(self) -> str:
        return self._image_parts.group("account")

    @property
    def image_region(self) -> str:
        return self._image_parts.group("region")

    @property
    def image_repository(self) -> str:
        return self._image_parts.group("repository")

    @property
    def image_digest(self) -> str:
        return self._image_parts.group("digest")

    @property
    def tag_map(self) -> dict[str, str]:
        return {tag.key: tag.value for tag in self.tags}


#: The project tag every hosted-provider resource carries, alongside the
#: SpringClean retention tag. Both are tags, not accounts: no account literal
#: enters this file.
PROJECT_TAG: tuple[str, str] = ("project", "unstuck")

#: `ModelName`, `EndpointConfigName` and `EndpointName` in the installed
#: SageMaker models all take the same shape, and the runtime data plane's
#: `EndpointName` is `^[a-zA-Z0-9](-*[a-zA-Z0-9])*` capped at 63. It is
#: anchored at both ends here, which is a strict subset of what the service
#: accepts, so a name this contract admits is one the service admits.
SageMakerResourceName = Annotated[
    str,
    Field(min_length=1, max_length=63, pattern=r"^[a-zA-Z0-9](?:-*[a-zA-Z0-9])*$"),
    AfterValidator(_tag_text),
]
VpcId = Annotated[str, Field(pattern=r"^vpc-[0-9a-f]{8,17}$")]

#: An `ml.` SageMaker instance type. The family letter is checked against the
#: declared device below. This is a naming-family check and not a capability
#: claim: it catches a GPU device paired with a general-purpose instance type,
#: and it does not assert that any particular type is available or sufficient.
SageMakerInstanceType = Annotated[
    str, Field(min_length=4, max_length=64, pattern=r"^ml\.[a-z0-9]+\.[a-z0-9]+$")
]
GPU_INSTANCE_FAMILIES: tuple[str, ...] = ("g", "p")
CPU_INSTANCE_FAMILIES: tuple[str, ...] = ("c", "m", "t", "r")

#: The model artifact location. SageMaker requires `ModelDataUrl` to point at a
#: `tar.gz` or it will not download the file, so that is required here rather
#: than discovered at deploy time.
_MODEL_DATA_URL = re.compile(
    r"^s3://(?P<bucket>[a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/(?P<key>[^\s]+\.tar\.gz)$"
)

#: A Bedrock model the narrator may be pointed at: a foundation model, which
#: carries no account in its ARN, or an inference profile, which does. Both
#: shapes are transcribed from the `modelId` pattern of the installed
#: `bedrock-runtime` `Converse` model.
_BEDROCK_MODEL_ARN = re.compile(
    rf"^arn:aws(?:-[a-z]+)*:bedrock:{_REGION}:(?:"
    r":foundation-model/[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}(?:[.:][a-z0-9-]{1,63})?"
    r"|\d{12}:(?:application-)?inference-profile/[a-zA-Z0-9-:.]+"
    r")$"
)


def _model_data_url(value: str) -> str:
    if _MODEL_DATA_URL.fullmatch(value) is None:
        raise ValueError(
            "the model artifact is an s3://<bucket>/<key>.tar.gz location, "
            "because SageMaker will not download anything else"
        )
    return _tag_text(value)


def _bedrock_model_arn(value: str) -> str:
    if _BEDROCK_MODEL_ARN.fullmatch(value) is None:
        raise ValueError(
            "the narration model is a Bedrock foundation-model or inference-profile ARN"
        )
    return _tag_text(value)


ModelDataUrl = Annotated[
    str, Field(min_length=1, max_length=1024), AfterValidator(_model_data_url)
]
BedrockModelArn = Annotated[
    str, Field(min_length=1, max_length=2048), AfterValidator(_bedrock_model_arn)
]


class HostedNetwork(Contract):
    """The existing private network the hosted providers attach to.

    The VPC is the owner's existing one rather than a new one, so its
    identifier and its private subnets and security groups arrive as inputs.
    Naming them keeps synthesis entirely offline: no availability-zone or
    subnet lookup is resolved against an account.
    """

    vpc_id: VpcId
    subnets: Annotated[tuple[SubnetId, ...], Field(min_length=1, max_length=16)]
    security_groups: Annotated[
        tuple[SecurityGroupId, ...], Field(min_length=1, max_length=16)
    ]

    @model_validator(mode="after")
    def distinct(self) -> Self:
        for collection in (self.subnets, self.security_groups):
            if len(set(collection)) != len(collection):
                raise ValueError("a network attachment names each resource once")
        return self


class HostedProviderInputs(Contract):
    """Every hosted-provider deployment input, with no live default.

    One endpoint, one model, one variant, one instance. The count is closed at
    one because one process holds one checkpoint: a second instance would hold
    a second copy of the same weights and would change nothing about the
    bounds, the receipts, or the two-call budget, and it would double the
    spend against a recorded ceiling.

    `device` and `instance_type` are checked against each other by instance
    family, so a `cuda` deployment pointed at a general-purpose instance type
    refuses here rather than at serving time. That is a naming check and not a
    capability claim.
    """

    model_name: SageMakerResourceName
    endpoint_config_name: SageMakerResourceName
    endpoint_name: SageMakerResourceName
    execution_role_name: Annotated[
        str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_+=,.@-]+$")
    ]
    image_uri: ContainerImageUri
    model_data_url: ModelDataUrl
    model_revision: SafeIdentifier
    instance_type: SageMakerInstanceType
    instance_count: Literal[1] = 1
    device: Literal["cuda", "cpu"]
    network: HostedNetwork
    narration_model_arn: BedrockModelArn
    acceptance_tier: Literal["strict_confidence", "read_only_structural"] = (
        "strict_confidence"
    )
    tags: Annotated[tuple[DeploymentTag, ...], Field(min_length=1, max_length=MAX_TAGS)]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        names = (self.model_name, self.endpoint_config_name, self.endpoint_name)
        if len(set(names)) != len(names):
            raise ValueError(
                "the model, its config and its endpoint are named distinctly"
            )
        family = self.instance_type.split(".")[1][0]
        permitted = (
            GPU_INSTANCE_FAMILIES if self.device == "cuda" else CPU_INSTANCE_FAMILIES
        )
        if family not in permitted:
            raise ValueError(
                f"a {self.device} device names an instance type whose family is "
                f"one of {permitted}"
            )
        keys = [tag.key for tag in self.tags]
        if len(set(keys)) != len(keys):
            raise ValueError("a deployment names each tag key once")
        for key, value in (SPRINGCLEAN_TAG, PROJECT_TAG):
            if not any(tag.key == key and tag.value == value for tag in self.tags):
                raise ValueError(f"every hosted resource carries the {key}={value} tag")
        return self

    @property
    def _image_parts(self) -> re.Match[str]:
        matched = PRIVATE_IMAGE_URI.fullmatch(self.image_uri)
        if matched is None:  # pragma: no cover - the validator already refused
            raise CloudFault(CloudReason.CONFIG_INVALID)
        return matched

    @property
    def image_account_id(self) -> str:
        return self._image_parts.group("account")

    @property
    def image_region(self) -> str:
        return self._image_parts.group("region")

    @property
    def image_repository(self) -> str:
        return self._image_parts.group("repository")

    @property
    def image_digest(self) -> str:
        return self._image_parts.group("digest")

    @property
    def model_bucket(self) -> str:
        matched = _MODEL_DATA_URL.fullmatch(self.model_data_url)
        if matched is None:  # pragma: no cover - the validator already refused
            raise CloudFault(CloudReason.CONFIG_INVALID)
        return matched.group("bucket")

    @property
    def model_key(self) -> str:
        matched = _MODEL_DATA_URL.fullmatch(self.model_data_url)
        if matched is None:  # pragma: no cover - the validator already refused
            raise CloudFault(CloudReason.CONFIG_INVALID)
        return matched.group("key")

    @property
    def model_prefix(self) -> str:
        """The one S3 prefix the execution role may read. Never the bucket."""
        key = self.model_key
        return key.rsplit("/", 1)[0] + "/" if "/" in key else ""

    @property
    def tag_map(self) -> dict[str, str]:
        return {tag.key: tag.value for tag in self.tags}


def hosted_provider_tags() -> tuple[DeploymentTag, ...]:
    """The minimum hosted tag set: retention, and the project it belongs to."""
    return tuple(
        DeploymentTag(key=key, value=value)
        for key, value in (SPRINGCLEAN_TAG, PROJECT_TAG)
    )


class PlannedResource(Contract):
    """One resource the change set would create, by logical id and type."""

    logical_id: Annotated[str, Field(min_length=1, max_length=255)]
    resource_type: Annotated[
        str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9:]+$")
    ]


class DeploymentBlocker(Contract):
    """One input an actual deploy still needs. Absence of evidence, named."""

    id: Annotated[str, Field(min_length=1, max_length=64)]
    detail: Annotated[str, Field(min_length=1, max_length=512)]


class LiveTestStep(Contract):
    """One step of the live test, in its fixed position."""

    order: Annotated[int, Field(ge=1, le=32)]
    id: Annotated[str, Field(min_length=1, max_length=64)]
    action: Annotated[str, Field(min_length=1, max_length=512)]
    expected: Annotated[str, Field(min_length=1, max_length=512)]


class DeploymentPlan(Contract):
    """A description of a change set. Never an action, and never a deploy.

    The variable names are carried without their values. A reader of this plan
    needs to know which documented variables a deployment would set; the values
    include the configured log group and stream, which the telemetry treatment
    table already marks for redaction in anything shared.
    """

    stack_name: SafeIdentifier
    region: AwsRegion
    account_id: AwsAccountId
    operation: Literal["change_set_description"] = "change_set_description"
    #: Closed at zero. A plan that made a call would be a different artifact.
    aws_calls: Literal[0] = 0
    #: Closed at empty. Nothing was created, updated, deleted, or pushed.
    mutations: Annotated[tuple[str, ...], Field(max_length=0)] = ()
    resources: Annotated[tuple[PlannedResource, ...], Field(max_length=256)]
    tags: Annotated[tuple[DeploymentTag, ...], Field(min_length=1, max_length=MAX_TAGS)]
    environment_variable_names: Annotated[
        tuple[EnvironmentName, ...], Field(max_length=MAX_ENVIRONMENT_VARIABLES)
    ] = ()
    environment_absent: Annotated[
        tuple[EnvironmentName, ...], Field(max_length=MAX_ENVIRONMENT_VARIABLES)
    ] = ()
    image_digest: ImageDigest
    template_hash: Digest
    assembly_path: Path
    blockers: Annotated[tuple[DeploymentBlocker, ...], Field(max_length=32)] = ()
    checklist: Annotated[tuple[LiveTestStep, ...], Field(max_length=32)] = ()

    @property
    def blocked(self) -> bool:
        """Whether an actual deploy is still refused."""
        return bool(self.blockers)


def agentcore_environment(
    inputs: AgentCoreRuntimeInputs,
    *,
    config: CloudResourceConfig,
    telemetry: TelemetryRuntimeConfig | None = None,
) -> dict[str, str]:
    """The variables a deployment injects into the runtime. Values, not secrets.

    Two groups, and nothing else. The identity group names the resources this
    controller is bound to, each one taken from the validated configuration or
    from the deployment inputs. The telemetry group is whatever
    `TelemetryRuntimeConfig.variables()` could project; a variable whose inputs
    are missing stays absent rather than arriving as an empty string.

    No credential variable is representable: every value is re-checked against
    the shared sensitive-marker vocabulary, and the telemetry projection already
    refuses the credential names outright.
    """
    identity: dict[str, str] = {
        "UNSTUCK_REGION": config.region,
        "UNSTUCK_ACCOUNT_ID": config.account_id,
        "UNSTUCK_RUNTIME_NAME": inputs.runtime_name,
        "UNSTUCK_RUNTIME_ENDPOINT_NAME": inputs.endpoint_name,
        "UNSTUCK_MODEL_REVISION": config.model_revision,
        "UNSTUCK_DATASET_VERSION": config.dataset_version,
        "UNSTUCK_RUN_TABLE": config.run_table_name,
        "UNSTUCK_RECEIPT_TABLE": config.receipt_table_name,
        "UNSTUCK_ARTIFACT_BUCKET": config.artifact_bucket,
        "UNSTUCK_DECIDER_API_ID": config.decider_api_id,
    }
    if set(identity) != set(RUNTIME_IDENTITY_VARIABLES):
        raise CloudFault(CloudReason.CONFIG_INVALID)
    projected = telemetry.variables().mapping if telemetry is not None else {}
    environment = {**identity, **projected}
    if len(environment) > MAX_ENVIRONMENT_VARIABLES:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    for name, value in environment.items():
        if len(name) > MAX_ENVIRONMENT_KEY or len(value) > MAX_ENVIRONMENT_VALUE:
            raise CloudFault(CloudReason.CONFIG_INVALID)
        _environment_value(value)
    return dict(sorted(environment.items()))


def real_provider_environment(
    *,
    endpoint_name: str,
    narration_model_id: str,
    acceptance_tier: str = "strict_confidence",
    decider_timeout_seconds: float | None = None,
    max_diagnostic_reads: int | None = None,
) -> dict[str, str]:
    """The additional variables a `real` deployment injects. Values, not secrets.

    These are deliberately projected by their own function rather than added to
    `agentcore_environment`. That one is checked against
    `RUNTIME_IDENTITY_VARIABLES` exactly, so a hosted-provider variable joining
    it would break every mock deployment's projection; keeping them apart is
    what lets a `mock` deployment stay byte for byte what it was.

    Nothing here is a credential and nothing here can become one: each value is
    re-checked against the shared sensitive-marker vocabulary, and the
    endpoint name and the model identifier are validated by the same contracts
    the runtime validates them with, so a value this function admits is a value
    the container will admit too.
    """
    from unstuck.cloud.decider import SageMakerDeciderSettings
    from unstuck.cloud.narration import NarrationSettings
    from unstuck.cloud.runtime import (
        DECIDER_TIMEOUT_VARIABLE,
        DIAGNOSTIC_READS_VARIABLE,
        MODE_VARIABLE,
        TIER_VARIABLE,
        decider_timeout_ms,
        diagnostic_read_bound,
    )

    if acceptance_tier not in {"strict_confidence", "read_only_structural"}:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    try:
        # Validated through the runtime's own contracts, so synthesis cannot
        # project a value the container would refuse at startup. The revision
        # is a placeholder here: the real one travels as UNSTUCK_MODEL_REVISION
        # and is the deployment's own, so this call only exercises the
        # endpoint-name constraint.
        SageMakerDeciderSettings(
            endpoint_name=endpoint_name, model_revision="placeholder"
        )
        NarrationSettings(model_id=narration_model_id)
    except (ValueError, TypeError) as error:
        raise CloudFault(CloudReason.CONFIG_INVALID) from error
    environment = {
        MODE_VARIABLE: "real",
        "UNSTUCK_DECIDER_ENDPOINT_NAME": endpoint_name,
        "UNSTUCK_NARRATION_MODEL_ID": narration_model_id,
        TIER_VARIABLE: acceptance_tier,
    }
    if decider_timeout_seconds is not None:
        # Rendered the way the container's own reader parses it, then read back
        # through that reader and the adapter contract, so synthesis cannot
        # project a budget the container would refuse at startup. The integral
        # form is used when the value is integral, because that is what an
        # operator writes and what the reader's pattern is narrowest about.
        rendered = (
            f"{decider_timeout_seconds:.0f}"
            if float(decider_timeout_seconds).is_integer()
            else f"{decider_timeout_seconds}"
        )
        try:
            SageMakerDeciderSettings(
                endpoint_name=endpoint_name,
                model_revision="placeholder",
                provider_ms=decider_timeout_ms({DECIDER_TIMEOUT_VARIABLE: rendered}),
            )
        except (ValueError, TypeError) as error:
            raise CloudFault(CloudReason.CONFIG_INVALID) from error
        environment[DECIDER_TIMEOUT_VARIABLE] = rendered
    if max_diagnostic_reads is not None:
        # Projected only when a deployer names a bound, so every existing
        # projection is unchanged, and read back through the container's own
        # reader so synthesis cannot project a bound it would refuse. A `bool`
        # is an `int` to Python and is refused rather than rendered as `True`.
        if type(max_diagnostic_reads) is not int:
            raise CloudFault(CloudReason.CONFIG_INVALID)
        rendered = str(max_diagnostic_reads)
        diagnostic_read_bound({DIAGNOSTIC_READS_VARIABLE: rendered})
        environment[DIAGNOSTIC_READS_VARIABLE] = rendered
    for name, value in environment.items():
        if len(name) > MAX_ENVIRONMENT_KEY or len(value) > MAX_ENVIRONMENT_VALUE:
            raise CloudFault(CloudReason.CONFIG_INVALID)
        _environment_value(value)
    return dict(sorted(environment.items()))


def check_hosted_agrees(
    inputs: HostedProviderInputs, config: CloudResourceConfig
) -> None:
    """Refuse a hosted image or artifact from another account than the deployment's.

    A serving image from another account is an image the deployment's own
    registry grant does not cover, and an artifact in another account's bucket
    is one the serving role's prefix grant does not cover. Both would fail at
    deploy time or, worse, succeed against somebody else's bytes.
    """
    if inputs.image_account_id != config.account_id:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    if inputs.image_region != config.region:
        raise CloudFault(CloudReason.CONFIG_INVALID)


def check_image_agrees(
    inputs: AgentCoreRuntimeInputs, config: CloudResourceConfig
) -> None:
    """Refuse an image from another account or Region than the deployment's."""
    if inputs.image_account_id != config.account_id:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    if inputs.image_region != config.region:
        raise CloudFault(CloudReason.CONFIG_INVALID)


def deployment_blockers(
    *,
    spend_limit_recorded: bool = False,
    prices_verified: bool = False,
    budget_alarm_configured: bool = False,
    image_published: bool = False,
    pf6_established: bool = False,
    pf8_established: bool = False,
    owner_approved: bool = False,
) -> tuple[DeploymentBlocker, ...]:
    """What an actual deploy still needs. Every default is "not supplied".

    Region, account and the image digest are deployment inputs the plan already
    validated, so they are not listed as unknowns. What remains unknown is
    evidence: a recorded ceiling, verified prices, a configured alarm, a
    published image, the two live preflights, and the owner's approval.
    """
    gates: tuple[tuple[bool, str, str], ...] = (
        (
            spend_limit_recorded,
            "spend-limit",
            "AWS_SPEND_LIMIT_USD is not recorded, so no batch or deploy is "
            "costed against a ceiling.",
        ),
        (
            prices_verified,
            "verified-prices",
            "No per-episode or hourly price input is verified, so an estimate "
            "would be unknown rather than zero (PF9).",
        ),
        (
            budget_alarm_configured,
            "budget-alarm",
            "No Budgets alarm is configured for the deployment account (PF9).",
        ),
        (
            image_published,
            "image-digest-published",
            "The digest-pinned controller image is not published to the "
            "deployment account's private registry.",
        ),
        (
            pf6_established,
            "PF6",
            "AgentCore runtime registration, runtimeSessionId bounds and "
            "private routing are unverified; no reachability is established.",
        ),
        (
            pf8_established,
            "PF8",
            "Anonymous denial of the unsigned invoke, the unsigned Decider "
            "call and the anonymous object read is unverified.",
        ),
        (
            owner_approved,
            "owner-approval",
            "The owner has not approved account access, deployment or spend.",
        ),
    )
    return tuple(
        DeploymentBlocker(id=identifier, detail=detail)
        for satisfied, identifier, detail in gates
        if not satisfied
    )


def live_test_checklist() -> tuple[LiveTestStep, ...]:
    """The live test, in the one order that can observe a denial safely."""
    steps: tuple[tuple[str, str, str], ...] = (
        (
            "deploy-empty-private-origin",
            "Create the stack with the runtime present and no restricted "
            "object, receipt or artifact uploaded yet.",
            "The runtime reports a status and the artifact prefixes are empty.",
        ),
        (
            "attach-auth-gate",
            "Confirm the runtime carries no AuthorizerConfiguration, so AWS "
            "SigV4 authorizes it, and that only the operator role holds "
            "bedrock-agentcore:InvokeAgentRuntime on it.",
            "The deployed template matches the synthesized one and no other "
            "principal holds the invoke action.",
        ),
        (
            "verify-anonymous-denial",
            "Send an unsigned POST to the runtime invocation path, an "
            "unsigned POST to the private Decider path, and an anonymous GET "
            "to an artifact key.",
            "Each call returns an identifiable 401 or 403 from the gate "
            "itself, carrying the service's own denial body or "
            "WWW-Authenticate header rather than a bare 403. Any 200 stops "
            "the deployment.",
        ),
        (
            "upload-restricted-content",
            "Only after the denial above is observed, publish the image "
            "digest and upload the first restricted artifact.",
            "The upload succeeds for the operator role and the anonymous GET "
            "still denies.",
        ),
        (
            "invoke-as-the-operator",
            "Invoke the runtime endpoint with SigV4 as the operator role and "
            "then as a principal holding no invoke grant.",
            "The operator receives a structured result; the ungranted "
            "principal receives AccessDeniedException.",
        ),
        (
            "capture-correlation",
            "Record the CloudTrail event for the invoke, the CloudWatch log "
            "stream for the runtime, and the OTel trace the deployment "
            "exported.",
            "The CloudTrail requestID, the CloudWatch stream carrying the "
            "runtimeSessionId, and the OTel trace id reconcile to one "
            "invocation.",
        ),
        (
            "rollback-and-teardown",
            "Record the rollback: delete the runtime endpoint, then the "
            "runtime, then execute the teardown plan with the retained "
            "buckets and tables left in place.",
            "The runtime is gone, every retained resource is intact, and the "
            "SpringClean tag still reads auto-delete=no on what remains.",
        ),
    )
    checklist = tuple(
        LiveTestStep(order=position, id=identifier, action=action, expected=expected)
        for position, (identifier, action, expected) in enumerate(steps, start=1)
    )
    if tuple(step.id for step in checklist) != LIVE_TEST_ORDER:
        raise CloudFault(CloudReason.PREFLIGHT_REQUIRED)
    return checklist


def _build(
    inputs: "SynthesisInputs",
    agentcore: AgentCoreRuntimeInputs,
    *,
    telemetry: TelemetryRuntimeConfig | None,
    outdir: Path | None,
    hosted: HostedProviderInputs | None = None,
) -> tuple["App", "UnstuckStack", dict[str, str]]:
    """Build the app with the AgentCore runtime wired in. Nothing is resolved.

    `infra` is synthesis-only and is deliberately not part of the installed
    distribution, so it is imported here rather than at module import time: a
    clean install of the wheel can import this module, and only a caller that
    actually synthesizes needs the stacks on the path.
    """
    from aws_cdk import App, Environment

    from infra.stack import UnstuckStack
    from unstuck.cloud.synth import SynthesisInputs

    validated = SynthesisInputs.model_validate(inputs)
    runtime_inputs = AgentCoreRuntimeInputs.model_validate(agentcore)
    check_image_agrees(runtime_inputs, validated.config)
    environment = agentcore_environment(
        runtime_inputs, config=validated.config, telemetry=telemetry
    )
    hosted_inputs = (
        HostedProviderInputs.model_validate(hosted) if hosted is not None else None
    )
    if hosted_inputs is not None:
        # The hosted path adds the real-mode deployment values on top of the
        # identity group, so the container resolves `real` and refuses while
        # any hosted input is absent.
        check_hosted_agrees(hosted_inputs, validated.config)
        environment = environment | real_provider_environment(
            endpoint_name=hosted_inputs.endpoint_name,
            narration_model_id=hosted_inputs.narration_model_arn,
            acceptance_tier=hosted_inputs.acceptance_tier,
        )
    app = App(outdir=str(outdir) if outdir is not None else None)
    stack = UnstuckStack(
        app,
        validated.stack_name,
        config=validated.config,
        holdout_bucket=validated.holdout_bucket,
        vpc_cidr=validated.vpc_cidr,
        decider_ami_id=validated.decider_ami_id,
        decider_instance_type=validated.decider_instance_type,
        main_model_arn=validated.main_model_arn,
        baseline_model_arn=validated.baseline_model_arn,
        operator_principal_arn=validated.operator_principal_arn,
        evaluator_principal_arn=validated.evaluator_principal_arn,
        deployer_principal_arn=validated.deployer_principal_arn,
        cloudformation_execution_role_arn=validated.cloudformation_execution_role_arn,
        agentcore=runtime_inputs,
        agentcore_environment=environment,
        hosted=hosted_inputs,
        telemetry=telemetry,
        env=Environment(
            account=validated.config.account_id, region=validated.config.region
        ),
    )
    return app, stack, environment


def _synthesize(app: "App", stack: "UnstuckStack") -> tuple[Path, dict[str, Any]]:
    assembly = app.synth()
    directory = Path(assembly.directory)
    template_path = directory / f"{stack.stack_name}.template.json"
    if not template_path.exists():
        raise CloudFault(CloudReason.SYNTHESIS_INVALID)
    body: dict[str, Any] = json.loads(template_path.read_text(encoding="utf-8"))
    return directory, body


def deployment_template(
    inputs: "SynthesisInputs",
    agentcore: AgentCoreRuntimeInputs,
    *,
    telemetry: TelemetryRuntimeConfig | None = None,
    outdir: Path | None = None,
    hosted: HostedProviderInputs | None = None,
) -> dict[str, Any]:
    """Synthesize the private topology with the AgentCore runtime wired in.

    `hosted` additionally wires the private SageMaker endpoint, the two
    interface endpoints and the serving role. Supplying nothing leaves the
    template exactly as it was, which is why a mock deployment's synthesis is
    unchanged by this parameter existing.
    """
    app, stack, _ = _build(
        inputs, agentcore, telemetry=telemetry, outdir=outdir, hosted=hosted
    )
    return _synthesize(app, stack)[1]


def plan_deployment(
    inputs: "SynthesisInputs",
    agentcore: AgentCoreRuntimeInputs,
    *,
    telemetry: TelemetryRuntimeConfig | None = None,
    outdir: Path | None = None,
    evidence: Mapping[str, bool] | None = None,
    hosted: HostedProviderInputs | None = None,
) -> DeploymentPlan:
    """Describe the change set an owner-approved deploy would create.

    `evidence` is the owner's record of which live gates have been satisfied.
    Supplying nothing means nothing is satisfied, which is the honest default
    and leaves every blocker in place.
    """
    app, stack, environment = _build(
        inputs, agentcore, telemetry=telemetry, outdir=outdir, hosted=hosted
    )
    directory, template = _synthesize(app, stack)
    runtime_inputs = AgentCoreRuntimeInputs.model_validate(agentcore)
    absent = telemetry.variables().absent if telemetry is not None else ()
    return DeploymentPlan(
        stack_name=stack.stack_name,
        region=stack.region,
        account_id=stack.account,
        resources=tuple(
            PlannedResource(logical_id=logical, resource_type=resource["Type"])
            for logical, resource in sorted(template.get("Resources", {}).items())
        ),
        tags=runtime_inputs.tags,
        environment_variable_names=tuple(sorted(environment)),
        environment_absent=tuple(sorted(absent)),
        image_digest=runtime_inputs.image_digest,
        template_hash=canonical_hash(template),
        assembly_path=directory,
        blockers=deployment_blockers(**dict(evidence or {})),
        checklist=live_test_checklist(),
    )


__all__ = [
    "AGENTCORE_NETWORK_MODES",
    "AGENTCORE_SERVER_PROTOCOLS",
    "INVOKE_ACTION",
    "LIVE_TEST_ORDER",
    "MAX_ENVIRONMENT_VARIABLES",
    "MAX_REQUEST_HEADERS",
    "PRIVATE_IMAGE_URI",
    "RUNTIME_CONTAINER_URI_PATTERN",
    "CPU_INSTANCE_FAMILIES",
    "GPU_INSTANCE_FAMILIES",
    "PROJECT_TAG",
    "SPRINGCLEAN_TAG",
    "BedrockModelArn",
    "HostedNetwork",
    "HostedProviderInputs",
    "ModelDataUrl",
    "SageMakerInstanceType",
    "SageMakerResourceName",
    "AgentCoreRuntimeInputs",
    "DeploymentBlocker",
    "DeploymentPlan",
    "DeploymentTag",
    "LiveTestStep",
    "PlannedResource",
    "VpcAttachment",
    "agentcore_environment",
    "check_hosted_agrees",
    "check_image_agrees",
    "deployment_blockers",
    "hosted_provider_tags",
    "deployment_template",
    "live_test_checklist",
    "plan_deployment",
    "real_provider_environment",
    "springclean_tags",
]
