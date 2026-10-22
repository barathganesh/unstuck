# Unit command: UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/cloud_platform/test_security_repair.py -q
"""Structural security assertions for the synthesized private topology.

Every assertion reads CloudFormation and IAM semantics out of the rendered
template rather than searching source text, so a grant can only pass by being
scoped in the template itself. Synthesis is offline: the autouse socket and
credential guards of `conftest.py` are active, no account is reachable, and a
template remains design evidence. PF6, PF8, REQ-SEC-10, REQ-SEC-11 and
REQ-SEC-12 stay Pending.

The findings covered here are the confirmed ones of the security review:
R5 stack and change-set scoping, R5 role passing, the holdout bucket allow-set,
trust-principal validation, R6 runtime and receipt isolation, and the Decider
target ingress identity.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from infra.decider import MODEL_SERVER_PORT, PROXY_PORT
from infra.roles import CHANGE_SET_NAME_PREFIX, STACK_OWNED_ROLE_NAMES
from infra.stack import STACK_NAME
from infra.storage import restrict_holdout_to_roles
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.synth import ROLE_PREFIXES, SynthesisInputs, stack_for, template_for

from .conftest import SYNTHETIC_ACCOUNT, SYNTHETIC_REGION

STACK_RESOURCE = (
    f"arn:aws:cloudformation:{SYNTHETIC_REGION}:{SYNTHETIC_ACCOUNT}:"
    f"stack/{STACK_NAME}/*"
)
CHANGE_SET_RESOURCE = (
    f"arn:aws:cloudformation:{SYNTHETIC_REGION}:{SYNTHETIC_ACCOUNT}:"
    f"changeSet/{CHANGE_SET_NAME_PREFIX}*/*"
)
EXECUTION_ROLE = f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:role/offline-cloudformation-exec"
AGENTCORE_SERVICE = "bedrock-agentcore.amazonaws.com"
#: A second synthetic stack name. Nothing here names a real stack.
ALTERNATE_STACK_NAME = "UnstuckOfflineAlternate"


@pytest.fixture
def template(inputs: SynthesisInputs, tmp_path: Path) -> dict[str, Any]:
    return template_for(inputs, outdir=tmp_path / "cdk.out")


@pytest.fixture
def pinned_inputs(inputs: SynthesisInputs) -> SynthesisInputs:
    """Inputs that pin one dedicated CloudFormation execution role."""
    return SynthesisInputs.model_validate(
        inputs.model_dump() | {"cloudformation_execution_role_arn": EXECUTION_ROLE}
    )


@pytest.fixture
def pinned_template(pinned_inputs: SynthesisInputs, tmp_path: Path) -> dict[str, Any]:
    return template_for(pinned_inputs, outdir=tmp_path / "pinned.out")


def properties_of(template: dict[str, Any], kind: str) -> Iterator[dict[str, Any]]:
    for resource in template.get("Resources", {}).values():
        if resource.get("Type") == kind:
            yield resource.get("Properties", {})


def statements(template: dict[str, Any], prefix: str = "") -> Iterator[dict[str, Any]]:
    for logical, resource in template.get("Resources", {}).items():
        if not logical.startswith(prefix):
            continue
        documents = [resource.get("Properties", {}).get("PolicyDocument")]
        for inline in resource.get("Properties", {}).get("Policies", []) or []:
            documents.append(inline.get("PolicyDocument"))
        for document in documents:
            if isinstance(document, dict):
                for statement in document.get("Statement", []):
                    if isinstance(statement, dict):
                        yield statement


def by_sid(template: dict[str, Any], prefix: str) -> dict[str, dict[str, Any]]:
    return {
        str(statement["Sid"]): statement
        for statement in statements(template, prefix)
        if statement.get("Sid")
    }


def actions_of(statement: dict[str, Any]) -> list[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


def listed(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def logical_id(template: dict[str, Any], prefix: str, kind: str) -> str:
    matches = [
        logical
        for logical, resource in template["Resources"].items()
        if resource.get("Type") == kind and logical.startswith(prefix)
    ]
    assert len(matches) == 1, (prefix, matches)
    return matches[0]


def role_arn_reference(template: dict[str, Any], role_id: str) -> dict[str, Any]:
    logical = logical_id(template, ROLE_PREFIXES[role_id], "AWS::IAM::Role")
    return {"Fn::GetAtt": [logical, "Arn"]}


# G1: CloudFormation and IAM resources are scoped to the Unstuck stack, the
# controller runtime, and the intended role principals.


def test_the_deployer_never_touches_cloudformation_outside_the_unstuck_stack_offline(
    template: dict[str, Any],
) -> None:
    allows = [
        statement
        for statement in statements(template, ROLE_PREFIXES["R5"])
        if statement.get("Effect", "Allow") == "Allow"
        and any(
            action.startswith("cloudformation:") for action in actions_of(statement)
        )
    ]
    assert allows
    for statement in allows:
        resources = listed(statement.get("Resource"))
        assert resources, statement
        assert "*" not in resources, statement
        assert set(resources) <= {STACK_RESOURCE, CHANGE_SET_RESOURCE}, statement


def test_the_deployer_explicitly_denies_every_other_stack_and_change_set_offline(
    template: dict[str, Any],
) -> None:
    denial = by_sid(template, ROLE_PREFIXES["R5"])["DenyEveryStackButUnstuck"]
    assert denial["Effect"] == "Deny"
    assert actions_of(denial) == ["cloudformation:*"]
    assert sorted(listed(denial["NotResource"])) == sorted(
        [STACK_RESOURCE, CHANGE_SET_RESOURCE]
    )
    assert "Resource" not in denial


def test_the_deployer_scope_follows_the_synthesized_stack_name_offline(
    inputs: SynthesisInputs, tmp_path: Path
) -> None:
    """A custom stack name must not lock the deployer out of its own stack.

    Synthesis accepts a stack name, so the R5 CloudFormation scope is built
    from the name that was actually synthesized. A constant here would allow
    only the default stack and deny the real one through `NotResource`.
    """
    renamed = SynthesisInputs.model_validate(
        inputs.model_dump() | {"stack_name": ALTERNATE_STACK_NAME}
    )
    assert renamed.stack_name != STACK_NAME
    template = template_for(renamed, outdir=tmp_path / "renamed.out")
    expected = (
        f"arn:aws:cloudformation:{SYNTHETIC_REGION}:{SYNTHETIC_ACCOUNT}:"
        f"stack/{ALTERNATE_STACK_NAME}/*"
    )
    rows = by_sid(template, ROLE_PREFIXES["R5"])
    for sid in ("UnstuckStackInspection", "UnstuckStackChangeSetLifecycle"):
        resources = listed(rows[sid]["Resource"])
        assert expected in resources, sid
        assert STACK_RESOURCE not in resources, sid
    denial = rows["DenyEveryStackButUnstuck"]
    assert expected in listed(denial["NotResource"])
    assert STACK_RESOURCE not in listed(denial["NotResource"])


def test_change_set_execution_pins_the_dedicated_execution_role_offline(
    pinned_template: dict[str, Any],
) -> None:
    mutating = by_sid(pinned_template, ROLE_PREFIXES["R5"])[
        "UnstuckStackChangeSetLifecycle"
    ]
    assert sorted(actions_of(mutating)) == [
        "cloudformation:CreateChangeSet",
        "cloudformation:ExecuteChangeSet",
    ]
    assert mutating["Condition"] == {
        "StringEquals": {"cloudformation:RoleArn": EXECUTION_ROLE}
    }


def test_an_unpinned_deployer_carries_no_execution_role_condition_offline(
    template: dict[str, Any],
) -> None:
    mutating = by_sid(template, ROLE_PREFIXES["R5"])["UnstuckStackChangeSetLifecycle"]
    assert "Condition" not in mutating
    assert EXECUTION_ROLE not in json.dumps(template)


def test_the_deployer_passes_only_the_intended_role_to_its_own_service_offline(
    pinned_template: dict[str, Any],
) -> None:
    rows = by_sid(pinned_template, ROLE_PREFIXES["R5"])
    expected = {
        "PassTheControllerRuntimeRoleToAgentCoreOnly": (
            role_arn_reference(pinned_template, "R1"),
            AGENTCORE_SERVICE,
        ),
        "PassTheDeciderHostRoleToEc2Only": (
            role_arn_reference(pinned_template, "R4"),
            "ec2.amazonaws.com",
        ),
        "PassTheExecutionRoleToCloudFormationOnly": (
            EXECUTION_ROLE,
            "cloudformation.amazonaws.com",
        ),
    }
    for sid, (resource, service) in expected.items():
        statement = rows[sid]
        assert actions_of(statement) == ["iam:PassRole"]
        assert listed(statement["Resource"]) == [resource], sid
        assert statement["Condition"] == {
            "StringEquals": {"iam:PassedToService": service}
        }, sid
    denial = rows["DenyPassingAnyOtherRole"]
    assert denial["Effect"] == "Deny"
    assert actions_of(denial) == ["iam:PassRole"]
    assert sorted(
        json.dumps(item, sort_keys=True) for item in listed(denial["NotResource"])
    ) == sorted(
        json.dumps(item, sort_keys=True)
        for item in (
            role_arn_reference(pinned_template, "R1"),
            role_arn_reference(pinned_template, "R4"),
            EXECUTION_ROLE,
        )
    )


def test_no_deployer_allow_statement_passes_a_wildcard_role_offline(
    template: dict[str, Any],
) -> None:
    for statement in statements(template, ROLE_PREFIXES["R5"]):
        if statement.get("Effect", "Allow") != "Allow":
            continue
        if "iam:PassRole" not in actions_of(statement):
            continue
        assert "*" not in listed(statement.get("Resource")), statement
        assert "iam:PassedToService" in statement["Condition"]["StringEquals"]


# G1: the local operator reaches one runtime and no raw receipt row.


def test_the_operator_invokes_exactly_the_configured_controller_runtime_offline(
    template: dict[str, Any], config: CloudResourceConfig
) -> None:
    statement = by_sid(template, ROLE_PREFIXES["R6"])["InvokeTheController"]
    assert actions_of(statement) == ["bedrock-agentcore:InvokeAgentRuntime"]
    assert listed(statement["Resource"]) == [config.controller_runtime_arn]
    assert "runtime/*" not in json.dumps(statement)


def test_the_operator_cannot_read_a_raw_receipt_row_offline(
    template: dict[str, Any],
) -> None:
    runs = logical_id(template, "RunStateTable", "AWS::DynamoDB::Table")
    receipts = logical_id(template, "ReceiptIndexTable", "AWS::DynamoDB::Table")
    rows = by_sid(template, ROLE_PREFIXES["R6"])
    reads = rows["RunStatusReadsWithScopedLeadingKeys"]
    assert listed(reads["Resource"]) == [{"Fn::GetAtt": [runs, "Arn"]}]
    assert reads["Condition"] == {
        "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["RUN#*"]}
    }
    for statement in statements(template, ROLE_PREFIXES["R6"]):
        if statement.get("Effect", "Allow") != "Allow":
            continue
        assert receipts not in json.dumps(statement.get("Resource")), statement
    denial = rows["DenyRawReceiptTableReads"]
    assert denial["Effect"] == "Deny"
    assert actions_of(denial) == ["dynamodb:*"]
    rendered = json.dumps(listed(denial["Resource"]))
    assert rendered.count(receipts) == 2
    assert "index/*" in rendered


# G2: the holdout bucket allow-set names the R5 and R7 role ARNs.


def test_the_holdout_bucket_admits_the_deployer_and_evaluator_roles_offline(
    template: dict[str, Any], inputs: SynthesisInputs
) -> None:
    denials = [
        statement
        for properties in properties_of(template, "AWS::S3::BucketPolicy")
        for statement in properties["PolicyDocument"]["Statement"]
        if statement.get("Sid") == "DenyEveryPrincipalButTheDeployerAndEvaluatorRoles"
    ]
    assert len(denials) == 1
    statement = denials[0]
    assert statement["Effect"] == "Deny"
    assert statement["Principal"] == {"AWS": "*"}
    allowed = statement["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]
    assert sorted(json.dumps(item, sort_keys=True) for item in listed(allowed)) == (
        sorted(
            json.dumps(item, sort_keys=True)
            for item in (
                role_arn_reference(template, "R5"),
                role_arn_reference(template, "R7"),
            )
        )
    )
    rendered = json.dumps(statement)
    assert inputs.deployer_principal_arn not in rendered
    assert inputs.evaluator_principal_arn not in rendered


def test_a_pinned_execution_role_is_admitted_to_the_holdout_bucket_offline(
    pinned_template: dict[str, Any],
) -> None:
    statement = next(
        item
        for properties in properties_of(pinned_template, "AWS::S3::BucketPolicy")
        for item in properties["PolicyDocument"]["Statement"]
        if item.get("Sid") == "DenyEveryPrincipalButTheDeployerAndEvaluatorRoles"
    )
    allowed = listed(statement["Condition"]["ArnNotEquals"]["aws:PrincipalArn"])
    assert EXECUTION_ROLE in allowed
    assert len(allowed) == 3


def test_an_empty_holdout_allow_set_is_refused_offline(
    inputs: SynthesisInputs, tmp_path: Path
) -> None:
    """An empty allow-set would deny the evaluator its own truth bucket."""
    holdout = stack_for(inputs, outdir=tmp_path / "empty.out").storage.holdout
    with pytest.raises(ValueError):
        restrict_holdout_to_roles(holdout, ())


def test_the_evaluator_role_is_not_denied_its_own_truth_bucket_offline(
    template: dict[str, Any],
) -> None:
    for statement in statements(template, ROLE_PREFIXES["R7"]):
        if statement.get("Effect") != "Deny":
            continue
        assert "s3:*" not in actions_of(statement), statement


# G2: trust inputs accept role ARNs only, in this account, pairwise distinct.


@pytest.mark.parametrize(
    "refused",
    [
        f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:root",
        f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:user/offline-person",
        f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:group/offline-group",
        "arn:aws:iam::999999999999:role/offline-evaluator",
        f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:role/",
        f"arn:aws:sts::{SYNTHETIC_ACCOUNT}:assumed-role/offline/session",
    ],
)
def test_a_trust_principal_must_be_a_role_in_the_declared_account_offline(
    inputs: SynthesisInputs, refused: str
) -> None:
    for field in (
        "operator_principal_arn",
        "evaluator_principal_arn",
        "deployer_principal_arn",
    ):
        with pytest.raises(ValidationError):
            SynthesisInputs.model_validate(inputs.model_dump() | {field: refused})


@pytest.mark.parametrize(
    ("field", "source"),
    [
        ("operator_principal_arn", "evaluator_principal_arn"),
        ("operator_principal_arn", "deployer_principal_arn"),
        ("evaluator_principal_arn", "deployer_principal_arn"),
    ],
)
def test_the_operator_evaluator_and_deployer_are_pairwise_distinct_offline(
    inputs: SynthesisInputs, field: str, source: str
) -> None:
    with pytest.raises(ValidationError):
        SynthesisInputs.model_validate(
            inputs.model_dump() | {field: getattr(inputs, source)}
        )


def test_the_execution_role_is_distinct_from_every_trust_principal_offline(
    inputs: SynthesisInputs,
) -> None:
    with pytest.raises(ValidationError):
        SynthesisInputs.model_validate(
            inputs.model_dump()
            | {"cloudformation_execution_role_arn": inputs.deployer_principal_arn}
        )
    with pytest.raises(ValidationError):
        SynthesisInputs.model_validate(
            inputs.model_dump()
            | {
                "cloudformation_execution_role_arn": (
                    "arn:aws:iam::999999999999:role/offline-cloudformation-exec"
                )
            }
        )


TRUST_FIELDS = (
    "operator_principal_arn",
    "evaluator_principal_arn",
    "deployer_principal_arn",
    "cloudformation_execution_role_arn",
)


@pytest.mark.parametrize("role_name", sorted(STACK_OWNED_ROLE_NAMES))
@pytest.mark.parametrize("field", TRUST_FIELDS)
def test_a_stack_owned_role_is_never_an_external_trust_principal_offline(
    inputs: SynthesisInputs, field: str, role_name: str
) -> None:
    """The stack creates these roles; naming one as the principal self-trusts.

    A same-account role ARN passes the shape check, so the five fixed names the
    stack itself owns have to be refused by name. IAM role names are not
    distinguished by case, so a case variant names the same role and is refused
    on the same terms.
    """
    for variant in (role_name, role_name.upper(), role_name.capitalize()):
        arn = f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:role/{variant}"
        with pytest.raises(ValidationError):
            SynthesisInputs.model_validate(inputs.model_dump() | {field: arn})
        with pytest.raises(ValidationError):
            SynthesisInputs.model_validate(
                inputs.model_dump()
                | {field: f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:role/offline/{variant}"}
            )


@pytest.mark.parametrize(
    ("field", "source"),
    [
        ("operator_principal_arn", "evaluator_principal_arn"),
        ("operator_principal_arn", "deployer_principal_arn"),
        ("evaluator_principal_arn", "deployer_principal_arn"),
    ],
)
def test_a_case_variant_is_not_a_distinct_trust_principal_offline(
    inputs: SynthesisInputs, field: str, source: str
) -> None:
    collision = str(getattr(inputs, source)).upper()
    assert collision != getattr(inputs, source)
    with pytest.raises(ValidationError):
        SynthesisInputs.model_validate(inputs.model_dump() | {field: collision})


def test_a_case_variant_execution_role_is_not_distinct_from_a_principal_offline(
    inputs: SynthesisInputs,
) -> None:
    name = inputs.deployer_principal_arn.rsplit("/", 1)[-1]
    collision = f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:role/{name.upper()}"
    with pytest.raises(ValidationError):
        SynthesisInputs.model_validate(
            inputs.model_dump() | {"cloudformation_execution_role_arn": collision}
        )


# Network: the Decider target admits the load balancer's security identity.


def test_the_proxy_port_admits_only_the_load_balancer_security_group_offline(
    template: dict[str, Any],
) -> None:
    proxy_logical = next(
        logical
        for logical, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::EC2::SecurityGroup"
        and "model server never exposed"
        in resource["Properties"].get("GroupDescription", "")
    )
    ingress = [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource.get("Type") == "AWS::EC2::SecurityGroupIngress"
        and resource["Properties"].get("GroupId")
        == {"Fn::GetAtt": [proxy_logical, "GroupId"]}
    ]
    inline = template["Resources"][proxy_logical]["Properties"].get(
        "SecurityGroupIngress", []
    )
    assert not inline, inline
    assert len(ingress) == 1
    rule = ingress[0]
    assert (rule["FromPort"], rule["ToPort"]) == (PROXY_PORT, PROXY_PORT)
    assert "CidrIp" not in rule
    assert "CidrIpv6" not in rule
    assert rule["SourceSecurityGroupId"]


def test_the_network_load_balancer_carries_its_own_security_group_offline(
    template: dict[str, Any],
) -> None:
    balancers = list(
        properties_of(template, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    )
    assert len(balancers) == 1
    groups = listed(balancers[0]["SecurityGroups"])
    assert len(groups) == 1
    descriptions = {
        resource["Properties"].get("GroupDescription", "")
        for resource in template["Resources"].values()
        if resource.get("Type") == "AWS::EC2::SecurityGroup"
    }
    assert any("load balancer" in item.lower() for item in descriptions)


def test_no_cidr_range_reaches_the_decider_serving_ports_offline(
    template: dict[str, Any],
) -> None:
    """The only CIDR ingress left is the interface endpoint's own 443 rule."""
    rules = list(properties_of(template, "AWS::EC2::SecurityGroupIngress"))
    for properties in properties_of(template, "AWS::EC2::SecurityGroup"):
        rules.extend(properties.get("SecurityGroupIngress", []) or [])
    assert rules
    serving = {PROXY_PORT, MODEL_SERVER_PORT}
    for rule in rules:
        if "CidrIp" not in rule and "CidrIpv6" not in rule:
            continue
        assert rule.get("FromPort") not in serving, rule
        assert rule.get("ToPort") not in serving, rule
        assert rule.get("FromPort") == 443, rule
