import fcntl
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import Field, JsonValue, ValidationError, model_validator

from unstuck.clock import Clock, SystemClock
from unstuck.decision import DecisionHistory, SelectionAuthority
from unstuck.domain import (
    Contract,
    EvidenceRecord,
    Incident,
    Provider,
    QueryFacts,
    QuerySpec,
    Reason,
    ReportVerification,
    RunState,
    RuntimeFault,
    Settings,
    TaskSpec,
    canonical_hash,
    terminal_mapping,
)
from unstuck.events import (
    DisplayOnly,
    EventRecord,
    Measurement,
    PersistedAs,
    PersistenceBoundary,
    Redactor,
    TerminalResult,
    project_events,
)


class Snapshot(Contract):
    """One run's durable authority.

    `selection` is the latest selection record and `first_selection` is the
    first one. They are separate because the causal sequence is part of the
    result: a second decision never overwrites what the first one chose, so an
    exported run can always be read back as first choice then final choice.
    """

    authority_version: Literal["2"]
    selection: SelectionAuthority | None
    first_selection: SelectionAuthority | None = None
    history: DecisionHistory | None = None
    verification: ReportVerification | None
    state: RunState
    query: QuerySpec
    incident: Incident
    evidence: Annotated[tuple[EvidenceRecord, ...], Field(max_length=16)] = ()
    events: Annotated[tuple[EventRecord, ...], Field(max_length=100)] = ()
    artifacts: Annotated[
        dict[str, Annotated[JsonValue, DisplayOnly()]], Field(max_length=16)
    ] = Field(default_factory=dict)
    result: Annotated[dict[str, JsonValue] | None, PersistedAs(TerminalResult)] = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (
            self.query.task_hash != self.state.task_hash
            or self.incident.task_hash != self.state.task_hash
        ):
            raise ValueError("task_mismatch")
        if (
            self.incident.run_id != self.state.run_id
            or self.query.dataset_version != self.state.dataset_version
        ):
            raise ValueError("snapshot identity mismatch")
        if [event.sequence for event in self.events] != list(
            range(1, len(self.events) + 1)
        ):
            raise ValueError("invalid event sequence")
        if any(event.run_id != self.state.run_id for event in self.events):
            raise ValueError("event identity mismatch")
        if any(event.incident_id != self.incident.incident_id for event in self.events):
            raise ValueError("event incident mismatch")
        if (
            self.incident.base_query_hash != self.query.base_query_hash
            or self.incident.fingerprint
            != Incident.open(self.state.run_id, self.query).fingerprint
        ):
            raise ValueError("incident identity mismatch")
        if len({record.evidence_id for record in self.evidence}) != len(self.evidence):
            raise ValueError("duplicate evidence identity")
        if (self.history is None) != (self.incident.counters.history_lookups == 0):
            raise ValueError("history authority does not match recorded lookups")
        if (self.first_selection is None) != (self.selection is None):
            raise ValueError("a recorded selection requires its first selection")
        if self.first_selection is not None and self.first_selection.pass_index != 1:
            raise ValueError("the first selection is the first pass")
        supplied = self.history or DecisionHistory()
        for record in (self.first_selection, self.selection):
            if record is None:
                continue
            if (
                record.history_status,
                record.receipt_ids,
                record.history_truncated,
            ) != (supplied.status, supplied.receipt_ids, supplied.truncated):
                raise ValueError("selection history binding mismatch")
        binding = self.verification
        if binding is not None:
            identifiers = binding.result.evidence_ids
            if (
                len(set(identifiers)) != len(identifiers)
                or not set(identifiers)
                <= {record.evidence_id for record in self.evidence}
                or binding.result.status == "passed"
                and (not identifiers or identifiers[0] != binding.report_id)
            ):
                raise ValueError("verification evidence mismatch")
            report = next(
                (
                    record
                    for record in self.evidence
                    if record.evidence_id == binding.report_id
                ),
                None,
            )
            if (
                report is None
                or not isinstance(report.payload, QueryFacts)
                or report.kind != "query"
                or report.tool_id != "execute_report"
                or binding.query_hash != self.query.query_hash
                or report.arguments_hash != binding.query_hash
                or report.payload.query_hash != binding.query_hash
                or report.dataset_id != binding.dataset_id
                or binding.dataset_id != self.state.task.dataset_id
                or report.dataset_version != binding.dataset_version
                or binding.dataset_version != self.state.dataset_version
                or (binding.result.status == "passed")
                != (binding.result.reason_code == "verified")
            ):
                raise ValueError("verification binding mismatch")
        terminal_states = {"DONE", "PENDING", "UNRESOLVED", "BUDGET_STOP", "FAILED"}
        is_terminal = self.state.status not in {"created", "running"}
        finals = [event for event in self.events if event.type == "run_finished"]
        if is_terminal:
            if (
                self.state.reason_code is None
                or len(finals) != 1
                or self.events[-1] != finals[0]
                or self.result is None
            ):
                raise ValueError("incomplete terminal snapshot")
            incident_state, _, status = terminal_mapping(self.state.reason_code)
            if self.incident.state != incident_state or self.state.status != status:
                raise ValueError("terminal state mismatch")
            result = TerminalResult.model_validate_json(json.dumps(self.result))
            if (
                result.run_id != self.state.run_id
                or result.reason_code != self.state.reason_code
            ):
                raise ValueError("terminal identity mismatch")
            for key, value in self.terminal_fields().items():
                if getattr(result, key) != value:
                    raise ValueError("terminal authority mismatch")
            if status in {"completed", "empty_verified"}:
                verified = self.verified_report()
                if (
                    verified is None
                    or not verified.encoding_proven
                    or not verified.source_complete
                    or (status == "completed") != bool(verified.rows)
                ):
                    raise ValueError(
                        "successful terminal requires complete verified report"
                    )
        elif (
            self.state.reason_code is not None
            or self.result is not None
            or finals
            or self.incident.state in terminal_states
        ):
            raise ValueError("nonterminal snapshot mismatch")
        if project_events(self.events) != self.result:
            raise ValueError("projection mismatch")
        return self

    def verified_report(self) -> QueryFacts | None:
        binding = self.verification
        return next(
            (
                record.payload
                for record in self.evidence
                if binding
                and binding.result.status == "passed"
                and record.evidence_id == binding.report_id
                and isinstance(record.payload, QueryFacts)
            ),
            None,
        )

    @property
    def observed_provider(self) -> Provider:
        """Which provider answered this run, read from its own selections.

        The value comes from the normalized selection authority and nowhere
        else: not from a provider mode, a deployment response label, or a
        constructor argument. A run that has recorded no selection has had no
        provider answer it, so it keeps the `mock` default.

        `recorded_selections` refuses a selection whose provider contradicts
        the first one on file, so the first and latest records always agree
        and reading either one gives the same answer.
        """
        recorded = self.selection or self.first_selection
        return "mock" if recorded is None else recorded.provider

    def terminal_fields(self) -> dict[str, object]:
        first = self.first_selection or self.selection
        final = self.selection
        history = self.history or DecisionHistory()
        report = self.verified_report()
        counters = self.incident.counters
        return {
            "rows": list(report.rows) if report else [],
            "evidence_ids": [record.evidence_id for record in self.evidence],
            "dataset_version": self.state.dataset_version,
            "task_hash": self.state.task_hash,
            "base_query_hash": self.query.base_query_hash,
            "query_hash": self.query.query_hash,
            "diagnostic_reads": counters.diagnostic_reads,
            "wait_checks": counters.wait_checks,
            "repair_attempts": counters.repair_attempts,
            "history_status": history.status,
            "receipt_ids": list(history.receipt_ids),
            "history_truncated": history.truncated,
            # Observational only, and derived from the selection authority
            # rather than from any configuration. It names who answered and
            # authorizes nothing: every field below it was decided without it.
            "provider": self.observed_provider,
            "pass_count": counters.decider_calls,
            "selection_pass": final.pass_index if final else None,
            "blocked_tool_id": final.blocked_tool_id if final else None,
            "blocked_reason_code": final.blocked_reason if final else None,
            # The first choice is D1's own; the final choice is the accepted
            # record that executed, which is D2's when a second decision ran.
            "first_choice": first.selected_id if first else None,
            "final_choice": final.selected_id if final and final.accepted else None,
            "first_confidence": first.confidence if first else None,
            "final_confidence": final.confidence if final and final.accepted else None,
            "confidence_available": first is not None and first.confidence is not None,
            "model_version": final.model_version if final else None,
            "verification_status": self.verification.result.status
            if self.verification
            else "not_run",
            "report_evidence_id": self.verification.report_id
            if self.verification
            else None,
            "verification_id": self.verification.result.verification_id
            if self.verification
            else None,
            "artifact_refs": ["events.jsonl", *self.artifacts],
        }


def recorded_selections(
    snapshot: Snapshot, selection: SelectionAuthority | None
) -> dict[str, SelectionAuthority]:
    """Route one new selection record, keeping the first one immutable.

    Every repository writes through this, so the first record is preserved
    wherever a run is stored: a second decision becomes the latest record and
    can never replace what the first decision chose, and a second pass with no
    first record on file is refused rather than written.

    One run has one provider. A selection whose provider contradicts the first
    record on file is refused rather than written, so the provider identity a
    run reports cannot change underneath its own terminal result, events, and
    telemetry. This is a coherence check on an observational field: it never
    decides, accepts, repairs, or relabels anything.
    """
    if selection is None:
        return {}
    if (
        snapshot.first_selection is not None
        and selection.provider != snapshot.first_selection.provider
    ):
        raise RuntimeFault(Reason.INVARIANT)
    if selection.pass_index == 1:
        return {"selection": selection, "first_selection": selection}
    if snapshot.first_selection is None:
        raise RuntimeFault(Reason.INVARIANT)
    return {"selection": selection}


class RunRepository(Protocol):
    def create_run(
        self, task: TaskSpec, dataset_version: str, idempotency_key: str
    ) -> Snapshot: ...
    def load_run(self, run_id: UUID) -> Snapshot: ...
    def acquire(
        self, run_id: UUID, owner: UUID, *, run_seconds: float = 120.0
    ) -> Snapshot | None: ...
    def record(
        self,
        snapshot: Snapshot,
        owner: UUID,
        event_type: str,
        payload: dict[str, JsonValue],
        *,
        query: QuerySpec | None = None,
        incident: Incident | None = None,
        evidence: tuple[EvidenceRecord, ...] | None = None,
        selection: SelectionAuthority | None = None,
        history: DecisionHistory | None = None,
        verification: ReportVerification | None = None,
    ) -> Snapshot: ...
    def finish(
        self,
        snapshot: Snapshot,
        owner: UUID,
        reason: Reason,
        payload: dict[str, JsonValue],
    ) -> Snapshot: ...


class LocalRunRepository:
    def __init__(
        self,
        root: Path,
        *,
        clock: Clock | None = None,
        redactor: Redactor | None = None,
    ):
        self.root = root.absolute()
        self.clock = clock or SystemClock()
        self.redactor = redactor or Redactor()
        self._active_directory: ContextVar[tuple[Path, int] | None] = ContextVar(
            "directory", default=None
        )
        for parent in (self.root, *self.root.parents):
            if parent.is_symlink():
                raise RuntimeFault(Reason.STORAGE_ERROR)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._open_root() as descriptor:
                identity = os.fstat(descriptor)
                self._root_identity = (identity.st_dev, identity.st_ino)
        except OSError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error

    @contextmanager
    def _open_root(self) -> Iterator[int]:
        descriptor = os.open(
            self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            for component in self.root.parts[1:]:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def _directory_descriptor(self, path: Path) -> int:
        active = self._active_directory.get()
        if active is None or active[0] != path:
            raise RuntimeFault(Reason.STORAGE_ERROR)
        return active[1]

    @contextmanager
    def _locked(self, run_id: UUID) -> Iterator[Path]:
        try:
            with self._open_root() as root_descriptor:
                identity = os.fstat(root_descriptor)
                if (identity.st_dev, identity.st_ino) != self._root_identity:
                    raise RuntimeFault(Reason.STORAGE_ERROR)
                name = str(UUID(str(run_id)))
                try:
                    os.mkdir(name, mode=0o700, dir_fd=root_descriptor)
                except FileExistsError:
                    pass
                directory = self.root / name
                directory_descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_descriptor,
                )
                try:
                    descriptor = os.open(
                        "writer.lock",
                        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_descriptor,
                    )
                    with os.fdopen(descriptor, "a") as handle:
                        fcntl.flock(handle, fcntl.LOCK_EX)
                        token = self._active_directory.set(
                            (directory, directory_descriptor)
                        )
                        try:
                            yield directory
                        finally:
                            self._active_directory.reset(token)
                            fcntl.flock(handle, fcntl.LOCK_UN)
                finally:
                    os.close(directory_descriptor)
        except OSError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error

    def _load(self, directory: Path) -> Snapshot:
        try:
            descriptor = os.open(
                "snapshot.json",
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=self._directory_descriptor(directory),
            )
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                snapshot = Snapshot.model_validate_json(handle.read())
            PersistenceBoundary(self.redactor).validate(snapshot, Reason.STORAGE_ERROR)
            return snapshot
        except (OSError, UnicodeError, ValidationError) as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error

    def _persist(self, directory: Path, snapshot: Snapshot) -> None:
        snapshot = Snapshot.model_validate(snapshot)
        PersistenceBoundary(self.redactor).validate(snapshot)
        text = snapshot.model_dump_json()
        if snapshot.state.status in {"completed", "empty_verified"} and (
            snapshot.state.run_deadline_at is None
            or self.clock.monotonic() >= snapshot.state.run_deadline_at
        ):
            raise RuntimeFault(Reason.DEADLINE)
        self._atomic(directory / "snapshot.json", text)

    def _atomic(self, path: Path, text: str) -> None:
        directory_descriptor = self._directory_descriptor(path.parent)
        temporary: str | None = None
        try:
            try:
                target = os.stat(
                    path.name, dir_fd=directory_descriptor, follow_symlinks=False
                )
                if not stat.S_ISREG(target.st_mode):
                    raise RuntimeFault(Reason.STORAGE_ERROR)
            except FileNotFoundError:
                pass
            temporary = ".pending-" + str(uuid4())
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_descriptor,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary,
                path.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            os.fsync(directory_descriptor)
        except (OSError, UnicodeError) as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise RuntimeFault(Reason.STORAGE_ERROR) from error

    def create_run(
        self, task: TaskSpec, dataset_version: str, idempotency_key: str
    ) -> Snapshot:
        PersistenceBoundary(self.redactor).validate(
            (task, dataset_version), Reason.TASK_MISMATCH
        )
        if not idempotency_key or len(idempotency_key) > 128:
            raise RuntimeFault(Reason.INVARIANT)
        run_id = uuid5(NAMESPACE_URL, canonical_hash(idempotency_key))
        with self._locked(run_id) as directory:
            entries = os.listdir(self._directory_descriptor(directory))
            if "snapshot.json" in entries:
                existing = self._load(directory)
                if (
                    existing.state.task_hash != task.task_hash
                    or existing.state.dataset_version != dataset_version
                ):
                    raise RuntimeFault(Reason.TASK_MISMATCH)
                return existing
            if any(name.startswith(".pending-") for name in entries):
                raise RuntimeFault(Reason.STORAGE_ERROR)
            query = QuerySpec(task=task, dataset_version=dataset_version)
            snapshot = Snapshot(
                authority_version="2",
                selection=None,
                verification=None,
                state=RunState(
                    run_id=run_id, task=task, dataset_version=dataset_version
                ),
                query=query,
                incident=Incident.open(run_id, query),
            )
            self._persist(directory, snapshot)
            return snapshot

    def load_run(self, run_id: UUID) -> Snapshot:
        with self._locked(run_id) as directory:
            return self._load(directory)

    def _event(
        self, snapshot: Snapshot, event_type: str, payload: dict[str, JsonValue]
    ) -> EventRecord:
        model_version = payload.get("model_version")
        PersistenceBoundary(self.redactor).validate((event_type, model_version))
        cleaned = self.redactor.object(payload)
        return EventRecord(
            run_id=snapshot.state.run_id,
            incident_id=snapshot.incident.incident_id,
            sequence=len(snapshot.events) + 1,
            timestamp=self.clock.utcnow(),
            type=event_type,
            input_hash=snapshot.query.query_hash,
            output_hash=canonical_hash(cleaned),
            budgets=snapshot.incident.counters,
            evidence_ids=tuple(record.evidence_id for record in snapshot.evidence),
            payload=cleaned,
            model_version=model_version if isinstance(model_version, str) else None,
            # Who had answered by the time this event was written, read from
            # the snapshot's own selection record. Events written before any
            # selection keep the default, which is the honest value.
            provider=snapshot.observed_provider,
        )

    def _append(
        self,
        snapshot: Snapshot,
        event_type: str,
        payload: dict[str, JsonValue],
        **changes: object,
    ) -> Snapshot:
        event = self._event(snapshot, event_type, payload)
        state = RunState.model_validate(
            snapshot.state.model_dump()
            | {"state_version": snapshot.state.state_version + 1}
        )
        return Snapshot.model_validate(
            snapshot.model_dump()
            | {"state": state, "events": snapshot.events + (event,)}
            | changes
        )

    def acquire(
        self, run_id: UUID, owner: UUID, *, run_seconds: float = 120.0
    ) -> Snapshot | None:
        PersistenceBoundary(self.redactor).validate(owner)
        run_seconds = Settings(run_seconds=run_seconds).run_seconds
        with self._locked(run_id) as directory:
            snapshot = self._load(directory)
            state = snapshot.state
            if state.status != "created" or state.lease_owner is not None:
                if state.status == "running" and self._lease_expired(state):
                    final = self._terminal(snapshot, Reason.LEASE_EXPIRED, {})
                    self._persist(directory, final)
                return None
            leased_state = RunState.model_validate(
                state.model_dump()
                | {
                    "status": "running",
                    "lease_owner": owner,
                    "lease_expires_at": self.clock.monotonic() + 150,
                    "lease_expires_utc": self.clock.utcnow() + timedelta(seconds=150),
                    "lease_clock_epoch": self.clock.epoch,
                    "run_deadline_at": self.clock.monotonic() + run_seconds,
                }
            )
            leased = Snapshot.model_validate(
                snapshot.model_dump() | {"state": leased_state}
            )
            leased = self._append(
                leased, "run_started", {"mode": "offline", "provider": "mock"}
            )
            self._persist(directory, leased)
            return leased

    def _lease_expired(self, state: RunState) -> bool:
        if state.lease_clock_epoch == self.clock.epoch:
            return (
                state.lease_expires_at is None
                or self.clock.monotonic() >= state.lease_expires_at
            )
        return (
            state.lease_expires_utc is None
            or self.clock.utcnow() >= state.lease_expires_utc
        )

    def _check_writer(self, actual: Snapshot, proposed: Snapshot, owner: UUID) -> None:
        if (
            actual.state.status != "running"
            or actual.state.lease_owner != owner
            or actual.state.state_version != proposed.state.state_version
            or actual != proposed
            or actual.state.lease_clock_epoch != self.clock.epoch
            or self._lease_expired(actual.state)
        ):
            raise RuntimeFault(Reason.STORAGE_ERROR)

    def record(
        self,
        snapshot: Snapshot,
        owner: UUID,
        event_type: str,
        payload: dict[str, JsonValue],
        *,
        query: QuerySpec | None = None,
        incident: Incident | None = None,
        evidence: tuple[EvidenceRecord, ...] | None = None,
        selection: SelectionAuthority | None = None,
        history: DecisionHistory | None = None,
        verification: ReportVerification | None = None,
    ) -> Snapshot:
        if event_type == "run_finished":
            raise RuntimeFault(Reason.INVARIANT)
        boundary = PersistenceBoundary(self.redactor)
        boundary.validate(event_type)
        boundary.validate(snapshot)
        boundary.validate((query, incident, evidence, verification), Reason.CONFLICT)
        boundary.validate(selection, Reason.FIRST_REJECTED)
        boundary.validate(history, Reason.RECEIPT_INVALID)
        with self._locked(snapshot.state.run_id) as directory:
            self._check_writer(self._load(directory), snapshot, owner)
            changed = Snapshot.model_validate(
                snapshot.model_dump()
                | {
                    "query": query or snapshot.query,
                    "incident": incident or snapshot.incident,
                    "evidence": snapshot.evidence if evidence is None else evidence,
                    "history": history or snapshot.history,
                    "verification": verification
                    or (
                        None
                        if query is not None or event_type == "query_observed"
                        else snapshot.verification
                    ),
                }
                | recorded_selections(snapshot, selection)
            )
            updated = self._append(changed, event_type, payload)
            self._persist(directory, updated)
            return updated

    def _terminal(
        self, snapshot: Snapshot, reason: Reason, payload: dict[str, JsonValue]
    ) -> Snapshot:
        terminal_state, disposition, status = terminal_mapping(reason)
        details = snapshot.terminal_fields() | {
            "cost": Measurement(),
            "duration_ms": Measurement(),
        }
        validated = TerminalResult.model_validate(
            details
            | payload
            | {
                "run_id": snapshot.state.run_id,
                "status": status,
                "reason_code": reason,
                "disposition": disposition,
            }
        )
        PersistenceBoundary(self.redactor).validate(validated)
        result = validated.model_dump(mode="json", exclude={"schema_version"})
        state = RunState.model_validate(
            snapshot.state.model_dump()
            | {
                "status": status,
                "reason_code": reason,
                "provider": validated.provider,
            }
        )
        incident = Incident.model_validate(
            snapshot.incident.model_dump() | {"state": terminal_state}
        )
        event = self._event(snapshot, "run_finished", result)
        event = EventRecord.model_validate(
            event.model_dump()
            | {"payload": result, "output_hash": canonical_hash(result)}
        )
        return Snapshot.model_validate(
            snapshot.model_dump()
            | {
                "state": RunState.model_validate(
                    state.model_dump() | {"state_version": state.state_version + 1}
                ),
                "incident": incident,
                "events": snapshot.events + (event,),
                "result": result,
            }
        )

    def finish(
        self,
        snapshot: Snapshot,
        owner: UUID,
        reason: Reason,
        payload: dict[str, JsonValue],
    ) -> Snapshot:
        with self._locked(snapshot.state.run_id) as directory:
            self._check_writer(self._load(directory), snapshot, owner)
            final = self._terminal(snapshot, reason, payload)
            self._persist(directory, final)
            return final

    def list_events(self, run_id: UUID, after_seq: int = 0) -> tuple[EventRecord, ...]:
        return tuple(
            event
            for event in self.load_run(run_id).events
            if event.sequence > after_seq
        )

    def save_artifact(
        self, snapshot: Snapshot, owner: UUID, name: str, payload: JsonValue
    ) -> Snapshot:
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.json", name) or name == "snapshot.json":
            raise RuntimeFault(Reason.STORAGE_ERROR)
        PersistenceBoundary(self.redactor).validate(name, Reason.STORAGE_ERROR)
        with self._locked(snapshot.state.run_id) as directory:
            self._check_writer(self._load(directory), snapshot, owner)
            artifacts = snapshot.artifacts | {name: self.redactor.clean(payload)}
            updated = self._append(
                snapshot, "artifact_saved", {"name": name}, artifacts=artifacts
            )
            self._persist(directory, updated)
            return updated

    def export_events(self, run_id: UUID) -> Path:
        with self._locked(run_id) as directory:
            snapshot = self._load(directory)
            path = directory / "events.jsonl"
            self._atomic(
                path,
                "".join(event.model_dump_json() + "\n" for event in snapshot.events),
            )
            return path
