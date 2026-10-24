"""Inventory current observable decisions and refuse unsupported holdouts."""

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any

from experiments.decider_finetune.corpus import signature as legacy_signature
from experiments.decider_finetune.oracle import (
    REVIEW,
    Config,
    Labeled,
    Prompt,
    bindings,
    label_after,
    label_fresh,
)
from experiments.decider_finetune.scenarios import (
    ASSIGNMENT,
    CATALOGS,
    PARTITIONS,
    Partition,
    Scenario,
)
from unstuck.cascade.choice import REVIEW_CRITERION, criterion_for
from unstuck.cascade.projector import QUESTION, SelectorInput

SEEDS = (17, 29, 43)
CONFIG = Config(model_revision="scripted-v2")
MIN_TEST_SIGNATURES = 3
MIN_CAPABILITIES = 2
ROOT = Path(__file__).resolve().parents[2]
PREREGISTRATION = ROOT / ".unlazy/true-generalization-v2/GATES.md"


class Refused(ValueError):
    """The observed input cannot support the frozen protocol."""


def _project(
    value: Mapping[str, Any], allowed: str, dropped: str = ""
) -> dict[str, Any]:
    if set(value) - set(allowed.split()):
        raise Refused("unsupported observable field")
    return {key: item for key, item in value.items() if key not in dropped.split()}


def _ordered(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))


def signature(prompt: Prompt, catalog_name: str) -> str:
    """Remove only known surface variation from the actual model input."""
    state = json.loads(prompt.state_text)
    payload = SelectorInput.model_validate_json(prompt.state_text)
    if prompt.question != QUESTION or payload.question != QUESTION:
        raise Refused("unsupported question")
    if payload.pass_index != 1 or payload.first is not None or payload.blocked:
        raise Refused("later decisions require a separate protocol")
    if payload.truncated or any(item.truncated for item in payload.observations):
        raise Refused("truncated observable state")
    catalog = CATALOGS[catalog_name]
    expected_cards = {
        item.card.public_tool_id: item.card for item in bindings(catalog.probes())
    }
    if {card.public_tool_id: card for card in payload.cards} != expected_cards:
        raise Refused("unknown catalog semantics")
    options = dict(prompt.options)
    expected_options = {
        identifier: criterion_for(expected_cards[identifier])
        for identifier in payload.available_tool_ids
    } | {REVIEW: REVIEW_CRITERION}
    if len(options) != len(prompt.options) or options != expected_options:
        raise Refused("options disagree with observable state")
    canonical = _project(
        state,
        "schema_version question review_option pass_index task_hash query_hash "
        "base_query_hash dataset_id versions problem_fingerprint decision_context_hash "
        "cards available_tool_ids history budgets blocked precluded first observations truncated",
        "task_hash query_hash base_query_hash dataset_id versions problem_fingerprint "
        "decision_context_hash question review_option",
    )
    _project(
        state["versions"],
        "schema_version calibration_hash catalog_version data_schema_version dataset_version "
        "model_revision policy_hash tool_contract_version",
    )
    canonical["cards"] = _ordered(
        _project(
            card.model_dump(mode="json"),
            "schema_version public_tool_id description applies_when observable_facts "
            "input_schema cost_class catalog_version tool_contract_version",
            "public_tool_id description applies_when catalog_version tool_contract_version",
        )
        | {"capability": catalog.capability(card.public_tool_id)}
        for card in payload.cards
    )
    canonical["available_tool_ids"] = sorted(
        catalog.capability(identifier) for identifier in payload.available_tool_ids
    )
    observations = []
    for item in state["observations"]:
        observation = _project(
            item,
            "schema_version evidence_id kind public_tool_id dataset_version arguments_hash "
            "summary truncated",
            "evidence_id dataset_version arguments_hash summary",
        )
        if item["kind"] != "query" or item["public_tool_id"] != "execute_report":
            raise Refused("unsupported observation kind")
        observation["facts"] = _project(
            json.loads(item["summary"]),
            "encoding_proven query_hash row_count source_complete",
            "query_hash",
        )
        if set(observation["facts"]) != {
            "encoding_proven",
            "row_count",
            "source_complete",
        }:
            raise Refused("incomplete observation facts")
        observations.append(observation)
    canonical["observations"] = observations
    history = _project(
        state["history"], "schema_version matches status truncated retrieval_truncated"
    )
    matches = []
    for match in history["matches"]:
        if catalog.capability(match["public_tool_id"]) != match["selected_capability"]:
            raise Refused("receipt capability mismatch")
        matches.append(
            _project(
                match,
                "schema_version admitted_evidence arguments_hash match_class moved_disposition "
                "outcome_class public_tool_id reason_code receipt_id selected_capability",
                "public_tool_id arguments_hash receipt_id",
            )
        )
    canonical["history"] = history | {"matches": _ordered(matches)}
    precluded = []
    for item in state.get("precluded", []):
        if catalog.capability(item["public_tool_id"]) != item["selected_capability"]:
            raise Refused("preclusion capability mismatch")
        precluded.append(
            _project(
                item,
                "schema_version arguments_hash blocked_class outcome_class public_tool_id "
                "reason_code receipt_id selected_capability",
                "public_tool_id arguments_hash receipt_id",
            )
        )
    canonical["precluded"] = _ordered(precluded)
    canonical["budgets"] = _project(
        state["budgets"],
        "schema_version remaining_diagnostic_reads remaining_ms remaining_selector_calls",
    )
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def verifier_label(labeled: Labeled, catalog_name: str) -> str | None:
    """Accept only complete, consistent counterfactual verifier outcomes."""
    offered = labeled.captured.available_tool_ids
    outcomes = labeled.outcomes
    if len(outcomes) != len(offered) or {item.public_id for item in outcomes} != set(
        offered
    ):
        raise Refused("incomplete counterfactual outcomes")
    winners = []
    for outcome in outcomes:
        productive = (
            outcome.first_read_outcome == "useful_evidence"
            and outcome.verified
            and not outcome.false_repair
        )
        if outcome.productive != productive:
            raise Refused("inconsistent verifier outcome")
        if CATALOGS[catalog_name].capability(outcome.public_id) != outcome.capability:
            raise Refused("outcome capability mismatch")
        if productive:
            winners.append(outcome.capability)
    return winners[0] if len(winners) == 1 else None


@dataclass(frozen=True)
class Decision:
    family: str
    original_partition: Partition
    signature: str
    legacy_signature: str
    label: str


@dataclass(frozen=True)
class Inventory:
    decisions: tuple[Decision, ...]
    absent: int = 0
    no_productive: int = 0
    ambiguous: int = 0


def inspect_scenario(scenario: Scenario) -> Inventory:
    decisions = []
    absent = no_productive = ambiguous = 0

    def record(labeled: Labeled | None) -> None:
        nonlocal absent, no_productive, ambiguous
        if labeled is None:
            absent += 1
            return
        normalized = signature(labeled.captured.prompt, scenario.catalog)
        label = verifier_label(labeled, scenario.catalog)
        if label is None:
            if labeled.productive:
                ambiguous += 1
            else:
                no_productive += 1
            return
        if scenario.partition not in PARTITIONS:
            raise Refused("only the preregistered assigned inventory is supported")
        partition: Partition = next(
            part for part in PARTITIONS if part == scenario.partition
        )
        decisions.append(
            Decision(
                scenario.family,
                partition,
                normalized,
                legacy_signature(
                    {
                        "state_text": labeled.captured.prompt.state_text,
                        "catalog": scenario.catalog,
                        "offered_capabilities": [
                            CATALOGS[scenario.catalog].capability(identifier)
                            for identifier in labeled.captured.available_tool_ids
                        ],
                    }
                ),
                label,
            )
        )

    first = label_fresh(scenario, 1, CONFIG)
    record(first)
    record(label_fresh(scenario, 2, CONFIG))
    if first is not None:
        for prior in first.captured.available_tool_ids:
            after = label_after(scenario, prior, CONFIG)
            record(None if after is None else after[1])
    return Inventory(tuple(decisions), absent, no_productive, ambiguous)


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
        tuple(
            decision for inventory in inventories for decision in inventory.decisions
        ),
        sum(item.absent for item in inventories),
        sum(item.no_productive for item in inventories),
        sum(item.ambiguous for item in inventories),
    )


def components(decisions: Sequence[Decision]) -> tuple[frozenset[str], ...]:
    """Whole source families sharing a signature cannot cross partitions."""
    groups = [{decision.family} for decision in decisions]
    signatures: dict[str, set[str]] = defaultdict(set)
    labels: dict[str, set[str]] = defaultdict(set)
    for decision in decisions:
        signatures[decision.signature].add(decision.family)
        labels[decision.signature].add(decision.label)
    if any(len(values) != 1 for values in labels.values()):
        raise Refused("one observable signature has conflicting verifier labels")
    for families in signatures.values():
        connected = [group for group in groups if group & families]
        groups = [group for group in groups if not group & families]
        groups.append(set().union(*connected))
    return tuple(
        sorted((frozenset(group) for group in groups), key=lambda group: sorted(group))
    )


def split_failures(splits: Mapping[str, Sequence[Decision]]) -> list[str]:
    if set(splits) != set(PARTITIONS):
        return ["missing_or_extra_partition"]
    signatures: dict[str, set[str]] = {
        part: {row.signature for row in splits[part]} for part in PARTITIONS
    }
    families: dict[str, set[str]] = {
        part: {row.family for row in splits[part]} for part in PARTITIONS
    }
    failures = []
    for part in PARTITIONS:
        if not splits[part]:
            failures.append(f"empty_{part}")
        if len({row.label for row in splits[part]}) < MIN_CAPABILITIES:
            failures.append(f"productive_capabilities_{part}")
    for first, second in (
        ("train", "calibration"),
        ("train", "test"),
        ("calibration", "test"),
    ):
        if signatures[first] & signatures[second]:
            failures.append(f"signature_overlap_{first}_{second}")
        if families[first] & families[second]:
            failures.append(f"family_overlap_{first}_{second}")
    if (
        len(signatures["test"] - signatures["train"] - signatures["calibration"])
        < MIN_TEST_SIGNATURES
    ):
        failures.append("unseen_test_signatures")
    return failures


def allocation(decisions: Sequence[Decision]) -> dict[str, list[Decision]] | None:
    groups = components(decisions)
    if len(groups) < 3:
        return None
    if len(groups) > 13:
        raise Refused("inventory exceeds the preregistered bounded search")
    for assignment in product(PARTITIONS, repeat=len(groups)):
        if set(assignment) != set(PARTITIONS):
            continue
        family_split = {
            family: partition
            for group, partition in zip(groups, assignment, strict=True)
            for family in group
        }
        splits: dict[str, list[Decision]] = {part: [] for part in PARTITIONS}
        for row in decisions:
            splits[family_split[row.family]].append(row)
        if not split_failures(splits):
            return splits
    return None


def summarize(inventory: Inventory) -> dict[str, Any]:
    decisions = inventory.decisions
    groups = components(decisions)
    splits = allocation(decisions)
    legacy = {
        part: {
            row.legacy_signature for row in decisions if row.original_partition == part
        }
        for part in PARTITIONS
    }
    current = {
        part: {row.signature for row in decisions if row.original_partition == part}
        for part in PARTITIONS
    }
    proposed = None
    if splits is not None:
        proposed = {
            "signature_counts": {
                part: len({row.signature for row in rows})
                for part, rows in splits.items()
            },
            "train_test_intersection": len(
                {row.signature for row in splits["train"]}
                & {row.signature for row in splits["test"]}
            ),
            "unseen_test_signatures": len(
                {row.signature for row in splits["test"]}
                - {row.signature for row in splits["train"]}
                - {row.signature for row in splits["calibration"]}
            ),
        }
    return {
        "verdict": "VIABLE_SCRIPTED_ONLY" if splits else "STOP_INSUFFICIENT_HOLDOUT",
        "scope": "assigned_v1_inventory_six_variants_production_catalog",
        "rows": len(decisions),
        "families": len({row.family for row in decisions}),
        "signature_count": len({row.signature for row in decisions}),
        "legacy_signature_count": len({row.legacy_signature for row in decisions}),
        "legacy_partition_signature_counts": {
            part: len(values) for part, values in legacy.items()
        },
        "legacy_train_test_intersection": len(legacy["train"] & legacy["test"]),
        "canonical_partition_signature_counts": {
            part: len(values) for part, values in current.items()
        },
        "canonical_train_test_intersection": len(current["train"] & current["test"]),
        "existing_unseen_test_signatures": len(
            current["test"] - current["train"] - current["calibration"]
        ),
        "productive_capabilities": sorted({row.label for row in decisions}),
        "productive_capability_counts": dict(
            sorted(Counter(row.label for row in decisions).items())
        ),
        "independent_components": len(groups),
        "component_family_counts": sorted(len(group) for group in groups),
        "excluded": {
            "absent": inventory.absent,
            "no_productive": inventory.no_productive,
            "ambiguous": inventory.ambiguous,
        },
        "proposed_v2_split": proposed,
        "fixed_seeds": list(SEEDS),
        "training_executed": False,
        "model_inference_executed": False,
        "test_experiment_opened": False,
        "safety_claim": False,
        "limitation": "No admissible family/component holdout in this inventory; no claim about new observable states."
        if splits is None
        else "Structural viability only; this inspected inventory is not an untouched test.",
    }


def check_preregistration(path: Path = PREREGISTRATION) -> None:
    if not path.is_file() or "Status: PREREGISTERED" not in path.read_text():
        raise Refused("missing preregistration; do not inspect inventory")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        check_preregistration()
        report = summarize(collect(args.workers))
    except (ValueError, KeyError, TypeError) as error:
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
    return 0 if report["proposed_v2_split"] is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
