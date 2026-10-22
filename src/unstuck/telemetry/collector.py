"""The telemetry sink seam and the recorder that owns the emission policy.

A sink receives records. It never returns anything the run reads, so no sink can
influence model selection, tool arguments, repair, evaluator truth, or the
terminal disposition. The default sink is the one that does nothing, which is
what makes telemetry opt-in rather than a behaviour change.

The recorder owns the whole policy, so a caller holds one object and the policy
cannot drift between call sites.

Refusal fails closed. A record that cannot be mapped, or that the shared
persistence boundary refuses, is not emitted. What is emitted instead is an
explicit `telemetry_dropped` marker carrying the correlation and the reason and
no observed payload, so the gap in the timeline is a recorded fact rather than a
silent absence.

Transport failure never reaches the run. A sink that raises is counted and the
run continues untouched, because an unwritable telemetry file is not a reason to
change a verified result. The count is readable, so a run whose telemetry was
incomplete can be told apart from one whose telemetry was complete.

Those counts live in memory, and that is the one fact a later process cannot
recover. So the recorder also persists them: `finalize` writes telemetry's own
completeness summary through the sink, and a file without one is unreconciled
rather than assumed whole.

File safety is descriptor-based, not path-based. The parent directory is opened
once without following a symlink and every later read and append goes through
that descriptor, so replacing the parent or the target after construction
cannot redirect a write or a read. Each opened descriptor is verified by
`fstat` rather than by its pathname: a regular file, owned by this process,
with one link, the same device and inode as the first open, and private to its
owner.
"""

import json
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal, Protocol
from uuid import UUID

from pydantic import ValidationError

from unstuck.cascade.selection import Provider
from unstuck.domain import Contract, Reason, RuntimeFault
from unstuck.events import EventRecord, PersistenceBoundary, Redactor
from unstuck.telemetry.contracts import (
    MAX_TIMELINE_RECORDS,
    CorrelationContext,
    InfrastructureDimensions,
    TelemetryCompleteness,
    TelemetryRecord,
    TelemetryTimeline,
    unsafe_public_fields,
)
from unstuck.telemetry.mapping import correlation_for, map_event
from unstuck.telemetry.sanitizer import sanitize

#: The private mode a telemetry file must have. The private bundle may retain
#: synthetic detail a public export would not, so it is readable by its owner
#: and by nobody else.
PRIVATE_MODE = 0o600

#: The most bytes one record line may carry, excluding its newline. A durable
#: event payload is already bounded well below this, so a longer line is
#: malformed input rather than a large record.
MAX_LINE_BYTES = 65_536

#: The most bytes a whole telemetry file may carry. Checked from the opened
#: descriptor before anything is read, so an oversized file costs one `fstat`
#: rather than one allocation of itself.
MAX_FILE_BYTES = MAX_TIMELINE_RECORDS * (MAX_LINE_BYTES + 1)

#: The fixed text prefix every public stdout line carries, record lines and the
#: completeness summary line alike. It names this project and the public line
#: schema generation and nothing else: no account, Region, runtime, endpoint,
#: stage or any other deployment identity, so the same token is correct in every
#: environment and a log filter built on it never has to be edited per
#: deployment. The trailing space is part of the prefix, so the JSON object
#: begins at a fixed offset and a reader strips a constant rather than searching
#: for a brace. A CloudWatch Logs filter pattern of the quoted prefix selects
#: exactly these lines, and unrelated container stdout is excluded rather than
#: parsed as telemetry.
PUBLIC_LINE_PREFIX = "UNSTUCK_TELEMETRY_V1 "

#: The suffix of the sidecar holding one run's completeness summary. It is a
#: sibling of the records file, opened through the same pinned directory.
SUMMARY_SUFFIX = ".summary.json"

#: Everything a sink or a projection may fail with. The tuple is explicit so a
#: fault this module does not own still propagates to the caller that owns it.
#: `AttributeError` and `KeyError` are here because a malformed event is the one
#: thing telemetry is handed from outside, and reading a field off it must not
#: become the run's problem.
_FAILURES = (
    AttributeError,
    KeyError,
    OSError,
    TypeError,
    ValidationError,
    ValueError,
    RuntimeFault,
)


class TelemetrySink(Protocol):
    """Receives one safe telemetry record. It answers the run nothing.

    A sink may also offer `finalize(completeness)`, which the recorder calls
    once at the run's last durable event. It is optional on purpose: a sink
    with nowhere to persist a summary simply does not have it, and its
    timelines are then unreconciled rather than wrongly complete.
    """

    def emit(self, record: TelemetryRecord) -> None: ...


class NullSink:
    """Records nothing. The default, so telemetry is never on by accident."""

    def emit(self, record: TelemetryRecord) -> None:
        return None


@dataclass
class MemorySink:
    """Keeps records in order for tests and for building one run's timeline."""

    records: list[TelemetryRecord] = field(default_factory=list)
    completeness: TelemetryCompleteness | None = None

    def emit(self, record: TelemetryRecord) -> None:
        self.records.append(record)

    def finalize(self, completeness: TelemetryCompleteness) -> None:
        """Keep telemetry's own account of how complete this record is."""
        self.completeness = TelemetryCompleteness.model_validate(completeness)

    def timeline(self, run_id: object | None = None) -> TelemetryTimeline:
        """Project the records of one run as its ordered timeline."""
        kept = [
            record
            for record in self.records
            if run_id is None or record.correlation.run_id == run_id
        ]
        if not kept:
            raise RuntimeFault(Reason.INVARIANT)
        identity = kept[0].correlation.run_id
        summary = self.completeness
        return TelemetryTimeline(
            visibility=kept[0].visibility,
            run_id=identity,
            records=tuple(kept),
            completeness=(
                summary if summary is not None and summary.run_id == identity else None
            ),
        )


class JsonlSink:
    """Appends private records to one newline-delimited file.

    The parent directory is opened once, without following a symlink, and every
    append and read goes through that descriptor. The target itself is opened
    with `O_NOFOLLOW` and then verified by `fstat`: a regular file, owned by
    this process, with exactly one link, on the same device and inode as the
    first open, and private to its owner. A target that already exists with a
    broader mode is tightened only once that ownership and link check has
    passed, and refused outright otherwise, so this never relaxes or rewrites
    the permissions of a file some other owner holds.

    Redaction has already happened: the recorder validates every record before
    this sink is reached, so nothing is cleaned on the way out of here.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_line_bytes: int = MAX_LINE_BYTES,
        max_records: int = MAX_TIMELINE_RECORDS,
    ):
        self.path = path.absolute()
        self.name = self.path.name
        self.summary_name = self.name + SUMMARY_SUFFIX
        self.max_file_bytes = max_file_bytes
        self.max_line_bytes = max_line_bytes
        self.max_records = max_records
        if not self.name or self.path.is_symlink():
            raise RuntimeFault(Reason.STORAGE_ERROR)
        self.directory = self._open_directory(self.path.parent)
        self.identity: tuple[int, int] | None = None
        self.written = 0

    @staticmethod
    def _open_directory(parent: Path) -> int:
        """Pin the parent directory, refusing a symlinked or absent one.

        Pinning is what makes every later open relative to the directory this
        sink was constructed against, so replacing the parent path afterwards
        reaches a different directory and not this one.
        """
        try:
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise RuntimeFault(Reason.STORAGE_ERROR)
        except OSError as error:
            os.close(descriptor)
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        except RuntimeFault:
            os.close(descriptor)
            raise
        return descriptor

    def close(self) -> None:
        """Release the pinned directory. Calling it twice is not an error."""
        if self.directory >= 0:
            os.close(self.directory)
            self.directory = -1

    def _open(self, flags: int) -> int:
        """Open the target through the pinned directory and verify it.

        Every check is made against the opened descriptor rather than the
        pathname, so a replacement between the open and the check cannot be
        the thing that is verified.
        """
        if self.directory < 0:
            raise RuntimeFault(Reason.STORAGE_ERROR)
        try:
            descriptor = os.open(
                self.name,
                flags | os.O_NOFOLLOW,
                PRIVATE_MODE,
                dir_fd=self.directory,
            )
        except OSError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        try:
            self._verify(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _verify(self, descriptor: int) -> None:
        try:
            status = os.fstat(descriptor)
        except OSError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        if not stat.S_ISREG(status.st_mode):
            raise RuntimeFault(Reason.STORAGE_ERROR)
        if status.st_uid != os.geteuid() or status.st_nlink != 1:
            raise RuntimeFault(Reason.STORAGE_ERROR)
        identity = (status.st_dev, status.st_ino)
        if self.identity is not None and identity != self.identity:
            raise RuntimeFault(Reason.STORAGE_ERROR)
        mode = stat.S_IMODE(status.st_mode)
        if mode != PRIVATE_MODE:
            # Ownership and link count have already been proven, so this
            # tightens a file this process owns alone and never one shared
            # with another owner or reachable under a second name.
            try:
                os.fchmod(descriptor, PRIVATE_MODE)
            except OSError as error:
                raise RuntimeFault(Reason.STORAGE_ERROR) from error
        self.identity = identity

    def emit(self, record: TelemetryRecord) -> None:
        text = TelemetryRecord.model_validate(record).model_dump_json() + "\n"
        descriptor = self._open(os.O_CREAT | os.O_WRONLY | os.O_APPEND)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        self.written += 1

    def finalize(self, completeness: TelemetryCompleteness) -> None:
        """Persist telemetry's own completeness summary beside the records."""
        summary = TelemetryCompleteness.model_validate(completeness)
        if self.directory < 0:
            raise RuntimeFault(Reason.STORAGE_ERROR)
        try:
            descriptor = os.open(
                self.summary_name,
                os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW,
                PRIVATE_MODE,
                dir_fd=self.directory,
            )
        except OSError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        try:
            self._verify_summary(descriptor)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            try:
                handle.write(summary.model_dump_json())
                handle.flush()
                os.fsync(handle.fileno())
            except OSError as error:
                raise RuntimeFault(Reason.STORAGE_ERROR) from error

    def _verify_summary(self, descriptor: int) -> None:
        try:
            status = os.fstat(descriptor)
        except OSError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        if not stat.S_ISREG(status.st_mode):
            raise RuntimeFault(Reason.STORAGE_ERROR)
        if status.st_uid != os.geteuid() or status.st_nlink != 1:
            raise RuntimeFault(Reason.STORAGE_ERROR)
        if stat.S_IMODE(status.st_mode) != PRIVATE_MODE:
            try:
                os.fchmod(descriptor, PRIVATE_MODE)
            except OSError as error:
                raise RuntimeFault(Reason.STORAGE_ERROR) from error

    def read(self) -> TelemetryTimeline:
        """Read one run's private timeline back, within declared bounds.

        The file size is checked from the opened descriptor, each line is read
        under its own byte bound, and the record bound stops the read before
        the next line is parsed. Nothing larger than the declared limits is
        ever allocated, and a malformed line is a reason-coded storage fault
        rather than a raw validation error reaching the caller.
        """
        descriptor = self._open(os.O_RDONLY)
        try:
            handle = os.fdopen(descriptor, "rb")
        except OSError as error:
            os.close(descriptor)
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        with handle:
            try:
                if os.fstat(handle.fileno()).st_size > self.max_file_bytes:
                    raise RuntimeFault(Reason.STORAGE_ERROR)
                records = self._records(handle)
            except OSError as error:
                raise RuntimeFault(Reason.STORAGE_ERROR) from error
        if not records:
            raise RuntimeFault(Reason.STORAGE_ERROR)
        return TelemetryTimeline(
            visibility=records[0].visibility,
            run_id=records[0].correlation.run_id,
            records=records,
            completeness=self._summary(),
        )

    def _records(self, handle: IO[bytes]) -> tuple[TelemetryRecord, ...]:
        records: list[TelemetryRecord] = []
        while True:
            # One more byte than a legal line plus its terminator, so an
            # oversized line is detected without being read whole.
            line = handle.readline(self.max_line_bytes + 2)
            if not line:
                break
            content = line.rstrip(b"\n")
            if len(content) > self.max_line_bytes:
                raise RuntimeFault(Reason.STORAGE_ERROR)
            if not content.strip():
                continue
            if len(records) >= self.max_records:
                raise RuntimeFault(Reason.STORAGE_ERROR)
            try:
                records.append(TelemetryRecord.model_validate_json(content))
            except (ValidationError, UnicodeError, ValueError) as error:
                raise RuntimeFault(Reason.STORAGE_ERROR) from error
        return tuple(records)

    def _summary(self) -> TelemetryCompleteness | None:
        """The persisted completeness summary, or nothing if none was written.

        An absent summary is not a fault. It is what makes the timeline
        unreconciled, which is the fact the export gate refuses on.
        """
        if self.directory < 0:
            return None
        try:
            descriptor = os.open(
                self.summary_name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=self.directory,
            )
        except OSError:
            return None
        try:
            handle = os.fdopen(descriptor, "rb")
        except OSError:
            os.close(descriptor)
            return None
        with handle:
            try:
                if os.fstat(handle.fileno()).st_size > self.max_line_bytes:
                    return None
                body = handle.read(self.max_line_bytes + 1)
            except OSError:
                return None
        try:
            return TelemetryCompleteness.model_validate_json(body)
        except (ValidationError, UnicodeError, ValueError):
            return None


class PublicLineSink:
    """Writes one compact sanitized public line per record to a text stream.

    This is the sink a hosted container attaches so its telemetry reaches the
    platform log it already writes to. The stream is the process's own stdout
    by default, so nothing here opens a file, a socket, or a client.

    What is written is the public projection, never the private record: the
    correlation arrives hashed, the hosting dimensions collapse to one identity
    hash, and the deployment values that have no public purpose are gone. The
    private record is what the owner's own `JsonlSink` keeps; a platform log is
    read by whoever can read the log group, so it gets the public form.

    Two refusals, and both fail closed. The projection is scanned by the typed
    last check, which follows the declared types rather than a value's shape, so
    a value that reached an unexpected field is refused rather than written. And
    a line past the declared byte bound is refused whole rather than truncated,
    because half a JSON line is not a record. Either way the recorder counts a
    dropped record and the run is untouched.

    Every written line begins with the fixed `PUBLIC_LINE_PREFIX`, so a reader
    of a shared platform log can select telemetry deterministically instead of
    guessing which lines are JSON. The declared byte bound covers the prefix
    together with the payload, so the bound still describes the whole line.

    The completeness summary travels on the same stream as its own final line.
    It carries the same prefix, telemetry's own counts and sequence bounds, and
    no `kind`, which is what tells it apart from a record line on arrival.
    """

    def __init__(
        self, stream: IO[str] | None = None, *, max_line_bytes: int = MAX_LINE_BYTES
    ):
        self.stream = stream
        self.max_line_bytes = max_line_bytes
        self.written = 0

    def emit(self, record: TelemetryRecord) -> None:
        """Write one record, projected onto its public form."""
        self._write(sanitize(record))

    def finalize(self, completeness: TelemetryCompleteness) -> None:
        """Write telemetry's own completeness summary as the final line."""
        self._write(TelemetryCompleteness.model_validate(completeness))

    def _write(self, value: Contract) -> None:
        """Check the projection, then write it as exactly one line.

        The check is the typed walk, which is the one a record and a timeline
        are checked with. The untyped walk is deliberately not used here: it
        grants no exemption for a declared type, so a hashed identity that
        happens to contain twelve consecutive digits would read as an account
        identifier and a legitimate record would be dropped at random.
        """
        if unsafe_public_fields(value):
            raise RuntimeFault(Reason.INVARIANT)
        payload = value.model_dump(mode="json", exclude_none=True)
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        line = PUBLIC_LINE_PREFIX + body
        if len(line.encode("utf-8")) > self.max_line_bytes:
            raise RuntimeFault(Reason.STORAGE_ERROR)
        stream = sys.stdout if self.stream is None else self.stream
        try:
            stream.write(line + "\n")
            stream.flush()
        except OSError as error:
            raise RuntimeFault(Reason.STORAGE_ERROR) from error
        self.written += 1


def public_line_payload(line: str) -> str | None:
    """The JSON body of one public line, or nothing if it is not one.

    This is the reader half of the emitted contract and the only sanctioned way
    to turn a log line into telemetry. A line without the declared prefix is
    not telemetry and is answered with nothing rather than parsed, so unrelated
    container stdout can never be accepted as a record.
    """
    if not line.startswith(PUBLIC_LINE_PREFIX):
        return None
    return line[len(PUBLIC_LINE_PREFIX) :]


@dataclass
class TelemetryHealth:
    """What telemetry itself did. Readable, so an incomplete record is visible.

    The sequence bounds and the observed count are telemetry's own view of the
    durable stream it was handed. They are what the persisted completeness
    summary is built from, so a later process can tell a whole record from one
    a transient failure punched a hole in.
    """

    emitted: int = 0
    refused: int = 0
    dropped: int = 0
    observed: int = 0
    run_id: UUID | None = None
    first_sequence: int | None = None
    final_sequence: int | None = None

    @property
    def complete(self) -> bool:
        return self.refused == 0 and self.dropped == 0

    @property
    def contiguous(self) -> bool:
        """Whether the observed durable sequences left no gap behind."""
        if self.first_sequence is None or self.final_sequence is None:
            return False
        return self.final_sequence - self.first_sequence + 1 == self.observed


class TelemetryRecorder:
    """Maps durable events to records and emits them under one fixed policy."""

    def __init__(
        self,
        sink: TelemetrySink | None = None,
        *,
        context: CorrelationContext | None = None,
        infrastructure: InfrastructureDimensions | None = None,
        redactor: Redactor | None = None,
    ):
        self.sink = sink or NullSink()
        self.context = context or CorrelationContext()
        self.infrastructure = infrastructure or InfrastructureDimensions()
        self.boundary = PersistenceBoundary(redactor or Redactor())
        self.health = TelemetryHealth()
        # Which provider answered this run's selector calls. It is observed
        # rather than configured: the caller tells the recorder what the
        # normalized selection said, and until a selection exists nothing has
        # answered, so the value is the scripted-stand-in default.
        self.provider: Provider = "mock"

    def observed_provider(self, provider: Provider) -> None:
        """Note who answered, from the normalized selection record.

        One run has one provider. A value contradicting an already observed
        real provider is ignored rather than applied, so a telemetry stream
        never reports two providers for one run; the run store is what refuses
        such a transition, and this never raises into the run. The value is a
        label on the record and never authority: by the time any selection
        exists, acceptance has already been recomputed from the offered
        options and the threshold.
        """
        if self.provider == "mock":
            self.provider = provider

    @property
    def active(self) -> bool:
        """Whether a sink was supplied at all. A null sink does no work."""
        return not isinstance(self.sink, NullSink)

    def record(
        self,
        event: EventRecord,
        *,
        stage: str | None = None,
        retry_state: Literal["new", "duplicate", "resumed"] | None = None,
    ) -> TelemetryRecord | None:
        """Record one durable event. This never raises into the run."""
        if not self.active:
            return None
        self._observe(event)
        try:
            projected = map_event(
                event,
                context=self.context,
                infrastructure=self.infrastructure,
                stage=stage,
                retry_state=retry_state,
                provider=self.provider,
            )
        except _FAILURES:
            self._refuse(event, "mapping_refused")
            return None
        try:
            self.boundary.validate(projected, Reason.INVARIANT)
        except RuntimeFault:
            self._refuse(event, "boundary_refused")
            return None
        return self._emit(projected)

    def _observe(self, event: EventRecord) -> None:
        """Note which durable event this is, before anything can refuse it.

        Counting here rather than after mapping is what makes the completeness
        summary honest: an event that could not be mapped at all still has to
        be accounted for, and it is the one case where the counts cannot
        balance and no summary is written.
        """
        sequence = getattr(event, "sequence", None)
        run_id = getattr(event, "run_id", None)
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            return None
        self.health.observed += 1
        if self.health.first_sequence is None:
            self.health.first_sequence = sequence
            self.health.run_id = run_id if isinstance(run_id, UUID) else None
        self.health.first_sequence = min(self.health.first_sequence, sequence)
        self.health.final_sequence = max(self.health.final_sequence or 0, sequence)
        return None

    def completeness(self, *, finalized: bool = True) -> TelemetryCompleteness | None:
        """Telemetry's own account of this run's record, or nothing.

        Nothing is returned when the counts do not account for every observed
        event, which is the fail-closed answer: without a summary the timeline
        is unreconciled and the export gate refuses it, rather than a summary
        claiming a completeness the counts do not support.
        """
        health = self.health
        if health.run_id is None or health.first_sequence is None:
            return None
        if health.final_sequence is None:
            return None
        try:
            return TelemetryCompleteness(
                run_id=health.run_id,
                first_sequence=health.first_sequence,
                final_sequence=health.final_sequence,
                expected_event_count=health.observed,
                emitted=health.emitted,
                refused=health.refused,
                dropped=health.dropped,
                finalized=finalized,
            )
        except ValidationError:
            return None

    def finalize(self) -> TelemetryCompleteness | None:
        """Persist the completeness summary. This never raises into the run."""
        if not self.active:
            return None
        summary = self.completeness()
        if summary is None:
            return None
        persist = getattr(self.sink, "finalize", None)
        if persist is None:
            return summary
        try:
            persist(summary)
        except _FAILURES:
            return None
        return summary

    def _refuse(self, event: EventRecord, reason: str) -> None:
        """Fail closed, and say so. The refused record is never emitted."""
        self.health.refused += 1
        try:
            marker = TelemetryRecord(
                kind="telemetry_dropped",
                correlation=correlation_for(event, self.context),
                infrastructure=self.infrastructure,
                provider=self.provider,
                drop_reason=reason,
            )
        except _FAILURES:
            return None
        self._emit(marker)
        return None

    def _emit(self, record: TelemetryRecord) -> TelemetryRecord | None:
        try:
            self.sink.emit(record)
        except _FAILURES:
            self.health.dropped += 1
            return None
        self.health.emitted += 1
        return record
