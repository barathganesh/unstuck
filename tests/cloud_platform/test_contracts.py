"""Contract tests for the cloud-platform typed surfaces. Offline and synthetic.

Every value here is a visibly synthetic identifier. No credential, no
authorization header, no environment value, no customer identifier, and no
evaluator reference reaches any contract.
"""

import json

import pytest
from pydantic import ValidationError

from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.guards import EgressDestination
from unstuck.cloud.keys import ArtifactPrefix, DynamoKey, ReceiptIndexKey
from unstuck.cloud.preflight import PriceInput, SpendGateInputs
from unstuck.cloud.session import RuntimeSessionId

from .conftest import (
    SYNTHETIC_ACCOUNT,
    SYNTHETIC_ARTIFACT_BUCKET,
    SYNTHETIC_CONTROLLER_ARN,
    SYNTHETIC_RUN_ID,
    SYNTHETIC_RUN_TABLE,
)

#: Values a configuration field must never accept, in synthetic form.
REJECTED_FIELD_VALUES = (
    "Bearer offline-placeholder-value",
    "authorization=offline-placeholder",
    "arn:aws:s3:::holdout-truth/fixtures/evaluator/answers.json",
    "customer-0001@example.invalid",
    "AWS_SECRET_ACCESS_KEY",
)


def test_config_rejects_extra_fields_offline(config: CloudResourceConfig) -> None:
    with pytest.raises(ValidationError):
        CloudResourceConfig.model_validate(
            config.model_dump() | {"authorization_header": "offline-placeholder"}
        )


def test_config_is_frozen_and_canonically_serializable_offline(
    config: CloudResourceConfig,
) -> None:
    with pytest.raises(ValidationError):
        config.region = "eu-west-1"  # type: ignore[misc]
    first = config.model_dump_json()
    second = CloudResourceConfig.model_validate_json(first).model_dump_json()
    assert first == second
    assert json.loads(first)["account_id"] == SYNTHETIC_ACCOUNT


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_bucket", "Unstuck.Synthetic.Caps"),
        ("artifact_bucket", "ab"),
        ("artifact_bucket", "192.168.0.1"),
        ("run_table_name", "no spaces allowed"),
        ("region", "not-a-region"),
        ("account_id", "12345"),
        ("controller_runtime_arn", "arn:aws:lambda:us-east-1:000000000000:function:x"),
        ("decider_vpc_endpoint_id", "vpc-0123456789abcdef0"),
    ],
)
def test_config_rejects_malformed_identifiers_offline(
    config: CloudResourceConfig, field: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        CloudResourceConfig.model_validate(config.model_dump() | {field: value})


@pytest.mark.parametrize("value", REJECTED_FIELD_VALUES)
def test_no_config_field_accepts_sensitive_values_offline(
    config: CloudResourceConfig, value: str
) -> None:
    for field in CloudResourceConfig.model_fields:
        if field == "schema_version":
            continue
        with pytest.raises(ValidationError):
            CloudResourceConfig.model_validate(config.model_dump() | {field: value})


def test_config_arns_must_carry_the_declared_account_and_region_offline(
    config: CloudResourceConfig,
) -> None:
    foreign = SYNTHETIC_CONTROLLER_ARN.replace(SYNTHETIC_ACCOUNT, "000000000001")
    with pytest.raises(ValidationError):
        CloudResourceConfig.model_validate(
            config.model_dump() | {"controller_runtime_arn": foreign}
        )
    other_region = SYNTHETIC_CONTROLLER_ARN.replace("us-east-1", "eu-west-1")
    with pytest.raises(ValidationError):
        CloudResourceConfig.model_validate(
            config.model_dump() | {"controller_runtime_arn": other_region}
        )


def test_key_contracts_reject_extra_fields_and_freeze_offline() -> None:
    key = DynamoKey.run_meta(SYNTHETIC_RUN_ID)
    with pytest.raises(ValidationError):
        DynamoKey.model_validate(key.model_dump() | {"ttl": 150})
    prefix = ArtifactPrefix(key=f"runs/{SYNTHETIC_RUN_ID}/events.jsonl", mode="write")
    with pytest.raises(ValidationError):
        ArtifactPrefix.model_validate(prefix.model_dump() | {"bucket": "other"})
    with pytest.raises(ValidationError):
        ReceiptIndexKey.model_validate({"partition": "RUN#1", "sort": "META"})


def test_runtime_session_id_contract_offline() -> None:
    session = RuntimeSessionId.for_run(SYNTHETIC_RUN_ID)
    assert session.value == f"unstuck-run-{SYNTHETIC_RUN_ID}"
    with pytest.raises(ValidationError):
        RuntimeSessionId(value="unstuck-run-not-a-uuid")
    with pytest.raises(ValidationError):
        RuntimeSessionId(value=f"other-prefix-{SYNTHETIC_RUN_ID}")


def test_egress_destination_contract_offline() -> None:
    destination = EgressDestination(host="dynamodb.us-east-1.amazonaws.com", port=443)
    assert destination.authority == "https://dynamodb.us-east-1.amazonaws.com:443"
    with pytest.raises(ValidationError):
        EgressDestination(host="dynamodb.us-east-1.amazonaws.com", port=0)
    with pytest.raises(ValidationError):
        EgressDestination.model_validate(
            {"host": "proxy.invalid", "port": 443, "scheme": "http"}
        )


def test_spend_gate_inputs_contract_offline() -> None:
    inputs = SpendGateInputs(
        spend_limit_usd="25.00",
        current_spend_usd=1.0,
        current_spend_available=True,
        episodes=4,
        expected_duration_hours=0.5,
        prices=(
            PriceInput(component="agentcore", unit_usd=0.5, availability="measured"),
        ),
    )
    assert inputs.prices[0].unit_usd == 0.5
    with pytest.raises(ValidationError):
        SpendGateInputs.model_validate(inputs.model_dump() | {"force": True})
    with pytest.raises(ValidationError):
        PriceInput(component="agentcore", unit_usd=None, availability="measured")
    with pytest.raises(ValidationError):
        PriceInput(component="agentcore", unit_usd=0.5, availability="unknown")
    with pytest.raises(ValidationError):
        SpendGateInputs.model_validate(
            inputs.model_dump() | {"current_spend_usd": None}
        )
    with pytest.raises(ValidationError):
        PriceInput(component="not_a_component", unit_usd=None)


def test_config_table_and_bucket_names_are_distinct_offline(
    config: CloudResourceConfig,
) -> None:
    with pytest.raises(ValidationError):
        CloudResourceConfig.model_validate(
            config.model_dump() | {"receipt_table_name": SYNTHETIC_RUN_TABLE}
        )
    assert config.artifact_bucket == SYNTHETIC_ARTIFACT_BUCKET
