"""The one controller that owns a run.

The controller holds the single writer lease, executes every tool, repairs
deterministically, and maps structured facts onto a terminal disposition. Tool
selection is never its own. There is exactly one selection algorithm, the
bounded Decision Receipt orchestrator, and every composition reaches it: the
controller hands it the complete current authoritative state and it performs
the history lookup, the fresh first decision, the exact-repetition check, and
the one optional second decision. A core selection engine composed in is
adapted onto that same orchestrator rather than given an authority path of its
own, so no composition can skip attempt accounting, receipt admission,
deterministic verification, or the second decision.

Execution and state projection stay here, so the orchestrator asks this
controller to run each selected capability and to project what is true after
it. Both travel as one per-invocation execution authority, so one orchestrator
can serve concurrent runs without any of them reaching another.

Telemetry is attached, not woven in. An optional sink receives a projection of
each durable event after the repository has accepted it, so the durable record
stays the only authority and nothing the run decides can depend on whether a
sink is present. With no sink the controller does no telemetry work at all.

Lease acquisition belongs to exactly one layer. `run` acquires for callers that
own the run outright; `resume` continues from a snapshot another layer already
leased and never acquires it a second time. Duplicate invocations are resolved
from the run store, which is the only durable idempotency authority: a finished
run returns its stored result and a live one is refused.
"""

from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread
from typing import Literal, cast
from uuid import UUID, uuid4

import duckdb
from pydantic import JsonValue, ValidationError

from unstuck.cascade.cards import canonical_capability, capability_cards
from unstuck.cascade.engine import CascadeEngine, CascadeRequest, CascadeResult
from unstuck.cascade.execution import DiagnosticRequest, StateProjector
from unstuck.cascade.identity import CallIdentity
from unstuck.cascade.integration import core_measurement, decision_history
from unstuck.cascade.matching import HistoryLookupResult, HistoryMatcher
from unstuck.cascade.policy import Arm, Calibration, CascadePolicy
from unstuck.cascade.projector import BlockedExecution, SelectorInput
from unstuck.cascade.receipts import (
    DecisionReceipt,
    EvidenceDelta,
    OutcomeClass,
    ReceiptNamespace,
)
from unstuck.cascade.repository import LocalReceiptRepository, ReceiptRepository
from unstuck.cascade.selection import (
    RawSelection,
    SelectionRecord,
    rejection_for,
)
from unstuck.cascade.serialization import StageMeasurement, canonical_digest
from unstuck.cascade.verifier import DiagnosticReport, ReceiptVerifier
from unstuck.clock import Clock, SystemClock
from unstuck.decision import (
    Decision,
    DecisionEngine,
    DecisionHistory,
    DecisionState,
    Probe,
    PublicEvidence,
    SelectionAuthority,
    default_catalog,
)
from unstuck.domain import (
    EvidenceRecord,
    Incident,
    PipelineFacts,
    QueryFacts,
    QuerySpec,
    Reason,
    ReportVerification,
    RuntimeFault,
    SchemaFacts,
    ScopeFacts,
    Settings,
    VerificationResult,
)
from unstuck.events import Measurement, PersistenceBoundary, Redactor
from unstuck.policy import compatible, consume, current_evidence, validate_repair
from unstuck.query import EvidenceEngine
from unstuck.storage import RunRepository, Snapshot
from unstuck.telemetry.collector import TelemetryRecorder, TelemetrySink
from unstuck.telemetry.contracts import (
    CorrelationContext,
    InfrastructureDimensions,
    execution_key,
)
from unstuck.verification import verify

#: The incident state each recorded event moves the incident into.
_STATES: dict[str, str] = {
    "incident_opened": "OBSERVE",
    "decision_history_requested": "DECISION_HISTORY",
    "decider_first_requested": "DECIDER_FIRST",
    "decider_final_requested": "DECIDER_FINAL",
    "repeat_blocked": "RECEIPT_CHECK",
    "direct_path_selected": "DIRECT_PATH",
    "probe_started": "RUN_PROBE",
    "wait_started": "WAIT",
    "repair_requested": "REPAIR",
    "verification_finished": "VERIFY",
}

#: The supported disposition a useful read opened. The reason names what the
#: read made possible, never a terminal outcome it cannot yet know; the run's
#: own terminal reason is recorded separately. A read that opened none of these
#: carries `INSUFFICIENT` instead, which is what makes it disposition-neutral
#: rather than useful, and only the two disposition-negative receipt classes
#: ever carry execution authority.
_OPENED: tuple[tuple[str, Reason], ...] = (
    ("wait_open", Reason.STILL_PENDING),
    ("empty_provable", Reason.EMPTY_INITIAL),
    ("repair_open", Reason.POPULATED_AFTER_REPAIR),
)


@dataclass(frozen=True)
class RunExecution:
    """One invocation's execution authority behind the orchestrator's seams.

    The authority is created for one cascade and handed to the orchestrator for
    that call only. There is no binding to replace and nothing to rebind, so a
    second concurrent run cannot redirect this one's reads or state, however
    many runs one orchestrator is serving.
    """

    controller: "Controller"

    async def execute(self, request: DiagnosticRequest) -> DiagnosticReport:
        return self.controller.execute_diagnostic(request)

    def project(self) -> DecisionState:
        """Project what is authoritative now, after everything already done."""
        return self.controller.decision_state()


@dataclass(frozen=True)
class CoreSelector:
    """Adapts a core selection engine onto the one bounded selector seam.

    The core engine answers about a `DecisionState`; the orchestrator asks
    about a bounded `SelectorInput`. This adapter carries one question across
    and normalizes one answer back. It holds no authority of its own: it
    narrows the authoritative state to the options the orchestrator currently
    offers, hands over the history the orchestrator looked up, and never
    chooses, scores, blocks, retries, or admits anything.
    """

    engine: DecisionEngine
    authority: StateProjector
    model_revision: str

    def _narrowed(self, request: SelectorInput) -> DecisionState:
        state = self.authority.project()
        offered = request.available_tool_ids
        candidates = tuple(probe for probe in state.candidates if probe.id in offered)
        if len(candidates) != len(offered):
            raise RuntimeFault(Reason.CONFLICT)
        return DecisionState.model_validate(
            state.model_dump()
            | {"candidates": candidates, "history": decision_history(request.history)}
        )

    async def select(self, request: SelectorInput) -> RawSelection:
        decision = await self.engine.select(self._narrowed(request))
        if not isinstance(decision, Decision):
            raise RuntimeFault(rejection_for(request.pass_index))
        try:
            decision = Decision.model_validate(decision)
        except ValidationError as error:
            raise RuntimeFault(rejection_for(request.pass_index)) from error
        return RawSelection(
            model_revision=decision.model_version,
            decision_id=decision.decision_id,
            model_call_id=uuid4(),
            pass_index=request.pass_index,
            candidate_ids=decision.candidate_ids,
            selected_id=decision.selected_id,
            probabilities=decision.probabilities,
            confidence=decision.confidence,
            validation_status=decision.validation_status,
            latency_ms=_stage(decision.latency_ms),
            cost=_stage(decision.cost),
            token_usage=_stage(decision.token_usage),
        )


def _stage(measurement: Measurement) -> StageMeasurement:
    """Widen a core measurement without inventing a value for an absent one."""
    return StageMeasurement(
        availability=measurement.availability, value=measurement.value
    )


def history_references(result: HistoryLookupResult) -> list[dict[str, JsonValue]]:
    """One structured durable entry per retrieved receipt, in retrieval order.

    Parallel arrays of identities, match classes and outcome classes can drift
    and cannot say which outcome belonged to which receipt. This records each
    receipt as one unit instead: its identity, how it was classified, the
    public tool and arguments it was about, the verifier's reason, whether the
    verifier judged it able to refuse a repetition, and the opaque binding the
    refusal would be about.

    Every value is the matcher's and the verifier's own. Nothing here decides
    eligibility; `ReceiptMatch.blocking` already did.
    """
    return [
        {
            "receipt_id": match.receipt_id,
            "match_class": match.match_class,
            "outcome_class": match.outcome_class,
            "public_tool_id": match.public_tool_id,
            "reason_code": match.reason_code.value,
            "blocking_eligible": match.blocking,
            "admitted_evidence": match.admitted_evidence,
            "arguments_hash": match.arguments_hash,
            "binding_hash": execution_key(match.public_tool_id, match.arguments_hash),
        }
        for match in result.matches
    ]


@dataclass(frozen=True)
class ControllerJournal:
    """Persists one cascade's durable record under the controller's lease."""

    controller: "Controller"

    def history(self, result: HistoryLookupResult, problem_fingerprint: str) -> None:
        self.controller.record_history(
            decision_history(result),
            problem_fingerprint,
            result.receipt_ids,
            references=history_references(result),
            retrieval_truncated=result.retrieval_truncated,
        )

    def requested(self, payload: SelectorInput) -> None:
        self.controller.record_request(payload)

    def returned(
        self,
        record: SelectionRecord,
        result: HistoryLookupResult,
        blocked: tuple[BlockedExecution, ...],
    ) -> None:
        self.controller.record_selection(record, result, blocked)

    def refused(self, execution: BlockedExecution) -> None:
        self.controller.record_refusal(execution)

    def precluded(self, execution: BlockedExecution) -> None:
        self.controller.record_refusal(execution, stage="pre_selection")

    def admitted(self, receipt: DecisionReceipt, stored: bool) -> None:
        self.controller.record_receipt(receipt, stored)


class Controller:
    def __init__(
        self,
        repository: RunRepository,
        engine: EvidenceEngine,
        decider: DecisionEngine | None = None,
        *,
        clock: Clock | None = None,
        catalog: tuple[Probe, ...] | None = None,
        settings: Settings | None = None,
        redactor: Redactor | None = None,
        initial_evidence: tuple[EvidenceRecord, ...] = (),
        cascade: CascadeEngine | None = None,
        receipts: ReceiptRepository | None = None,
        namespace: ReceiptNamespace | None = None,
        idempotency_key: str | None = None,
        telemetry: TelemetrySink | None = None,
        correlation: CorrelationContext | None = None,
        infrastructure: InfrastructureDimensions | None = None,
    ):
        self.repository = repository
        self.engine = engine
        self.clock = clock or SystemClock()
        self.engine.clock = self.clock
        self.catalog = default_catalog() if catalog is None else catalog
        self.settings = settings or Settings()
        self.redactor = redactor or Redactor()
        self.initial_evidence = initial_evidence
        self.history = DecisionHistory()
        self.owner = uuid4()
        self.snapshot: Snapshot | None = None
        self.started = 0.0
        self.active = False
        # Observation only, and off unless a sink is supplied. The recorder
        # reads durable events after they are written and answers nothing the
        # run reads back, so no selection, argument, repair, or disposition can
        # depend on whether telemetry is attached.
        self.telemetry = TelemetryRecorder(
            telemetry,
            context=correlation,
            infrastructure=infrastructure,
            redactor=self.redactor,
        )
        self._observed = 0
        self._retry_state: Literal["new", "resumed"] = "new"
        if (cascade is None) == (decider is None):
            # Exactly one composition, never two authorities and never none.
            raise RuntimeFault(Reason.INVARIANT)
        arm: Arm = self.settings.baseline_id
        self.namespace = namespace or ReceiptNamespace(arm=arm, case_id="local")
        if self.namespace.arm != arm:
            raise RuntimeFault(Reason.INVARIANT)
        self.idempotency_key = idempotency_key
        if cascade is not None:
            if receipts is not None or not idempotency_key:
                raise RuntimeFault(Reason.INVARIANT)
            self.cascade = cascade
            return
        if decider is None:
            raise RuntimeFault(Reason.INVARIANT)
        # One algorithm. A core engine is adapted onto it, never around it.
        store = LocalReceiptRepository() if receipts is None else receipts
        cards = capability_cards(self.catalog)
        policy = CascadePolicy(
            baseline_id=arm,
            model_revision=self.settings.model_revision,
            calibration=Calibration(
                first_confidence_min=self.settings.first_confidence_min,
                final_confidence_min=self.settings.final_confidence_min,
            ),
        )
        self.cascade = CascadeEngine(
            decider=CoreSelector(decider, RunExecution(self), policy.model_revision),
            repository=store,
            verifier=ReceiptVerifier(self.clock, cards=cards, redactor=self.redactor),
            clock=self.clock,
            policy=policy,
            cards=cards,
            matcher=HistoryMatcher(store),
            redactor=self.redactor,
            provider_seconds=self.settings.provider_seconds,
        )

    @property
    def current(self) -> Snapshot:
        if self.snapshot is None:
            raise RuntimeFault(Reason.INVARIANT)
        return self.snapshot

    def _observe(self) -> None:
        """Record every durable event written since the last observation.

        Telemetry follows the durable record rather than leading it, so an
        event is observed only once the repository has accepted it. Reading the
        whole unobserved tail keeps the timeline complete even for an event the
        repository wrote without the controller asking, such as the one
        acquiring the lease writes.

        The run's last durable event is also where telemetry's own completeness
        summary is persisted. Those counts live in memory, so without this a
        file reopened in a later process could not tell a whole record from one
        a transient sink failure punched a hole in.
        """
        if self.snapshot is None or not self.telemetry.active:
            return
        stage = self.snapshot.incident.state
        for event in self.snapshot.events:
            if event.sequence <= self._observed:
                continue
            self.telemetry.record(
                event,
                stage=stage,
                retry_state=(
                    self._retry_state if event.type == "run_started" else None
                ),
            )
            self._observed = event.sequence
            if event.type == "run_finished":
                self.telemetry.finalize()

    def begin(self, run_id: UUID) -> bool:
        snapshot = self.repository.acquire(
            run_id, self.owner, run_seconds=self.settings.run_seconds
        )
        if snapshot is None:
            return False
        self.snapshot = snapshot
        self.started = self.clock.monotonic()
        self._observe()
        return True

    def adopt(self, snapshot: Snapshot, owner: UUID) -> bool:
        """Take over a lease another layer already acquired.

        The controller never acquires a run it is handed. A snapshot that is
        not running, or that is held by a different writer, carries no writer
        authority here and is refused exactly as a lost acquisition is.
        """
        if not isinstance(snapshot, Snapshot):
            raise RuntimeFault(Reason.INVARIANT)
        adopted = Snapshot.model_validate(snapshot)
        if adopted.state.status != "running" or adopted.state.lease_owner != owner:
            return False
        self.owner = owner
        self.snapshot = adopted
        self.started = self.clock.monotonic()
        self._retry_state = "resumed"
        self._observe()
        return True

    def _remaining(self) -> float:
        return self.settings.run_seconds - (self.clock.monotonic() - self.started)

    def _deadline(self) -> None:
        if self._remaining() <= 0:
            raise RuntimeFault(Reason.DEADLINE)

    def _event(
        self,
        name: str,
        payload: dict[str, JsonValue],
        *,
        state: str | None = None,
        query: QuerySpec | None = None,
        incident: Incident | None = None,
        evidence: tuple[EvidenceRecord, ...] | None = None,
        selection: SelectionAuthority | None = None,
        history: DecisionHistory | None = None,
        verification: ReportVerification | None = None,
    ) -> None:
        resolved = state or _STATES.get(name)
        if resolved is not None:
            incident = Incident.model_validate(
                (incident or self.current.incident).model_dump() | {"state": resolved}
            )
        self.snapshot = self.repository.record(
            self.current,
            self.owner,
            name,
            payload,
            query=query,
            incident=incident,
            evidence=evidence,
            selection=selection,
            history=history,
            verification=verification,
        )
        self._observe()

    def _attempt(
        self,
        name: str,
        event: str,
        payload: dict[str, JsonValue],
        *,
        state: str | None = None,
        history: DecisionHistory | None = None,
    ) -> None:
        self._deadline()
        counters = consume(self.current.incident.counters, name)
        incident = Incident.model_validate(
            self.current.incident.model_dump() | {"counters": counters}
        )
        self._event(event, payload, state=state, incident=incident, history=history)

    def reject_report_arguments(self) -> dict[str, JsonValue]:
        self._attempt(
            "report_queries", "report_rejected", {"reason_code": "validation_error"}
        )
        return {"error": "validation_error"}

    def execute_report(self) -> EvidenceRecord:
        self._attempt("report_queries", "report_requested", {})
        query, task_hash = self.current.query, self.current.state.task_hash
        record = self._tool(
            lambda: self.engine.report(query, task_hash), "execute_report", "query"
        )
        self._event(
            "query_observed",
            {"evidence_id": str(record.evidence_id)},
            evidence=self.current.evidence + (record,),
        )
        return record

    def _tool(
        self, action: Callable[[], EvidenceRecord], producer: str, kind: str
    ) -> EvidenceRecord:
        self._deadline()
        started = self.clock.monotonic()
        timeout = min(self.settings.provider_seconds, self._remaining())
        responses: Queue[
            EvidenceRecord | RuntimeFault | ValidationError | duckdb.Error | OSError
        ] = Queue(maxsize=1)

        def execute() -> None:
            try:
                responses.put(action())
            except (RuntimeFault, ValidationError, duckdb.Error, OSError) as error:
                responses.put(error)

        Thread(target=execute, daemon=True, name="unstuck-local-read").start()
        try:
            response = responses.get(timeout=timeout)
        except Empty as error:
            self.engine.cancel()
            self._deadline()
            raise RuntimeFault(Reason.TOOL_ERROR) from error
        self._deadline()
        if self.clock.monotonic() - started >= self.settings.provider_seconds:
            self.engine.cancel()
            raise RuntimeFault(Reason.TOOL_ERROR)
        if isinstance(response, RuntimeFault):
            raise response
        if isinstance(response, (ValidationError, duckdb.Error, OSError)):
            raise RuntimeFault(Reason.TOOL_ERROR) from response
        if not isinstance(response, EvidenceRecord):
            raise RuntimeFault(Reason.TOOL_ERROR)
        try:
            response = EvidenceRecord.model_validate(response)
        except ValidationError as error:
            raise RuntimeFault(Reason.CONFLICT) from error
        if (
            response.tool_id != producer
            or response.kind != kind
            or not current_evidence(self.current.query, response, self.clock.utcnow())
            or isinstance(response.payload, QueryFacts)
            and response.payload.query_hash != self.current.query.query_hash
        ):
            raise RuntimeFault(Reason.CONFLICT)
        PersistenceBoundary(self.redactor).validate(response, Reason.CONFLICT)
        return response

    def _candidates(self) -> tuple[Probe, ...]:
        return tuple(
            probe
            for probe in self.catalog
            if probe.public.available
            and probe.public.id not in self.current.incident.used_probes
        )

    def _carried_history(self) -> DecisionHistory:
        """Validate the history this run carries before it reaches a selector.

        The orchestrator owns retrieval and writes the result back here, so an
        untyped or unvalidated history is a fail-closed refusal rather than
        something that could travel to a selector as if it were looked up.
        """
        if not isinstance(self.history, DecisionHistory):
            raise RuntimeFault(Reason.RECEIPT_INVALID)
        try:
            history = DecisionHistory.model_validate(self.history)
        except ValidationError as error:
            raise RuntimeFault(Reason.RECEIPT_INVALID) from error
        PersistenceBoundary(self.redactor).validate(history, Reason.RECEIPT_INVALID)
        self.history = history
        return history

    def decision_state(self) -> DecisionState:
        """Project the current authoritative state. The one state source.

        Every selector input and every decision context is derived from this
        projection, including the refreshed one the second decision reasons
        over, so the orchestrator never has to keep a state of its own.
        """
        candidates = self._candidates()
        if not candidates:
            raise RuntimeFault(Reason.NO_READ)
        return self._decision_state(candidates)

    def _decision_state(self, candidates: tuple[Probe, ...]) -> DecisionState:
        observations = []
        for record in self.current.evidence[-4:]:
            facts: PipelineFacts | SchemaFacts | ScopeFacts | dict[str, JsonValue]
            if isinstance(record.payload, QueryFacts):
                # The two established facts travel with the row count. They are
                # what the verifier already consults, they say what the report
                # proved rather than what is finally true, and without them an
                # empty result is indistinguishable from any other.
                facts = {
                    "row_count": len(record.payload.rows),
                    "query_hash": record.payload.query_hash,
                    "encoding_proven": record.payload.encoding_proven,
                    "source_complete": record.payload.source_complete,
                }
            else:
                facts = record.payload
            observations.append(
                PublicEvidence(
                    evidence_id=record.evidence_id,
                    kind=record.kind,
                    tool_id=record.tool_id,
                    dataset_version=record.dataset_version,
                    arguments_hash=record.arguments_hash,
                    observed_at=record.observed_at.isoformat(),
                    facts=facts,
                )
            )
        history = self._carried_history()
        state = DecisionState(
            task=self.current.state.task,
            query=self.current.query,
            task_hash=self.current.state.task_hash,
            query_hash=self.current.query.query_hash,
            dataset_version=self.current.state.dataset_version,
            candidates=tuple(probe.public for probe in candidates),
            budgets=self.current.incident.counters,
            remaining_ms=max(0, int(self._remaining() * 1000)),
            bounded_evidence=tuple(observations),
            history=history,
            truncated=len(self.current.evidence) > 4,
        )
        PersistenceBoundary(self.redactor).validate(state, Reason.FIRST_REJECTED)
        return state

    async def _select(self) -> None:
        """Hand the current state to the one bounded orchestrator.

        The orchestrator performs the bounded history lookup, the fresh first
        decision, the exact-repetition check, the refresh of authoritative
        state after a verified read, and at most one second decision. It calls
        back into this controller for every read and for every state
        projection, so execution, receipt admission, and the terminal
        disposition stay here.
        """
        state = self.decision_state()
        request = CascadeRequest(
            cascade_id=uuid4(),
            idempotency_key=self.idempotency_key or str(self.current.state.run_id),
            namespace=self.namespace,
            call=CallIdentity(
                run_id=self.current.state.run_id,
                request_id=uuid4(),
                session_id=self.current.incident.incident_id,
                trace_id=uuid4(),
                lease_owner=self.owner,
                observed_at=self.clock.utcnow(),
            ),
            state=state,
            used_capabilities=tuple(
                canonical_capability(identifier, self.cascade.cards)
                for identifier in self.current.incident.used_probes
            ),
            remaining_ms=max(0, int(self._remaining() * 1000)),
        )
        result = await self.cascade.resolve(
            request,
            executor=RunExecution(self),
            journal=ControllerJournal(self),
        )
        if not isinstance(result, CascadeResult):
            raise RuntimeFault(Reason.INVARIANT)
        if result.path == "unresolved":
            raise RuntimeFault(result.unresolved_reason or Reason.FIRST_REJECTED)

    def _disposition(self) -> dict[str, JsonValue]:
        """The supported dispositions the current admissible evidence opens.

        This is the deterministic projection the receipt delta is taken over.
        It consults exactly the gates the terminal assessment consults and has
        no side effect, so a read that admits facts which cannot move any gate
        is proven to have made no progress rather than judged to have failed.
        """
        schema, pipeline = self._facts()
        repair_open = False
        if schema is not None:
            proof = next(
                record
                for record in reversed(self.current.evidence)
                if isinstance(record.payload, SchemaFacts)
            )
            try:
                validate_repair(
                    self.current.query,
                    self.current.incident,
                    proof,
                    self.engine.fixture.dataset_version,
                    self.clock.utcnow(),
                )
                repair_open = True
            except RuntimeFault:
                repair_open = False
        return {
            "wait_open": pipeline is not None and pipeline.job_state == "pending",
            "empty_provable": schema is not None
            and compatible(self.current.query, schema)
            and schema.partition_complete,
            "repair_open": repair_open,
        }

    def _opened(self, facts: dict[str, JsonValue]) -> Reason:
        return next(
            (reason for name, reason in _OPENED if facts[name]), Reason.INSUFFICIENT
        )

    def _admissible(self) -> str:
        """Canonical identity of the admissible evidence this run holds.

        The projection is a set of observed facts, not a list of records:
        operational identity and observation time are excluded, so a read that
        re-observes facts the run already holds leaves this identity unchanged
        and is proven to have admitted nothing new.
        """
        return canonical_digest(
            sorted(
                {
                    canonical_digest(
                        {
                            "kind": record.kind,
                            "tool_id": record.tool_id,
                            "arguments_hash": record.arguments_hash,
                            "dataset_id": record.dataset_id,
                            "dataset_version": record.dataset_version,
                            "facts": record.payload.model_dump(mode="json"),
                        }
                    )
                    for record in self.current.evidence
                }
            )
        )

    def _report(
        self,
        request: DiagnosticRequest,
        status: Literal["evidence", "provider_error", "invalid_evidence"],
        evidence: tuple[str, str],
        disposition: tuple[str, str],
        evidence_ids: tuple[UUID, ...],
        reason: Reason,
    ) -> DiagnosticReport:
        """Report what was observed: two proven deltas and nothing else.

        The evidence delta names only the canonical facts this read added. The
        disposition delta states separately whether those facts moved a
        supported disposition, so a read that admitted a fact is never reported
        as an empty read and a read that moved nothing never claims to have
        admitted something.
        """
        report = DiagnosticReport(
            diagnostic_call_id=request.diagnostic_call_id,
            capability=request.capability,
            public_tool_id=request.public_tool_id,
            arguments_hash=request.arguments_hash,
            status=status,
            delta=EvidenceDelta(
                evidence_before_hash=evidence[0],
                evidence_after_hash=evidence[1],
                evidence_ids=evidence_ids,
                disposition_before_hash=disposition[0],
                disposition_after_hash=disposition[1],
            ),
            admissible=status == "evidence",
            reason_code=reason,
        )
        PersistenceBoundary(self.redactor).validate(report, Reason.CONFLICT)
        return report

    def execute_diagnostic(self, request: DiagnosticRequest) -> DiagnosticReport:
        """Execute one selected capability and report only observed facts.

        This is the controller's own execution authority. The orchestrator
        names a public tool and a canonical capability; nothing it supplies
        reaches the query, and nothing returned here carries a label the
        structured evidence does not prove.
        """
        request = DiagnosticRequest.model_validate(request)
        selected = next(
            (
                probe
                for probe in self.catalog
                if probe.public.id == request.public_tool_id
                and probe.capability == request.capability
            ),
            None,
        )
        if selected is None or request.arguments_hash != self.current.query.query_hash:
            raise RuntimeFault(Reason.CONFLICT)
        if request.pass_index == 1:
            self._event(
                "direct_path_selected",
                {"pass_index": 1, "tool_id": selected.public.id},
            )
        state = "RUN_PROBE" if request.pass_index == 1 else "RUN_PROBE_FINAL"
        # Which read this is, recorded explicitly rather than inferred from the
        # decision pass. A first decision refused as an exact repetition
        # consumes no read, so the second decision's first execution is read
        # one on pass two, and only the charged counter says so.
        read_index = self.current.incident.counters.diagnostic_reads + 1
        self._attempt(
            "diagnostic_reads",
            "probe_started",
            {
                "tool_id": selected.public.id,
                "pass_index": request.pass_index,
                "diagnostic_call_id": str(request.diagnostic_call_id),
                "read_index": read_index,
                "attempt_index": read_index,
            },
            state=state,
        )
        opened = canonical_digest(self._disposition())
        held = self._admissible()
        unchanged = ((held, held), (opened, opened))
        query = self.current.query
        try:
            record = self._tool(
                lambda: self.engine.diagnose(selected.capability, query),
                selected.capability,
                selected.capability.removeprefix("inspect_"),
            )
        except RuntimeFault as fault:
            if fault.reason is Reason.TOOL_ERROR:
                return self._report(
                    request, "provider_error", *unchanged, (), Reason.TOOL_ERROR
                )
            if fault.reason is Reason.CONFLICT:
                return self._report(
                    request, "invalid_evidence", *unchanged, (), Reason.CONFLICT
                )
            raise
        record = EvidenceRecord.model_validate(
            record.model_dump() | {"tool_id": selected.public.id}
        )
        incident = Incident.model_validate(
            self.current.incident.model_dump()
            | {"used_probes": self.current.incident.used_probes + (selected.public.id,)}
        )
        self._event(
            "probe_returned",
            {
                "tool_id": selected.public.id,
                "pass_index": request.pass_index,
                "evidence_id": str(record.evidence_id),
            },
            state=state,
            incident=incident,
            evidence=self.current.evidence + (record,),
        )
        admitted = self._admissible()
        evidence = (held, admitted)
        # Only a newly admitted canonical fact is named. A re-observation of
        # facts the run already holds leaves the canonical identity equal
        # however fresh the provider's record identity and observation time
        # are, so it names nothing and is proven to have admitted nothing.
        ids = (record.evidence_id,) if admitted != held else ()
        try:
            facts = self._disposition()
        except RuntimeFault as fault:
            if fault.reason is Reason.INSUFFICIENT:
                # The facts are admitted but open nothing the assessment uses.
                # That is still admitted evidence; the reason names what it
                # opened, and only the delta decides the outcome class.
                return self._report(
                    request,
                    "evidence",
                    evidence,
                    (opened, opened),
                    ids,
                    Reason.INSUFFICIENT,
                )
            if fault.reason is Reason.CONFLICT:
                # Contradicted evidence is not admissible evidence at all.
                return self._report(
                    request, "invalid_evidence", *unchanged, (), Reason.CONFLICT
                )
            raise
        moved = canonical_digest(facts)
        if moved == opened:
            # No gate moved. Both deltas are reported as observed and the
            # verifier derives the class from the pair: a duplicate observation
            # admitted nothing and is the strict no-progress class, while a
            # newly admitted fact that opened nothing is the
            # disposition-neutral class. The two are not the same fact and the
            # reported reason is the same either way, because what the read
            # opened is exactly nothing.
            return self._report(
                request,
                "evidence",
                evidence,
                (opened, opened),
                ids,
                Reason.INSUFFICIENT,
            )
        return self._report(
            request,
            "evidence",
            evidence,
            (opened, moved),
            ids,
            self._opened(facts),
        )

    def record_history(
        self,
        history: DecisionHistory,
        problem_fingerprint: str,
        receipt_ids: tuple[str, ...],
        *,
        references: list[dict[str, JsonValue]] | None = None,
        retrieval_truncated: bool = False,
    ) -> None:
        """Record one bounded lookup. An absent history is never a decision.

        `retrieval_truncated` is the store's own fact: its read budget stopped
        before the namespace was exhausted, so receipts were left unread. It is
        persisted on both history events rather than discarded, because the
        recorded status alone would not say why the history was unavailable.

        `references` is the matcher's per-receipt classification, persisted as
        one structured entry each so a multi-receipt lookup can be read back
        receipt by receipt rather than as parallel arrays that can drift.
        """
        self.history = DecisionHistory.model_validate(history)
        PersistenceBoundary(self.redactor).validate(
            self.history, Reason.RECEIPT_INVALID
        )
        retrieved = list(references or [])
        if len(retrieved) != len(self.history.references):
            raise RuntimeFault(Reason.RECEIPT_INVALID)
        self._attempt(
            "history_lookups",
            "decision_history_requested",
            {
                "history_status": self.history.status,
                "reference_count": len(self.history.references),
                "problem_fingerprint": problem_fingerprint,
                "history_retrieval_truncated": retrieval_truncated,
            },
            history=self.history,
        )
        classes: list[JsonValue] = [
            item.match_class for item in self.history.references
        ]
        outcomes: list[JsonValue] = [
            item.outcome_class for item in self.history.references
        ]
        entries: list[JsonValue] = list(retrieved)
        if [entry["match_class"] for entry in retrieved] != classes or [
            entry["outcome_class"] for entry in retrieved
        ] != outcomes:
            raise RuntimeFault(Reason.RECEIPT_INVALID)
        self._event(
            "decision_history_returned",
            {
                "history_status": self.history.status,
                "receipt_ids": list(receipt_ids),
                "reference_count": len(retrieved),
                # The structured list is the one a telemetry reader projects:
                # each receipt's own identity, classification, tool, reason and
                # binding together. The two arrays are the existing shape the
                # held-out evaluation reads, kept beside it and proven above to
                # agree with it element for element, so neither can drift.
                "match_classes": classes,
                "outcome_classes": outcomes,
                "history_references": entries,
                "truncated": self.history.truncated,
                "history_retrieval_truncated": retrieval_truncated,
            },
        )

    def record_request(self, payload: SelectorInput) -> None:
        """Charge and persist one selector attempt before the request leaves.

        Only identities and public identifiers are recorded. The payload's own
        bounded content stays with the selector; the private capability never
        reaches a persisted event.
        """
        name = (
            "decider_first_requested"
            if payload.pass_index == 1
            else "decider_final_requested"
        )
        recorded: dict[str, JsonValue] = {
            "pass_index": payload.pass_index,
            "state_hash": payload.input_hash,
            "candidate_hash": canonical_digest(list(payload.available_tool_ids)),
            "decision_context_hash": payload.decision_context_hash,
            "problem_fingerprint": payload.problem_fingerprint,
            "history_status": payload.history.status,
            "receipt_ids": list(payload.history.receipt_ids),
            "reference_count": len(payload.history.matches),
            # The same per-receipt classification the lookup recorded, plus
            # the decision identity and bound versions this call is made
            # against, so the history D1 saw is reconstructable from the
            # one event that also says what it was bound to.
            "history_references": [
                entry
                | {
                    "decision_context_hash": payload.decision_context_hash,
                    "versions_hash": payload.versions.versions_hash,
                }
                for entry in history_references(payload.history)
            ],
            "truncated": payload.history.truncated,
            "history_retrieval_truncated": payload.history.retrieval_truncated,
            "model_revision": payload.versions.model_revision,
            "catalog_version": payload.versions.catalog_version,
            "tool_contract_version": payload.versions.tool_contract_version,
            "data_schema_version": payload.versions.data_schema_version,
            "policy_hash": payload.versions.policy_hash,
            "calibration_hash": payload.versions.calibration_hash,
            "available_tool_ids": list(payload.available_tool_ids),
            "remaining_selector_calls": payload.budgets.remaining_selector_calls,
            "remaining_diagnostic_reads": (payload.budgets.remaining_diagnostic_reads),
        }
        if payload.precluded:
            # The narrowed offer is explicit, and only when it was narrowed, so
            # an unblocked request records exactly what it always did.
            recorded["precluded_tool_ids"] = [
                item.public_tool_id for item in payload.precluded
            ]
        self._attempt("decider_calls", name, recorded)

    def record_selection(
        self,
        record: SelectionRecord,
        result: HistoryLookupResult,
        blocked: tuple[BlockedExecution, ...],
    ) -> None:
        name = (
            "decider_first_returned"
            if record.pass_index == 1
            else "decider_final_returned"
        )
        authority = SelectionAuthority(
            decision_id=record.decision_id,
            selected_id=record.selected_id,
            confidence=record.confidence,
            model_version=record.model_revision,
            accepted=record.accepted,
            pass_index=record.pass_index,
            # Taken from the normalized record and nowhere else, so what the
            # run reports about who answered is what actually answered. It is
            # evidence about the call: `accepted` above was recomputed from
            # the offered options and the threshold before this was read.
            provider=record.provider,
            history_status=result.status,
            receipt_ids=result.receipt_ids,
            history_truncated=result.truncated,
            blocked_tool_id=blocked[0].public_tool_id if blocked else None,
            blocked_reason=blocked[0].reason_code if blocked else None,
        )
        # Telemetry observes the same normalized record the durable authority
        # does. It is told who answered rather than configured with it, so an
        # unattached or misconfigured deployment cannot make a record claim a
        # provider that never answered.
        self.telemetry.observed_provider(record.provider)
        self._event(
            name,
            {
                "pass_index": record.pass_index,
                "decision_id": str(record.decision_id),
                "model_call_id": str(record.model_call_id),
                "selected_id": record.selected_id,
                "confidence": record.confidence,
                "accepted": record.accepted,
                "validation_status": record.validation_status,
                "state_hash": record.selector_input_hash,
                "decision_context_hash": record.decision_context_hash,
                "reason_code": (
                    record.rejection.value if record.rejection is not None else None
                ),
                "model_version": record.model_revision,
                # Which provider answered, recorded beside the revision it
                # answered under. Evidence about the call, never authority.
                "provider": record.provider,
                "candidate_ids": list(record.candidate_ids),
                # The reported distribution, recorded as reported. It is
                # evidence about the call, never an authority: acceptance was
                # already recomputed from the offered options above.
                "probabilities": (
                    [item.model_dump(mode="json") for item in record.probabilities]
                    if record.probabilities is not None
                    else None
                ),
                "measured_latency_ms": core_measurement(record.latency_ms).model_dump(
                    mode="json"
                ),
                "measured_cost": core_measurement(record.cost).model_dump(mode="json"),
                "token_usage": core_measurement(record.token_usage).model_dump(
                    mode="json"
                ),
            },
            selection=authority,
        )

    def record_refusal(
        self,
        execution: BlockedExecution,
        *,
        stage: Literal["pre_selection"] | None = None,
    ) -> None:
        """Record a refused repetition. A failed read is its own receipt.

        A `pre_selection` refusal was precluded from the offer before any
        selector request, and says so; nothing else about the record differs.
        """
        if execution.blocked_class != "exact_repeat":
            return
        payload: dict[str, JsonValue] = {
            "public_tool_id": execution.public_tool_id,
            "arguments_hash": execution.arguments_hash,
            "receipt_id": execution.receipt_id,
            "outcome_class": execution.outcome_class,
            "reason_code": execution.reason_code.value,
            "blocked_class": execution.blocked_class,
        }
        if stage is not None:
            payload["stage"] = stage
        self._event("repeat_blocked", payload)

    def record_receipt(self, receipt: DecisionReceipt, stored: bool) -> None:
        outcome: OutcomeClass = receipt.outcome_class
        self._event(
            "decision_receipt_written",
            {
                "receipt_id": receipt.receipt_id,
                "decision_id": str(receipt.decision_id),
                "model_call_id": str(receipt.model_call_id),
                "diagnostic_call_id": (
                    str(receipt.diagnostic_call_id)
                    if receipt.diagnostic_call_id is not None
                    else None
                ),
                "problem_fingerprint": receipt.problem_fingerprint,
                "decision_context_hash": receipt.decision_context_hash,
                # The isolation boundary and the bound versions travel as
                # hashes. A reader can tell two receipts apart, and compare one
                # against the context it was written under, without the arm,
                # the case, or any bound version being readable here.
                "namespace_hash": canonical_digest(receipt.namespace),
                "versions_hash": receipt.versions.versions_hash,
                "public_tool_id": receipt.public_tool_id,
                "arguments_hash": receipt.arguments_hash,
                "outcome_class": outcome,
                "reason_code": receipt.reason_code.value,
                "evidence_before_hash": receipt.delta.evidence_before_hash,
                "evidence_after_hash": receipt.delta.evidence_after_hash,
                "evidence_ids": [
                    str(identifier) for identifier in receipt.delta.evidence_ids
                ],
                "disposition_before_hash": receipt.delta.disposition_before_hash,
                "disposition_after_hash": receipt.delta.disposition_after_hash,
                "admitted_evidence": receipt.delta.changed,
                "blocking": receipt.blocking,
                "stored": stored,
            },
        )

    def _verify(self, report: EvidenceRecord, repaired: bool = False) -> None:
        supporting = tuple(
            record
            for record in self.current.evidence
            if isinstance(record.payload, SchemaFacts)
        )
        result = verify(self.current.query, report, supporting, now=self.clock.utcnow())
        if not isinstance(result, VerificationResult):
            raise RuntimeFault(Reason.REPAIR_FAILED if repaired else Reason.CONFLICT)
        try:
            result = VerificationResult.model_validate(result)
        except ValidationError as error:
            raise RuntimeFault(
                Reason.REPAIR_FAILED if repaired else Reason.CONFLICT
            ) from error
        PersistenceBoundary(self.redactor).validate(
            result, Reason.REPAIR_FAILED if repaired else Reason.CONFLICT
        )
        self._event(
            "verification_finished",
            result.model_dump(mode="json"),
            verification=ReportVerification(
                report_id=report.evidence_id,
                dataset_id=report.dataset_id,
                dataset_version=report.dataset_version,
                query_hash=self.current.query.query_hash,
                result=result,
            ),
        )
        if result.status != "passed":
            raise RuntimeFault(Reason.REPAIR_FAILED if repaired else Reason.CONFLICT)

    def _facts(self) -> tuple[SchemaFacts | None, PipelineFacts | None]:
        schema = None
        pipeline = None
        for record in self.current.evidence:
            if isinstance(record.payload, QueryFacts):
                continue
            if not current_evidence(
                self.current.query, record, self.clock.utcnow(), allow_base=True
            ):
                raise RuntimeFault(Reason.CONFLICT)
            if isinstance(record.payload, SchemaFacts):
                if schema is not None and schema != record.payload:
                    raise RuntimeFault(Reason.CONFLICT)
                schema = record.payload
            elif isinstance(record.payload, PipelineFacts):
                if (
                    pipeline is not None
                    and pipeline != record.payload
                    and record.kind != "wait"
                ):
                    raise RuntimeFault(Reason.CONFLICT)
                pipeline = record.payload
            elif isinstance(record.payload, ScopeFacts):
                if (
                    not record.payload.query_matches_task_scope
                    or not record.payload.scope_covered
                ):
                    raise RuntimeFault(Reason.CONFLICT)
                if not record.payload.metadata_complete:
                    raise RuntimeFault(Reason.INSUFFICIENT)
        if (
            schema is not None
            and pipeline is not None
            and schema.partition_complete
            != (pipeline.job_state == "complete" and pipeline.scope_complete)
        ):
            raise RuntimeFault(Reason.CONFLICT)
        return schema, pipeline

    async def _assess(self, report: EvidenceRecord) -> Reason:
        schema, pipeline = self._facts()
        if pipeline is not None and pipeline.job_state == "pending":
            self._attempt("wait_checks", "wait_started", {})
            await self.clock.sleep(
                min(self.settings.wait_seconds, max(0, self._remaining()))
            )
            self._deadline()
            query = self.current.query
            elapsed = self.clock.monotonic() - self.started
            observation = self._tool(
                lambda: self.engine.recheck(query, elapsed),
                "wait_for_partition",
                "wait",
            )
            try:
                observation = EvidenceRecord.model_validate(observation.model_dump())
            except ValidationError as error:
                raise RuntimeFault(Reason.CONFLICT) from error
            if observation.kind != "wait" or not current_evidence(
                query, observation, self.clock.utcnow()
            ):
                raise RuntimeFault(Reason.CONFLICT)
            self._event(
                "wait_finished",
                {"evidence_id": str(observation.evidence_id)},
                evidence=self.current.evidence + (observation,),
            )
            assert isinstance(observation.payload, PipelineFacts)
            if observation.payload.job_state == "pending":
                return Reason.STILL_PENDING
            if (
                observation.payload.job_state != "complete"
                or not observation.payload.scope_complete
            ):
                return Reason.INSUFFICIENT
            report = self.execute_report()
            self._verify(report)
            assert isinstance(report.payload, QueryFacts)
            if report.payload.rows:
                return Reason.POPULATED_AFTER_WAIT
            if schema is not None and compatible(self.current.query, schema):
                return Reason.EMPTY_AFTER_WAIT
            return Reason.INSUFFICIENT
        if (
            schema is not None
            and compatible(self.current.query, schema)
            and schema.partition_complete
        ):
            return Reason.EMPTY_INITIAL
        if schema is not None:
            proof = next(
                record
                for record in reversed(self.current.evidence)
                if isinstance(record.payload, SchemaFacts)
            )
            repaired_query = validate_repair(
                self.current.query,
                self.current.incident,
                proof,
                self.engine.fixture.dataset_version,
                self.clock.utcnow(),
            )
            self._attempt("repair_attempts", "repair_requested", {})
            self._event(
                "repair_applied",
                {
                    "base_query_hash": self.current.query.base_query_hash,
                    "join_key_mode": repaired_query.join_key_mode,
                },
                query=repaired_query,
            )
            report = self.execute_report()
            self._verify(report, repaired=True)
            assert isinstance(report.payload, QueryFacts)
            if report.payload.rows:
                return Reason.POPULATED_AFTER_REPAIR
            if schema.partition_complete and compatible(self.current.query, schema):
                return Reason.EMPTY_AFTER_REPAIR
        return Reason.INSUFFICIENT

    def _finish(self, reason: Reason) -> dict[str, JsonValue]:
        if reason in {
            Reason.POPULATED_INITIAL,
            Reason.POPULATED_AFTER_WAIT,
            Reason.POPULATED_AFTER_REPAIR,
            Reason.EMPTY_INITIAL,
            Reason.EMPTY_AFTER_WAIT,
            Reason.EMPTY_AFTER_REPAIR,
        }:
            self._deadline()
            report = self.current.verified_report()
            if (
                report is None
                or not report.encoding_proven
                or not report.source_complete
            ):
                raise RuntimeFault(Reason.CONFLICT)
        if reason in {
            Reason.FIRST_REJECTED,
            Reason.FINAL_REJECTED,
            Reason.DECIDER_ERROR,
            Reason.NO_READ,
            Reason.REPEAT_BLOCKED,
        }:
            self._event("selection_unresolved", {"reason_code": reason.value})
        if reason in {
            Reason.DEADLINE,
            Reason.REPORTS_EXHAUSTED,
            Reason.TURNS_EXHAUSTED,
        }:
            self._event("budget_stop", {"reason_code": reason.value})
        details: dict[str, JsonValue] = {
            "baseline_id": self.settings.baseline_id,
            "duration_ms": Measurement(
                availability="measured",
                value=(self.clock.monotonic() - self.started) * 1000,
            ).model_dump(mode="json"),
        }
        self.snapshot = self.repository.finish(
            self.current, self.owner, reason, details
        )
        self._observe()
        return cast(dict[str, JsonValue], self.current.result)

    async def run(self, run_id: UUID) -> dict[str, JsonValue]:
        """Acquire this run and execute it. The caller owns no lease yet."""
        return await self._guarded(run_id, lambda: self.begin(run_id))

    async def resume(self, snapshot: Snapshot, owner: UUID) -> dict[str, JsonValue]:
        """Execute a run whose lease another layer already acquired.

        Exactly one layer acquires a run. A caller that admitted and leased the
        run itself continues through here, so the controller never contends
        with the lease it was handed.
        """
        return await self._guarded(
            snapshot.state.run_id, lambda: self.adopt(snapshot, owner)
        )

    def _duplicate(self, run_id: UUID) -> dict[str, JsonValue]:
        """Answer a duplicate invocation from the durable run record.

        The run store is the only idempotency authority. A run that already
        reached a terminal result answers with that stored result, so a
        repeated invocation never decides or reads again. A run still held by a
        live writer is refused: this invocation neither acquires nor executes.
        """
        try:
            existing = self.repository.load_run(run_id)
        except RuntimeFault:
            return {"run_id": str(run_id), "status": "lease_rejected"}
        if existing.result is None:
            return {"run_id": str(run_id), "status": "lease_rejected"}
        return existing.result

    async def _guarded(
        self, run_id: UUID, admit: Callable[[], bool]
    ) -> dict[str, JsonValue]:
        if self.active:
            # This controller's writer authority is already committed to a run.
            return {"run_id": str(run_id), "status": "lease_rejected"}
        self.active = True
        try:
            return await self._execute(run_id, admit)
        finally:
            self.active = False

    async def _execute(
        self, run_id: UUID, admit: Callable[[], bool]
    ) -> dict[str, JsonValue]:
        try:
            if not admit():
                return self._duplicate(run_id)
            self._attempt("agent_turns", "agent_turn_started", {})
            report = self.execute_report()
            self._verify(report)
            assert isinstance(report.payload, QueryFacts)
            if report.payload.rows:
                return self._finish(Reason.POPULATED_INITIAL)
            self._event(
                "incident_opened", {"fingerprint": self.current.incident.fingerprint}
            )
            if self.initial_evidence:
                producers = {
                    "pipeline": "inspect_pipeline",
                    "schema_keys": "inspect_schema_keys",
                    "scope": "inspect_scope",
                }
                for record in self.initial_evidence:
                    if not isinstance(record, EvidenceRecord):
                        raise RuntimeFault(Reason.CONFLICT)
                    try:
                        record = EvidenceRecord.model_validate(record)
                    except ValidationError as error:
                        raise RuntimeFault(Reason.CONFLICT) from error
                    PersistenceBoundary(self.redactor).validate(record, Reason.CONFLICT)
                    if producers.get(
                        record.kind
                    ) != record.tool_id or not current_evidence(
                        self.current.query, record, self.clock.utcnow()
                    ):
                        raise RuntimeFault(Reason.CONFLICT)
                self._event(
                    "public_evidence_observed",
                    {},
                    evidence=self.current.evidence + self.initial_evidence,
                )
                schema, pipeline = self._facts()
                if (
                    schema is not None
                    and compatible(self.current.query, schema)
                    and schema.partition_complete
                ):
                    return self._finish(Reason.EMPTY_INITIAL)
            await self._select()
            return self._finish(await self._assess(report))
        except ValidationError:
            reason = Reason.FIRST_REJECTED
        except duckdb.Error:
            reason = Reason.TOOL_ERROR
        except RuntimeFault as error:
            reason = error.reason
        if reason != Reason.STORAGE_ERROR and self.snapshot is not None:
            try:
                return self._finish(reason)
            except RuntimeFault as finalization_error:
                if finalization_error.reason != Reason.STORAGE_ERROR:
                    raise
                reason = finalization_error.reason
        return {
            "run_id": str(run_id),
            "status": "failed",
            "reason_code": reason.value,
            "mode": "offline",
            # Who answered, if anyone did, read from whatever selection this
            # run managed to record before the store became unavailable. No
            # selection means no provider answered, which is the default.
            "provider": (
                "mock" if self.snapshot is None else self.snapshot.observed_provider
            ),
            "persistence": "unavailable",
        }
