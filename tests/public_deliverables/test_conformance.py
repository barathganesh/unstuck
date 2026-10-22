"""The recipe reproduces the product's bounded Decision Receipt contract.

The truth table below is the shared authority. It is stated once, as data, and
asserted twice: against the standalone recipe imported from a copy of its own
directory, and against the application's own contracts. Nothing is imported
across that boundary in either direction, so parity is proven by behaviour
rather than by a shared module.

Every row names what the product does in `unstuck.cascade.engine`: one history
lookup, an exact-repetition preclusion before the first decision that costs no
decision and no tool run, one fresh first decision over what remains, at most
one second decision in the same call, and no third selection.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Literal, get_args
from uuid import uuid4

import pytest

from unstuck.cascade.identity import BoundVersions
from unstuck.cascade.receipts import (
    BLOCKING_OUTCOMES,
    DecisionReceipt,
    EvidenceDelta,
    OutcomeClass,
    ReceiptNamespace,
    receipt_identity,
)
from unstuck.cascade.serialization import (
    MAX_DIAGNOSTIC_READS,
    MAX_SELECTOR_CALLS,
    canonical_digest,
)
from unstuck.domain import Reason

CATALOG_NAMES = ("inspect_pipeline", "inspect_schema_keys", "inspect_scope")
PREFERRED = "inspect_schema_keys"
FALLBACK = "inspect_pipeline"


@dataclass(frozen=True)
class Row:
    """One row of the shared truth table."""

    name: str
    #: What the store already holds for this problem, if anything.
    stored: Literal[
        "nothing",
        "exact_no_progress",
        "exact_no_progress_other_arguments",
        "exact_provider_error",
        "exact_useful_evidence",
        "compatible_no_progress",
    ]
    #: What the first executed read observed, when one executes at all.
    first_result: Literal[
        "useful_and_moved",
        "useful_not_moved",
        "no_progress",
        "provider_error",
        "invalid_evidence",
        "not_executed",
    ]
    #: What the second executed read observed, when one executes at all.
    second_result: Literal["useful_and_moved", "no_progress", "not_executed"]
    path: Literal["direct", "rechosen", "unresolved"]
    decisions: int
    tool_runs: int
    blocked_kinds: tuple[str, ...]
    refused: str | None
    #: Exact repetitions removed from the offer before the first decision.
    precluded_kinds: tuple[str, ...] = ()


TRUTH_TABLE: tuple[Row, ...] = (
    Row(
        name="a first read that moved a disposition resolves directly",
        stored="nothing",
        first_result="useful_and_moved",
        second_result="not_executed",
        path="direct",
        decisions=1,
        tool_runs=1,
        blocked_kinds=(),
        refused=None,
    ),
    Row(
        name="an admitted fact that moved nothing opens the same-call second decision",
        stored="nothing",
        first_result="useful_not_moved",
        second_result="useful_and_moved",
        path="rechosen",
        decisions=2,
        tool_runs=2,
        blocked_kinds=("failed_execution",),
        refused=None,
    ),
    Row(
        name="a no-progress first read opens the same-call second decision",
        stored="nothing",
        first_result="no_progress",
        second_result="useful_and_moved",
        path="rechosen",
        decisions=2,
        tool_runs=2,
        blocked_kinds=("failed_execution",),
        refused=None,
    ),
    Row(
        name="a provider failure opens the same-call second decision",
        stored="nothing",
        first_result="provider_error",
        second_result="useful_and_moved",
        path="rechosen",
        decisions=2,
        tool_runs=2,
        blocked_kinds=("failed_execution",),
        refused=None,
    ),
    Row(
        name="inadmissible evidence opens the same-call second decision",
        stored="nothing",
        first_result="invalid_evidence",
        second_result="useful_and_moved",
        path="rechosen",
        decisions=2,
        tool_runs=2,
        blocked_kinds=("failed_execution",),
        refused=None,
    ),
    Row(
        name="an exact no-progress receipt precludes the repetition before deciding",
        stored="exact_no_progress",
        first_result="useful_and_moved",
        second_result="not_executed",
        path="direct",
        decisions=1,
        tool_runs=1,
        blocked_kinds=(),
        refused=None,
        precluded_kinds=("exact_repeat",),
    ),
    Row(
        name="a no-progress receipt for other arguments refuses nothing",
        stored="exact_no_progress_other_arguments",
        first_result="useful_and_moved",
        second_result="not_executed",
        path="direct",
        decisions=1,
        tool_runs=1,
        blocked_kinds=(),
        refused=None,
    ),
    Row(
        name="a provider-error receipt never refuses a repetition",
        stored="exact_provider_error",
        first_result="useful_and_moved",
        second_result="not_executed",
        path="direct",
        decisions=1,
        tool_runs=1,
        blocked_kinds=(),
        refused=None,
    ),
    Row(
        name="a useful-evidence receipt never refuses a repetition",
        stored="exact_useful_evidence",
        first_result="useful_and_moved",
        second_result="not_executed",
        path="direct",
        decisions=1,
        tool_runs=1,
        blocked_kinds=(),
        refused=None,
    ),
    Row(
        name="current facts dominate a merely compatible receipt",
        stored="compatible_no_progress",
        first_result="useful_and_moved",
        second_result="not_executed",
        path="direct",
        decisions=1,
        tool_runs=1,
        blocked_kinds=(),
        refused=None,
    ),
    Row(
        name="a second read that observed nothing useful ends without a third",
        stored="nothing",
        first_result="no_progress",
        second_result="no_progress",
        path="unresolved",
        decisions=2,
        tool_runs=2,
        blocked_kinds=("failed_execution",),
        refused="decisions_exhausted",
    ),
)


def _catalog(module: ModuleType) -> Any:
    return module.Catalog(
        cards=(
            module.ToolCard(
                tool_id=CATALOG_NAMES[0],
                capability="pipeline",
                description="Report upstream completion.",
                observable=("job_state",),
            ),
            module.ToolCard(
                tool_id=CATALOG_NAMES[1],
                capability="schema_keys",
                description="Report key encodings.",
                observable=("key_encoding",),
            ),
            module.ToolCard(
                tool_id=CATALOG_NAMES[2],
                capability="scope",
                description="Report scope match.",
                observable=("scope_matches",),
            ),
        )
    )


def _context(module: ModuleType, catalog: Any, **changes: Any) -> Any:
    base: dict[str, Any] = {
        "problem_id": "p-1",
        "task_digest": module.canonical_digest({"task": 1}),
        "versions": module.Versions(
            data_version="v17",
            model_revision="rev-1",
            catalog_version="cards-v1",
            policy_version="policy-v1",
        ),
        "arguments_digest": module.canonical_digest({"date": "2026-10-18"}),
        "candidates": tuple(sorted(card.tool_id for card in catalog.cards)),
    }
    return module.ProblemContext(**(base | changes))


def _result(module: ModuleType, kind: str, arguments_digest: str) -> Any:
    shapes: dict[str, dict[str, Any]] = {
        "useful_and_moved": {
            "ran": True,
            "admissible": True,
            "evidence_changed": True,
            "disposition_moved": True,
            "reason": "evidence admitted and a gate moved",
        },
        "useful_not_moved": {
            "ran": True,
            "admissible": True,
            "evidence_changed": True,
            "disposition_moved": False,
            "reason": "evidence admitted and no gate moved",
        },
        "no_progress": {
            "ran": True,
            "admissible": True,
            "evidence_changed": False,
            "disposition_moved": False,
            "reason": "nothing admitted",
        },
        "provider_error": {
            "ran": False,
            "admissible": False,
            "evidence_changed": False,
            "disposition_moved": False,
            "reason": "transport failed",
        },
        "invalid_evidence": {
            "ran": True,
            "admissible": False,
            "evidence_changed": False,
            "disposition_moved": False,
            "reason": "evidence contradicted",
        },
    }
    return module.ToolResult(arguments_digest=arguments_digest, **shapes[kind])


def _seed(module: ModuleType, catalog: Any, context: Any, row: Row) -> Any:
    """Build the store this row starts from. Nothing is written by hand."""
    store = module.ReceiptStore()
    if row.stored == "nothing":
        return store
    card = catalog.card(PREFERRED)
    kinds = {
        "exact_no_progress": ("no_progress", context.arguments_digest),
        "exact_no_progress_other_arguments": (
            "no_progress",
            module.canonical_digest({"date": "2026-10-17"}),
        ),
        "exact_provider_error": ("provider_error", context.arguments_digest),
        "exact_useful_evidence": ("useful_and_moved", context.arguments_digest),
        "compatible_no_progress": ("no_progress", context.arguments_digest),
    }
    kind, arguments = kinds[row.stored]
    written_context = (
        context
        if row.stored != "compatible_no_progress"
        else _context(module, catalog, evidence_digests=("an-older-fact",))
    )
    if arguments != context.arguments_digest:
        written_context = _context(module, catalog, arguments_digest=arguments)
    store.put(
        module.make_receipt(written_context, card, _result(module, kind, arguments))
    )
    return store


def _selector(module: ModuleType) -> Any:
    """A stubborn selector: it always wants the preferred tool when offered."""

    def call(payload: dict[str, Any]) -> Any:
        offered = {item["tool_id"] for item in payload["tools"] if item["available"]}
        if PREFERRED in offered and "blocked_repetition" not in payload:
            return module.Choice(tool_id=PREFERRED, confidence=0.95)
        if FALLBACK in offered:
            return module.Choice(tool_id=FALLBACK, confidence=0.9)
        return module.Choice(tool_id=None, confidence=None, requested_review=True)

    return call


def _executor(module: ModuleType, row: Row) -> Any:
    """Replays this row's observed results. It labels no outcome itself."""
    observed: list[Any] = []

    def call(request: Any) -> Any:
        observed.append(request)
        kind = row.first_result if request.pass_index == 1 else row.second_result
        return _result(module, kind, request.arguments_digest)

    call.observed = observed  # type: ignore[attr-defined]
    return call


@pytest.mark.parametrize("row", TRUTH_TABLE, ids=lambda row: row.name)
def test_the_recipe_matches_the_product_truth_table_offline(
    isolated_selector: ModuleType, row: Row
) -> None:
    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    store = _seed(isolated_selector, catalog, context, row)
    available = frozenset(card.tool_id for card in catalog.cards)
    resolution = isolated_selector.resolve_problem(
        context,
        catalog,
        available,
        store,
        _selector(isolated_selector),
        _executor(isolated_selector, row),
    )
    assert resolution.path == row.path, row.name
    assert resolution.decisions == row.decisions, row.name
    assert resolution.tool_runs == row.tool_runs, row.name
    assert tuple(item.kind for item in resolution.refusals) == row.blocked_kinds
    assert tuple(item.kind for item in resolution.precluded) == row.precluded_kinds
    assert (
        resolution.refused.value if resolution.refused is not None else None
    ) == row.refused


def test_no_row_ever_opens_a_third_decision_offline() -> None:
    for row in TRUTH_TABLE:
        assert row.decisions <= 2, row.name
        assert row.tool_runs <= 2, row.name


def test_the_product_agrees_with_the_recipes_declared_bounds_offline(
    isolated_selector: ModuleType,
) -> None:
    assert isolated_selector.MAX_DECISIONS == MAX_SELECTOR_CALLS
    assert isolated_selector.MAX_TOOL_RUNS == MAX_DIAGNOSTIC_READS
    assert isolated_selector.BLOCKING_OUTCOMES == BLOCKING_OUTCOMES


def test_the_product_classifies_the_same_runtime_outcomes_offline(
    isolated_selector: ModuleType,
) -> None:
    product = set(get_args(OutcomeClass)) - {"superseded"}
    assert set(get_args(isolated_selector.OutcomeClass)) == product


def test_the_product_blocks_on_the_same_conjunctions_offline(
    isolated_selector: ModuleType,
) -> None:
    """Both disposition-negative classes carry it; a moved gate never does."""
    held = canonical_digest(["admissible", 0])
    fields: dict[str, Any] = {
        "namespace": ReceiptNamespace(arm="decider_memory", case_id="parity"),
        "decision_id": uuid4(),
        "model_call_id": uuid4(),
        "diagnostic_call_id": uuid4(),
        "problem_fingerprint": canonical_digest(["problem"]),
        "decision_context_hash": canonical_digest(["context"]),
        "selected_capability": "inspect_schema_keys",
        "public_tool_id": "inspect_schema_keys",
        "arguments_hash": canonical_digest(["arguments"]),
        "delta": EvidenceDelta(
            evidence_before_hash=held,
            evidence_after_hash=held,
            disposition_before_hash=held,
            disposition_after_hash=held,
        ),
        "outcome_class": "no_progress",
        "reason_code": Reason.INSUFFICIENT,
        "versions": BoundVersions(
            dataset_version="v17",
            model_revision="rev-1",
            policy_hash=canonical_digest(["policy"]),
            calibration_hash=canonical_digest(["calibration"]),
        ),
    }
    stalled = DecisionReceipt.model_validate(
        fields
        | {"receipt_id": receipt_identity(fields), "observed_at": datetime.now(UTC)}
    )
    assert stalled.blocking is True

    admitted = canonical_digest(["admissible", 1])
    neutral_fields = fields | {
        "delta": EvidenceDelta(
            evidence_before_hash=held,
            evidence_after_hash=admitted,
            evidence_ids=(uuid4(),),
            disposition_before_hash=held,
            disposition_after_hash=held,
        ),
        "outcome_class": "disposition_unchanged",
    }
    neutral = DecisionReceipt.model_validate(
        neutral_fields
        | {
            "receipt_id": receipt_identity(neutral_fields),
            "observed_at": datetime.now(UTC),
        }
    )
    assert neutral.blocking is True

    catalog = _catalog(isolated_selector)
    context = _context(isolated_selector, catalog)
    card = catalog.card(PREFERRED)
    recipe_blocking = isolated_selector.make_receipt(
        context,
        card,
        _result(isolated_selector, "no_progress", context.arguments_digest),
    )
    recipe_neutral = isolated_selector.make_receipt(
        context,
        card,
        _result(isolated_selector, "useful_not_moved", context.arguments_digest),
    )
    recipe_moved = isolated_selector.make_receipt(
        context,
        card,
        _result(isolated_selector, "useful_and_moved", context.arguments_digest),
    )
    assert recipe_blocking.blocking is True
    assert recipe_neutral.outcome == "disposition_unchanged"
    assert recipe_neutral.blocking is True, (
        "a real read that moved no gate is the blind spot under test"
    )
    assert recipe_moved.blocking is False, (
        "a read that moved a gate can never refuse a future repetition"
    )
