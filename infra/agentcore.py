"""The private AgentCore Runtime resource and its endpoint.

The resource types and every property set below come from the installed
`aws_cdk.aws_bedrockagentcore` L1 constructs, whose own `cloudformationResource`
metadata declares `AWS::BedrockAgentCore::Runtime` and
`AWS::BedrockAgentCore::RuntimeEndpoint`. Nothing here is a generic
`CfnResource` carrying a guessed type name, and nothing here spells a property
the installed props class does not declare;
`tests/cloud_platform/test_agentcore_deployment.py` asserts both against the
installed package.

The L1 constructs are used rather than the L2 `AgentCoreRuntime`, for one
reason: the L1 creates exactly the resources it is told to and no execution role
of its own, so the stack keeps exactly the five roles of `docs/spec/design.md`
section 15 and the role matrix diff stays exact.

Three security properties are structural rather than configured.

No `authorizer_configuration` is set. The installed control-plane model declares
`AuthorizerConfiguration` as a union whose only member is `customJWTAuthorizer`,
so the contract offers no unauthenticated, public, or API-key inbound path and
omitting the authorizer selects the service's own AWS SigV4 authorization. A
synthesized runtime carrying an authorizer would be a weakening of that, which
is why none is written and why a test asserts the property is absent.

The invoke grant is action-level and resource-level. Exactly one role holds
exactly one action, `bedrock-agentcore:InvokeAgentRuntime`, against exactly the
runtime this stack creates and the one endpoint it creates. No wildcard resource
and no service-wide action is granted, so proving who is calling is paired with
bounding what they may call.

Telemetry writes are confined to the configured destination. The log group and
stream are deployment inputs, so the grant names them rather than a prefix: when
both are supplied the resource is the exact stream, and when only the group is
supplied the grant stays inside that group. An unconfigured destination produces
no grant at all.

Nothing here deploys. Synthesis is offline and credential-free, and a resource
in a template is not a resource in an account: PF6 and PF8 stay Pending.
"""

from dataclasses import dataclass

from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from constructs import Construct

from unstuck.cloud.deployment import INVOKE_ACTION, AgentCoreRuntimeInputs
from unstuck.cloud.telemetry import TelemetryRuntimeConfig

#: The CloudFormation types, taken from the installed L1 constructs rather than
#: written out, so a rename in the library cannot drift from this module.
AGENTCORE_RUNTIME_TYPE = agentcore.CfnRuntime.CFN_RESOURCE_TYPE_NAME
AGENTCORE_ENDPOINT_TYPE = agentcore.CfnRuntimeEndpoint.CFN_RESOURCE_TYPE_NAME

#: Statement identifiers, named so a security assertion can find exactly the
#: statement it means rather than matching on an action list.
OPERATOR_INVOKE_SID = "InvokeTheControllerRuntimeAndItsEndpointOnly"
CONTROLLER_LOG_SID = "TelemetryWritesToTheConfiguredDestinationOnly"

#: The endpoint ARN suffix, as `AgentRuntimeEndpointArn` in the installed
#: control-plane model spells it: the runtime ARN, then `runtime-endpoint`, then
#: the endpoint name.
ENDPOINT_ARN_INFIX = "runtime-endpoint"


@dataclass(frozen=True)
class AgentCoreRuntime:
    """The runtime and the one endpoint that points at it."""

    runtime: agentcore.CfnRuntime
    endpoint: agentcore.CfnRuntimeEndpoint


def _network(
    inputs: AgentCoreRuntimeInputs,
) -> agentcore.CfnRuntime.NetworkConfigurationProperty:
    if inputs.vpc is None:
        return agentcore.CfnRuntime.NetworkConfigurationProperty(
            network_mode=inputs.network_mode
        )
    return agentcore.CfnRuntime.NetworkConfigurationProperty(
        network_mode=inputs.network_mode,
        network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
            security_groups=list(inputs.vpc.security_groups),
            subnets=list(inputs.vpc.subnets),
        ),
    )


def _lifecycle(
    inputs: AgentCoreRuntimeInputs,
) -> agentcore.CfnRuntime.LifecycleConfigurationProperty | None:
    if inputs.idle_session_timeout_seconds is None and (
        inputs.max_lifetime_seconds is None
    ):
        return None
    return agentcore.CfnRuntime.LifecycleConfigurationProperty(
        idle_runtime_session_timeout=inputs.idle_session_timeout_seconds,
        max_lifetime=inputs.max_lifetime_seconds,
    )


def _telemetry_resource(
    telemetry: TelemetryRuntimeConfig | None, *, region: str, account_id: str
) -> str | None:
    """The one log destination telemetry may write to, or nothing.

    A configured group and stream resolve to the exact stream. A group on its
    own stays inside that group, because a stream name the owner has not
    supplied cannot be named, and a group-wide stream scope is still bounded by
    the configured group. Nothing configured produces no grant.
    """
    if telemetry is None or telemetry.log_group is None:
        return None
    base = f"arn:aws:logs:{region}:{account_id}:log-group:{telemetry.log_group}"
    stream = "*" if telemetry.log_stream is None else telemetry.log_stream
    return f"{base}:log-stream:{stream}"


def add_agentcore_runtime(
    scope: Construct,
    *,
    inputs: AgentCoreRuntimeInputs,
    region: str,
    account_id: str,
    controller_role: iam.Role,
    operator_role: iam.Role,
    environment: dict[str, str],
    telemetry: TelemetryRuntimeConfig | None = None,
) -> AgentCoreRuntime:
    """Synthesize the runtime, its endpoint, and the two grants they imply."""
    tags = inputs.tag_map
    runtime = agentcore.CfnRuntime(
        scope,
        "ControllerAgentRuntime",
        agent_runtime_name=inputs.runtime_name,
        agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
            container_configuration=(
                agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=inputs.image_uri
                )
            )
        ),
        role_arn=controller_role.role_arn,
        # No authorizer_configuration: the contract's only alternative is a
        # custom JWT authorizer, so absence is the service's own SigV4.
        description="R1 controller runtime; private, SigV4 authorized",
        environment_variables=dict(environment),
        lifecycle_configuration=_lifecycle(inputs),
        network_configuration=_network(inputs),
        protocol_configuration=inputs.server_protocol,
        request_header_configuration=(
            agentcore.CfnRuntime.RequestHeaderConfigurationProperty(
                request_header_allowlist=list(inputs.request_header_allowlist)
            )
        ),
        tags=tags,
    )
    endpoint = agentcore.CfnRuntimeEndpoint(
        scope,
        "ControllerAgentRuntimeEndpoint",
        agent_runtime_id=runtime.attr_agent_runtime_id,
        name=inputs.endpoint_name,
        description="The one endpoint the operator may invoke",
        tags=tags,
    )
    endpoint.add_dependency(runtime)
    runtime_arn = runtime.attr_agent_runtime_arn
    operator_role.add_to_policy(
        iam.PolicyStatement(
            sid=OPERATOR_INVOKE_SID,
            actions=[INVOKE_ACTION],
            resources=[
                runtime_arn,
                f"{runtime_arn}/{ENDPOINT_ARN_INFIX}/{inputs.endpoint_name}",
            ],
        )
    )
    destination = _telemetry_resource(telemetry, region=region, account_id=account_id)
    if destination is not None:
        controller_role.add_to_policy(
            iam.PolicyStatement(
                sid=CONTROLLER_LOG_SID,
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[destination],
            )
        )
    return AgentCoreRuntime(runtime=runtime, endpoint=endpoint)


__all__ = [
    "AGENTCORE_ENDPOINT_TYPE",
    "AGENTCORE_RUNTIME_TYPE",
    "CONTROLLER_LOG_SID",
    "ENDPOINT_ARN_INFIX",
    "OPERATOR_INVOKE_SID",
    "AgentCoreRuntime",
    "add_agentcore_runtime",
]
