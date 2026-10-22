"""The public recipe carries the product's card and receipt contract. Offline.

Repair wave 3, finding 4: the shipped recipe's `ToolCard` had no preconditions
and no contract version, and its `Receipt` omitted the decision, model-call and
diagnostic-call identities, the input hash, the post-call state hash, the
before and after evidence identities, the schema and tool-contract versions,
and the observation time. The conformance suite compared the outcome taxonomy,
the bounds and the blocking conjunction, so none of those absences failed a
test, and an adopter could not reconstruct which model call selected the read,
what input it saw, or what state the read left behind.

Parity is asserted structurally and by field, through an explicit mapping. The
recipe is still imported from a copy of its own directory with nothing else
importable, so this test proves the contract agrees without the recipe
depending on the application in either direction.
"""

from dataclasses import fields, replace
from types import ModuleType

import pytest
from test_conformance import PREFERRED, _catalog, _context, _result

from unstuck.cascade.cards import ToolCapabilityCard
from unstuck.cascade.receipts import DecisionReceipt

#: Product card field to the recipe field that carries the same meaning.
#: `catalog_version` is bound per decision in the recipe rather than per card,
#: so it is named on `Versions`; nothing is excluded.
CARD_PARITY: dict[str, str] = {
    "public_tool_id": "tool_id",
    "description": "description",
    # The product publishes one condition sentence per card; the recipe
    # publishes a tuple of conditions. Both are the public statement of when
    # observing that read distinguishes something, stated as observable
    # conditions rather than as advice about choosing.
    "applies_when": "preconditions",
    "input_schema": "input_schema",
    "observable_facts": "observable",
    "cost_class": "cost_class",
    "tool_contract_version": "contract_version",
}
CARD_ON_VERSIONS: dict[str, str] = {"catalog_version": "catalog_version"}

#: Product receipt field to the recipe field, or fields, carrying it.
RECEIPT_PARITY: dict[str, tuple[str, ...]] = {
    "receipt_id": ("receipt_id",),
    "decision_id": ("decision_id",),
    "model_call_id": ("model_call_id",),
    "diagnostic_call_id": ("diagnostic_call_id",),
    "problem_fingerprint": ("problem_fingerprint",),
    "decision_context_hash": ("context_digest",),
    "selected_capability": ("capability",),
    "public_tool_id": ("public_tool_id",),
    "arguments_hash": ("arguments_digest",),
    "delta": (
        "evidence_before_digests",
        "evidence_after_digests",
        "evidence_changed",
        "disposition_moved",
    ),
    "outcome_class": ("outcome",),
    "reason_code": ("reason",),
    "versions": ("versions", "versions_digest"),
    "observed_at": ("observed_at",),
}
#: The one product field with no recipe counterpart, and why. The recipe ships
#: no arm or case isolation: an adopter supplies their own store, so the
#: namespace is theirs to choose rather than a field of this contract.
RECEIPT_CONTROLLER_ONLY = frozenset({"namespace"})

#: What the review requires on the recipe beyond the product's own field names.
REQUIRED_RECEIPT_FIELDS = (
    "decision_id",
    "model_call_id",
    "diagnostic_call_id",
    "input_digest",
    "post_call_state_digest",
    "evidence_before_digests",
    "evidence_after_digests",
    "observed_at",
)
REQUIRED_VERSION_FIELDS = (
    "data_version",
    "data_schema_version",
    "tool_contract_version",
    "catalog_version",
    "model_revision",
    "policy_version",
)


def names(kind: type) -> frozenset[str]:
    return frozenset(field.name for field in fields(kind))


def product_fields(contract: type) -> frozenset[str]:
    return frozenset(contract.model_fields) - {"schema_version"}  # type: ignore[attr-defined]


def receipt(module: ModuleType, **changes: object) -> object:
    catalog = _catalog(module)
    context = _context(module, catalog, evidence_digests=("held",))
    return module.make_receipt(
        context,
        catalog.card(PREFERRED),
        _result(module, "no_progress", context.arguments_digest),
        **changes,
    )


def test_every_product_card_field_has_a_recipe_counterpart_offline(
    isolated_selector: ModuleType,
) -> None:
    recipe = names(isolated_selector.ToolCard)
    versions = names(isolated_selector.Versions)
    for product, mapped in CARD_PARITY.items():
        assert product in product_fields(ToolCapabilityCard), product
        assert mapped in recipe, (product, mapped)
    for product, mapped in CARD_ON_VERSIONS.items():
        assert product in product_fields(ToolCapabilityCard), product
        assert mapped in versions, (product, mapped)
    unmapped = (
        product_fields(ToolCapabilityCard) - set(CARD_PARITY) - set(CARD_ON_VERSIONS)
    )
    assert unmapped == frozenset(), unmapped


def test_the_published_card_carries_its_preconditions_and_version_offline(
    isolated_selector: ModuleType,
) -> None:
    card = isolated_selector.ToolCard(
        tool_id="inspect_scope",
        capability="scope",
        description="Report scope match.",
        observable=("scope_matches",),
        preconditions=("the task names a scope",),
    )
    public = card.public()
    assert public["preconditions"] == ["the task names a scope"]
    assert public["contract_version"] == isolated_selector.TOOL_CONTRACT_VERSION
    assert public["input_schema"] == "no_arguments"
    assert public["cost_class"] == "local_read"
    assert public["observable"] == ["scope_matches"]
    # The binding stays controller-side: the card that leaves never carries it.
    assert "capability" not in public
    assert card.capability == "scope"


def test_every_product_receipt_field_has_a_recipe_counterpart_offline(
    isolated_selector: ModuleType,
) -> None:
    recipe = names(isolated_selector.Receipt) | {"receipt_id"}
    for product, mapped in RECEIPT_PARITY.items():
        assert product in product_fields(DecisionReceipt), product
        for name in mapped:
            assert name in recipe, (product, name)
    unmapped = (
        product_fields(DecisionReceipt) - set(RECEIPT_PARITY) - RECEIPT_CONTROLLER_ONLY
    )
    assert unmapped == frozenset(), unmapped


def test_the_recipe_receipt_carries_every_required_binding_offline(
    isolated_selector: ModuleType,
) -> None:
    recipe = names(isolated_selector.Receipt)
    for name in REQUIRED_RECEIPT_FIELDS:
        assert name in recipe, name
    versions = names(isolated_selector.Versions)
    for name in REQUIRED_VERSION_FIELDS:
        assert name in versions, name


def test_a_written_receipt_fills_every_binding_offline(
    isolated_selector: ModuleType,
) -> None:
    written = receipt(isolated_selector)
    assert len(written.decision_id) == 64  # type: ignore[attr-defined]
    assert len(written.model_call_id) == 64  # type: ignore[attr-defined]
    assert len(str(written.diagnostic_call_id)) == 64  # type: ignore[attr-defined]
    assert len(written.input_digest) == 64  # type: ignore[attr-defined]
    assert len(written.post_call_state_digest) == 64  # type: ignore[attr-defined]
    assert written.evidence_before_digests == ("held",)  # type: ignore[attr-defined]
    assert written.evidence_after_digests == ("held",)  # type: ignore[attr-defined]
    assert written.versions.data_schema_version  # type: ignore[attr-defined]
    assert written.versions.tool_contract_version  # type: ignore[attr-defined]
    assert written.observed_at == isolated_selector.UNRECORDED_OBSERVATION  # type: ignore[attr-defined]


def test_a_supplied_trace_and_input_are_what_the_receipt_binds_offline(
    isolated_selector: ModuleType,
) -> None:
    trace = isolated_selector.CallTrace(
        decision_id="decision-7", model_call_id="call-7", diagnostic_call_id="read-7"
    )
    written = receipt(
        isolated_selector,
        trace=trace,
        input_digest="the-input-the-model-saw",
        observed_at="2026-10-19T00:00:00+00:00",
    )
    assert written.decision_id == "decision-7"  # type: ignore[attr-defined]
    assert written.model_call_id == "call-7"  # type: ignore[attr-defined]
    assert written.diagnostic_call_id == "read-7"  # type: ignore[attr-defined]
    assert written.input_digest == "the-input-the-model-saw"  # type: ignore[attr-defined]
    assert written.observed_at == "2026-10-19T00:00:00+00:00"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decision_id", "another-decision"),
        ("model_call_id", "another-call"),
        ("diagnostic_call_id", "another-read"),
        ("input_digest", "another-input"),
        ("post_call_state_digest", "another-state"),
        ("evidence_after_digests", ("held", "admitted")),
        ("public_tool_id", "a_renamed_tool"),
        ("arguments_digest", "another-argument-set"),
        ("context_digest", "another-context"),
        ("versions_digest", "another-version-set"),
    ],
)
def test_receipt_identity_covers_the_whole_binding_set_offline(
    isolated_selector: ModuleType, field: str, value: object
) -> None:
    written = receipt(isolated_selector)
    changes: dict[str, object] = {field: value}
    if field == "evidence_after_digests":
        changes["evidence_changed"] = True
    assert replace(written, **changes).receipt_id != written.receipt_id, field  # type: ignore[type-var]


def test_the_observation_instant_is_never_part_of_identity_offline(
    isolated_selector: ModuleType,
) -> None:
    """A receipt is identified by content, so the same fact twice is one row."""
    written = receipt(isolated_selector)
    stamped = replace(written, observed_at="2026-10-19T12:00:00Z")  # type: ignore[type-var]
    assert stamped.receipt_id == written.receipt_id
    store = isolated_selector.ReceiptStore()
    assert store.put(written) is True
    assert store.put(stamped) is False
    assert len(store.rows) == 1


def test_an_observation_instant_outside_utc_is_refused_offline(
    isolated_selector: ModuleType,
) -> None:
    with pytest.raises(isolated_selector.SelectionRefused) as raised:
        receipt(isolated_selector, observed_at="2026-10-19T12:00:00+02:00")
    assert raised.value.reason is isolated_selector.SelectionError.RESULT_INVALID


def test_a_verified_outcome_names_the_read_it_came_from_offline(
    isolated_selector: ModuleType,
) -> None:
    written = receipt(isolated_selector)
    with pytest.raises(isolated_selector.SelectionRefused):
        replace(written, diagnostic_call_id=None)  # type: ignore[type-var]
    # A read that never ran has no read identity to name, and says so.
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    failed = isolated_selector.make_receipt(
        context,
        catalog.card(PREFERRED),
        _result(isolated_selector, "provider_error", context.arguments_digest),
    )
    assert failed.outcome == "provider_error"
    assert failed.diagnostic_call_id is None


def test_a_receipt_cannot_report_withdrawn_evidence_offline(
    isolated_selector: ModuleType,
) -> None:
    written = receipt(isolated_selector)
    with pytest.raises(isolated_selector.SelectionRefused) as raised:
        replace(written, evidence_after_digests=())  # type: ignore[type-var]
    assert raised.value.reason is isolated_selector.SelectionError.RESULT_INVALID
