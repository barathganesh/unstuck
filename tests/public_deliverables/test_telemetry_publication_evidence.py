"""The public telemetry bundle as publication evidence. Written first. Offline.

Nothing here publishes, uploads, or submits anything. These tests check the
step before that: whether a telemetry bundle is the kind of artifact that could
be shown at all, and what a claim is allowed to cite from it.

Four refusals are pinned. A private bundle is refused outright, because "we
meant to sanitize it" is not a property an artifact has. A bundle whose
manifest no longer binds its parts is refused, so a part swapped after the fact
cannot be cited. A record that is not labelled synthetic is refused, because
unlabelled offline evidence reads as a measurement of a live system. And the
record this produces carries counts and hashes only, so no figure originates in
the publication path.

The defect pinned on top of those is the live claim. Support used to follow
from the deployment configuration being complete, so a fully configured
offline bundle was live-claim-capable and this file asserted it. Support now
requires live-classified records from an enabled deployment, complete source
provenance, and an explicitly established live evidence gate, which no bundle
this repository produces satisfies.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/public_deliverables/test_telemetry_evidence.py -q
"""

import pytest
from pydantic import ValidationError

from publishing import PublicationFault, PublicationReason
from publishing.telemetry import PublicTelemetryEvidence, validate_public_bundle
from tests.telemetry.placeholders import (
    SYNTHETIC_ACCOUNT,
    SYNTHETIC_LOG_GROUP,
    SYNTHETIC_RUNTIME_ARN,
)
from tests.telemetry.test_telemetry_bundle import bundle, timeline
from tests.telemetry.test_telemetry_evidence_invariants import public_unreconciled
from tests.telemetry.test_telemetry_signals import REQUEST, SESSION, SPAN, TRACE
from unstuck.cloud.telemetry import REQUIRED_LIVE_FIELDS, TelemetryRuntimeConfig
from unstuck.telemetry.bundle import (
    LiveEvidenceState,
    hackathon_export,
    public_bundle,
)

SUBJECT = "telemetry-evidence-bundle"


def evidence() -> PublicTelemetryEvidence:
    return validate_public_bundle(hackathon_export(bundle()), subject=SUBJECT)


def test_a_private_bundle_is_never_evidence_offline() -> None:
    with pytest.raises(PublicationFault) as raised:
        validate_public_bundle(bundle(), subject=SUBJECT)
    assert raised.value.reason is PublicationReason.EVIDENCE_SCHEMA_INVALID


def test_a_public_bundle_reduces_to_counts_and_hashes_offline() -> None:
    record = evidence()
    assert record.subject == SUBJECT
    assert record.bundle_hash == public_bundle(bundle()).bundle_hash
    assert len(record.manifest_hash) == 64
    assert len(record.redaction_policy_hash) == 64
    assert record.join_edges >= record.cross_source_join_edges
    assert {tally.kind for tally in record.record_tallies} >= {
        "application_timeline",
        "otel_spans",
    }


def test_the_evidence_record_carries_no_environment_identity_offline() -> None:
    body = evidence().model_dump_json()
    for raw in (
        SYNTHETIC_ACCOUNT,
        SYNTHETIC_LOG_GROUP,
        SYNTHETIC_RUNTIME_ARN,
        TRACE,
        SPAN,
        SESSION,
        REQUEST,
    ):
        assert raw not in body


def test_every_coverage_status_is_tallied_exactly_once_offline() -> None:
    tallies = evidence().coverage_tallies
    statuses = [tally.status for tally in tallies]
    assert len(set(statuses)) == len(statuses)
    assert set(statuses) == {
        "captured",
        "unavailable",
        "redacted",
        "not_configured",
        "pending_live_gate",
    }


def test_a_swapped_part_is_refused_with_a_hash_reason_offline() -> None:
    exported = hackathon_export(bundle())
    broken = type(exported).model_construct(**(exported.model_dump() | {"joins": ()}))
    with pytest.raises(PublicationFault) as raised:
        validate_public_bundle(broken, subject=SUBJECT)
    assert raised.value.reason in {
        PublicationReason.EVIDENCE_HASH_MISMATCH,
        PublicationReason.EVIDENCE_SCHEMA_INVALID,
    }


def test_an_unreconciled_record_is_never_evidence_offline() -> None:
    exported = hackathon_export(bundle())
    broken = type(exported).model_construct(
        **(exported.model_dump() | {"timeline": timeline(reconciled=False)})
    )
    with pytest.raises(PublicationFault):
        validate_public_bundle(broken, subject=SUBJECT)


def test_an_unconfigured_deployment_supports_no_live_claim_offline() -> None:
    record = validate_public_bundle(
        hackathon_export(bundle(configuration=TelemetryRuntimeConfig().configured())),
        subject=SUBJECT,
    )
    assert record.live_claim_supported is False
    assert set(record.live_inputs_missing) == set(REQUIRED_LIVE_FIELDS)


def test_a_fully_configured_offline_bundle_supports_no_live_claim_offline() -> None:
    """Regression: complete configuration used to be read as live support.

    Every deployment input is supplied here and nothing is missing, so the old
    test asserted live support. The inputs existing says a form was filled in;
    the records still say they were produced offline and synthetically.
    """
    record = evidence()
    assert record.live_inputs_missing == ()
    assert record.live_evidence.offline_synthetic is True
    assert record.live_evidence.live_supported is False
    assert record.live_claim_supported is False
    assert record.unmatched_records >= 0


def test_the_evidence_record_carries_every_live_condition_offline() -> None:
    """The conditions travel with the record, so a reader need not infer them."""
    state = evidence().live_evidence
    assert state.mode == "offline"
    assert state.classification == "synthetic"
    assert state.telemetry_enabled is True
    assert state.source_provenance_complete is True
    assert state.live_evidence_gate == "pending"
    assert state.live_inputs_missing == ()


def test_a_disabled_or_unproven_deployment_supports_no_live_claim_offline() -> None:
    """Each live condition is withdrawn in turn from an otherwise live state."""
    record = evidence()

    def supported(**changes: object) -> bool:
        state = LiveEvidenceState.model_validate(
            {
                "mode": "live",
                "classification": "live",
                "telemetry_enabled": True,
                "source_provenance_complete": True,
                "live_evidence_gate": "established",
                "live_inputs_missing": (),
            }
            | changes
        )
        return PublicTelemetryEvidence.model_validate(
            record.model_dump()
            | {
                "live_evidence": state.model_dump(),
                "live_inputs_missing": state.live_inputs_missing,
            }
        ).live_claim_supported

    assert supported() is True
    assert supported(telemetry_enabled=False) is False
    assert supported(classification="synthetic") is False
    assert supported(mode="offline") is False
    assert supported(source_provenance_complete=False) is False
    assert supported(live_evidence_gate="pending") is False
    assert supported(live_inputs_missing=("log_group",)) is False


def test_a_record_disagreeing_with_itself_about_missing_inputs_is_refused() -> None:
    record = evidence()
    with pytest.raises(ValidationError):
        PublicTelemetryEvidence.model_validate(
            record.model_dump() | {"live_inputs_missing": ("log_group",)}
        )


def test_the_record_itself_originates_no_figure_offline() -> None:
    record = evidence()
    dumped = record.model_dump()
    numeric = {
        name
        for name, value in dumped.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    assert numeric <= {"join_edges", "cross_source_join_edges", "unmatched_records"}


def test_a_public_bundle_over_a_partial_record_is_refused_offline() -> None:
    with pytest.raises(PublicationFault) as raised:
        validate_public_bundle(public_unreconciled(), subject=SUBJECT)
    assert raised.value.reason is PublicationReason.EVIDENCE_HASH_MISMATCH


def test_an_inverted_window_in_the_record_is_refused_offline() -> None:
    record = evidence()
    with pytest.raises(ValidationError):
        PublicTelemetryEvidence.model_validate(
            record.model_dump()
            | {
                "capture_window_start": record.capture_window_end,
                "capture_window_end": record.capture_window_start,
            }
        )


def test_a_record_crossing_more_sources_than_it_has_edges_is_refused_offline() -> None:
    record = evidence()
    with pytest.raises(ValidationError):
        PublicTelemetryEvidence.model_validate(
            record.model_dump() | {"cross_source_join_edges": record.join_edges + 1}
        )


def test_a_repeated_status_or_kind_tally_is_refused_offline() -> None:
    record = evidence()
    with pytest.raises(ValidationError):
        PublicTelemetryEvidence.model_validate(
            record.model_dump()
            | {"coverage_tallies": (record.coverage_tallies[0],) * 2}
        )
    with pytest.raises(ValidationError):
        PublicTelemetryEvidence.model_validate(
            record.model_dump() | {"record_tallies": (record.record_tallies[0],) * 2}
        )
