"""The recipe's contract defects, each named by the review finding. Offline.

Four properties are asserted here because the shipped recipe did not hold them:
the private capability must never reach model input, a refusal must match the
complete execution key rather than the capability alone, the declared tool-run
ledger must actually be enforced, and the same-call result-to-receipt-to-second
decision path must exist at all.
"""

from types import ModuleType
from typing import Any

import pytest
from test_conformance import PREFERRED, _catalog, _context, _result, _selector

CAPABILITIES = ("pipeline", "schema_keys", "scope")


def _executor(module: ModuleType, kinds: tuple[str, ...]) -> Any:
    observed: list[Any] = []

    def call(request: Any) -> Any:
        index = min(len(observed), len(kinds) - 1)
        observed.append(request)
        return _result(module, kinds[index], request.arguments_digest)

    call.observed = observed  # type: ignore[attr-defined]
    return call


def test_no_model_payload_carries_the_private_capability_offline(
    isolated_selector: ModuleType,
) -> None:
    """Finding 13: the selector payload sent `tool_capability_ran`."""
    import json

    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    store.put(
        isolated_selector.make_receipt(
            context,
            catalog.card(PREFERRED),
            _result(isolated_selector, "no_progress", context.arguments_digest),
        )
    )
    seen: list[dict[str, Any]] = []

    def recording(payload: dict[str, Any]) -> Any:
        seen.append(payload)
        return _selector(isolated_selector)(payload)

    isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        store,
        recording,
        _executor(isolated_selector, ("useful_and_moved",)),
    )
    assert len(seen) == 1, "the dead end is precluded; one fresh first decision"
    for payload in seen:
        rendered = json.dumps(payload)
        for capability in CAPABILITIES:
            assert f'"{capability}"' not in rendered, capability
        assert "capability" not in rendered
    history = seen[0]["history"]
    assert history, "the second call's history reached the model"
    assert set(history[0]) == {
        "match",
        "tool_id",
        "execution_key",
        "what_happened",
        "reason",
        "note",
    }
    assert seen[0]["precluded"][0]["tool_id"] == PREFERRED
    assert "capability" not in seen[0]["precluded"][0]


def test_a_refusal_matches_the_whole_execution_key_offline(
    isolated_selector: ModuleType,
) -> None:
    """Finding 13: a receipt for one argument set blocked a different call."""
    catalog = _catalog(isolated_selector)
    current = _context(isolated_selector, catalog)
    other = _context(
        isolated_selector,
        catalog,
        arguments_digest=isolated_selector.canonical_digest({"date": "2026-10-17"}),
    )
    store = isolated_selector.ReceiptStore()
    store.put(
        isolated_selector.make_receipt(
            other,
            catalog.card(PREFERRED),
            _result(isolated_selector, "no_progress", other.arguments_digest),
        )
    )
    assert store.blocked_keys(current) == {("schema_keys", other.arguments_digest)}
    outcome = isolated_selector.select_tool(
        current,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        store,
        _selector(isolated_selector),
    )
    assert outcome.tool_id == PREFERRED
    assert outcome.decisions == 1
    assert outcome.refusals == ()

    repeated = isolated_selector.select_tool(
        other,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        store,
        _selector(isolated_selector),
    )
    assert repeated.decisions == 1
    assert tuple(item.kind for item in repeated.precluded) == ("exact_repeat",)
    assert repeated.tool_id != PREFERRED


def test_a_receipt_is_bound_to_the_arguments_it_ran_with_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    with pytest.raises(isolated_selector.SelectionRefused) as raised:
        isolated_selector.make_receipt(
            context,
            catalog.card(PREFERRED),
            _result(isolated_selector, "no_progress", "a-different-digest"),
        )
    assert raised.value.reason is isolated_selector.SelectionError.ARGUMENTS_MISMATCH


def test_the_tool_run_ledger_is_enforced_not_merely_declared_offline(
    isolated_selector: ModuleType,
) -> None:
    """Finding 13: MAX_TOOL_RUNS was declared and never checked."""
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    executor = _executor(isolated_selector, ("no_progress", "useful_and_moved"))
    resolution = isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        store,
        _selector(isolated_selector),
        executor,
        max_tool_runs=1,
    )
    assert resolution.path == "unresolved"
    assert resolution.refused is isolated_selector.SelectionError.RUNS_EXHAUSTED
    assert resolution.tool_runs == 1
    assert len(executor.observed) == 1, "the second read never ran"


def test_a_raised_budget_cannot_exceed_the_declared_bound_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    executor = _executor(isolated_selector, ("no_progress", "no_progress"))
    resolution = isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        isolated_selector.ReceiptStore(),
        _selector(isolated_selector),
        executor,
        max_decisions=9,
        max_tool_runs=9,
    )
    assert resolution.decisions == isolated_selector.MAX_DECISIONS
    assert resolution.tool_runs == isolated_selector.MAX_TOOL_RUNS
    assert resolution.path == "unresolved"


def test_a_spent_tool_run_ends_the_problem_without_asking_d2_offline(
    isolated_selector: ModuleType,
) -> None:
    """The product skips a second decision that no read is left to run."""
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    seen: list[dict[str, Any]] = []

    def recording(payload: dict[str, Any]) -> Any:
        seen.append(payload)
        return _selector(isolated_selector)(payload)

    executor = _executor(isolated_selector, ("no_progress", "useful_and_moved"))
    resolution = isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        isolated_selector.ReceiptStore(),
        recording,
        executor,
        max_tool_runs=1,
    )
    assert resolution.path == "unresolved"
    assert resolution.refused is isolated_selector.SelectionError.RUNS_EXHAUSTED
    assert resolution.tool_runs == 1 == len(executor.observed)
    assert resolution.decisions == 1 == len(seen), "no unusable D2 model call"


def test_a_precluded_repeat_leaves_the_one_tool_run_for_d1_offline(
    isolated_selector: ModuleType,
) -> None:
    """A preclusion spends no run and no decision, so D1's choice runs."""
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    store.put(
        isolated_selector.make_receipt(
            context,
            catalog.card(PREFERRED),
            _result(isolated_selector, "no_progress", context.arguments_digest),
        )
    )
    seen: list[dict[str, Any]] = []

    def recording(payload: dict[str, Any]) -> Any:
        seen.append(payload)
        return _selector(isolated_selector)(payload)

    executor = _executor(isolated_selector, ("useful_and_moved",))
    resolution = isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        store,
        recording,
        executor,
        max_tool_runs=1,
    )
    assert resolution.path == "direct"
    assert resolution.refused is None
    assert resolution.decisions == 1 == len(seen)
    assert resolution.tool_runs == 1 == len(executor.observed)
    assert executor.observed[0].tool_id != PREFERRED
    assert executor.observed[0].pass_index == 1


def test_the_same_call_path_writes_a_receipt_per_executed_read_offline(
    isolated_selector: ModuleType,
) -> None:
    """Finding 13: the recipe returned a choice and stopped."""
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    resolution = isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        store,
        _selector(isolated_selector),
        _executor(isolated_selector, ("no_progress", "useful_and_moved")),
    )
    assert resolution.path == "rechosen"
    assert len(resolution.receipt_ids) == 2 == resolution.tool_runs
    assert resolution.outcomes == ("no_progress", "useful_evidence")
    assert set(resolution.receipt_ids) <= set(store.rows)
    blocking = [store.rows[item].blocking for item in resolution.receipt_ids]
    assert blocking == [True, False]


def test_a_future_call_refuses_only_the_stalled_repetition_offline(
    isolated_selector: ModuleType,
) -> None:
    """A useful read never becomes a future block; a stalled one does."""
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    store = isolated_selector.ReceiptStore()
    isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        store,
        _selector(isolated_selector),
        _executor(isolated_selector, ("no_progress", "useful_not_moved")),
    )
    blocked = store.blocked_keys(context)
    assert blocked == {("schema_keys", context.arguments_digest)}
    assert ("pipeline", context.arguments_digest) not in blocked


def test_an_executed_read_is_handed_the_bound_arguments_offline(
    isolated_selector: ModuleType,
) -> None:
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    executor = _executor(isolated_selector, ("useful_and_moved",))
    isolated_selector.resolve_problem(
        context,
        catalog,
        frozenset(card.tool_id for card in catalog.cards),
        isolated_selector.ReceiptStore(),
        _selector(isolated_selector),
        executor,
    )
    request = executor.observed[0]
    assert request.arguments_digest == context.arguments_digest
    assert request.tool_id == PREFERRED
    assert request.pass_index == 1


def test_a_tool_result_cannot_move_a_gate_it_admitted_nothing_for_offline(
    isolated_selector: ModuleType,
) -> None:
    with pytest.raises(isolated_selector.SelectionRefused) as raised:
        isolated_selector.ToolResult(
            arguments_digest="d",
            ran=True,
            evidence_changed=False,
            disposition_moved=True,
            admissible=True,
            reason="impossible",
        )
    assert raised.value.reason is isolated_selector.SelectionError.RESULT_INVALID
