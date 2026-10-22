"""The IAM role matrix of `docs/spec/design.md` section 15.

Five roles exist and no more: R1 controller runtime, R4 Decider host, R5
deployer, R6 local operator, R7 evaluator. R2 and R3 are retired identifiers,
not roles.

Static prefix scoping is not per-run isolation; the controller lease and
conditional writes carry run identity. Every grant below is a planned grant
asserted against the design matrix, never a verified one. The exact AgentCore
service actions and trust principal remain Pending PF6.

Two boundaries are resource-scoped rather than action-scoped, because the
action name alone does not bound them. CloudFormation always runs a stack with
that stack's own associated service role, and any principal able to create and
execute a change set on a stack borrows that role whether or not it holds
`iam:PassRole`. R5 is therefore confined to the Unstuck stack and its change
sets by resource, and an explicit `NotResource` denial closes every other
stack. Role passing is likewise confined to the exact intended role and to the
one service that role is for, so a template cannot redirect a pass.
"""

from dataclasses import dataclass

from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from unstuck.cloud.keys import (
    PUBLIC_FIXTURE_PREFIX,
    RECEIPT_EVIDENCE_PREFIX,
    RUN_PREFIX,
)

AGENTCORE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"
EC2_PRINCIPAL = "ec2.amazonaws.com"
CLOUDFORMATION_PRINCIPAL = "cloudformation.amazonaws.com"
MODEL_PREFIX = "models/"
EXPERIMENT_PREFIX = "experiments/"
DECIDER_CHECKPOINT_PREFIX = "models/decider/"

#: The fixed role names this stack creates, keyed by design.md section 15 row.
#: They are the only names the stack owns, so none of them may ever arrive as
#: an external trust principal: a role cannot be the identity trusted to assume
#: itself, and naming one here would widen the boundary to whoever can already
#: assume it. IAM role names are not distinguished by case, so comparisons
#: against this set are case-insensitive.
STACK_OWNED_ROLES: dict[str, str] = {
    "R1": "unstuck-controller-runtime",
    "R4": "unstuck-decider-host",
    "R5": "unstuck-deployer",
    "R6": "unstuck-local-operator",
    "R7": "unstuck-evaluator",
}

#: The same names, normalized for case-insensitive comparison.
STACK_OWNED_ROLE_NAMES = frozenset(name.lower() for name in STACK_OWNED_ROLES.values())

#: The change-set name prefix an owner-approved deploy uses. Change-set ARNs
#: carry the change-set name rather than the stack name, so the deployer's
#: change-set resources are bounded by this prefix and by the explicit denial
#: of every stack but the Unstuck one.
CHANGE_SET_NAME_PREFIX = "cdk-deploy-change-set"

#: Read-only stack inspection. No condition key is present on these calls, so
#: an execution-role condition here would deny them outright.
STACK_READ_ACTIONS = (
    "cloudformation:DescribeChangeSet",
    "cloudformation:DescribeStackEvents",
    "cloudformation:DescribeStacks",
    "cloudformation:GetTemplate",
)
#: The two calls that change the stack. `cloudformation:RoleArn` is present on
#: both, so a pinned execution role is enforceable here.
STACK_CHANGE_ACTIONS = (
    "cloudformation:CreateChangeSet",
    "cloudformation:ExecuteChangeSet",
)


@dataclass(frozen=True)
class RoleSet:
    """The five roles, in design.md section 15 order."""

    controller: iam.Role
    decider_host: iam.Role
    deployer: iam.Role
    operator: iam.Role
    evaluator: iam.Role


def _objects(bucket: s3.Bucket, prefix: str) -> str:
    return bucket.arn_for_objects(f"{prefix}*")


def _deny_holdout(bucket: s3.Bucket) -> iam.PolicyStatement:
    return iam.PolicyStatement(
        sid="DenyEvaluatorTruthBucket",
        effect=iam.Effect.DENY,
        actions=["s3:*"],
        resources=[bucket.bucket_arn, bucket.arn_for_objects("*")],
    )


def add_roles(
    scope: Construct,
    *,
    account_id: str,
    region: str,
    runs: dynamodb.Table,
    receipts: dynamodb.Table,
    artifacts: s3.Bucket,
    holdout: s3.Bucket,
    repository: ecr.Repository,
    main_model_arn: str,
    baseline_model_arn: str,
    controller_runtime_arn: str,
    stack_name: str,
    operator_principal_arn: str,
    evaluator_principal_arn: str,
    deployer_principal_arn: str,
    cloudformation_execution_role_arn: str | None = None,
) -> RoleSet:
    log_group = f"arn:aws:logs:{region}:{account_id}:log-group:/aws/unstuck/*"
    controller = iam.Role(
        scope,
        "ControllerRuntimeRole",
        role_name=STACK_OWNED_ROLES["R1"],
        description="R1 controller runtime; AgentCore trust Pending PF6",
        assumed_by=iam.ServicePrincipal(
            AGENTCORE_PRINCIPAL,
            conditions={"StringEquals": {"aws:SourceAccount": account_id}},
        ),
    )
    decider_host = iam.Role(
        scope,
        "DeciderHostRole",
        role_name=STACK_OWNED_ROLES["R4"],
        description="R4 Decider host; checkpoint reads and scoped logs only",
        assumed_by=iam.ServicePrincipal(EC2_PRINCIPAL),
    )
    deployer = iam.Role(
        scope,
        "DeployerRole",
        role_name=STACK_OWNED_ROLES["R5"],
        description="R5 deployer; never assumed by the runtime",
        assumed_by=iam.ArnPrincipal(deployer_principal_arn),
    )
    operator = iam.Role(
        scope,
        "LocalOperatorRole",
        role_name=STACK_OWNED_ROLES["R6"],
        description="R6 local operator; invoke and read only",
        assumed_by=iam.ArnPrincipal(operator_principal_arn),
    )
    evaluator = iam.Role(
        scope,
        "EvaluatorRole",
        role_name=STACK_OWNED_ROLES["R7"],
        description="R7 evaluator; scores after execution in isolation",
        assumed_by=iam.ArnPrincipal(evaluator_principal_arn),
    )
    _grant_controller(
        controller,
        runs=runs,
        receipts=receipts,
        artifacts=artifacts,
        holdout=holdout,
        repository=repository,
        main_model_arn=main_model_arn,
        baseline_model_arn=baseline_model_arn,
        log_group=log_group,
    )
    _grant_decider_host(
        decider_host, artifacts=artifacts, holdout=holdout, log_group=log_group
    )
    _grant_deployer(
        deployer,
        account_id=account_id,
        region=region,
        stack_name=stack_name,
        controller_arn=controller.role_arn,
        decider_host_arn=decider_host.role_arn,
        execution_role_arn=cloudformation_execution_role_arn,
    )
    _grant_operator(
        operator,
        runs=runs,
        receipts=receipts,
        artifacts=artifacts,
        holdout=holdout,
        controller_runtime_arn=controller_runtime_arn,
    )
    _grant_evaluator(evaluator, artifacts=artifacts, holdout=holdout, runs=runs)
    return RoleSet(
        controller=controller,
        decider_host=decider_host,
        deployer=deployer,
        operator=operator,
        evaluator=evaluator,
    )


def _grant_controller(
    role: iam.Role,
    *,
    runs: dynamodb.Table,
    receipts: dynamodb.Table,
    artifacts: s3.Bucket,
    holdout: s3.Bucket,
    repository: ecr.Repository,
    main_model_arn: str,
    baseline_model_arn: str,
    log_group: str,
) -> None:
    role.add_to_policy(
        iam.PolicyStatement(
            sid="RunStateWithScopedLeadingKeys",
            actions=[
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:Query",
            ],
            resources=[runs.table_arn],
            conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["RUN#*"]}},
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="ReceiptIndexWithScopedLeadingKeys",
            actions=[
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:Query",
            ],
            resources=[receipts.table_arn, f"{receipts.table_arn}/index/*"],
            conditions={
                "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["PROBLEM#*"]}
            },
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="ScopedArtifactWrites",
            actions=["s3:GetObject", "s3:PutObject"],
            resources=[
                _objects(artifacts, RUN_PREFIX),
                _objects(artifacts, RECEIPT_EVIDENCE_PREFIX),
            ],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="PublicFixtureReads",
            actions=["s3:GetObject"],
            resources=[_objects(artifacts, PUBLIC_FIXTURE_PREFIX)],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="PinnedBedrockModels",
            actions=["bedrock:InvokeModel"],
            resources=[main_model_arn, baseline_model_arn],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="ScopedLogs",
            actions=["logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[log_group],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="ControllerImagePull",
            actions=[
                "ecr:BatchCheckLayerAvailability",
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
            ],
            resources=[repository.repository_arn],
        )
    )
    role.add_to_policy(_deny_holdout(holdout))
    role.add_to_policy(
        iam.PolicyStatement(
            sid="DenyCheckpointAndExperimentPrefixes",
            effect=iam.Effect.DENY,
            actions=["s3:*"],
            resources=[
                _objects(artifacts, MODEL_PREFIX),
                _objects(artifacts, EXPERIMENT_PREFIX),
            ],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="DenyRolePassing",
            effect=iam.Effect.DENY,
            actions=["iam:PassRole"],
            resources=["*"],
        )
    )


def _grant_decider_host(
    role: iam.Role, *, artifacts: s3.Bucket, holdout: s3.Bucket, log_group: str
) -> None:
    role.add_to_policy(
        iam.PolicyStatement(
            sid="StagedCheckpointReads",
            actions=["s3:GetObject"],
            resources=[_objects(artifacts, DECIDER_CHECKPOINT_PREFIX)],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="ScopedLogs",
            actions=["logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[log_group],
        )
    )
    role.add_to_policy(_deny_holdout(holdout))
    role.add_to_policy(
        iam.PolicyStatement(
            sid="DenyRunAndReceiptData",
            effect=iam.Effect.DENY,
            actions=["s3:*"],
            resources=[
                _objects(artifacts, RUN_PREFIX),
                _objects(artifacts, RECEIPT_EVIDENCE_PREFIX),
            ],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="DenyEverythingTheHostDoesNotServe",
            effect=iam.Effect.DENY,
            actions=[
                "dynamodb:*",
                "bedrock:*",
                "secretsmanager:*",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "iam:PassRole",
            ],
            resources=["*"],
        )
    )


def _grant_deployer(
    role: iam.Role,
    *,
    account_id: str,
    region: str,
    stack_name: str,
    controller_arn: str,
    decider_host_arn: str,
    execution_role_arn: str | None,
) -> None:
    """Confine the deployer to the Unstuck stack and to exact role passing.

    A change set executed against another stack would run with that stack's
    own service role, which is why the scope here is the stack resource rather
    than the action list, and why the denial below names every other stack.
    """
    stack_arn = f"arn:aws:cloudformation:{region}:{account_id}:stack/{stack_name}/*"
    change_set_arn = (
        f"arn:aws:cloudformation:{region}:{account_id}:"
        f"changeSet/{CHANGE_SET_NAME_PREFIX}*/*"
    )
    scoped = [stack_arn, change_set_arn]
    role.add_to_policy(
        iam.PolicyStatement(
            sid="UnstuckStackInspection",
            actions=list(STACK_READ_ACTIONS),
            resources=scoped,
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="UnstuckStackChangeSetLifecycle",
            actions=list(STACK_CHANGE_ACTIONS),
            resources=scoped,
            conditions=(
                {"StringEquals": {"cloudformation:RoleArn": execution_role_arn}}
                if execution_role_arn is not None
                else None
            ),
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="DenyEveryStackButUnstuck",
            effect=iam.Effect.DENY,
            actions=["cloudformation:*"],
            not_resources=scoped,
        )
    )
    passable = [
        (
            "PassTheControllerRuntimeRoleToAgentCoreOnly",
            controller_arn,
            AGENTCORE_PRINCIPAL,
        ),
        ("PassTheDeciderHostRoleToEc2Only", decider_host_arn, EC2_PRINCIPAL),
    ]
    if execution_role_arn is not None:
        passable.append(
            (
                "PassTheExecutionRoleToCloudFormationOnly",
                execution_role_arn,
                CLOUDFORMATION_PRINCIPAL,
            )
        )
    for sid, passed_role, service in passable:
        role.add_to_policy(
            iam.PolicyStatement(
                sid=sid,
                actions=["iam:PassRole"],
                resources=[passed_role],
                conditions={"StringEquals": {"iam:PassedToService": service}},
            )
        )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="DenyPassingAnyOtherRole",
            effect=iam.Effect.DENY,
            actions=["iam:PassRole"],
            not_resources=[passed_role for _, passed_role, _ in passable],
        )
    )


def _grant_operator(
    role: iam.Role,
    *,
    runs: dynamodb.Table,
    receipts: dynamodb.Table,
    artifacts: s3.Bucket,
    holdout: s3.Bucket,
    controller_runtime_arn: str,
) -> None:
    """Invoke one runtime, read run status, and never read a raw receipt.

    A receipt row stores its complete serialized body, so a table read is a
    read of everything the receipt holds across every namespace. The operator
    therefore has no receipt-table grant at all: a redacted, namespace-scoped
    status projection is the only path this role may ever gain, and until that
    projection exists the grant stays absent rather than approximated.
    """
    role.add_to_policy(
        iam.PolicyStatement(
            sid="InvokeTheController",
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[controller_runtime_arn],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="RunStatusReadsWithScopedLeadingKeys",
            actions=["dynamodb:GetItem", "dynamodb:Query"],
            resources=[runs.table_arn],
            conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["RUN#*"]}},
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="DenyRawReceiptTableReads",
            effect=iam.Effect.DENY,
            actions=["dynamodb:*"],
            resources=[receipts.table_arn, f"{receipts.table_arn}/index/*"],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="ArtifactReads",
            actions=["s3:GetObject"],
            resources=[
                _objects(artifacts, RUN_PREFIX),
                _objects(artifacts, EXPERIMENT_PREFIX),
            ],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="StageTheCheckpoint",
            actions=["s3:PutObject"],
            resources=[_objects(artifacts, DECIDER_CHECKPOINT_PREFIX)],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="BudgetReads", actions=["budgets:ViewBudget"], resources=["*"]
        )
    )
    role.add_to_policy(_deny_holdout(holdout))
    role.add_to_policy(
        iam.PolicyStatement(
            sid="DenyDeciderInvokeReceiptWritesAndRolePassing",
            effect=iam.Effect.DENY,
            actions=[
                "execute-api:Invoke",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "iam:PassRole",
            ],
            resources=["*"],
        )
    )


def _grant_evaluator(
    role: iam.Role, *, artifacts: s3.Bucket, holdout: s3.Bucket, runs: dynamodb.Table
) -> None:
    role.add_to_policy(
        iam.PolicyStatement(
            sid="EvaluatorTruthReads",
            actions=["s3:GetObject", "s3:ListBucket"],
            resources=[holdout.bucket_arn, holdout.arn_for_objects("*")],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="RunReadsAndExperimentWrites",
            actions=["s3:GetObject"],
            resources=[_objects(artifacts, RUN_PREFIX)],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="ExperimentWrites",
            actions=["s3:PutObject"],
            resources=[_objects(artifacts, EXPERIMENT_PREFIX)],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="DenyRuntimeInvokeSecretsAndRunWrites",
            effect=iam.Effect.DENY,
            actions=[
                "bedrock-agentcore:InvokeAgentRuntime",
                "secretsmanager:*",
                "iam:PassRole",
            ],
            resources=["*"],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="DenyRunTableWrites",
            effect=iam.Effect.DENY,
            actions=[
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
            ],
            resources=[runs.table_arn],
        )
    )
