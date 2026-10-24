"""Test whether receipt-enriched later-call states support a decision holdout.

A later call is a new pass-one decision on one receipt namespace after one or
two scripted prior calls, each with read budget 1. Its state carries the
prior verifier-owned receipts and any exact-repeat preclusion. Every
candidate it offers is executed counterfactually through the real controller
and verifier. Nothing here adds a tool, a runtime fact, or a fixture.
"""

import argparse
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any

from experiments.decider_finetune.oracle import (
    REVIEW,
    CallTrace,
    Config,
    Labeled,
    _outcome,
    _single,
    choose,
    label_fresh,
    run_call,
)
from experiments.decider_finetune.scenarios import (
    ASSIGNMENT,
    CATALOGS,
    PARTITIONS,
    PRODUCTION,
    Partition,
    Scenario,
)
from experiments.decider_finetune_v2.viability import (
    CONFIG,
    MIN_CAPABILITIES,
    MIN_TEST_SIGNATURES,
    ROOT,
    Decision,
    Refused,
    allocation,
    check_preregistration,
    components,
    signature,
    verifier_label,
)
from unstuck.cascade.repository import LocalReceiptRepository

PHASE2_PREREGISTRATION = ROOT / ".unlazy/true-generalization-v2/RECEIPT-STATES-GATES.md"
CAPABILITIES = PRODUCTION.order
MAX_DEPTH = 2
MIN_CHOICES = 2
CORE_FIELDS = ("budgets", "cards", "observations")


class PriorNotExecuted(Exception):
    """A scheduled prior read was not offered or not executed in its call."""


@dataclass(frozen=True)
class State:
    family: str
    partition: Partition
    depth: int
    signature: str
    label: str
    choices: int

    def decision(self) -> Decision:
        return Decision(
            self.family, self.partition, self.signature, self.signature, self.label
        )


@dataclass(frozen=True)
class Inventory:
    states: tuple[State, ...]
    absent: int = 0
    no_productive: int = 0
    ambiguous: int = 0
    prior_not_executed: int = 0


def _replay(
    scenario: Scenario,
    priors: Sequence[str],
    store: LocalReceiptRepository,
    config: Config,
) -> None:
    """Run the scripted prior calls, each executing exactly its scheduled read."""
    catalog = CATALOGS[scenario.catalog]
    for index, capability in enumerate(priors, start=1):
        public_id = catalog.public_id(capability)
        trace = run_call(
            scenario,
            call_index=index,
            budget=1,
            store=store,
            answerer=choose(public_id),
            config=config,
        )
        if tuple(receipt.public_tool_id for receipt in trace.receipts) != (public_id,):
            raise PriorNotExecuted(capability)


def label_chain(
    scenario: Scenario, priors: Sequence[str], config: Config = CONFIG
) -> Labeled | None:
    """Label the pass-one decision of the call after `priors` on one namespace."""
    if not 1 <= len(priors) <= MAX_DEPTH:
        raise Refused("receipt depth outside the preregistered range")
    call_index = len(priors) + 1

    def labeled_call(store: LocalReceiptRepository, answer: str) -> CallTrace:
        return run_call(
            scenario,
            call_index=call_index,
            budget=1,
            store=store,
            answerer=choose(answer),
            config=config,
        )

    store = LocalReceiptRepository()
    _replay(scenario, priors, store, config)
    probe = labeled_call(store, REVIEW)
    if not probe.captured:
        return None
    traces = {}
    for public_id in probe.captured[0].available_tool_ids:
        store = LocalReceiptRepository()
        _replay(scenario, priors, store, config)
        traces[public_id] = labeled_call(store, public_id)
    captured = _single([probe, *traces.values()])
    return Labeled(
        captured,
        tuple(_outcome(trace, scenario, pid) for pid, trace in traces.items()),
    )


def inspect_scenario(scenario: Scenario) -> Inventory:
    if scenario.partition not in PARTITIONS:
        raise Refused("only the preregistered assigned inventory is supported")
    partition: Partition = next(
        part for part in PARTITIONS if part == scenario.partition
    )
    states: list[State] = []
    counts: Counter[str] = Counter()

    def record(depth: int, labeled: Labeled | None) -> None:
        if labeled is None:
            counts["absent"] += 1
            return
        normalized = signature(labeled.captured.prompt, scenario.catalog)
        label = verifier_label(labeled, scenario.catalog)
        if label is None:
            counts["ambiguous" if labeled.productive else "no_productive"] += 1
            return
        states.append(
            State(
                scenario.family,
                partition,
                depth,
                normalized,
                label,
                len(labeled.captured.available_tool_ids),
            )
        )

    def chain(priors: tuple[str, ...]) -> None:
        try:
            record(len(priors), label_chain(scenario, priors))
        except PriorNotExecuted:
            counts["prior_not_executed"] += 1

    first = label_fresh(scenario, 1, CONFIG)
    record(0, first)
    record(0, label_fresh(scenario, 2, CONFIG))
    if first is not None:
        catalog = CATALOGS[scenario.catalog]
        for public_id in first.captured.available_tool_ids:
            chain((catalog.capability(public_id),))
    for pair in product(CAPABILITIES, repeat=2):
        chain(pair)
    return Inventory(
        tuple(states),
        counts["absent"],
        counts["no_productive"],
        counts["ambiguous"],
        counts["prior_not_executed"],
    )


def collect(workers: int = 1) -> Inventory:
    if not 1 <= workers <= 8:
        raise Refused("workers must be between 1 and 8")
    scenarios = [
        Scenario(partition, family, variant, "production")
        for partition in PARTITIONS
        for family in ASSIGNMENT[partition]
        for variant in range(6)
    ]
    if workers == 1:
        inventories = list(map(inspect_scenario, scenarios))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            inventories = list(pool.map(inspect_scenario, scenarios))
    return Inventory(
        tuple(state for inventory in inventories for state in inventory.states),
        sum(item.absent for item in inventories),
        sum(item.no_productive for item in inventories),
        sum(item.ambiguous for item in inventories),
        sum(item.prior_not_executed for item in inventories),
    )


def core(normalized: str) -> str:
    """The receipt-free part of a signature: cards, observations, budgets."""
    state = json.loads(normalized)
    return json.dumps(
        {key: state[key] for key in CORE_FIELDS}, sort_keys=True, separators=(",", ":")
    )


def receipt_names_label(normalized: str, label: str) -> bool:
    """The label is the capability an exact useful receipt already names."""
    return any(
        match["match_class"] == "exact"
        and match["outcome_class"] == "useful_evidence"
        and match["selected_capability"] == label
        for match in json.loads(normalized)["history"]["matches"]
    )


def labels_by_signature(states: Iterable[State]) -> dict[str, str]:
    labels: dict[str, set[str]] = defaultdict(set)
    for state in states:
        labels[state.signature].add(state.label)
    if any(len(values) != 1 for values in labels.values()):
        raise Refused("one observable signature has conflicting verifier labels")
    return {key: next(iter(values)) for key, values in labels.items()}


def conditional_entropy(pairs: dict[str, str], key: Callable[[str], str]) -> float:
    """H(label | key(signature)) in bits, over distinct signatures."""
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    for normalized, label in pairs.items():
        groups[key(normalized)][label] += 1
    total = len(pairs)
    entropy = 0.0
    for counts in groups.values():
        size = sum(counts.values())
        entropy -= sum(
            size / total * (count / size) * math.log2(count / size)
            for count in counts.values()
        )
    # Adding zero turns a negative zero from empty sums into a plain zero.
    return entropy + 0.0


def _family_atomic(states: Sequence[State]) -> dict[str, Any]:
    decisions = [state.decision() for state in states]
    groups = components(decisions)
    labels = {
        group: {row.label for row in decisions if row.family in group}
        for group in groups
    }
    return {
        "states": len(states),
        "signatures": len({state.signature for state in states}),
        "components": len(groups),
        "component_family_counts": sorted(len(group) for group in groups),
        "components_with_multiple_capabilities": sum(
            len(values) >= MIN_CAPABILITIES for values in labels.values()
        ),
        "allocation_exists": bool(states) and allocation(decisions) is not None,
    }


def summarize(inventory: Inventory) -> dict[str, Any]:
    states = inventory.states
    pairs = labels_by_signature(states)
    train = {state.signature for state in states if state.depth <= 1}
    later = [state for state in states if state.depth == MAX_DEPTH]
    multi = [state for state in later if state.choices >= MIN_CHOICES]
    held_out = {state.signature for state in multi} - train
    train_cores: dict[str, set[str]] = defaultdict(set)
    for normalized in train:
        train_cores[core(normalized)].add(pairs[normalized])
    collapsed = {
        normalized
        for normalized in held_out
        if train_cores.get(core(normalized)) == {pairs[normalized]}
    }
    open_signatures = held_out - collapsed
    open_capabilities = sorted({pairs[normalized] for normalized in open_signatures})
    h_core = conditional_entropy(pairs, core)
    h_signature = conditional_entropy(pairs, lambda normalized: normalized)
    information = h_core - h_signature
    failures = []
    if len(held_out) < MIN_TEST_SIGNATURES:
        failures.append("held_out_signatures")
    if held_out & train:
        failures.append("signature_intersection")
    if len(open_signatures) < MIN_TEST_SIGNATURES:
        failures.append("collapse_to_training_core")
    if len(open_capabilities) < MIN_CAPABILITIES:
        failures.append("productive_capabilities")
    if information <= 1e-12:
        failures.append("no_receipt_information")
    by_depth = {}
    for depth in range(MAX_DEPTH + 1):
        rows = [state for state in states if state.depth == depth]
        choices: dict[str, set[str]] = defaultdict(set)
        for state in rows:
            choices[str(state.choices)].add(state.signature)
        by_depth[str(depth)] = {
            "rows": len(rows),
            "signatures": len({state.signature for state in rows}),
            "signatures_by_executable_choices": {
                count: len(values) for count, values in sorted(choices.items())
            },
            "productive_capability_counts": dict(
                sorted(Counter(state.label for state in rows).items())
            ),
        }
    return {
        "verdict": "STOP_RECEIPT_STATES_COLLAPSE"
        if failures
        else "VIABLE_SCRIPTED_ONLY",
        "failures": failures,
        "scope": "assigned_v1_inventory_six_variants_production_catalog_receipt_depth_0_to_2",
        "rows": len(states),
        "families": len({state.family for state in states}),
        "signature_count": len(pairs),
        "by_depth": by_depth,
        "excluded": {
            "absent": inventory.absent,
            "no_productive": inventory.no_productive,
            "ambiguous": inventory.ambiguous,
            "prior_not_executed": inventory.prior_not_executed,
        },
        "family_atomic": {
            "all_depths": _family_atomic(states),
            "receipt_depths_only": _family_atomic(
                [state for state in states if state.depth >= 1]
            ),
            "depth_2_multi_choice_only": _family_atomic(multi),
        },
        "decision_level": {
            "train_signatures": len(train),
            "depth_2_signatures": len({state.signature for state in later}),
            "depth_2_single_choice_signatures": len(
                {state.signature for state in later if state.choices < MIN_CHOICES}
            ),
            "depth_2_multi_choice_signatures": len(
                {state.signature for state in multi}
            ),
            "depth_2_multi_choice_seen_in_train": len(
                {state.signature for state in multi} & train
            ),
            "held_out_signatures": len(held_out),
            "train_held_out_intersection": len(held_out & train),
            "collapsed_to_training_core": len(collapsed),
            "non_collapsed": len(open_signatures),
            "receipt_names_label": sum(
                receipt_names_label(normalized, pairs[normalized])
                for normalized in held_out
            ),
            "held_out_capabilities": sorted({pairs[item] for item in held_out}),
            "non_collapsed_capabilities": open_capabilities,
            "shares_families_with_train": bool(
                {state.family for state in multi if state.signature in held_out}
                & {state.family for state in states if state.depth <= 1}
            ),
        },
        "information_bits": {
            "label_given_core": round(h_core, 6),
            "label_given_signature": round(h_signature, 6),
            "receipt_information": round(information, 6),
            "distinct_cores": len({core(normalized) for normalized in pairs}),
            "labels_per_core": sorted(
                len({pairs[item] for item in pairs if core(item) == key})
                for key in {core(normalized) for normalized in pairs}
            ),
        },
        "proposed_split": None,
        "factorial_prototype_executed": False,
        "training_executed": False,
        "model_inference_executed": False,
        "test_experiment_opened": False,
        "safety_claim": False,
    }


def check_phase2_preregistration(path: Path = PHASE2_PREREGISTRATION) -> None:
    check_preregistration()
    if not path.is_file() or "Status: PREREGISTERED" not in path.read_text():
        raise Refused("missing phase 2 preregistration; do not inspect inventory")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        check_phase2_preregistration()
        report = summarize(collect(args.workers))
    except (ValueError, KeyError, TypeError, RuntimeError) as error:
        print(
            json.dumps(
                {
                    "verdict": "STOP_INVALID_INVENTORY",
                    "error_type": type(error).__name__,
                }
            )
        )
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["verdict"] == "VIABLE_SCRIPTED_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
