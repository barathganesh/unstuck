"""The curated public record of the pre-selection loop-escape proofs. Offline.

Reads three private records, the hosted Decider proof, the AgentCore proof and
the re-derived measurement summary, and writes the minimum causal facts a judge
needs: who chose what, whether the proven dead end was precluded before the
first Decider request, how many inferences and reads each call spent, how each
call ended, and the one latency comparison the pair supports.

Only the optimized pre-selection flow is publishable here. A record of the
superseded post-D1 flow, where the Decider repeated the dead end and a second
Decider call recovered, is refused: it is kept as described history, not as the
lead claim.

Scrubbing is by construction, not by redaction. Each private record is parsed
into a model that names only the fields this module reads, and every other
field is ignored, so an identifier, hash, revision, version, timestamp or raw
model output has no field to arrive in. The rendered text is then scanned
against the public scan's full pattern set, and any finding refuses the write.

Every number in the output sits in an object beside its denominator, and every
measured quantity also carries its unit.
"""

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from publishing.public_scan import scan_text
from unstuck.cascade.serialization import MAX_DIAGNOSTIC_READS, MAX_SELECTOR_CALLS

SCHEMA_VERSION: Final = "2"
OUTPUT_LABEL: Final = "dist/public-demo/loop-escape-proof.json"

#: The one basis every measured quantity in the record is read against.
BASIS: Final = (
    "n = 1: the same synthetic case, each proof run once; the superseded "
    "post-D1 Run 2 is a separate earlier run of the superseded flow on the "
    "same endpoint"
)

#: Decimal places the inputs carry, so a delta is never stated finer.
CLIENT_PLACES: Final = 1
SELECTOR_PLACES: Final = 2
PERCENT_PLACES: Final = 1


class _Read(BaseModel):
    """A private record read through an allowlist: unknown fields are dropped."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class _Receipt(_Read):
    public_tool_id: str
    outcome_class: str
    stored: bool


class _Call(_Read):
    history_status: str
    receipt_match: str
    d1_choice: str
    d2_choice: str | None = None
    blocked_tool_id: str | None = None
    blocked_before_first_read: bool | None = None
    blocked_before_selection: bool | None = None
    executed_tools: tuple[str, ...]
    diagnostic_reads: int
    selector_calls: int
    repair_applied: bool
    verified_success: bool
    terminal_status: str
    terminal_reason: str
    receipts: tuple[_Receipt, ...]


class _Observation(_Read):
    history_status: str
    option_ids: tuple[str, ...]
    validation_status: str
    measured_latency_ms: float | None = None


class _Linkage(_Read):
    call_1_receipt_cites_call_1_d1_decision: bool | None = None
    call_2_block_cites_call_1_receipt: bool | None = None
    call_2_block_precedes_any_read: bool | None = None
    call_2_block_precedes_selector_request: bool | None = None
    call_2_d1_differs_from_blocked_choice: bool | None = None


class _Check(_Read):
    name: str
    passed: bool


class RealProof(_Read):
    """The hosted Decider proof, as far as this module reads it."""

    fixture: Literal["synthetic"]
    status: Literal["complete"]
    verdict: str
    max_diagnostic_reads: int
    control_calls: tuple[_Call, ...]
    memory_calls: tuple[_Call, ...]
    observations: tuple[_Observation, ...]
    linkage: _Linkage
    checks: tuple[_Check, ...]


class _HostedCall(_Read):
    blocked: str | None = None
    client_latency_ms: float
    diagnostic_reads: int
    event_kinds: tuple[str, ...]
    first_choice: str
    final_choice: str
    history: str
    narration_status: str
    reason: str
    repair_attempts: int
    terminal: str


class _MetricRun(_Read):
    client_latency_ms: float
    diagnostic_reads: int
    reason: str
    selector_inferences: int
    selector_latency_ms_sum: float
    terminal: str


class _Difference(_Read):
    absolute_ms: float
    percent: float


class _MetricDelta(_Read):
    client_latency: _Difference
    inferences: int
    selector_latency_sum: _Difference


class _AgentCoreMetrics(_Read):
    run_1: _MetricRun
    run_2_optimized: _MetricRun
    old_v10_run_2_post_d1: _MetricRun
    run_2_delta_vs_old: _MetricDelta


class _Timeline(_Read):
    emitted: int
    expected_event_count: int
    finalized: bool
    dropped: int
    refused: int


class AgentCoreProof(_Read):
    """The AgentCore proof, as far as this module reads it."""

    classification: Literal["synthetic"]
    visibility: Literal["private"]
    verdict: str
    calls: dict[str, _HostedCall]
    checks: dict[str, bool]
    metrics: _AgentCoreMetrics
    telemetry_summaries: dict[str, _Timeline]
    leaks: tuple[str, ...]


class _MeasuredRun(_Read):
    choices: tuple[str, ...]
    inferences: int
    reads: int
    reason: str
    selector_ms_sum: float
    terminal: str
    client_ms: float | None = None
    offers: tuple[int, ...] = ()


class _MeasuredDifference(_Read):
    absolute_ms: float
    percent: float


class _MeasuredDelta(_Read):
    inferences: int
    selector: _MeasuredDifference
    client: _MeasuredDifference | None = None


class _MeasuredSurface(_Read):
    run_1: _MeasuredRun
    old_run_2: _MeasuredRun
    optimized_run_2: _MeasuredRun
    run_2_delta: _MeasuredDelta


class Measurements(_Read):
    """The re-derived measurement summary, as far as this module reads it."""

    agentcore: _MeasuredSurface
    hosted: _MeasuredSurface
    verdicts: dict[str, str]


class EvidenceRefused(ValueError):
    """The private records cannot be published as they stand."""


def count(value: int, denominator: str) -> dict[str, object]:
    """One whole number, always beside what it was counted against."""
    return {"value": value, "denominator": denominator}


def measure(value: float, unit: str, denominator: str = BASIS) -> dict[str, object]:
    """One measured quantity, always beside its unit and its basis."""
    return {"value": value, "unit": unit, "denominator": denominator}


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise EvidenceRefused(detail)


def _order(call: _HostedCall, earlier: str, later: str) -> bool | None:
    """Whether ``earlier`` precedes ``later`` in this call's event timeline."""
    kinds = list(call.event_kinds)
    if earlier not in kinds or later not in kinds:
        return None
    return kinds.index(earlier) < kinds.index(later)


def _false_preclusion(call: _Call, dead_end: str) -> bool:
    """A preclusion that no exact receipt for that one dead end stands behind."""
    if call.blocked_tool_id is None:
        return False
    return not (
        call.receipt_match == "exact"
        and call.blocked_tool_id == dead_end
        and call.d1_choice != call.blocked_tool_id
        and call.blocked_before_selection is True
    )


def _hosted_false_preclusion(call: _HostedCall, exact_receipt: bool) -> bool:
    if call.blocked is None:
        return False
    return not (
        exact_receipt
        and call.history == "matched"
        and call.blocked != call.first_choice
        and _order(call, "repeat_blocked", "decider_first_requested") is True
    )


def _reads(call: _Call | _HostedCall, bound: int) -> dict[str, object]:
    return count(call.diagnostic_reads, f"of a bound of {bound} read per call")


def _real_call(
    label: str, call: _Call, bound: int, options: int
) -> dict[str, object]:
    precluded = call.blocked_tool_id is not None
    return {
        "call": label,
        "decision_history": call.history_status,
        "receipt_match": call.receipt_match,
        "dead_end_precluded": precluded,
        "precluded_before_selection": (
            None if not precluded else call.blocked_before_selection
        ),
        "precluded_before_any_read": (
            None if not precluded else call.blocked_before_first_read
        ),
        "options_offered": count(options, "diagnostics offered to the Decider"),
        "decider_choice": call.d1_choice,
        "re_decision_d2": call.d2_choice,
        "decider_choices": count(
            call.selector_calls, f"of a bound of {MAX_SELECTOR_CALLS} per call"
        ),
        "diagnostic_reads": _reads(call, bound),
        "executed": list(call.executed_tools),
        "repair_applied": call.repair_applied,
        "verified": call.verified_success,
        "terminal_status": call.terminal_status,
        "terminal_reason": call.terminal_reason,
        "receipts_stored": [
            {"tool": receipt.public_tool_id, "outcome_class": receipt.outcome_class}
            for receipt in call.receipts
            if receipt.stored
        ],
    }


def _hosted_call(label: str, call: _HostedCall, bound: int) -> dict[str, object]:
    precluded = call.blocked is not None
    return {
        "call": label,
        "decision_history": call.history,
        "dead_end_precluded": precluded,
        "preclusion_precedes_first_decider_call": (
            None if not precluded else _order(
                call, "repeat_blocked", "decider_first_requested"
            )
        ),
        "preclusion_precedes_first_read": (
            None if not precluded else _order(call, "repeat_blocked", "probe_started")
        ),
        "decider_choice": call.first_choice,
        "final_choice": call.final_choice,
        "second_decider_call": "decider_final_requested" in call.event_kinds,
        "diagnostic_reads": _reads(call, bound),
        "repairs": count(call.repair_attempts, "of a bound of 1 repair per call"),
        "terminal_status": call.terminal,
        "terminal_reason": call.reason,
        "narration": call.narration_status,
        "telemetry_events": count(
            len(call.event_kinds), "events in this call's timeline"
        ),
    }


def _one_observation(real: RealProof, history_status: str) -> _Observation:
    found = [item for item in real.observations if item.history_status == history_status]
    _require(
        len(found) == 1,
        f"the hosted proof does not hold exactly one {history_status} Decider answer",
    )
    return found[0]


def _check(agentcore: AgentCoreProof, name: str) -> None:
    _require(
        agentcore.checks.get(name) is True,
        f"the AgentCore proof does not record {name} as passed",
    )


def _refuse_superseded_flow(real: RealProof, agentcore: AgentCoreProof) -> None:
    """Accept only the pre-selection flow on both surfaces."""
    run_1, run_2 = real.memory_calls
    _require(
        run_1.selector_calls == 1 and run_1.blocked_tool_id is None,
        "Run 1 of the hosted proof is not the single-inference learning call",
    )
    _require(
        run_2.d2_choice is None,
        "Run 2 of the hosted proof made a second Decider choice, which is the "
        "superseded post-D1 flow",
    )
    _require(
        run_2.selector_calls == 1,
        "Run 2 of the hosted proof did not spend exactly one Decider inference",
    )
    _require(
        run_2.blocked_before_selection is True,
        "Run 2 of the hosted proof did not preclude the dead end before selection",
    )
    _require(
        real.linkage.call_2_block_precedes_selector_request is True,
        "the hosted proof does not link the preclusion ahead of the first "
        "Decider request",
    )
    _require(
        real.linkage.call_2_d1_differs_from_blocked_choice is True,
        "the hosted proof does not record a fresh Decider choice away from the "
        "precluded dead end",
    )

    call_2 = agentcore.calls["call-2"]
    _require(
        _order(call_2, "repeat_blocked", "decider_first_requested") is True,
        "the AgentCore Run 2 timeline does not place the preclusion before the "
        "first Decider request, which is the superseded post-D1 flow",
    )
    _require(
        "decider_final_requested" not in call_2.event_kinds,
        "the AgentCore Run 2 timeline records a second Decider request",
    )
    _check(agentcore, "call2_no_d2")
    _check(agentcore, "call2_preclusion_before_decider_first_requested")


def _cross_check(
    real: RealProof, agentcore: AgentCoreProof, measured: Measurements
) -> None:
    """Refuse unless the three private records agree on every published number."""
    _require(
        all(value == "PASS" for value in measured.verdicts.values()),
        "the measurement summary does not record every surface as passing",
    )

    metrics = agentcore.metrics
    pairs = (
        ("Run 1", measured.agentcore.run_1, metrics.run_1),
        ("Run 2", measured.agentcore.optimized_run_2, metrics.run_2_optimized),
        (
            "the superseded Run 2",
            measured.agentcore.old_run_2,
            metrics.old_v10_run_2_post_d1,
        ),
    )
    for label, measured_run, proof_run in pairs:
        _require(
            measured_run.client_ms == proof_run.client_latency_ms,
            f"the measured client time for {label} differs from the AgentCore proof",
        )
        _require(
            measured_run.inferences == proof_run.selector_inferences,
            f"the measured inference count for {label} differs from the AgentCore proof",
        )
        _require(
            measured_run.selector_ms_sum == proof_run.selector_latency_ms_sum,
            f"the measured selector time for {label} differs from the AgentCore proof",
        )
        _require(
            measured_run.reads == proof_run.diagnostic_reads
            and measured_run.terminal == proof_run.terminal
            and measured_run.reason == proof_run.reason,
            f"the measured outcome for {label} differs from the AgentCore proof",
        )

    for label, key, measured_run in (
        ("Run 1", "call-1", measured.agentcore.run_1),
        ("Run 2", "call-2", measured.agentcore.optimized_run_2),
    ):
        call = agentcore.calls[key]
        _require(
            measured_run.client_ms == call.client_latency_ms,
            f"the measured client time for {label} differs from the recorded call",
        )
        _require(
            measured_run.reads == call.diagnostic_reads,
            f"the measured read count for {label} differs from the recorded call",
        )

    run_1, run_2 = real.memory_calls
    hosted_pairs = (
        ("Run 1", measured.hosted.run_1, _one_observation(real, "empty"), run_1),
        (
            "Run 2",
            measured.hosted.optimized_run_2,
            _one_observation(real, "matched"),
            run_2,
        ),
    )
    for label, measured_run, observation, memory_call in hosted_pairs:
        _require(
            observation.measured_latency_ms is not None
            and round(observation.measured_latency_ms, SELECTOR_PLACES)
            == measured_run.selector_ms_sum,
            f"the measured hosted selector time for {label} differs from the proof",
        )
        _require(
            measured_run.offers == (len(observation.option_ids),),
            f"the measured option count for {label} differs from the proof",
        )
        _require(
            measured_run.inferences == memory_call.selector_calls
            and measured_run.reads == memory_call.diagnostic_reads
            and measured_run.terminal == memory_call.terminal_status
            and measured_run.reason == memory_call.terminal_reason,
            f"the measured hosted outcome for {label} differs from the proof",
        )
    _require(
        measured.hosted.old_run_2.inferences == 2
        and measured.agentcore.old_run_2.inferences == 2,
        "the superseded Run 2 is not recorded as two Decider inferences",
    )


def _delta(
    old: float, optimized: float, places: int, recorded: Sequence[_Difference]
) -> tuple[float, float]:
    """The absolute and relative fall, refused unless every record agrees."""
    absolute = round(old - optimized, places)
    percent = round(100.0 * (old - optimized) / old, PERCENT_PLACES)
    for item in recorded:
        _require(
            round(item.absolute_ms, places) == absolute
            and round(item.percent, PERCENT_PLACES) == percent,
            "a recomputed difference differs from the recorded one",
        )
    return absolute, percent


def _measured_latency(
    agentcore: AgentCoreProof, measured: Measurements
) -> dict[str, object]:
    recorded = agentcore.metrics.run_2_delta_vs_old
    cloud = measured.agentcore
    direct = measured.hosted
    _require(
        measured.agentcore.run_2_delta.client is not None,
        "the measurement summary holds no client difference for Run 2",
    )
    client_recorded = measured.agentcore.run_2_delta.client
    assert client_recorded is not None  # narrowed by the refusal above

    client_absolute, client_percent = _delta(
        cloud.old_run_2.client_ms or 0.0,
        cloud.optimized_run_2.client_ms or 0.0,
        CLIENT_PLACES,
        (recorded.client_latency, _as_difference(client_recorded)),
    )
    selector_absolute, selector_percent = _delta(
        cloud.old_run_2.selector_ms_sum,
        cloud.optimized_run_2.selector_ms_sum,
        SELECTOR_PLACES,
        (
            recorded.selector_latency_sum,
            _as_difference(measured.agentcore.run_2_delta.selector),
        ),
    )
    direct_absolute, direct_percent = _delta(
        direct.old_run_2.selector_ms_sum,
        direct.optimized_run_2.selector_ms_sum,
        SELECTOR_PLACES,
        (_as_difference(direct.run_2_delta.selector),),
    )
    fewer = cloud.old_run_2.inferences - cloud.optimized_run_2.inferences
    _require(
        fewer == recorded.inferences == direct.run_2_delta.inferences,
        "the recorded inference difference is not one fewer inference",
    )

    def elapsed(value: float | None, unit: str) -> dict[str, object]:
        assert value is not None  # every published run carries this quantity
        return measure(value, unit)

    return {
        "what_this_measures": (
            "One comparison per surface between the superseded post-D1 flow and "
            "the pre-selection flow on the same synthetic case. Client time is "
            "the caller's wall time for one runtime invocation. Selector time is "
            "the measured Decider inference time, summed over the inferences a "
            "call made."
        ),
        "basis": BASIS,
        "run_1_is_not_saved": (
            "Run 1 pays the learning cost once and is unchanged by the "
            "pre-selection block: one Decider inference, one read, unresolved, "
            "receipt stored. Run 1 is never presented as a saving, and no saving "
            "is claimed for it."
        ),
        "no_dollar_saving": (
            "No dollar or cost saving is measured and none is claimed: the "
            "Decider is served by a real-time endpoint billed by instance "
            "uptime, not per request. What the pair shows is one fewer "
            "inference, less occupied model time, and a lower observed Run 2 "
            "elapsed time on this one case, at n = 1."
        ),
        "agentcore_client_time": {
            "run_1_learning": {
                "elapsed": elapsed(cloud.run_1.client_ms, "ms"),
                "decider_inferences": measure(cloud.run_1.inferences, "inferences"),
            },
            "superseded_run_2_post_d1": {
                "elapsed": elapsed(cloud.old_run_2.client_ms, "ms"),
                "decider_inferences": measure(cloud.old_run_2.inferences, "inferences"),
            },
            "optimized_run_2_preselection": {
                "elapsed": elapsed(cloud.optimized_run_2.client_ms, "ms"),
                "decider_inferences": measure(
                    cloud.optimized_run_2.inferences, "inferences"
                ),
            },
            "difference": {
                "absolute": measure(
                    client_absolute, "ms below the superseded Run 2 elapsed time"
                ),
                "relative": measure(
                    client_percent, "percent below the superseded Run 2 elapsed time"
                ),
                "decider_inferences": measure(fewer, "fewer inference"),
            },
        },
        "agentcore_selector_time": {
            "superseded_run_2_post_d1": {
                "elapsed": measure(cloud.old_run_2.selector_ms_sum, "ms"),
                "decider_inferences": measure(cloud.old_run_2.inferences, "inferences"),
            },
            "optimized_run_2_preselection": {
                "elapsed": measure(cloud.optimized_run_2.selector_ms_sum, "ms"),
                "decider_inferences": measure(
                    cloud.optimized_run_2.inferences, "inferences"
                ),
            },
            "difference": {
                "absolute": measure(
                    selector_absolute,
                    "ms below the superseded Run 2 selector time",
                ),
                "relative": measure(
                    selector_percent,
                    "percent below the superseded Run 2 selector time",
                ),
            },
        },
        "hosted_selector_time": {
            "run_1_learning": {
                "elapsed": measure(direct.run_1.selector_ms_sum, "ms"),
                "decider_inferences": measure(direct.run_1.inferences, "inferences"),
            },
            "superseded_run_2_post_d1": {
                "elapsed": measure(direct.old_run_2.selector_ms_sum, "ms"),
                "decider_inferences": measure(
                    direct.old_run_2.inferences, "inferences"
                ),
            },
            "optimized_run_2_preselection": {
                "elapsed": measure(direct.optimized_run_2.selector_ms_sum, "ms"),
                "decider_inferences": measure(
                    direct.optimized_run_2.inferences, "inferences"
                ),
            },
            "difference": {
                "absolute": measure(
                    direct_absolute,
                    "ms below the superseded Run 2 selector time",
                ),
                "relative": measure(
                    direct_percent,
                    "percent below the superseded Run 2 selector time",
                ),
            },
        },
    }


def _as_difference(value: _Difference | _MeasuredDifference) -> _Difference:
    return _Difference(absolute_ms=value.absolute_ms, percent=value.percent)


def build(
    real: RealProof, agentcore: AgentCoreProof, measured: Measurements
) -> dict[str, object]:
    """The curated record. Refuses a proof that did not pass as recorded."""
    _require(real.verdict == "PASS", "the hosted Decider proof did not pass")
    _require(all(check.passed for check in real.checks), "a hosted check failed")
    _require(agentcore.verdict == "PASS", "the AgentCore proof did not pass")
    _require(all(agentcore.checks.values()), "an AgentCore check failed")
    _require(agentcore.leaks == (), "the AgentCore proof recorded a leak")
    _require(len(real.memory_calls) == 2, "the receipt sequence is not one pair")
    _require(set(agentcore.calls) == {"call-1", "call-2"}, "AgentCore is not one pair")
    _require(
        all(item.validation_status == "valid" for item in real.observations),
        "a hosted Decider answer was not valid",
    )
    _refuse_superseded_flow(real, agentcore)
    _cross_check(real, agentcore, measured)

    bound = real.max_diagnostic_reads
    controls = real.control_calls
    run_1, run_2 = real.memory_calls
    calls = (*controls, *real.memory_calls)
    choices = len(real.observations)
    first_offer = _one_observation(real, "empty")
    second_offer = _one_observation(real, "matched")
    control_offer = len(first_offer.option_ids)
    dead_end = next(
        receipt.public_tool_id for receipt in run_1.receipts if receipt.stored
    )
    preclusions = sum(1 for call in calls if call.blocked_tool_id is not None)
    cited = real.linkage.call_2_block_cites_call_1_receipt
    false_preclusions = sum(1 for call in calls if _false_preclusion(call, dead_end))
    if run_2.blocked_tool_id is not None and not cited:
        false_preclusions += 1
    control_choice, control_repeats = Counter(
        call.d1_choice for call in controls
    ).most_common(1)[0]
    cloud_1, cloud_2 = agentcore.calls["call-1"], agentcore.calls["call-2"]
    cloud_preclusions = sum(1 for call in (cloud_1, cloud_2) if call.blocked)
    exact = agentcore.checks.get("call2_history_matched_exact_receipt", False)
    cloud_false = sum(
        1 for call in (cloud_1, cloud_2) if _hosted_false_preclusion(call, exact)
    )
    answers = (
        agentcore.metrics.run_1.selector_inferences
        + agentcore.metrics.run_2_optimized.selector_inferences
    )
    timelines = sorted(agentcore.telemetry_summaries.values(), key=lambda t: t.emitted)

    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "classification": "synthetic",
        "what_this_is": (
            "The minimum causal facts from three private records on one declared "
            "synthetic repairable case, each proof run once, under a diagnostic "
            "read bound of one read per call. A memoryless control chose the same "
            "unproductive diagnostic on every call and stayed unresolved 3 of 3. "
            "With Decision Receipts, Run 1 made that same choice, spent its one "
            "read, stayed unresolved, and the verifier stored a Decision Receipt "
            "for the dead end. Run 2 is a new call on the same problem: the "
            "controller matched that receipt and precluded only the proven dead "
            "end before the first Decider request, Decider 2B then made one fresh "
            "choice over the three remaining diagnostics, the call spent one read, "
            "applied the repair and verified it. No second Decider call was made."
        ),
        "what_this_is_not": (
            "Not a rate, a benchmark, a statistical result, an accuracy figure or "
            "a cost claim: n = 1 case, and one sequence per proof. Not model "
            "learning: the checkpoint is unchanged between calls, and it chose "
            "the dead end on its first exposure to the full option set. Not an "
            "infinite loop: every call is bounded at one read and at most two "
            "Decider choices, and the observed Run 2 used one. No generalization "
            "to any other case, fixture, endpoint, model or workload. Each "
            "elapsed-time comparison recorded here is one observation at n = 1, "
            "not a general latency improvement claim."
        ),
        "identifiers_note": (
            "Generated from the private records through an allowlist. No "
            "account, resource, runtime, endpoint, session, request, decision, "
            "call or receipt identifier, no hash or digest, no model revision, "
            "no version, no path, no timestamp and no raw model output is "
            "carried."
        ),
        "scope": {
            "cases": count(1, "one declared synthetic repairable case"),
            "runs_of_each_proof": count(1, "each proof was run once"),
            "diagnostic_read_bound": count(
                bound,
                "read per call, configured below the absolute ceiling of "
                f"{MAX_DIAGNOSTIC_READS}",
            ),
            "decider_choice_bound": count(
                MAX_SELECTOR_CALLS, "per call, a first choice and one optional re-choice"
            ),
            "first_call_options": count(
                control_offer, "diagnostics offered before any dead end is proven"
            ),
            "options_after_preclusion": count(
                len(second_offer.option_ids),
                f"diagnostics left once the one proven dead end of "
                f"{control_offer} is precluded",
            ),
        },
        "hosted_decider_proof": {
            "where": (
                "An experiment harness called one pinned Strands Decider 2B "
                "checkpoint on a private Amazon SageMaker endpoint."
            ),
            "verdict": real.verdict,
            "checks_passed": count(
                sum(1 for check in real.checks if check.passed),
                f"of {len(real.checks)} checks",
            ),
            "hosted_decider_choices": count(
                choices, f"signed hosted choices across {len(calls)} calls"
            ),
            "without_receipts_control": {
                "independent_calls": count(len(controls), "memoryless calls"),
                "same_first_choice": count(
                    control_repeats,
                    f"of {len(controls)} calls chose {control_choice}",
                ),
                "unresolved": count(
                    sum(1 for call in controls if call.terminal_status == "unresolved"),
                    f"of {len(controls)} calls",
                ),
                "calls": [
                    _real_call(f"Control {index}", call, bound, control_offer)
                    for index, call in enumerate(controls, start=1)
                ],
            },
            "with_receipts": [
                _real_call("Run 1", run_1, bound, len(first_offer.option_ids)),
                _real_call("Run 2", run_2, bound, len(second_offer.option_ids)),
            ],
            "linkage": {
                "run_1_receipt_cites_run_1_decider_choice": (
                    real.linkage.call_1_receipt_cites_call_1_d1_decision
                ),
                "run_2_preclusion_cites_run_1_receipt": cited,
                "run_2_preclusion_precedes_any_read": (
                    real.linkage.call_2_block_precedes_any_read
                ),
                "run_2_preclusion_precedes_first_decider_call": (
                    real.linkage.call_2_block_precedes_selector_request
                ),
                "run_2_decider_choice_differs_from_precluded_dead_end": (
                    real.linkage.call_2_d1_differs_from_blocked_choice
                ),
            },
            "preclusions": count(preclusions, f"across {len(calls)} calls"),
            "false_preclusions": count(
                false_preclusions,
                f"of {preclusions} preclusion in the observed sequence of "
                f"{choices} Decider choices",
            ),
        },
        "agentcore_proof": {
            "where": (
                "The controller on a private Amazon Bedrock AgentCore Runtime, "
                "calling the same private Decider 2B endpoint."
            ),
            "verdict": agentcore.verdict,
            "checks_passed": count(
                sum(1 for value in agentcore.checks.values() if value),
                f"of {len(agentcore.checks)} checks",
            ),
            "calls": [
                _hosted_call("Run 1", cloud_1, bound),
                _hosted_call("Run 2", cloud_2, bound),
            ],
            "decider_answers_served": count(
                answers, "answers, one per call, every one accepted with HTTP 200"
            ),
            "preclusions": count(cloud_preclusions, "across 2 calls"),
            "false_preclusions": count(
                cloud_false,
                f"of {cloud_preclusions} preclusion in the observed sequence",
            ),
            "narration_rule": {
                "run_1_narration_not_permitted": agentcore.checks.get(
                    "call1_narration_not_permitted"
                ),
                "run_2_narrated_after_completion_only": agentcore.checks.get(
                    "call2_narration_after_completion_only"
                ),
            },
            "telemetry_timelines": [
                {
                    "events": count(timeline.emitted, "events emitted"),
                    "expected": count(timeline.expected_event_count, "events"),
                    "finalized": timeline.finalized,
                    "dropped": count(timeline.dropped, f"of {timeline.emitted}"),
                    "refused": count(timeline.refused, f"of {timeline.emitted}"),
                }
                for timeline in timelines
            ],
        },
        "measured_latency": _measured_latency(agentcore, measured),
        "earlier_evidence": (
            "Two earlier results stand and are superseded as the lead claim, "
            "not erased. The post-D1 proof ran the same case through the "
            "superseded flow: Decider 2B was offered the full option set, "
            "repeated the proven dead end, the controller refused that exact "
            "repeat before any read, and a second Decider call chose "
            "inspect_schema_keys, so Run 2 there spent two inferences instead "
            "of one. Before that, a two-read cloud pair showed one fewer read "
            "on a single pair once the bound allowed two reads, which is "
            "efficiency only and cannot show an escape."
        ),
        "not_claimed": [
            "nothing beyond the calls recorded here",
            "a rate or statistical significance",
            "accuracy or calibration",
            "model learning: the checkpoint is not changed between calls",
            "a literal infinite loop",
            "generalization beyond this case, fixture, endpoint and checkpoint",
            "production readiness or novelty",
            "a dollar or cost saving",
            "a latency claim beyond this one observed n = 1 comparison",
        ],
    }
    return record


def render(record: dict[str, object]) -> str:
    """Canonical text, refused if any public scan pattern matches it."""
    text = json.dumps(record, indent=2, sort_keys=False) + "\n"
    findings = scan_text(OUTPUT_LABEL, text)
    if findings:
        detail = "; ".join(f"{item.check} {item.matched!r}" for item in findings)
        raise EvidenceRefused(f"the curated record would leak: {detail}")
    return text


def load(
    real_path: Path, agentcore_path: Path, measurements_path: Path
) -> tuple[RealProof, AgentCoreProof, Measurements]:
    real = RealProof.model_validate_json(real_path.read_text(encoding="utf-8"))
    agentcore = AgentCoreProof.model_validate_json(
        agentcore_path.read_text(encoding="utf-8")
    )
    measured = Measurements.model_validate_json(
        measurements_path.read_text(encoding="utf-8")
    )
    return real, agentcore, measured


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("real", type=Path, help="the hosted Decider proof record")
    parser.add_argument("agentcore", type=Path, help="the AgentCore proof record")
    parser.add_argument("measurements", type=Path, help="the measurement summary")
    parser.add_argument("out", type=Path, help="where to write the curated record")
    arguments = parser.parse_args(argv)
    real, agentcore, measured = load(
        arguments.real, arguments.agentcore, arguments.measurements
    )
    arguments.out.write_text(
        render(build(real, agentcore, measured)), encoding="utf-8"
    )
    return 0


__all__ = [
    "BASIS",
    "OUTPUT_LABEL",
    "SCHEMA_VERSION",
    "AgentCoreProof",
    "EvidenceRefused",
    "Measurements",
    "RealProof",
    "build",
    "count",
    "load",
    "main",
    "measure",
    "render",
]


if __name__ == "__main__":  # pragma: no cover - command line entry point
    raise SystemExit(main())
