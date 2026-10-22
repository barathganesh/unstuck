"""A runnable example of the Decision Receipt pattern. Standard library only.

Run it:

    python example.py

It walks one problem through the whole bounded pattern twice. The first walk is
a cold start: the model decides, the read runs through the executor, the
verifier writes a receipt, and because that read left the problem where it was,
one same-call second decision runs. The second walk is the same problem again,
so the exact no-progress receipt from the first walk removes the identical
repetition from the offer before the model is asked, and the one fresh first
decision chooses among what remains: one decision and one read.

The same walk then runs against a *different* catalog whose tool identifiers
are all different and whose capabilities are the same, which is the point: the
pattern does not know or care what your tools are called.

Every value printed here is synthetic. Nothing in this file is a measurement.
"""

from __future__ import annotations

import json
from typing import Any

from selector import (
    Catalog,
    Choice,
    ExecutionRequest,
    ProblemContext,
    ReceiptStore,
    ToolCard,
    ToolResult,
    Versions,
    canonical_digest,
    resolve_problem,
)

DEVELOPMENT_CATALOG = Catalog(
    cards=(
        ToolCard(
            tool_id="inspect_pipeline",
            capability="pipeline",
            description="Report whether the upstream job finished for this scope.",
            observable=("job_state", "watermark", "scope_complete"),
        ),
        ToolCard(
            tool_id="inspect_schema_keys",
            capability="schema_keys",
            description="Report the join key encodings and whether they are unique.",
            observable=("key_encoding", "keys_unique"),
        ),
        ToolCard(
            tool_id="inspect_scope",
            capability="scope",
            description="Report whether the request scope matches the task.",
            observable=("scope_matches", "metadata_complete"),
        ),
    )
)

#: Different identifiers, same three capabilities. Nothing else changes.
HELDOUT_CATALOG = Catalog(
    cards=(
        ToolCard(
            tool_id="check_feed_state",
            capability="pipeline",
            description="Report whether the upstream feed completed for this scope.",
            observable=("job_state", "watermark", "scope_complete"),
        ),
        ToolCard(
            tool_id="check_key_shapes",
            capability="schema_keys",
            description="Report the key shapes and whether they are unique.",
            observable=("key_encoding", "keys_unique"),
        ),
        ToolCard(
            tool_id="check_request_scope",
            capability="scope",
            description="Report whether the request scope matches the task.",
            observable=("scope_matches", "metadata_complete"),
        ),
    )
)

VERSIONS = Versions(
    data_version="your-data-version",
    model_revision="your-pinned-revision",
    catalog_version="cards-v1",
    policy_version="policy-v1",
)


def scripted_selector(preferred: str, fallback: str) -> Any:
    """A stand-in for your model call. It always wants `preferred` first.

    That is deliberate: it lets the example show the blocking rule doing its
    job. A real model would decide fresh; this one is stubborn so the pattern's
    refusal is visible.
    """

    calls: list[dict[str, Any]] = []

    def call(payload: dict[str, Any]) -> Choice:
        calls.append(payload)
        offered = {item["tool_id"] for item in payload["tools"] if item["available"]}
        if preferred in offered and "blocked_repetition" not in payload:
            return Choice(tool_id=preferred, confidence=0.93)
        if fallback in offered:
            return Choice(tool_id=fallback, confidence=0.88)
        return Choice(tool_id=None, confidence=None, requested_review=True)

    call.calls = calls  # type: ignore[attr-defined]
    return call


def scripted_executor(preferred: str) -> Any:
    """A stand-in for your tool run. It returns facts and labels nothing.

    The preferred read observes nothing new, which is what gives the verifier a
    no-progress receipt to write. Anything else admits a fact and moves a gate.
    """

    def call(request: ExecutionRequest) -> ToolResult:
        stalled = request.tool_id == preferred
        return ToolResult(
            arguments_digest=request.arguments_digest,
            ran=True,
            evidence_changed=not stalled,
            disposition_moved=not stalled,
            admissible=True,
            reason=(
                "no admissible evidence returned"
                if stalled
                else "evidence admitted and a gate moved"
            ),
        )

    return call


def walk(catalog: Catalog, preferred: str, fallback: str) -> list[dict[str, Any]]:
    """Two calls over one problem: a cold start, then an exact repeat refused."""
    store = ReceiptStore()
    available = frozenset(card.tool_id for card in catalog.cards)
    context = ProblemContext(
        problem_id="orders-by-country-empty",
        task_digest=canonical_digest({"dataset": "your-dataset", "metric": "units"}),
        versions=VERSIONS,
        arguments_digest=canonical_digest({"order_date_utc": "2026-01-01"}),
        candidates=tuple(sorted(available)),
    )
    selector = scripted_selector(preferred, fallback)
    executor = scripted_executor(preferred)
    results: list[dict[str, Any]] = []

    # Call 1: no history exists, so the model decides from current state alone.
    # The chosen read makes no progress, the verifier writes its receipt, and
    # one same-call second decision resolves the problem.
    first = resolve_problem(context, catalog, available, store, selector, executor)
    results.append(_render("call 1: cold start, one same-call second decision", first))
    results.append(
        {
            "step": "verifier: receipts written",
            "outcomes": list(first.outcomes),
            "blocking": [store.rows[item].blocking for item in first.receipt_ids],
            "classification": "synthetic",
        }
    )

    # Call 2: same problem, same context, same arguments. The model would still
    # want the same tool; the exact no-progress receipt removes it from the
    # offer first, so one decision over what remains costs one read, not two.
    second = resolve_problem(context, catalog, available, store, selector, executor)
    results.append(_render("call 2: exact repeat refused before it ran", second))
    return results


def _render(label: str, resolution: Any) -> dict[str, Any]:
    return {
        "step": label,
        "path": resolution.path,
        "chose": resolution.tool_id,
        "decisions": resolution.decisions,
        "tool_runs": resolution.tool_runs,
        "refused_repetitions": [
            {"tool_id": item.tool_id, "kind": item.kind}
            for item in resolution.precluded + resolution.refusals
        ],
        "refused": resolution.refused.value if resolution.refused else None,
        "history": list(resolution.history_classes),
        "classification": "synthetic",
    }


def main() -> int:
    report = {
        "classification": "synthetic",
        "measurements": "none; this example measures nothing",
        "development_catalog": walk(
            DEVELOPMENT_CATALOG, "inspect_schema_keys", "inspect_pipeline"
        ),
        "heldout_catalog": walk(
            HELDOUT_CATALOG, "check_key_shapes", "check_feed_state"
        ),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
