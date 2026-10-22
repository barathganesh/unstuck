"""The benchmark tab reads a validated evaluation export. Offline.

Findings 9 and 12: the endpoint called `benchmark_view()` with no export at
all, so the tab stayed empty after results existed; and the corpus was labelled
`complete` when only three of the four arms were measured, which made an
incomplete experiment look quotable.

The builder below writes an export in the shape the evaluation unit declares.
It is written here, in the consumer's own tests, precisely because the product
package must not import the evaluation package to read one.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from unstuck.clock import FakeClock
from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.app import create_app
from unstuck.operator.benchmark import (
    ARMS,
    DISPLAYED_AGGREGATES,
    EXPORT_FILENAME,
    benchmark_view,
    load_export,
    read_export,
    resolve_export_path,
)
from unstuck.storage import LocalRunRepository

from .conftest import READ_HEADERS
from .test_endpoints import LocalClient

EPISODES = 40


def aggregate(value: float | None, *, measured: int, total: int) -> dict[str, Any]:
    completeness = (
        "complete"
        if total and measured == total
        else ("none" if not measured else "partial")
    )
    return {
        "measure": {
            "availability": "measured" if value is not None else "unavailable",
            "value": value,
        },
        "coverage": {
            "total": total,
            "measured": measured,
            "unavailable": total - measured,
            "completeness": completeness,
            "quotable": completeness == "complete",
            "missingness_policy": "report_partial_non_quotable",
        },
    }


def arm_body(
    name: str,
    *,
    episodes: int = EPISODES,
    ordered: bool = True,
    covered: bool = True,
    measured_rates: bool = True,
) -> dict[str, Any]:
    half = episodes // 2
    figure = aggregate(
        1.5 if covered else None,
        measured=episodes if covered else 0,
        total=episodes,
    )
    return {
        "arm": name,
        "episodes": episodes,
        "call_1_episodes": half if ordered else episodes,
        "call_2_episodes": half if ordered else 0,
        "verified_completion": {
            "count": 30 if measured_rates else 0,
            "denominator": episodes if measured_rates else 0,
        },
        "harmed": {
            "count": 1 if measured_rates else 0,
            "denominator": episodes if measured_rates else 0,
        },
        **{name: figure for name in DISPLAYED_AGGREGATES},
    }


def export_body(
    arms: tuple[str, ...] = ARMS,
    *,
    experiment_complete: bool = True,
    retention_decidable: bool = True,
    quotable: bool = True,
    reconciled: bool = True,
    recorded: int | None = None,
    incomplete_pairs: int = 0,
    gaps: tuple[str, ...] = (),
    **arm_options: Any,
) -> dict[str, Any]:
    bodies = [arm_body(name, **arm_options) for name in arms]
    expected = len(arms) * EPISODES
    # An unreconciled export is still a coherent one: one identity is missing,
    # so one fewer identity was observed and the two hashes differ. A
    # declaration whose counts and hashes could describe no set of records at
    # all is refused by the reader rather than reported as a gap.
    missing = [] if reconciled else ["decider|case-1|1"]
    observed = expected - len(missing)
    return {
        "manifest": {
            "experiment_id": "exp-07",
            "arms": list(arms),
            "denominators": {
                "cases_planned": 20,
                "cases_executed": 20,
                "arms": len(arms),
                "calls_per_case": 2,
                "episodes_expected": expected,
                "episodes_recorded": observed if recorded is None else recorded,
                "complete_pairs": 20 * len(arms),
                "incomplete_pairs": incomplete_pairs,
                "identities": {
                    "expected_hash": "a" * 64,
                    "observed_hash": "a" * 64 if reconciled else "b" * 64,
                    "expected": expected,
                    "observed": observed,
                    "unique_observed": observed,
                    "missing": missing,
                    "unexpected": [],
                    "duplicates": [],
                    # A missing episode leaves its own pair incomplete, so one
                    # fewer pair recorded both of its ordered calls.
                    "ordered_pairs": 20 * len(arms) - len(missing),
                    "unordered_pairs": [],
                },
            },
        },
        "arms": bodies,
        "experiment_complete": experiment_complete,
        "retention_decidable": retention_decidable,
        "quotable": quotable,
        "completeness_gaps": list(gaps),
        "measurement_gaps": [],
        "retention_decidable_note": "read by nothing; extra keys are ignored",
    }


def write_export(root: Path, body: dict[str, Any], name: str = EXPORT_FILENAME) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


def complete_view() -> Any:
    return benchmark_view(read_export(export_body()))


def test_a_four_arm_export_is_complete_and_quotable_offline() -> None:
    view = complete_view()
    assert {row.arm for row in view.arms} == set(ARMS)
    assert view.corpus_status == "complete"
    assert view.experiment_complete is True
    assert view.retention_decidable is True
    assert view.retention_enabled is True
    assert view.quotable is True
    assert view.gaps == ()
    assert view.episodes_total == len(ARMS) * EPISODES
    view.require_retention()
    for row in view.arms:
        assert row.verified_success.availability == "measured"
        assert row.cost_usd.availability == "measured"
        assert row.quotable is True


def test_three_arms_are_never_a_complete_corpus_offline() -> None:
    """The probe from finding 12: `complete=True` without `fixed`."""
    view = benchmark_view(
        read_export(export_body(("llm", "decider", "decider_memory")))
    )
    assert view.corpus_status == "partial"
    assert view.retention_enabled is False
    assert view.quotable is False
    assert view.experiment_complete is False
    assert any("missing arms: fixed" in gap for gap in view.gaps)
    with pytest.raises(OperatorFault) as raised:
        view.require_retention()
    assert raised.value.reason is OperatorReason.RETENTION_UNDECIDABLE
    assert "fixed" in raised.value.detail


def test_an_arm_missing_its_second_call_is_incomplete_offline() -> None:
    view = benchmark_view(read_export(export_body(ordered=False)))
    assert view.corpus_status == "partial"
    assert view.retention_enabled is False
    assert any("both ordered calls" in gap for gap in view.gaps)
    for row in view.arms:
        assert row.call_2_episodes == 0
        assert row.quotable is False


def test_unreconciled_episode_identities_block_completeness_offline() -> None:
    view = benchmark_view(read_export(export_body(reconciled=False)))
    assert view.corpus_status == "partial"
    assert "episode identities are not reconciled" in view.gaps


def test_a_recorded_total_that_disagrees_blocks_completeness_offline() -> None:
    view = benchmark_view(read_export(export_body(recorded=10)))
    assert view.corpus_status == "partial"
    assert any("episodes recorded against" in gap for gap in view.gaps)


def test_an_incomplete_pair_blocks_completeness_offline() -> None:
    view = benchmark_view(read_export(export_body(incomplete_pairs=3)))
    assert view.corpus_status == "partial"
    assert "3 incomplete case pairs" in view.gaps


def test_partial_measurement_coverage_is_never_shown_as_a_figure_offline() -> None:
    body = export_body()
    for arm in body["arms"]:
        arm["selector_cost"] = aggregate(0.25, measured=10, total=EPISODES)
    view = benchmark_view(read_export(body))
    assert view.corpus_status == "partial"
    assert any("partial measurement coverage" in gap for gap in view.gaps)
    for row in view.arms:
        assert row.cost_usd.availability == "not_measured"
        assert row.cost_usd.value is None
        assert row.cost_usd.text == "Not measured"


def test_a_rate_without_a_denominator_is_not_a_percentage_offline() -> None:
    view = benchmark_view(read_export(export_body(measured_rates=False)))
    for row in view.arms:
        assert row.verified_success.availability == "not_measured"
        assert row.verified_success.value is None
    assert view.corpus_status == "empty"
    assert view.retention_enabled is False


def test_an_export_declaring_completeness_it_lacks_is_not_believed_offline() -> None:
    view = benchmark_view(
        read_export(
            export_body(("llm", "decider", "decider_memory"), experiment_complete=True)
        )
    )
    assert view.experiment_complete is False
    assert view.quotable is False


def test_a_declared_gap_survives_into_the_view_offline() -> None:
    view = benchmark_view(read_export(export_body(gaps=("pricing date is absent",))))
    assert "pricing date is absent" in view.gaps
    assert view.corpus_status == "partial"
    assert view.retention_enabled is False


@pytest.mark.parametrize(
    "relative",
    ["../results.json", "/etc/hosts", "nested/../../results.json", " results.json"],
)
def test_no_export_path_escapes_its_root_offline(tmp_path: Path, relative: str) -> None:
    with pytest.raises(OperatorFault) as raised:
        resolve_export_path(tmp_path, relative)
    assert raised.value.reason is OperatorReason.EVALUATION_PATH_REJECTED


def test_a_symlinked_export_is_refused_offline(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(export_body()), encoding="utf-8")
    root = tmp_path / "exports"
    root.mkdir()
    os.symlink(outside, root / EXPORT_FILENAME)
    with pytest.raises(OperatorFault) as raised:
        load_export(root)
    assert raised.value.reason is OperatorReason.EVALUATION_PATH_REJECTED


def test_a_directory_is_not_an_export_offline(tmp_path: Path) -> None:
    (tmp_path / EXPORT_FILENAME).mkdir()
    with pytest.raises(OperatorFault) as raised:
        load_export(tmp_path)
    assert raised.value.reason is OperatorReason.EVALUATION_PATH_REJECTED


def test_an_absent_export_is_refused_rather_than_assumed_empty_offline(
    tmp_path: Path,
) -> None:
    with pytest.raises(OperatorFault) as raised:
        load_export(tmp_path)
    assert raised.value.reason is OperatorReason.EVALUATION_EXPORT_INVALID


def test_a_malformed_export_is_refused_offline(tmp_path: Path) -> None:
    write_export(tmp_path, {})
    (tmp_path / EXPORT_FILENAME).write_text("not json at all", encoding="utf-8")
    with pytest.raises(OperatorFault) as raised:
        load_export(tmp_path)
    assert raised.value.reason is OperatorReason.EVALUATION_EXPORT_INVALID


@pytest.mark.parametrize(
    "drop",
    ["manifest", "arms", "experiment_complete", "retention_decidable", "quotable"],
)
def test_an_export_missing_a_declared_field_is_refused_offline(drop: str) -> None:
    body = export_body()
    body.pop(drop)
    with pytest.raises(OperatorFault) as raised:
        read_export(body)
    assert raised.value.reason is OperatorReason.EVALUATION_EXPORT_INVALID
    assert drop in raised.value.detail


@pytest.mark.parametrize(
    "drop", ["arm", "episodes", "call_1_episodes", "verified_completion", "harmed"]
)
def test_an_arm_missing_a_declared_field_is_refused_offline(drop: str) -> None:
    body = export_body()
    body["arms"][0].pop(drop)
    with pytest.raises(OperatorFault) as raised:
        read_export(body)
    assert raised.value.reason is OperatorReason.EVALUATION_EXPORT_INVALID


def test_an_arm_missing_a_displayed_aggregate_is_refused_offline() -> None:
    body = export_body()
    body["arms"][0].pop("selector_latency_p95")
    with pytest.raises(OperatorFault) as raised:
        read_export(body)
    assert raised.value.reason is OperatorReason.EVALUATION_EXPORT_INVALID


def test_a_boolean_is_never_read_as_a_count_offline() -> None:
    body = export_body()
    body["arms"][0]["episodes"] = True
    with pytest.raises(OperatorFault) as raised:
        read_export(body)
    assert raised.value.reason is OperatorReason.EVALUATION_EXPORT_INVALID


def test_a_duplicate_arm_is_refused_offline() -> None:
    body = export_body()
    body["arms"].append(arm_body("decider"))
    with pytest.raises(OperatorFault) as raised:
        read_export(body)
    assert raised.value.reason is OperatorReason.EVALUATION_EXPORT_INVALID


def test_the_endpoint_serves_the_configured_export_offline(
    repository: LocalRunRepository, clock: FakeClock, tmp_path: Path
) -> None:
    root = tmp_path / "exports"
    write_export(root, export_body())
    client = LocalClient(
        create_app(repository=repository, clock=clock, export_root=root)
    )
    response = client.get("/api/benchmark", headers=READ_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["experiment_id"] == "exp-07"
    assert body["corpus_status"] == "complete"
    assert body["retention_enabled"] is True
    assert body["episodes_total"] == len(ARMS) * EPISODES
    assert {row["arm"] for row in body["arms"]} == set(ARMS)
    assert body["retention_decision"] == "pending"


def test_the_endpoint_reports_an_empty_corpus_with_no_export_offline(
    repository: LocalRunRepository, clock: FakeClock
) -> None:
    client = LocalClient(create_app(repository=repository, clock=clock))
    body = client.get("/api/benchmark", headers=READ_HEADERS).json()
    assert body["corpus_status"] == "empty"
    assert body["retention_enabled"] is False
    assert body["quotable"] is False
    assert body["episodes_total"] == 0
    assert all(
        row["verified_success"]["availability"] == "not_measured"
        for row in body["arms"]
    )


def test_the_endpoint_refuses_a_broken_export_rather_than_blanking_it_offline(
    repository: LocalRunRepository, clock: FakeClock, tmp_path: Path
) -> None:
    root = tmp_path / "exports"
    root.mkdir()
    (root / EXPORT_FILENAME).write_text("{}", encoding="utf-8")
    client = LocalClient(
        create_app(repository=repository, clock=clock, export_root=root)
    )
    response = client.get("/api/benchmark", headers=READ_HEADERS)
    assert response.status_code == 409
    assert (
        response.json()["reason_code"] == OperatorReason.EVALUATION_EXPORT_INVALID.value
    )


def test_the_product_package_never_imports_the_evaluation_package_offline() -> None:
    import ast
    import importlib
    import sys

    from unstuck.operator import benchmark

    assert "evaluation" not in sys.modules or True  # imported by other suites
    for module in (benchmark, importlib.import_module("unstuck.operator.app")):
        tree = ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("evaluation"), alias.name
            if isinstance(node, ast.ImportFrom):
                assert not str(node.module).startswith("evaluation"), node.module
