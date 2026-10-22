"""runtimeSessionId derivation and the controller-writer lease policy.

`docs/spec/design.md` section 11 fixes the shape: the session identity is
`"unstuck-run-" + run_id` and the same run always uses the same identity, so a
duplicate invocation cannot create a second session. The lease is acquired by
one conditional update, lasts 150 seconds to cover the 120 second run, and is
never renewed. A duplicate invocation that loses the race performs no action.
An expired lease over a non-terminal run permits exactly one transition:
`failed` with `lease_expired_incomplete`.

The AgentCore session charset and length bound remain Pending PF6. What this
module asserts is the derivation and the lease algebra, which are ours.
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from unstuck.cloud import CloudReason
from unstuck.domain import Contract

SESSION_PREFIX = "unstuck-run-"
SESSION_PATTERN = r"^[A-Za-z0-9_-]{33,100}$"
LEASE_SECONDS = 150.0
RUN_SECONDS = 120.0

ACQUIRE_CONDITION = "attribute_not_exists(lease_owner) AND #status = :created"
WRITE_CONDITION = "lease_owner = :owner AND state_version = :expected"
EVENT_CONDITION = "attribute_not_exists(SK)"

RunStatus = Literal[
    "created",
    "running",
    "completed",
    "empty_verified",
    "pending",
    "unresolved",
    "budget_exhausted",
    "failed",
]
TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "empty_verified",
        "pending",
        "unresolved",
        "budget_exhausted",
        "failed",
    }
)


class RuntimeSessionId(Contract):
    """The AgentCore session identity for one run. Stable across invocations."""

    value: Annotated[str, Field(pattern=SESSION_PATTERN)]

    @model_validator(mode="after")
    def derived(self) -> Self:
        if not self.value.startswith(SESSION_PREFIX):
            raise ValueError("session identity must carry the run prefix")
        tail = self.value[len(SESSION_PREFIX) :]
        try:
            run_id = UUID(tail)
        except ValueError as error:
            raise ValueError("session identity must end in its run identity") from error
        if str(run_id) != tail:
            raise ValueError("session identity must use the canonical run identity")
        return self

    @classmethod
    def for_run(cls, run_id: UUID) -> Self:
        return cls(value=f"{SESSION_PREFIX}{run_id}")

    @property
    def run_id(self) -> UUID:
        return UUID(self.value[len(SESSION_PREFIX) :])


class LeaseTerms(Contract):
    """Fixed lease window. The lease covers the run and is never renewed."""

    lease_seconds: Annotated[float, Field(gt=0, le=900, allow_inf_nan=False)] = (
        LEASE_SECONDS
    )
    run_seconds: Annotated[float, Field(gt=0, le=120, allow_inf_nan=False)] = (
        RUN_SECONDS
    )
    renewable: Literal[False] = False

    @model_validator(mode="after")
    def covering(self) -> Self:
        if self.lease_seconds <= self.run_seconds:
            raise ValueError("the lease must outlast the run it protects")
        return self


class LeaseState(Contract):
    """The observed META state a new invocation reads before it acts."""

    status: RunStatus
    lease_owner: UUID | None
    expired: bool

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.status == "created") != (self.lease_owner is None):
            raise ValueError("lease ownership does not match the recorded status")
        return self

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class LeaseDecision(Contract):
    """What this invocation is allowed to do. There is no third option."""

    action: Literal["acquire", "reject", "fail_expired"]
    reason: CloudReason | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.action == "acquire") != (self.reason is None):
            raise ValueError("a refused lease carries its reason code")
        return self


def lease_outcome(
    *, status: str, lease_owner: UUID | None, expired: bool
) -> LeaseDecision:
    """Decide deterministically from the observed state. No clock, no retry."""
    state = LeaseState.model_validate(
        {"status": status, "lease_owner": lease_owner, "expired": expired}
    )
    if state.status == "created":
        return LeaseDecision(action="acquire")
    if not state.terminal and state.expired:
        return LeaseDecision(action="fail_expired", reason=CloudReason.LEASE_EXPIRED)
    return LeaseDecision(action="reject", reason=CloudReason.LEASE_REJECTED)
