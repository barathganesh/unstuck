"""Private DynamoDB tables and S3 buckets.

`docs/spec/design.md` section 16 fixes the controls: Block Public Access on all
four settings, a TLS-only bucket policy denying `aws:SecureTransport=false`,
default encryption, enforced object ownership, and prefix scoping. The
evaluator-truth bucket is a separate bucket with the same controls whose policy
admits only the deployer and evaluator roles.

The allow-set names role ARNs, not the identities that assume them, because
`aws:PrincipalArn` for a role session is the role's own ARN rather than the
caller's. Naming the assuming identity would deny the designed R5 and R7 roles
while admitting whichever human or federated principal was configured, so the
bucket policy is attached after the roles exist and is keyed on their ARNs.

Nothing here is a verified grant. Synthesis is offline and credential-free, and
a bucket policy in a template is not a denied request: REQ-SEC-11, REQ-SEC-12
and PF8 stay Pending.
"""

from dataclasses import dataclass

from aws_cdk import RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from unstuck.cloud.receipts import RECEIPT_ID_INDEX

STRING = dynamodb.AttributeType.STRING


@dataclass(frozen=True)
class StorageResources:
    """Every storage construct the rest of the topology binds to."""

    runs: dynamodb.Table
    receipts: dynamodb.Table
    artifacts: s3.Bucket
    holdout: s3.Bucket


def _table(scope: Construct, identifier: str, name: str) -> dynamodb.Table:
    return dynamodb.Table(
        scope,
        identifier,
        table_name=name,
        partition_key=dynamodb.Attribute(name="PK", type=STRING),
        sort_key=dynamodb.Attribute(name="SK", type=STRING),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True
        ),
        deletion_protection=True,
        removal_policy=RemovalPolicy.RETAIN,
    )


def _bucket(scope: Construct, identifier: str, name: str) -> s3.Bucket:
    return s3.Bucket(
        scope,
        identifier,
        bucket_name=name,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.S3_MANAGED,
        enforce_ssl=True,
        minimum_tls_version=1.2,
        object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
        versioned=True,
        public_read_access=False,
        removal_policy=RemovalPolicy.RETAIN,
    )


def restrict_holdout_to_roles(holdout: s3.Bucket, role_arns: tuple[str, ...]) -> None:
    """Deny the holdout bucket to every principal but the named roles.

    `role_arns` are the resolved ARNs of the roles themselves, so the allow-set
    matches what an R5 or R7 session actually presents. `ArnNotEquals` is the
    ARN-aware comparison for `aws:PrincipalArn`; the explicit deny remains in
    force for every other principal, including the identities that assume
    those roles.
    """
    if not role_arns:
        raise ValueError("the holdout allow-set names at least one role")
    holdout.add_to_resource_policy(
        iam.PolicyStatement(
            sid="DenyEveryPrincipalButTheDeployerAndEvaluatorRoles",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["s3:*"],
            resources=[holdout.bucket_arn, holdout.arn_for_objects("*")],
            conditions={"ArnNotEquals": {"aws:PrincipalArn": list(role_arns)}},
        )
    )


def add_storage(
    scope: Construct,
    *,
    run_table_name: str,
    receipt_table_name: str,
    artifact_bucket: str,
    holdout_bucket: str,
) -> StorageResources:
    runs = _table(scope, "RunStateTable", run_table_name)
    receipts = _table(scope, "ReceiptIndexTable", receipt_table_name)
    # `supersede` is handed a namespace and a receipt id but no problem
    # fingerprint, so it resolves the owning partition through this index.
    receipts.add_global_secondary_index(
        index_name=RECEIPT_ID_INDEX,
        partition_key=dynamodb.Attribute(name="SK", type=STRING),
        projection_type=dynamodb.ProjectionType.KEYS_ONLY,
    )
    artifacts = _bucket(scope, "ArtifactBucket", artifact_bucket)
    holdout = _bucket(scope, "HoldoutBucket", holdout_bucket)
    # design.md section 16: every principal except the deployer and evaluator
    # roles is denied, so no runtime role can read evaluator truth. The denial
    # itself is attached by `restrict_holdout_to_roles` once those roles exist
    # and their ARNs can be named.
    return StorageResources(
        runs=runs, receipts=receipts, artifacts=artifacts, holdout=holdout
    )
