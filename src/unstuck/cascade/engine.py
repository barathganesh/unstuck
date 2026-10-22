"""Bounded Decision Receipt orchestration.

The literal owner sequence for one incident is:

1. Compute the problem fingerprint and the current decision context.
2. Retrieve a bounded set of exact or compatible Decision Receipts, when the
   arm authorizes memory at all. A retrieval that could not be read, or whose
   read budget stopped with receipts unread, is an incomplete history and
   stops the incident before any selector or tool call.
3. Preclude, before any selector call, every available execution an exact
   current-context receipt proves already moved no supported disposition with
   the identical capability and arguments, whether because it admitted nothing
   at all or because the fact it admitted opened nothing. Each preclusion is
   journaled before the request. When nothing remains, stop with no call.
4. Call the first decision with the complete current state, that labeled
   history, and the remaining offer. It always chooses fresh, and execute it.
5. Verify the evidence delta and persist a receipt from verifier-owned facts.
6. When the admitted evidence moved a supported disposition, finish. Otherwise
   refresh the authoritative state from the execution authority, derive the
   current decision context from it, and open exactly one second decision over
   the remaining candidates, with the first execution removed but its receipt,
   its reason, and the first record attached.

Opening that second decision and refusing a future repetition are two separate
questions, and they stay separate. The second decision opens whenever the first
read moved no supported disposition. Refusing a *future* identical execution
stays with `DecisionReceipt.blocking`, which requires one of the two
disposition-negative classes plus an exact current-context match on the
identical capability and arguments. A preclusion only narrows the offer: the
decision identity stays the full original context, the remaining candidates
keep their order, and nothing is chosen, ranked, or scored by a receipt. It
stands for the whole call: matching identity is the call's original context,
so a proven dead end is never re-offered to a same-call second decision. A read that moved a gate is never
refusable, however the incident ended, and a same-call receipt travels into the
second decision as labeled history that carries no authority of its own.

Hard bounds: two selector calls and two diagnostic reads per incident. A frozen
experiment budget may lower the read bound to one, never raise it. There is
no recursion, no third selection, no identical-input transport retry, and no
budget reset by a renamed tool, a reordered evidence set, a new operational
identity, or a repeated invocation. Current authoritative facts always dominate
history; a changed context can make a previously unhelpful read eligible again.

Duplicate invocations are not resolved here. Idempotency is a durable property
of the run store, so this unit keeps no cross-call state of its own and one
instance may serve concurrent runs: the execution authority and the durable
journal are handed in per invocation and nothing about one run is reachable
from another.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID, uuid4

from pydantic import Field, ValidationError, model_validator

from unstuck.cascade.cards import CardBinding, canonical_capability, capability_cards
from unstuck.cascade.execution import DiagnosticExecutor, DiagnosticRequest
from unstuck.cascade.identity import CallIdentity, DecisionContext
from unstuck.cascade.integration import context_for
from unstuck.cascade.matching import HistoryLookupResult, HistoryMatcher
from unstuck.cascade.policy import Arm, CascadePolicy
from unstuck.cascade.projector import (
    BlockedExecution,
    SelectorBudgets,
    SelectorInput,
    project_selector_input,
)
from unstuck.cascade.receipts import (
    EVIDENCE_OUTCOMES,
    DecisionReceipt,
    OutcomeClass,
    ReceiptNamespace,
)
from unstuck.cascade.repository import ReceiptRepository
from unstuck.cascade.selection import (
    Decider,
    RawSelection,
    SelectionRecord,
    first_choice,
    normalize,
    rejection_for,
)
from unstuck.cascade.serialization import (
    MAX_DIAGNOSTIC_READS,
    MAX_SELECTOR_CALLS,
    StageMeasurement,
)
from unstuck.cascade.verifier import DiagnosticReport, ReceiptVerifier
from unstuck.clock import Clock
from unstuck.decision import DecisionState
from unstuck.domain import (
    Contract,
    Counters,
    Digest,
    Identifier,
    Reason,
    RuntimeFault,
)
from unstuck.events import PersistenceBoundary, Redactor
from unstuck.policy import consume

#: Every reason this unit may report. Terminal data outcomes stay outside it.
CASCADE_REASONS = frozenset(
    {
        Reason.FIRST_REJECTED,
        Reason.FINAL_REJECTED,
        Reason.HISTORY_UNAVAILABLE,
        Reason.RECEIPT_INVALID,
        Reason.REPEAT_BLOCKED,
        Reason.NO_READ,
        Reason.INSUFFICIENT,
        Reason.CONFLICT,
        Reason.DIAGNOSTICS_EXHAUSTED,
        Reason.REPAIR_FAILED,
        Reason.UNSUPPORTED_REPAIR,
        Reason.DECIDER_ERROR,
        Reason.TOOL_ERROR,
        Reason.DEADLINE,
        Reason.TASK_MISMATCH,
        Reason.INVARIANT,
    }
)


class CascadeRequest(Contract):
    """One invocation. Operational identity is separate from semantic identity.

    The caller's idempotency key travels here as a label only. Whether an
    invocation is a duplicate is a durable question about the run store, not a
    judgement this unit makes, so nothing here is matched against a previous
    invocation.
    """

    cascade_id: UUID
    idempotency_key: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_:-]+$")
    ]
    namespace: ReceiptNamespace
    call: CallIdentity
    state: DecisionState
    used_capabilities: Annotated[tuple[Identifier, ...], Field(max_length=2)] = ()
    remaining_ms: Annotated[int, Field(ge=0, le=120000)] = 120000

    @property
    def arguments_hash(self) -> str:
        """Diagnostic reads take no arguments, so the bound query is the input."""
        return self.state.query_hash


class CascadeResult(Contract):
    """The complete record of one bounded cascade."""

    cascade_id: UUID
    baseline_id: Arm
    path: Literal["direct", "rechosen", "unresolved"]
    history: HistoryLookupResult
    consumed: Counters
    duration_ms: StageMeasurement
    selected_tool_id: Identifier | None = None
    executed_capability: Identifier | None = None
    unresolved_reason: Reason | None = None
    outcome_class: OutcomeClass | None = None
    first: SelectionRecord | None = None
    final: SelectionRecord | None = None
    blocked: Annotated[tuple[BlockedExecution, ...], Field(max_length=2)] = ()
    #: Exact repetitions removed from the first offer before any selector call.
    precluded: Annotated[tuple[BlockedExecution, ...], Field(max_length=3)] = ()
    created_receipt_ids: Annotated[tuple[Digest, ...], Field(max_length=2)] = ()
    reports: Annotated[tuple[DiagnosticReport, ...], Field(max_length=2)] = ()

    @model_validator(mode="after")
    def mapped(self) -> Self:
        if any(item.blocked_class != "exact_repeat" for item in self.precluded):
            raise ValueError("only an exact repetition is precluded")
        if self.path == "unresolved":
            if (
                self.selected_tool_id is not None
                or self.executed_capability is not None
            ):
                raise ValueError("an unresolved cascade executed no accepted read")
            if self.unresolved_reason not in CASCADE_REASONS:
                raise ValueError("unresolved reason is outside cascade authority")
        else:
            if self.selected_tool_id is None or self.executed_capability is None:
                raise ValueError("a resolved cascade names its executed read")
            if self.unresolved_reason is not None:
                raise ValueError("a resolved cascade carries no unresolved reason")
            if self.outcome_class not in EVIDENCE_OUTCOMES:
                raise ValueError(
                    "a resolved cascade requires admitted current evidence"
                )
        if self.path == "direct" and (
            self.consumed.decider_calls != 1 or self.final is not None
        ):
            raise ValueError("a direct path uses exactly one selector call")
        if self.path == "rechosen" and (
            self.consumed.decider_calls != 2 or self.final is None or not self.blocked
        ):
            raise ValueError("a second choice requires a blocked execution and record")
        # Reads are counted before execution, so a failed read consumes its
        # allowance without producing a report.
        if len(self.reports) > self.consumed.diagnostic_reads:
            raise ValueError("more reports than the consumed read allowance")
        if len(self.created_receipt_ids) != len(self.reports):
            raise ValueError("every observed report has exactly one receipt")
        if self.path != "unresolved" and (
            len(self.reports) != self.consumed.diagnostic_reads
        ):
            raise ValueError("a resolved cascade observed every counted read")
        if len(set(self.created_receipt_ids)) != len(self.created_receipt_ids):
            raise ValueError("duplicate receipt identities")
        if (
            self.consumed.wait_checks
            or self.consumed.report_queries
            or self.consumed.repair_attempts
            or self.consumed.agent_turns
        ):
            raise ValueError("the cascade never consumes a controller allowance")
        return self

    @property
    def selector_calls(self) -> int:
        return self.consumed.decider_calls

    @property
    def diagnostic_reads(self) -> int:
        return self.consumed.diagnostic_reads


class CascadeJournal(Protocol):
    """The owning caller's durable record of one cascade.

    The cascade owns every decision; the journal owns every durable write. Each
    method is called at the moment the matching allowance or verified fact
    becomes final, and always before the provider or tool call that allowance
    pays for, so a refusal here stops the cascade instead of hiding an attempt.
    The journal belongs to one invocation and is handed in per call.
    """

    def history(
        self, result: HistoryLookupResult, problem_fingerprint: str
    ) -> None: ...
    def requested(self, payload: SelectorInput) -> None: ...
    def returned(
        self,
        record: SelectionRecord,
        result: HistoryLookupResult,
        blocked: tuple[BlockedExecution, ...],
    ) -> None: ...
    def refused(self, execution: BlockedExecution) -> None: ...
    def precluded(self, execution: BlockedExecution) -> None: ...
    def admitted(self, receipt: DecisionReceipt, stored: bool) -> None: ...


class SilentJournal:
    """Records nothing. Used when no durable caller owns the cascade."""

    def history(self, result: HistoryLookupResult, problem_fingerprint: str) -> None:
        return None

    def requested(self, payload: SelectorInput) -> None:
        return None

    def returned(
        self,
        record: SelectionRecord,
        result: HistoryLookupResult,
        blocked: tuple[BlockedExecution, ...],
    ) -> None:
        return None

    def refused(self, execution: BlockedExecution) -> None:
        return None

    def precluded(self, execution: BlockedExecution) -> None:
        return None

    def admitted(self, receipt: DecisionReceipt, stored: bool) -> None:
        return None


@dataclass
class _Run:
    """One in-flight cascade. Every mutable value here belongs to one call.

    `counters` is this cascade's own consumption, which is what the result
    reports. `incident` is the whole incident's consumption, inherited from the
    controller's current budgets, and it is what the hard maxima are enforced
    against. A read the controller already spent therefore cannot be spent
    again here.

    `state` and `context` are the current authoritative state and its derived
    identity. They are replaced once, after a verified first read, so the one
    optional second decision reasons over what is true then.
    """

    request: CascadeRequest
    started: float
    incident: Counters
    journal: CascadeJournal
    executor: DiagnosticExecutor
    state: DecisionState
    context: DecisionContext
    used: tuple[str, ...] = ()
    counters: Counters = field(default_factory=Counters)
    history: HistoryLookupResult = field(default_factory=HistoryLookupResult)
    first: SelectionRecord | None = None
    final: SelectionRecord | None = None
    blocked: tuple[BlockedExecution, ...] = ()
    precluded: tuple[BlockedExecution, ...] = ()
    receipts: tuple[str, ...] = ()
    reports: tuple[DiagnosticReport, ...] = ()
    outcome: OutcomeClass | None = None


class CascadeEngine:
    """The one bounded orchestrator. It never executes or repairs anything."""

    def __init__(
        self,
        *,
        decider: Decider,
        repository: ReceiptRepository,
        verifier: ReceiptVerifier,
        clock: Clock,
        policy: CascadePolicy,
        cards: tuple[CardBinding, ...] | None = None,
        matcher: HistoryMatcher | None = None,
        redactor: Redactor | None = None,
        provider_seconds: float = 20.0,
        max_diagnostic_reads: int = MAX_DIAGNOSTIC_READS,
    ):
        # A frozen experiment budget may lower the read bound, never raise it.
        if not 1 <= max_diagnostic_reads <= MAX_DIAGNOSTIC_READS:
            raise RuntimeFault(Reason.INVARIANT)
        self.decider = decider
        self.repository = repository
        self.verifier = verifier
        self.clock = clock
        self.policy = policy
        self.cards = cards or capability_cards()
        self.matcher = matcher or HistoryMatcher(repository)
        self.boundary = PersistenceBoundary(redactor or Redactor())
        self.provider_seconds = provider_seconds
        self.max_diagnostic_reads = max_diagnostic_reads

    def _new_run(
        self,
        request: CascadeRequest,
        journal: CascadeJournal,
        executor: DiagnosticExecutor,
    ) -> _Run:
        """Open one cascade, inheriting the incident's consumed allowances."""
        return _Run(
            request=request,
            started=self.clock.monotonic(),
            incident=request.state.budgets,
            journal=journal,
            executor=executor,
            state=request.state,
            context=self._context_for(request.state, request.used_capabilities),
            used=request.used_capabilities,
        )

    async def resolve(
        self,
        request: CascadeRequest,
        *,
        executor: DiagnosticExecutor,
        journal: CascadeJournal | None = None,
    ) -> CascadeResult:
        """Resolve one invocation through one caller-owned authority.

        The execution authority and the journal belong to the invocation, never
        to this instance, so concurrent runs cannot reach each other. A failure
        this unit owns becomes an unresolved result; a failure it does not own,
        such as a refused durable write, is raised for the caller to map.
        """
        recorder: CascadeJournal = journal or SilentJournal()
        if not isinstance(request, CascadeRequest):
            raise RuntimeFault(Reason.INVARIANT)
        request = CascadeRequest.model_validate(request)
        run = self._new_run(request, recorder, executor)
        try:
            return await self._cascade(run)
        except RuntimeFault as error:
            if error.reason not in CASCADE_REASONS:
                raise
            return self._unresolved(run, error.reason)

    async def _cascade(self, run: _Run) -> CascadeResult:
        # The namespace and state reach receipts and the selector, so they pass
        # the shared boundary before any lookup or provider call.
        self.boundary.validate(run.request, Reason.RECEIPT_INVALID)
        self._remaining_ms(run)
        run.history = self._lookup(run, run.context)
        # An unreadable store and a truncated bounded read are the same fact
        # here: the history is incomplete, so a blocker may be unread and
        # nothing is selected or executed on it.
        if run.history.status == "unavailable" or run.history.retrieval_truncated:
            raise RuntimeFault(Reason.HISTORY_UNAVAILABLE)
        if not run.context.available_tool_ids:
            raise RuntimeFault(Reason.NO_READ)
        self._preclude(run)
        run.first = await self._decide(run, 1)
        if not run.first.accepted:
            raise RuntimeFault(run.first.rejection or Reason.FIRST_REJECTED)
        selected = str(run.first.selected_id)
        capability = self._capability(selected)
        if (  # pragma: no cover - a precluded execution is never offered
            run.history.blocking_match((capability, run.request.arguments_hash))
            is not None
        ):
            raise RuntimeFault(Reason.INVARIANT)
        receipt = await self._execute(run, 1, run.first, selected, capability)
        if receipt.delta.progressed:
            # The admitted fact moved a supported disposition, so the first
            # read answered the incident and nothing is re-decided.
            return self._resolved(run, "direct", selected, capability)
        # Every other verified first read leaves the incident where it was: a
        # disposition-neutral read, a stalled one, a provider failure, or
        # inadmissible evidence. Each opens the one same-call second decision.
        # The receipt travels with it as labeled same-call history and carries
        # no execution authority in this call; whether it may refuse a future
        # repetition is `DecisionReceipt.blocking`'s question alone.
        run.blocked = (
            BlockedExecution(
                public_tool_id=selected,
                selected_capability=capability,
                arguments_hash=receipt.arguments_hash,
                receipt_id=receipt.receipt_id,
                outcome_class=receipt.outcome_class,
                reason_code=receipt.reason_code,
                blocked_class="failed_execution",
            ),
        )
        run.journal.refused(run.blocked[0])
        self._refresh(run, capability, receipt.reason_code)
        return await self._second(run, receipt.reason_code)

    def _preclude(self, run: _Run) -> None:
        """Remove every exact proven dead end from the offer, before any call.

        Matching was computed against the full original context, and each
        available candidate is checked against it on its own capability and
        the bound arguments. Only a match whose blocking conjunction re-derives
        true from its own deltas qualifies, so a compatible, failed, invalid,
        or useful receipt precludes nothing. The reason carried is the
        verifier's own reason from that receipt.
        """
        precluded: list[BlockedExecution] = []
        for identifier in run.context.available_tool_ids:
            capability = self._capability(identifier)
            proof = run.history.blocking_match((capability, run.request.arguments_hash))
            if proof is None:
                continue
            precluded.append(
                BlockedExecution(
                    public_tool_id=identifier,
                    selected_capability=capability,
                    arguments_hash=run.request.arguments_hash,
                    receipt_id=proof.receipt_id,
                    outcome_class=proof.outcome_class,
                    reason_code=proof.reason_code,
                    blocked_class="exact_repeat",
                )
            )
        run.precluded = tuple(precluded)
        # Each preclusion is durable before the request it narrows leaves.
        for execution in run.precluded:
            run.journal.precluded(execution)
        if len(run.precluded) == len(run.context.available_tool_ids):
            # Every executable option is a proven dead end: nothing is asked.
            raise RuntimeFault(Reason.REPEAT_BLOCKED)

    def _refresh(self, run: _Run, capability: str, fallback: Reason) -> None:
        """Adopt the authority's post-read state as the current one.

        The controller executed the read and admitted its evidence, so it is
        the only holder of what is now true. Replacing the frozen request state
        here is what makes the second decision, its context identity, and its
        receipt describe the current situation. The problem itself may not
        move: a changed task or base query is a different incident, never a
        refreshed one.
        """
        try:
            projected = run.executor.project()
        except RuntimeFault as error:
            if error.reason is Reason.NO_READ:
                # The read consumed the last candidate; nothing remains.
                raise RuntimeFault(fallback) from error
            raise
        if not isinstance(projected, DecisionState):
            raise RuntimeFault(Reason.INVARIANT)
        try:
            state = DecisionState.model_validate(projected)
        except ValidationError as error:
            raise RuntimeFault(Reason.CONFLICT) from error
        self.boundary.validate(state, Reason.RECEIPT_INVALID)
        if (state.task_hash, state.query.base_query_hash, state.task.dataset_id) != (
            run.state.task_hash,
            run.state.query.base_query_hash,
            run.state.task.dataset_id,
        ):
            raise RuntimeFault(Reason.TASK_MISMATCH)
        used = tuple(dict.fromkeys(run.used + (capability,)))
        run.state = state
        run.used = used
        run.context = self._context_for(state, used)

    async def _second(self, run: _Run, fallback: Reason) -> CascadeResult:
        refused = {item.public_tool_id for item in run.blocked + run.precluded}
        if not [
            identifier
            for identifier in run.context.available_tool_ids
            if identifier not in refused
        ]:
            raise RuntimeFault(fallback)
        if run.incident.diagnostic_reads >= self.max_diagnostic_reads:
            raise RuntimeFault(Reason.DIAGNOSTICS_EXHAUSTED)
        run.final = await self._decide(run, 2)
        if not run.final.accepted:
            raise RuntimeFault(run.final.rejection or Reason.FINAL_REJECTED)
        selected = str(run.final.selected_id)
        capability = self._capability(selected)
        receipt = await self._execute(run, 2, run.final, selected, capability)
        if receipt.outcome_class not in EVIDENCE_OUTCOMES:
            # The second choice is final. There is no third selection.
            raise RuntimeFault(receipt.reason_code)
        return self._resolved(run, "rechosen", selected, capability)

    def _context_for(
        self, state: DecisionState, used: tuple[str, ...]
    ) -> DecisionContext:
        """Derive the current decision identity through the one shared builder."""
        return context_for(state, self.policy, self.cards, used_capabilities=used)

    def _capability(self, label: str) -> str:
        try:
            return canonical_capability(label, self.cards)
        except ValueError as error:
            raise RuntimeFault(Reason.INVARIANT) from error

    def _lookup(self, run: _Run, context: DecisionContext) -> HistoryLookupResult:
        requested = self.policy.memory_authorized
        result = self.matcher.lookup(
            run.request.namespace, context, requested=requested
        )
        if requested:
            self._consume(run, "history_lookups")
            run.journal.history(result, context.problem_fingerprint)
        return result

    def _consume(self, run: _Run, name: str) -> None:
        """Charge one allowance to the incident and to this cascade.

        The incident ledger is the one the hard maxima are enforced against;
        exceeding it means a guard was bypassed, never a third attempt.
        """
        try:
            run.incident = consume(run.incident, name)
        except RuntimeFault as error:
            raise RuntimeFault(Reason.INVARIANT) from error
        run.counters = consume(run.counters, name)

    def _remaining_seconds(self, run: _Run) -> float:
        elapsed = self.clock.monotonic() - run.started
        return run.request.remaining_ms / 1000 - elapsed

    def _remaining_ms(self, run: _Run) -> int:
        remaining = self._remaining_seconds(run)
        if remaining <= 0:
            raise RuntimeFault(Reason.DEADLINE)
        return min(120000, max(0, int(remaining * 1000)))

    async def _decide(self, run: _Run, pass_index: Literal[1, 2]) -> SelectionRecord:
        remaining_ms = self._remaining_ms(run)
        if run.incident.decider_calls >= MAX_SELECTOR_CALLS:
            # The incident has no selector allowance left; nothing is retried.
            raise RuntimeFault(rejection_for(pass_index))
        context = run.context
        payload = project_selector_input(
            pass_index=pass_index,
            context=context,
            cards=self.cards,
            state=run.state,
            history=run.history,
            budgets=SelectorBudgets(
                remaining_selector_calls=MAX_SELECTOR_CALLS
                - run.incident.decider_calls,
                remaining_diagnostic_reads=max(
                    0, self.max_diagnostic_reads - run.incident.diagnostic_reads
                ),
                remaining_ms=remaining_ms,
            ),
            blocked=run.blocked if pass_index == 2 else (),
            precluded=run.precluded,
            first=(
                first_choice(run.first)
                if pass_index == 2 and run.first is not None
                else None
            ),
            redactor=self.boundary.redactor,
        )
        # The attempt is counted and recorded before the request leaves, so a
        # failure, a late answer, or a refused durable write all consume it.
        self._consume(run, "decider_calls")
        run.journal.requested(payload)
        raw = await self._call(run, payload)
        threshold = (
            self.policy.first_confidence_min
            if pass_index == 1
            else self.policy.final_confidence_min
        )
        record = normalize(
            raw, payload, threshold=threshold, tier=self.policy.acceptance_tier
        )
        if self._context_for(run.state, run.used).decision_context_hash != (
            context.decision_context_hash
        ):
            raise RuntimeFault(Reason.CONFLICT)
        # A first decision made over a narrowed offer carries the preclusion
        # that narrowed it, so its durable authority names the refused read.
        run.journal.returned(
            record, run.history, run.blocked if pass_index == 2 else run.precluded
        )
        return record

    async def _call(self, run: _Run, payload: object) -> RawSelection:
        pass_index = getattr(payload, "pass_index", 1)
        rejection = rejection_for(pass_index)
        started = self.clock.monotonic()
        try:
            async with asyncio.timeout(
                min(self.provider_seconds, max(0.0, self._remaining_seconds(run)))
            ):
                raw = await self.decider.select(payload)  # type: ignore[arg-type]
        except (TimeoutError, OSError, asyncio.CancelledError) as error:
            raise RuntimeFault(Reason.DECIDER_ERROR) from error
        # The whole-run deadline is the harder bound and is checked first, so a
        # run that is out of time stops for that reason rather than reporting a
        # provider failure it would also have had.
        self._remaining_ms(run)
        if self.clock.monotonic() - started >= self.provider_seconds:
            # A late answer is ignored for execution and never retried.
            raise RuntimeFault(Reason.DECIDER_ERROR)
        if not isinstance(raw, RawSelection):
            raise RuntimeFault(rejection)
        try:
            raw = RawSelection.model_validate(raw)
        except ValidationError as error:
            raise RuntimeFault(rejection) from error
        self.boundary.validate(raw, rejection)
        if raw.pass_index != pass_index:
            raise RuntimeFault(rejection)
        if raw.model_revision != self.policy.model_revision:
            raise RuntimeFault(rejection)
        # The application's own wall-clock measurement of this call, which is
        # the only one it ever reports as measured.
        return RawSelection.model_validate(
            raw.model_dump()
            | {
                "latency_ms": StageMeasurement(
                    availability="measured",
                    value=max(0.0, (self.clock.monotonic() - started) * 1000),
                )
            }
        )

    async def _execute(
        self,
        run: _Run,
        pass_index: Literal[1, 2],
        record: SelectionRecord,
        selected: str,
        capability: str,
    ) -> DecisionReceipt:
        context = run.context
        self._remaining_ms(run)
        if run.incident.diagnostic_reads >= self.max_diagnostic_reads:
            raise RuntimeFault(Reason.DIAGNOSTICS_EXHAUSTED)
        request = DiagnosticRequest(
            diagnostic_call_id=uuid4(),
            pass_index=pass_index,
            capability=capability,
            public_tool_id=selected,
            arguments_hash=run.request.arguments_hash,
        )
        # The read is counted before it runs, failures included.
        self._consume(run, "diagnostic_reads")
        started = self.clock.monotonic()
        try:
            async with asyncio.timeout(
                min(self.provider_seconds, max(0.0, self._remaining_seconds(run)))
            ):
                report = await run.executor.execute(request)
        except (TimeoutError, OSError, asyncio.CancelledError) as error:
            raise RuntimeFault(Reason.TOOL_ERROR) from error
        if self.clock.monotonic() - started >= self.provider_seconds:
            raise RuntimeFault(Reason.TOOL_ERROR)
        if not isinstance(report, DiagnosticReport):
            raise RuntimeFault(Reason.TOOL_ERROR)
        try:
            report = DiagnosticReport.model_validate(report)
        except ValidationError as error:
            raise RuntimeFault(Reason.CONFLICT) from error
        self.boundary.validate(report, Reason.CONFLICT)
        if (report.capability, report.public_tool_id, report.arguments_hash) != (
            capability,
            selected,
            request.arguments_hash,
        ):
            raise RuntimeFault(Reason.CONFLICT)
        if report.diagnostic_call_id not in {None, request.diagnostic_call_id}:
            raise RuntimeFault(Reason.CONFLICT)
        # Revalidate current identity before any authority changes.
        if self._context_for(run.state, run.used).decision_context_hash != (
            context.decision_context_hash
        ):
            raise RuntimeFault(Reason.CONFLICT)
        receipt = self.verifier.create(
            namespace=run.request.namespace,
            decision_id=record.decision_id,
            model_call_id=record.model_call_id,
            context=context,
            report=report,
        )
        stored = (
            self.repository.put(receipt, self.verifier.token)
            if self.policy.memory_authorized
            else False
        )
        run.journal.admitted(receipt, stored)
        run.reports += (report,)
        run.receipts += (receipt.receipt_id,)
        run.outcome = receipt.outcome_class
        return receipt

    def _duration(self, run: _Run) -> StageMeasurement:
        return StageMeasurement(
            availability="measured",
            value=max(0.0, (self.clock.monotonic() - run.started) * 1000),
        )

    def _base(self, run: _Run) -> dict[str, object]:
        return {
            "cascade_id": run.request.cascade_id,
            "baseline_id": self.policy.baseline_id,
            "history": run.history,
            "consumed": run.counters,
            "duration_ms": self._duration(run),
            "first": run.first,
            "final": run.final,
            "blocked": run.blocked,
            "precluded": run.precluded,
            "created_receipt_ids": run.receipts,
            "reports": run.reports,
            "outcome_class": run.outcome,
        }

    def _resolved(
        self,
        run: _Run,
        path: Literal["direct", "rechosen"],
        selected: str,
        capability: str,
    ) -> CascadeResult:
        return CascadeResult.model_validate(
            self._base(run)
            | {
                "path": path,
                "selected_tool_id": selected,
                "executed_capability": capability,
            }
        )

    def _unresolved(self, run: _Run, reason: Reason) -> CascadeResult:
        resolved = reason if reason in CASCADE_REASONS else Reason.INVARIANT
        return CascadeResult.model_validate(
            self._base(run) | {"path": "unresolved", "unresolved_reason": resolved}
        )
