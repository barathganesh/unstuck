"""Public identity, and why a value's shape never buys it an exemption.

Two defects are pinned here. The first is that the experiment and case labels
used to survive sanitization unchanged, so a customer, tenant, user, or case
name published as written. The second is that the public scan used to exempt
any lowercase hex string of digest width and any UUID-shaped string, whatever
field it sat in, so a credential or an external user identifier in a free
identifier or configuration field was treated as opaque.

The fix is the same idea twice: opacity is a property of the declared field
type, never of the value that arrived. A field is skipped because the schema
admits nothing but a hash, a trace identity, or a minted identifier there, and
anything else is scanned however opaque it looks.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_public_identity.py -q
"""

import json
from datetime import UTC, datetime
from io import StringIO
from uuid import uuid4

import pytest
from pydantic import ValidationError

from unstuck.telemetry import (
    Correlation,
    CorrelationContext,
    DecisionTelemetry,
    InfrastructureDimensions,
    Measurements,
    PricingProvenance,
    PublicLineSink,
    TelemetryRecord,
    public_correlation,
    public_digest,
    public_identity,
    public_line_payload,
    sanitize,
    unsafe_public_fields,
    unsafe_public_values,
)

DIGEST = "a" * 64

#: Model revisions a decision could plausibly name. None of them matches a
#: forbidden pattern, so nothing but hashing the declared field removes them.
MODEL_REVISIONS = ("scripted-v1", "example-provider-model-4-5-20260101")

#: Values a caller could plausibly put in a free label. None of them matches a
#: forbidden pattern, so nothing but hashing the typed field removes them.
IDENTITY_LABELS = (
    "customer-acme",
    "acct-900123",
    "user_8f3a",
    "tenant-northwind",
)

#: Values whose shape used to buy an exemption. Each one carries an account
#: identifier, and each one sits in a field whose type is free text.
SHAPED_SECRETS = (
    "f" * 48 + "123456789012" + "abcd",
    "a5d0a854-4373-512c-a639-123456789012",
    "123456789012",
    "0123456789ab123456789012cdef0123",
)


def correlation(**changes: object) -> Correlation:
    base: dict[str, object] = {
        "run_id": uuid4(),
        "event_id": uuid4(),
        "sequence": 1,
        "timestamp": datetime.now(UTC),
    }
    return Correlation.model_validate(base | changes)


def record(**changes: object) -> TelemetryRecord:
    base: dict[str, object] = {"kind": "probe_started", "correlation": correlation()}
    return TelemetryRecord.model_validate(base | changes)


def priced(source: str) -> Measurements:
    return Measurements(
        pricing=PricingProvenance(
            availability="measured",
            pricing_date="2026-10-20",
            source=source,
            currency="USD",
            unit="total_call",
        )
    )


@pytest.mark.parametrize("label", IDENTITY_LABELS)
def test_security_a_free_case_or_experiment_label_is_hashed_not_kept(label):
    """Regression: an identity-shaped label used to publish unchanged.

    The schema types both labels as free identifiers, so no check can prove a
    given value is a synthetic label rather than a real name. They are hashed,
    which keeps a public bundle groupable without naming anyone.
    """
    for field in ("experiment_id", "case_id"):
        raw = correlation(**{field: label})
        public = public_correlation(raw)
        assert getattr(public, field) is None
        assert getattr(public, f"{field}_hash") == public_digest(label)
        assert label not in public.model_dump_json()
        # The contract refuses the raw value too, so skipping the sanitizer
        # does not get the label through.
        assert raw.identity_bearing
        with pytest.raises(ValidationError):
            record(visibility="public", correlation=raw)


def test_security_a_sanitized_record_keeps_no_case_identity_anywhere():
    private = record(
        correlation=correlation(experiment_id="exp-1", case_id="customer-acme"),
        infrastructure=InfrastructureDimensions(
            source="runtime_configuration", region="eu-west-1"
        ),
    )
    public = sanitize(private)
    rendered = public.model_dump_json()
    assert "customer-acme" not in rendered and "exp-1" not in rendered
    assert public.correlation.case_id_hash == public_digest("customer-acme")
    assert public.correlation.experiment_id_hash == public_digest("exp-1")
    assert unsafe_public_fields(public) == ()


def test_the_supplied_context_still_carries_the_raw_labels_privately():
    """The private record is the complete evidence, so nothing is lost."""
    context = CorrelationContext(experiment_id="exp-1", case_id="case-01")
    supplied = context.model_dump()
    supplied.pop("schema_version", None)
    private = record(correlation=correlation(**supplied))
    assert private.visibility == "private"
    assert private.correlation.case_id == "case-01"
    assert private.correlation.experiment_id == "exp-1"


@pytest.mark.parametrize("value", SHAPED_SECRETS)
def test_security_an_opaque_looking_value_in_a_free_field_is_still_refused(value):
    """Regression: digest width and UUID shape used to bypass the scan.

    Each value here would have passed a shape test. The pricing source is
    typed as free configuration text, so the typed walk scans it and the public
    contract refuses the record.
    """
    assert unsafe_public_values(value) == (value,)
    leaked = record(measurements=priced(value))
    assert unsafe_public_fields(leaked) == (value,)
    with pytest.raises(ValidationError):
        record(visibility="public", measurements=priced(value))


def test_security_a_digest_typed_field_is_skipped_by_its_type_alone():
    """A hash is exempt because its type admits nothing else.

    The exemption is checked against a digest whose own characters contain an
    account identifier, which is the case a shape test would get wrong in the
    other direction: it must still be skipped, because the field cannot hold
    anything but a hash.
    """
    shaped = "f" * 48 + "123456789012" + "abcd"
    public = record(
        visibility="public",
        correlation=correlation(trace_id_hash=shaped, case_id_hash=DIGEST),
    )
    assert unsafe_public_fields(public) == ()
    assert public.correlation.trace_id_hash == shaped


def test_security_a_minted_identity_is_skipped_because_the_field_is_typed_uuid():
    """A run of fresh records cannot fail an export at random."""
    for _ in range(300):
        assert unsafe_public_fields(sanitize(record())) == ()


@pytest.mark.parametrize("revision", MODEL_REVISIONS)
def test_security_a_decision_model_revision_is_hashed_not_kept(revision):
    """Regression: the source model identity used to publish unchanged.

    `IDENTITY_TREATMENT` declares `model_revision` as hashed, and an imported
    span and an exported invocation already projected it that way. The record's
    own decision did not, so the one place the product names the model it asked
    survived into a public line as written.
    """
    private = record(decision=DecisionTelemetry(model_revision=revision))
    public = sanitize(private)

    assert public.decision is not None
    assert public.decision.model_revision is None
    assert public.decision.model_revision_hash == public_identity(
        "model_revision", revision, domain="model"
    )
    # The domain separation is what keeps a model label from matching a
    # correlation identity that happens to read the same.
    assert public.decision.model_revision_hash != public_digest(revision)
    assert revision not in public.model_dump_json()
    assert unsafe_public_fields(public) == ()


def test_security_a_written_public_line_carries_no_source_model_identity():
    """The sink writes the projection, so the line carries the hash only."""
    stream = StringIO()
    revision = MODEL_REVISIONS[1]

    PublicLineSink(stream).emit(
        record(decision=DecisionTelemetry(model_revision=revision))
    )

    assert revision not in stream.getvalue()
    payload = public_line_payload(stream.getvalue().rstrip("\n"))
    assert payload is not None
    written = json.loads(payload)
    assert "model_revision" not in written["decision"]
    assert written["decision"]["model_revision_hash"] == public_identity(
        "model_revision", revision, domain="model"
    )


def test_the_typed_walk_scans_an_untyped_mapping_key_and_value_alike():
    """A mapping has no declared element type, so neither side is exempt."""
    assert unsafe_public_fields({"/home/builder": "arn:aws:s3:::bucket"}) == (
        "/home/builder",
        "arn:aws:s3:::bucket",
    )
    assert unsafe_public_fields({"safe": ["ok", 7, None]}) == ()
    assert unsafe_public_fields(None) == ()
