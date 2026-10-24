"""Receipt-state viability tests. Invented tokens or real replays, never a test set."""

import json
from pathlib import Path
from typing import Any

import pytest

from experiments.decider_finetune.oracle import Labeled, label_after
from experiments.decider_finetune.scenarios import Scenario
from experiments.decider_finetune_v2 import receipt_states, viability
from experiments.decider_finetune_v2.receipt_states import (
    Inventory,
    PriorNotExecuted,
    State,
    check_phase2_preregistration,
    conditional_entropy,
    core,
    inspect_scenario,
    label_chain,
    labels_by_signature,
    receipt_names_label,
    summarize,
)
from experiments.decider_finetune_v2.viability import (
    CONFIG,
    Refused,
    signature,
    verifier_label,
)

PENDING = Scenario("train", "arrival_pending_plain", 0, "production")
DRIFT = Scenario("train", "key_drift_dimension", 0, "production")
HELD_DRIFT = Scenario("test", "key_drift_fact", 0, "production")
PIPELINE, SCHEMA, SCOPE = "inspect_pipeline", "inspect_schema_keys", "inspect_scope"


def chained(scenario: Scenario, *priors: str) -> Labeled:
    labeled = label_chain(scenario, priors)
    assert labeled is not None
    return labeled


def state_of(labeled: Labeled) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(
        signature(labeled.captured.prompt, "production")
    )
    return result


def test_one_receipt_chain_matches_existing_oracle() -> None:
    labeled = chained(PENDING, SCHEMA)
    after = label_after(PENDING, SCHEMA, CONFIG)
    assert after is not None
    assert labeled.captured.wire == after[1].captured.wire
    assert labeled.outcomes == after[1].outcomes


def test_two_blocking_receipts_leave_one_executable_choice() -> None:
    labeled = chained(PENDING, SCHEMA, SCOPE)
    state = state_of(labeled)
    assert state["available_tool_ids"] == [PIPELINE]
    assert len(state["history"]["matches"]) == 2
    assert {item["selected_capability"] for item in state["precluded"]} == {
        SCHEMA,
        SCOPE,
    }
    assert verifier_label(labeled, "production") == PIPELINE


def test_two_choice_states_carry_the_answer_in_a_receipt() -> None:
    forward = chained(PENDING, PIPELINE, SCHEMA)
    backward = chained(PENDING, SCHEMA, PIPELINE)
    assert len(forward.captured.available_tool_ids) == 2
    assert signature(forward.captured.prompt, "production") == signature(
        backward.captured.prompt, "production"
    )
    label = verifier_label(forward, "production")
    assert label == PIPELINE
    assert receipt_names_label(signature(forward.captured.prompt, "production"), label)


def test_receipts_from_another_family_reproduce_the_same_state() -> None:
    assert signature(
        chained(DRIFT, PIPELINE, SCOPE).captured.prompt, "production"
    ) == signature(chained(HELD_DRIFT, PIPELINE, SCOPE).captured.prompt, "production")


def test_precluded_prior_is_not_counted_as_a_receipt() -> None:
    with pytest.raises(PriorNotExecuted):
        label_chain(PENDING, (SCHEMA, SCHEMA))


@pytest.mark.parametrize("priors", [(), (PIPELINE, SCHEMA, SCOPE)])
def test_depth_outside_protocol_refused(priors: tuple[str, ...]) -> None:
    with pytest.raises(Refused, match="depth"):
        label_chain(PENDING, priors)


def synthetic(observed: str, receipts: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "budgets": {"remaining_diagnostic_reads": 1},
            "cards": ["card-a", "card-b", "card-c"],
            "observations": [observed],
            "history": {
                "matches": [
                    {
                        "match_class": "exact",
                        "outcome_class": "useful_evidence"
                        if receipt.startswith("useful")
                        else "disposition_unchanged",
                        "selected_capability": receipt.split(":")[-1],
                    }
                    for receipt in receipts
                ]
            },
        },
        sort_keys=True,
    )


def row(observed: str, receipts: tuple[str, ...], label: str, choices: int) -> State:
    return State(
        "alpha", "train", len(receipts), synthetic(observed, receipts), label, choices
    )


def test_core_ignores_receipts_and_keeps_observations() -> None:
    plain = synthetic("fact-x", ())
    assert core(plain) == core(synthetic("fact-x", ("other:cap-b",)))
    assert core(plain) != core(synthetic("fact-y", ()))
    assert receipt_names_label(synthetic("fact-x", ("useful:cap-a",)), "cap-a")
    assert not receipt_names_label(synthetic("fact-x", ("other:cap-a",)), "cap-a")


def test_entropy_measures_receipt_information() -> None:
    determined = {synthetic("x", ()): "a", synthetic("x", ("other:b",)): "a"}
    assert conditional_entropy(determined, core) == 0.0
    split = {synthetic("x", ()): "a", synthetic("x", ("other:b",)): "b"}
    assert conditional_entropy(split, core) == pytest.approx(1.0)
    assert conditional_entropy(split, lambda item: item) == 0.0


def test_discriminating_receipts_are_reported_viable() -> None:
    rows = [
        row("x", (), "cap-a", 3),
        row("x", ("other:cap-c",), "cap-a", 2),
        row("x", ("other:cap-c", "other:cap-d"), "cap-b", 2),
        row("x", ("other:cap-c", "other:cap-e"), "cap-b", 2),
        row("x", ("other:cap-d", "other:cap-e"), "cap-f", 2),
    ]
    report = summarize(Inventory(tuple(rows)))
    assert report["verdict"] == "VIABLE_SCRIPTED_ONLY"
    assert report["decision_level"]["non_collapsed"] == 3
    assert report["information_bits"]["receipt_information"] > 0
    assert report["decision_level"]["shares_families_with_train"]


def test_collapsed_receipt_states_stop() -> None:
    rows = [
        row("x", (), "cap-a", 3),
        row("y", (), "cap-b", 3),
        row("x", ("useful:cap-a", "other:cap-c"), "cap-a", 2),
        row("x", ("useful:cap-a", "other:cap-d"), "cap-a", 2),
        row("y", ("useful:cap-b", "other:cap-c"), "cap-b", 2),
        row("y", ("other:cap-c", "other:cap-d"), "cap-b", 1),
    ]
    report = summarize(Inventory(tuple(rows)))
    assert report["verdict"] == "STOP_RECEIPT_STATES_COLLAPSE"
    assert report["failures"] == [
        "collapse_to_training_core",
        "productive_capabilities",
        "no_receipt_information",
    ]
    assert report["decision_level"]["held_out_signatures"] == 3
    assert report["decision_level"]["receipt_names_label"] == 3
    assert report["decision_level"]["depth_2_single_choice_signatures"] == 1
    assert report["proposed_split"] is None


def test_single_choice_states_never_count_as_held_out() -> None:
    rows = [row("x", (), "a", 3)] + [
        row("x", ("other:b", f"other:{name}"), "a", 1) for name in "cde"
    ]
    report = summarize(Inventory(tuple(rows)))
    assert report["decision_level"]["held_out_signatures"] == 0
    assert "held_out_signatures" in report["failures"]


def test_conflicting_visible_twins_refused() -> None:
    with pytest.raises(Refused, match="conflicting"):
        labels_by_signature([row("x", (), "a", 3), row("x", (), "b", 3)])


def test_phase2_preregistration_is_required(tmp_path: Path) -> None:
    check_phase2_preregistration()
    with pytest.raises(Refused, match="phase 2"):
        check_phase2_preregistration(tmp_path / "absent.md")


def test_cli_stops_before_inventory_without_preregistration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse() -> None:
        raise Refused("missing preregistration")

    def forbidden(workers: int) -> Inventory:
        raise AssertionError("inventory must not open")

    monkeypatch.setattr(receipt_states, "check_phase2_preregistration", refuse)
    monkeypatch.setattr(receipt_states, "collect", forbidden)
    assert receipt_states.main([]) == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "STOP_INVALID_INVENTORY"


@pytest.fixture(scope="module")
def real() -> Inventory:
    inventories = [inspect_scenario(scenario) for scenario in (PENDING, DRIFT)]
    return Inventory(
        tuple(state for item in inventories for state in item.states),
        prior_not_executed=sum(item.prior_not_executed for item in inventories),
    )


def test_depth_zero_and_one_match_phase_one(real: Inventory) -> None:
    for scenario in (PENDING, DRIFT):
        phase_one = viability.inspect_scenario(scenario).decisions
        assert {(row.signature, row.label) for row in phase_one} == {
            (state.signature, state.label)
            for state in real.states
            if state.family == scenario.family and state.depth <= 1
        }


def test_real_receipt_states_collapse(real: Inventory) -> None:
    report = summarize(real)
    assert report["verdict"] == "STOP_RECEIPT_STATES_COLLAPSE"
    assert report["information_bits"]["receipt_information"] == 0.0
    assert report["information_bits"]["label_given_core"] == 0.0
    assert report["excluded"]["prior_not_executed"] == 4
    level = report["decision_level"]
    assert level["train_held_out_intersection"] == 0
    assert level["depth_2_single_choice_signatures"] == 2
    assert level["non_collapsed"] == 0
    assert level["receipt_names_label"] == level["held_out_signatures"]
    assert not report["family_atomic"]["all_depths"]["allocation_exists"]
    assert not report["training_executed"]
    assert not report["test_experiment_opened"]
