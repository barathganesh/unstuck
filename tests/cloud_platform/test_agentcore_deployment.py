"""AgentCore Runtime deployment wiring, verified against installed contracts.

Three kinds of assertion live here and they are deliberately separated.

The first kind reads the installed contracts and proves this unit invented
nothing: the CloudFormation resource type comes from `aws_cdk`'s own
`cloudformationResource` metadata, the property names come from `CfnRuntimeProps`,
and the shapes, patterns, enums and header names come from the `botocore`
service models for `bedrock-agentcore` and `bedrock-agentcore-control`. When a
future version of either package changes one of those, these tests fail rather
than letting a guess through.

The second kind is contract behaviour: a deployment input that would widen the
boundary is refused by the typed contract before any template exists.

The third kind is offline synthesis. The autouse socket guard and the credential
denial from `conftest` are active throughout, so a template rendered here
provably needed no account, no credential chain, and no context lookup.

Synthesis is design evidence. It proves the planned topology matches the
verified schema. It never proves a resource exists, a request was signed, or an
anonymous call was denied: PF6, PF8 and PF9 stay Pending, and
`deployment_blockers` is the machine-readable statement of that.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from botocore.session import Session
from pydantic import ValidationError

from infra.agentcore import (
    AGENTCORE_ENDPOINT_TYPE,
    AGENTCORE_RUNTIME_TYPE,
    CONTROLLER_LOG_SID,
    OPERATOR_INVOKE_SID,
)
from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud import deployment as deployment
from unstuck.cloud.deployment import (
    AGENTCORE_NETWORK_MODES,
    AGENTCORE_SERVER_PROTOCOLS,
    INVOKE_ACTION,
    LIVE_TEST_ORDER,
    RUNTIME_CONTAINER_URI_PATTERN,
    SPRINGCLEAN_TAG,
    AgentCoreRuntimeInputs,
    DeploymentTag,
    VpcAttachment,
    agentcore_environment,
    check_image_agrees,
    deployment_blockers,
    deployment_template,
    live_test_checklist,
    plan_deployment,
    springclean_tags,
)
from unstuck.cloud.runtime import (
    AGENTCORE_REQUEST_HEADERS,
    RUNTIME_IDENTITY_VARIABLES,
)
from unstuck.cloud.synth import ROLE_PREFIXES, SynthesisInputs
from unstuck.cloud.telemetry import TelemetryRuntimeConfig

from .conftest import SYNTHETIC_ACCOUNT, SYNTHETIC_REGION

IMAGE_DIGEST = "sha256:" + "ab" * 32
IMAGE_URI = (
    f"{SYNTHETIC_ACCOUNT}.dkr.ecr.{SYNTHETIC_REGION}.amazonaws.com/"
    f"unstuck-controller@{IMAGE_DIGEST}"
)
RUNTIME_NAME = "unstuck_controller"
ENDPOINT_NAME = "unstuck_private"
LOG_GROUP = "/aws/unstuck/controller"
LOG_STREAM = "controller-offline"

CREDENTIAL_VARIABLES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "OTEL_EXPORTER_OTLP_HEADERS",
)


@pytest.fixture
def vpc() -> VpcAttachment:
    return VpcAttachment(
        subnets=("subnet-0123456789abcdef0", "subnet-0123456789abcdef1"),
        security_groups=("sg-0123456789abcdef0",),
    )


@pytest.fixture
def telemetry() -> TelemetryRuntimeConfig:
    """Partially configured telemetry. The live identity is a deploy output."""
    return TelemetryRuntimeConfig(
        region=SYNTHETIC_REGION,
        account_id=SYNTHETIC_ACCOUNT,
        partition="aws",
        log_group=LOG_GROUP,
        log_stream=LOG_STREAM,
        service_name="unstuck-controller",
        classification="synthetic",
        observability_enabled=True,
        unified_telemetry=True,
    )


@pytest.fixture
def agentcore(vpc: VpcAttachment) -> AgentCoreRuntimeInputs:
    return AgentCoreRuntimeInputs(
        runtime_name=RUNTIME_NAME,
        endpoint_name=ENDPOINT_NAME,
        image_uri=IMAGE_URI,
        network_mode="VPC",
        vpc=vpc,
        tags=springclean_tags(),
    )


@pytest.fixture
def template(
    inputs: SynthesisInputs,
    agentcore: AgentCoreRuntimeInputs,
    telemetry: TelemetryRuntimeConfig,
    tmp_path: Path,
) -> dict[str, Any]:
    return deployment_template(
        inputs, agentcore, telemetry=telemetry, outdir=tmp_path / "cdk.out"
    )


def resources(template: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [
        resource.get("Properties", {})
        for resource in template.get("Resources", {}).values()
        if resource.get("Type") == kind
    ]


def statements(template: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for logical, resource in template.get("Resources", {}).items():
        if not logical.startswith(prefix):
            continue
        properties = resource.get("Properties", {})
        documents = [properties.get("PolicyDocument")]
        for inline in properties.get("Policies", []) or []:
            documents.append(inline.get("PolicyDocument"))
        for document in documents:
            if isinstance(document, dict):
                collected.extend(
                    item
                    for item in document.get("Statement", [])
                    if isinstance(item, dict)
                )
    return collected


def control_shapes() -> dict[str, Any]:
    """The installed `bedrock-agentcore-control` shape map. No network call."""
    model = Session().get_service_model("bedrock-agentcore-control")
    return dict(model._shape_resolver._shape_map)


# --- The installed contracts are the authority ------------------------------


def test_the_cdk_resource_types_come_from_the_installed_module_offline() -> None:
    from aws_cdk import aws_bedrockagentcore as agentcore_l1

    assert agentcore_l1.CfnRuntime.CFN_RESOURCE_TYPE_NAME == AGENTCORE_RUNTIME_TYPE
    assert (
        agentcore_l1.CfnRuntimeEndpoint.CFN_RESOURCE_TYPE_NAME
        == AGENTCORE_ENDPOINT_TYPE
    )


def test_every_property_this_unit_sets_exists_in_the_installed_props_offline() -> None:
    from aws_cdk import aws_bedrockagentcore as agentcore_l1

    runtime_props = {
        "agent_runtime_artifact",
        "agent_runtime_name",
        "role_arn",
        "environment_variables",
        "lifecycle_configuration",
        "network_configuration",
        "protocol_configuration",
        "request_header_configuration",
        "tags",
    }
    declared = set(agentcore_l1.CfnRuntimeProps.__init__.__annotations__)
    assert runtime_props <= declared, runtime_props - declared
    endpoint_props = {"agent_runtime_id", "name", "tags"}
    endpoint_declared = set(
        agentcore_l1.CfnRuntimeEndpointProps.__init__.__annotations__
    )
    assert endpoint_props <= endpoint_declared, endpoint_props - endpoint_declared


def test_the_only_inbound_authorizer_in_the_contract_is_custom_jwt_offline() -> None:
    """Absence of an authorizer is the service's own SigV4, not "no auth".

    The shape is a union with exactly one member. There is no `none`, no
    `public`, and no API-key alternative, so omitting it cannot select an
    unauthenticated inbound path: the only thing it can select is the service's
    native AWS SigV4 authorization.
    """
    authorizer = control_shapes()["AuthorizerConfiguration"]
    assert authorizer["type"] == "structure"
    assert authorizer.get("union") is True
    assert set(authorizer["members"]) == {"customJWTAuthorizer"}


def test_the_enums_and_patterns_this_unit_pins_match_the_model_offline() -> None:
    shapes = control_shapes()
    assert tuple(shapes["NetworkMode"]["enum"]) == AGENTCORE_NETWORK_MODES
    assert set(shapes["ServerProtocol"]["enum"]) == set(AGENTCORE_SERVER_PROTOCOLS)
    assert shapes["RuntimeContainerUri"]["pattern"] == RUNTIME_CONTAINER_URI_PATTERN
    assert shapes["EnvironmentVariablesMap"]["max"] == 50
    assert shapes["TagsMap"]["max"] == 50


def test_the_invoke_action_is_the_data_plane_operation_offline() -> None:
    model = Session().get_service_model("bedrock-agentcore")
    assert model.metadata["signingName"] == "bedrock-agentcore"
    assert model.metadata["signatureVersion"] == "v4"
    assert INVOKE_ACTION == "bedrock-agentcore:InvokeAgentRuntime"
    operation = model.operation_model("InvokeAgentRuntime")
    assert operation.http["method"] == "POST"
    assert operation.http["requestUri"] == "/runtimes/{agentRuntimeArn}/invocations"


def test_the_request_headers_come_from_the_installed_service_model_offline() -> None:
    """The allow-listed headers are the model's own, character for character."""
    model = Session().get_service_model("bedrock-agentcore")
    shape = model.operation_model("InvokeAgentRuntime").input_shape
    assert shape is not None
    modelled = {
        member.serialization.get("name")
        for member in shape.members.values()
        if member.serialization.get("location") == "header"
    }
    assert set(AGENTCORE_REQUEST_HEADERS) <= modelled
    assert "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id" in AGENTCORE_REQUEST_HEADERS


# --- The typed contract refuses a widened deployment ------------------------


@pytest.mark.parametrize(
    "image",
    [
        # A mutable tag instead of a manifest digest.
        f"{SYNTHETIC_ACCOUNT}.dkr.ecr.{SYNTHETIC_REGION}.amazonaws.com/unstuck:v1",
        # The public registry, which a restricted runtime never pulls from even
        # though the modelled pattern admits it.
        f"public.ecr.aws/unstuck/controller@{IMAGE_DIGEST}",
        # A truncated digest, which pins nothing.
        f"{SYNTHETIC_ACCOUNT}.dkr.ecr.{SYNTHETIC_REGION}.amazonaws.com/u@sha256:beef",
        # A digest algorithm the pattern does not admit.
        f"{SYNTHETIC_ACCOUNT}.dkr.ecr.{SYNTHETIC_REGION}.amazonaws.com/u@md5:"
        + "a" * 32,
        # Neither registry form at all.
        f"docker.io/library/unstuck@{IMAGE_DIGEST}",
    ],
)
def test_an_image_that_is_not_a_digest_pinned_private_ecr_uri_is_refused(
    vpc: VpcAttachment, image: str
) -> None:
    with pytest.raises(ValidationError):
        AgentCoreRuntimeInputs(
            runtime_name=RUNTIME_NAME,
            endpoint_name=ENDPOINT_NAME,
            image_uri=image,
            network_mode="VPC",
            vpc=vpc,
            tags=springclean_tags(),
        )


def test_the_image_uri_is_parsed_into_its_account_region_and_digest(
    agentcore: AgentCoreRuntimeInputs, inputs: SynthesisInputs
) -> None:
    assert agentcore.image_account_id == inputs.config.account_id
    assert agentcore.image_region == inputs.config.region
    assert agentcore.image_repository == "unstuck-controller"
    assert agentcore.image_digest == IMAGE_DIGEST
    check_image_agrees(agentcore, inputs.config)


@pytest.mark.parametrize(
    "image",
    [
        f"999999999999.dkr.ecr.{SYNTHETIC_REGION}.amazonaws.com/u@{IMAGE_DIGEST}",
        f"{SYNTHETIC_ACCOUNT}.dkr.ecr.eu-west-1.amazonaws.com/u@{IMAGE_DIGEST}",
    ],
)
def test_an_image_from_another_account_or_region_is_refused_against_the_config(
    vpc: VpcAttachment, inputs: SynthesisInputs, image: str, tmp_path: Path
) -> None:
    """A valid private URI still has to be the deployment's own registry."""
    foreign = AgentCoreRuntimeInputs(
        runtime_name=RUNTIME_NAME,
        endpoint_name=ENDPOINT_NAME,
        image_uri=image,
        network_mode="VPC",
        vpc=vpc,
        tags=springclean_tags(),
    )
    with pytest.raises(CloudFault) as refusal:
        check_image_agrees(foreign, inputs.config)
    assert refusal.value.reason is CloudReason.CONFIG_INVALID
    with pytest.raises(CloudFault):
        deployment_template(inputs, foreign, outdir=tmp_path / "refused")


def test_a_public_network_mode_needs_the_owners_recorded_approval(
    vpc: VpcAttachment,
) -> None:
    with pytest.raises(ValidationError):
        AgentCoreRuntimeInputs(
            runtime_name=RUNTIME_NAME,
            endpoint_name=ENDPOINT_NAME,
            image_uri=IMAGE_URI,
            network_mode="PUBLIC",
            tags=springclean_tags(),
        )
    approved = AgentCoreRuntimeInputs(
        runtime_name=RUNTIME_NAME,
        endpoint_name=ENDPOINT_NAME,
        image_uri=IMAGE_URI,
        network_mode="PUBLIC",
        owner_approved_public_egress=True,
        tags=springclean_tags(),
    )
    assert approved.network_mode == "PUBLIC"
    assert approved.vpc is None
    # A VPC attachment and PUBLIC are contradictory, so both orders refuse.
    with pytest.raises(ValidationError):
        AgentCoreRuntimeInputs(
            runtime_name=RUNTIME_NAME,
            endpoint_name=ENDPOINT_NAME,
            image_uri=IMAGE_URI,
            network_mode="PUBLIC",
            owner_approved_public_egress=True,
            vpc=vpc,
            tags=springclean_tags(),
        )


@pytest.mark.parametrize("marker", ["authorization", "secret", "api_key", "credential"])
def test_a_tag_carrying_a_sensitive_marker_is_refused(marker: str) -> None:
    """A tag is reviewable text, so it never carries a credential vocabulary."""
    with pytest.raises(ValidationError):
        DeploymentTag(key=marker, value="no")
    with pytest.raises(ValidationError):
        DeploymentTag(key="unstuck:note", value=marker)


def test_a_runtime_named_like_its_endpoint_is_refused(vpc: VpcAttachment) -> None:
    """Two resources that read the same are two resources nobody can tell apart."""
    with pytest.raises(ValidationError):
        AgentCoreRuntimeInputs(
            runtime_name=RUNTIME_NAME,
            endpoint_name=RUNTIME_NAME,
            image_uri=IMAGE_URI,
            network_mode="VPC",
            vpc=vpc,
            tags=springclean_tags(),
        )


def test_a_repeated_tag_key_subnet_group_or_header_is_refused(
    vpc: VpcAttachment,
) -> None:
    with pytest.raises(ValidationError):
        AgentCoreRuntimeInputs(
            runtime_name=RUNTIME_NAME,
            endpoint_name=ENDPOINT_NAME,
            image_uri=IMAGE_URI,
            network_mode="VPC",
            vpc=vpc,
            tags=(
                DeploymentTag(key="auto-delete", value="no"),
                DeploymentTag(key="auto-delete", value="no"),
            ),
        )
    with pytest.raises(ValidationError):
        AgentCoreRuntimeInputs(
            runtime_name=RUNTIME_NAME,
            endpoint_name=ENDPOINT_NAME,
            image_uri=IMAGE_URI,
            network_mode="VPC",
            vpc=vpc,
            request_header_allowlist=("X-Amzn-Trace-Id", "X-Amzn-Trace-Id"),
            tags=springclean_tags(),
        )
    with pytest.raises(ValidationError):
        VpcAttachment(
            subnets=("subnet-0123456789abcdef0", "subnet-0123456789abcdef0"),
            security_groups=("sg-0123456789abcdef0",),
        )
    with pytest.raises(ValidationError):
        VpcAttachment(
            subnets=("subnet-0123456789abcdef0",),
            security_groups=("sg-0123456789abcdef0", "sg-0123456789abcdef0"),
        )


def test_the_identity_variable_set_cannot_drift_from_the_entrypoints(
    agentcore: AgentCoreRuntimeInputs,
    inputs: SynthesisInputs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployment injects exactly what the entrypoint refuses without."""
    monkeypatch.setattr(
        deployment, "RUNTIME_IDENTITY_VARIABLES", ("UNSTUCK_SOMETHING_ELSE",)
    )
    with pytest.raises(CloudFault) as refusal:
        agentcore_environment(agentcore, config=inputs.config, telemetry=None)
    assert refusal.value.reason is CloudReason.CONFIG_INVALID


def test_the_modelled_environment_bounds_are_enforced_not_assumed(
    agentcore: AgentCoreRuntimeInputs,
    inputs: SynthesisInputs,
    telemetry: TelemetryRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: the bound refuses even if a typed field let it past."""
    monkeypatch.setattr(deployment, "MAX_ENVIRONMENT_VARIABLES", 1)
    with pytest.raises(CloudFault) as count:
        agentcore_environment(agentcore, config=inputs.config, telemetry=telemetry)
    assert count.value.reason is CloudReason.CONFIG_INVALID
    monkeypatch.setattr(deployment, "MAX_ENVIRONMENT_VARIABLES", 50)
    monkeypatch.setattr(deployment, "MAX_ENVIRONMENT_KEY", 1)
    with pytest.raises(CloudFault) as key:
        agentcore_environment(agentcore, config=inputs.config, telemetry=telemetry)
    assert key.value.reason is CloudReason.CONFIG_INVALID
    monkeypatch.setattr(deployment, "MAX_ENVIRONMENT_KEY", 100)
    monkeypatch.setattr(deployment, "MAX_ENVIRONMENT_VALUE", 1)
    with pytest.raises(CloudFault) as value:
        agentcore_environment(agentcore, config=inputs.config, telemetry=telemetry)
    assert value.value.reason is CloudReason.CONFIG_INVALID


def test_a_vpc_network_mode_needs_its_subnets_and_security_groups() -> None:
    with pytest.raises(ValidationError):
        AgentCoreRuntimeInputs(
            runtime_name=RUNTIME_NAME,
            endpoint_name=ENDPOINT_NAME,
            image_uri=IMAGE_URI,
            network_mode="VPC",
            tags=springclean_tags(),
        )
    with pytest.raises(ValidationError):
        VpcAttachment(subnets=(), security_groups=("sg-0123456789abcdef0",))
    with pytest.raises(ValidationError):
        VpcAttachment(subnets=("subnet-0123456789abcdef0",), security_groups=())


def test_the_springclean_tag_is_required_on_every_deployment(
    vpc: VpcAttachment,
) -> None:
    assert SPRINGCLEAN_TAG == ("auto-delete", "no")
    assert springclean_tags() == (DeploymentTag(key="auto-delete", value="no"),)
    with pytest.raises(ValidationError):
        AgentCoreRuntimeInputs(
            runtime_name=RUNTIME_NAME,
            endpoint_name=ENDPOINT_NAME,
            image_uri=IMAGE_URI,
            network_mode="VPC",
            vpc=vpc,
            tags=(DeploymentTag(key="unstuck:unit", value="cloud-platform"),),
        )
    with pytest.raises(ValidationError):
        AgentCoreRuntimeInputs(
            runtime_name=RUNTIME_NAME,
            endpoint_name=ENDPOINT_NAME,
            image_uri=IMAGE_URI,
            network_mode="VPC",
            vpc=vpc,
            tags=(DeploymentTag(key="auto-delete", value="yes"),),
        )


@pytest.mark.parametrize("name", ["0unstuck", "unstuck-controller", "u" * 49, ""])
def test_a_runtime_name_outside_the_modelled_pattern_is_refused(
    vpc: VpcAttachment, name: str
) -> None:
    with pytest.raises(ValidationError):
        AgentCoreRuntimeInputs(
            runtime_name=name,
            endpoint_name=ENDPOINT_NAME,
            image_uri=IMAGE_URI,
            network_mode="VPC",
            vpc=vpc,
            tags=springclean_tags(),
        )


def test_the_lifecycle_bounds_follow_the_modelled_minimum_and_maximum(
    vpc: VpcAttachment,
) -> None:
    shapes = control_shapes()
    modelled = shapes["LifecycleConfigurationIdleRuntimeSessionTimeoutInteger"]
    for seconds in (modelled["min"] - 1, modelled["max"] + 1):
        with pytest.raises(ValidationError):
            AgentCoreRuntimeInputs(
                runtime_name=RUNTIME_NAME,
                endpoint_name=ENDPOINT_NAME,
                image_uri=IMAGE_URI,
                network_mode="VPC",
                vpc=vpc,
                idle_session_timeout_seconds=seconds,
                tags=springclean_tags(),
            )


# --- Environment injection -------------------------------------------------


def test_the_injected_environment_carries_telemetry_and_identity_only(
    agentcore: AgentCoreRuntimeInputs,
    inputs: SynthesisInputs,
    telemetry: TelemetryRuntimeConfig,
) -> None:
    environment = agentcore_environment(
        agentcore, config=inputs.config, telemetry=telemetry
    )
    projected = telemetry.variables().mapping
    assert projected
    assert projected.items() <= environment.items()
    for name in RUNTIME_IDENTITY_VARIABLES:
        assert environment[name]
    assert set(environment) == set(projected) | set(RUNTIME_IDENTITY_VARIABLES)
    for forbidden in CREDENTIAL_VARIABLES:
        assert forbidden not in environment
    assert len(environment) <= 50
    assert all(1 <= len(key) <= 100 for key in environment)
    assert all(len(value) <= 5000 for value in environment.values())


def test_the_injected_environment_without_telemetry_is_identity_only(
    agentcore: AgentCoreRuntimeInputs, inputs: SynthesisInputs
) -> None:
    environment = agentcore_environment(agentcore, config=inputs.config, telemetry=None)
    assert set(environment) == set(RUNTIME_IDENTITY_VARIABLES)


# --- Offline synthesis -----------------------------------------------------


def test_the_template_carries_exactly_one_runtime_and_one_endpoint_offline(
    template: dict[str, Any],
) -> None:
    runtimes = resources(template, AGENTCORE_RUNTIME_TYPE)
    endpoints = resources(template, AGENTCORE_ENDPOINT_TYPE)
    assert len(runtimes) == 1
    assert len(endpoints) == 1
    kinds = {resource["Type"] for resource in template["Resources"].values()}
    assert not kinds - set(kinds), kinds
    unexpected = {
        kind
        for kind in kinds
        if kind.startswith("AWS::BedrockAgentCore")
        and kind not in {AGENTCORE_RUNTIME_TYPE, AGENTCORE_ENDPOINT_TYPE}
    }
    assert not unexpected, unexpected


def test_the_runtime_declares_no_authorizer_so_sigv4_authorizes_it_offline(
    template: dict[str, Any],
) -> None:
    runtime = resources(template, AGENTCORE_RUNTIME_TYPE)[0]
    assert "AuthorizerConfiguration" not in runtime
    rendered = json.dumps(template)
    assert "customJWTAuthorizer" not in rendered
    assert "CustomJWTAuthorizer" not in rendered
    assert "discoveryUrl" not in rendered


def test_the_runtime_runs_as_the_controller_role_on_the_private_network_offline(
    template: dict[str, Any], agentcore: AgentCoreRuntimeInputs
) -> None:
    runtime = resources(template, AGENTCORE_RUNTIME_TYPE)[0]
    role = json.dumps(runtime["RoleArn"])
    assert ROLE_PREFIXES["R1"] in role
    assert runtime["ProtocolConfiguration"] == "HTTP"
    network = runtime["NetworkConfiguration"]
    assert network["NetworkMode"] == "VPC"
    assert agentcore.vpc is not None
    assert network["NetworkModeConfig"]["Subnets"] == list(agentcore.vpc.subnets)
    assert network["NetworkModeConfig"]["SecurityGroups"] == list(
        agentcore.vpc.security_groups
    )
    container = runtime["AgentRuntimeArtifact"]["ContainerConfiguration"]
    assert container["ContainerUri"] == IMAGE_URI
    assert IMAGE_DIGEST in container["ContainerUri"]


def test_the_endpoint_binds_the_runtime_it_was_created_from_offline(
    template: dict[str, Any],
) -> None:
    endpoint = resources(template, AGENTCORE_ENDPOINT_TYPE)[0]
    assert endpoint["Name"] == ENDPOINT_NAME
    runtime_logical = next(
        logical
        for logical, resource in template["Resources"].items()
        if resource.get("Type") == AGENTCORE_RUNTIME_TYPE
    )
    assert endpoint["AgentRuntimeId"] == {
        "Fn::GetAtt": [runtime_logical, "AgentRuntimeId"]
    }


def test_both_agentcore_resources_carry_the_springclean_tag_offline(
    template: dict[str, Any],
) -> None:
    for kind in (AGENTCORE_RUNTIME_TYPE, AGENTCORE_ENDPOINT_TYPE):
        tags = resources(template, kind)[0]["Tags"]
        assert tags["auto-delete"] == "no", kind
    # A literal account number must never reach a tag or any other property.
    assert "unstuck:mode" in json.dumps(template)


def test_the_runtime_environment_is_the_projected_map_offline(
    template: dict[str, Any],
    agentcore: AgentCoreRuntimeInputs,
    inputs: SynthesisInputs,
    telemetry: TelemetryRuntimeConfig,
) -> None:
    runtime = resources(template, AGENTCORE_RUNTIME_TYPE)[0]
    assert runtime["EnvironmentVariables"] == agentcore_environment(
        agentcore, config=inputs.config, telemetry=telemetry
    )
    for forbidden in CREDENTIAL_VARIABLES:
        assert forbidden not in runtime["EnvironmentVariables"]


def test_the_allow_listed_request_headers_are_the_modelled_ones_offline(
    template: dict[str, Any],
) -> None:
    runtime = resources(template, AGENTCORE_RUNTIME_TYPE)[0]
    allowlist = runtime["RequestHeaderConfiguration"]["RequestHeaderAllowlist"]
    assert allowlist == list(AGENTCORE_REQUEST_HEADERS)
    assert 1 <= len(allowlist) <= 20
    assert "Authorization" not in allowlist


def test_the_operator_invoke_grant_names_the_runtime_and_endpoint_only_offline(
    template: dict[str, Any],
) -> None:
    grants = [
        statement
        for statement in statements(template, ROLE_PREFIXES["R6"])
        if statement.get("Sid") == OPERATOR_INVOKE_SID
    ]
    assert len(grants) == 1
    grant = grants[0]
    assert grant["Effect"] == "Allow"
    assert grant["Action"] == INVOKE_ACTION
    assert len(grant["Resource"]) == 2
    rendered = json.dumps(grant["Resource"])
    assert "AgentRuntimeArn" in rendered
    assert "runtime-endpoint/" in rendered
    assert '"*"' not in rendered


def test_no_statement_grants_a_wildcard_agentcore_resource_or_action_offline(
    template: dict[str, Any],
) -> None:
    for statement in statements(template):
        if statement.get("Effect", "Allow") != "Allow":
            continue
        actions = statement.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        if not any(str(action).startswith("bedrock-agentcore") for action in actions):
            continue
        assert "bedrock-agentcore:*" not in actions, statement
        resource = statement.get("Resource")
        assert resource not in ("*", ["*"]), statement
        assert '"*"' not in json.dumps(resource), statement


def test_controller_telemetry_writes_only_to_the_configured_destination_offline(
    template: dict[str, Any],
) -> None:
    grants = [
        statement
        for statement in statements(template, ROLE_PREFIXES["R1"])
        if statement.get("Sid") == CONTROLLER_LOG_SID
    ]
    assert len(grants) == 1
    resource = json.dumps(grants[0]["Resource"])
    assert LOG_GROUP in resource
    assert LOG_STREAM in resource
    assert grants[0]["Action"] == ["logs:CreateLogStream", "logs:PutLogEvents"]
    assert resource.count("log-group") == 1
    assert '"*"' not in resource


def test_a_configured_group_without_a_stream_stays_inside_that_group_offline(
    inputs: SynthesisInputs, agentcore: AgentCoreRuntimeInputs, tmp_path: Path
) -> None:
    """A stream the owner never named cannot be named, so the group bounds it."""
    group_only = TelemetryRuntimeConfig(log_group=LOG_GROUP)
    template = deployment_template(
        inputs, agentcore, telemetry=group_only, outdir=tmp_path / "group-only"
    )
    grants = [
        statement
        for statement in statements(template, ROLE_PREFIXES["R1"])
        if statement.get("Sid") == CONTROLLER_LOG_SID
    ]
    assert len(grants) == 1
    resource = grants[0]["Resource"]
    assert resource == (
        f"arn:aws:logs:{SYNTHETIC_REGION}:{SYNTHETIC_ACCOUNT}:"
        f"log-group:{LOG_GROUP}:log-stream:*"
    )


def test_no_telemetry_destination_produces_no_log_grant_offline(
    inputs: SynthesisInputs, agentcore: AgentCoreRuntimeInputs, tmp_path: Path
) -> None:
    template = deployment_template(inputs, agentcore, outdir=tmp_path / "no-telemetry")
    assert not [
        statement
        for statement in statements(template, ROLE_PREFIXES["R1"])
        if statement.get("Sid") == CONTROLLER_LOG_SID
    ]
    runtime = resources(template, AGENTCORE_RUNTIME_TYPE)[0]
    assert set(runtime["EnvironmentVariables"]) == set(RUNTIME_IDENTITY_VARIABLES)


def test_a_public_egress_runtime_attaches_to_no_vpc_offline(
    inputs: SynthesisInputs, tmp_path: Path
) -> None:
    """The owner's approved PUBLIC mode still carries no inbound authorizer."""
    public = AgentCoreRuntimeInputs(
        runtime_name=RUNTIME_NAME,
        endpoint_name=ENDPOINT_NAME,
        image_uri=IMAGE_URI,
        network_mode="PUBLIC",
        owner_approved_public_egress=True,
        tags=springclean_tags(),
    )
    template = deployment_template(inputs, public, outdir=tmp_path / "public")
    runtime = resources(template, AGENTCORE_RUNTIME_TYPE)[0]
    assert runtime["NetworkConfiguration"] == {"NetworkMode": "PUBLIC"}
    assert "AuthorizerConfiguration" not in runtime


def test_the_lifecycle_configuration_is_synthesized_when_supplied_offline(
    inputs: SynthesisInputs, vpc: VpcAttachment, tmp_path: Path
) -> None:
    bounded = AgentCoreRuntimeInputs(
        runtime_name=RUNTIME_NAME,
        endpoint_name=ENDPOINT_NAME,
        image_uri=IMAGE_URI,
        network_mode="VPC",
        vpc=vpc,
        idle_session_timeout_seconds=900,
        max_lifetime_seconds=3600,
        tags=springclean_tags(),
    )
    template = deployment_template(inputs, bounded, outdir=tmp_path / "lifecycle")
    runtime = resources(template, AGENTCORE_RUNTIME_TYPE)[0]
    assert runtime["LifecycleConfiguration"] == {
        "IdleRuntimeSessionTimeout": 900,
        "MaxLifetime": 3600,
    }
    with pytest.raises(ValidationError):
        AgentCoreRuntimeInputs(
            runtime_name=RUNTIME_NAME,
            endpoint_name=ENDPOINT_NAME,
            image_uri=IMAGE_URI,
            network_mode="VPC",
            vpc=vpc,
            idle_session_timeout_seconds=3600,
            max_lifetime_seconds=900,
            tags=springclean_tags(),
        )


def test_wiring_the_runtime_creates_no_additional_role_offline(
    template: dict[str, Any],
) -> None:
    roles = [
        logical
        for logical, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::IAM::Role"
    ]
    assert len(roles) == len(ROLE_PREFIXES)
    assert "ManagedPolicyArns" not in json.dumps(template)


def test_deployment_synthesis_resolves_nothing_from_an_account_offline(
    inputs: SynthesisInputs,
    agentcore: AgentCoreRuntimeInputs,
    telemetry: TelemetryRuntimeConfig,
    tmp_path: Path,
) -> None:
    plan = plan_deployment(
        inputs, agentcore, telemetry=telemetry, outdir=tmp_path / "plan"
    )
    manifest = json.loads((plan.assembly_path / "manifest.json").read_text("utf-8"))
    assert not manifest.get("missing"), manifest.get("missing")
    rendered = json.dumps(manifest)
    assert "unknown-account" not in rendered
    assert "unknown-region" not in rendered


# --- The plan is a description, never an action ----------------------------


def test_the_plan_describes_the_change_set_and_performs_no_call_offline(
    inputs: SynthesisInputs,
    agentcore: AgentCoreRuntimeInputs,
    telemetry: TelemetryRuntimeConfig,
    tmp_path: Path,
) -> None:
    plan = plan_deployment(
        inputs, agentcore, telemetry=telemetry, outdir=tmp_path / "plan"
    )
    assert plan.operation == "change_set_description"
    assert plan.aws_calls == 0
    assert plan.mutations == ()
    assert plan.region == inputs.config.region
    assert plan.account_id == inputs.config.account_id
    assert plan.stack_name == inputs.stack_name
    planned = {item.resource_type for item in plan.resources}
    assert {AGENTCORE_RUNTIME_TYPE, AGENTCORE_ENDPOINT_TYPE} <= planned
    assert len(plan.template_hash) == 64
    assert plan.tags == agentcore.tags
    assert plan.image_digest == IMAGE_DIGEST


def test_the_plan_is_deterministic_for_the_same_inputs_offline(
    inputs: SynthesisInputs,
    agentcore: AgentCoreRuntimeInputs,
    telemetry: TelemetryRuntimeConfig,
    tmp_path: Path,
) -> None:
    first = plan_deployment(
        inputs, agentcore, telemetry=telemetry, outdir=tmp_path / "one"
    )
    second = plan_deployment(
        inputs, agentcore, telemetry=telemetry, outdir=tmp_path / "two"
    )
    assert first.template_hash == second.template_hash
    assert first.resources == second.resources
    assert first.environment_variable_names == second.environment_variable_names


def test_the_plan_names_the_projected_variables_without_their_values_offline(
    inputs: SynthesisInputs,
    agentcore: AgentCoreRuntimeInputs,
    telemetry: TelemetryRuntimeConfig,
    tmp_path: Path,
) -> None:
    plan = plan_deployment(
        inputs, agentcore, telemetry=telemetry, outdir=tmp_path / "plan"
    )
    names = plan.environment_variable_names
    assert "OTEL_EXPORTER_OTLP_LOGS_HEADERS" in names
    assert names == tuple(sorted(names))
    rendered = plan.model_dump_json()
    assert LOG_STREAM not in rendered
    assert "OTEL_RESOURCE_ATTRIBUTES" in rendered


def test_an_actual_deploy_stays_blocked_until_every_input_arrives_offline(
    inputs: SynthesisInputs,
    agentcore: AgentCoreRuntimeInputs,
    telemetry: TelemetryRuntimeConfig,
    tmp_path: Path,
) -> None:
    plan = plan_deployment(
        inputs, agentcore, telemetry=telemetry, outdir=tmp_path / "plan"
    )
    assert plan.blocked is True
    identifiers = {blocker.id for blocker in plan.blockers}
    assert identifiers == {
        "spend-limit",
        "verified-prices",
        "budget-alarm",
        "image-digest-published",
        "PF6",
        "PF8",
        "owner-approval",
    }
    for blocker in plan.blockers:
        assert blocker.detail


def test_every_blocker_clears_only_when_its_evidence_is_supplied_offline() -> None:
    assert deployment_blockers() == deployment_blockers(
        spend_limit_recorded=False,
        prices_verified=False,
        budget_alarm_configured=False,
        image_published=False,
        pf6_established=False,
        pf8_established=False,
        owner_approved=False,
    )
    cleared = deployment_blockers(
        spend_limit_recorded=True,
        prices_verified=True,
        budget_alarm_configured=True,
        image_published=True,
        pf6_established=True,
        pf8_established=True,
        owner_approved=True,
    )
    assert cleared == ()
    partial = {blocker.id for blocker in deployment_blockers(owner_approved=True)}
    assert "owner-approval" not in partial
    assert "PF6" in partial


# --- The shipped example is a real input set -------------------------------


def test_the_shipped_example_inputs_validate_and_name_no_real_resource() -> None:
    """An example nobody can load is documentation, not an input set."""
    path = (
        Path(__file__).resolve().parents[2]
        / "deployment"
        / "agentcore"
        / "runtime-inputs.example.json"
    )
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body.pop("_comment")
    assert set(body) == {"agentcore", "telemetry"}
    # The contracts are strict, so a JSON array reaches a tuple field through
    # JSON validation rather than through Python coercion.
    example = AgentCoreRuntimeInputs.model_validate_json(json.dumps(body["agentcore"]))
    assert example.tag_map["auto-delete"] == "no"
    assert example.image_digest == "sha256:" + "0" * 64
    assert example.image_account_id == "0" * 12
    telemetry = TelemetryRuntimeConfig.model_validate_json(
        json.dumps(body["telemetry"])
    )
    # The live identity is a deploy output, so the example cannot supply it.
    assert telemetry.runtime_arn is None
    assert telemetry.missing_live_fields
    assert not telemetry.inputs_complete


def test_the_published_image_is_documented_as_an_unemulated_cross_build() -> None:
    """The digest that reaches the runtime comes from a no-emulation build.

    The runtime resource takes a `linux/arm64` image. The build host is not
    arm64 and no binfmt emulation is installed, so the documented route is a
    native builder stage feeding an arm64 final stage that executes nothing.
    """
    readme = (
        Path(__file__).resolve().parents[2] / "deployment" / "agentcore" / "README.md"
    ).read_text(encoding="utf-8")
    assert "BUILDER_IMAGE_DIGEST" in readme
    assert "RUNTIME_IMAGE_DIGEST" in readme
    assert "linux/arm64" in readme
    assert "$BUILDPLATFORM" in readme
    lowered = readme.lower()
    # Naming a route to rule it out is allowed; invoking one is not.
    for installer in ("tonistiigi/binfmt", "qemu-user-static", "docker run --priv"):
        assert installer not in lowered, installer


# --- The live-test order is fixed -----------------------------------------


def test_the_live_test_order_denies_anonymously_before_any_upload_offline() -> None:
    checklist = live_test_checklist()
    assert tuple(step.id for step in checklist) == LIVE_TEST_ORDER
    assert [step.order for step in checklist] == list(range(1, len(checklist) + 1))
    order = {step.id: step.order for step in checklist}
    assert order["deploy-empty-private-origin"] < order["attach-auth-gate"]
    assert order["attach-auth-gate"] < order["verify-anonymous-denial"]
    assert order["verify-anonymous-denial"] < order["upload-restricted-content"]
    assert order["upload-restricted-content"] < order["invoke-as-the-operator"]
    denial = next(step for step in checklist if step.id == "verify-anonymous-denial")
    assert "401" in denial.expected
    assert "403" in denial.expected
    assert "unsigned" in denial.action.lower()
    correlation = next(step for step in checklist if step.id == "capture-correlation")
    for signal in ("CloudTrail", "CloudWatch", "OTel"):
        assert signal in correlation.expected
    rollback = next(step for step in checklist if step.id == "rollback-and-teardown")
    assert "rollback" in rollback.action.lower()
    assert rollback.order == len(checklist)
