"""Budget and fail-closed tests for the preflight and spend gates.

Offline and synthetic. Every price below is an invented development figure, not
a quoted AWS price: PF9 pricing evidence does not exist, so no cost conclusion
is drawn from these numbers.
"""

from collections.abc import Callable

import pytest

from unstuck.cloud import CloudReason
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.preflight import (
    HOURLY_COMPONENTS,
    PER_EPISODE_COMPONENTS,
    REQUIRED_COMPONENTS,
    PriceInput,
    SpendGateInputs,
    evaluate_spend_gate,
    run_preflight,
)

SYNTHETIC_UNIT_USD = 0.25


def measured_prices(
    *, omit: str | None = None, unknown: str | None = None
) -> tuple[PriceInput, ...]:
    """Build a complete synthetic price table, optionally degraded."""
    return tuple(
        PriceInput(component=component, unit_usd=None, availability="unknown")
        if component == unknown
        else PriceInput(
            component=component,
            unit_usd=SYNTHETIC_UNIT_USD,
            availability="measured",
        )
        for component in REQUIRED_COMPONENTS
        if component != omit
    )


def inputs(**changes: object) -> SpendGateInputs:
    return SpendGateInputs.model_validate(
        {
            "spend_limit_usd": "100.00",
            "current_spend_usd": 10.0,
            "current_spend_available": True,
            "episodes": 4,
            "expected_duration_hours": 2.0,
            "prices": measured_prices(),
        }
        | changes
    )


def exploding_factory() -> Callable[[str], object]:
    def build(service: str) -> object:
        raise AssertionError(f"no AWS client may be constructed offline: {service}")

    return build


@pytest.mark.parametrize("limit", ["", "   ", "0", "-5", "not-a-number"])
def test_spend_gate_refuses_an_unusable_limit_offline(limit: str) -> None:
    decision = evaluate_spend_gate(inputs(spend_limit_usd=limit))
    assert decision.allowed is False
    assert decision.reason is CloudReason.SPEND_LIMIT_UNSET
    assert decision.estimate.availability == "not_measured"
    assert decision.estimate.value is None


@pytest.mark.parametrize("component", REQUIRED_COMPONENTS)
def test_spend_gate_refuses_an_unknown_or_missing_price_offline(component: str) -> None:
    unknown = evaluate_spend_gate(inputs(prices=measured_prices(unknown=component)))
    assert unknown.reason is CloudReason.PRICE_UNKNOWN
    missing = evaluate_spend_gate(inputs(prices=measured_prices(omit=component)))
    assert missing.reason is CloudReason.PRICE_UNKNOWN
    assert unknown.estimate.value is None and missing.estimate.value is None


def test_spend_gate_refuses_an_unreadable_current_spend_offline() -> None:
    decision = evaluate_spend_gate(
        inputs(current_spend_usd=None, current_spend_available=False)
    )
    assert decision.reason is CloudReason.SPEND_UNREADABLE
    assert decision.remaining.availability == "not_measured"
    assert decision.remaining.value is None


def test_spend_gate_refuses_an_estimate_over_the_remaining_limit_offline() -> None:
    expensive = inputs(spend_limit_usd="2.00", current_spend_usd=1.0, episodes=64)
    decision = evaluate_spend_gate(expensive)
    assert decision.reason is CloudReason.ESTIMATE_EXCEEDS_LIMIT
    assert decision.estimate.availability == "measured"
    assert decision.estimate.value is not None
    assert decision.remaining.value == 1.0
    assert decision.estimate.value > decision.remaining.value


def test_spend_gate_allows_a_complete_estimate_within_the_limit_offline() -> None:
    decision = evaluate_spend_gate(inputs())
    per_episode = SYNTHETIC_UNIT_USD * len(PER_EPISODE_COMPONENTS) * 4
    hourly = SYNTHETIC_UNIT_USD * len(HOURLY_COMPONENTS) * 2.0
    assert decision.allowed is True
    assert decision.reason is None
    assert decision.estimate.value == pytest.approx(per_episode + hourly)
    assert decision.remaining.value == pytest.approx(90.0)


def test_no_aws_client_is_constructed_on_any_refusal_path_offline(
    config: CloudResourceConfig,
) -> None:
    for degraded in (
        inputs(spend_limit_usd=""),
        inputs(prices=measured_prices(unknown="bedrock")),
        inputs(current_spend_usd=None, current_spend_available=False),
        inputs(spend_limit_usd="2.00", current_spend_usd=1.0, episodes=64),
    ):
        report = run_preflight(config, degraded, client_factory=exploding_factory())
        assert report.live_passed is False
        assert report.spend.allowed is False


def test_preflight_passes_offline_checks_and_holds_live_checks_pending(
    config: CloudResourceConfig,
) -> None:
    report = run_preflight(config, inputs(), client_factory=exploding_factory())
    assert report.offline_passed is True
    assert report.live_passed is False
    live = {check.id: check for check in report.checks if check.scope == "live"}
    assert {"PF6", "PF8", "PF9"} <= set(live)
    assert all(check.status == "pending" for check in live.values())
    assert report.spend.allowed is True
    assert all(
        check.status == "passed" for check in report.checks if check.scope == "offline"
    )
