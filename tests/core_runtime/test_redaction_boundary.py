"""Redaction and persistence-boundary regressions for serialized secrets.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/core_runtime/test_redaction_boundary.py -q

The Redactor is the release-gate control for REQ-SEC-15 and NFR2.2, and the
`PersistenceBoundary` refuses a contract whose strings the Redactor would
change. Both therefore have to recognise the serialized shapes the targeted
keywords actually take in a stringified payload, an exception message, or a
credentials-file fragment, not only the bare `keyword=value` form.

Every placeholder below is an invented string shaped like a sensitive value.
None is a real credential and none names a live destination.
"""

import pytest

from unstuck.domain import Reason, RuntimeFault
from unstuck.events import PersistenceBoundary, Redactor

REDACTED = "[REDACTED]"

PEM_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "b2ZmbGluZS1wbGFjZWhvbGRlci1ub3QtYS1rZXk=\n"
    "-----END RSA PRIVATE KEY-----"
)

#: Serialized shapes that previously survived redaction untouched.
SERIALIZED_SECRETS = (
    '{"password": "offline-placeholder"}',
    "{'token': 'offline-placeholder'}",
    '"secret":"offline-placeholder"',
    '{"api_key": "offline-placeholder"}',
    "aws_secret_access_key=b2ZmbGluZS1wbGFjZWhvbGRlci1zaWduaW5n",
    "aws_access_key_id=AKIAOFFLINEPLACEHOLD",
    "aws_session_token=b2ZmbGluZS1wbGFjZWhvbGRlci1zZXNzaW9u",
    '{"aws_secret_access_key": "b2ZmbGluZS1wbGFjZWhvbGRlcg=="}',
    PEM_BLOCK,
)

#: Shapes the Redactor already handled. They must keep failing closed.
ALREADY_COVERED = (
    "password=offline-placeholder",
    "Authorization: Bearer offline-placeholder",
    "bearer offline-placeholder",
    "AKIAOFFLINEPLACEHOLD1",
    "ASIAOFFLINEPLACEHOLD1",
)


@pytest.mark.parametrize("payload", SERIALIZED_SECRETS + ALREADY_COVERED)
def test_a_serialized_secret_never_survives_redaction(payload: str) -> None:
    cleaned = Redactor().clean(payload)
    assert isinstance(cleaned, str)
    assert cleaned != payload
    assert REDACTED in cleaned


@pytest.mark.parametrize("payload", SERIALIZED_SECRETS + ALREADY_COVERED)
def test_a_serialized_secret_never_crosses_the_persistence_boundary(
    payload: str,
) -> None:
    redactor = Redactor()
    assert redactor.safe_authority(payload) is False
    assert redactor.safe_authority({"note": payload}) is False
    assert redactor.safe_authority([payload]) is False


def test_the_secret_value_itself_is_removed_not_only_its_label() -> None:
    cleaned = Redactor().clean(
        "aws_secret_access_key=b2ZmbGluZS1wbGFjZWhvbGRlci1zaWduaW5n"
    )
    assert isinstance(cleaned, str)
    assert "b2ZmbGluZS1wbGFjZWhvbGRlci1zaWduaW5n" not in cleaned
    assert "aws_secret_access_key" not in cleaned
    assert cleaned == REDACTED


def test_a_private_key_block_is_removed_whole() -> None:
    cleaned = Redactor().clean(f"context before {PEM_BLOCK} context after")
    assert isinstance(cleaned, str)
    assert "BEGIN RSA PRIVATE KEY" not in cleaned
    assert "b2ZmbGluZS1wbGFjZWhvbGRlci1ub3QtYS1rZXk=" not in cleaned
    assert cleaned == f"context before {REDACTED} context after"


def test_a_configured_secret_value_is_still_removed() -> None:
    redactor = Redactor(("offline-configured-value",))
    cleaned = redactor.clean("body offline-configured-value tail")
    assert cleaned == f"body {REDACTED} tail"
    assert redactor.safe_authority("body offline-configured-value tail") is False


@pytest.mark.parametrize(
    "benign",
    [
        '{"token_usage": 42}',
        '{"token_usage_available": true}',
        "token_usage=42",
        '{"availability": "measured", "token_usage": 1}',
    ],
)
def test_token_usage_metadata_is_never_falsely_redacted(benign: str) -> None:
    redactor = Redactor()
    assert redactor.clean(benign) == benign
    assert redactor.safe_authority(benign) is True


def test_token_usage_survives_as_a_mapping_key() -> None:
    payload = {"token_usage": {"availability": "measured", "value": 7}}
    assert Redactor().object(payload) == payload


@pytest.mark.parametrize(
    "benign",
    [
        "unstuck-synthetic-artifacts",
        "arn:aws:dynamodb:us-east-1:000000000000:table/unstuck-synthetic-runs",
        "a" * 64,
        "scripted-v1",
        "RUN#11111111-1111-4111-8111-111111111111",
        "ARM#decider_memory#CASE#case-01#RECEIPT#" + "b" * 64,
    ],
)
def test_an_ordinary_identifier_is_left_alone(benign: str) -> None:
    redactor = Redactor()
    assert redactor.clean(benign) == benign
    assert redactor.safe_authority(benign) is True


def test_the_boundary_refuses_a_serialized_secret_in_a_nested_payload() -> None:
    boundary = PersistenceBoundary(Redactor())
    boundary.validate({"rows": [{"detail": "ordinary text"}]})
    with pytest.raises(RuntimeFault) as raised:
        boundary.validate({"rows": [{"detail": SERIALIZED_SECRETS[0]}]})
    assert raised.value.reason is Reason.INVARIANT
