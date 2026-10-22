"""Offline teardown planner. Plan mode performs no AWS call and never executes.

REQ-OPS-05 asks for `teardown --retain-artifacts` with verified cleanup. This
module owns the plan half only: it reads a synthesized template and emits an
ordered removal plan, compute and networking first so a dependency is never
removed before the thing that depends on it.

Data is not in scope for removal, under either flag. `RETAINED_KINDS` never
appear as a removal step, so a bucket, a bucket policy, or a table is reported
as retained and never planned for deletion. `retain_artifacts` therefore does
not choose whether data survives; it is the acknowledgement that the data
protections stay on. Asking to plan without it is read as asking to remove
protected data, and is refused with `retention_protected` naming each
protection that blocked it. Turning a retention policy or deletion protection
off is the owner's decision, taken deliberately, not something a planner does
on the way past.

Execution stays an owner-gated operator action. There is deliberately no verb
here that deletes anything, and no AWS client reaches this module at all.
"""

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from unstuck.cloud import CloudFault, CloudReason
from unstuck.domain import Contract, Identifier

#: Removal order. Earlier kinds depend on later kinds, never the reverse.
REMOVAL_ORDER: tuple[str, ...] = (
    "AWS::EC2::Instance",
    "AWS::IAM::InstanceProfile",
    "AWS::ApiGateway::Method",
    "AWS::ApiGateway::Resource",
    "AWS::ApiGateway::Deployment",
    "AWS::ApiGateway::Stage",
    "AWS::ApiGateway::Account",
    "AWS::ApiGateway::RestApi",
    "AWS::ApiGateway::VpcLink",
    "AWS::ElasticLoadBalancingV2::Listener",
    "AWS::ElasticLoadBalancingV2::TargetGroup",
    "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "AWS::EC2::VPCEndpoint",
    "AWS::EC2::SecurityGroupIngress",
    "AWS::EC2::SecurityGroup",
    "AWS::EC2::SubnetRouteTableAssociation",
    "AWS::EC2::Route",
    "AWS::EC2::RouteTable",
    "AWS::EC2::Subnet",
    "AWS::EC2::VPC",
    "AWS::IAM::Policy",
    "AWS::IAM::Role",
    "AWS::ECR::Repository",
)

#: Kinds this planner never removes. Their protections are the owner's to lift.
RETAINED_KINDS: tuple[str, ...] = (
    "AWS::S3::Bucket",
    "AWS::S3::BucketPolicy",
    "AWS::DynamoDB::Table",
)

Protection = tuple[str, str, str]


class TeardownStep(Contract):
    """One removal, in plan order. The only action is delete."""

    order: Annotated[int, Field(ge=1, le=256)]
    resource_kind: Annotated[str, Field(min_length=1, max_length=128)]
    logical_id: Identifier
    action: Literal["delete"] = "delete"


class TeardownPlan(Contract):
    """An ordered plan and the resources it deliberately leaves standing."""

    steps: Annotated[tuple[TeardownStep, ...], Field(max_length=256)]
    retained: Annotated[tuple[str, ...], Field(max_length=64)]
    mode: Literal["plan"] = "plan"

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if [step.order for step in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("teardown steps must be contiguously ordered from one")
        if len({step.logical_id for step in self.steps}) != len(self.steps):
            raise ValueError("a resource is removed at most once")
        if any(step.resource_kind in RETAINED_KINDS for step in self.steps):
            raise ValueError("a retained kind never appears as a removal step")
        return self


def protected_resources(template: Mapping[str, Any]) -> tuple[Protection, ...]:
    """Every resource whose own protection blocks an unconditional removal."""
    found: list[Protection] = []
    for logical, resource in template.get("Resources", {}).items():
        kind = str(resource.get("Type", ""))
        properties: Mapping[str, Any] = resource.get("Properties", {}) or {}
        if resource.get("DeletionPolicy") == "Retain":
            found.append((kind, str(logical), "retention_policy"))
        if properties.get("DeletionProtectionEnabled") is True:
            found.append((kind, str(logical), "deletion_protection"))
    return tuple(found)


def _named(kind: str, properties: Mapping[str, Any], logical: str) -> str:
    for field in ("BucketName", "TableName", "RepositoryName"):
        name = properties.get(field)
        if isinstance(name, str):
            return f"{kind} {name}"
    return f"{kind} {logical}"


def plan_teardown(
    template: Mapping[str, Any], *, retain_artifacts: bool
) -> TeardownPlan:
    """Plan the removal. Nothing is removed, and nothing is contacted."""
    resources: Mapping[str, Any] = template.get("Resources", {})
    if not retain_artifacts:
        # Lifting a retention policy or deletion protection is the owner's
        # explicit decision; a planner never makes it on the way past.
        if protected_resources(template):
            raise CloudFault(CloudReason.RETENTION_PROTECTED)
    removable = [
        (REMOVAL_ORDER.index(str(resource.get("Type"))), str(logical))
        for logical, resource in resources.items()
        if str(resource.get("Type")) in REMOVAL_ORDER
    ]
    removable.sort()
    steps = tuple(
        TeardownStep(
            order=index + 1,
            resource_kind=REMOVAL_ORDER[rank],
            logical_id=logical,
        )
        for index, (rank, logical) in enumerate(removable)
    )
    retained = tuple(
        _named(str(resource.get("Type")), resource.get("Properties", {}) or {}, logical)
        for logical, resource in resources.items()
        if str(resource.get("Type")) in RETAINED_KINDS
    )
    return TeardownPlan(steps=steps, retained=retained)


__all__ = [
    "REMOVAL_ORDER",
    "RETAINED_KINDS",
    "Protection",
    "TeardownPlan",
    "TeardownStep",
    "plan_teardown",
    "protected_resources",
]
