"""Verifier-derived labels: execute every offered candidate through the real stack.

One call here is one real `Controller.run` over the real DuckDB
`EvidenceEngine`, the real `CascadeEngine`, the real `ReceiptVerifier` and a
real local receipt store, exactly as `tests/core_runtime/test_receipt_controller`
composes them. The only stand-in is the Decider: `CapturingDecider` records the
exact production Choice payload the controller built (`choice_request`, then
`serve_request` and `encode_serve_request`) and answers with whatever the caller
supplies, so a label can be derived by answering each candidate in turn.

A candidate is productive iff the verifier classifies its read
`useful_evidence` and the run then reaches a verified terminal reason with no
false repair. A false repair is any applied repair in a run that did not end
populated or empty after that repair, or in a fixture whose declared encodings
do not call for it. The verifier owns the outcome class and the controller
owns the terminal; nothing here labels anything by hand.

Identities that production mints with `uuid4` (evidence records and diagnostic
call identities) are pinned to `uuid5` of the call scope while a call runs, so
the same case always renders the same bytes. Decision and model-call
identities come from this module's own Decider. The shape of every payload is
unchanged; only which random identity it carries is fixed.
"""

import asyncio
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

import unstuck.cascade.engine as cascade_engine
from deployment.sagemaker.server.contract import ServeRequest
from experiments.decider_finetune.identity import prompt_digest
from experiments.decider_finetune.scenarios import CATALOGS, Scenario
from unstuck.cascade.cards import CardBinding, capability_cards
from unstuck.cascade.choice import ChoiceRequest, choice_request
from unstuck.cascade.engine import CascadeEngine
from unstuck.cascade.matching import HistoryMatcher
from unstuck.cascade.policy import AcceptanceTier, Calibration, CascadePolicy
from unstuck.cascade.projector import SelectorInput
from unstuck.cascade.receipts import ReceiptNamespace
from unstuck.cascade.repository import LocalReceiptRepository
from unstuck.cascade.selection import Provider, RawSelection
from unstuck.cascade.verifier import ReceiptVerifier
from unstuck.clock import FakeClock
from unstuck.cloud.decider import encode_serve_request, serve_request
from unstuck.controller import Controller
from unstuck.decision import OptionProbability, Probe
from unstuck.domain import EvidenceRecord, Settings
from unstuck.fixtures import Fixture
from unstuck.query import EvidenceEngine
from unstuck.storage import LocalRunRepository

ARM: Literal["decider_memory"] = "decider_memory"
REVIEW = "request_review"
#: The namespace every pinned identity is derived under.
PIN_NAMESPACE = UUID("9b3f0c1e-58d2-4c4a-a1f7-2e6d0b9c7a35")
#: Every call's clock starts here, so no wall-clock time reaches any record.
FIXED_ORIGIN = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
#: Terminal reasons the controller reaches only through verified evidence.
VERIFIED_REASONS = frozenset(
    {
        "populated_after_repair",
        "populated_after_wait",
        "empty_initial",
        "empty_after_wait",
        "empty_after_repair",
        "still_pending_after_wait",
    }
)
REPAIRED_REASONS = frozenset({"populated_after_repair", "empty_after_repair"})


@dataclass(frozen=True)
class Prompt:
    """What the model reads: the question, the state text, the ordered options."""

    question: str
    state_text: str
    options: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, payload: ChoiceRequest) -> "Prompt":
        return cls(
            question=payload.question,
            state_text=payload.state_text,
            options=tuple((o.option_id, o.criterion) for o in payload.options),
        )

    @property
    def digest(self) -> str:
        """Content identity of the prompt alone, independent of call identity."""
        return prompt_digest(self.question, self.state_text, self.options)


@dataclass(frozen=True)
class Answer:
    """One selector answer. One-hot unless a distribution is supplied."""

    selected_id: str
    probabilities: tuple[tuple[str, float], ...] | None = None
    confidence: float = 1.0


Answerer = Callable[[SelectorInput, Prompt], Answer]


@dataclass(frozen=True)
class Captured:
    """One selector request exactly as production would have sent it."""

    pass_index: int
    history_status: str
    available_tool_ids: tuple[str, ...]
    precluded_tool_ids: tuple[str, ...]
    prompt: Prompt
    wire: bytes


@dataclass
class CapturingDecider:
    """Records every production payload and answers through `answerer`."""

    answerer: Answerer
    model_revision: str
    scope: str
    provider: Provider = "mock"
    captured: list[Captured] = field(default_factory=list)

    async def select(self, request: SelectorInput) -> RawSelection:
        payload = choice_request(request)
        ordinal = len(self.captured) + 1
        call_id = uuid5(PIN_NAMESPACE, f"{self.scope}:model-call:{ordinal}")
        wire = encode_serve_request(
            serve_request(payload, model_revision=self.model_revision, call_id=call_id)
        )
        # The model host's own closed contract must accept the exact bytes.
        ServeRequest.model_validate_json(wire)
        prompt = Prompt.of(payload)
        self.captured.append(
            Captured(
                pass_index=request.pass_index,
                history_status=request.history.status,
                available_tool_ids=request.available_tool_ids,
                precluded_tool_ids=tuple(i.public_tool_id for i in request.precluded),
                prompt=prompt,
                wire=wire,
            )
        )
        answer = self.answerer(request, prompt)
        options = payload.option_ids
        distribution = dict(answer.probabilities or ())
        if not distribution:
            distribution = {o: float(o == answer.selected_id) for o in options}
        return RawSelection(
            model_revision=self.model_revision,
            decision_id=uuid5(PIN_NAMESPACE, f"{self.scope}:decision:{ordinal}"),
            model_call_id=call_id,
            pass_index=request.pass_index,
            candidate_ids=options,
            validation_status="valid",
            provider=self.provider,
            selected_id=answer.selected_id,
            probabilities=tuple(
                OptionProbability(id=o, probability=distribution.get(o, 0.0))
                for o in options
            ),
            confidence=answer.confidence,
        )


_scope: dict[str, Any] = {"key": "", "n": 0}


def _pinned() -> UUID:
    _scope["n"] += 1
    return uuid5(PIN_NAMESPACE, f"{_scope['key']}:{_scope['n']}")


@contextmanager
def pinned_identities(scope: str) -> Iterator[None]:
    """Pin production's random identities to the call scope, then restore them."""
    evidence = EvidenceRecord.model_fields["evidence_id"]
    shipped_factory = evidence.default_factory
    shipped_uuid4 = getattr(cascade_engine, "uuid4")  # noqa: B009
    _scope.update(key=scope, n=0)
    evidence.default_factory = _pinned
    EvidenceRecord.model_rebuild(force=True)
    setattr(cascade_engine, "uuid4", _pinned)  # noqa: B010
    try:
        yield
    finally:
        evidence.default_factory = shipped_factory
        EvidenceRecord.model_rebuild(force=True)
        setattr(cascade_engine, "uuid4", shipped_uuid4)  # noqa: B010


def bindings(probes: tuple[Probe, ...]) -> tuple[CardBinding, ...]:
    """Publish a catalog's cards with each private capability resolvable.

    A renamed public identifier is not a label `canonical_capability` knows,
    so the capability itself is bound as a private resolution label. It is
    never part of a card, so no model input carries it from here.
    """
    published: list[CardBinding] = []
    for binding in capability_cards(probes):
        labels = tuple(
            dict.fromkeys(
                label
                for label in (binding.capability, *binding.aliases)
                if label != binding.card.public_tool_id
            )
        )
        published.append(
            CardBinding(
                card=binding.card, capability=binding.capability, aliases=labels
            )
        )
    return tuple(published)


@dataclass(frozen=True)
class Receipt:
    public_tool_id: str
    outcome_class: str
    blocking: bool


@dataclass(frozen=True)
class CallTrace:
    """One finished call, read back from its own durable events."""

    status: str
    reason_code: str
    captured: tuple[Captured, ...]
    executed: tuple[str, ...]
    receipts: tuple[Receipt, ...]
    precluded: tuple[str, ...]
    repair_applied: bool
    rows: tuple[tuple[str, int], ...]

    @property
    def first_read_outcome(self) -> str | None:
        return self.receipts[0].outcome_class if self.receipts else None


@dataclass(frozen=True)
class Config:
    """The frozen call configuration every row and every replay is made under."""

    model_revision: str
    acceptance_tier: AcceptanceTier = "read_only_structural"


def run_call(
    scenario: Scenario,
    *,
    call_index: int,
    budget: int,
    store: LocalReceiptRepository,
    answerer: Answerer,
    config: Config,
    provider: Provider = "mock",
) -> CallTrace:
    """Run one real call for one scenario and read it back."""
    fixture = scenario.fixture()
    probes = CATALOGS[scenario.catalog].probes()
    cards = bindings(probes)
    scope = f"{scenario.key}:budget{budget}:call{call_index}"
    clock = FakeClock()
    clock.origin = FIXED_ORIGIN
    policy = CascadePolicy(
        baseline_id=ARM,
        model_revision=config.model_revision,
        calibration=Calibration(acceptance_tier=config.acceptance_tier),
    )
    decider = CapturingDecider(answerer, config.model_revision, scope, provider)
    cascade = CascadeEngine(
        decider=decider,
        repository=store,
        verifier=ReceiptVerifier(clock, cards=cards),
        clock=clock,
        policy=policy,
        cards=cards,
        matcher=HistoryMatcher(store),
        max_diagnostic_reads=budget,
    )
    with tempfile.TemporaryDirectory() as root, pinned_identities(scope):
        runs = LocalRunRepository(Path(root).resolve(), clock=clock)
        run = runs.create_run(scenario.task(), fixture.dataset_version, scope[-128:])
        engine = EvidenceEngine(fixture)
        try:
            controller = Controller(
                runs,
                engine,
                clock=clock,
                catalog=probes,
                settings=Settings(
                    baseline_id=ARM, model_revision=config.model_revision
                ),
                cascade=cascade,
                namespace=ReceiptNamespace(arm=ARM, case_id=f"c{scenario.opaque}"),
                idempotency_key=f"k{scenario.opaque}-{budget}-{call_index}",
            )
            result = asyncio.run(controller.run(run.state.run_id))
            final = runs.load_run(run.state.run_id)
        finally:
            engine.close()
    events = final.events
    return CallTrace(
        status=str(result.get("status")),
        reason_code=str(result.get("reason_code")),
        captured=tuple(decider.captured),
        executed=tuple(
            str(e.payload["tool_id"]) for e in events if e.type == "probe_returned"
        ),
        receipts=tuple(
            Receipt(
                str(e.payload["public_tool_id"]),
                str(e.payload["outcome_class"]),
                bool(e.payload["blocking"]),
            )
            for e in events
            if e.type == "decision_receipt_written"
        ),
        precluded=tuple(
            str(e.payload["public_tool_id"])
            for e in events
            if e.type == "repeat_blocked" and e.payload.get("stage") == "pre_selection"
        ),
        repair_applied=any(e.type == "repair_applied" for e in events),
        rows=_rows(result),
    )


def _rows(result: dict[str, Any]) -> tuple[tuple[str, int], ...]:
    rows = result.get("rows") or ()
    return tuple(
        sorted(
            (str(r["country_code"]), int(r["units"]))
            for r in rows
            if isinstance(r, dict)
        )
    )


def expected_rows(
    fixture: Fixture, country_codes: tuple[str, ...]
) -> tuple[tuple[str, int], ...]:
    """What a correct populated report returns, from the public fixture alone."""
    if fixture.job_state != "complete":
        return ()
    codes = {
        c.country_id.removeprefix("country:"): c.country_code for c in fixture.countries
    }
    totals: dict[str, int] = {}
    for order in fixture.orders:
        code = codes.get(order.country_id.removeprefix("country:"))
        if order.order_date_utc != fixture.order_date_utc or code is None:
            continue
        if country_codes and code not in country_codes:
            continue
        totals[code] = totals.get(code, 0) + order.units
    return tuple(sorted(totals.items()))


def false_repair(trace: CallTrace, fixture: Fixture) -> bool:
    if not trace.repair_applied:
        return False
    warranted = (
        fixture.orders_key_encoding != fixture.countries_key_encoding
        and "strip_country_prefix" in fixture.approved_join_modes
    )
    return not warranted or trace.reason_code not in REPAIRED_REASONS


def verified(trace: CallTrace, scenario: Scenario) -> bool:
    """The run ended on a verified terminal with the rows the fixture implies."""
    fixture = scenario.fixture()
    if trace.reason_code not in VERIFIED_REASONS or false_repair(trace, fixture):
        return False
    if trace.reason_code.startswith("populated"):
        return trace.rows == expected_rows(fixture, scenario.task().country_codes)
    return not trace.rows


def productive(trace: CallTrace, scenario: Scenario) -> bool:
    return trace.first_read_outcome == "useful_evidence" and verified(trace, scenario)


def choose(public_id: str) -> Answerer:
    """Answer the first decision with `public_id` and any later one with review."""

    def answer(request: SelectorInput, prompt: Prompt) -> Answer:
        if request.pass_index == 1 and public_id in request.available_tool_ids:
            return Answer(public_id)
        return Answer(REVIEW)

    return answer


@dataclass(frozen=True)
class Outcome:
    """What one candidate did when it was the answer to one request."""

    public_id: str
    capability: str
    first_read_outcome: str | None
    reason_code: str
    verified: bool
    productive: bool
    false_repair: bool


@dataclass(frozen=True)
class Labeled:
    """One request and what every offered candidate did from it."""

    captured: Captured
    outcomes: tuple[Outcome, ...]

    @property
    def productive(self) -> tuple[str, ...]:
        return tuple(o.public_id for o in self.outcomes if o.productive)


def _outcome(trace: CallTrace, scenario: Scenario, public_id: str) -> Outcome:
    catalog = CATALOGS[scenario.catalog]
    return Outcome(
        public_id=public_id,
        capability=catalog.capability(public_id),
        first_read_outcome=trace.first_read_outcome,
        reason_code=trace.reason_code,
        verified=verified(trace, scenario),
        productive=productive(trace, scenario),
        false_repair=false_repair(trace, scenario.fixture()),
    )


def _single(traces: list[CallTrace]) -> Captured:
    """The one first-decision request every counterfactual saw. Must be identical."""
    firsts = [t.captured[0] for t in traces if t.captured]
    if len(firsts) != len(traces) or len({c.wire for c in firsts}) != 1:
        raise RuntimeError("counterfactual runs saw different first requests")
    return firsts[0]


def label_fresh(scenario: Scenario, budget: int, config: Config) -> Labeled | None:
    """A new call on an empty namespace: run each offered candidate as D1."""
    probe = run_call(
        scenario,
        call_index=1,
        budget=budget,
        store=LocalReceiptRepository(),
        answerer=choose(REVIEW),
        config=config,
    )
    if not probe.captured:
        return None
    offered = probe.captured[0].available_tool_ids
    traces = {
        public_id: run_call(
            scenario,
            call_index=1,
            budget=budget,
            store=LocalReceiptRepository(),
            answerer=choose(public_id),
            config=config,
        )
        for public_id in offered
    }
    captured = _single([probe, *traces.values()])
    return Labeled(
        captured,
        tuple(_outcome(trace, scenario, pid) for pid, trace in traces.items()),
    )


def label_after(
    scenario: Scenario, prior: str, config: Config
) -> tuple[CallTrace, Labeled] | None:
    """Run 2 after Run 1 answered `prior`, in the loop-escape configuration."""

    def run_one(store: LocalReceiptRepository) -> CallTrace:
        return run_call(
            scenario,
            call_index=1,
            budget=1,
            store=store,
            answerer=choose(prior),
            config=config,
        )

    probe_store = LocalReceiptRepository()
    first = run_one(probe_store)
    probe = run_call(
        scenario,
        call_index=2,
        budget=1,
        store=probe_store,
        answerer=choose(REVIEW),
        config=config,
    )
    if not probe.captured:
        return None
    offered = probe.captured[0].available_tool_ids
    traces: dict[str, CallTrace] = {}
    for public_id in offered:
        store = LocalReceiptRepository()
        run_one(store)
        traces[public_id] = run_call(
            scenario,
            call_index=2,
            budget=1,
            store=store,
            answerer=choose(public_id),
            config=config,
        )
    captured = _single([probe, *traces.values()])
    return first, Labeled(
        captured,
        tuple(_outcome(trace, scenario, pid) for pid, trace in traces.items()),
    )
