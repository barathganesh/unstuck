"""The recipe's typed surface and the pattern's behaviour. Offline.

Every test runs against the recipe imported from a copy of its own directory,
and the behaviour tests run twice: once per catalog, with different tool
identifiers and the same capabilities.
"""

import json
from types import ModuleType
from typing import Any

import pytest
from recipe_paths import RECIPE


def build(module: ModuleType, names: tuple[str, str, str]) -> Any:
    """A catalog with the given public identifiers over the same capabilities."""
    pipeline, keys, scope = names
    return module.Catalog(
        cards=(
            module.ToolCard(
                tool_id=pipeline,
                capability="pipeline",
                description="Report upstream completion.",
                observable=("job_state",),
            ),
            module.ToolCard(
                tool_id=keys,
                capability="schema_keys",
                description="Report key encodings.",
                observable=("key_encoding",),
            ),
            module.ToolCard(
                tool_id=scope,
                capability="scope",
                description="Report scope match.",
                observable=("scope_matches",),
            ),
        )
    )


CATALOGS = (
    ("inspect_pipeline", "inspect_schema_keys", "inspect_scope"),
    ("check_feed_state", "check_key_shapes", "check_request_scope"),
)


def context_for(module: ModuleType, catalog: Any, **changes: Any) -> Any:
    versions = module.Versions(
        data_version="v17",
        model_revision="rev-1",
        catalog_version="cards-v1",
        policy_version="policy-v1",
    )
    base: dict[str, Any] = {
        "problem_id": "p-1",
        "task_digest": module.canonical_digest({"task": 1}),
        "versions": versions,
        "arguments_digest": module.canonical_digest({}),
        "candidates": tuple(sorted(card.tool_id for card in catalog.cards)),
    }
    return module.ProblemContext(**(base | changes))


def always(module: ModuleType, tool_id: str, confidence: float = 0.95) -> Any:
    def call(payload: dict[str, Any]) -> Any:
        return module.Choice(tool_id=tool_id, confidence=confidence)

    return call


def test_a_card_never_exposes_the_private_capability_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = build(isolated_selector, CATALOGS[0])
    for card in catalog.cards:
        public = card.public()
        assert "capability" not in public
        assert public["tool_id"] == card.tool_id
    available = frozenset(card.tool_id for card in catalog.cards)
    rendered = json.dumps(catalog.public(available))
    for capability in ("pipeline", "schema_keys", "scope"):
        assert f'"capability": "{capability}"' not in rendered


def test_a_renamed_tool_resolves_to_the_same_capability_offline(
    isolated_selector: ModuleType,
) -> None:
    first = build(isolated_selector, CATALOGS[0])
    second = build(isolated_selector, CATALOGS[1])
    assert first.capability_of("inspect_schema_keys") == "schema_keys"
    assert second.capability_of("check_key_shapes") == "schema_keys"
    with pytest.raises(isolated_selector.SelectionRefused) as raised:
        first.card("not_a_tool")
    assert raised.value.reason is isolated_selector.SelectionError.UNKNOWN_TOOL


def test_a_receipt_is_identified_by_content_not_by_time_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = build(isolated_selector, CATALOGS[0])
    context = context_for(isolated_selector, catalog)
    result = isolated_selector.ToolResult(
        arguments_digest=context.arguments_digest,
        ran=True,
        evidence_changed=False,
        admissible=True,
        reason="nothing admitted",
    )
    card = catalog.card("inspect_schema_keys")
    first = isolated_selector.make_receipt(context, card, result)
    second = isolated_selector.make_receipt(context, card, result)
    assert first.receipt_id == second.receipt_id
    rendered = json.dumps(first.__dict__, default=str).lower()
    for term in ("time", "date", "expires", "ttl"):
        assert term not in rendered, term


def test_the_outcome_is_classified_by_code_not_the_model_offline(
    isolated_selector: ModuleType,
) -> None:
    def result(**changes: Any) -> Any:
        base: dict[str, Any] = {
            "arguments_digest": "d",
            "ran": True,
            "evidence_changed": True,
            "admissible": True,
            "reason": "r",
        }
        return isolated_selector.ToolResult(**(base | changes))

    classify = isolated_selector.classify_outcome
    # Both dimensions decide the class: a read that admitted a fact is useful
    # only if that fact moved a gate, and disposition-neutral otherwise.
    assert classify(result(disposition_moved=True)) == "useful_evidence"
    assert classify(result()) == "disposition_unchanged"
    assert classify(result(evidence_changed=False)) == "no_progress"
    # A read that did not run, or whose evidence was inadmissible, admitted
    # nothing: the result contract refuses the contradictory combination.
    assert classify(result(ran=False, evidence_changed=False)) == "provider_error"
    assert (
        classify(result(admissible=False, evidence_changed=False)) == "invalid_evidence"
    )
    with pytest.raises(isolated_selector.SelectionRefused):
        result(ran=False, evidence_changed=True)
    assert isolated_selector.BLOCKING_OUTCOMES == frozenset(
        {"no_progress", "disposition_unchanged"}
    )


@pytest.mark.parametrize("names", CATALOGS)
def test_a_first_decision_runs_from_current_state_alone_offline(
    isolated_selector: ModuleType, names: tuple[str, str, str]
) -> None:
    catalog = build(isolated_selector, names)
    context = context_for(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    available = frozenset(card.tool_id for card in catalog.cards)
    outcome = isolated_selector.select_tool(
        context, catalog, available, store, always(isolated_selector, names[1])
    )
    assert outcome.tool_id == names[1]
    assert outcome.capability == "schema_keys"
    assert outcome.decisions == 1
    assert outcome.refusals == ()
    assert outcome.history_classes == ()


@pytest.mark.parametrize("names", CATALOGS)
def test_an_exact_no_progress_receipt_blocks_the_repeat_offline(
    isolated_selector: ModuleType, names: tuple[str, str, str]
) -> None:
    catalog = build(isolated_selector, names)
    context = context_for(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    available = frozenset(card.tool_id for card in catalog.cards)
    store.put(
        isolated_selector.make_receipt(
            context,
            catalog.card(
                [
                    card.tool_id
                    for card in catalog.cards
                    if card.capability == "schema_keys"
                ][0]
            ),
            isolated_selector.ToolResult(
                arguments_digest=context.arguments_digest,
                ran=True,
                evidence_changed=False,
                admissible=True,
                reason="nothing admitted",
            ),
        )
    )
    seen: list[dict[str, Any]] = []

    def stubborn(payload: dict[str, Any]) -> Any:
        seen.append(payload)
        offered = [item["tool_id"] for item in payload["tools"] if item["available"]]
        wanted = names[1] if names[1] in offered else names[0]
        return isolated_selector.Choice(tool_id=wanted, confidence=0.95)

    outcome = isolated_selector.select_tool(
        context, catalog, available, store, stubborn
    )
    assert outcome.blocked_tool_id == names[1]
    assert tuple(item.kind for item in outcome.precluded) == ("exact_repeat",)
    assert outcome.refusals == ()
    assert outcome.decisions == 1 == len(seen), "one decision, over what remains"
    assert outcome.tool_id == names[0]
    assert outcome.capability != "schema_keys"
    assert outcome.history_classes == ("exact",)


@pytest.mark.parametrize("names", CATALOGS)
def test_a_provider_error_receipt_never_blocks_offline(
    isolated_selector: ModuleType, names: tuple[str, str, str]
) -> None:
    catalog = build(isolated_selector, names)
    context = context_for(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    available = frozenset(card.tool_id for card in catalog.cards)
    store.put(
        isolated_selector.make_receipt(
            context,
            catalog.card(
                [
                    card.tool_id
                    for card in catalog.cards
                    if card.capability == "schema_keys"
                ][0]
            ),
            isolated_selector.ToolResult(
                arguments_digest=context.arguments_digest,
                ran=False,
                evidence_changed=False,
                admissible=False,
                reason="transport failed",
            ),
        )
    )
    outcome = isolated_selector.select_tool(
        context, catalog, available, store, always(isolated_selector, names[1])
    )
    assert outcome.refusals == ()
    assert outcome.tool_id == names[1]
    assert outcome.decisions == 1


def test_changed_current_facts_stop_an_old_receipt_blocking_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = build(isolated_selector, CATALOGS[0])
    context = context_for(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    store.put(
        isolated_selector.make_receipt(
            context,
            catalog.card(
                [
                    card.tool_id
                    for card in catalog.cards
                    if card.capability == "schema_keys"
                ][0]
            ),
            isolated_selector.ToolResult(
                arguments_digest=context.arguments_digest,
                ran=True,
                evidence_changed=False,
                admissible=True,
                reason="nothing admitted",
            ),
        )
    )
    moved = context_for(isolated_selector, catalog, evidence_digests=("new-evidence",))
    assert store.blocked_keys(moved) == set(), "current facts dominate history"
    receipts = store.matching(moved)
    assert [receipt.match(moved) for receipt in receipts] == ["compatible"]


def test_history_is_labelled_as_evidence_and_never_as_advice_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = build(isolated_selector, CATALOGS[0])
    context = context_for(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    store.put(
        isolated_selector.make_receipt(
            context,
            catalog.card(
                [
                    card.tool_id
                    for card in catalog.cards
                    if card.capability == "schema_keys"
                ][0]
            ),
            isolated_selector.ToolResult(
                arguments_digest=context.arguments_digest,
                ran=True,
                evidence_changed=False,
                admissible=True,
                reason="nothing admitted",
            ),
        )
    )
    payload = isolated_selector.build_payload(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        store.matching(context),
    )
    entry = payload["history"][0]
    # The entry disclaims advice explicitly, and carries no directive.
    assert "not a recommendation" in entry["note"]
    assert set(entry) == {
        "match",
        "tool_id",
        "execution_key",
        "what_happened",
        "reason",
        "note",
    }
    assert entry["tool_id"] == "inspect_schema_keys"
    for capability in ("pipeline", "schema_keys", "scope"):
        assert f'"{capability}"' not in json.dumps(payload), capability
    rendered = json.dumps(payload).lower()
    for advice in ("you should", "best choice", "pick this", "we suggest", "prefer"):
        assert advice not in rendered, advice
    # "recommend" appears only inside the disclaimer, never as a directive.
    assert rendered.count("recommend") == rendered.count("not a recommendation")


def test_an_unusable_choice_fails_closed_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = build(isolated_selector, CATALOGS[0])
    context = context_for(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    available = frozenset({"inspect_pipeline"})
    errors = isolated_selector.SelectionError

    low = isolated_selector.select_tool(
        context,
        catalog,
        available,
        store,
        always(isolated_selector, "inspect_pipeline", 0.1),
    )
    assert low.refused is errors.LOW_CONFIDENCE
    assert low.tool_id is None

    def absent_confidence(payload: dict[str, Any]) -> Any:
        return isolated_selector.Choice(tool_id="inspect_pipeline", confidence=None)

    assert (
        isolated_selector.select_tool(
            context, catalog, available, store, absent_confidence
        ).refused
        is errors.LOW_CONFIDENCE
    ), "absent confidence is not a pass"

    def unavailable(payload: dict[str, Any]) -> Any:
        return isolated_selector.Choice(tool_id="inspect_scope", confidence=0.99)

    assert (
        isolated_selector.select_tool(
            context, catalog, available, store, unavailable
        ).refused
        is errors.TOOL_UNAVAILABLE
    )

    def review(payload: dict[str, Any]) -> Any:
        return isolated_selector.Choice(
            tool_id=None, confidence=None, requested_review=True
        )

    assert (
        isolated_selector.select_tool(
            context, catalog, available, store, review
        ).refused
        is errors.REVIEW_REQUESTED
    )


def test_a_blocked_repeat_with_no_alternative_ends_without_a_second_call_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = build(isolated_selector, CATALOGS[0])
    context = context_for(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    store.put(
        isolated_selector.make_receipt(
            context,
            catalog.card(
                [
                    card.tool_id
                    for card in catalog.cards
                    if card.capability == "schema_keys"
                ][0]
            ),
            isolated_selector.ToolResult(
                arguments_digest=context.arguments_digest,
                ran=True,
                evidence_changed=False,
                admissible=True,
                reason="nothing admitted",
            ),
        )
    )
    outcome = isolated_selector.select_tool(
        context,
        catalog,
        frozenset({"inspect_schema_keys"}),
        store,
        always(isolated_selector, "inspect_schema_keys"),
    )
    assert outcome.refused is isolated_selector.SelectionError.NO_ALTERNATIVE
    assert outcome.decisions == 0, "no call at all when nothing remains"
    assert outcome.blocked_tool_id == "inspect_schema_keys"


def test_the_store_is_idempotent_and_freezable_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = build(isolated_selector, CATALOGS[0])
    context = context_for(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    receipt = isolated_selector.make_receipt(
        context,
        catalog.card("inspect_scope"),
        isolated_selector.ToolResult(
            arguments_digest=context.arguments_digest,
            ran=True,
            evidence_changed=True,
            admissible=True,
            reason="admitted",
        ),
    )
    assert store.put(receipt) is True
    assert store.put(receipt) is False, "the same fact twice is one row"
    store.frozen = True
    other = isolated_selector.make_receipt(
        context,
        catalog.card("inspect_pipeline"),
        isolated_selector.ToolResult(
            arguments_digest=context.arguments_digest,
            ran=True,
            evidence_changed=True,
            admissible=True,
            reason="admitted",
        ),
    )
    assert store.put(other) is False, "a frozen store admits nothing"


def test_the_payload_is_bounded_and_refuses_rather_than_truncating_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = build(isolated_selector, CATALOGS[0])
    context = context_for(isolated_selector, catalog)
    available = frozenset(card.tool_id for card in catalog.cards)
    payload = isolated_selector.build_payload(context, catalog, available, [])
    assert len(json.dumps(payload)) <= isolated_selector.SELECTOR_INPUT_MAX_CHARS
    huge = build(
        isolated_selector,
        ("a" * 40, "b" * 40, "c" * 40),
    )
    oversized = isolated_selector.Catalog(cards=huge.cards * 200)
    with pytest.raises(isolated_selector.SelectionRefused) as raised:
        isolated_selector.build_payload(
            context,
            oversized,
            frozenset(card.tool_id for card in oversized.cards),
            [],
        )
    assert raised.value.reason is isolated_selector.SelectionError.PAYLOAD_TOO_LARGE


def test_the_declared_bounds_match_the_documented_ones_offline(
    isolated_selector: ModuleType,
) -> None:
    assert isolated_selector.MAX_DECISIONS == 2
    assert isolated_selector.MAX_TOOL_RUNS == 2
    assert isolated_selector.MAX_RECEIPT_REFERENCES == 4
    assert isolated_selector.SELECTOR_INPUT_MAX_CHARS == 8000
    readme = (RECIPE / "README.md").read_text(encoding="utf-8")
    for value in ("2", "4", "8000"):
        assert value in readme
