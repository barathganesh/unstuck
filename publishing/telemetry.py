"""The public telemetry bundle, validated as typed publication evidence.

Nothing here publishes, uploads, submits, or sends anything. Publishing is a
separate owner action, and this module's whole job is the step before it: to
say whether a public telemetry bundle is the kind of artifact that could be
shown at all, and to reduce it to a record a claim can cite.

Four refusals, each one reason-coded.

A bundle that is not public is refused. A private bundle carries the account,
the Region, the runtime, the endpoint, the log group, the request identity and
the session identity in the clear, and "we meant to sanitize it" is not a
property an artifact has.

A bundle whose manifest does not bind its own parts is refused. The hash of
each part is recomputed and compared, so a part that was swapped after the
manifest was written cannot be cited.

A bundle whose records are not labelled is refused. Offline evidence is
synthetic, and synthetic evidence that does not say so reads as a measurement
of a live system.

And a bundle that still carries private identity anywhere in it is refused,
whatever its own validator concluded, because this is the last check before a
human looks at it.

The record this produces carries counts and hashes only. No figure, finding, or
rubric score originates here, which is the same rule the rest of the package
follows.

A record that passes every refusal above still supports no live claim. Live
support needs live-classified records from an enabled deployment with complete
source provenance and an explicitly established live evidence gate, and
offline synthetic evidence satisfies none of that. Complete deployment
configuration is not the question: it says the inputs exist, not that anything
was observed.
"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from publishing import PublicationFault, PublicationReason
from unstuck.domain import Contract, Digest, Identifier
from unstuck.telemetry.bundle import (
    EvidenceBundle,
    LiveEvidenceState,
    bundle_findings,
    live_evidence_state,
)
from unstuck.telemetry.contracts import unsafe_public_fields


class CoverageTally(Contract):
    """How many fields hold one coverage status.

    A tally counts rows of the coverage matrix, never observations, so a zero
    here says "no field has this status" and never "this measured zero".
    """

    status: Literal[
        "captured", "unavailable", "redacted", "not_configured", "pending_live_gate"
    ]
    fields: Annotated[int, Field(ge=0, le=64)]


class KindTally(Contract):
    """How many records of one kind the public bundle carries."""

    kind: Identifier
    records: Annotated[int, Field(ge=0, le=10**6)]


class PublicTelemetryEvidence(Contract):
    """One public telemetry bundle, reduced to what a claim may cite.

    Every hash is the bundle's own. A reader who holds the bundle can recompute
    all of them, which is what makes a claim that cites this record checkable
    rather than merely attributed.
    """

    subject: Identifier
    bundle_hash: Digest
    manifest_hash: Digest
    config_hash: Digest
    redaction_policy_hash: Digest
    capture_window_start: datetime
    capture_window_end: datetime
    record_tallies: Annotated[tuple[KindTally, ...], Field(min_length=1, max_length=8)]
    join_edges: Annotated[int, Field(ge=0, le=10**6)]
    cross_source_join_edges: Annotated[int, Field(ge=0, le=10**6)]
    unmatched_records: Annotated[int, Field(ge=0, le=10**6)]
    coverage_tallies: Annotated[
        tuple[CoverageTally, ...], Field(min_length=1, max_length=8)
    ]
    live_evidence: LiveEvidenceState
    live_inputs_missing: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.capture_window_end < self.capture_window_start:
            raise ValueError("a capture window is ordered")
        if self.cross_source_join_edges > self.join_edges:
            raise ValueError("a bundle cannot cross more sources than it has edges")
        statuses = [item.status for item in self.coverage_tallies]
        if len(set(statuses)) != len(statuses):
            raise ValueError("a coverage status is tallied twice")
        kinds = [item.kind for item in self.record_tallies]
        if len(set(kinds)) != len(kinds):
            raise ValueError("a record kind is tallied twice")
        if self.live_inputs_missing != self.live_evidence.live_inputs_missing:
            raise ValueError("a record states one account of its missing inputs")
        return self

    @property
    def live_claim_supported(self) -> bool:
        """Whether this bundle could support a claim about a live deployment.

        Complete deployment configuration is not the question. The inputs
        existing says a form was filled in; a live claim needs live-classified
        records observed on an enabled deployment, with every imported kind
        traced to a source, and the owner's live evidence gate established.
        Offline synthetic evidence fails on its own labels, which is why this
        is false for every bundle this repository can produce.
        """
        return self.live_evidence.live_supported


def validate_public_bundle(
    bundle: EvidenceBundle, *, subject: str
) -> PublicTelemetryEvidence:
    """Accept one public telemetry bundle as evidence, or refuse it.

    Returning a record rather than a verdict is deliberate: a claim cites the
    record, and the record names the hashes the claim stands on.
    """
    try:
        validated = EvidenceBundle.model_validate(bundle)
    except ValidationError as error:
        raise PublicationFault(
            PublicationReason.EVIDENCE_SCHEMA_INVALID,
            "the telemetry bundle does not validate as a bundle",
        ) from error
    if validated.visibility != "public":
        raise PublicationFault(
            PublicationReason.EVIDENCE_SCHEMA_INVALID,
            "a telemetry bundle is sanitized before it is evidence",
        )
    findings = bundle_findings(validated)
    if findings:
        raise PublicationFault(
            PublicationReason.EVIDENCE_HASH_MISMATCH,
            "; ".join(findings[:4]),
        )
    if any(
        record.classification != "synthetic" or record.mode != "offline"
        for record in validated.timeline.records
    ):
        raise PublicationFault(
            PublicationReason.LABEL_MISSING,
            "every offline telemetry record is labelled synthetic",
        )
    unsafe = unsafe_public_fields(validated)
    if unsafe:
        raise PublicationFault(
            PublicationReason.EVIDENCE_SCHEMA_INVALID,
            "the public telemetry bundle retains private identity",
        )
    manifest = validated.manifest
    counts = validated.coverage.counts
    return PublicTelemetryEvidence(
        subject=subject,
        bundle_hash=validated.bundle_hash,
        manifest_hash=manifest.manifest_hash,
        config_hash=manifest.config_hash,
        redaction_policy_hash=manifest.redaction_policy_hash,
        capture_window_start=manifest.capture_window_start,
        capture_window_end=manifest.capture_window_end,
        record_tallies=tuple(
            KindTally(kind=item.kind, records=item.count)
            for item in manifest.record_counts
        ),
        join_edges=sum(item.edges for item in manifest.join_counts),
        cross_source_join_edges=sum(
            item.cross_source_edges for item in manifest.join_counts
        ),
        unmatched_records=sum(gap.unmatched for gap in validated.gaps),
        coverage_tallies=tuple(
            CoverageTally(status=status, fields=fields)  # type: ignore[arg-type]
            for status, fields in sorted(counts.items())
        ),
        live_evidence=live_evidence_state(validated),
        live_inputs_missing=validated.configuration.missing_live_fields,
    )
