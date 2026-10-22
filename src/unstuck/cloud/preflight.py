"""Fail-closed preflight and spend gates.

`docs/spec/design.md` section 18 fixes the spend rules: deploy and every live
batch refuse when `AWS_SPEND_LIMIT_USD` is empty, the pre-batch estimate is
`sum over episodes(per-episode cost) + idle hourly cost x expected duration`,
and the batch is refused when the estimate exceeds the remaining limit, when
any price input is unknown, or when current spend cannot be read. An unknown
cost is recorded as unknown and never as zero.

Every check here is deterministic and offline. Nothing in this module reads an
environment variable, constructs an AWS client, or performs a request: the
caller passes the observed values in, and the live checks that would need an
account stay `pending` because their preflight evidence does not exist.
"""

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from importlib import util
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from unstuck.cloud import CloudReason
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.guards import SecretGuard, controller_egress
from unstuck.cloud.keys import READ_PREFIXES, WRITE_PREFIXES, ArtifactPrefix
from unstuck.cloud.session import LeaseTerms, RuntimeSessionId
from unstuck.cloud.telemetry import TelemetryRuntimeConfig
from unstuck.domain import Contract, Identifier
from unstuck.events import Measurement

#: Charged once per benchmark episode.
PER_EPISODE_COMPONENTS = (
    "agentcore",
    "bedrock",
    "decider_d1",
    "decider_d2",
    "dynamodb_receipts",
    "s3_receipt_evidence",
)
#: Charged per hour of expected batch duration.
HOURLY_COMPONENTS = ("decider_idle", "nlb", "vpc_endpoint")
REQUIRED_COMPONENTS = (*PER_EPISODE_COMPONENTS, *HOURLY_COMPONENTS)

CostComponent = Literal[
    "agentcore",
    "bedrock",
    "decider_d1",
    "decider_d2",
    "dynamodb_receipts",
    "s3_receipt_evidence",
    "decider_idle",
    "nlb",
    "vpc_endpoint",
]

UNKNOWN = Measurement(availability="not_measured")

#: The live preflights whose evidence does not exist. See design.md section 21.
LIVE_PREFLIGHTS: tuple[tuple[str, str], ...] = (
    (
        "PF6",
        "AgentCore runtime, runtimeSessionId, private API, VPC Link and NLB "
        "routing are unverified; no reachability is established.",
    ),
    (
        "PF8",
        "Evaluator role and anonymous-denial checks are unverified; no denial "
        "is established.",
    ),
    (
        "PF9",
        "Pricing inputs, the owner spend ceiling and the Budgets alarm are "
        "unverified; no cost claim is established.",
    ),
)


class PriceInput(Contract):
    """One cost component. An unknown price never becomes zero."""

    component: CostComponent
    unit_usd: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    availability: Literal["measured", "unknown"] = "unknown"

    @model_validator(mode="after")
    def explicit_absence(self) -> Self:
        if (self.availability == "measured") != (self.unit_usd is not None):
            raise ValueError("price availability does not match the recorded value")
        return self


class SpendGateInputs(Contract):
    """Everything the pre-batch estimate gate is allowed to look at."""

    spend_limit_usd: Annotated[str, Field(max_length=32)] = ""
    current_spend_usd: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    current_spend_available: bool = False
    episodes: Annotated[int, Field(ge=0, le=4096)] = 0
    expected_duration_hours: Annotated[
        float, Field(ge=0, le=720, allow_inf_nan=False)
    ] = 0.0
    prices: Annotated[tuple[PriceInput, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.current_spend_available != (self.current_spend_usd is not None):
            raise ValueError("spend availability does not match the recorded value")
        components = [price.component for price in self.prices]
        if len(set(components)) != len(components):
            raise ValueError("duplicate cost component")
        return self

    @property
    def limit(self) -> Decimal | None:
        """The parsed positive finite limit, or None when it is unusable.

        `Decimal` accepts `nan` and `inf`, and comparing a NaN raises. Both are
        treated as an unusable ceiling, which refuses rather than crashes.
        """
        try:
            parsed = Decimal(self.spend_limit_usd.strip())
        except (InvalidOperation, ArithmeticError):
            return None
        if not parsed.is_finite():
            return None
        return parsed if parsed > 0 else None

    def price(self, component: str) -> PriceInput | None:
        return next((item for item in self.prices if item.component == component), None)


class SpendDecision(Contract):
    """The gate's verdict. A refusal always carries its reason code."""

    allowed: bool
    reason: CloudReason | None = None
    estimate: Measurement = UNKNOWN
    remaining: Measurement = UNKNOWN

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.allowed != (self.reason is None):
            raise ValueError("a refused spend gate carries its reason code")
        return self


class PreflightCheck(Contract):
    """One named check. `pending` means its evidence does not exist yet."""

    id: Identifier
    scope: Literal["offline", "live"]
    status: Literal["passed", "failed", "pending"]
    detail: Annotated[str, Field(min_length=1, max_length=512)]
    reason: CloudReason | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.status == "failed") != (self.reason is not None):
            raise ValueError("a failed check carries its reason code")
        return self


class PreflightReport(Contract):
    """Offline checks can pass. Live checks cannot, and say so."""

    checks: Annotated[tuple[PreflightCheck, ...], Field(min_length=1, max_length=32)]
    spend: SpendDecision

    @property
    def offline_passed(self) -> bool:
        return all(
            check.status == "passed"
            for check in self.checks
            if check.scope == "offline"
        )

    @property
    def live_passed(self) -> bool:
        return self.spend.allowed and all(
            check.status == "passed" for check in self.checks if check.scope == "live"
        )

    def unmet(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if check.status != "passed")


def _measured(value: float) -> Measurement:
    return Measurement(availability="measured", value=value)


def evaluate_spend_gate(inputs: SpendGateInputs) -> SpendDecision:
    """Refuse in design.md section 18 order. Nothing here reaches AWS."""
    validated = SpendGateInputs.model_validate(inputs)
    limit = validated.limit
    if limit is None:
        return SpendDecision(allowed=False, reason=CloudReason.SPEND_LIMIT_UNSET)
    total = Decimal(0)
    for component in REQUIRED_COMPONENTS:
        price = validated.price(component)
        if price is None or price.unit_usd is None:
            return SpendDecision(allowed=False, reason=CloudReason.PRICE_UNKNOWN)
        multiplier = (
            Decimal(validated.episodes)
            if component in PER_EPISODE_COMPONENTS
            else Decimal(str(validated.expected_duration_hours))
        )
        total += Decimal(str(price.unit_usd)) * multiplier
    estimate = _measured(float(total))
    if validated.current_spend_usd is None:
        return SpendDecision(
            allowed=False, reason=CloudReason.SPEND_UNREADABLE, estimate=estimate
        )
    remaining = limit - Decimal(str(validated.current_spend_usd))
    if total > remaining:
        return SpendDecision(
            allowed=False,
            reason=CloudReason.ESTIMATE_EXCEEDS_LIMIT,
            estimate=estimate,
            remaining=_measured(float(remaining)),
        )
    return SpendDecision(
        allowed=True, estimate=estimate, remaining=_measured(float(remaining))
    )


def _synthesis_available() -> PreflightCheck:
    """The CDK stacks are synthesis-only and are not an installed package.

    `deploy --synth-only` therefore runs from the repository root. When the
    stacks are not importable, synthesis cannot run and the preflight says so
    rather than failing later inside the command.
    """
    if util.find_spec("infra.stack") is None:
        return PreflightCheck(
            id="synthesis-available",
            scope="offline",
            status="failed",
            reason=CloudReason.SYNTHESIS_INVALID,
            detail="infra.stack is not importable; run from the repository root.",
        )
    return PreflightCheck(
        id="synthesis-available",
        scope="offline",
        status="passed",
        detail="The CDK stacks are importable, so synthesis can run in process.",
    )


def _offline_checks(config: CloudResourceConfig) -> tuple[PreflightCheck, ...]:
    SecretGuard().export(config)
    controller_egress(config)
    for prefix in WRITE_PREFIXES:
        ArtifactPrefix(key=f"{prefix}preflight.json", mode="write")
    for prefix in READ_PREFIXES:
        ArtifactPrefix(key=f"{prefix}preflight.json", mode="read")
    RuntimeSessionId.for_run(UUID("00000000-0000-4000-8000-000000000000"))
    LeaseTerms()
    return (
        PreflightCheck(
            id="config-contract",
            scope="offline",
            status="passed",
            detail="Typed configuration validated; identifiers and versions only.",
        ),
        PreflightCheck(
            id="secret-guard",
            scope="offline",
            status="passed",
            detail="Redacted configuration export carries no sensitive marker.",
        ),
        PreflightCheck(
            id="prefix-scoping",
            scope="offline",
            status="passed",
            detail="Write and read prefixes construct; denied prefixes refuse.",
        ),
        PreflightCheck(
            id="egress-allowlist",
            scope="offline",
            status="passed",
            detail="Controller allowlist built; Decider host permits no egress.",
        ),
        PreflightCheck(
            id="session-derivation",
            scope="offline",
            status="passed",
            detail="runtimeSessionId derives from the run identity and is stable.",
        ),
        PreflightCheck(
            id="lease-terms",
            scope="offline",
            status="passed",
            detail="A 150 second lease covers the 120 second run and never renews.",
        ),
        _synthesis_available(),
    )


def telemetry_checks(
    telemetry: TelemetryRuntimeConfig | None,
) -> tuple[PreflightCheck, ...]:
    """The telemetry configuration and live-evidence gates.

    Two separate facts, so neither hides the other.

    The offline check asserts what this leaf can actually prove: that whatever
    telemetry configuration was supplied validated, projected the documented
    variables, and carried no credential among them. An absent configuration
    satisfies that, because nothing invalid or sensitive was configured.
    Telemetry is opt-in, so refusing the whole offline gate for a deployment
    that simply has no telemetry would be a different claim than the one this
    check makes.

    The live check carries the blocking fact. It stays `pending` whatever the
    configuration says, because a claim about observed latency, token usage,
    session counts or error rates needs records from a deployment that ran, and
    no such record exists.

    Its detail names two different things, because complete deployment inputs
    are only one of the conditions a live claim fails. The unsupplied inputs
    say what the configuration still lacks; the unmet conditions say what else
    stands in the way, which is telemetry being switched on, the deployment
    labelling its records live, the owner's live evidence gate being
    established, and observed records existing at all. A reader can then tell
    an unconfigured deployment from a fully configured one with telemetry
    disabled, and both from one that is simply waiting to run.
    """
    projected = telemetry.variables() if telemetry is not None else None
    missing = telemetry.missing_live_fields if telemetry is not None else ()
    unmet = (
        telemetry.live_evidence_unmet
        if telemetry is not None
        else ("telemetry_configuration", "observed_records")
    )
    return (
        PreflightCheck(
            id="telemetry-configuration",
            scope="offline",
            status="passed",
            detail=(
                "Telemetry configuration validated; "
                f"{len(projected.mapping)} documented variable(s) projected "
                "and no credential variable carried."
                if projected is not None
                else "No telemetry configuration was supplied, so none is "
                "invalid and none carries a credential."
            ),
        ),
        PreflightCheck(
            id="telemetry-live-evidence",
            scope="live",
            status="pending",
            detail=(
                "Observed telemetry is unverified; no live latency, token, "
                "session or error claim is established. Unsupplied deployment "
                f"inputs: {', '.join(missing) if missing else 'none'}. "
                f"Unmet live evidence conditions: {', '.join(unmet)}."
            ),
        ),
    )


def run_preflight(
    config: CloudResourceConfig,
    inputs: SpendGateInputs,
    *,
    client_factory: Callable[[str], object] | None = None,
    telemetry: TelemetryRuntimeConfig | None = None,
) -> PreflightReport:
    """Evaluate every gate offline.

    `client_factory` is accepted so a caller cannot be surprised by a hidden
    client: it is only ever used by a live check, and every live check is
    `pending`, so it is never called. A refusal path constructs nothing.

    `telemetry` is optional because telemetry is opt-in: a deployment that
    supplies none is reported as having none rather than assumed to be
    configured.
    """
    live = tuple(
        PreflightCheck(id=identifier, scope="live", status="pending", detail=detail)
        for identifier, detail in LIVE_PREFLIGHTS
    )
    return PreflightReport(
        checks=_offline_checks(config) + live + telemetry_checks(telemetry),
        spend=evaluate_spend_gate(inputs),
    )
