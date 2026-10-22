"""The recipe's second decision sees the state the first read left. Offline.

Repair wave 3, finding 3: `resolve_problem` wrote the first read's receipt and
then called the second decision with the original `ProblemContext` and the
references matching it, so the model saw the pre-diagnostic evidence count and
used-tool state, and the same-call receipt was labelled `exact` against a state
that no longer held. At the same time `current_state.already_used` serialized
`context.used_capabilities` directly, which is the private capability string the
module promises never to send.

The leakage test that existed used a context with the default empty tuple, so
it never exercised that field. These tests populate every private field.
"""

import json
from types import ModuleType
from typing import Any

from test_conformance import PREFERRED, _catalog, _context, _result, _selector

CAPABILITIES = ("pipeline", "schema_keys", "scope")


def _recording(module: ModuleType) -> Any:
    seen: list[dict[str, Any]] = []
    inner = _selector(module)

    def call(payload: dict[str, Any]) -> Any:
        seen.append(json.loads(json.dumps(payload)))
        return inner(payload)

    call.seen = seen  # type: ignore[attr-defined]
    return call


def _executor(module: ModuleType, kinds: tuple[str, ...], **extra: Any) -> Any:
    observed: list[Any] = []

    def call(request: Any) -> Any:
        index = min(len(observed), len(kinds) - 1)
        observed.append(request)
        base = _result(module, kinds[index], request.arguments_digest)
        if not extra:
            return base
        from dataclasses import replace

        return replace(base, **extra)

    call.observed = observed  # type: ignore[attr-defined]
    return call


def _loaded(module: ModuleType, catalog: Any) -> Any:
    """A context with every private field populated, not the empty default."""
    return _context(
        module,
        catalog,
        evidence_digests=("held-fact-1", "held-fact-2"),
        used_capabilities=("scope",),
    )


def test_the_second_decision_sees_the_post_read_state_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    context = _loaded(isolated_selector, catalog)
    selector = _recording(isolated_selector)
    isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        isolated_selector.ReceiptStore(),
        selector,
        _executor(
            isolated_selector,
            ("no_progress", "useful_and_moved"),
        ),
    )
    first, second = selector.seen
    assert first["current_state"] != second["current_state"], (
        "the second decision was handed the pre-diagnostic state"
    )
    # The read that ran is recorded as having run, by its public identifier.
    assert PREFERRED not in first["current_state"]["already_used"]
    assert PREFERRED in second["current_state"]["already_used"]


def test_admitted_evidence_reaches_the_second_decision_offline(
    isolated_selector: ModuleType,
) -> None:
    """A read that admitted a fact and moved nothing refreshes the evidence."""
    catalog = _catalog(isolated_selector)
    context = _loaded(isolated_selector, catalog)
    selector = _recording(isolated_selector)
    isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        isolated_selector.ReceiptStore(),
        selector,
        _executor(
            isolated_selector,
            ("useful_not_moved", "useful_and_moved"),
            admitted_digests=("newly-admitted",),
        ),
    )
    first, second = selector.seen
    assert second["current_state"]["evidence_count"] == (
        first["current_state"]["evidence_count"] + 1
    )


def test_an_executor_projection_is_the_refreshed_state_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    context = _loaded(isolated_selector, catalog)
    # A read admits facts and never withdraws one, so the projection carries
    # the facts the problem already held plus the one this read admitted.
    projection = isolated_selector.StateProjection(
        evidence_digests=("held-fact-1", "held-fact-2", "projected-c"),
        candidates=("inspect_pipeline", "inspect_scope"),
    )
    selector = _recording(isolated_selector)
    isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        isolated_selector.ReceiptStore(),
        selector,
        _executor(
            isolated_selector,
            ("useful_not_moved", "useful_and_moved"),
            admitted_digests=("projected-c",),
            state_after=projection,
        ),
    )
    assert selector.seen[1]["current_state"]["evidence_count"] == 3


def test_the_same_call_receipt_is_not_labelled_exact_against_it_offline(
    isolated_selector: ModuleType,
) -> None:
    """The receipt describes the pre-call state, so it is history, not a match."""
    catalog = _catalog(isolated_selector)
    context = _loaded(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    selector = _recording(isolated_selector)
    resolution = isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        store,
        selector,
        _executor(isolated_selector, ("no_progress", "useful_and_moved")),
    )
    written = store.rows[resolution.receipt_ids[0]]
    assert written.context_digest == context.context_digest
    refreshed = isolated_selector.refreshed_context(
        context,
        "schema_keys",
        _result(isolated_selector, "no_progress", context.arguments_digest),
    )
    assert refreshed.context_digest != context.context_digest
    assert written.match(refreshed) == "compatible"
    assert written.match(context) == "exact"
    # The second decision's history carries it as compatible, and the
    # prohibition is bound to the state the receipt was recorded under.
    assert selector.seen[1]["history"]
    assert {entry["match"] for entry in selector.seen[1]["history"]} == {"compatible"}
    assert store.blocked_keys(refreshed) == set()
    assert store.blocked_keys(context) == {("schema_keys", context.arguments_digest)}


def test_the_refreshed_state_records_the_read_that_ran_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    context = _loaded(isolated_selector, catalog)
    refreshed = isolated_selector.refreshed_context(
        context,
        "schema_keys",
        _result(isolated_selector, "no_progress", context.arguments_digest),
    )
    assert set(refreshed.used_capabilities) == {"scope", "schema_keys"}
    assert refreshed.evidence_digests == context.evidence_digests
    assert refreshed.problem_fingerprint == context.problem_fingerprint


def test_no_payload_carries_a_private_capability_with_every_field_set_offline(
    isolated_selector: ModuleType,
) -> None:
    """Finding 3: `already_used` serialized the private capability strings."""
    catalog = _catalog(isolated_selector)
    context = _loaded(isolated_selector, catalog)
    selector = _recording(isolated_selector)
    isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        isolated_selector.ReceiptStore(),
        selector,
        _executor(isolated_selector, ("no_progress", "useful_and_moved")),
    )
    assert len(selector.seen) == 2
    for payload in selector.seen:
        rendered = json.dumps(payload)
        for capability in CAPABILITIES:
            assert f'"{capability}"' not in rendered, capability
        assert "capability" not in rendered
        assert payload["current_state"]["already_used"] == sorted(
            payload["current_state"]["already_used"]
        )


def test_an_unpublished_capability_travels_as_an_opaque_key_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    context = _context(
        isolated_selector,
        catalog,
        used_capabilities=("a_capability_this_catalog_does_not_publish",),
    )
    rendered = isolated_selector.public_executions(context, catalog)
    assert rendered == [
        isolated_selector.opaque_execution_key(
            "a_capability_this_catalog_does_not_publish", context.arguments_digest
        )
    ]
    payload = isolated_selector.build_payload(
        context, catalog, frozenset(card.tool_id for card in catalog.cards), []
    )
    body = json.dumps(payload)
    assert "does_not_publish" not in body
    assert rendered[0] in body


def test_a_published_capability_travels_as_its_public_identifier_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    context = _context(
        isolated_selector, catalog, used_capabilities=("schema_keys", "pipeline")
    )
    assert isolated_selector.public_executions(context, catalog) == [
        "inspect_pipeline",
        "inspect_schema_keys",
    ]


def test_a_read_that_admitted_nothing_names_no_new_evidence_offline(
    isolated_selector: ModuleType,
) -> None:
    import pytest

    with pytest.raises(isolated_selector.SelectionRefused) as raised:
        isolated_selector.ToolResult(
            arguments_digest="d",
            ran=True,
            evidence_changed=False,
            admissible=True,
            reason="nothing admitted",
            admitted_digests=("invented",),
        )
    assert raised.value.reason is isolated_selector.SelectionError.RESULT_INVALID
