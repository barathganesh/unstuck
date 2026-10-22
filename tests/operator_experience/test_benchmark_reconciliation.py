"""Benchmark completeness is reconciled, never declared. Offline.

Repair wave 3, finding 2: the reader enforced the four arm names and the basic
call totals, then trusted everything else. It never compared the expected and
observed identity hashes, never compared the expected, observed and unique
identity counts, never reconciled the identity pair count with the declared
complete pairs, accepted a rate measured over one episode of forty as measured,
and accepted a coverage label beside counts that contradicted it. An export
could therefore be marked complete, retention-enabled and quotable while
describing a corpus it had not recorded.

Each test below mutates exactly one field of an otherwise complete export and
leaves the discrepancy lists empty, which is the shape the old reader could not
see through. A declaration that could describe no set of records at all is
refused outright; a declaration that is coherent but does not reconcile blocks
completeness, every figure and the retention panel.
"""

from typing import Any

import pytest

from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.benchmark import (
    ARMS,
    DISPLAYED_AGGREGATES,
    IdentityReconciliation,
    benchmark_view,
    read_export,
)

from .test_benchmark import EPISODES, aggregate, export_body

PAIRS = 20 * len(ARMS)
EXPECTED = len(ARMS) * EPISODES


def identities(body: dict[str, Any]) -> dict[str, Any]:
    return body["manifest"]["denominators"]["identities"]


def denominators(body: dict[str, Any]) -> dict[str, Any]:
    return body["manifest"]["denominators"]


def refused(body: dict[str, Any]) -> OperatorFault:
    with pytest.raises(OperatorFault) as raised:
        read_export(body)
    assert raised.value.reason is OperatorReason.EVALUATION_EXPORT_INVALID
    return raised.value


def incomplete(body: dict[str, Any]) -> Any:
    """Read the export and prove no figure or decision survives its gaps."""
    view = benchmark_view(read_export(body))
    assert view.corpus_status != "complete"
    assert view.quotable is False
    assert view.experiment_complete is False
    assert view.retention_enabled is False
    assert view.retention_decision == "pending"
    assert view.gaps
    with pytest.raises(OperatorFault) as raised:
        view.require_retention()
    assert raised.value.reason is OperatorReason.RETENTION_UNDECIDABLE
    return view


def test_the_unmutated_export_is_the_complete_control_offline() -> None:
    """Every test below mutates this one export, so it must start complete."""
    view = benchmark_view(read_export(export_body()))
    assert view.corpus_status == "complete"
    assert view.quotable is True
    assert view.retention_enabled is True
    assert view.gaps == ()


def test_disagreeing_identity_hashes_are_never_reconciled_offline() -> None:
    body = export_body()
    identities(body)["observed_hash"] = "c" * 64
    # Equal discrepancy lists beside unequal hashes describe no record set.
    assert refused(body)
    identities(body)["missing"] = ["decider|case-1|1"]
    identities(body)["observed"] = EXPECTED - 1
    identities(body)["unique_observed"] = EXPECTED - 1
    identities(body)["ordered_pairs"] = PAIRS - 1
    view = incomplete(body)
    assert any("identity hashes disagree" in gap for gap in view.gaps)


def test_an_identity_count_short_of_the_expectation_blocks_completeness_offline() -> (
    None
):
    body = export_body()
    identities(body)["expected"] = EXPECTED + 40
    denominators(body)["episodes_expected"] = EXPECTED + 40
    # Forty more expected identities with nothing named missing is a set
    # relation no records could satisfy, so the declaration is refused.
    assert "do not reconcile with the sets" in str(refused(body).detail)

    # Named as missing, it is coherent and simply does not reconcile.
    identities(body)["missing"] = [f"decider|case-{index}|1" for index in range(40)]
    identities(body)["observed_hash"] = "c" * 64
    identities(body)["ordered_pairs"] = PAIRS - 20
    view = incomplete(body)
    assert any("unique identities against" in gap for gap in view.gaps)
    assert "episode identities are not reconciled" in view.gaps


def test_duplicate_identity_records_block_completeness_offline() -> None:
    body = export_body()
    identities(body)["observed"] = EXPECTED + 2
    # Records beyond the unique identities require the duplicates to be named.
    assert refused(body)
    identities(body)["duplicates"] = ["decider|case-1|1", "decider|case-2|1"]
    view = incomplete(body)
    assert any("duplicate records" in gap for gap in view.gaps)


def test_a_duplicated_discrepancy_list_is_refused_offline() -> None:
    body = export_body()
    identities(body)["duplicates"] = ["decider|case-1|1", "decider|case-1|1"]
    assert "same identity twice" in str(refused(body).detail)


def test_a_pair_total_that_does_not_account_for_the_episodes_offline() -> None:
    body = export_body()
    denominators(body)["complete_pairs"] = PAIRS + 5
    view = incomplete(body)
    assert any("do not account for" in gap for gap in view.gaps)
    assert any("first calls against" in gap for gap in view.gaps)

    # Fewer pairs than the recorded episodes could have come from at all is a
    # refusal rather than a gap.
    denominators(body)["complete_pairs"] = PAIRS - 5
    identities(body)["ordered_pairs"] = PAIRS - 5
    assert "the pairs could produce" in str(refused(body).detail)


def test_ordered_pairs_disagreeing_with_complete_pairs_offline() -> None:
    body = export_body()
    identities(body)["ordered_pairs"] = PAIRS - 1
    view = incomplete(body)
    assert any("ordered pairs against" in gap for gap in view.gaps)


def test_one_arm_recording_a_pair_count_of_its_own_offline() -> None:
    """Equal call counts are not the corpus pair total for the arm."""
    body = export_body()
    body["arms"][0]["call_1_episodes"] = 10
    body["arms"][0]["call_2_episodes"] = 10
    body["arms"][0]["episodes"] = 20
    for name in DISPLAYED_AGGREGATES:
        body["arms"][0][name] = aggregate(1.5, measured=20, total=20)
    body["arms"][0]["verified_completion"] = {"count": 10, "denominator": 20}
    body["arms"][0]["harmed"] = {"count": 0, "denominator": 20}
    view = incomplete(body)
    assert any("did not record both ordered calls" in gap for gap in view.gaps)


def test_a_rate_measured_over_one_episode_of_many_is_not_measured_offline() -> None:
    """The review's own counterexample: success over one episode of forty."""
    body = export_body()
    for arm in body["arms"]:
        arm["verified_completion"] = {"count": 1, "denominator": 1}
    view = incomplete(body)
    assert any("verified completion over 1 of" in gap for gap in view.gaps)
    for row in view.arms:
        assert row.quotable is False


def test_a_harm_rate_with_a_short_denominator_is_not_measured_offline() -> None:
    body = export_body()
    for arm in body["arms"]:
        arm["harmed"] = {"count": 0, "denominator": 2}
    view = incomplete(body)
    assert any("harm over 2 of" in gap for gap in view.gaps)


def test_coverage_counted_over_fewer_episodes_than_the_arm_ran_offline() -> None:
    body = export_body()
    for arm in body["arms"]:
        arm["selector_cost"] = aggregate(0.25, measured=1, total=1)
    assert "coverage is counted over 1 of" in str(refused(body).detail)


def test_a_coverage_label_that_contradicts_its_counts_is_refused_offline() -> None:
    body = export_body()
    for arm in body["arms"]:
        figure = aggregate(0.25, measured=10, total=EPISODES)
        figure["coverage"]["completeness"] = "complete"
        figure["coverage"]["quotable"] = True
        arm["selector_cost"] = figure
    assert "disagrees with the counts" in str(refused(body).detail)


def test_a_quotable_flag_over_partial_counts_never_shows_a_figure_offline() -> None:
    """Coverage that reports one of forty measured is not a quotable figure."""
    body = export_body()
    for arm in body["arms"]:
        arm["reads_per_success"] = aggregate(1.5, measured=1, total=EPISODES)
    view = incomplete(body)
    assert any("partial measurement coverage" in gap for gap in view.gaps)
    for row in view.arms:
        assert row.diagnostic_reads.availability == "not_measured"
        assert row.diagnostic_reads.value is None


def test_coverage_counts_that_do_not_add_up_are_refused_offline() -> None:
    body = export_body()
    for arm in body["arms"]:
        figure = aggregate(1.5, measured=EPISODES, total=EPISODES)
        figure["coverage"]["unavailable"] = 5
        arm["selector_latency_p50"] = figure
    assert "reconcile with its own total" in str(refused(body).detail)


def test_the_arms_must_carry_the_recorded_episodes_offline() -> None:
    body = export_body()
    denominators(body)["episodes_recorded"] = EXPECTED - 40
    view = incomplete(body)
    assert any("against 120 recorded" in gap for gap in view.gaps)


def test_an_export_declaring_a_calls_per_case_this_view_cannot_reconcile_offline() -> (
    None
):
    body = export_body()
    denominators(body)["calls_per_case"] = 3
    assert "ordered calls per case" in str(refused(body).detail)


@pytest.mark.parametrize(
    "field",
    [
        "expected_hash",
        "observed_hash",
        "expected",
        "observed",
        "unique_observed",
        "ordered_pairs",
        "missing",
        "unexpected",
        "duplicates",
        "unordered_pairs",
    ],
)
def test_a_missing_identity_field_is_refused_offline(field: str) -> None:
    body = export_body()
    identities(body).pop(field)
    assert field in str(refused(body).detail)


@pytest.mark.parametrize(
    "field", ["identities", "calls_per_case", "complete_pairs", "incomplete_pairs"]
)
def test_a_missing_denominator_is_refused_offline(field: str) -> None:
    body = export_body()
    denominators(body).pop(field)
    assert field in str(refused(body).detail)


def test_a_missing_coverage_total_is_refused_offline() -> None:
    body = export_body()
    body["arms"][0]["selector_cost"]["coverage"].pop("total")
    assert "total" in str(refused(body).detail)


@pytest.mark.parametrize(
    "changes",
    [
        {"unique_observed": 5},
        {"observed": 1},
        {"missing": 1},
        {"duplicates": 1},
        {"ordered_pairs": 100},
    ],
)
def test_an_impossible_identity_combination_is_refused_offline(
    changes: dict[str, int],
) -> None:
    base = {
        "expected_hash": "a" * 64,
        "observed_hash": "a" * 64,
        "expected": 8,
        "observed": 8,
        "unique_observed": 8,
        "ordered_pairs": 4,
    }
    with pytest.raises(ValueError):
        IdentityReconciliation(**(base | changes))  # type: ignore[arg-type]


def test_a_reconciled_identity_set_reports_no_gap_offline() -> None:
    reconciliation = IdentityReconciliation(
        expected_hash="a" * 64,
        observed_hash="a" * 64,
        expected=8,
        observed=8,
        unique_observed=8,
        ordered_pairs=4,
    )
    assert reconciliation.reconciled is True
    assert reconciliation.gaps == ()
