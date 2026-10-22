"""Typed contracts for the operator surface.

Three properties are structural rather than configured. The bind address
accepts a loopback host only, so no deployment can widen it. `RunRequest`
carries a scenario and an idempotency key and nothing else, so a caller cannot
restate the immutable task, its dataset, or its date. And `DisplayValue` cannot
hold a number unless the measurement was actually taken, so an unavailable
value can never be rendered as zero.
"""

import re
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from unstuck.domain import Contract, Digest, Identifier, Reason, Row

#: The only hosts this surface will ever bind.
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
DEFAULT_PORT = 8081

Mode = Literal["offline", "live", "replay"]
Availability = Literal["measured", "not_measured", "not_provided", "not_called"]
NodeKind = Literal[
    "task",
    "decision_first",
    "history_lookup",
    "diagnostic",
    "receipt",
    "blocked_repeat",
    "decision_final",
    "wait",
    "repair",
    "verification",
    "result",
]
NodeState = Literal[
    "verified", "pending", "blocked", "unresolved", "error", "skipped", "informational"
]
ExportFormat = Literal["jsonl", "json", "csv"]

#: Text for an availability that carries no number. None of these is "0".
ABSENT_TEXT: dict[str, str] = {
    "not_measured": "Not measured",
    "not_provided": "Not provided",
    "not_called": "Not called",
}

_SENSITIVE = re.compile(
    r"authorization|secret|token|password|api[_-]?key|credential|environment"
    # Stale architecture names are deliberately absent: they are not sensitive
    # data, the field patterns already bound these inputs, and a literal here
    # would put a retired name into the shipped package, which the design.md
    # section 20 leakage scan refuses.
    r"|evaluator|bearer\s|hidden|expected[_\s-]*(?:rows|action)",
    re.I,
)


def _safe(value: str) -> str:
    if _SENSITIVE.search(value):
        raise ValueError("operator input carries a forbidden marker")
    return value


SafeLabel = Annotated[Identifier, Field(min_length=1, max_length=64)]


class BindAddress(Contract):
    """Where the local API listens. Loopback only, by construction."""

    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1024, le=65535)] = DEFAULT_PORT

    @model_validator(mode="after")
    def loopback_only(self) -> Self:
        if self.host not in LOOPBACK_HOSTS:
            raise ValueError("the operator API binds a loopback host only")
        return self

    @property
    def authority(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def authorities(self) -> tuple[str, ...]:
        """Every Host header value this bind accepts, exactly."""
        return tuple(f"{host}:{self.port}" for host in ("127.0.0.1", "localhost"))

    @property
    def origins(self) -> tuple[str, ...]:
        return tuple(f"http://{item}" for item in self.authorities)


class RunRequest(Contract):
    """Everything a caller may say. The task is not theirs to restate."""

    scenario: SafeLabel
    idempotency_key: Annotated[Identifier, Field(min_length=1, max_length=128)]
    mode: Mode = "offline"

    @model_validator(mode="after")
    def clean(self) -> Self:
        _safe(self.scenario)
        _safe(self.idempotency_key)
        return self


class ModeLabel(Contract):
    """What was asked for, what actually runs, and the gate in between."""

    requested: Mode
    effective: Literal["offline", "replay"] | None = None
    provider: Literal["mock", "replay"] | None = None
    blocked: bool = False
    gate: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.blocked != (self.effective is None):
            raise ValueError("a blocked mode has no effective mode")
        if self.blocked != (self.gate is not None):
            raise ValueError("a blocked mode names the gate it waits for")
        if (self.effective is None) != (self.provider is None):
            raise ValueError("an effective mode names its provider")
        return self

    @property
    def text(self) -> str:
        if self.blocked:
            return f"{self.requested.capitalize()} mode blocked: {self.gate}"
        return f"{str(self.effective).capitalize()} mode, {self.provider} provider"

    def require_runnable(self) -> None:
        """Refuse to execute a blocked mode. There is no silent fallback."""
        from unstuck.operator import OperatorFault, OperatorReason

        if not self.blocked:
            return
        reason = (
            OperatorReason.LIVE_MODE_BLOCKED
            if self.requested == "live"
            else OperatorReason.REPLAY_UNAVAILABLE
        )
        raise OperatorFault(reason, str(self.gate))


class DisplayValue(Contract):
    """A rendered measurement. An absent one carries no number at all."""

    text: Annotated[str, Field(min_length=1, max_length=64)]
    availability: Availability
    value: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None

    @model_validator(mode="after")
    def explicit_absence(self) -> Self:
        if (self.availability == "measured") != (self.value is not None):
            raise ValueError("availability does not match the recorded value")
        if self.value is None and self.text != ABSENT_TEXT[self.availability]:
            raise ValueError("an absent measurement uses its declared text")
        return self

    @classmethod
    def for_availability(cls, availability: Availability) -> "DisplayValue":
        return cls(text=ABSENT_TEXT[availability], availability=availability)

    @classmethod
    def measured(cls, value: float) -> "DisplayValue":
        return cls(text=f"{value:g}", availability="measured", value=value)


class SpineNode(Contract):
    """One step of the provenance spine. Absent steps say so."""

    node_id: SafeLabel
    order: Annotated[int, Field(ge=1, le=32)]
    kind: NodeKind
    label: Annotated[str, Field(min_length=1, max_length=120)]
    state: NodeState
    detail: Annotated[str, Field(min_length=1, max_length=512)]
    present: bool
    evidence_ids: Annotated[tuple[UUID, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not self.present and self.state != "skipped":
            raise ValueError("a step that did not happen is skipped")
        if self.present and self.state == "skipped":
            raise ValueError("a step that happened is not skipped")
        return self


class EvidencePanel(Contract):
    """The evidence, raw event, and validation tabs for one node."""

    node_id: SafeLabel
    evidence: dict[str, JsonValue]
    validation: dict[str, JsonValue]
    raw_event: dict[str, JsonValue] | None = None


class RunView(Contract):
    """Everything Run Detail shows, already truthful and already redacted."""

    run_id: UUID
    #: The scenario this run was created from, and the canonical identity of
    #: the fixture that scenario built. They are the run's own authority: a
    #: reader can see which synthetic inputs produced the stored run rather
    #: than having to trust the token the caller last typed.
    scenario: SafeLabel | None = None
    fixture_identity: Digest | None = None
    status: Literal[
        "created",
        "running",
        "completed",
        "empty_verified",
        "pending",
        "unresolved",
        "budget_exhausted",
        "failed",
    ]
    reason_code: Reason | None
    outcome_text: Annotated[str, Field(min_length=1, max_length=256)]
    mode: ModeLabel
    classification: Literal["synthetic"] = "synthetic"
    baseline_id: Literal["decider", "decider_memory"]
    path: Literal["direct", "re_decided", "no_cascade"]
    task_hash: Digest
    base_query_hash: Digest
    query_hash: Digest
    duration: DisplayValue
    cost: DisplayValue
    confidence: DisplayValue
    selector_calls: Annotated[int, Field(ge=0, le=2)]
    diagnostic_reads: Annotated[int, Field(ge=0, le=2)]
    wait_checks: Annotated[int, Field(ge=0, le=1)]
    repair_attempts: Annotated[int, Field(ge=0, le=1)]
    history_status: Literal["not_requested", "unavailable", "empty", "matched"]
    receipt_references: Annotated[tuple[Digest, ...], Field(max_length=4)] = ()
    history_truncated: bool = False
    first_choice: Identifier | None = None
    final_choice: Identifier | None = None
    blocked_tool_id: Identifier | None = None
    blocked_reason_code: Reason | None = None
    verification_status: Literal["passed", "failed", "not_run"]
    rows: Annotated[tuple[Row, ...], Field(max_length=250)] = ()
    artifact_refs: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    spine: Annotated[tuple[SpineNode, ...], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.scenario is None) != (self.fixture_identity is None):
            raise ValueError("a bound scenario names its fixture identity")
        if (self.blocked_tool_id is None) != (self.blocked_reason_code is None):
            raise ValueError("a blocked repetition names its tool and its reason")
        if (self.history_status == "matched") != bool(self.receipt_references):
            raise ValueError("matched history carries its references")
        if self.history_truncated and self.history_status != "matched":
            raise ValueError("truncation requires matched history")
        if [node.order for node in self.spine] != list(range(1, len(self.spine) + 1)):
            raise ValueError("the spine is contiguously ordered from one")
        return self


class ExportRequest(Contract):
    """One evidence export. The label is explicit, never inferred."""

    run_id: UUID
    format: ExportFormat
    experiment_id: SafeLabel | None = None


__all__ = [
    "ABSENT_TEXT",
    "DEFAULT_PORT",
    "LOOPBACK_HOSTS",
    "Availability",
    "BindAddress",
    "DisplayValue",
    "EvidencePanel",
    "ExportFormat",
    "ExportRequest",
    "Mode",
    "ModeLabel",
    "NodeKind",
    "NodeState",
    "RunRequest",
    "RunView",
    "SpineNode",
]
