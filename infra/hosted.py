"""The hosted provider path: a private SageMaker endpoint and Bedrock access.

Synthesis only. This module builds CloudFormation resources in process and
performs no AWS API call: it creates, updates and deletes nothing, pushes no
image, uploads no artifact, reads no ambient credential or profile, resolves no
CloudFormation context lookup, and spends nothing. A resource in a template is
not a resource in an account, so reachability, denial and cost all stay Pending
their named gates.

No CI or pipeline resource is created here. The deployment is the owner's
manual, approved step, so there is no build project, no pipeline, no artifact
store, and no deploy role of its own.

Five security properties are structural rather than configured.

The endpoint is private and reached through an interface endpoint. The VPC is
the owner's existing one, named by identifier, and the two interface endpoints
are attached to the private subnets and security groups the owner supplied. No
subnet range appears in any rule and no public subnet, NAT gateway or internet
gateway is created or referenced. Both endpoints carry a resource policy that
admits exactly one principal and exactly one action, so network placement
inside those subnets grants nothing by itself.

The model is reached by exactly one action. The controller role holds
`sagemaker:InvokeEndpoint` against exactly the endpoint this stack creates, and
`bedrock:InvokeModel` against exactly the narration model the deployment named.
No wildcard resource and no service-wide action is granted, so proving who is
calling is paired with bounding what they may call.

The execution role reads the artifact and nothing else. It may pull exactly the
digest-pinned image from exactly the repository it came from, read exactly the
model prefix the artifact lives under, and write logs to exactly the SageMaker
endpoint log group. Everything the model host does not serve is explicitly
denied, including the run tables, the receipt table, the artifact prefixes it
has no business reading, the evaluator-truth bucket, Bedrock, Secrets Manager,
and role passing.

The network path is one way. The controller's security group may reach the
interface endpoints on 443 and the endpoints accept from the controller's
security identity, not from a CIDR. Nothing opens an inbound rule from
`0.0.0.0/0` and nothing publishes a port.

The endpoint resource is tagged for retention. Every resource carries
`auto-delete=no` and `project=unstuck`, so a sweeper leaves it and an owner can
find it.
"""

from dataclasses import dataclass

from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sagemaker as sagemaker
from constructs import Construct

from unstuck.cloud.deployment import HostedProviderInputs

#: The data-plane actions, one each. These are the only actions any role in
#: this topology holds against the hosted providers.
INVOKE_ENDPOINT_ACTION = "sagemaker:InvokeEndpoint"
INVOKE_MODEL_ACTION = "bedrock:InvokeModel"

#: The interface endpoint service short names, taken from the installed
#: `aws_ec2.InterfaceVpcEndpointAwsService` constants rather than written out,
#: so a rename in the library cannot drift from this module.
SAGEMAKER_RUNTIME_SHORT_NAME = (
    ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_RUNTIME.short_name
)
BEDROCK_RUNTIME_SHORT_NAME = (
    ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME.short_name
)

#: Statement identifiers, named so a security assertion can find exactly the
#: statement it means rather than matching on an action list.
CONTROLLER_INVOKE_ENDPOINT_SID = "InvokeTheHostedDeciderEndpointOnly"
CONTROLLER_INVOKE_MODEL_SID = "InvokeTheNarrationModelOnly"
SERVING_IMAGE_SID = "PullExactlyTheServingImage"
SERVING_ARTIFACT_SID = "ReadExactlyTheModelPrefix"
SERVING_LOGS_SID = "WriteEndpointLogsOnly"
SERVING_DENY_SID = "DenyEverythingTheModelHostDoesNotServe"

#: The one production variant name. One variant, one instance, one checkpoint.
VARIANT_NAME = "AllTraffic"

#: The interface endpoint port. HTTPS and nothing else.
ENDPOINT_PORT = 443


@dataclass(frozen=True)
class HostedProviders:
    """Every hosted-provider construct the rest of the topology binds to."""

    execution_role: iam.Role
    model: sagemaker.CfnModel
    endpoint_config: sagemaker.CfnEndpointConfig
    endpoint: sagemaker.CfnEndpoint
    sagemaker_endpoint: ec2.CfnVPCEndpoint
    bedrock_endpoint: ec2.CfnVPCEndpoint


def _service_name(region: str, short_name: str) -> str:
    """The interface endpoint service name for one Region."""
    return f"com.amazonaws.{region}.{short_name}"


def _endpoint_policy(
    principal_arn: str, actions: list[str], resources: list[str]
) -> iam.PolicyDocument:
    """Admit one principal and one action set. Deny everything else.

    An interface endpoint with no policy admits every call that reaches it from
    the attached subnets, which would make network placement the only boundary.
    This names the principal instead, so a workload that gains placement in
    those subnets gains nothing.
    """
    return iam.PolicyDocument(
        statements=[
            iam.PolicyStatement(
                sid="AllowTheControllerRoleOnly",
                effect=iam.Effect.ALLOW,
                principals=[iam.ArnPrincipal(principal_arn)],
                actions=actions,
                resources=resources,
            ),
            iam.PolicyStatement(
                sid="DenyAnyOtherPrincipal",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["*"],
                resources=["*"],
                conditions={"StringNotEquals": {"aws:PrincipalArn": principal_arn}},
            ),
        ]
    )


def add_hosted_providers(
    scope: Construct,
    *,
    inputs: HostedProviderInputs,
    region: str,
    account_id: str,
    controller_role: iam.Role,
) -> HostedProviders:
    """Synthesize the private endpoint, the two interface endpoints, and the grants."""
    tags = [
        {"key": key, "value": value} for key, value in sorted(inputs.tag_map.items())
    ]
    endpoint_arn = (
        f"arn:aws:sagemaker:{region}:{account_id}:endpoint/{inputs.endpoint_name}"
    )

    execution_role = iam.Role(
        scope,
        "DeciderServingRole",
        role_name=inputs.execution_role_name,
        description="R8 hosted Decider serving role; artifact and logs only",
        assumed_by=iam.ServicePrincipal(
            "sagemaker.amazonaws.com",
            conditions={"StringEquals": {"aws:SourceAccount": account_id}},
        ),
    )
    _grant_serving(
        execution_role,
        inputs=inputs,
        region=region,
        account_id=account_id,
    )

    model = sagemaker.CfnModel(
        scope,
        "DeciderServingModel",
        model_name=inputs.model_name,
        execution_role_arn=execution_role.role_arn,
        primary_container=sagemaker.CfnModel.ContainerDefinitionProperty(
            image=inputs.image_uri,
            model_data_url=inputs.model_data_url,
            # The deployment inputs the server validates at startup. None is a
            # credential and none is a guess: the revision is the immutable
            # checkpoint identity the artifact declares, the device is the
            # owner's declared one, and the two offline flags make a
            # serving-time fetch impossible.
            environment={
                "UNSTUCK_SERVING_MODEL_DIR": "/opt/ml/model",
                "UNSTUCK_SERVING_MODEL_REVISION": inputs.model_revision,
                "UNSTUCK_SERVING_DEVICE": inputs.device,
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
            },
        ),
        # The model container runs inside the owner's existing private subnets,
        # so it has no route to the internet of its own.
        vpc_config=sagemaker.CfnModel.VpcConfigProperty(
            security_group_ids=list(inputs.network.security_groups),
            subnets=list(inputs.network.subnets),
        ),
        enable_network_isolation=True,
        tags=tags,
    )

    endpoint_config = sagemaker.CfnEndpointConfig(
        scope,
        "DeciderServingEndpointConfig",
        endpoint_config_name=inputs.endpoint_config_name,
        production_variants=[
            sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                variant_name=VARIANT_NAME,
                model_name=inputs.model_name,
                initial_instance_count=inputs.instance_count,
                instance_type=inputs.instance_type,
                initial_variant_weight=1,
            )
        ],
        tags=tags,
    )
    endpoint_config.add_dependency(model)

    endpoint = sagemaker.CfnEndpoint(
        scope,
        "DeciderServingEndpoint",
        endpoint_name=inputs.endpoint_name,
        endpoint_config_name=inputs.endpoint_config_name,
        tags=tags,
    )
    endpoint.add_dependency(endpoint_config)

    sagemaker_endpoint = ec2.CfnVPCEndpoint(
        scope,
        "SageMakerRuntimeVpcEndpoint",
        service_name=_service_name(region, SAGEMAKER_RUNTIME_SHORT_NAME),
        vpc_id=inputs.network.vpc_id,
        vpc_endpoint_type="Interface",
        private_dns_enabled=True,
        subnet_ids=list(inputs.network.subnets),
        security_group_ids=list(inputs.network.security_groups),
        policy_document=_endpoint_policy(
            controller_role.role_arn, [INVOKE_ENDPOINT_ACTION], [endpoint_arn]
        ).to_json(),
    )
    bedrock_endpoint = ec2.CfnVPCEndpoint(
        scope,
        "BedrockRuntimeVpcEndpoint",
        service_name=_service_name(region, BEDROCK_RUNTIME_SHORT_NAME),
        vpc_id=inputs.network.vpc_id,
        vpc_endpoint_type="Interface",
        private_dns_enabled=True,
        subnet_ids=list(inputs.network.subnets),
        security_group_ids=list(inputs.network.security_groups),
        policy_document=_endpoint_policy(
            controller_role.role_arn,
            [INVOKE_MODEL_ACTION],
            [inputs.narration_model_arn],
        ).to_json(),
    )

    controller_role.add_to_policy(
        iam.PolicyStatement(
            sid=CONTROLLER_INVOKE_ENDPOINT_SID,
            actions=[INVOKE_ENDPOINT_ACTION],
            resources=[endpoint_arn],
        )
    )
    controller_role.add_to_policy(
        iam.PolicyStatement(
            sid=CONTROLLER_INVOKE_MODEL_SID,
            actions=[INVOKE_MODEL_ACTION],
            resources=[inputs.narration_model_arn],
        )
    )
    return HostedProviders(
        execution_role=execution_role,
        model=model,
        endpoint_config=endpoint_config,
        endpoint=endpoint,
        sagemaker_endpoint=sagemaker_endpoint,
        bedrock_endpoint=bedrock_endpoint,
    )


def _grant_serving(
    role: iam.Role,
    *,
    inputs: HostedProviderInputs,
    region: str,
    account_id: str,
) -> None:
    """The image, the model prefix, and the endpoint log group. Nothing else."""
    repository_arn = (
        f"arn:aws:ecr:{inputs.image_region}:{inputs.image_account_id}:"
        f"repository/{inputs.image_repository}"
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid=SERVING_IMAGE_SID,
            actions=[
                "ecr:BatchCheckLayerAvailability",
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
            ],
            resources=[repository_arn],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="AuthorizeTheRegistry",
            actions=["ecr:GetAuthorizationToken"],
            # `GetAuthorizationToken` takes no resource in the service's own
            # model, so a resource scope here would deny it outright. It
            # returns a registry token and reads no image.
            resources=["*"],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid=SERVING_ARTIFACT_SID,
            actions=["s3:GetObject"],
            resources=[
                f"arn:aws:s3:::{inputs.model_bucket}/{inputs.model_prefix}*",
            ],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid=SERVING_LOGS_SID,
            actions=[
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogStreams",
            ],
            resources=[
                f"arn:aws:logs:{region}:{account_id}:log-group:"
                f"/aws/sagemaker/Endpoints/{inputs.endpoint_name}:*"
            ],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid=SERVING_DENY_SID,
            effect=iam.Effect.DENY,
            actions=[
                "dynamodb:*",
                "bedrock:*",
                "bedrock-agentcore:*",
                "sagemaker:InvokeEndpoint",
                "secretsmanager:*",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "sts:AssumeRole",
                "iam:PassRole",
                "s3:PutObject",
                "s3:DeleteObject",
            ],
            resources=["*"],
        )
    )


__all__ = [
    "BEDROCK_RUNTIME_SHORT_NAME",
    "CONTROLLER_INVOKE_ENDPOINT_SID",
    "CONTROLLER_INVOKE_MODEL_SID",
    "ENDPOINT_PORT",
    "INVOKE_ENDPOINT_ACTION",
    "INVOKE_MODEL_ACTION",
    "SAGEMAKER_RUNTIME_SHORT_NAME",
    "SERVING_ARTIFACT_SID",
    "SERVING_DENY_SID",
    "SERVING_IMAGE_SID",
    "SERVING_LOGS_SID",
    "VARIANT_NAME",
    "HostedProviders",
    "add_hosted_providers",
]
