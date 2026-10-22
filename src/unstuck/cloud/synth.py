"""In-process CDK synthesis. No credential, no lookup, no network, no deploy.

Synthesis runs through `aws_cdk.App().synth()` inside this process. The account
and region are supplied explicitly, so no environment-agnostic stack, context
lookup, or `from_lookup` call can reach an account. Nothing here deploys,
bootstraps, or spends.

A synthesized template is design evidence, not deployment evidence. The policy
diff below compares what was synthesized against the role matrix of
`docs/spec/design.md` section 15, so a drift between the design and the
template is visible; agreement proves the template matches the plan, never that
a grant exists. REQ-SEC-10 and PF6 stay Pending.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from aws_cdk import App, Environment
from aws_cdk import assertions as cdk_assertions
from pydantic import Field, model_validator

from infra.roles import STACK_OWNED_ROLE_NAMES
from infra.stack import STACK_NAME, UnstuckStack
from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.config import (
    BucketName,
    CloudResourceConfig,
    SafeIdentifier,
    sensitive_text,
)
from unstuck.domain import Contract

#: A trust principal is an IAM role in this account and nothing else. The
#: account root, an IAM user, a group, a federated principal, and an
#: `sts` assumed-role ARN are all refused: each would either widen the trust
#: boundary beyond the designed role or name an identity the role matrix does
#: not describe. A role this stack creates is refused as well; see
#: `STACK_OWNED_ROLE_NAMES` and the `coherent` validator below.
IAM_ROLE_ARN = r"^arn:aws:iam::\d{12}:role(?:/[A-Za-z0-9+=,.@_-]{1,64}){1,8}$"


def role_name(arn: str) -> str:
    """The normalized IAM role name an ARN names, path excluded.

    IAM role names are account-unique and are not distinguished by case, so two
    ARNs that differ only by case, or only by path, name the same role. The
    normalized name is therefore the one comparable identity, and comparing
    whole ARN strings would let a case variant pass as a distinct principal.
    """
    return arn.rsplit("/", 1)[-1].lower()


#: Construct-id prefixes that identify each design.md section 15 role.
ROLE_PREFIXES: Mapping[str, str] = {
    "R1": "ControllerRuntimeRole",
    "R4": "DeciderHostRole",
    "R5": "DeployerRole",
    "R6": "LocalOperatorRole",
    "R7": "EvaluatorRole",
}


@dataclass(frozen=True)
class RoleExpectation:
    """The design.md section 15 row for one role, as service prefixes."""

    role_id: str
    allowed: frozenset[str]
    denied: frozenset[str]


#: design.md section 15, transcribed. `allowed` and `denied` are the service
#: prefixes each row grants and explicitly refuses.
#:
#: Two tightenings go beyond the written row and are recorded here so the diff
#: stays exact rather than approximate: `iam` appears in the denied set of R6
#: and R7 because neither principal has any reason to pass a role, and `s3`
#: appears in the denied set of R4 because the Decider host must not reach run
#: or receipt-evidence prefixes even though the row only names the buckets it
#: may read. A tightening is still a planned grant, not a verified one.
DESIGN_15_MATRIX: tuple[RoleExpectation, ...] = (
    RoleExpectation(
        role_id="R1",
        allowed=frozenset({"dynamodb", "s3", "bedrock", "logs", "ecr", "execute-api"}),
        denied=frozenset({"s3", "iam"}),
    ),
    RoleExpectation(
        role_id="R4",
        allowed=frozenset({"s3", "logs"}),
        denied=frozenset({"s3", "dynamodb", "bedrock", "secretsmanager", "ssm", "iam"}),
    ),
    RoleExpectation(
        role_id="R5",
        allowed=frozenset({"cloudformation", "iam"}),
        denied=frozenset({"cloudformation", "iam"}),
    ),
    RoleExpectation(
        role_id="R6",
        allowed=frozenset({"bedrock-agentcore", "dynamodb", "s3", "budgets"}),
        denied=frozenset({"s3", "execute-api", "dynamodb", "iam"}),
    ),
    RoleExpectation(
        role_id="R7",
        allowed=frozenset({"s3"}),
        denied=frozenset({"bedrock-agentcore", "secretsmanager", "iam", "dynamodb"}),
    ),
)


class SynthesisInputs(Contract):
    """Everything synthesis needs. Identifiers and versions only."""

    config: CloudResourceConfig
    holdout_bucket: BucketName
    vpc_cidr: Annotated[str, Field(pattern=r"^10\.\d{1,3}\.0\.0/16$")] = "10.80.0.0/16"
    decider_ami_id: Annotated[str, Field(pattern=r"^ami-[0-9a-f]{8,17}$")]
    decider_instance_type: Annotated[
        str, Field(pattern=r"^[a-z][a-z0-9]*\.[a-z0-9]+$")
    ] = "g5.xlarge"
    main_model_arn: Annotated[str, Field(pattern=r"^arn:aws:bedrock:[a-z0-9-]+:.*$")]
    baseline_model_arn: Annotated[
        str, Field(pattern=r"^arn:aws:bedrock:[a-z0-9-]+:.*$")
    ]
    operator_principal_arn: Annotated[str, Field(pattern=IAM_ROLE_ARN)]
    evaluator_principal_arn: Annotated[str, Field(pattern=IAM_ROLE_ARN)]
    deployer_principal_arn: Annotated[str, Field(pattern=IAM_ROLE_ARN)]
    #: The one dedicated CloudFormation execution role a deploy may pin. When
    #: absent, no execution role is passed and none is admitted anywhere.
    cloudformation_execution_role_arn: (
        Annotated[str, Field(pattern=IAM_ROLE_ARN)] | None
    ) = None
    stack_name: SafeIdentifier = STACK_NAME

    @model_validator(mode="after")
    def coherent(self) -> "SynthesisInputs":
        if self.holdout_bucket == self.config.artifact_bucket:
            raise ValueError("evaluator truth lives in its own bucket")
        if sensitive_text(self.holdout_bucket):
            raise ValueError("a bucket name must not advertise its contents")
        principals = (
            self.operator_principal_arn,
            self.evaluator_principal_arn,
            self.deployer_principal_arn,
        )
        names = tuple(role_name(arn) for arn in principals)
        if len(set(names)) != len(names):
            raise ValueError("operator, evaluator and deployer are distinct")
        execution = self.cloudformation_execution_role_arn
        if execution is not None and role_name(execution) in names:
            raise ValueError("the execution role is not a trust principal")
        supplied = (*principals, *((execution,) if execution is not None else ()))
        for arn in supplied:
            if arn.split(":")[4] != self.config.account_id:
                raise ValueError("every role lives in the declared account")
            # The stack creates these five roles. Trusting one of them to
            # assume a role this stack owns would widen the boundary to
            # whoever can already assume it.
            if role_name(arn) in STACK_OWNED_ROLE_NAMES:
                raise ValueError("a stack-owned role is not an external principal")
        return self


def build_app(inputs: SynthesisInputs, *, outdir: Path | None = None) -> App:
    """Build the app with an explicit environment. No lookup is possible."""
    validated = SynthesisInputs.model_validate(inputs)
    app = App(outdir=str(outdir) if outdir is not None else None)
    UnstuckStack(
        app,
        validated.stack_name,
        config=validated.config,
        holdout_bucket=validated.holdout_bucket,
        vpc_cidr=validated.vpc_cidr,
        decider_ami_id=validated.decider_ami_id,
        decider_instance_type=validated.decider_instance_type,
        main_model_arn=validated.main_model_arn,
        baseline_model_arn=validated.baseline_model_arn,
        operator_principal_arn=validated.operator_principal_arn,
        evaluator_principal_arn=validated.evaluator_principal_arn,
        deployer_principal_arn=validated.deployer_principal_arn,
        cloudformation_execution_role_arn=(validated.cloudformation_execution_role_arn),
        env=Environment(
            account=validated.config.account_id, region=validated.config.region
        ),
    )
    return app


def stack_for(inputs: SynthesisInputs, *, outdir: Path | None = None) -> UnstuckStack:
    app = build_app(inputs, outdir=outdir)
    stack = app.node.find_child(SynthesisInputs.model_validate(inputs).stack_name)
    if not isinstance(stack, UnstuckStack):
        raise CloudFault(CloudReason.SYNTHESIS_INVALID)
    return stack


def template_for(
    inputs: SynthesisInputs, *, outdir: Path | None = None
) -> dict[str, Any]:
    """Synthesize in process and return the CloudFormation template."""
    rendered = cdk_assertions.Template.from_stack(stack_for(inputs, outdir=outdir))
    body: dict[str, Any] = dict(rendered.to_json())
    return body


def synthesize(inputs: SynthesisInputs, outdir: Path) -> Path:
    """Write the cloud assembly to `outdir` and return the directory."""
    assembly = build_app(inputs, outdir=outdir).synth()
    return Path(assembly.directory)


def _statements(template: Mapping[str, Any], prefix: str) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for logical, resource in template.get("Resources", {}).items():
        if not logical.startswith(prefix):
            continue
        if resource.get("Type") not in {"AWS::IAM::Policy", "AWS::IAM::Role"}:
            continue
        documents = [resource.get("Properties", {}).get("PolicyDocument")]
        for inline in resource.get("Properties", {}).get("Policies", []) or []:
            documents.append(inline.get("PolicyDocument"))
        for document in documents:
            if isinstance(document, dict):
                collected.extend(
                    item
                    for item in document.get("Statement", [])
                    if isinstance(item, dict)
                )
    return collected


def _service_prefixes(statement: Mapping[str, Any]) -> set[str]:
    actions = statement.get("Action", [])
    if isinstance(actions, str):
        actions = [actions]
    return {
        str(action).split(":", 1)[0]
        for action in actions
        if isinstance(action, str) and ":" in action
    }


def policy_diff(template: Mapping[str, Any]) -> dict[str, dict[str, list[str]]]:
    """Diff the synthesized role policies against design.md section 15."""
    report: dict[str, dict[str, list[str]]] = {}
    for expectation in DESIGN_15_MATRIX:
        prefix = ROLE_PREFIXES[expectation.role_id]
        allowed: set[str] = set()
        denied: set[str] = set()
        for statement in _statements(template, prefix):
            target = denied if statement.get("Effect") == "Deny" else allowed
            target |= _service_prefixes(statement)
        report[expectation.role_id] = {
            "unexpected_allow": sorted(allowed - expectation.allowed),
            "missing_allow": sorted(expectation.allowed - allowed),
            "unexpected_deny": sorted(denied - expectation.denied),
            "missing_deny": sorted(expectation.denied - denied),
        }
    return report


__all__ = [
    "DESIGN_15_MATRIX",
    "IAM_ROLE_ARN",
    "ROLE_PREFIXES",
    "RoleExpectation",
    "SynthesisInputs",
    "build_app",
    "policy_diff",
    "role_name",
    "stack_for",
    "synthesize",
    "template_for",
]
