"""Tests for the offline teardown planner.

Plan mode is pure: it reads a synthesized template and emits an ordered removal
plan. It performs no AWS call, and it never executes. Teardown execution stays
an owner-gated operator action and is not implemented in this unit (REQ-OPS-05).
"""

import inspect
from pathlib import Path
from typing import Any

import pytest

from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.synth import SynthesisInputs, template_for
from unstuck.cloud.teardown import (
    REMOVAL_ORDER,
    RETAINED_KINDS,
    TeardownPlan,
    plan_teardown,
)

from .conftest import SYNTHETIC_ARTIFACT_BUCKET, SYNTHETIC_HOLDOUT_BUCKET


@pytest.fixture
def template(inputs: SynthesisInputs, tmp_path: Path) -> dict[str, Any]:
    return template_for(inputs, outdir=tmp_path / "cdk.out")


def test_the_plan_removes_compute_and_networking_in_order_offline(
    template: dict[str, Any],
) -> None:
    plan = plan_teardown(template, retain_artifacts=True)
    assert isinstance(plan, TeardownPlan)
    assert plan.mode == "plan"
    assert [step.order for step in plan.steps] == list(range(1, len(plan.steps) + 1))
    kinds = [step.resource_kind for step in plan.steps]
    assert "AWS::EC2::Instance" in kinds
    assert "AWS::EC2::VPC" in kinds
    assert "AWS::ApiGateway::RestApi" in kinds
    assert kinds.index("AWS::EC2::Instance") < kinds.index("AWS::EC2::VPC")
    assert kinds.index("AWS::ElasticLoadBalancingV2::LoadBalancer") < kinds.index(
        "AWS::EC2::Subnet"
    )
    assert all(step.action == "delete" for step in plan.steps)


def test_the_plan_follows_the_declared_removal_order_offline(
    template: dict[str, Any],
) -> None:
    plan = plan_teardown(template, retain_artifacts=True)
    ranks = [REMOVAL_ORDER.index(step.resource_kind) for step in plan.steps]
    assert ranks == sorted(ranks)
    assert set(plan.steps and ranks) <= set(range(len(REMOVAL_ORDER)))


def test_retain_artifacts_keeps_both_buckets_and_both_tables_offline(
    template: dict[str, Any],
) -> None:
    plan = plan_teardown(template, retain_artifacts=True)
    assert not any(step.resource_kind in RETAINED_KINDS for step in plan.steps)
    retained = " ".join(plan.retained)
    assert SYNTHETIC_ARTIFACT_BUCKET in retained
    assert SYNTHETIC_HOLDOUT_BUCKET in retained
    assert len(plan.retained) >= 4


def test_the_planner_refuses_to_remove_a_protected_resource_offline(
    template: dict[str, Any],
) -> None:
    with pytest.raises(CloudFault) as raised:
        plan_teardown(template, retain_artifacts=False)
    assert raised.value.reason is CloudReason.RETENTION_PROTECTED


def test_the_planner_reports_which_protections_blocked_it_offline(
    template: dict[str, Any],
) -> None:
    from unstuck.cloud.teardown import protected_resources

    protections = protected_resources(template)
    assert protections
    kinds = {kind for kind, _, _ in protections}
    assert "AWS::S3::Bucket" in kinds
    assert "AWS::DynamoDB::Table" in kinds
    reasons = {reason for _, _, reason in protections}
    assert reasons == {"retention_policy", "deletion_protection"}


def test_plan_mode_performs_no_aws_call_offline() -> None:
    import ast

    signature = inspect.signature(plan_teardown)
    assert set(signature.parameters) == {"template", "retain_artifacts"}
    module = inspect.getmodule(plan_teardown)
    assert module is not None
    tree = ast.parse(inspect.getsource(module))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(
        name.startswith(("boto3", "botocore", "aws_cdk")) for name in imported
    ), imported
    identifiers = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for forbidden in ("client", "delete_item", "delete_bucket", "destroy"):
        assert not any(forbidden in name for name in identifiers), forbidden


def test_teardown_execution_is_not_implemented_in_this_unit_offline() -> None:
    import unstuck.cloud.teardown as planner

    surface = {name for name in dir(planner) if not name.startswith("_")}
    assert "execute_teardown" not in surface
    assert "run_teardown" not in surface
    assert "apply" not in surface
