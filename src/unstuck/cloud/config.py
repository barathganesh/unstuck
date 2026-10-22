"""Typed cloud resource configuration. Identifiers and non-secret versions only.

Per `docs/spec/design.md` section 14 there is no external supplemental provider
secret in this architecture, so runtime configuration carries AWS resource
identifiers plus pinned versions and hashes and nothing else. Every field is
pattern constrained, and a value shaped like a credential, an authorization
header, an environment value, an evaluator reference, or a customer identifier
is refused by the contract rather than redacted after the fact.

The separate evaluator-truth bucket of design.md section 16 is deliberately
absent here: the controller never needs its name, so the controller cannot
reference it at all. It is a synthesis input (`unstuck.cloud.synth`) and an IAM
denial, not runtime configuration.
"""

import re
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from unstuck.cascade.cards import (
    CATALOG_VERSION,
    DATA_SCHEMA_VERSION,
    TOOL_CONTRACT_VERSION,
)
from unstuck.domain import Contract, Digest, Identifier
from unstuck.events import Redactor

_REDACTOR = Redactor()
_IP_FORM = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_REGION = r"[a-z]{2}(?:-[a-z]+)+-\d"


def sensitive_text(value: str) -> bool:
    """True when a string carries a credential, authority, or secret marker.

    This is the single shared definition used by the configuration contract and
    by `unstuck.cloud.guards.SecretGuard`, so both refuse the same vocabulary.
    """
    return (
        bool(_REDACTOR.keys.search(value))
        or bool(_REDACTOR.authority_markers.search(value))
        or _REDACTOR.clean(value) != value
    )


def _clean(value: str) -> str:
    if sensitive_text(value):
        raise ValueError("configuration values carry no sensitive marker")
    return value


def _bucket_name(value: str) -> str:
    if ".." in value or value.startswith("xn--") or value.endswith("-s3alias"):
        raise ValueError("invalid S3 bucket name")
    if _IP_FORM.match(value):
        raise ValueError("S3 bucket name must not take an IP address form")
    return _clean(value)


AwsRegion = Annotated[str, Field(pattern=rf"^{_REGION}$"), AfterValidator(_clean)]
AwsAccountId = Annotated[str, Field(pattern=r"^\d{12}$")]
BucketName = Annotated[
    str,
    Field(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$"),
    AfterValidator(_bucket_name),
]
TableName = Annotated[
    str,
    Field(min_length=3, max_length=255, pattern=r"^[A-Za-z0-9_.-]+$"),
    AfterValidator(_clean),
]
ControllerRuntimeArn = Annotated[
    str,
    Field(
        max_length=512,
        pattern=rf"^arn:aws:bedrock-agentcore:{_REGION}:\d{{12}}:runtime/[A-Za-z0-9_-]{{1,64}}$",
    ),
    AfterValidator(_clean),
]
ApiGatewayId = Annotated[
    str, Field(pattern=r"^[a-z0-9]{6,16}$"), AfterValidator(_clean)
]
VpcEndpointId = Annotated[
    str, Field(pattern=r"^vpce-[0-9a-f]{8,32}$"), AfterValidator(_clean)
]
SafeIdentifier = Annotated[Identifier, AfterValidator(_clean)]
# `EndpointName` in the installed `sagemaker-runtime` model is
# `^[a-zA-Z0-9](-*[a-zA-Z0-9])*` with a maximum length of 63. It is anchored at
# both ends here, which is a strict subset of what the service accepts, so a
# name this contract admits is always a name the service admits.
# `tests/cloud_platform/test_cloud_decider.py` compares it against that model.
SageMakerEndpointName = Annotated[
    str,
    Field(min_length=1, max_length=63, pattern=r"^[a-zA-Z0-9](?:-*[a-zA-Z0-9])*$"),
    AfterValidator(_clean),
]

# The typed vocabulary telemetry configuration is built from. It lives here,
# beside the resource configuration, so one definition of "a safe configured
# string" covers both and neither drifts. Every one of these types describes a
# shape; none of them carries a value, so no deployment is built into them.
AwsPartition = Annotated[
    str, Field(pattern=r"^aws(?:-[a-z]+)*$"), AfterValidator(_clean)
]
AgentCoreName = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
    AfterValidator(_clean),
]
AgentRuntimeArn = Annotated[
    str,
    Field(
        max_length=512,
        pattern=(
            rf"^arn:aws(?:-[a-z]+)*:bedrock-agentcore:{_REGION}:\d{{12}}"
            r":runtime/[A-Za-z0-9_-]{1,64}$"
        ),
    ),
    AfterValidator(_clean),
]
ModelIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$"),
    AfterValidator(_clean),
]
# CloudWatch permits a wide character set in a log group or stream name, so
# these are constrained to what a name may contain rather than to a pattern
# this project invents. A `:` or `*` in a stream name is refused because
# CloudWatch refuses it too.
LogGroupName = Annotated[
    str,
    Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_./#-]+$"),
    AfterValidator(_clean),
]
LogStreamName = Annotated[
    str,
    Field(min_length=1, max_length=512, pattern=r"^[^:*\s]+$"),
    AfterValidator(_clean),
]
ServiceName = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$"),
    AfterValidator(_clean),
]


class CloudResourceConfig(Contract):
    """AWS resource identifiers and pinned versions. No credential, ever."""

    region: AwsRegion
    account_id: AwsAccountId
    run_table_name: TableName
    receipt_table_name: TableName
    artifact_bucket: BucketName
    controller_runtime_arn: ControllerRuntimeArn
    decider_api_id: ApiGatewayId
    decider_vpc_endpoint_id: VpcEndpointId
    dataset_version: SafeIdentifier
    model_revision: SafeIdentifier
    policy_hash: Digest
    calibration_hash: Digest
    catalog_version: SafeIdentifier = CATALOG_VERSION
    tool_contract_version: SafeIdentifier = TOOL_CONTRACT_VERSION
    data_schema_version: SafeIdentifier = DATA_SCHEMA_VERSION
    receipt_schema_version: Literal["1"] = "1"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.run_table_name == self.receipt_table_name:
            raise ValueError("run state and receipt index use distinct tables")
        _, _, _, region, account, _ = self.controller_runtime_arn.split(":", 5)
        if (region, account) != (self.region, self.account_id):
            raise ValueError("controller ARN does not match the declared account")
        return self

    @property
    def decider_api_arn(self) -> str:
        """The `execute-api` invoke resource for the private Decider API."""
        return (
            f"arn:aws:execute-api:{self.region}:{self.account_id}:"
            f"{self.decider_api_id}/*/*/*"
        )

    @property
    def run_table_arn(self) -> str:
        return (
            f"arn:aws:dynamodb:{self.region}:{self.account_id}:"
            f"table/{self.run_table_name}"
        )

    @property
    def receipt_table_arn(self) -> str:
        return (
            f"arn:aws:dynamodb:{self.region}:{self.account_id}:"
            f"table/{self.receipt_table_name}"
        )

    @property
    def artifact_bucket_arn(self) -> str:
        return f"arn:aws:s3:::{self.artifact_bucket}"
