"""Error paths and derived accessors across the cloud layers. Offline.

Every failure here is reason-coded. A transport failure is never retried, a
missing or unreadable row fails closed, and a derived identifier is computed
rather than configured.
"""

from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from unstuck.cascade.receipts import ReceiptNamespace
from unstuck.cascade.repository import ReceiptWriteToken
from unstuck.clock import FakeClock
from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.config import CloudResourceConfig, sensitive_text
from unstuck.cloud.dynamodb import (
    DynamoDbRunRepository,
    derive_run_id,
    initial_snapshot,
    meta_item,
    snapshot_from_items,
)
from unstuck.cloud.keys import (
    ArtifactPrefix,
    DynamoKey,
    ReceiptIndexKey,
    receipt_evidence_key,
    run_artifact_key,
)
from unstuck.cloud.preflight import PriceInput, SpendGateInputs
from unstuck.cloud.receipts import DynamoDbReceiptRepository
from unstuck.cloud.session import RuntimeSessionId, lease_outcome
from unstuck.cloud.synth import SynthesisInputs
from unstuck.cloud.teardown import plan_teardown
from unstuck.domain import Reason, RuntimeFault, TaskSpec

from .conftest import (
    SYNTHETIC_ACCOUNT,
    SYNTHETIC_ARTIFACT_BUCKET,
    SYNTHETIC_FINGERPRINT,
    SYNTHETIC_RUN_ID,
    StubbedClient,
)

TRANSPORT = {"Error": {"Code": "ServiceUnavailable", "Message": "offline"}}


def transport(operation: str) -> ClientError:
    return ClientError(TRANSPORT, operation)


def test_derived_identifiers_are_computed_from_the_config_offline(
    config: CloudResourceConfig,
) -> None:
    assert config.decider_api_arn == (
        f"arn:aws:execute-api:us-east-1:{SYNTHETIC_ACCOUNT}:"
        f"{config.decider_api_id}/*/*/*"
    )
    assert config.run_table_arn.endswith(f"table/{config.run_table_name}")
    assert config.receipt_table_arn.endswith(f"table/{config.receipt_table_name}")
    assert config.artifact_bucket_arn == f"arn:aws:s3:::{SYNTHETIC_ARTIFACT_BUCKET}"


@pytest.mark.parametrize(
    "value,sensitive",
    [
        ("us-east-1", False),
        ("unstuck-synthetic-runs", False),
        ("AWS_SECRET_ACCESS_KEY", True),
        ("Authorization", True),
        ("fixtures/evaluator/answers.json", True),
        ("Bearer offline-placeholder", True),
    ],
)
def test_the_sensitive_marker_definition_is_shared_offline(
    value: str, sensitive: bool
) -> None:
    assert sensitive_text(value) is sensitive


def test_key_helpers_derive_scoped_identifiers_offline(
    namespace: ReceiptNamespace,
) -> None:
    assert DynamoKey.partition_for(SYNTHETIC_RUN_ID) == f"RUN#{SYNTHETIC_RUN_ID}"
    assert (
        ReceiptIndexKey.for_receipt(
            namespace, SYNTHETIC_FINGERPRINT, "b" * 64
        ).receipt_id
        == "b" * 64
    )
    assert ReceiptIndexKey.for_receipt(namespace, SYNTHETIC_FINGERPRINT, "b" * 64).item[
        "PK"
    ] == {"S": f"PROBLEM#{SYNTHETIC_FINGERPRINT}"}
    run_key = run_artifact_key(SYNTHETIC_RUN_ID, "events.jsonl")
    assert run_key.prefix == "runs/"
    receipt_key = receipt_evidence_key("b" * 64, "delta.json")
    assert receipt_key.prefix == "receipt-evidence/"
    assert ArtifactPrefix(key="fixtures/public/a.json", mode="read").prefix == (
        "fixtures/public/"
    )


def test_a_session_identity_must_end_in_a_canonical_run_identity_offline() -> None:
    assert RuntimeSessionId.for_run(SYNTHETIC_RUN_ID).run_id == SYNTHETIC_RUN_ID
    with pytest.raises(ValidationError):
        RuntimeSessionId(value="unstuck-run-" + "z" * 36)
    with pytest.raises(ValidationError):
        RuntimeSessionId(value="unstuck-run-AAAAAAAA-1111-4111-8111-111111111111")
    with pytest.raises(ValidationError):
        RuntimeSessionId(value="unstuck-run-11111111111141118111111111111111")


def test_every_terminal_status_refuses_a_lease_offline() -> None:
    for status in (
        "completed",
        "empty_verified",
        "pending",
        "unresolved",
        "budget_exhausted",
        "failed",
    ):
        decision = lease_outcome(status=status, lease_owner=uuid4(), expired=True)
        assert decision.reason is CloudReason.LEASE_REJECTED


def test_an_unusable_idempotency_key_is_refused_offline() -> None:
    for key in ("", "k" * 129):
        with pytest.raises(RuntimeFault) as raised:
            derive_run_id(key)
        assert raised.value.reason is Reason.INVARIANT


def test_a_missing_or_unreadable_run_fails_closed_offline(
    dynamo: StubbedClient, config: CloudResourceConfig, clock: FakeClock
) -> None:
    adapter = DynamoDbRunRepository(dynamo.client, config, clock=clock)
    dynamo.stubber.add_response("get_item", {})
    with pytest.raises(RuntimeFault) as absent:
        adapter.load_run(SYNTHETIC_RUN_ID)
    assert absent.value.reason is Reason.STORAGE_ERROR
    dynamo.stubber.add_client_error("get_item", service_error_code="ServiceUnavailable")
    with pytest.raises(RuntimeFault) as unreadable:
        adapter.load_run(SYNTHETIC_RUN_ID)
    assert unreadable.value.reason is Reason.STORAGE_ERROR
    dynamo.stubber.assert_no_pending_responses()


@pytest.mark.parametrize(
    "meta,events",
    [
        ({}, []),
        ({"body": {"S": "{"}}, []),
        ({"body": {"S": "{}"}}, []),
        ({"body": {"N": "1"}}, []),
    ],
)
def test_a_malformed_stored_run_fails_closed_offline(
    meta: dict[str, object], events: list[dict[str, object]]
) -> None:
    with pytest.raises(RuntimeFault) as raised:
        snapshot_from_items(meta, events)
    assert raised.value.reason is Reason.STORAGE_ERROR


def test_a_transport_failure_on_create_is_never_retried_offline(
    dynamo: StubbedClient,
    config: CloudResourceConfig,
    clock: FakeClock,
    task: TaskSpec,
) -> None:
    adapter = DynamoDbRunRepository(dynamo.client, config, clock=clock)
    dynamo.stubber.add_client_error("put_item", service_error_code="ServiceUnavailable")
    with pytest.raises(RuntimeFault) as raised:
        adapter.create_run(task, "v17", "offline-case-03")
    assert raised.value.reason is Reason.STORAGE_ERROR
    assert len(dynamo.params("PutItem")) == 1
    dynamo.stubber.assert_no_pending_responses()


def test_record_refuses_a_terminal_event_or_an_unheld_lease_offline(
    dynamo: StubbedClient,
    config: CloudResourceConfig,
    clock: FakeClock,
    task: TaskSpec,
) -> None:
    adapter = DynamoDbRunRepository(dynamo.client, config, clock=clock)
    snapshot = initial_snapshot(task, "v17", "offline-case-04")
    with pytest.raises(RuntimeFault) as terminal:
        adapter.record(snapshot, uuid4(), "run_finished", {})
    assert terminal.value.reason is Reason.INVARIANT
    with pytest.raises(RuntimeFault) as unheld:
        adapter.record(snapshot, uuid4(), "query_observed", {})
    assert unheld.value.reason is Reason.STORAGE_ERROR
    with pytest.raises(RuntimeFault) as unfinished:
        adapter.finish(snapshot, uuid4(), Reason.INSUFFICIENT, {})
    assert unfinished.value.reason is Reason.STORAGE_ERROR
    assert dynamo.params("TransactWriteItems") == []


def test_meta_item_omits_an_absent_lease_owner_offline(task: TaskSpec) -> None:
    item = meta_item(initial_snapshot(task, "v17", "offline-case-05"))
    assert "lease_owner" not in item
    assert item["status"] == {"S": "created"}


def test_the_receipt_adapter_refuses_a_non_receipt_and_a_transport_failure_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    write_token: ReceiptWriteToken,
    make_receipt: object,
) -> None:
    adapter = DynamoDbReceiptRepository(receipt_dynamo.client, config)
    with pytest.raises(RuntimeFault) as wrong_type:
        adapter.put({"receipt_id": "b" * 64}, write_token)  # type: ignore[arg-type]
    assert wrong_type.value.reason is Reason.RECEIPT_INVALID
    receipt_dynamo.stubber.add_client_error(
        "put_item", service_error_code="ServiceUnavailable"
    )
    with pytest.raises(RuntimeFault) as unavailable:
        adapter.put(make_receipt(), write_token)  # type: ignore[operator]
    assert unavailable.value.reason is Reason.STORAGE_ERROR
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_supersede_error_paths_are_reason_coded_offline(
    receipt_dynamo: StubbedClient,
    config: CloudResourceConfig,
    write_token: ReceiptWriteToken,
    namespace: ReceiptNamespace,
) -> None:
    adapter = DynamoDbReceiptRepository(receipt_dynamo.client, config)
    receipt_dynamo.stubber.add_client_error(
        "query", service_error_code="ServiceUnavailable"
    )
    with pytest.raises(RuntimeFault) as lookup:
        adapter.supersede(namespace, "b" * 64, write_token)
    assert lookup.value.reason is Reason.STORAGE_ERROR

    partition = {"S": f"PROBLEM#{SYNTHETIC_FINGERPRINT}"}
    receipt_dynamo.stubber.add_response("query", {"Items": [{"PK": partition}]})
    receipt_dynamo.stubber.add_client_error(
        "update_item", service_error_code="ConditionalCheckFailedException"
    )
    assert adapter.supersede(namespace, "b" * 64, write_token) is False

    receipt_dynamo.stubber.add_response("query", {"Items": [{"PK": partition}]})
    receipt_dynamo.stubber.add_client_error(
        "update_item", service_error_code="ServiceUnavailable"
    )
    with pytest.raises(RuntimeFault) as write:
        adapter.supersede(namespace, "b" * 64, write_token)
    assert write.value.reason is Reason.STORAGE_ERROR

    receipt_dynamo.stubber.add_response(
        "query", {"Items": [{"NoPartition": partition}]}
    )
    with pytest.raises(RuntimeFault) as malformed:
        adapter.supersede(namespace, "b" * 64, write_token)
    assert malformed.value.reason is Reason.STORAGE_ERROR
    receipt_dynamo.stubber.assert_no_pending_responses()


def test_synthesis_inputs_refuse_an_incoherent_topology_offline(
    inputs: SynthesisInputs,
) -> None:
    with pytest.raises(ValidationError):
        SynthesisInputs.model_validate(
            inputs.model_dump() | {"holdout_bucket": inputs.config.artifact_bucket}
        )
    with pytest.raises(ValidationError):
        SynthesisInputs.model_validate(
            inputs.model_dump() | {"holdout_bucket": "unstuck-evaluator-truth"}
        )
    with pytest.raises(ValidationError):
        SynthesisInputs.model_validate(
            inputs.model_dump()
            | {"operator_principal_arn": inputs.deployer_principal_arn}
        )
    with pytest.raises(ValidationError):
        SynthesisInputs.model_validate(inputs.model_dump() | {"vpc_cidr": "0.0.0.0/0"})


def test_the_teardown_plan_names_each_retained_resource_offline() -> None:
    plan = plan_teardown(
        {
            "Resources": {
                "ArtifactBucket": {
                    "Type": "AWS::S3::Bucket",
                    "Properties": {"BucketName": SYNTHETIC_ARTIFACT_BUCKET},
                },
                "Policy": {
                    "Type": "AWS::S3::BucketPolicy",
                    "Properties": {},
                },
                "Repo": {
                    "Type": "AWS::ECR::Repository",
                    "Properties": {"RepositoryName": "unstuck-controller"},
                },
            }
        },
        retain_artifacts=True,
    )
    assert plan.retained == (
        f"AWS::S3::Bucket {SYNTHETIC_ARTIFACT_BUCKET}",
        "AWS::S3::BucketPolicy Policy",
    )
    assert [step.logical_id for step in plan.steps] == ["Repo"]


def test_a_price_table_refuses_a_duplicate_component_offline() -> None:
    price = PriceInput(component="agentcore", unit_usd=0.5, availability="measured")
    with pytest.raises(ValidationError):
        SpendGateInputs(spend_limit_usd="10", prices=(price, price))
    assert SpendGateInputs(spend_limit_usd=" 10.00 ").limit is not None
    assert SpendGateInputs(spend_limit_usd="nan").limit is None
    assert SpendGateInputs().price("agentcore") is None


def test_an_artifact_key_must_name_an_object_inside_its_prefix_offline() -> None:
    with pytest.raises(ValidationError):
        ArtifactPrefix(key="runs", mode="write")
    with pytest.raises(ValidationError):
        ArtifactPrefix(key="receipt-evidence", mode="write")
    assert ArtifactPrefix(key="runs/a", mode="write").key == "runs/a"


def test_an_artifact_store_refuses_a_plaintext_endpoint_offline(
    config: CloudResourceConfig,
) -> None:
    from unstuck.cloud.artifacts import S3ArtifactStore

    class NoMeta:
        pass

    with pytest.raises(CloudFault) as raised:
        S3ArtifactStore(NoMeta(), config)
    assert raised.value.reason is CloudReason.CONFIG_INVALID


def test_a_run_identity_is_stable_and_distinct_offline() -> None:
    assert derive_run_id("offline-case-01") == derive_run_id("offline-case-01")
    assert isinstance(derive_run_id("offline-case-01"), UUID)
    assert len({derive_run_id(f"offline-case-{index}") for index in range(8)}) == 8
