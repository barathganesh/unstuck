"""The evidence bundle: joins, gaps, coverage, manifest, public projection.

A bundle is one run's application telemetry joined to the external signals an
operator exported for the same capture window, with a manifest that binds every
part by content. It exists so the evidence survives the run: a reader months
later can tell what was captured, what was not, what was withheld, and what is
still waiting on a live gate, without having to trust a summary.

Five properties are the whole design.

Every join basis is named. A record pair is linked by the AWS request identity,
the trace identity, the span identity, the runtime session identity, or the
application's own correlation hash, and the basis travels with the edge. A
single opaque "matched" flag would make a session-level coincidence
indistinguishable from a request-level identity, which is the difference
between evidence and decoration.

No unmatched record is dropped. Every imported record stays in the bundle, and
the gap report says per kind how many joined, how many did not, and which ones.
A join that quietly discards its misses reports perfect coverage over whatever
survived.

The manifest binds the content, and nothing in the manifest is taken on trust.
Each part is hashed, the gap report included, and the bundle's own validator
recomputes every part hash, recomputes the gap report from the records and the
joins, and then rebuilds the whole manifest from the validated parts and
requires it to be equal. A summary that can be recomputed is never believed:
the source list, the record counts, the join counts, the pricing provenance,
the capture window and the redaction policy hash are all derived, so a manifest
cannot describe a bundle it does not belong to, and a duplicated count cannot
inflate a published tally. That is what makes a public bundle independently
verifiable: recompute the parts, rebuild the manifest, hash it.

No absent value becomes zero. The field coverage matrix states, for every field
the bundle could carry, whether it was captured, was unavailable from a source
that was supplied, was redacted for the public, was never configured, or is
pending its live gate. None of those five is a number, so none of them can be
mistaken for a measurement of zero, and an offline bundle can never read as a
live one.

Public is a projection that keeps the causality. The account, principal,
runtime, endpoint, log group, request and session identity are hashed or
removed, and the joins survive because the join key is hashed with the same
unsalted function the application timeline already uses. A reader can still
follow one request through the spans, the trail, the invocation and the
application's own record; they just cannot say whose account it was.

Nothing in this module publishes, uploads, submits, or sends anything.
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from unstuck.cascade.serialization import canonical_digest
from unstuck.domain import Contract, Digest, Identifier, Reason, RuntimeFault
from unstuck.telemetry.contracts import (
    MAX_TIMELINE_RECORDS,
    PricingProvenance,
    TelemetryTimeline,
    TelemetryVersion,
    Visibility,
    public_digest,
    unsafe_public_fields,
)
from unstuck.telemetry.runtime import (
    LIVE_IDENTITY_FIELDS,
    VARIABLE_TREATMENT,
    ConfiguredTelemetry,
    LiveEvidenceGate,
    OtelConfigMap,
)
from unstuck.telemetry.sanitizer import (
    CORRELATION_TREATMENT,
    IDENTITY_TREATMENT,
    INFRASTRUCTURE_TREATMENT,
    sanitize_timeline,
)
from unstuck.telemetry.signals import (
    MAX_SIGNAL_RECORDS,
    SIGNAL_KIND_ORDER,
    ExternalSignals,
    SignalSource,
    SourceKind,
    source_order,
)

BundleVersion = Literal["telemetry-bundle-1"]
BUNDLE_VERSION: BundleVersion = "telemetry-bundle-1"

JoinBasis = Literal[
    "aws_request_id",
    "trace_id",
    "span_id",
    "runtime_session_id",
    "application_correlation_hash",
]

#: Every basis a join may rest on, in a fixed order so a manifest's join counts
#: are comparable between bundles.
JOIN_BASES: tuple[JoinBasis, ...] = (
    "application_correlation_hash",
    "aws_request_id",
    "runtime_session_id",
    "span_id",
    "trace_id",
)

#: The imported signal kinds, re-exported so a caller does not have to reach
#: into the signals module for the same tuple.
SIGNAL_KINDS = SIGNAL_KIND_ORDER

#: The most join edges one bundle may carry. Five bases over bounded record
#: sets, so this is a ceiling rather than an expected size.
MAX_JOIN_EDGES = 2048

#: Every part of a bundle the manifest binds by content. `gaps` is one of them:
#: publication derives its unmatched-record tally from the gap report, so a gap
#: report outside the hash boundary is a published claim nothing binds.
OutputName = Literal[
    "configuration", "coverage", "gaps", "joins", "signals", "timeline"
]
OUTPUT_NAMES: tuple[OutputName, ...] = (
    "configuration",
    "coverage",
    "gaps",
    "joins",
    "signals",
    "timeline",
)

#: Every record kind the manifest counts, in the order it counts them. The
#: application timeline is last because it is the bundle's own record rather
#: than an imported one.
RECORD_KINDS: tuple[SourceKind, ...] = (*SIGNAL_KIND_ORDER, "application_timeline")

CoverageStatus = Literal[
    "captured", "unavailable", "redacted", "not_configured", "pending_live_gate"
]
CoverageGroup = Literal[
    "runtime_configuration",
    "otel_environment",
    "application",
    "external_signal",
    "pricing",
]

#: The configuration fields the coverage matrix reports on. The same tuple the
#: deployment contract and the preflight use, so a field cannot be missing from
#: one view and present in another.
CONFIGURATION_FIELDS: tuple[str, ...] = LIVE_IDENTITY_FIELDS

#: Which configuration fields a public bundle withholds. Every one of them is
#: environment identity, so the public answer is `redacted` rather than a value.
PUBLIC_WITHHELD_FIELDS: frozenset[str] = frozenset(CONFIGURATION_FIELDS)


def redaction_policy_hash() -> str:
    """One hash over every declared redaction treatment this bundle applies.

    Binding the policy rather than describing it is what lets a later reader
    prove two bundles were sanitized under the same rules, and notice when they
    were not.
    """
    return canonical_digest(
        {
            "correlation": CORRELATION_TREATMENT,
            "identity": IDENTITY_TREATMENT,
            "infrastructure": INFRASTRUCTURE_TREATMENT,
            "variables": VARIABLE_TREATMENT,
            "configuration_fields": sorted(PUBLIC_WITHHELD_FIELDS),
        }
    )


class JoinEdge(Contract):
    """One join: a basis, the hashed key, and every record it reached.

    Signal records are referenced by their index in the bundle's own signal
    tuples. An index is not identity, so it is public as it stands, and it
    cannot drift from the record it points at the way a copied identifier
    could.
    """

    basis: JoinBasis
    key_hash: Digest
    application_sequences: Annotated[
        tuple[int, ...], Field(max_length=MAX_TIMELINE_RECORDS)
    ] = ()
    span_indices: Annotated[tuple[int, ...], Field(max_length=MAX_SIGNAL_RECORDS)] = ()
    trail_indices: Annotated[tuple[int, ...], Field(max_length=MAX_SIGNAL_RECORDS)] = ()
    metric_indices: Annotated[
        tuple[int, ...], Field(max_length=MAX_SIGNAL_RECORDS)
    ] = ()
    invocation_indices: Annotated[
        tuple[int, ...], Field(max_length=MAX_SIGNAL_RECORDS)
    ] = ()

    @model_validator(mode="after")
    def ordered(self) -> Self:
        for members in self._members:
            if members != tuple(sorted(set(members))):
                raise ValueError("join members are ordered and unique")
        if self.degree == 0:
            raise ValueError("a join edge reaches at least one record")
        return self

    @property
    def _members(self) -> tuple[tuple[int, ...], ...]:
        return (
            self.application_sequences,
            self.span_indices,
            self.trail_indices,
            self.metric_indices,
            self.invocation_indices,
        )

    @property
    def degree(self) -> int:
        """How many records this edge reaches, across every source."""
        return sum(len(members) for members in self._members)

    @property
    def cross_source(self) -> bool:
        """Whether the edge links more than one source, not just itself."""
        return sum(1 for members in self._members if members) > 1


class SignalGap(Contract):
    """What joined and what did not, for one imported kind.

    `unmatched_indices` names the misses rather than counting them, because a
    count alone cannot be checked against the records the bundle carries.
    """

    kind: SourceKind
    total: Annotated[int, Field(ge=0, le=MAX_SIGNAL_RECORDS)]
    joined: Annotated[int, Field(ge=0, le=MAX_SIGNAL_RECORDS)]
    unmatched: Annotated[int, Field(ge=0, le=MAX_SIGNAL_RECORDS)]
    unmatched_indices: Annotated[
        tuple[int, ...], Field(max_length=MAX_SIGNAL_RECORDS)
    ] = ()

    @model_validator(mode="after")
    def accounted(self) -> Self:
        if self.joined + self.unmatched != self.total:
            raise ValueError("a gap report accounts for every record")
        if len(self.unmatched_indices) != self.unmatched:
            raise ValueError("a gap report names every unmatched record")
        if self.unmatched_indices != tuple(sorted(set(self.unmatched_indices))):
            raise ValueError("unmatched records are ordered and unique")
        return self


class FieldCoverage(Contract):
    """One field, and which of the five honest answers applies to it."""

    field: Annotated[
        str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_]+$")
    ]
    group: CoverageGroup
    status: CoverageStatus
    detail: Annotated[str, Field(min_length=1, max_length=256)]


class CoverageMatrix(Contract):
    """Every field the bundle could carry, with its status. No value is zero."""

    entries: Annotated[tuple[FieldCoverage, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def unique(self) -> Self:
        names = [entry.field for entry in self.entries]
        if len(set(names)) != len(names):
            raise ValueError("a coverage field appears twice")
        if names != sorted(names):
            raise ValueError("coverage entries are ordered by field")
        return self

    def entry(self, field: str) -> FieldCoverage:
        """One field's coverage. A field the matrix does not cover is a fault."""
        found = next((item for item in self.entries if item.field == field), None)
        if found is None:
            raise RuntimeFault(Reason.INVARIANT)
        return found

    @property
    def counts(self) -> dict[str, int]:
        """How many fields hold each status, including the statuses at zero.

        A status count is a count of matrix rows, never a measurement of the
        system, so reporting it as zero claims nothing about what was observed.
        """
        tally = dict.fromkeys(
            (
                "captured",
                "unavailable",
                "redacted",
                "not_configured",
                "pending_live_gate",
            ),
            0,
        )
        for entry in self.entries:
            tally[entry.status] += 1
        return tally


class RecordCount(Contract):
    """How many records of one kind the bundle carries."""

    kind: SourceKind
    count: Annotated[int, Field(ge=0, le=MAX_SIGNAL_RECORDS)]


class JoinCount(Contract):
    """How one basis performed: how many edges, and how many crossed sources."""

    basis: JoinBasis
    edges: Annotated[int, Field(ge=0, le=MAX_JOIN_EDGES)]
    cross_source_edges: Annotated[int, Field(ge=0, le=MAX_JOIN_EDGES)]

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.cross_source_edges > self.edges:
            raise ValueError("a basis cannot cross more sources than it has edges")
        return self


class OutputHash(Contract):
    """One part of the bundle, bound by the hash of its canonical form."""

    name: OutputName
    content_hash: Digest


class EvidenceManifest(Contract):
    """What this bundle is, what went into it, and what came out of it.

    Every list here is canonical and unique, not merely complete as a set. A
    duplicated record count or join count inflates a published tally, and a
    reordered output list makes the same content hash two ways, so both are
    refused by this contract before the bundle's own validator compares the
    whole manifest against the parts it was built from.
    """

    bundle_version: BundleVersion = BUNDLE_VERSION
    telemetry_version: TelemetryVersion
    visibility: Visibility = "private"
    capture_window_start: datetime
    capture_window_end: datetime
    config_hash: Digest
    redaction_policy_hash: Digest
    sources: Annotated[tuple[SignalSource, ...], Field(max_length=32)] = ()
    record_counts: Annotated[tuple[RecordCount, ...], Field(min_length=1, max_length=8)]
    join_counts: Annotated[tuple[JoinCount, ...], Field(min_length=1, max_length=8)]
    pricing: PricingProvenance = PricingProvenance()
    outputs: Annotated[tuple[OutputHash, ...], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def complete(self) -> Self:
        if self.capture_window_end < self.capture_window_start:
            raise ValueError("a manifest capture window is ordered")
        if tuple(item.name for item in self.outputs) != OUTPUT_NAMES:
            raise ValueError("a manifest binds every output once, in order")
        if tuple(item.basis for item in self.join_counts) != JOIN_BASES:
            raise ValueError("a manifest names every join basis once, in order")
        if tuple(item.kind for item in self.record_counts) != RECORD_KINDS:
            raise ValueError("a manifest counts every record kind once, in order")
        keys = [source_order(item) for item in self.sources]
        if len(set(keys)) != len(keys):
            raise ValueError("a manifest declares a signal source twice")
        if keys != sorted(keys):
            raise ValueError("manifest sources are ordered by kind and label")
        return self

    @property
    def manifest_hash(self) -> str:
        return canonical_digest(self)

    def output(self, name: str) -> str:
        found = next(
            (item.content_hash for item in self.outputs if item.name == name), None
        )
        if found is None:
            raise RuntimeFault(Reason.INVARIANT)
        return found


class EvidenceBundle(Contract):
    """One run's joined telemetry evidence, private or already sanitized.

    The validator is the gate, and it recomputes rather than reads. It checks
    that the parts agree on visibility, that every index and sequence names a
    record the bundle actually carries, that the gap report is exactly the one
    the records and joins produce, that every part still hashes to what the
    manifest says, and that the whole manifest equals the one its own parts
    would produce. For a public bundle it then checks that no raw identity
    survived anywhere in it. A bundle that fails any of those is not
    constructible, so a caller cannot hand one on as evidence.
    """

    manifest: EvidenceManifest
    configuration: ConfiguredTelemetry
    timeline: TelemetryTimeline
    signals: ExternalSignals
    joins: Annotated[tuple[JoinEdge, ...], Field(max_length=MAX_JOIN_EDGES)] = ()
    gaps: Annotated[tuple[SignalGap, ...], Field(min_length=1, max_length=8)]
    coverage: CoverageMatrix

    @model_validator(mode="after")
    def bound(self) -> Self:
        visibilities = {
            self.manifest.visibility,
            self.configuration.visibility,
            self.timeline.visibility,
            self.signals.visibility,
        }
        if len(visibilities) != 1:
            raise ValueError("every part of a bundle shares its visibility")
        if self.manifest.config_hash != self.configuration.config_hash:
            raise ValueError("a bundle manifest binds its own configuration")
        self._references_exist()
        if self.gaps != _gaps(self.signals, self.timeline, self.joins):
            raise ValueError("a bundle gap report does not match its own records")
        for name, digest in _part_hashes(self).items():
            if self.manifest.output(name) != digest:
                raise ValueError("a bundle part does not match its manifest hash")
        if self.manifest != _expected_manifest(self):
            raise ValueError("a bundle manifest does not match its own parts")
        if self.visibility != "public":
            return self
        if self.signals.identity_bearing:
            raise ValueError("a public bundle carries hashed signal identity only")
        if self.configuration.dimensions.identity_bearing:
            raise ValueError("a public bundle carries no raw hosting identity")
        unsafe = unsafe_public_fields(self)
        if unsafe:
            raise ValueError("a public bundle retains private identity")
        return self

    def _references_exist(self) -> None:
        """Refuse an index or sequence that names no record in this bundle.

        An index is only not identity because it points at a record the bundle
        carries. One past the end of its collection is a dangling reference,
        and a gap report built on one accounts for a record nobody can read.
        """
        sizes = {
            "span_indices": len(self.signals.spans),
            "trail_indices": len(self.signals.trail),
            "metric_indices": len(self.signals.metrics),
            "invocation_indices": len(self.signals.invocations),
        }
        sequences = {record.correlation.sequence for record in self.timeline.records}
        for edge in self.joins:
            for name, size in sizes.items():
                members: tuple[int, ...] = getattr(edge, name)
                if any(index >= size for index in members):
                    raise ValueError("a join edge names a record the bundle lacks")
            if not set(edge.application_sequences) <= sequences:
                raise ValueError(
                    "a join edge names an application record the bundle lacks"
                )
        totals = self.signals.counts
        for gap in self.gaps:
            if gap.kind == "application_timeline":
                if not set(gap.unmatched_indices) <= sequences:
                    raise ValueError(
                        "a gap report names an application record the bundle lacks"
                    )
                if gap.total != len(self.timeline.records):
                    raise ValueError("a gap report miscounts the application timeline")
                continue
            if gap.total != totals[gap.kind]:
                raise ValueError("a gap report miscounts an imported kind")
            if any(index >= gap.total for index in gap.unmatched_indices):
                raise ValueError("a gap report names a record the bundle lacks")

    @property
    def visibility(self) -> Visibility:
        return self.manifest.visibility

    @property
    def bundle_hash(self) -> str:
        """The bundle's identity: the hash of the manifest that binds it."""
        return self.manifest.manifest_hash

    @property
    def verified(self) -> bool:
        """Whether this bundle is safe to hand on as evidence.

        Construction already proved the manifest binding. What is left is the
        completeness of the application record: a timeline with no finalized
        summary accounting for exactly its records is a partial causal story,
        and a partial story is not evidence.
        """
        return not bundle_findings(self)


def _canonical(value: object) -> str:
    return canonical_digest(value)


def _hashes(
    *,
    configuration: ConfiguredTelemetry,
    coverage: CoverageMatrix,
    gaps: tuple[SignalGap, ...],
    joins: tuple[JoinEdge, ...],
    signals: ExternalSignals,
    timeline: TelemetryTimeline,
) -> dict[str, str]:
    """The content hash of each bundle part, in the manifest's own vocabulary.

    One definition, used by the manifest that records the hashes and by the
    validator that re-checks them, so the two can never disagree about how a
    part is hashed.
    """
    return {
        "configuration": _canonical(configuration),
        "coverage": _canonical(coverage),
        "gaps": _canonical([gap.model_dump(mode="json") for gap in gaps]),
        "joins": _canonical([edge.model_dump(mode="json") for edge in joins]),
        "signals": _canonical(signals),
        "timeline": _canonical(timeline),
    }


def _part_hashes(bundle: EvidenceBundle) -> dict[str, str]:
    return _hashes(
        configuration=bundle.configuration,
        coverage=bundle.coverage,
        gaps=bundle.gaps,
        joins=bundle.joins,
        signals=bundle.signals,
        timeline=bundle.timeline,
    )


def _expected_manifest(bundle: EvidenceBundle) -> EvidenceManifest:
    """The manifest this bundle's own parts produce.

    Comparing against this is what makes every summary in the manifest derived
    rather than declared. Nothing a reader could recompute is believed: the
    source list, the record counts, the join counts, the pricing provenance,
    the capture window and the redaction policy hash all come from the parts.
    """
    return evidence_manifest(
        configuration=bundle.configuration,
        timeline=bundle.timeline,
        signals=bundle.signals,
        joins=bundle.joins,
        gaps=bundle.gaps,
        coverage=bundle.coverage,
        visibility=bundle.manifest.visibility,
    )


def bundle_findings(bundle: EvidenceBundle) -> tuple[str, ...]:
    """Every reason this bundle is not safe to hand on, or nothing.

    Returning the reasons rather than a flag is what makes a refusal
    diagnosable: a caller can say which property failed instead of reporting
    that something did.

    The manifest binding is not re-checked here, because construction already
    proved it: an `EvidenceBundle` whose parts do not hash to what its manifest
    says cannot be built. A bundle assembled around that check fails to
    validate at all, which is the first finding below.

    What is left is the completeness of the application record, which
    construction deliberately does not enforce so that an incomplete bundle can
    still be inspected and diagnosed rather than merely refused.
    """
    try:
        validated = EvidenceBundle.model_validate(bundle)
    except ValidationError as error:
        return (f"the bundle does not validate: {error.error_count()} refusal(s)",)
    findings: list[str] = []
    if not validated.timeline.reconciled:
        findings.append("application timeline is unreconciled")
    if validated.visibility == "public":
        findings.extend(unsafe_public_fields(validated))
    return tuple(findings)


def _application_keys(
    timeline: TelemetryTimeline,
) -> dict[JoinBasis, dict[str, list[int]]]:
    """Every application record's join keys, hashed, by basis.

    A private record carries the raw identity and a public one carries the hash
    already. Both are reduced to the same hashed key here, which is why a
    public bundle's joins are identical to the private bundle's.
    """
    keys: dict[JoinBasis, dict[str, list[int]]] = {
        basis: defaultdict(list) for basis in JOIN_BASES
    }
    for record in timeline.records:
        correlation = record.correlation
        sequence = correlation.sequence
        raw: dict[JoinBasis, tuple[str | None, str | None]] = {
            "aws_request_id": (
                correlation.aws_request_id,
                correlation.aws_request_id_hash,
            ),
            "trace_id": (correlation.trace_id, correlation.trace_id_hash),
            "span_id": (correlation.span_id, correlation.span_id_hash),
            "runtime_session_id": (
                correlation.runtime_session_id,
                correlation.runtime_session_id_hash,
            ),
            "application_correlation_hash": (
                None,
                correlation.idempotency_identity_hash,
            ),
        }
        for basis, (value, hashed) in raw.items():
            key = public_digest(value) if value is not None else hashed
            if key is not None:
                keys[basis][key].append(sequence)
    return keys


def _signal_keys(
    signals: ExternalSignals,
) -> dict[JoinBasis, dict[str, dict[str, list[int]]]]:
    """Every imported record's join keys, hashed, by basis and by kind."""
    keys: dict[JoinBasis, dict[str, dict[str, list[int]]]] = {
        basis: defaultdict(lambda: defaultdict(list)) for basis in JOIN_BASES
    }

    def add(
        basis: JoinBasis, kind: str, index: int, raw: str | None, hashed: str | None
    ) -> None:
        key = public_digest(raw) if raw is not None else hashed
        if key is not None:
            keys[basis][key][kind].append(index)

    for index, span in enumerate(signals.spans):
        add("trace_id", "otel_spans", index, span.trace_id, span.trace_id_hash)
        add("span_id", "otel_spans", index, span.span_id, span.span_id_hash)
        add(
            "runtime_session_id",
            "otel_spans",
            index,
            span.runtime_session_id,
            span.runtime_session_id_hash,
        )
        add(
            "aws_request_id",
            "otel_spans",
            index,
            span.aws_request_id,
            span.aws_request_id_hash,
        )
    for index, record in enumerate(signals.trail):
        add(
            "aws_request_id",
            "cloudtrail",
            index,
            record.aws_request_id,
            record.aws_request_id_hash,
        )
    for index, reading in enumerate(signals.metrics):
        add(
            "runtime_session_id",
            "cloudwatch_metrics",
            index,
            reading.runtime_session_id,
            reading.runtime_session_id_hash,
        )
    for index, call in enumerate(signals.invocations):
        add(
            "aws_request_id",
            "model_invocations",
            index,
            call.aws_request_id,
            call.aws_request_id_hash,
        )
        add("trace_id", "model_invocations", index, call.trace_id, call.trace_id_hash)
        add("span_id", "model_invocations", index, call.span_id, call.span_id_hash)
        add(
            "runtime_session_id",
            "model_invocations",
            index,
            call.runtime_session_id,
            call.runtime_session_id_hash,
        )
    return keys


def _ordered(values: Iterable[int]) -> tuple[int, ...]:
    return tuple(sorted(set(values)))


def _join(
    timeline: TelemetryTimeline, signals: ExternalSignals
) -> tuple[JoinEdge, ...]:
    """Build one edge per basis and key that reaches anything at all."""
    application = _application_keys(timeline)
    external = _signal_keys(signals)
    edges: list[JoinEdge] = []
    for basis in JOIN_BASES:
        for key in sorted(set(application[basis]) | set(external[basis])):
            members = external[basis].get(key, {})
            edge = JoinEdge(
                basis=basis,
                key_hash=key,
                application_sequences=_ordered(application[basis].get(key, ())),
                span_indices=_ordered(members.get("otel_spans", ())),
                trail_indices=_ordered(members.get("cloudtrail", ())),
                metric_indices=_ordered(members.get("cloudwatch_metrics", ())),
                invocation_indices=_ordered(members.get("model_invocations", ())),
            )
            edges.append(edge)
    return tuple(edges)


def _gaps(
    signals: ExternalSignals, timeline: TelemetryTimeline, joins: tuple[JoinEdge, ...]
) -> tuple[SignalGap, ...]:
    """What joined to an application record, per kind. Misses are named.

    A signal record counts as joined only when an edge links it to at least one
    application record. An edge that reaches two spans and no application
    record is a coincidence between exports, not evidence about this run.
    """
    matched: dict[str, set[int]] = {kind: set() for kind in SIGNAL_KIND_ORDER}
    attribute = {
        "otel_spans": "span_indices",
        "cloudtrail": "trail_indices",
        "cloudwatch_metrics": "metric_indices",
        "model_invocations": "invocation_indices",
    }
    for edge in joins:
        if not edge.application_sequences:
            continue
        for kind, name in attribute.items():
            matched[kind] |= set(getattr(edge, name))
    totals = {
        "otel_spans": len(signals.spans),
        "cloudtrail": len(signals.trail),
        "cloudwatch_metrics": len(signals.metrics),
        "model_invocations": len(signals.invocations),
    }
    gaps = [
        SignalGap(
            kind=kind,
            total=totals[kind],
            joined=len(matched[kind]),
            unmatched=totals[kind] - len(matched[kind]),
            unmatched_indices=tuple(
                index for index in range(totals[kind]) if index not in matched[kind]
            ),
        )
        for kind in SIGNAL_KIND_ORDER
    ]
    linked = {
        sequence
        for edge in joins
        if edge.cross_source
        for sequence in edge.application_sequences
    }
    records = len(timeline.records)
    gaps.append(
        SignalGap(
            kind="application_timeline",
            total=records,
            joined=len(linked),
            unmatched=records - len(linked),
            unmatched_indices=tuple(
                record.correlation.sequence
                for record in timeline.records
                if record.correlation.sequence not in linked
            ),
        )
    )
    return tuple(gaps)


def _configuration_coverage(
    configuration: ConfiguredTelemetry,
) -> list[FieldCoverage]:
    public = configuration.visibility == "public"
    dimensions = configuration.dimensions
    recorded = {
        "account_id": dimensions.account_id,
        "region": dimensions.region,
        "agent_id": dimensions.agent_id,
        "runtime_id": dimensions.runtime_id,
        "endpoint_name": dimensions.endpoint_id,
        "model_id": dimensions.model_id,
        "log_group": dimensions.log_group,
    }
    missing = set(configuration.missing_live_fields)
    entries: list[FieldCoverage] = []
    for field in CONFIGURATION_FIELDS:
        if public:
            status: CoverageStatus = (
                "redacted" if field not in missing else "not_configured"
            )
            detail = (
                "Withheld from the public bundle; the hosting identity hash "
                "stands in its place."
                if status == "redacted"
                else "Deployment configuration supplied no value."
            )
        elif field in missing:
            status = "not_configured"
            detail = "Deployment configuration supplied no value."
        else:
            status = "captured"
            detail = (
                "Supplied by validated deployment configuration and recorded on "
                "every telemetry record."
                if recorded.get(field) is not None
                else "Supplied by validated deployment configuration."
            )
        entries.append(
            FieldCoverage(
                field=field,
                group="runtime_configuration",
                status=status,
                detail=detail,
            )
        )
    return entries


def _variable_coverage(variables: OtelConfigMap) -> list[FieldCoverage]:
    entries: list[FieldCoverage] = []
    for name, value in variables.mapping.items():
        entries.append(
            FieldCoverage(
                field=name,
                group="otel_environment",
                status="captured",
                detail=f"Projected from deployment configuration ({len(value)} bytes).",
            )
        )
    for name in variables.absent:
        entries.append(
            FieldCoverage(
                field=name,
                group="otel_environment",
                status="not_configured",
                detail="Its deployment inputs were not supplied, so it is unset.",
            )
        )
    for name in variables.redacted:
        entries.append(
            FieldCoverage(
                field=name,
                group="otel_environment",
                status="redacted",
                detail="Withheld from the public bundle; it carries hosting identity.",
            )
        )
    return entries


def _signal_coverage(
    signals: ExternalSignals, configuration: ConfiguredTelemetry
) -> list[FieldCoverage]:
    """One status per imported kind, derived from its declared source.

    `unavailable` is earned by a source, not inferred from configuration. A
    kind that named an export which returned nothing is unavailable; a kind
    that named no export at all is `not_configured`, or `pending_live_gate`
    while the deployment inputs are still incomplete. Reporting the last two as
    `unavailable` would claim somebody looked.
    """
    entries: list[FieldCoverage] = []
    supplied = signals.supplied_kinds
    for kind, count in signals.counts.items():
        if count:
            status: CoverageStatus = "captured"
            detail = (
                f"{count} exported record(s) imported for this window from a "
                "declared source."
            )
        elif kind in supplied:
            status = "unavailable"
            detail = "A source was declared for this window and it delivered no record."
        elif not configuration.inputs_complete:
            status = "pending_live_gate"
            detail = "Live telemetry inputs are incomplete, so no record can exist yet."
        else:
            status = "not_configured"
            detail = "No export was supplied for this kind, so nothing was read."
        entries.append(
            FieldCoverage(
                field=kind, group="external_signal", status=status, detail=detail
            )
        )
    return entries


def _application_coverage(timeline: TelemetryTimeline) -> list[FieldCoverage]:
    reconciled = timeline.reconciled
    return [
        FieldCoverage(
            field="application_timeline",
            group="application",
            status="captured" if timeline.records else "unavailable",
            detail=(
                f"{len(timeline.records)} recorded event(s) for this run."
                if timeline.records
                else "No application telemetry was recorded for this run."
            ),
        ),
        FieldCoverage(
            field="application_completeness",
            group="application",
            status="captured" if reconciled else "unavailable",
            detail=(
                "A finalized summary accounts for exactly these records."
                if reconciled
                else "No finalized summary accounts for these records."
            ),
        ),
    ]


def _pricing_coverage(pricing: PricingProvenance) -> FieldCoverage:
    if pricing.availability == "measured":
        return FieldCoverage(
            field="pricing",
            group="pricing",
            status="captured",
            detail=f"Priced from {pricing.source} dated {pricing.pricing_date}.",
        )
    return FieldCoverage(
        field="pricing",
        group="pricing",
        status="not_configured",
        detail="No pricing source or date was supplied, so no cost is claimed.",
    )


def coverage_matrix(
    *,
    configuration: ConfiguredTelemetry,
    timeline: TelemetryTimeline,
    signals: ExternalSignals,
) -> CoverageMatrix:
    """Every field the bundle could carry, with one of five honest statuses."""
    entries = (
        _configuration_coverage(configuration)
        + _variable_coverage(configuration.variables)
        + _signal_coverage(signals, configuration)
        + _application_coverage(timeline)
        + [_pricing_coverage(configuration.pricing)]
    )
    return CoverageMatrix(entries=tuple(sorted(entries, key=lambda entry: entry.field)))


def evidence_manifest(
    *,
    configuration: ConfiguredTelemetry,
    timeline: TelemetryTimeline,
    signals: ExternalSignals,
    joins: tuple[JoinEdge, ...],
    gaps: tuple[SignalGap, ...],
    coverage: CoverageMatrix,
    visibility: Visibility,
) -> EvidenceManifest:
    """Derive the manifest of one bundle from the parts it is made of.

    Every field is computed here and nothing is passed in as a summary, which
    is what lets `EvidenceBundle` compare a supplied manifest against this and
    refuse any difference.
    """
    counts = signals.counts
    hashes = _hashes(
        configuration=configuration,
        coverage=coverage,
        gaps=gaps,
        joins=joins,
        signals=signals,
        timeline=timeline,
    )
    return EvidenceManifest(
        telemetry_version=timeline.telemetry_version,
        visibility=visibility,
        capture_window_start=signals.capture_window_start,
        capture_window_end=signals.capture_window_end,
        config_hash=configuration.config_hash,
        redaction_policy_hash=redaction_policy_hash(),
        sources=signals.sources,
        record_counts=tuple(
            RecordCount(kind=kind, count=counts[kind]) for kind in SIGNAL_KIND_ORDER
        )
        + (RecordCount(kind="application_timeline", count=len(timeline.records)),),
        join_counts=tuple(
            JoinCount(
                basis=basis,
                edges=sum(1 for edge in joins if edge.basis == basis),
                cross_source_edges=sum(
                    1 for edge in joins if edge.basis == basis and edge.cross_source
                ),
            )
            for basis in JOIN_BASES
        ),
        pricing=configuration.pricing,
        outputs=tuple(
            OutputHash(name=name, content_hash=hashes[name]) for name in OUTPUT_NAMES
        ),
    )


def build_bundle(
    *,
    configuration: ConfiguredTelemetry,
    timeline: TelemetryTimeline,
    signals: ExternalSignals,
) -> EvidenceBundle:
    """Join one run's telemetry to its exported signals, or refuse to.

    Fail closed, in two ways that matter. An application timeline with no
    finalized completeness summary accounting for exactly its records is
    refused as a storage fault, because a partial record presented as a whole
    one is worse than none. And a record outside the declared capture window is
    refused as an invariant failure, because a bundle whose window does not
    cover its own evidence cannot be reasoned about.
    """
    validated_configuration = ConfiguredTelemetry.model_validate(configuration)
    validated_timeline = TelemetryTimeline.model_validate(timeline)
    validated_signals = ExternalSignals.model_validate(signals)
    if not validated_timeline.reconciled:
        raise RuntimeFault(Reason.STORAGE_ERROR)
    for record in validated_timeline.records:
        moment = record.correlation.timestamp
        if not (
            validated_signals.capture_window_start
            <= moment
            <= validated_signals.capture_window_end
        ):
            raise RuntimeFault(Reason.INVARIANT)
    joins = _join(validated_timeline, validated_signals)
    gaps = _gaps(validated_signals, validated_timeline, joins)
    coverage = coverage_matrix(
        configuration=validated_configuration,
        timeline=validated_timeline,
        signals=validated_signals,
    )
    return EvidenceBundle(
        manifest=evidence_manifest(
            configuration=validated_configuration,
            timeline=validated_timeline,
            signals=validated_signals,
            joins=joins,
            gaps=gaps,
            coverage=coverage,
            visibility=validated_timeline.visibility,
        ),
        configuration=validated_configuration,
        timeline=validated_timeline,
        signals=validated_signals,
        joins=joins,
        gaps=gaps,
        coverage=coverage,
    )


def public_bundle(bundle: EvidenceBundle) -> EvidenceBundle:
    """Project one private bundle onto its public form.

    Deterministic and idempotent: a bundle that is already public is returned
    unchanged rather than hashed a second time. The joins are rebuilt from the
    sanitized parts rather than copied, which is what proves the public
    projection really does preserve them.
    """
    validated = EvidenceBundle.model_validate(bundle)
    if validated.visibility == "public":
        return validated
    return build_bundle(
        configuration=validated.configuration.public(),
        timeline=sanitize_timeline(validated.timeline),
        signals=validated.signals.public(),
    )


class LiveEvidenceState(Contract):
    """Whether one bundle's evidence could support a claim about a live system.

    Every condition is carried rather than reduced to a flag, so a reader can
    tell an unconfigured deployment from one that is configured, switched on,
    and simply has not run. That distinction is the whole reason this record
    exists: a complete offline bundle and a live one differ in none of their
    counts, coverage, or hashes, and only these labels separate them.

    `live_supported` requires all of them. Offline synthetic evidence fails on
    its mode and its classification whatever else is satisfied, so a scripted
    latency can never be cited as a measurement of a deployment.
    """

    mode: Literal["offline", "live"] = "offline"
    classification: Literal["synthetic", "live", "not_supplied"] = "not_supplied"
    telemetry_enabled: bool = False
    source_provenance_complete: bool = False
    live_evidence_gate: LiveEvidenceGate = "pending"
    live_inputs_missing: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()

    @property
    def offline_synthetic(self) -> bool:
        """Whether these records say they were produced offline, synthetically."""
        return self.mode == "offline" and self.classification == "synthetic"

    @property
    def live_supported(self) -> bool:
        """Whether a claim about a live deployment may stand on this evidence."""
        return (
            self.mode == "live"
            and self.classification == "live"
            and self.telemetry_enabled
            and self.source_provenance_complete
            and self.live_evidence_gate == "established"
            and not self.live_inputs_missing
        )


def live_evidence_state(bundle: EvidenceBundle) -> LiveEvidenceState:
    """Read one bundle's live-evidence state from the bundle itself.

    The record labels dominate the configuration. A deployment may declare
    itself live, but if any record in the timeline says it ran offline, or says
    its values are synthetic, then the evidence is offline and synthetic and
    the configuration's own opinion does not enter into it.
    """
    configuration = bundle.configuration
    records = bundle.timeline.records
    offline = not records or any(record.mode == "offline" for record in records)
    synthetic = not records or any(
        record.classification == "synthetic" for record in records
    )
    return LiveEvidenceState(
        mode="offline" if offline else "live",
        classification="synthetic" if synthetic else configuration.classification,
        telemetry_enabled=configuration.observability_enabled,
        source_provenance_complete=bundle.signals.provenance_complete,
        live_evidence_gate=configuration.live_evidence_gate,
        live_inputs_missing=configuration.missing_live_fields,
    )


def hackathon_export(bundle: EvidenceBundle) -> EvidenceBundle:
    """The export gate: the public projection of a verified bundle, or nothing.

    This returns an artifact. It publishes nothing, uploads nothing, and sends
    nothing: showing it to anyone is a separate, owner-approved action.
    """
    try:
        validated = EvidenceBundle.model_validate(bundle)
    except ValidationError as error:
        raise RuntimeFault(Reason.STORAGE_ERROR) from error
    if not validated.verified:
        raise RuntimeFault(Reason.STORAGE_ERROR)
    return public_bundle(validated)
