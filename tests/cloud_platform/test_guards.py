"""Security-negative tests for the secret and serving-time egress guards.

Offline and synthetic. Each placeholder below is an invented string shaped like
a sensitive value; none is a real credential, and none is a live destination.
"""

import pytest

from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.guards import (
    DECIDER_HOST_EGRESS,
    EgressDestination,
    EgressPolicy,
    SecretGuard,
    controller_egress,
)

from .conftest import SENTINEL_MARKERS, SYNTHETIC_REGION

#: Field names a runtime configuration must never carry.
FORBIDDEN_FIELD_TOKENS = (
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
    "api_key",
    "apikey",
    "environment",
    "evaluator",
)

SENSITIVE_PLACEHOLDERS = (
    {"authorization": "Bearer offline-placeholder"},
    {"decider_secret": "offline-placeholder"},
    {"api_key": "offline-placeholder"},
    {"note": "password=offline-placeholder"},
    {"note": "evaluator truth path fixtures/evaluator/answers.json"},
    {"environment": "offline-placeholder"},
)


def test_runtime_config_carries_identifiers_and_versions_only_offline(
    config: CloudResourceConfig,
) -> None:
    for field in CloudResourceConfig.model_fields:
        lowered = field.lower()
        assert not any(token in lowered for token in FORBIDDEN_FIELD_TOKENS), field
    for value in config.model_dump(mode="json").values():
        assert isinstance(value, str)
        assert "\n" not in value and len(value) <= 256


@pytest.mark.parametrize("payload", SENSITIVE_PLACEHOLDERS)
def test_secret_guard_rejects_a_sensitive_value_offline(
    payload: dict[str, str],
) -> None:
    with pytest.raises(CloudFault) as raised:
        SecretGuard().check(payload)
    assert raised.value.reason is CloudReason.SECRET_REJECTED


def test_secret_guard_accepts_a_clean_configuration_offline(
    config: CloudResourceConfig,
) -> None:
    SecretGuard().check(config.model_dump(mode="json"))


def test_redacted_export_has_zero_sentinel_markers_offline(
    config: CloudResourceConfig,
) -> None:
    export = SecretGuard().export(config)
    rendered = " ".join(f"{key} {value}" for key, value in export.items()).lower()
    for marker in SENTINEL_MARKERS:
        assert marker not in rendered, marker
    assert export["region"] == SYNTHETIC_REGION
    assert "[REDACTED]" not in rendered


def test_controller_allowlist_denies_an_unlisted_destination_offline(
    config: CloudResourceConfig,
) -> None:
    policy = controller_egress(config)
    allowed = EgressDestination(
        host=f"dynamodb.{SYNTHETIC_REGION}.amazonaws.com", port=443
    )
    policy.check(allowed)
    for denied in (
        EgressDestination(host="models.example.invalid", port=443),
        EgressDestination(host=f"dynamodb.{SYNTHETIC_REGION}.amazonaws.com", port=8443),
        EgressDestination(host="dynamodb.eu-west-1.amazonaws.com", port=443),
    ):
        with pytest.raises(CloudFault) as raised:
            policy.check(denied)
        assert raised.value.reason is CloudReason.EGRESS_DENIED


def test_redirect_to_an_unlisted_destination_is_denied_offline(
    config: CloudResourceConfig,
) -> None:
    policy = controller_egress(config)
    origin = EgressDestination(host=f"s3.{SYNTHETIC_REGION}.amazonaws.com", port=443)
    policy.check(origin)
    with pytest.raises(CloudFault) as raised:
        policy.check_redirect(
            origin, EgressDestination(host="relay.example.invalid", port=443)
        )
    assert raised.value.reason is CloudReason.EGRESS_DENIED


def test_decider_host_declares_no_serving_time_internet_egress_offline() -> None:
    assert DECIDER_HOST_EGRESS.allows_internet is False
    assert DECIDER_HOST_EGRESS.destinations == ()
    with pytest.raises(CloudFault) as raised:
        DECIDER_HOST_EGRESS.check(
            EgressDestination(host="pypi.example.invalid", port=443)
        )
    assert raised.value.reason is CloudReason.EGRESS_DENIED


def test_empty_allowlist_cannot_be_widened_after_construction_offline() -> None:
    policy = EgressPolicy()
    assert policy.destinations == ()
    with pytest.raises(AttributeError):
        policy.destinations = (  # type: ignore[misc]
            EgressDestination(host="relay.example.invalid", port=443),
        )
