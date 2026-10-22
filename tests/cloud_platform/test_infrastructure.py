"""CDK security and IAM assertions over the synthesized private topology.

Synthesis runs in this process with an explicit synthetic account and region.
The autouse socket guard is active throughout, so a template that synthesizes
here provably needed no network, no credential chain, and no context lookup.

A template is design evidence. It proves the planned topology matches
`docs/spec/design.md`; it never proves a grant exists or a request is denied.
REQ-SEC-06, REQ-SEC-10, REQ-SEC-11, REQ-SEC-12 and PF6 stay Pending.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from infra.decider import (
    DECIDER_PATH,
    MODEL_SERVER_BIND,
    MODEL_SERVER_PORT,
    PROVIDER_DEADLINE_SECONDS,
    PROXY_PORT,
)
from unstuck.cloud.receipts import RECEIPT_ID_INDEX
from unstuck.cloud.synth import (
    DESIGN_15_MATRIX,
    ROLE_PREFIXES,
    SynthesisInputs,
    policy_diff,
    synthesize,
    template_for,
)

from .conftest import (
    SYNTHETIC_ARTIFACT_BUCKET,
    SYNTHETIC_HOLDOUT_BUCKET,
    SYNTHETIC_RECEIPT_TABLE,
    SYNTHETIC_RUN_TABLE,
)

BUCKET_NAMES = (SYNTHETIC_ARTIFACT_BUCKET, SYNTHETIC_HOLDOUT_BUCKET)
RUNTIME_ROLES = ("R1", "R4", "R6")
OPEN_CIDRS = ("0.0.0.0/0", "::/0")


@pytest.fixture
def template(inputs: SynthesisInputs, tmp_path: Path) -> dict[str, Any]:
    return template_for(inputs, outdir=tmp_path / "cdk.out")


def resources(template: dict[str, Any], kind: str) -> Iterator[dict[str, Any]]:
    for resource in template.get("Resources", {}).values():
        if resource.get("Type") == kind:
            yield resource.get("Properties", {})


def statements(template: dict[str, Any], prefix: str = "") -> Iterator[dict[str, Any]]:
    for logical, resource in template.get("Resources", {}).items():
        if not logical.startswith(prefix):
            continue
        properties = resource.get("Properties", {})
        documents = [properties.get("PolicyDocument")]
        for inline in properties.get("Policies", []) or []:
            documents.append(inline.get("PolicyDocument"))
        for document in documents:
            if isinstance(document, dict):
                for statement in document.get("Statement", []):
                    if isinstance(statement, dict):
                        yield statement


def test_both_buckets_block_public_access_on_all_four_settings_offline(
    template: dict[str, Any],
) -> None:
    buckets = {
        properties["BucketName"]: properties
        for properties in resources(template, "AWS::S3::Bucket")
    }
    assert set(buckets) == set(BUCKET_NAMES)
    for name in BUCKET_NAMES:
        block = buckets[name]["PublicAccessBlockConfiguration"]
        assert block == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }, name


def test_both_buckets_enforce_encryption_and_object_ownership_offline(
    template: dict[str, Any],
) -> None:
    for properties in resources(template, "AWS::S3::Bucket"):
        encryption = properties["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        assert (
            encryption[0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
        )
        ownership = properties["OwnershipControls"]["Rules"]
        assert ownership == [{"ObjectOwnership": "BucketOwnerEnforced"}]
        assert properties["VersioningConfiguration"] == {"Status": "Enabled"}


def test_both_buckets_deny_plaintext_transport_offline(
    template: dict[str, Any],
) -> None:
    denials = [
        statement
        for properties in resources(template, "AWS::S3::BucketPolicy")
        for statement in properties["PolicyDocument"]["Statement"]
        if statement.get("Effect") == "Deny"
        and statement.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport")
        in {"false", False}
    ]
    assert len(denials) == len(BUCKET_NAMES)
    for statement in denials:
        assert statement["Action"] == "s3:*"
        assert statement["Principal"] == {"AWS": "*"}


def test_no_allow_statement_grants_an_unconditioned_public_principal_offline(
    template: dict[str, Any],
) -> None:
    for statement in statements(template):
        if statement.get("Effect", "Allow") != "Allow":
            continue
        principal = statement.get("Principal")
        public = principal in ("*", {"AWS": "*"}) or (
            isinstance(principal, dict) and principal.get("AWS") == "*"
        )
        assert not (public and not statement.get("Condition")), statement
    rendered = json.dumps(template)
    assert "AllUsers" not in rendered
    assert "AuthenticatedUsers" not in rendered


def test_no_security_group_permits_open_ingress_offline(
    template: dict[str, Any],
) -> None:
    rules: list[dict[str, Any]] = []
    for properties in resources(template, "AWS::EC2::SecurityGroup"):
        rules.extend(properties.get("SecurityGroupIngress", []) or [])
    rules.extend(resources(template, "AWS::EC2::SecurityGroupIngress"))
    assert rules
    for rule in rules:
        assert rule.get("CidrIp") not in OPEN_CIDRS, rule
        assert rule.get("CidrIpv6") not in OPEN_CIDRS, rule


def test_the_model_server_port_is_never_reachable_from_the_network_offline(
    template: dict[str, Any],
) -> None:
    groups = list(resources(template, "AWS::EC2::SecurityGroup"))
    assert groups
    for properties in groups:
        for rule in properties.get("SecurityGroupIngress", []) or []:
            assert rule.get("FromPort") != MODEL_SERVER_PORT, rule
            assert rule.get("ToPort") != MODEL_SERVER_PORT, rule
    proxy_logical = next(
        logical
        for logical, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::EC2::SecurityGroup"
        and "model server never exposed"
        in resource["Properties"].get("GroupDescription", "")
    )
    # The proxy port admits one security identity, so the rule is a separate
    # resource rather than an inline CIDR range.
    assert (
        "SecurityGroupIngress" not in template["Resources"][proxy_logical]["Properties"]
    )
    proxy_rules = [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource.get("Type") == "AWS::EC2::SecurityGroupIngress"
        and resource["Properties"].get("GroupId")
        == {"Fn::GetAtt": [proxy_logical, "GroupId"]}
    ]
    assert len(proxy_rules) == 1
    for rule in proxy_rules:
        assert (rule["FromPort"], rule["ToPort"]) == (PROXY_PORT, PROXY_PORT)
        assert "CidrIp" not in rule
        assert rule["SourceSecurityGroupId"]
    instances = list(resources(template, "AWS::EC2::Instance"))
    assert len(instances) == 1
    user_data = json.dumps(instances[0]["UserData"])
    assert MODEL_SERVER_BIND in user_data
    assert "0.0.0.0" not in user_data
    interfaces = instances[0]["NetworkInterfaces"]
    assert len(interfaces) == 1
    assert interfaces[0]["AssociatePublicIpAddress"] is False
    assert interfaces[0]["GroupSet"]


def test_the_decider_api_is_private_and_iam_authorized_offline(
    template: dict[str, Any],
) -> None:
    apis = list(resources(template, "AWS::ApiGateway::RestApi"))
    assert len(apis) == 1
    assert apis[0]["EndpointConfiguration"]["Types"] == ["PRIVATE"]
    methods = list(resources(template, "AWS::ApiGateway::Method"))
    assert methods
    for method in methods:
        assert method["AuthorizationType"] == "AWS_IAM"
        assert method["Integration"]["ConnectionType"] == "VPC_LINK"
    assert list(resources(template, "AWS::ApiGateway::VpcLink"))
    endpoints = [
        properties
        for properties in resources(template, "AWS::EC2::VPCEndpoint")
        if properties.get("VpcEndpointType") == "Interface"
    ]
    assert endpoints


def test_the_decider_resource_policy_admits_only_the_controller_offline(
    template: dict[str, Any],
) -> None:
    policy = next(resources(template, "AWS::ApiGateway::RestApi"))["Policy"]
    allows = [item for item in policy["Statement"] if item["Effect"] == "Allow"]
    denies = [item for item in policy["Statement"] if item["Effect"] == "Deny"]
    assert len(allows) == 1
    assert allows[0]["Action"] == "execute-api:Invoke"
    assert allows[0]["Resource"] == f"execute-api:/*/POST{DECIDER_PATH}"
    assert "aws:SourceVpce" in allows[0]["Condition"]["StringEquals"]
    assert json.dumps(allows[0]["Principal"]).count("ControllerRuntimeRole") == 1
    assert len(denies) == 2
    conditions = [
        key for item in denies for key in item["Condition"]["StringNotEquals"]
    ]
    assert sorted(conditions) == ["aws:PrincipalArn", "aws:SourceVpce"]


def test_the_integration_timeout_covers_the_provider_deadline_offline(
    template: dict[str, Any],
) -> None:
    for method in resources(template, "AWS::ApiGateway::Method"):
        milliseconds = method["Integration"]["TimeoutInMillis"]
        assert milliseconds >= PROVIDER_DEADLINE_SECONDS * 1000, milliseconds


def test_every_runtime_role_is_denied_the_evaluator_bucket_offline(
    template: dict[str, Any],
) -> None:
    holdout_logical = next(
        logical
        for logical, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::S3::Bucket"
        and resource["Properties"]["BucketName"] == SYNTHETIC_HOLDOUT_BUCKET
    )
    for role in RUNTIME_ROLES:
        denials = [
            statement
            for statement in statements(template, ROLE_PREFIXES[role])
            if statement.get("Effect") == "Deny"
            and holdout_logical in json.dumps(statement.get("Resource"))
        ]
        assert denials, role
        assert any("s3:*" in json.dumps(item["Action"]) for item in denials), role
    bucket_policy = [
        statement
        for properties in resources(template, "AWS::S3::BucketPolicy")
        for statement in properties["PolicyDocument"]["Statement"]
        if statement.get("Sid") == "DenyEveryPrincipalButTheDeployerAndEvaluatorRoles"
    ]
    assert len(bucket_policy) == 1
    # The allow-set names the R5 and R7 role ARNs, because `aws:PrincipalArn`
    # for a role session is the role's own ARN, not the assuming identity's.
    allowed = bucket_policy[0]["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]
    assert len(allowed) == 2
    rendered_allowed = json.dumps(allowed)
    for role in ("R5", "R7"):
        logical = next(
            logical
            for logical, resource in template["Resources"].items()
            if resource.get("Type") == "AWS::IAM::Role"
            and logical.startswith(ROLE_PREFIXES[role])
        )
        assert rendered_allowed.count(logical) == 1, role


def test_the_controller_role_cannot_pass_a_role_or_reach_restricted_prefixes_offline(
    template: dict[str, Any],
) -> None:
    allows: list[dict[str, Any]] = []
    denies: list[dict[str, Any]] = []
    for statement in statements(template, ROLE_PREFIXES["R1"]):
        (denies if statement.get("Effect") == "Deny" else allows).append(statement)
    rendered_allows = json.dumps(allows)
    assert "iam:PassRole" not in rendered_allows
    assert "models/" not in rendered_allows
    assert "experiments/" not in rendered_allows
    assert "sts:AssumeRole" not in rendered_allows
    rendered_denies = json.dumps(denies)
    assert "iam:PassRole" in rendered_denies
    assert "models/*" in rendered_denies
    assert "experiments/*" in rendered_denies
    assert "runs/*" in rendered_allows
    assert "receipt-evidence/*" in rendered_allows
    assert "fixtures/public/*" in rendered_allows


def test_the_run_and_receipt_tables_are_private_and_indexed_offline(
    template: dict[str, Any],
) -> None:
    tables = {
        properties["TableName"]: properties
        for properties in resources(template, "AWS::DynamoDB::Table")
    }
    assert set(tables) == {SYNTHETIC_RUN_TABLE, SYNTHETIC_RECEIPT_TABLE}
    for properties in tables.values():
        assert properties["SSESpecification"] == {"SSEEnabled": True}
        assert properties["DeletionProtectionEnabled"] is True
        assert properties["BillingMode"] == "PAY_PER_REQUEST"
        keys = {
            item["AttributeName"]: item["KeyType"] for item in properties["KeySchema"]
        }
        assert keys == {"PK": "HASH", "SK": "RANGE"}
    indexes = tables[SYNTHETIC_RECEIPT_TABLE]["GlobalSecondaryIndexes"]
    assert [item["IndexName"] for item in indexes] == [RECEIPT_ID_INDEX]
    assert indexes[0]["KeySchema"] == [{"AttributeName": "SK", "KeyType": "HASH"}]


def test_the_template_carries_exactly_the_five_design_roles_offline(
    template: dict[str, Any],
) -> None:
    roles = [
        logical
        for logical, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::IAM::Role"
    ]
    assert len(roles) == len(ROLE_PREFIXES)
    for prefix in ROLE_PREFIXES.values():
        assert sum(logical.startswith(prefix) for logical in roles) == 1, prefix
    # A managed policy attachment would bypass the design.md section 15 diff.
    assert "ManagedPolicyArns" not in json.dumps(template)


def test_the_synthesized_policies_match_design_section_15_offline(
    template: dict[str, Any],
) -> None:
    diff = policy_diff(template)
    assert set(diff) == {expectation.role_id for expectation in DESIGN_15_MATRIX}
    for role, findings in diff.items():
        assert findings == {
            "unexpected_allow": [],
            "missing_allow": [],
            "unexpected_deny": [],
            "missing_deny": [],
        }, (role, findings)


def test_synthesis_needs_no_credential_lookup_or_network_offline(
    inputs: SynthesisInputs, tmp_path: Path
) -> None:
    assembly = synthesize(inputs, tmp_path / "assembly")
    manifest = json.loads((assembly / "manifest.json").read_text(encoding="utf-8"))
    # An entry here is a context lookup CDK would resolve against an account.
    assert not manifest.get("missing"), manifest.get("missing")
    rendered = json.dumps(manifest)
    assert "unknown-account" not in rendered
    assert "unknown-region" not in rendered
    # The only parameter named anywhere is the deploy-time bootstrap version
    # check, which an owner-approved deploy would read. The empty `missing`
    # list above is the proof that synthesis resolved nothing from an account.
    assert set(
        re.findall(r'"bootstrapStackVersionSsmParameter": "([^"]+)"', rendered)
    ) == {"/cdk-bootstrap/hnb659fds/version"}
    assert "SsmParameter" not in rendered.replace(
        "bootstrapStackVersionSsmParameter", ""
    )
    environments = {
        artifact.get("environment")
        for artifact in manifest["artifacts"].values()
        if artifact.get("environment")
    }
    assert environments == {f"aws://{inputs.config.account_id}/{inputs.config.region}"}
    assert (assembly / f"{inputs.stack_name}.template.json").exists()


def test_a_plain_synthesis_carries_no_agentcore_runtime_offline(
    template: dict[str, Any],
) -> None:
    """The runtime needs a published image digest, which synthesis cannot invent.

    The resource type and its properties are verified against the installed
    `aws_cdk.aws_bedrockagentcore` constructs, so the runtime is no longer
    withheld for want of a contract. What it needs is a digest-pinned image in
    the deployment account's own registry, which is a deployment input. A plain
    synthesis therefore carries everything the runtime is bounded by and none of
    the runtime itself; `unstuck.cloud.deployment.deployment_template` adds it
    once that input exists, and
    `tests/cloud_platform/test_agentcore_deployment.py` asserts its shape.
    """
    kinds = {resource["Type"] for resource in template.get("Resources", {}).values()}
    assert not any(kind.startswith("AWS::BedrockAgentCore") for kind in kinds)
    assert "AWS::ECR::Repository" in kinds
    assert "AWS::IAM::Role" in kinds
