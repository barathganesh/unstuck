from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from unstuck.domain import (
    Contract,
    Counters,
    Digest,
    Identifier,
    PipelineFacts,
    Probability,
    Provider,
    QuerySpec,
    Reason,
    RuntimeFault,
    SchemaFacts,
    ScopeFacts,
    TaskSpec,
)
from unstuck.events import Measurement, PersistenceBoundary, Redactor

HistoryStatus = Literal["not_requested", "unavailable", "empty", "matched"]
MatchClass = Literal["exact", "compatible"]
OutcomeClass = Literal[
    "useful_evidence",
    "disposition_unchanged",
    "no_progress",
    "provider_error",
    "invalid_evidence",
    "superseded",
]


class PublicProbe(Contract):
    id: Identifier
    description: Annotated[str, Field(min_length=1, max_length=512)]
    #: The published condition under which observing this probe's facts
    #: distinguishes something. Condition language only: it states a situation,
    #: never another tool, a case class, a category, an expected outcome, or
    #: any evaluator fact. It is part of the public surface, so a renamed
    #: catalog publishes renamed conditions.
    applies_when: Annotated[str, Field(min_length=1, max_length=256)]
    input_schema: Literal["no_arguments"] = "no_arguments"
    observable_facts: Annotated[
        tuple[Identifier, ...], Field(min_length=1, max_length=8)
    ]
    available: bool = True
    cost_class: Literal["local_read"] = "local_read"


@dataclass(frozen=True)
class Probe:
    public: PublicProbe
    capability: Literal["inspect_pipeline", "inspect_schema_keys", "inspect_scope"]


def default_catalog() -> tuple[Probe, ...]:
    return (
        Probe(
            PublicProbe(
                id="inspect_pipeline",
                description="Observe requested partition readiness and watermark",
                applies_when=(
                    "it is still open whether the requested partition has "
                    "finished arriving at all, so the rows may simply not be "
                    "present yet"
                ),
                observable_facts=("job_state", "scope_complete"),
            ),
            "inspect_pipeline",
        ),
        Probe(
            PublicProbe(
                id="inspect_schema_keys",
                description="Observe declared join encodings, coverage, and key uniqueness",
                applies_when=(
                    "the rows may already be present and still not line up, "
                    "because it is still open whether the two sides of the "
                    "join declare the same key encoding"
                ),
                observable_facts=(
                    "key_encoding",
                    "metadata_complete",
                    "normalized_dimension_keys_unique",
                ),
            ),
            "inspect_schema_keys",
        ),
        Probe(
            PublicProbe(
                id="inspect_scope",
                description="Observe whether metadata covers the immutable requested scope",
                applies_when=(
                    "it is still open whether the request itself asks for a "
                    "range the available metadata covers, so the request "
                    "rather than the data may be the mismatch"
                ),
                observable_facts=("query_matches_task_scope", "scope_covered"),
            ),
            "inspect_scope",
        ),
    )


class PublicEvidence(Contract):
    evidence_id: UUID
    kind: Identifier
    tool_id: Identifier
    dataset_version: Identifier
    arguments_hash: Digest
    observed_at: str
    facts: PipelineFacts | SchemaFacts | ScopeFacts | dict[str, JsonValue]


class ReceiptReference(Contract):
    """Bounded redacted reference to one verifier-owned Decision Receipt.

    The reference is labeled historical evidence for the selector. It never
    selects a tool, replays a choice, or supplies a factual outcome. Matching,
    admission, and blocking authority stay with the controller and verifier.
    """

    receipt_id: Digest
    match_class: MatchClass
    outcome_class: OutcomeClass
    public_tool_id: Identifier
    arguments_hash: Digest
    reason_code: Reason


class DecisionHistory(Contract):
    """Explicit history status plus the bounded references handed to a selector.

    Core runtime neither retrieves nor matches receipts. It carries a supplied
    history through the typed seam, records the lookup, and projects the status
    so an absent or unavailable history can never look like a cached decision.
    """

    status: HistoryStatus = "not_requested"
    references: Annotated[tuple[ReceiptReference, ...], Field(max_length=4)] = ()
    truncated: bool = False

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.status == "matched") != bool(self.references):
            raise ValueError("history status does not match reference availability")
        if self.truncated and self.status != "matched":
            raise ValueError("truncation requires matched history")
        identifiers = tuple(item.receipt_id for item in self.references)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate receipt identities")
        return self

    @property
    def receipt_ids(self) -> tuple[str, ...]:
        return tuple(item.receipt_id for item in self.references)


class DecisionState(Contract):
    task: TaskSpec
    query: QuerySpec
    task_hash: Digest
    query_hash: Digest
    dataset_version: Identifier
    candidates: Annotated[tuple[PublicProbe, ...], Field(min_length=1, max_length=3)]
    budgets: Counters
    remaining_ms: Annotated[int, Field(ge=0, le=120000)]
    bounded_evidence: Annotated[tuple[PublicEvidence, ...], Field(max_length=4)] = ()
    history: DecisionHistory = DecisionHistory()
    question: Literal[
        "Which available diagnostic read would best distinguish the remaining explanations for this empty result, considering both the current scenario and the attached verified Decision Receipts?"
    ] = "Which available diagnostic read would best distinguish the remaining explanations for this empty result, considering both the current scenario and the attached verified Decision Receipts?"
    review_option: Literal["request_review"] = "request_review"
    truncated: bool = False

    @model_validator(mode="after")
    def bounded(self) -> Self:
        identifiers = tuple(probe.id for probe in self.candidates)
        if len(set(identifiers)) != len(identifiers) or "request_review" in identifiers:
            raise ValueError("invalid candidate identities")
        if (
            self.task_hash != self.task.task_hash
            or self.query.task_hash != self.task_hash
            or self.query_hash != self.query.query_hash
            or self.dataset_version != self.query.dataset_version
        ):
            raise ValueError("selection identity mismatch")
        try:
            PersistenceBoundary(Redactor()).validate(self)
        except RuntimeFault as error:
            raise ValueError("unsafe selection input") from error
        if len(self.model_dump_json()) > 8000:
            raise ValueError("selection input exceeds reserved capacity")
        return self


class OptionProbability(Contract):
    id: Identifier
    probability: Probability


class Decision(Contract):
    provider: Literal["mock"] = "mock"
    model_version: Identifier = "scripted-v1"
    decision_id: UUID = Field(default_factory=uuid4)
    pass_index: Literal[1] = 1
    candidate_ids: Annotated[tuple[Identifier, ...], Field(max_length=4)]
    selected_id: Identifier | None = None
    probabilities: (
        Annotated[tuple[OptionProbability, ...], Field(max_length=4)] | None
    ) = None
    confidence: Probability | None = None
    validation_status: Literal["valid", "invalid", "error"]
    latency_ms: Measurement = Field(default_factory=Measurement)
    cost: Measurement = Field(default_factory=Measurement)
    token_usage: Measurement = Field(
        default_factory=lambda: Measurement(availability="not_provided")
    )


class DecisionEngine(Protocol):
    async def select(self, state: DecisionState) -> Decision: ...


class SelectionAuthority(Contract):
    decision_id: UUID
    selected_id: Identifier | None
    confidence: Probability | None
    model_version: Identifier
    accepted: bool
    pass_index: Literal[1, 2] = 1
    #: Which provider answered the call this authority was normalized from.
    #: It travels with the authority so the run record, the durable events,
    #: and the terminal result can say who answered without re-reading a
    #: configuration value. It gates nothing: `accepted` above was already
    #: recomputed from the offered options and the threshold.
    provider: Provider = "mock"
    history_status: HistoryStatus = "not_requested"
    receipt_ids: Annotated[tuple[Digest, ...], Field(max_length=4)] = ()
    history_truncated: bool = False
    blocked_tool_id: Identifier | None = None
    blocked_reason: Reason | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.accepted and (
            self.selected_id in {None, "request_review"} or self.confidence is None
        ):
            raise ValueError("accepted selection missing choice or confidence")
        if (self.history_status == "matched") != bool(self.receipt_ids):
            raise ValueError("history status does not match receipt availability")
        if self.history_truncated and self.history_status != "matched":
            raise ValueError("truncation requires matched history")
        if len(set(self.receipt_ids)) != len(self.receipt_ids):
            raise ValueError("duplicate receipt identities")
        if (self.blocked_tool_id is None) != (self.blocked_reason is None):
            raise ValueError("blocked repetition requires tool and reason")
        return self


def accepted(decision: Decision, state: DecisionState, threshold: float) -> bool:
    identifiers = tuple(probe.id for probe in state.candidates) + ("request_review",)
    if len(set(identifiers)) != len(identifiers):
        return False
    if (
        decision.validation_status != "valid"
        or decision.confidence is None
        or decision.confidence < threshold
    ):
        return False
    if (
        decision.candidate_ids != identifiers
        or decision.selected_id not in identifiers[:-1]
    ):
        return False
    if (
        decision.probabilities is None
        or tuple(item.id for item in decision.probabilities) != identifiers
    ):
        return False
    return abs(sum(item.probability for item in decision.probabilities) - 1.0) <= 0.001
