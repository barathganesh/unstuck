"""Structural tests use invented tokens, never a model evaluation test set."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from experiments.decider_finetune.oracle import (
    Labeled,
    Prompt,
    label_after,
    label_fresh,
)
from experiments.decider_finetune.scenarios import (
    ASSIGNMENT,
    CATALOGS,
    PARTITIONS,
    Scenario,
)
from experiments.decider_finetune_v2 import viability
from experiments.decider_finetune_v2.viability import (
    CONFIG,
    Decision,
    Inventory,
    Refused,
    allocation,
    check_preregistration,
    components,
    inspect_scenario,
    signature,
    split_failures,
    summarize,
    verifier_label,
)


@pytest.fixture(scope="module")
def fresh() -> Labeled:
    labeled = label_fresh(
        Scenario("train", "arrival_pending_plain", 0, "production"), 1, CONFIG
    )
    assert labeled is not None
    return labeled


@pytest.fixture(scope="module")
def receipt() -> Labeled:
    labeled = label_after(
        Scenario("train", "arrival_pending_plain", 0, "production"),
        "inspect_schema_keys",
        CONFIG,
    )
    assert labeled is not None
    return labeled[1]


def changed(prompt: Prompt, state: dict[str, object]) -> Prompt:
    return replace(prompt, state_text=json.dumps(state))


def test_identity_and_option_order_never_create_novelty(fresh: Labeled) -> None:
    prompt = fresh.captured.prompt
    state = json.loads(prompt.state_text)
    state["dataset_id"] = "different-dataset"
    for key in (
        "task_hash",
        "query_hash",
        "base_query_hash",
        "decision_context_hash",
        "problem_fingerprint",
    ):
        state[key] = "a" * 64
    state["versions"]["dataset_version"] = "different-version"
    state["versions"]["model_revision"] = "different-name"
    state["observations"][0]["evidence_id"] = "different-evidence"
    state["observations"][0]["arguments_hash"] = "b" * 64
    facts = json.loads(state["observations"][0]["summary"])
    facts["query_hash"] = "c" * 64
    state["observations"][0]["summary"] = json.dumps(facts)
    state["cards"].reverse()
    state["available_tool_ids"].reverse()
    varied = replace(changed(prompt, state), options=tuple(reversed(prompt.options)))
    assert signature(varied, "production") == signature(prompt, "production")


@pytest.mark.parametrize("catalog", ["r1", "r2", "r3", "r4", "r5", "r6"])
def test_registered_names_and_paraphrases_are_not_unseen(
    fresh: Labeled, catalog: str
) -> None:
    labeled = label_fresh(
        Scenario("train", "arrival_pending_plain", 5, catalog), 1, CONFIG
    )
    assert labeled is not None
    assert signature(labeled.captured.prompt, catalog) == signature(
        fresh.captured.prompt, "production"
    )


def test_receipt_renaming_does_not_create_novelty(receipt: Labeled) -> None:
    catalog = CATALOGS["r6"]
    labeled = label_after(
        Scenario("test", "arrival_pending_prefixed", 3, "r6"),
        catalog.public_id("inspect_schema_keys"),
        CONFIG,
    )
    assert labeled is not None
    assert signature(labeled[1].captured.prompt, "r6") == signature(
        receipt.captured.prompt, "production"
    )


@pytest.mark.parametrize("fact", ["encoding_proven", "source_complete", "row_count"])
def test_observable_facts_are_preserved(fresh: Labeled, fact: str) -> None:
    prompt = fresh.captured.prompt
    state = json.loads(prompt.state_text)
    facts = json.loads(state["observations"][0]["summary"])
    facts[fact] = 1 if fact == "row_count" else not facts[fact]
    state["observations"][0]["summary"] = json.dumps(facts)
    assert signature(changed(prompt, state), "production") != signature(
        prompt, "production"
    )


def test_budgets_and_history_are_semantic(fresh: Labeled, receipt: Labeled) -> None:
    prompt = fresh.captured.prompt
    state = json.loads(prompt.state_text)
    state["budgets"]["remaining_diagnostic_reads"] = 2
    assert signature(changed(prompt, state), "production") != signature(
        prompt, "production"
    )
    assert signature(receipt.captured.prompt, "production") != signature(
        prompt, "production"
    )
    state = json.loads(receipt.captured.prompt.state_text)
    state["history"]["truncated"] = True
    assert signature(
        changed(receipt.captured.prompt, state), "production"
    ) != signature(receipt.captured.prompt, "production")
    state = json.loads(prompt.state_text)
    state["history"]["status"] = "unavailable"
    state["history"]["retrieval_truncated"] = True
    assert signature(changed(prompt, state), "production") != signature(
        prompt, "production"
    )


@pytest.mark.parametrize("where", ["state", "facts", "budgets", "history", "versions"])
def test_unknown_fields_fail_closed(fresh: Labeled, where: str) -> None:
    prompt = fresh.captured.prompt
    state = json.loads(prompt.state_text)
    if where == "facts":
        facts = json.loads(state["observations"][0]["summary"])
        facts["new_semantic_field"] = True
        state["observations"][0]["summary"] = json.dumps(facts)
    elif where == "state":
        state["new_semantic_field"] = True
    else:
        state[where]["new_semantic_field"] = True
    with pytest.raises(ValueError):
        signature(changed(prompt, state), "production")


@pytest.mark.parametrize(
    "change", ["question", "option", "card", "truncated", "facts_missing"]
)
def test_unsupported_inputs_are_not_silently_normalized(
    fresh: Labeled, change: str
) -> None:
    prompt = fresh.captured.prompt
    state = json.loads(prompt.state_text)
    if change == "question":
        prompt = replace(prompt, question="A different decision")
    elif change == "option":
        prompt = replace(prompt, options=prompt.options[:-1])
    elif change == "card":
        state["cards"][0]["observable_facts"] = ["new_fact"]
    elif change == "truncated":
        state["truncated"] = True
    else:
        state["observations"][0]["summary"] = "{}"
    with pytest.raises(ValueError):
        signature(changed(prompt, state), "production")


def test_labels_come_from_verifier_outcomes(fresh: Labeled) -> None:
    assert verifier_label(fresh, "production") == "inspect_pipeline"
    rejected = replace(
        fresh,
        outcomes=tuple(
            replace(outcome, productive=False, verified=False)
            for outcome in fresh.outcomes
        ),
    )
    assert verifier_label(rejected, "production") is None
    ambiguous = replace(
        fresh,
        outcomes=tuple(
            replace(
                outcome,
                first_read_outcome="useful_evidence",
                productive=True,
                verified=True,
                false_repair=False,
            )
            for outcome in fresh.outcomes
        ),
    )
    assert verifier_label(ambiguous, "production") is None


def test_incomplete_or_self_contradictory_labels_refused(fresh: Labeled) -> None:
    with pytest.raises(Refused, match="incomplete"):
        verifier_label(replace(fresh, outcomes=fresh.outcomes[:-1]), "production")
    winner = next(outcome for outcome in fresh.outcomes if outcome.productive)
    for invalid in (
        replace(winner, verified=False),
        replace(winner, false_repair=True),
    ):
        with pytest.raises(Refused, match="inconsistent"):
            verifier_label(
                replace(
                    fresh,
                    outcomes=tuple(
                        invalid if item == winner else item for item in fresh.outcomes
                    ),
                ),
                "production",
            )


def decision(family: str, situation: str, label: str = "capability-a") -> Decision:
    return Decision(family, "train", situation, situation, label)


def test_components_are_transitive_and_family_atomic() -> None:
    rows = [
        decision("alpha", "one"),
        decision("beta", "one"),
        decision("beta", "two"),
        decision("gamma", "two"),
        decision("delta", "three"),
    ]
    assert set(components(rows)) == {
        frozenset({"alpha", "beta", "gamma"}),
        frozenset({"delta"}),
    }
    assert allocation(rows) is None


def test_conflicting_visible_twins_refused() -> None:
    with pytest.raises(Refused, match="conflicting"):
        components(
            [decision("alpha", "same"), decision("beta", "same", "capability-b")]
        )


def independent_rows() -> list[Decision]:
    return [
        decision(family, f"{family}-{index}", f"capability-{index % 2}")
        for family in ("alpha", "beta", "gamma")
        for index in range(3)
    ]


def test_valid_structural_holdout_is_possible_on_control_inventory() -> None:
    rows = independent_rows()
    splits = allocation(rows)
    assert splits is not None
    assert not split_failures(splits)
    assert sum(len(partition) for partition in splits.values()) == len(rows)
    assert allocation(tuple(reversed(rows))) == {
        part: list(reversed(values)) for part, values in splits.items()
    }
    report = summarize(Inventory(tuple(rows)))
    assert report["proposed_v2_split"]["train_test_intersection"] == 0
    assert report["proposed_v2_split"]["unseen_test_signatures"] >= 3
    assert not report["test_experiment_opened"]


@pytest.mark.parametrize(
    "failure", ["overlap", "family", "minimum", "capability", "partition"]
)
def test_invalid_splits_refused(failure: str) -> None:
    splits = allocation(independent_rows())
    assert splits is not None
    if failure == "overlap":
        splits["test"][0] = replace(
            splits["test"][0], signature=splits["train"][0].signature
        )
    elif failure == "family":
        splits["test"][0] = replace(splits["test"][0], family=splits["train"][0].family)
    elif failure == "minimum":
        splits["test"] = splits["test"][:2]
    elif failure == "capability":
        splits["test"] = [
            replace(row, label="only-capability") for row in splits["test"]
        ]
    else:
        del splits["calibration"]
    assert split_failures(splits)


def test_empty_inventory_never_passes() -> None:
    report = summarize(Inventory(()))
    assert report["verdict"] == "STOP_INSUFFICIENT_HOLDOUT"
    assert report["proposed_v2_split"] is None


def test_preregistration_is_required(tmp_path: Path) -> None:
    check_preregistration()
    with pytest.raises(Refused, match="preregistration"):
        check_preregistration(tmp_path / "absent.md")


def test_cli_stops_before_inventory_without_preregistration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse() -> None:
        raise Refused("missing preregistration")

    def forbidden(workers: int) -> Inventory:
        raise AssertionError("inventory must not open")

    monkeypatch.setattr(viability, "check_preregistration", refuse)
    monkeypatch.setattr(viability, "collect", forbidden)
    assert viability.main([]) == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "STOP_INVALID_INVENTORY"


def test_real_family_inventory_cannot_be_resplit() -> None:
    inventories = [
        inspect_scenario(Scenario(partition, family, 0, "production"))
        for partition in PARTITIONS
        for family in ASSIGNMENT[partition]
    ]
    inventory = Inventory(tuple(row for item in inventories for row in item.decisions))
    report = summarize(inventory)
    assert (
        report["legacy_train_test_intersection"]
        == report["legacy_partition_signature_counts"]["test"]
    )
    assert report["existing_unseen_test_signatures"] == 0
    assert len(report["productive_capabilities"]) == 2
    assert report["families"] == sum(len(families) for families in ASSIGNMENT.values())
    assert allocation(inventory.decisions) is None
    assert report["proposed_v2_split"] is None
    assert report["verdict"] == "STOP_INSUFFICIENT_HOLDOUT"
    assert not report["safety_claim"]
