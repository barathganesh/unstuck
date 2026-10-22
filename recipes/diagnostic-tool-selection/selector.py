"""Decision Receipt tool selection: a standalone, reusable pattern.

This file deliberately imports nothing but the standard library. It does not
import the application it was extracted from, so an adopter inherits the
pattern and not somebody else's architecture. Substitute your own tool catalog,
your own model call, and your own tool executor; everything else here is yours
to keep.

The pattern, in one paragraph. Before each selection you build the *current*
authoritative state - including the identity of the arguments the next read
would run with - and look up any past receipts that match this exact problem.
You hand the model both, as public tool identifiers and labelled past outcomes
only, and it decides **fresh** every time: a past choice is evidence, never a
replayed answer. The chosen tool then runs through your executor, and a
deterministic verifier - not the model - decides what the outcome was and
writes a receipt. The authority a receipt can carry is only ever negative, and
it rests on two separately recorded facts: whether the read admitted evidence
the problem did not already hold, and whether that evidence moved one of the
gates your assessment uses. An exact receipt proving that the identical
capability with the identical arguments under the identical context moved no
gate removes that one execution from the offer before the model is asked, and
the one fresh first decision is made over what remains. A read that then runs
and moves nothing opens one second decision. There is no third decision.

That second dimension is the whole point. A read that returns real, admissible
data and still leaves the decision exactly where it was is the blind spot a
model repeats; recording only "did it return anything" cannot express it, and a
pattern that classifies every data-bearing read as useful can never refuse the
repetition.

Six rules make it safe:

* The model selects; it never decides the outcome. `classify_outcome` is
  deterministic code.
* The model never sees the private capability. It sees the public `tool_id` and
  an opaque `execution_key` that only your code can resolve. That holds for the
  tools it is offered, for the history it is shown, and for the executions it
  is told have already run.
* The second decision is built from the state *after* the first read, not from
  the state before it. Your executor returns a projection of the refreshed
  state, and the same-call receipt travels into the second decision labelled
  against that refreshed state rather than as an exact match on a state that
  no longer holds.
* Only the two disposition-negative classes block, only on an exact context
  match, and only for the identical `(capability, arguments)` execution key. A
  provider error, invalid evidence, or a read that actually moved a gate is
  history, not a prohibition. A block narrows the offer and nothing else: it
  never chooses, ranks, or reorders what remains.
* A receipt is identified by its content, never by its age. There is no expiry
  and no time window, so a receipt is not a cache.
* At most two decisions and two tool runs per problem, both enforced before the
  call they pay for.

Acceptance is a separate question from selection, and it has two tiers.
`strict_confidence` is the default and the only tier a state-changing path may
run under: a structurally valid answer must also clear the configured floor.
`read_only_structural` accepts a structurally valid answer whatever confidence
it reported, and only over a surface where every executable option offered is
argument-free and read-only. Neither tier is calibration, and neither lets a
reported confidence stand in for a fact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

SELECTOR_INPUT_MAX_CHARS = 8000
MAX_RECEIPT_REFERENCES = 4
MAX_DECISIONS = 2
MAX_TOOL_RUNS = 2
#: The contract version a published card is described under. Bump it when the
#: public shape of a card changes, so a receipt names the shape it was read in.
TOOL_CONTRACT_VERSION = "tools-v1"
#: The schema version the state projection and the receipt are written under.
DATA_SCHEMA_VERSION = "schema-v1"
#: What a receipt records when the caller passed no observation instant. It is
#: a label, never a value: this module has no clock of its own, so an adopter
#: supplies the UTC instant their own clock observed.
UNRECORDED_OBSERVATION = "not_recorded"

#: Which risk tier a structurally valid answer is accepted under.
#:
#: `strict_confidence` is the default and the only tier a state-changing path
#: may ever run under: a structurally valid answer must additionally clear the
#: configured confidence floor. `read_only_structural` accepts a structurally
#: valid answer naming an available, argument-free, read-only diagnostic
#: whatever confidence it reported, and is refused outright unless every
#: executable option offered is argument-free and read-only. It is sound only
#: because the worst outcome of a wrong accept over that surface is one local
#: observation the verifier then classifies, inside the same two-read bound.
#: Neither tier is calibration. In both, a reported confidence is recorded
#: telemetry and never a statement that the choice is correct.
AcceptanceTier = Literal["strict_confidence", "read_only_structural"]
ACCEPTANCE_TIERS: tuple[AcceptanceTier, ...] = (
    "strict_confidence",
    "read_only_structural",
)
#: The published surface the relaxed tier is sound over. A card outside either
#: set makes the surface unsafe for that tier at any confidence, so the tier is
#: structurally unable to reach a write or an argument-taking capability.
READ_ONLY_INPUT_SCHEMAS: frozenset[str] = frozenset({"no_arguments"})
READ_ONLY_COST_CLASSES: frozenset[str] = frozenset({"local_read"})

OutcomeClass = Literal[
    "useful_evidence",
    "disposition_unchanged",
    "no_progress",
    "provider_error",
    "invalid_evidence",
]
MatchClass = Literal["exact", "compatible", "no_match"]
RefusalKind = Literal["exact_repeat", "failed_execution"]
ResolutionPath = Literal["direct", "rechosen", "unresolved"]
#: The two outcomes that prove a fact was admitted. Both resolve a problem.
EVIDENCE_OUTCOMES: frozenset[str] = frozenset(
    {"useful_evidence", "disposition_unchanged"}
)
#: The only outcomes that may ever be eligible to refuse a future repetition.
#: Both are disposition-negative: one admitted nothing, the other admitted a
#: real fact that moved no gate. Eligibility is still not a refusal.
BLOCKING_OUTCOMES: frozenset[str] = frozenset({"no_progress", "disposition_unchanged"})


class SelectionError(StrEnum):
    """Reason codes. Every refusal names one; none is a bare exception."""

    UNKNOWN_TOOL = "unknown_tool"
    TOOL_UNAVAILABLE = "tool_unavailable"
    LOW_CONFIDENCE = "low_confidence"
    UNSAFE_SURFACE = "unsafe_tier_surface"
    REVIEW_REQUESTED = "review_requested"
    REPEAT_BLOCKED = "repeat_blocked"
    NO_ALTERNATIVE = "no_alternative_candidate"
    DECISIONS_EXHAUSTED = "decisions_exhausted"
    RUNS_EXHAUSTED = "tool_runs_exhausted"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    ARGUMENTS_MISMATCH = "arguments_mismatch"
    RESULT_INVALID = "tool_result_invalid"


class SelectionRefused(Exception):
    """A reason-coded refusal."""

    def __init__(self, reason: SelectionError, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(reason.value)


def canonical_digest(value: Any) -> str:
    """One canonical hash, so identity never depends on key order."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class ToolCard:
    """One tool, bound to its private capability. `public()` is the card.

    `tool_id` is what the model sees and what you may rename freely.
    `capability` is your internal identity, held here as a controller-side
    binding: two cards with different ids and the same capability are the same
    tool, so renaming cannot reset a budget or evade a block. It is never part
    of the published card, so nothing that leaves this module carries it.

    The published card is the whole public contract of the tool: what it
    observes, what must hold before it is worth running, what shape its input
    takes, what class of cost it incurs, and the contract version it was
    published under.
    """

    tool_id: str
    capability: str
    description: str
    observable: tuple[str, ...]
    cost_class: str = "local_read"
    #: What must already hold for this read to be worth running. Public, and
    #: stated as observable conditions rather than as advice about choosing.
    preconditions: tuple[str, ...] = ()
    input_schema: str = "no_arguments"
    contract_version: str = TOOL_CONTRACT_VERSION

    def public(self) -> dict[str, Any]:
        """Exactly what is sent to the model. The capability is not in it."""
        return {
            "tool_id": self.tool_id,
            "description": self.description,
            "observable": list(self.observable),
            "preconditions": list(self.preconditions),
            "input_schema": self.input_schema,
            "cost_class": self.cost_class,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True)
class Catalog:
    """Your tool catalog. Substitute freely; the pattern does not change."""

    cards: tuple[ToolCard, ...]

    def card(self, tool_id: str) -> ToolCard:
        for card in self.cards:
            if card.tool_id == tool_id:
                return card
        raise SelectionRefused(SelectionError.UNKNOWN_TOOL, tool_id)

    def capability_of(self, tool_id: str) -> str:
        return self.card(tool_id).capability

    def public(self, available: frozenset[str]) -> list[dict[str, Any]]:
        return [
            card.public() | {"available": card.tool_id in available}
            for card in self.cards
        ]

    def public_id_of(self, capability: str) -> str | None:
        """The public identifier bound to one private capability, if any."""
        for card in self.cards:
            if card.capability == capability:
                return card.tool_id
        return None


@dataclass(frozen=True)
class Versions:
    """Everything that must still hold for a receipt to describe reality."""

    data_version: str
    model_revision: str
    catalog_version: str
    policy_version: str
    data_schema_version: str = DATA_SCHEMA_VERSION
    tool_contract_version: str = TOOL_CONTRACT_VERSION

    @property
    def digest(self) -> str:
        return canonical_digest(self.__dict__)


@dataclass(frozen=True)
class ProblemContext:
    """The current authoritative state. Operational identity is excluded.

    `problem_fingerprint` is stable across calls, so history for the same
    problem can be found at all. `context_digest` is the complete current
    identity: change the evidence, the candidates, or a bound version and it
    changes, which is what makes an "exact" match meaningful.

    `arguments_digest` is the identity of the arguments the next read would run
    with. It is deliberately *not* part of the context digest: it is the second
    half of the execution key, so a receipt recorded for one argument set can
    never refuse a different one under the same context.
    """

    problem_id: str
    task_digest: str
    versions: Versions
    arguments_digest: str
    evidence_digests: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    used_capabilities: tuple[str, ...] = ()

    @property
    def problem_fingerprint(self) -> str:
        return canonical_digest(
            {"problem_id": self.problem_id, "task": self.task_digest}
        )

    @property
    def context_digest(self) -> str:
        return canonical_digest(
            {
                "problem_id": self.problem_id,
                "task": self.task_digest,
                "versions": self.versions.__dict__,
                "evidence": sorted(self.evidence_digests),
                "candidates": sorted(self.candidates),
                "used": sorted(self.used_capabilities),
            }
        )

    def execution_key(self, capability: str) -> tuple[str, str]:
        """The complete identity of one execution under this exact context."""
        return capability, self.arguments_digest


def opaque_execution_key(capability: str, arguments_digest: str) -> str:
    """An execution identity the model can compare but cannot resolve.

    The private capability never reaches model input, so a history entry names
    the public tool plus this digest. Two entries for the same internal
    execution are recognisably the same; none of them reveals the capability.
    """
    return canonical_digest({"execution": [capability, arguments_digest]})


@dataclass(frozen=True)
class CallTrace:
    """Which decision, which model call, and which read a receipt describes.

    These identities are what lets an adopter reconstruct the call afterwards:
    which model call selected the read, and which executed read the outcome
    came from. They are part of the receipt's immutable binding set and never
    part of the context it matches on, so a retry with new identities is not a
    new problem.

    `diagnostic_call_id` is absent exactly when no read executed.
    """

    decision_id: str
    model_call_id: str
    diagnostic_call_id: str | None = None


def derive_trace(
    context: ProblemContext, card: ToolCard, result: ToolResult
) -> CallTrace:
    """A deterministic trace for a caller that keeps no call identities.

    Real identities are better: pass your own. These are derived from the
    bound context, tool and arguments, so the same facts recorded twice stay
    one row rather than becoming two.
    """
    bound = {
        "context": context.context_digest,
        "tool": card.tool_id,
        "arguments": result.arguments_digest,
    }
    return CallTrace(
        decision_id=canonical_digest({"decision": bound}),
        model_call_id=canonical_digest({"model_call": bound}),
        diagnostic_call_id=canonical_digest({"read": bound}) if result.ran else None,
    )


@dataclass(frozen=True)
class Receipt:
    """Verifier-owned evidence about one past decision and one tool run.

    Identified by content: the complete immutable binding set of the call it
    describes. The observation instant is audit information and is excluded
    from that identity, so the same verified fact recorded twice is one row,
    and there is no expiry anywhere. This is not a cache.
    """

    capability: str
    public_tool_id: str
    arguments_digest: str
    outcome: OutcomeClass
    reason: str
    problem_fingerprint: str
    context_digest: str
    versions_digest: str
    evidence_changed: bool
    #: Which decision, model call and executed read this receipt describes.
    decision_id: str
    model_call_id: str
    diagnostic_call_id: str | None
    #: The identity of the input the selection was made from, and of the
    #: authoritative state the executed read left behind.
    input_digest: str
    post_call_state_digest: str
    #: The admissible evidence before and after the read, by identity.
    evidence_before_digests: tuple[str, ...]
    evidence_after_digests: tuple[str, ...]
    #: Every version the receipt is bound to, carried in full beside its digest.
    versions: Versions
    #: The UTC instant the caller observed, or `UNRECORDED_OBSERVATION`.
    observed_at: str = UNRECORDED_OBSERVATION
    disposition_moved: bool = False

    def __post_init__(self) -> None:
        before, after = (
            set(self.evidence_before_digests),
            set(self.evidence_after_digests),
        )
        if not before <= after:
            raise SelectionRefused(
                SelectionError.RESULT_INVALID,
                "admissible evidence cannot be withdrawn by a read",
            )
        if before != after and not self.evidence_changed:
            raise SelectionRefused(
                SelectionError.RESULT_INVALID,
                "new evidence identities require an admitted fact",
            )
        if self.diagnostic_call_id is None and self.outcome != "provider_error":
            raise SelectionRefused(
                SelectionError.RESULT_INVALID,
                "a verified outcome names the read that produced it",
            )
        if self.observed_at != UNRECORDED_OBSERVATION and not (
            self.observed_at.endswith("Z") or self.observed_at.endswith("+00:00")
        ):
            raise SelectionRefused(
                SelectionError.RESULT_INVALID,
                "an observation instant is recorded in UTC or not at all",
            )

    @property
    def receipt_id(self) -> str:
        """The identity of the complete immutable binding set."""
        return canonical_digest(
            {
                "capability": self.capability,
                "public_tool_id": self.public_tool_id,
                "arguments": self.arguments_digest,
                "outcome": self.outcome,
                "reason": self.reason,
                "problem": self.problem_fingerprint,
                "context": self.context_digest,
                "versions": self.versions_digest,
                "changed": self.evidence_changed,
                "moved": self.disposition_moved,
                "decision": self.decision_id,
                "model_call": self.model_call_id,
                "read": self.diagnostic_call_id,
                "input": self.input_digest,
                "post_call_state": self.post_call_state_digest,
                "evidence_before": sorted(self.evidence_before_digests),
                "evidence_after": sorted(self.evidence_after_digests),
            }
        )

    @property
    def execution_key(self) -> tuple[str, str]:
        return self.capability, self.arguments_digest

    @property
    def opaque_key(self) -> str:
        return opaque_execution_key(self.capability, self.arguments_digest)

    @property
    def blocking(self) -> bool:
        """Whether this receipt is eligible to refuse a repetition.

        Two classes qualify, and each proves its own conjunction. Both are
        re-read from the recorded facts rather than taken on trust from the
        outcome label:

        * `no_progress`: the read admitted nothing and moved no gate.
        * `disposition_unchanged`: the read admitted a real, admissible fact
          and still moved no gate. This is the blind spot a model repeats, and
          it is the whole reason the second dimension is recorded.

        A read that moved a gate is never eligible, whatever else it did.
        """
        if self.disposition_moved:
            return False
        if self.outcome == "no_progress":
            return not self.evidence_changed
        if self.outcome == "disposition_unchanged":
            return self.evidence_changed
        return False

    def match(self, context: ProblemContext) -> MatchClass:
        """Exact needs the same context. Compatible needs the same versions."""
        if self.problem_fingerprint != context.problem_fingerprint:
            return "no_match"
        if self.context_digest == context.context_digest:
            return "exact"
        if self.versions_digest == context.versions.digest:
            return "compatible"
        return "no_match"


@dataclass
class ReceiptStore:
    """An in-memory store. Swap in your own; keep the semantics.

    Writes are idempotent by receipt identity and nothing is ever overwritten.
    Freezing is how you stop admission before an evaluation.
    """

    rows: dict[str, Receipt] = field(default_factory=dict)
    frozen: bool = False

    def put(self, receipt: Receipt) -> bool:
        if self.frozen:
            return False
        if receipt.receipt_id in self.rows:
            return False
        self.rows[receipt.receipt_id] = receipt
        return True

    def matching(self, context: ProblemContext) -> list[Receipt]:
        """Bounded, deterministic, exact matches first."""
        found = [
            receipt
            for receipt in self.rows.values()
            if receipt.match(context) != "no_match"
        ]
        found.sort(
            key=lambda receipt: (receipt.match(context) != "exact", receipt.receipt_id)
        )
        return found[:MAX_RECEIPT_REFERENCES]

    def blocked_keys(self, context: ProblemContext) -> set[tuple[str, str]]:
        """The capability and arguments an exact no-progress receipt refuses."""
        return {
            receipt.execution_key
            for receipt in self.rows.values()
            if receipt.blocking and receipt.match(context) == "exact"
        }


@dataclass(frozen=True)
class Choice:
    """What the model returned. `confidence` may be None; that is not zero."""

    tool_id: str | None
    confidence: float | None
    requested_review: bool = False


class Selector(Protocol):
    """Your model call. One attempt, no retry inside it."""

    def __call__(self, payload: dict[str, Any]) -> Choice: ...


@dataclass(frozen=True)
class StateProjection:
    """The authoritative state your code observed *after* one read.

    Your executor returns this so the second decision is built from the state
    the first read left behind. Only the evidence identities are required: the
    candidates, the used capabilities and the bound arguments default to the
    pre-call state, because a read that changed none of them changed none of
    them.
    """

    evidence_digests: tuple[str, ...]
    candidates: tuple[str, ...] | None = None
    used_capabilities: tuple[str, ...] | None = None
    arguments_digest: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """What your tool returned, as facts. No outcome label, no prose.

    `evidence_changed` is whether the read admitted a fact the problem did not
    already hold. `disposition_moved` is whether that fact moved one of the
    gates your assessment actually uses. They are two different questions, and
    a gate can never move without an admitted fact.

    `state_after` is the refreshed authoritative state, and `admitted_digests`
    names the evidence identities this read admitted. Either is enough to carry
    the post-call state into the second decision; with neither, the refresh
    still records the capability that just ran, so the second decision is never
    handed the pre-call state unchanged.
    """

    arguments_digest: str
    ran: bool
    evidence_changed: bool
    admissible: bool
    reason: str
    disposition_moved: bool = False
    admitted_digests: tuple[str, ...] = ()
    state_after: StateProjection | None = None

    def __post_init__(self) -> None:
        if self.disposition_moved and not self.evidence_changed:
            raise SelectionRefused(
                SelectionError.RESULT_INVALID,
                "a disposition cannot move without an admitted fact",
            )
        if self.evidence_changed and not (self.ran and self.admissible):
            raise SelectionRefused(
                SelectionError.RESULT_INVALID,
                "only an admissible read that ran can admit a fact",
            )
        if self.admitted_digests and not self.evidence_changed:
            raise SelectionRefused(
                SelectionError.RESULT_INVALID,
                "a read that admitted nothing names no new evidence",
            )


@dataclass(frozen=True)
class ExecutionRequest:
    """One read your executor is asked to perform. Arguments are bound here."""

    tool_id: str
    capability: str
    arguments_digest: str
    pass_index: int


class ToolExecutor(Protocol):
    """Your tool run. It returns observed facts and labels nothing."""

    def __call__(self, request: ExecutionRequest) -> ToolResult: ...


@dataclass(frozen=True)
class Refusal:
    """One refused or unproductive execution, carried into the next decision.

    `exact_repeat` is a refusal before anything ran: the execution is
    precluded from the offer before the first decision. `failed_execution` is a
    read that ran and left the problem where it was, and opens the one second
    decision. Only the first of them was ever prohibited.
    """

    tool_id: str
    capability: str
    arguments_digest: str
    outcome: OutcomeClass | None
    reason: str
    kind: RefusalKind
    receipt_id: str | None = None

    @property
    def opaque_key(self) -> str:
        return opaque_execution_key(self.capability, self.arguments_digest)


def classify_outcome(result: ToolResult) -> OutcomeClass:
    """Deterministic. The model never supplies this.

    Both recorded dimensions are used, because they answer different
    questions. A read that admitted a fact and moved a gate is useful. A read
    that admitted a fact and moved nothing is real evidence that left the
    decision exactly where it was, which is its own class. A read that
    admitted nothing is the strict no-progress class.
    """
    if not result.ran:
        return "provider_error"
    if not result.admissible:
        return "invalid_evidence"
    if not result.evidence_changed:
        return "no_progress"
    return "useful_evidence" if result.disposition_moved else "disposition_unchanged"


def refreshed_context(
    context: ProblemContext, capability: str, result: ToolResult
) -> ProblemContext:
    """The authoritative state after one executed read.

    Your executor's own projection wins where it supplies one. Without one, the
    refresh is still not the pre-call state: the capability that just ran is
    recorded as used, and anything the read admitted joins the evidence. No
    fact is invented here, and nothing is carried forward that the read
    contradicted.
    """
    projection = result.state_after
    used = tuple(sorted(set(context.used_capabilities) | {capability}))
    evidence = tuple(
        sorted(set(context.evidence_digests) | set(result.admitted_digests))
    )
    candidates = context.candidates
    arguments = context.arguments_digest
    if projection is not None:
        evidence = tuple(sorted(set(projection.evidence_digests)))
        if projection.used_capabilities is not None:
            used = tuple(sorted(set(projection.used_capabilities)))
        if projection.candidates is not None:
            candidates = tuple(sorted(set(projection.candidates)))
        if projection.arguments_digest is not None:
            arguments = projection.arguments_digest
    return ProblemContext(
        problem_id=context.problem_id,
        task_digest=context.task_digest,
        versions=context.versions,
        arguments_digest=arguments,
        evidence_digests=evidence,
        candidates=candidates,
        used_capabilities=used,
    )


def public_executions(context: ProblemContext, catalog: Catalog) -> list[str]:
    """The already-run executions as public identifiers only.

    A capability the catalog publishes travels as its public `tool_id`. One it
    does not publish travels as an opaque execution key, which the model can
    compare and cannot resolve. The private capability string never leaves.
    """
    found: list[str] = []
    for capability in context.used_capabilities:
        public = catalog.public_id_of(capability)
        found.append(
            public
            if public is not None
            else opaque_execution_key(capability, context.arguments_digest)
        )
    return sorted(set(found))


def build_payload(
    context: ProblemContext,
    catalog: Catalog,
    available: frozenset[str],
    receipts: list[Receipt],
    *,
    refusal: Refusal | None = None,
    precluded: tuple[Refusal, ...] = (),
) -> dict[str, Any]:
    """Current state plus bounded history. Refuses rather than truncating.

    The history is labelled `history`, each entry says what happened, and
    nothing in it tells the model what to pick. That labelling is the whole
    point: the model must decide fresh. Every identifier in here is public: the
    private capability travels only as a public `tool_id` or an opaque
    execution key, including under `already_used`.

    `precluded` names exact proven dead ends already removed from `available`.
    The key appears only when something was precluded, so an unblocked payload
    is exactly what it would be without this rule.
    """
    payload: dict[str, Any] = {
        "current_state": {
            "problem_id": context.problem_id,
            "task": context.task_digest,
            "arguments": context.arguments_digest,
            "versions": context.versions.__dict__,
            "evidence_count": len(context.evidence_digests),
            "already_used": public_executions(context, catalog),
        },
        "tools": catalog.public(available),
        "history": [
            {
                "match": receipt.match(context),
                "tool_id": receipt.public_tool_id,
                "execution_key": receipt.opaque_key,
                "what_happened": receipt.outcome,
                "reason": receipt.reason,
                "note": "evidence about a past run; not a recommendation",
            }
            for receipt in receipts
        ],
        "rules": [
            "Choose one available tool, or request review.",
            "Past runs are evidence only. Decide from the current state.",
        ],
    }
    if refusal is not None:
        payload["blocked_repetition"] = {
            "tool_id": refusal.tool_id,
            "execution_key": refusal.opaque_key,
            "what_happened": refusal.outcome,
            "why": (
                "an exact no-progress receipt refuses this repetition"
                if refusal.kind == "exact_repeat"
                else "this read ran and left the problem where it was"
            ),
        }
    if precluded:
        payload["precluded"] = [
            {
                "tool_id": item.tool_id,
                "execution_key": item.opaque_key,
                "what_happened": item.outcome,
                "why": "an exact receipt proves this identical read moved no gate "
                "under this same context, so it is not offered",
            }
            for item in precluded
        ]
    if len(json.dumps(payload, sort_keys=True)) > SELECTOR_INPUT_MAX_CHARS:
        raise SelectionRefused(SelectionError.PAYLOAD_TOO_LARGE)
    return payload


@dataclass(frozen=True)
class SelectionOutcome:
    """The result of one bounded selection round. Nothing has executed yet."""

    tool_id: str | None
    capability: str | None
    decisions: int
    refused: SelectionError | None
    history_classes: tuple[MatchClass, ...]
    refusals: tuple[Refusal, ...] = ()
    #: The identity of each decision input, in the order the calls were made.
    #: A receipt binds the one the read it describes was selected from.
    input_digests: tuple[str, ...] = ()
    #: Exact proven dead ends removed from the offer before the first decision.
    precluded: tuple[Refusal, ...] = ()

    @property
    def blocked_tool_id(self) -> str | None:
        refused = self.precluded or self.refusals
        return refused[0].tool_id if refused else None

    @property
    def input_digest(self) -> str:
        """The input the final decision of this round was made from."""
        return self.input_digests[-1] if self.input_digests else ""


def _decide(
    context: ProblemContext,
    catalog: Catalog,
    available: frozenset[str],
    receipts: list[Receipt],
    selector: Selector,
    *,
    confidence_floor: float,
    tier: AcceptanceTier,
    refusal: Refusal | None,
    precluded: tuple[Refusal, ...] = (),
) -> tuple[Choice | None, str | None, SelectionError | None, str]:
    """One model call, validated before anything may execute. Fails closed.

    The digest of the exact payload the model saw is returned with the choice,
    so a receipt can bind the input the decision was made from rather than a
    reconstruction of it.
    """
    payload = build_payload(
        context, catalog, available, receipts, refusal=refusal, precluded=precluded
    )
    digest = canonical_digest(payload)
    choice = selector(payload)
    rejection = _unusable(choice, catalog, available, confidence_floor, tier)
    if rejection is not None:
        return None, None, rejection, digest
    return choice, catalog.capability_of(str(choice.tool_id)), None, digest


def select_tool(
    context: ProblemContext,
    catalog: Catalog,
    available: frozenset[str],
    store: ReceiptStore,
    selector: Selector,
    *,
    confidence_floor: float = 0.8,
    tier: AcceptanceTier = "strict_confidence",
    max_decisions: int = MAX_DECISIONS,
) -> SelectionOutcome:
    """Preclude exact dead ends, then one fresh decision over what remains.

    This is the selection round only: nothing here executes a tool. Every
    offered tool whose identical execution an exact blocking receipt refuses is
    removed before the model is called, with that receipt as its proof; when
    nothing remains the model is never called. A refusal is returned rather
    than raised so a caller can record it; a programming error (an unknown
    tool) still raises.

    `tier` is the acceptance tier the answer is judged under. It is part of
    your frozen policy, so declare it inside `Versions.policy_version`: that
    way it enters the decision context every receipt is bound to, and a receipt
    written under one tier cannot match a decision made under the other.
    """
    budget = min(max_decisions, MAX_DECISIONS)
    receipts = store.matching(context)
    classes = tuple(receipt.match(context) for receipt in receipts)
    precluded = preclusions(context, catalog, available, store)
    remaining = frozenset(
        tool_id
        for tool_id in available
        if tool_id not in {item.tool_id for item in precluded}
    )
    decisions = 0

    if precluded and not remaining:
        # Every offered tool is a proven dead end, so the model is not asked.
        return SelectionOutcome(
            None,
            None,
            decisions,
            SelectionError.NO_ALTERNATIVE,
            classes,
            precluded=precluded,
        )
    if decisions >= budget:
        return SelectionOutcome(
            None,
            None,
            decisions,
            SelectionError.DECISIONS_EXHAUSTED,
            classes,
            precluded=precluded,
        )
    choice, capability, rejection, digest = _decide(
        context,
        catalog,
        remaining,
        receipts,
        selector,
        confidence_floor=confidence_floor,
        tier=tier,
        refusal=None,
        precluded=precluded,
    )
    decisions += 1
    inputs = (digest,)
    if rejection is not None or choice is None or capability is None:
        return SelectionOutcome(
            None,
            None,
            decisions,
            rejection,
            classes,
            input_digests=inputs,
            precluded=precluded,
        )
    return SelectionOutcome(
        str(choice.tool_id),
        capability,
        decisions,
        None,
        classes,
        input_digests=inputs,
        precluded=precluded,
    )


def preclusions(
    context: ProblemContext,
    catalog: Catalog,
    available: frozenset[str],
    store: ReceiptStore,
) -> tuple[Refusal, ...]:
    """The offered executions an exact blocking receipt refuses, in card order.

    Each names the public tool and the matching receipt's own class, so a
    reader can tell which of the two disposition-negative proofs refused it.
    The capability stays here. Nothing about the remaining tools changes.
    """
    found: list[Refusal] = []
    for card in catalog.cards:
        if card.tool_id not in available:
            continue
        key = context.execution_key(card.capability)
        proof = next(
            (
                receipt
                for receipt in sorted(
                    store.rows.values(), key=lambda item: item.receipt_id
                )
                if receipt.blocking
                and receipt.match(context) == "exact"
                and receipt.execution_key == key
            ),
            None,
        )
        if proof is None:
            continue
        found.append(
            Refusal(
                tool_id=card.tool_id,
                capability=card.capability,
                arguments_digest=context.arguments_digest,
                outcome=proof.outcome,
                reason=SelectionError.REPEAT_BLOCKED.value,
                kind="exact_repeat",
                receipt_id=proof.receipt_id,
            )
        )
    return tuple(found)


def _second(
    context: ProblemContext,
    catalog: Catalog,
    available: frozenset[str],
    receipts: list[Receipt],
    selector: Selector,
    store: ReceiptStore,
    *,
    refusal: Refusal,
    decisions: int,
    budget: int,
    classes: tuple[MatchClass, ...],
    confidence_floor: float,
    tier: AcceptanceTier = "strict_confidence",
    inputs: tuple[str, ...] = (),
    tool_run_left: bool = True,
    precluded: tuple[Refusal, ...] = (),
) -> SelectionOutcome:
    """Exactly one second decision over the remaining candidates.

    `context` is the state this decision is actually made from. After a read
    has executed that is the refreshed state, not the state the first decision
    saw, and `receipts` are the references that match the refreshed state.
    `precluded` is what still stands precluded under that state.
    """
    removed = {refusal.capability} | {item.capability for item in precluded}
    remaining = frozenset(
        tool_id
        for tool_id in available
        if catalog.capability_of(tool_id) not in removed
    )
    if not remaining:
        return SelectionOutcome(
            None,
            None,
            decisions,
            SelectionError.NO_ALTERNATIVE,
            classes,
            (refusal,),
            input_digests=inputs,
        )
    if not tool_run_left:
        # A choice no read is left to run could not be acted on, so the model
        # is never asked for it.
        return SelectionOutcome(
            None,
            None,
            decisions,
            SelectionError.RUNS_EXHAUSTED,
            classes,
            (refusal,),
            input_digests=inputs,
        )
    if decisions >= budget:
        return SelectionOutcome(
            None,
            None,
            decisions,
            SelectionError.DECISIONS_EXHAUSTED,
            classes,
            (refusal,),
            input_digests=inputs,
        )
    choice, capability, rejection, digest = _decide(
        context,
        catalog,
        remaining,
        receipts,
        selector,
        confidence_floor=confidence_floor,
        tier=tier,
        refusal=refusal,
        precluded=precluded,
    )
    decisions += 1
    inputs += (digest,)
    if rejection is not None or choice is None or capability is None:
        return SelectionOutcome(
            None, None, decisions, rejection, classes, (refusal,), input_digests=inputs
        )
    return SelectionOutcome(
        str(choice.tool_id),
        capability,
        decisions,
        None,
        classes,
        (refusal,),
        input_digests=inputs,
    )


def read_only_surface(catalog: Catalog, available: frozenset[str]) -> bool:
    """Whether every executable option offered is an argument-free read.

    This is the one precondition of the relaxed tier, and it is a property of
    the offered surface rather than of the answer. An option with no published
    card, or one whose published surface leaves either declared set, makes the
    surface unsafe for that tier at any confidence.
    """
    if not available:
        return False
    for tool_id in sorted(available):
        card = next((item for item in catalog.cards if item.tool_id == tool_id), None)
        if card is None:
            return False
        if card.input_schema not in READ_ONLY_INPUT_SCHEMAS:
            return False
        if card.cost_class not in READ_ONLY_COST_CLASSES:
            return False
    return True


def _unusable(
    choice: Choice,
    catalog: Catalog,
    available: frozenset[str],
    floor: float,
    tier: AcceptanceTier = "strict_confidence",
) -> SelectionError | None:
    """Validate the choice before it can execute. Fails closed.

    Structural validity is a hard precondition in every tier and no tier
    weakens any clause of it: the answer must name one currently offered
    executable option, must not be the review option, and must carry a
    confidence at all. The tier decides only what happens to an answer that
    already passed all of that. `strict_confidence` additionally requires the
    configured floor. `read_only_structural` requires instead that the whole
    offered surface is argument-free and read-only, and then accepts whatever
    confidence was reported; over any other surface it refuses outright.
    """
    if choice.requested_review:
        return SelectionError.REVIEW_REQUESTED
    catalog.card(str(choice.tool_id))
    if choice.tool_id not in available:
        return SelectionError.TOOL_UNAVAILABLE
    if choice.confidence is None:
        return SelectionError.LOW_CONFIDENCE
    if tier == "read_only_structural":
        if read_only_surface(catalog, available):
            return None
        return SelectionError.UNSAFE_SURFACE
    if choice.confidence < floor:
        return SelectionError.LOW_CONFIDENCE
    return None


def make_receipt(
    context: ProblemContext,
    card: ToolCard,
    result: ToolResult,
    *,
    trace: CallTrace | None = None,
    input_digest: str | None = None,
    observed_at: str = UNRECORDED_OBSERVATION,
) -> Receipt:
    """The verifier writes this. The model has no part in it.

    The result must describe the arguments this context is bound to, so a
    receipt can never be filed against an execution that did not happen.

    Everything the receipt binds comes from facts this function was handed:
    the pre-call context, the published card, the observed result, the call
    identities, the identity of the decision input, and the caller's own UTC
    observation instant. Absent a trace or an input digest, both are derived
    deterministically from the same bound facts. Absent an observation instant,
    the receipt records that none was recorded rather than inventing one.
    """
    if result.arguments_digest != context.arguments_digest:
        raise SelectionRefused(
            SelectionError.ARGUMENTS_MISMATCH,
            "the observed arguments are not the ones this context is bound to",
        )
    identities = trace or derive_trace(context, card, result)
    after = refreshed_context(context, card.capability, result)
    return Receipt(
        capability=card.capability,
        public_tool_id=card.tool_id,
        arguments_digest=result.arguments_digest,
        outcome=classify_outcome(result),
        reason=result.reason,
        problem_fingerprint=context.problem_fingerprint,
        context_digest=context.context_digest,
        versions_digest=context.versions.digest,
        evidence_changed=result.evidence_changed,
        decision_id=identities.decision_id,
        model_call_id=identities.model_call_id,
        diagnostic_call_id=identities.diagnostic_call_id,
        input_digest=(
            input_digest
            if input_digest is not None
            else canonical_digest(
                {
                    "state": context.context_digest,
                    "arguments": context.arguments_digest,
                }
            )
        ),
        post_call_state_digest=after.context_digest,
        evidence_before_digests=tuple(sorted(set(context.evidence_digests))),
        evidence_after_digests=tuple(sorted(set(after.evidence_digests))),
        versions=context.versions,
        observed_at=observed_at,
        disposition_moved=result.disposition_moved,
    )


@dataclass(frozen=True)
class Resolution:
    """One bounded problem, resolved or explicitly not.

    `direct` means the first read moved a gate. `rechosen` means exactly one
    second decision ran, either because the first execution was refused as an
    exact repetition or because it ran and achieved nothing. `unresolved` names
    the reason it stopped. There is never a third decision or a third read.
    """

    path: ResolutionPath
    tool_id: str | None
    capability: str | None
    decisions: int
    tool_runs: int
    receipt_ids: tuple[str, ...]
    outcomes: tuple[OutcomeClass, ...]
    refusals: tuple[Refusal, ...]
    refused: SelectionError | None
    history_classes: tuple[MatchClass, ...]
    #: Exact proven dead ends removed from the offer before the first decision.
    precluded: tuple[Refusal, ...] = ()


def resolve_problem(
    context: ProblemContext,
    catalog: Catalog,
    available: frozenset[str],
    store: ReceiptStore,
    selector: Selector,
    executor: ToolExecutor,
    *,
    confidence_floor: float = 0.8,
    tier: AcceptanceTier = "strict_confidence",
    max_decisions: int = MAX_DECISIONS,
    max_tool_runs: int = MAX_TOOL_RUNS,
    observed_at: str = UNRECORDED_OBSERVATION,
) -> Resolution:
    """The whole bounded pattern: preclude, decide, execute, verify, re-decide.

    Both ledgers are checked before the call they pay for, and neither can be
    raised above the declared bound by a caller. A read that moves a supported
    disposition finishes the problem; every other verified read opens the one
    second decision and travels into it as labelled same-call history.

    The state advances with the reads. After each executed read the
    authoritative state is refreshed from the executor's own projection, and
    the second decision is made from that refreshed state: its references are
    the ones that match it, and the same-call receipt reaches it labelled
    against it rather than as an exact match on a state that no longer holds.
    A preclusion stands for the whole call: it was matched against the call's
    original state, so a proven dead end is never re-offered to the second
    decision.
    """
    decisions_budget = min(max_decisions, MAX_DECISIONS)
    runs_budget = min(max_tool_runs, MAX_TOOL_RUNS)
    selection = select_tool(
        context,
        catalog,
        available,
        store,
        selector,
        confidence_floor=confidence_floor,
        tier=tier,
        max_decisions=decisions_budget,
    )
    receipts: tuple[str, ...] = ()
    outcomes: tuple[OutcomeClass, ...] = ()
    runs = 0
    refusals = selection.refusals
    if selection.refused is not None or selection.tool_id is None:
        return Resolution(
            path="unresolved",
            tool_id=None,
            capability=None,
            decisions=selection.decisions,
            tool_runs=runs,
            receipt_ids=receipts,
            outcomes=outcomes,
            refusals=refusals,
            refused=selection.refused,
            history_classes=selection.history_classes,
            precluded=selection.precluded,
        )

    decisions = selection.decisions
    selected = selection.tool_id
    capability = str(selection.capability)
    input_digest = selection.input_digest
    while True:
        if runs >= runs_budget:
            return Resolution(
                path="unresolved",
                tool_id=None,
                capability=None,
                decisions=decisions,
                tool_runs=runs,
                receipt_ids=receipts,
                outcomes=outcomes,
                refusals=refusals,
                refused=SelectionError.RUNS_EXHAUSTED,
                history_classes=selection.history_classes,
                precluded=selection.precluded,
            )
        pass_index = 1 if runs == 0 and not refusals else 2
        result = executor(
            ExecutionRequest(
                tool_id=selected,
                capability=capability,
                arguments_digest=context.arguments_digest,
                pass_index=pass_index,
            )
        )
        runs += 1
        card = catalog.card(selected)
        receipt = make_receipt(
            context, card, result, input_digest=input_digest, observed_at=observed_at
        )
        store.put(receipt)
        receipts += (receipt.receipt_id,)
        outcomes += (receipt.outcome,)
        # The read happened, so the authoritative state has moved on. Every
        # later step in this call uses the refreshed state.
        context = refreshed_context(context, capability, result)
        if result.disposition_moved:
            return Resolution(
                path="rechosen" if refusals else "direct",
                tool_id=selected,
                capability=capability,
                decisions=decisions,
                tool_runs=runs,
                receipt_ids=receipts,
                outcomes=outcomes,
                refusals=refusals,
                refused=None,
                history_classes=selection.history_classes,
                precluded=selection.precluded,
            )
        # The read ran and left the problem where it was. It opens the one
        # second decision as labelled same-call history. Whether it may refuse
        # a future repetition is the receipt's own question, decided from the
        # two facts it recorded and never from this refusal.
        refusal = Refusal(
            tool_id=selected,
            capability=capability,
            arguments_digest=result.arguments_digest,
            outcome=receipt.outcome,
            reason=result.reason,
            kind="failed_execution",
            receipt_id=receipt.receipt_id,
        )
        if decisions >= decisions_budget:
            # The second choice is final. There is no third selection.
            return Resolution(
                path="unresolved",
                tool_id=None,
                capability=None,
                decisions=decisions,
                tool_runs=runs,
                receipt_ids=receipts,
                outcomes=outcomes,
                refusals=refusals,
                refused=SelectionError.DECISIONS_EXHAUSTED,
                history_classes=selection.history_classes,
                precluded=selection.precluded,
            )
        refusals += (refusal,)
        round_two = _second(
            context,
            catalog,
            available,
            store.matching(context),
            selector,
            store,
            refusal=refusal,
            decisions=decisions,
            budget=decisions_budget,
            classes=selection.history_classes,
            confidence_floor=confidence_floor,
            tier=tier,
            tool_run_left=runs < runs_budget,
            precluded=selection.precluded,
        )
        decisions = round_two.decisions
        input_digest = round_two.input_digest or input_digest
        if round_two.refused is not None or round_two.tool_id is None:
            return Resolution(
                path="unresolved",
                tool_id=None,
                capability=None,
                decisions=decisions,
                tool_runs=runs,
                receipt_ids=receipts,
                outcomes=outcomes,
                refusals=refusals,
                refused=round_two.refused,
                history_classes=selection.history_classes,
                precluded=selection.precluded,
            )
        selected = round_two.tool_id
        capability = str(round_two.capability)


__all__ = [
    "ACCEPTANCE_TIERS",
    "BLOCKING_OUTCOMES",
    "EVIDENCE_OUTCOMES",
    "DATA_SCHEMA_VERSION",
    "MAX_DECISIONS",
    "MAX_RECEIPT_REFERENCES",
    "MAX_TOOL_RUNS",
    "READ_ONLY_COST_CLASSES",
    "READ_ONLY_INPUT_SCHEMAS",
    "SELECTOR_INPUT_MAX_CHARS",
    "TOOL_CONTRACT_VERSION",
    "UNRECORDED_OBSERVATION",
    "AcceptanceTier",
    "CallTrace",
    "Catalog",
    "Choice",
    "ExecutionRequest",
    "MatchClass",
    "OutcomeClass",
    "ProblemContext",
    "Receipt",
    "ReceiptStore",
    "Refusal",
    "RefusalKind",
    "Resolution",
    "ResolutionPath",
    "SelectionError",
    "SelectionOutcome",
    "SelectionRefused",
    "Selector",
    "StateProjection",
    "ToolCard",
    "ToolExecutor",
    "ToolResult",
    "Versions",
    "build_payload",
    "canonical_digest",
    "classify_outcome",
    "derive_trace",
    "make_receipt",
    "opaque_execution_key",
    "public_executions",
    "read_only_surface",
    "refreshed_context",
    "resolve_problem",
    "select_tool",
]
