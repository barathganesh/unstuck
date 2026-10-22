"""The single Unstuck stack.

Storage, the private Decider path, the controller image repository, the five IAM
roles, and optionally the AgentCore Runtime itself are synthesized into one
stack so that one template carries the whole topology and a security assertion
can prove a property across all of it in one pass.

The AgentCore Runtime is wired only when a caller supplies `agentcore`. Its
CloudFormation type and properties are verified against the installed
`aws_cdk.aws_bedrockagentcore` L1 constructs and the installed `botocore`
service models, so the resource itself is no longer a guess. What it still needs
is a digest-pinned image published to the deployment account, which is a
deployment input rather than something synthesis can invent; a plain synthesis
therefore carries the topology the runtime is bounded by, and
`unstuck.cloud.deployment` adds the runtime once that input exists. Reachability
and denial remain unverified either way: PF6 and PF8 stay Pending.
"""

from collections.abc import Mapping

from aws_cdk import Environment, RemovalPolicy, Stack, Tags
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from constructs import Construct

from infra.agentcore import AgentCoreRuntime, add_agentcore_runtime
from infra.decider import DECIDER_PATH, DeciderPath, add_decider_path
from infra.hosted import HostedProviders, add_hosted_providers
from infra.roles import RoleSet, add_roles
from infra.storage import StorageResources, add_storage, restrict_holdout_to_roles
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.deployment import AgentCoreRuntimeInputs, HostedProviderInputs
from unstuck.cloud.telemetry import TelemetryRuntimeConfig

#: The default stack name. Synthesis may supply another, and every scope
#: derived from the stack follows the name that was actually synthesized.
STACK_NAME = "UnstuckPrivateTopology"


class UnstuckStack(Stack):
    """Every resource the private topology needs, and nothing it does not."""

    def __init__(
        self,
        scope: Construct,
        identifier: str,
        *,
        config: CloudResourceConfig,
        holdout_bucket: str,
        vpc_cidr: str,
        decider_ami_id: str,
        decider_instance_type: str,
        main_model_arn: str,
        baseline_model_arn: str,
        operator_principal_arn: str,
        evaluator_principal_arn: str,
        deployer_principal_arn: str,
        cloudformation_execution_role_arn: str | None = None,
        agentcore: AgentCoreRuntimeInputs | None = None,
        agentcore_environment: Mapping[str, str] | None = None,
        hosted: HostedProviderInputs | None = None,
        telemetry: TelemetryRuntimeConfig | None = None,
        env: Environment | None = None,
    ) -> None:
        self._zones = [f"{config.region}a", f"{config.region}b"]
        super().__init__(scope, identifier, env=env)
        Tags.of(self).add("unstuck:mode", "offline-synthesis")
        Tags.of(self).add("unstuck:evidence", "pending-pf6")
        # The retention tag the deployment inputs carry is applied as a stack
        # tag as well as on the AgentCore resources, so every resource in the
        # template carries it rather than only the two that name it directly.
        for tag in agentcore.tags if agentcore is not None else ():
            Tags.of(self).add(tag.key, tag.value)
        for tag in hosted.tags if hosted is not None else ():
            Tags.of(self).add(tag.key, tag.value)

        self.repository = ecr.Repository(
            self,
            "ControllerImageRepository",
            repository_name="unstuck-controller",
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.storage: StorageResources = add_storage(
            self,
            run_table_name=config.run_table_name,
            receipt_table_name=config.receipt_table_name,
            artifact_bucket=config.artifact_bucket,
            holdout_bucket=holdout_bucket,
        )
        self.roles: RoleSet = add_roles(
            self,
            account_id=config.account_id,
            region=config.region,
            runs=self.storage.runs,
            receipts=self.storage.receipts,
            artifacts=self.storage.artifacts,
            holdout=self.storage.holdout,
            repository=self.repository,
            main_model_arn=main_model_arn,
            baseline_model_arn=baseline_model_arn,
            controller_runtime_arn=config.controller_runtime_arn,
            # The deployer's CloudFormation scope has to name the stack this
            # synthesis actually produces. A constant here would allow only the
            # default name and deny the real stack through `NotResource`.
            stack_name=self.stack_name,
            operator_principal_arn=operator_principal_arn,
            evaluator_principal_arn=evaluator_principal_arn,
            deployer_principal_arn=deployer_principal_arn,
            cloudformation_execution_role_arn=cloudformation_execution_role_arn,
        )
        # The allow-set has to name the resulting role ARNs, so the holdout
        # denial is attached here rather than inside `add_storage`.
        restrict_holdout_to_roles(
            self.storage.holdout,
            (
                self.roles.deployer.role_arn,
                self.roles.evaluator.role_arn,
                *(
                    (cloudformation_execution_role_arn,)
                    if cloudformation_execution_role_arn is not None
                    else ()
                ),
            ),
        )
        self.decider: DeciderPath = add_decider_path(
            self,
            vpc_cidr=vpc_cidr,
            region=config.region,
            decider_ami_id=decider_ami_id,
            decider_instance_type=decider_instance_type,
            host_role=self.roles.decider_host,
            controller_role_arn=self.roles.controller.role_arn,
        )
        # The controller may invoke exactly one method, and only when the call
        # arrives through the private endpoint the resource policy names.
        self.roles.controller.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeThePrivateDeciderApiThroughTheEndpointOnly",
                actions=["execute-api:Invoke"],
                resources=[
                    f"arn:aws:execute-api:{config.region}:{config.account_id}:"
                    f"{self.decider.api.rest_api_id}/*/POST{DECIDER_PATH}"
                ],
                conditions={
                    "StringEquals": {
                        "aws:SourceVpce": self.decider.endpoint.vpc_endpoint_id
                    }
                },
            )
        )
        self.agentcore: AgentCoreRuntime | None = None
        if agentcore is not None:
            self.agentcore = add_agentcore_runtime(
                self,
                inputs=agentcore,
                region=config.region,
                account_id=config.account_id,
                controller_role=self.roles.controller,
                operator_role=self.roles.operator,
                environment=dict(agentcore_environment or {}),
                telemetry=telemetry,
            )
        # The hosted provider path, wired only when a caller supplies its
        # inputs. A plain synthesis therefore carries exactly the five design
        # roles and no hosted resource, and the serving role appears only in a
        # deployment that actually hosts a checkpoint.
        self.hosted: HostedProviders | None = None
        if hosted is not None:
            self.hosted = add_hosted_providers(
                self,
                inputs=hosted,
                region=config.region,
                account_id=config.account_id,
                controller_role=self.roles.controller,
            )
            # Defence in depth on the one bucket the serving role must never
            # reach. Its only S3 allow is the model prefix, so this adds a
            # denial that survives a later widening of that allow.
            self.hosted.execution_role.add_to_policy(
                iam.PolicyStatement(
                    sid="DenyEvaluatorTruthBucket",
                    effect=iam.Effect.DENY,
                    actions=["s3:*"],
                    resources=[
                        self.storage.holdout.bucket_arn,
                        self.storage.holdout.arn_for_objects("*"),
                    ],
                )
            )

    @property
    def availability_zones(self) -> list[str]:
        """Name the zones instead of discovering them.

        The default implementation asks the account's availability-zone context
        provider, which records a lookup CDK would resolve against AWS. Naming
        the zones keeps synthesis entirely offline.
        """
        return list(self._zones)
