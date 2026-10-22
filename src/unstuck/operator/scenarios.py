"""The allowlisted synthetic scenarios a local run may be started from.

A scenario token is not a free-form label. It names one entry in the allowlist
below, each of which resolves to one deterministic synthetic fixture and one
isolated receipt namespace. An unknown token is refused before any run is
created, so the surface cannot be driven into a dataset nobody declared.

Choosing a scenario changes the run that actually happens: the fixtures differ
in upstream state, key encoding, and row presence, so the real controller
reaches a different terminal disposition through a different path for each one.
Nothing here decides an outcome, selects a tool, or authorizes a repair; it
only declares which synthetic inputs exist.

`memory-repeat` additionally declares one capability as already observed. The
record is produced by the real evidence engine before the run starts, so the
run genuinely re-observes a fact it already holds. That is what gives the
deterministic verifier a stalled read to write a no-progress receipt for, and
what makes the cross-call refusal visible on the second call of the same
scenario.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from unstuck.domain import EvidenceRecord, TaskSpec
from unstuck.fixtures import Country, Fixture, load_fixture
from unstuck.operator import OperatorFault, OperatorReason

if TYPE_CHECKING:
    from unstuck.clock import Clock

#: The arm the local surface runs. Memory is the product's own claim, so the
#: surface exercises it rather than the one-pass arm.
LOCAL_ARM: Literal["decider_memory"] = "decider_memory"


@dataclass(frozen=True)
class Scenario:
    """One declared synthetic scenario and the fixture it resolves to."""

    token: str
    title: str
    detail: str
    case_id: str
    changes: Mapping[str, Any] = field(default_factory=dict)
    #: Capabilities whose evidence the run starts already holding.
    preobserved: tuple[str, ...] = ()

    def fixture(self) -> Fixture:
        """Build this scenario's fixture from the one synthetic dataset."""
        return Fixture.model_validate(load_fixture().model_dump() | dict(self.changes))


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        token="repairable-join",
        title="Repairable join key",
        detail=(
            "The dimension keys carry a prefix the fact keys do not. One "
            "allowlisted normalization is applied and the report verifies."
        ),
        case_id="operator-repairable-join",
    ),
    Scenario(
        token="memory-repeat",
        title="Repeated read, refused by a receipt",
        detail=(
            "The run starts already holding one observed schema read, so "
            "reading it again admits nothing. The first call writes a "
            "no-progress receipt and re-decides once in the same call; a "
            "second call on this scenario refuses the identical repetition "
            "before it runs."
        ),
        case_id="operator-memory-repeat",
        preobserved=("inspect_schema_keys",),
    ),
    Scenario(
        token="verified-empty",
        title="Verified empty result",
        detail=(
            "The scope is covered and the keys agree, and there are no orders "
            "for the date. The empty result is proven rather than assumed."
        ),
        case_id="operator-verified-empty",
        changes={
            "orders": (),
            "countries_key_encoding": "plain",
            "countries": (
                Country(
                    country_id="US", country_code="US", country_name="United States"
                ),
            ),
        },
    ),
    Scenario(
        token="pending-partition",
        title="Bounded wait on pending evidence",
        detail=(
            "The upstream partition has not arrived. The first read admits "
            "nothing, the one same-call second decision reaches the pipeline "
            "read, and one bounded wait and recheck runs against explicit "
            "pending evidence. The run reports pending rather than a number."
        ),
        case_id="operator-pending-partition",
        changes={"job_state": "pending", "scope_complete": False},
        preobserved=("inspect_schema_keys",),
    ),
    Scenario(
        token="stalled-upstream",
        title="Incomplete upstream partition",
        detail=(
            "The upstream job has not finished for this scope and never "
            "arrives. The run ends unresolved and names why, rather than "
            "reporting a number it cannot prove."
        ),
        case_id="operator-stalled-upstream",
        changes={"job_state": "pending", "scope_complete": False},
    ),
)

#: Every token the surface accepts, in declaration order.
SCENARIO_TOKENS: tuple[str, ...] = tuple(scenario.token for scenario in SCENARIOS)


def preobserved_evidence(
    scenario: Scenario, fixture: Fixture, *, clock: "Clock"
) -> tuple[EvidenceRecord, ...]:
    """Produce the evidence one scenario declares already observed.

    The records come from the real evidence engine, so a run that starts with
    them re-observes facts it genuinely holds rather than facts this module
    invented. That is what gives the deterministic verifier a stalled read to
    write a no-progress receipt for.

    This is shared rather than restated, because every surface that starts a
    scenario needs the same records: a second copy of it would be a second
    chance for one surface to drift into inventing evidence.
    """
    from unstuck.domain import QuerySpec
    from unstuck.query import EvidenceEngine

    if not scenario.preobserved:
        return ()
    query = QuerySpec(
        task=TaskSpec(
            dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
        ),
        dataset_version=fixture.dataset_version,
    )
    engine = EvidenceEngine(fixture, clock=clock)
    try:
        return tuple(
            EvidenceRecord.model_validate(
                engine.diagnose(capability, query).model_dump()
                | {"tool_id": capability}
            )
            for capability in scenario.preobserved
        )
    finally:
        engine.close()


def resolve_scenario(token: str) -> Scenario:
    """Resolve one allowlisted token. An unknown token fails closed."""
    for scenario in SCENARIOS:
        if scenario.token == token:
            return scenario
    raise OperatorFault(
        OperatorReason.SCENARIO_REJECTED,
        f"{token} is not one of: {', '.join(SCENARIO_TOKENS)}",
    )


__all__ = [
    "LOCAL_ARM",
    "SCENARIOS",
    "SCENARIO_TOKENS",
    "Scenario",
    "preobserved_evidence",
    "resolve_scenario",
]
