"""Invariant and security-negative tests for key derivation and prefix scoping.

Offline and synthetic. Every denial here is a contract refusal, not a runtime
permission check: a planned IAM grant is never evidence of a denied request.
"""

import pytest
from pydantic import ValidationError

from unstuck.cascade.receipts import ReceiptNamespace
from unstuck.cloud.keys import (
    DENIED_PREFIXES,
    EVENT_SEQUENCE_DIGITS,
    READ_PREFIXES,
    WRITE_PREFIXES,
    ArtifactPrefix,
    DynamoKey,
    ReceiptIndexKey,
)

from .conftest import SYNTHETIC_FINGERPRINT, SYNTHETIC_RUN_ID

ESCAPE_KEYS = (
    "/runs/absolute.json",
    "runs/../models/decider/v19/weights.bin",
    "runs/./nested/../../experiments/out.json",
    "..",
    "runs//double.json",
    "runs/trailing/",
    "models/decider/v19/weights.bin",
    "experiments/holdout/summary.json",
    "fixtures/evaluator/answers.json",
    "runs/\x00null.json",
    "runs\\windows.json",
    "",
)


def test_run_partition_and_meta_sort_key_offline() -> None:
    key = DynamoKey.run_meta(SYNTHETIC_RUN_ID)
    assert key.partition == f"RUN#{SYNTHETIC_RUN_ID}"
    assert key.sort == "META"
    assert key.item == {
        "PK": {"S": f"RUN#{SYNTHETIC_RUN_ID}"},
        "SK": {"S": "META"},
    }


def test_event_sort_keys_are_zero_padded_and_ordered_offline() -> None:
    first = DynamoKey.run_event(SYNTHETIC_RUN_ID, 1)
    tenth = DynamoKey.run_event(SYNTHETIC_RUN_ID, 10)
    assert first.sort == "EVENT#" + "1".rjust(EVENT_SEQUENCE_DIGITS, "0")
    assert first.sort < tenth.sort
    assert first.partition == tenth.partition
    with pytest.raises(ValidationError):
        DynamoKey.run_event(SYNTHETIC_RUN_ID, 0)
    with pytest.raises(ValidationError):
        DynamoKey.run_event(SYNTHETIC_RUN_ID, 10**EVENT_SEQUENCE_DIGITS)


def test_dynamo_key_rejects_an_unscoped_partition_offline() -> None:
    with pytest.raises(ValidationError):
        DynamoKey(partition="MODELS#decider", sort="META")
    with pytest.raises(ValidationError):
        DynamoKey(partition=f"RUN#{SYNTHETIC_RUN_ID}", sort="SNAPSHOT")


def test_receipt_partition_and_namespace_scoped_sort_key_offline(
    namespace: ReceiptNamespace,
) -> None:
    key = ReceiptIndexKey.for_receipt(namespace, SYNTHETIC_FINGERPRINT, "b" * 64)
    assert key.partition == f"PROBLEM#{SYNTHETIC_FINGERPRINT}"
    assert key.sort.startswith(ReceiptIndexKey.namespace_prefix(namespace))
    assert key.sort == (
        f"ARM#{namespace.arm}#CASE#{namespace.case_id}#RECEIPT#{'b' * 64}"
    )


def test_receipt_namespace_prefixes_cannot_collide_offline(
    namespace: ReceiptNamespace, other_namespace: ReceiptNamespace
) -> None:
    mine = ReceiptIndexKey.namespace_prefix(namespace)
    theirs = ReceiptIndexKey.namespace_prefix(other_namespace)
    assert mine != theirs
    assert not mine.startswith(theirs) and not theirs.startswith(mine)
    sibling = ReceiptNamespace(arm=namespace.arm, case_id=namespace.case_id + "x")
    assert not ReceiptIndexKey.namespace_prefix(sibling).startswith(mine)


def test_receipt_index_key_rejects_a_foreign_identity_offline(
    namespace: ReceiptNamespace,
) -> None:
    with pytest.raises(ValidationError):
        ReceiptIndexKey.for_receipt(namespace, "not-a-digest", "b" * 64)
    with pytest.raises(ValidationError):
        ReceiptIndexKey.for_receipt(namespace, SYNTHETIC_FINGERPRINT, "not-a-digest")


def test_write_prefixes_are_runs_and_receipt_evidence_only_offline() -> None:
    assert WRITE_PREFIXES == ("runs/", "receipt-evidence/")
    for prefix in WRITE_PREFIXES:
        assert ArtifactPrefix(
            key=f"{prefix}case/body.json", mode="write"
        ).key.startswith(prefix)


def test_public_fixtures_are_readable_but_never_writable_offline() -> None:
    assert READ_PREFIXES == (*WRITE_PREFIXES, "fixtures/public/")
    readable = ArtifactPrefix(key="fixtures/public/orders.csv", mode="read")
    assert readable.mode == "read"
    with pytest.raises(ValidationError):
        ArtifactPrefix(key="fixtures/public/orders.csv", mode="write")


@pytest.mark.parametrize("key", ESCAPE_KEYS)
@pytest.mark.parametrize("mode", ["read", "write"])
def test_artifact_prefix_refuses_escape_and_denied_targets_offline(
    key: str, mode: str
) -> None:
    with pytest.raises(ValidationError):
        ArtifactPrefix(key=key, mode=mode)


def test_denied_prefixes_cover_the_checkpoint_and_experiment_paths_offline() -> None:
    assert DENIED_PREFIXES == ("models/", "experiments/")
    for denied in DENIED_PREFIXES:
        assert not any(denied.startswith(allowed) for allowed in READ_PREFIXES)
        with pytest.raises(ValidationError):
            ArtifactPrefix(key=f"{denied}payload.json", mode="read")


def test_only_the_public_fixtures_prefix_is_ever_readable_offline() -> None:
    """Every other fixtures path is refused by the exhaustive allowed set.

    The evaluator-truth path is deliberately not named in the shipped package:
    the allowed set is checked first, so refusing it needs no literal, and a
    literal would be a leakage-scan finding of design.md section 20.
    """
    assert [prefix for prefix in READ_PREFIXES if prefix.startswith("fixtures/")] == [
        "fixtures/public/"
    ]
    for rejected in (
        "fixtures/evaluator/answers.json",
        "fixtures/holdout/rows.json",
        "fixtures/private/cause.json",
        "fixtures/answers.json",
    ):
        for mode in ("read", "write"):
            with pytest.raises(ValidationError):
                ArtifactPrefix(key=rejected, mode=mode)
