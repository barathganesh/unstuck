"""Deterministic offline fakes for the Decider and diagnostic seams.

These are scripted recorders. They have no network path, no credentials, and
no access to evaluator truth, hidden causes, or expected answers. They exist so
the bounded orchestration can be proven offline; they cannot satisfy any live
gate, and no latency or cost measured against them is a real measurement.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from unstuck.cascade.execution import DiagnosticRequest
from unstuck.cascade.projector import SelectorInput
from unstuck.cascade.receipts import EvidenceDelta
from unstuck.cascade.selection import RawSelection, ValidationStatus
from unstuck.cascade.serialization import canonical_digest
from unstuck.cascade.verifier import DiagnosticReport
from unstuck.decision import DecisionState, OptionProbability
from unstuck.domain import Reason, RuntimeFault

#: Keyed on the reported status and the two scripted deltas, matching the
#: verifier's own taxonomy: only a read whose admitted fact moved a supported
#: disposition may name what it opened.
DEFAULT_REASONS: dict[tuple[str, bool, bool], Reason] = {
    ("evidence", True, True): Reason.POPULATED_INITIAL,
    ("evidence", True, False): Reason.INSUFFICIENT,
    ("evidence", False, False): Reason.INSUFFICIENT,
    ("provider_error", False, False): Reason.TOOL_ERROR,
    ("invalid_evidence", False, False): Reason.CONFLICT,
}


class MockSelection(RawSelection):
    """One scripted answer. The type itself cannot name the real provider.

    `RawSelection.provider` admits `decider` because a real checkpoint has to be
    able to say so. Narrowing the literal here is what makes the claim one-way:
    every scripted stand-in answers through this type, so no fake in this
    repository can present itself as a real provider even by mistake.
    """

    provider: Literal["mock"] = "mock"


@dataclass(frozen=True)
class ScriptedChoice:
    """One scripted selector answer, or a scripted transport failure."""

    selected_id: str | None = None
    confidence: float | None = 0.95
    validation_status: ValidationStatus = "valid"
    transport_error: bool = False
    delay_seconds: float = 0.0
    candidate_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ScriptedOutcome:
    """One scripted diagnostic result.

    `changed` scripts evidence novelty and `moved` scripts decision progress.
    They are independent, which is what lets a test script the real case the
    strict stalled conjunction cannot express: a read that admitted a genuine
    fact and still left every supported disposition where it was. `moved`
    defaults to `changed`, so a caller that does not separate the two keeps
    exactly its previous meaning.
    """

    status: Literal["evidence", "provider_error", "invalid_evidence"] = "evidence"
    changed: bool = True
    moved: bool | None = None
    reason_code: Reason | None = None
    transport_error: bool = False
    delay_seconds: float = 0.0
    drop_call_identity: bool = False

    @property
    def progressed(self) -> bool:
        return self.changed if self.moved is None else self.moved


@dataclass
class ScriptedDecider:
    """Replays one scripted answer per pass and records every request."""

    choices: Sequence[ScriptedChoice]
    model_revision: str = "scripted-v1"
    requests: list[SelectorInput] = field(default_factory=list)

    async def select(self, request: SelectorInput) -> RawSelection:
        self.requests.append(request)
        index = min(request.pass_index - 1, len(self.choices) - 1)
        choice = self.choices[index]
        if choice.delay_seconds:
            await asyncio.sleep(choice.delay_seconds)
        if choice.transport_error:
            raise TimeoutError("scripted provider failure")
        options = (
            choice.candidate_ids
            if choice.candidate_ids is not None
            else request.option_ids
        )
        return MockSelection(
            model_revision=self.model_revision,
            decision_id=uuid4(),
            model_call_id=uuid4(),
            pass_index=request.pass_index,
            candidate_ids=options,
            selected_id=choice.selected_id,
            confidence=choice.confidence,
            probabilities=tuple(
                OptionProbability(
                    id=identifier,
                    probability=float(identifier == choice.selected_id),
                )
                for identifier in options
            ),
            validation_status=choice.validation_status,
        )


@dataclass
class ScriptedExecutor:
    """Replays one scripted outcome per public tool and records every request.

    The projected state is scripted too. A fake holds no authoritative state of
    its own, so `state` is whatever the test declares the controller would
    project after a read; a cascade that reaches a second decision needs one.
    """

    outcomes: Mapping[str, ScriptedOutcome] = field(default_factory=dict)
    default: ScriptedOutcome = ScriptedOutcome()
    requests: list[DiagnosticRequest] = field(default_factory=list)
    version: int = 0
    #: The scripted disposition generation, advanced only by a read the script
    #: declares moved a supported gate.
    opened: int = 0
    state: DecisionState | None = None

    def project(self) -> DecisionState:
        if self.state is None:
            raise RuntimeFault(Reason.INVARIANT)
        return self.state

    async def execute(self, request: DiagnosticRequest) -> DiagnosticReport:
        self.requests.append(request)
        outcome = self.outcomes.get(request.public_tool_id, self.default)
        if outcome.delay_seconds:
            await asyncio.sleep(outcome.delay_seconds)
        if outcome.transport_error:
            raise TimeoutError("scripted diagnostic failure")
        before = canonical_digest(["admissible", self.version])
        opened = canonical_digest(["disposition", self.opened])
        if outcome.changed:
            self.version += 1
        if outcome.progressed:
            self.opened += 1
        after = canonical_digest(["admissible", self.version])
        moved = canonical_digest(["disposition", self.opened])
        reason = (
            outcome.reason_code
            or DEFAULT_REASONS[(outcome.status, outcome.changed, outcome.progressed)]
        )
        return DiagnosticReport(
            diagnostic_call_id=(
                None if outcome.drop_call_identity else request.diagnostic_call_id
            ),
            capability=request.capability,
            public_tool_id=request.public_tool_id,
            arguments_hash=request.arguments_hash,
            status=outcome.status,
            delta=EvidenceDelta(
                evidence_before_hash=before,
                evidence_after_hash=after,
                evidence_ids=(uuid4(),) if outcome.changed else (),
                disposition_before_hash=opened,
                disposition_after_hash=moved,
            ),
            admissible=outcome.status == "evidence",
            reason_code=reason,
        )
