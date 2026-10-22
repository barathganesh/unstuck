"""Adapter tests for the private S3 artifact store. Offline and stubbed.

Prefix scoping and evaluator denial are asserted as refusals that happen before
a request is built. A planned bucket policy is not a denied request: REQ-SEC-11,
REQ-SEC-12 and PF8 remain Pending.
"""

import io
import json
from uuid import UUID

import pytest
from botocore.response import StreamingBody

from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.artifacts import SERVER_SIDE_ENCRYPTION, S3ArtifactStore
from unstuck.cloud.config import CloudResourceConfig

from .conftest import (
    SYNTHETIC_ARTIFACT_BUCKET,
    SYNTHETIC_HOLDOUT_BUCKET,
    SYNTHETIC_RUN_ID,
    StubbedClient,
)

RUN_KEY = f"runs/{SYNTHETIC_RUN_ID}/events.jsonl"
RECEIPT_KEY = f"receipt-evidence/{'b' * 64}/delta.json"
DENIED_KEYS = (
    "models/decider/v19/weights.bin",
    "experiments/holdout/summary.json",
    "fixtures/evaluator/answers.json",
    "/runs/absolute.json",
    "runs/../models/weights.bin",
)


def store(stub: StubbedClient, config: CloudResourceConfig) -> S3ArtifactStore:
    return S3ArtifactStore(
        stub.client,
        config,
        denied_buckets=frozenset({SYNTHETIC_HOLDOUT_BUCKET}),
    )


def body_response(payload: object) -> dict[str, object]:
    encoded = json.dumps(payload).encode()
    return {"Body": StreamingBody(io.BytesIO(encoded), len(encoded))}


@pytest.mark.parametrize("key", [RUN_KEY, RECEIPT_KEY])
def test_writes_are_confined_to_the_scoped_prefixes_offline(
    s3: StubbedClient, config: CloudResourceConfig, key: str
) -> None:
    s3.stubber.add_response("put_object", {})
    store(s3, config).put(key, {"status": "offline"})
    request = s3.only("PutObject")
    assert request["Bucket"] == SYNTHETIC_ARTIFACT_BUCKET
    assert request["Key"] == key
    assert request["ServerSideEncryption"] == SERVER_SIDE_ENCRYPTION
    assert request["ContentType"] == "application/json"
    s3.stubber.assert_no_pending_responses()


@pytest.mark.parametrize("key", DENIED_KEYS)
def test_a_denied_or_escaping_write_key_is_refused_before_any_request_offline(
    s3: StubbedClient, config: CloudResourceConfig, key: str
) -> None:
    with pytest.raises(CloudFault) as raised:
        store(s3, config).put(key, {"status": "offline"})
    assert raised.value.reason is CloudReason.PREFIX_SCOPE_DENIED
    assert s3.params("PutObject") == []


def test_public_fixtures_are_readable_but_never_writable_offline(
    s3: StubbedClient, config: CloudResourceConfig
) -> None:
    s3.stubber.add_response("get_object", body_response({"rows": 0}))
    assert store(s3, config).get("fixtures/public/orders.json") == {"rows": 0}
    assert s3.only("GetObject")["Key"] == "fixtures/public/orders.json"
    with pytest.raises(CloudFault) as raised:
        store(s3, config).put("fixtures/public/orders.json", {"rows": 0})
    assert raised.value.reason is CloudReason.PREFIX_SCOPE_DENIED
    s3.stubber.assert_no_pending_responses()


def test_evaluator_bucket_access_is_refused_before_any_request_offline(
    s3: StubbedClient, config: CloudResourceConfig
) -> None:
    with pytest.raises(CloudFault) as raised:
        store(s3, config).read_from(
            SYNTHETIC_HOLDOUT_BUCKET, "fixtures/public/orders.json"
        )
    assert raised.value.reason is CloudReason.EVALUATOR_DENIED
    with pytest.raises(CloudFault) as other:
        store(s3, config).read_from("some-other-bucket", RUN_KEY)
    assert other.value.reason is CloudReason.PREFIX_SCOPE_DENIED
    assert s3.params("GetObject") == []


def test_the_bound_bucket_is_readable_through_read_from_offline(
    s3: StubbedClient, config: CloudResourceConfig
) -> None:
    s3.stubber.add_response("get_object", body_response({"ok": True}))
    assert store(s3, config).read_from(SYNTHETIC_ARTIFACT_BUCKET, RUN_KEY) == {
        "ok": True
    }
    s3.stubber.assert_no_pending_responses()


def test_redaction_runs_before_the_body_reaches_the_client_offline(
    s3: StubbedClient, config: CloudResourceConfig
) -> None:
    s3.stubber.add_response("put_object", {})
    store(s3, config).put(
        RUN_KEY,
        {
            "authorization": "Bearer offline-placeholder",
            "note": "evaluator expected_rows for this case",
            "rows": 1,
        },
    )
    sent = s3.only("PutObject")["Body"]
    rendered = sent.decode() if isinstance(sent, bytes) else str(sent)
    assert "Bearer offline-placeholder" not in rendered
    assert "evaluator" not in rendered.lower()
    assert "[REDACTED]" in rendered
    assert json.loads(rendered)["rows"] == 1
    s3.stubber.assert_no_pending_responses()


def test_a_non_tls_client_is_refused_at_construction_offline(
    s3: StubbedClient, config: CloudResourceConfig
) -> None:
    assert str(s3.client.meta.endpoint_url).startswith("https://")

    class PlaintextMeta:
        endpoint_url = "http://s3.us-east-1.amazonaws.com"

    class PlaintextClient:
        meta = PlaintextMeta()

    with pytest.raises(CloudFault) as raised:
        S3ArtifactStore(PlaintextClient(), config)
    assert raised.value.reason is CloudReason.CONFIG_INVALID


def test_run_and_receipt_helpers_build_scoped_keys_offline(
    s3: StubbedClient, config: CloudResourceConfig
) -> None:
    bound = store(s3, config)
    assert bound.bucket == SYNTHETIC_ARTIFACT_BUCKET
    assert bound.run_key(UUID(str(SYNTHETIC_RUN_ID)), "events.jsonl") == RUN_KEY
    assert bound.receipt_key("b" * 64, "delta.json") == RECEIPT_KEY
