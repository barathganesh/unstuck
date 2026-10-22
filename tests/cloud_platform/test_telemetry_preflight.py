"""The telemetry preflight, written first. Offline, no account reachable.

The defect this pins is a live claim that passes because the configuration was
complete. A complete configuration proves the deployment inputs exist; it
proves nothing about latency, token usage, session counts, or error rates,
because those are observed facts and this leaf has observed nothing. So the
telemetry live check is `pending` whatever the configuration says, and its
detail names the inputs still missing so a reader can tell an unconfigured
deployment from a configured one that has simply not run.

The offline check is the opposite: it asserts something this leaf really can
prove, that the configuration validated and projected the documented variables
with no credential among them.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/cloud_platform/test_telemetry_preflight.py -q
"""

import pytest
from pydantic import ValidationError

from tests.telemetry.placeholders import (
    SYNTHETIC_ACCOUNT,
    SYNTHETIC_AGENT,
    SYNTHETIC_ENDPOINT,
    SYNTHETIC_LOG_GROUP,
    SYNTHETIC_LOG_STREAM,
    SYNTHETIC_MODEL,
    SYNTHETIC_PARTITION,
    SYNTHETIC_REGION,
    SYNTHETIC_RUNTIME,
    SYNTHETIC_RUNTIME_ARN,
)
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.guards import SecretGuard
from unstuck.cloud.preflight import (
    PreflightReport,
    SpendGateInputs,
    run_preflight,
    telemetry_checks,
)
from unstuck.cloud.telemetry import REQUIRED_LIVE_FIELDS, TelemetryRuntimeConfig


def configured() -> TelemetryRuntimeConfig:
    return TelemetryRuntimeConfig(
        account_id=SYNTHETIC_ACCOUNT,
        region=SYNTHETIC_REGION,
        partition=SYNTHETIC_PARTITION,
        agent_id=SYNTHETIC_AGENT,
        runtime_id=SYNTHETIC_RUNTIME,
        endpoint_name=SYNTHETIC_ENDPOINT,
        runtime_arn=SYNTHETIC_RUNTIME_ARN,
        model_id=SYNTHETIC_MODEL,
        log_group=SYNTHETIC_LOG_GROUP,
        log_stream=SYNTHETIC_LOG_STREAM,
        observability_enabled=True,
        unified_telemetry=True,
        transaction_search="enabled",
    )


def report(
    config: CloudResourceConfig, telemetry: TelemetryRuntimeConfig | None
) -> PreflightReport:
    return run_preflight(config, SpendGateInputs(), telemetry=telemetry)


def test_a_complete_configuration_passes_only_the_offline_check_offline(
    config: CloudResourceConfig,
) -> None:
    checks = {check.id: check for check in report(config, configured()).checks}
    assert checks["telemetry-configuration"].status == "passed"
    assert checks["telemetry-live-evidence"].status == "pending"
    assert checks["telemetry-live-evidence"].scope == "live"


def test_a_complete_configuration_never_makes_the_live_gate_pass_offline(
    config: CloudResourceConfig,
) -> None:
    assert report(config, configured()).live_passed is False


def test_an_absent_configuration_leaves_the_offline_gate_intact_offline(
    config: CloudResourceConfig,
) -> None:
    built = report(config, None)
    checks = {check.id: check for check in built.checks}
    assert checks["telemetry-configuration"].status == "passed"
    assert checks["telemetry-configuration"].reason is None
    assert built.offline_passed is True
    assert "telemetry-live-evidence" in {check.id for check in built.unmet()}
    assert built.live_passed is False


def test_the_live_detail_names_every_unsupplied_input_offline() -> None:
    _, live = telemetry_checks(TelemetryRuntimeConfig())
    for name in REQUIRED_LIVE_FIELDS:
        assert name in live.detail
    _, complete = telemetry_checks(configured())
    assert "none" in complete.detail


def test_the_live_detail_names_the_unmet_conditions_offline() -> None:
    """Regression: readiness read as complete inputs and nothing else.

    `configured()` supplies every deployment input and enables observability,
    so the inputs alone say nothing is missing. The detail has to go on to name
    what still blocks a live claim, or a reader takes "none" for readiness.
    """
    _, complete = telemetry_checks(configured())
    assert "Unsupplied deployment inputs: none" in complete.detail
    assert "live_classification" in complete.detail
    assert "live_evidence_gate" in complete.detail
    assert "observed_records" in complete.detail
    assert complete.status == "pending"


def test_a_disabled_deployment_says_telemetry_is_off_offline() -> None:
    disabled = TelemetryRuntimeConfig.model_validate(
        configured().model_dump() | {"observability_enabled": False}
    )
    _, live = telemetry_checks(disabled)
    assert "observability_enabled" in live.detail
    assert live.status == "pending"


def test_an_absent_configuration_still_names_its_unmet_conditions_offline() -> None:
    _, live = telemetry_checks(None)
    assert "telemetry_configuration" in live.detail
    assert "observed_records" in live.detail


def test_an_established_gate_never_makes_the_live_check_pass_offline(
    config: CloudResourceConfig,
) -> None:
    """The gate is necessary, never sufficient: no record has been observed."""
    ready = TelemetryRuntimeConfig.model_validate(
        configured().model_dump()
        | {"classification": "live", "live_evidence_gate": "established"}
    )
    _, live = telemetry_checks(ready)
    assert live.status == "pending"
    assert live.detail.endswith("observed_records.")
    assert report(config, ready).live_passed is False


def test_the_configuration_export_stays_free_of_a_sensitive_marker_offline() -> None:
    guard = SecretGuard()
    guard.check(configured().variables().mapping)
    guard.check(
        {
            name: str(value)
            for name, value in configured().model_dump(mode="json").items()
            if value is not None
        }
    )


def test_the_telemetry_configuration_matches_the_resource_configuration_offline(
    config: CloudResourceConfig,
) -> None:
    telemetry = configured()
    assert telemetry.account_id == config.account_id
    assert telemetry.region == config.region
    with pytest.raises(ValidationError):
        TelemetryRuntimeConfig.model_validate(
            telemetry.model_dump() | {"region": "eu-west-2"}
        )


def test_the_preflight_constructs_no_client_even_when_one_is_offered(
    config: CloudResourceConfig,
) -> None:
    def refuse(service: str) -> object:
        raise AssertionError("the telemetry preflight builds no client")

    built = run_preflight(
        config, SpendGateInputs(), client_factory=refuse, telemetry=configured()
    )
    assert built.live_passed is False
