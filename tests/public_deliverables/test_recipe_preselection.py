"""The recipe precludes an exact proven dead end before the first decision.

Parity with `unstuck.cascade.engine`: an exact current-context receipt whose own
two facts prove the identical capability and arguments moved no gate removes
that one execution from the offer before the model is asked anything. The one
fresh first decision is made over what remains. Without a blocker nothing about
the payload changes. Offline; the recipe is imported from a copy of its own
directory.
"""

import json
from types import ModuleType
from typing import Any

from test_conformance import FALLBACK, PREFERRED, _catalog, _context, _result

CAPABILITIES = ("pipeline", "schema_keys", "scope")


def _seed(module: ModuleType, catalog: Any, context: Any, kind: str) -> Any:
    store = module.ReceiptStore()
    store.put(
        module.make_receipt(
            context,
            catalog.card(PREFERRED),
            _result(module, kind, context.arguments_digest),
        )
    )
    return store


def _stubborn(module: ModuleType, seen: list[dict[str, Any]]) -> Any:
    """Wants the preferred tool whenever it is offered; never sees a receipt."""

    def call(payload: dict[str, Any]) -> Any:
        seen.append(payload)
        offered = [item["tool_id"] for item in payload["tools"] if item["available"]]
        if PREFERRED in offered:
            return module.Choice(tool_id=PREFERRED, confidence=0.95)
        if FALLBACK in offered:
            return module.Choice(tool_id=FALLBACK, confidence=0.9)
        if offered:
            return module.Choice(tool_id=offered[0], confidence=0.9)
        return module.Choice(tool_id=None, confidence=None, requested_review=True)

    return call


def _executor(module: ModuleType, kinds: tuple[str, ...]) -> Any:
    observed: list[Any] = []

    def call(request: Any) -> Any:
        index = min(len(observed), len(kinds) - 1)
        observed.append(request)
        return _result(module, kinds[index], request.arguments_digest)

    call.observed = observed  # type: ignore[attr-defined]
    return call


def _all(catalog: Any) -> frozenset[str]:
    return frozenset(card.tool_id for card in catalog.cards)


def test_an_exact_blocker_is_removed_before_the_one_first_decision_offline(
    isolated_selector: ModuleType,
) -> None:
    for kind in ("no_progress", "useful_not_moved"):
        catalog = _catalog(isolated_selector)
        context = _context(isolated_selector, catalog)
        store = _seed(isolated_selector, catalog, context, kind)
        (proof,) = store.rows.values()
        seen: list[dict[str, Any]] = []
        executor = _executor(isolated_selector, ("useful_and_moved",))
        resolution = isolated_selector.resolve_problem(
            context,
            catalog,
            _all(catalog),
            store,
            _stubborn(isolated_selector, seen),
            executor,
        )
        assert resolution.path == "direct", kind
        assert resolution.decisions == 1 == len(seen), kind
        assert resolution.tool_runs == 1, kind
        assert resolution.refusals == (), kind
        assert [(item.tool_id, item.kind) for item in resolution.precluded] == [
            (PREFERRED, "exact_repeat")
        ], kind
        assert resolution.precluded[0].receipt_id == proof.receipt_id
        assert resolution.precluded[0].outcome == proof.outcome
        assert resolution.tool_id == FALLBACK
        assert [request.tool_id for request in executor.observed] == [FALLBACK]
        assert executor.observed[0].pass_index == 1
        offered = {item["tool_id"]: item["available"] for item in seen[0]["tools"]}
        assert offered[PREFERRED] is False
        assert [item["tool_id"] for item in seen[0]["precluded"]] == [PREFERRED]
        assert "blocked_repetition" not in seen[0]
        rendered = json.dumps(seen[0])
        for capability in CAPABILITIES:
            assert f'"{capability}"' not in rendered, capability


def test_without_a_blocker_the_payload_is_unchanged_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    for kind in ("useful_and_moved", "provider_error", "invalid_evidence"):
        store = _seed(isolated_selector, catalog, context, kind)
        expected = isolated_selector.build_payload(
            context, catalog, _all(catalog), store.matching(context)
        )
        seen: list[dict[str, Any]] = []
        resolution = isolated_selector.resolve_problem(
            context,
            catalog,
            _all(catalog),
            store,
            _stubborn(isolated_selector, seen),
            _executor(isolated_selector, ("useful_and_moved",)),
        )
        assert resolution.precluded == (), kind
        assert set(seen[0]) == {"current_state", "tools", "history", "rules"}, kind
        assert seen[0] == expected
        assert resolution.tool_id == PREFERRED


def test_a_compatible_receipt_precludes_nothing_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    older = _context(isolated_selector, catalog, evidence_digests=("an-older-fact",))
    store = _seed(isolated_selector, catalog, older, "no_progress")
    context = _context(isolated_selector, catalog)
    outcome = isolated_selector.select_tool(
        context, catalog, _all(catalog), store, _stubborn(isolated_selector, [])
    )
    assert outcome.precluded == ()
    assert outcome.tool_id == PREFERRED
    assert outcome.history_classes == ("compatible",)


def test_every_option_precluded_ends_without_asking_the_model_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    store = _seed(isolated_selector, catalog, context, "no_progress")
    seen: list[dict[str, Any]] = []
    resolution = isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset({PREFERRED}),
        store,
        _stubborn(isolated_selector, seen),
        _executor(isolated_selector, ("useful_and_moved",)),
    )
    assert resolution.path == "unresolved"
    assert resolution.refused is isolated_selector.SelectionError.NO_ALTERNATIVE
    assert resolution.decisions == 0 == len(seen)
    assert resolution.tool_runs == 0
    assert [item.tool_id for item in resolution.precluded] == [PREFERRED]


def test_one_run_precluded_then_no_progress_d1_stops_without_d2_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    store = _seed(isolated_selector, catalog, context, "no_progress")
    seen: list[dict[str, Any]] = []
    resolution = isolated_selector.resolve_problem(
        context,
        catalog,
        _all(catalog),
        store,
        _stubborn(isolated_selector, seen),
        _executor(isolated_selector, ("no_progress",)),
        max_tool_runs=1,
    )
    assert resolution.path == "unresolved"
    assert resolution.refused is isolated_selector.SelectionError.RUNS_EXHAUSTED
    assert resolution.decisions == 1 == len(seen)
    assert resolution.tool_runs == 1


def test_precluded_then_no_progress_d1_opens_one_d2_without_the_dead_end_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    store = _seed(isolated_selector, catalog, context, "no_progress")
    seen: list[dict[str, Any]] = []
    executor = _executor(isolated_selector, ("no_progress", "useful_and_moved"))
    resolution = isolated_selector.resolve_problem(
        context,
        catalog,
        _all(catalog),
        store,
        _stubborn(isolated_selector, seen),
        executor,
    )
    assert resolution.decisions == 2 == len(seen)
    assert resolution.tool_runs == 2
    assert [item.kind for item in resolution.refusals] == ["failed_execution"]
    assert [item.tool_id for item in resolution.precluded] == [PREFERRED]
    # The preclusion was matched against the call's original state and
    # stands for the whole call, so D2 is never offered the proven dead end.
    d2 = {item["tool_id"]: item["available"] for item in seen[1]["tools"]}
    assert d2[FALLBACK] is False
    assert d2[PREFERRED] is False
    assert [item["tool_id"] for item in seen[1]["precluded"]] == [PREFERRED]
    observed = [request.tool_id for request in executor.observed]
    assert observed[0] == FALLBACK
    assert PREFERRED not in observed
