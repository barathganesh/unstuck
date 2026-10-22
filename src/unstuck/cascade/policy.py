"""Frozen selector policy and calibration for one run or benchmark arm.

`decider` is the explicit one-pass arm: it performs no history lookup and
writes no cross-call receipts. `decider_memory` adds bounded cross-call
Decision Receipts. Both arms get the identical same-call second decision, so
the only difference between them is persistent memory, never the call budget.
"""

from typing import Literal

from pydantic import Field

from unstuck.cascade.identity import BoundVersions
from unstuck.cascade.serialization import canonical_digest
from unstuck.domain import Contract, Identifier, Probability

Arm = Literal["decider", "decider_memory"]

#: Which risk tier a structurally valid answer is accepted under.
#:
#: `strict_confidence` is the product default and the only tier any
#: state-changing path may run under: a valid answer must additionally clear
#: the configured confidence floor. `read_only_structural` accepts a valid
#: answer naming an available, executable, argument-free local read whatever
#: confidence it reported, because the worst outcome of a wrong accept there is
#: one read-only observation the verifier then classifies, inside the same
#: two-read bound. Neither tier is calibration, and the reported confidence is
#: recorded telemetry rather than execution authority in either.
AcceptanceTier = Literal["strict_confidence", "read_only_structural"]
ACCEPTANCE_TIERS: tuple[AcceptanceTier, ...] = (
    "strict_confidence",
    "read_only_structural",
)
#: The product's uncalibrated default floor. Unchanged, and still labeled
#: unmeasured: nothing in this project has measured reported confidence
#: against outcomes.
STRICT_CONFIDENCE_MIN: Probability = 0.80
#: The published surface the relaxed tier is only sound over. A card outside
#: either set cannot be accepted under it at any confidence, so the tier is
#: structurally unable to reach a write or an argument-taking capability.
READ_ONLY_INPUT_SCHEMAS = frozenset({"no_arguments"})
READ_ONLY_COST_CLASSES = frozenset({"local_read"})


class Calibration(Contract):
    """Development-selected thresholds, frozen before any held-out call."""

    label: Literal["bootstrap_unmeasured"] = "bootstrap_unmeasured"
    acceptance_tier: AcceptanceTier = "strict_confidence"
    first_confidence_min: Probability = STRICT_CONFIDENCE_MIN
    final_confidence_min: Probability = STRICT_CONFIDENCE_MIN

    @property
    def calibration_hash(self) -> str:
        return canonical_digest(self)


class CascadePolicy(Contract):
    """Explicit arm selection. There is no silent runtime bypass."""

    baseline_id: Arm
    model_revision: Identifier
    calibration: Calibration = Field(default_factory=Calibration)

    @property
    def memory_authorized(self) -> bool:
        return self.baseline_id == "decider_memory"

    @property
    def first_confidence_min(self) -> float:
        return self.calibration.first_confidence_min

    @property
    def final_confidence_min(self) -> float:
        return self.calibration.final_confidence_min

    @property
    def acceptance_tier(self) -> AcceptanceTier:
        return self.calibration.acceptance_tier

    @property
    def policy_hash(self) -> str:
        return canonical_digest(self)

    def versions(self, dataset_version: str) -> BoundVersions:
        return BoundVersions(
            dataset_version=dataset_version,
            model_revision=self.model_revision,
            policy_hash=self.policy_hash,
            calibration_hash=self.calibration.calibration_hash,
        )
