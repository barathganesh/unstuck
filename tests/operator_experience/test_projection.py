"""Invariant tests for the truthful run projection. Offline.

FR10.3 is the rule under test: never present historical decisions as answers,
model confidence as correctness, no-progress as hidden ground truth, or an
unavailable measurement as zero. The run under test is a real offline run
produced by the real controller.
"""

import json

import pytest

from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.modes import resolve_mode
from unstuck.operator.projection import display, evidence_panel, run_view, spine_for
from unstuck.storage import Snapshot

from .conftest import SENTINEL_MARKERS

STALE_NAMES = ("jev", "decider_jev", "bma")


def test_the_spine_is_an_ordered_list_of_known_nodes_offline(
    completed_run: Snapshot,
) -> None:
    spine = spine_for(completed_run)
    assert spine
    assert [node.order for node in spine] == list(range(1, len(spine) + 1))
    assert len({node.node_id for node in spine}) == len(spine)
    kinds = [node.kind for node in spine]
    assert kinds[0] == "task"
    assert kinds[-1] == "result"
    assert "decision_first" in kinds
    assert "verification" in kinds
    for node in spine:
        assert node.label and node.detail
        assert not any(stale in node.label.lower() for stale in STALE_NAMES)
        assert not any(stale in node.detail.lower() for stale in STALE_NAMES)


def test_the_direct_and_re_decided_paths_are_distinguishable_by_text_offline(
    completed_run: Snapshot,
) -> None:
    view = run_view(completed_run, resolve_mode("offline"))
    assert view.path in {"direct", "re_decided", "no_cascade"}
    blocked = [node for node in view.spine if node.kind == "blocked_repeat"]
    final = [node for node in view.spine if node.kind == "decision_final"]
    if view.path == "direct":
        assert all(not node.present for node in blocked + final)
        assert any("no second" in node.detail.lower() for node in final) or not final
    else:
        assert view.selector_calls >= 1
    # The path is stated in words, never implied by a colour or an icon alone.
    assert view.outcome_text
    assert view.path.replace("_", " ") in json.dumps(view.model_dump(mode="json"))


def test_an_unavailable_measurement_is_never_rendered_as_zero_offline(
    completed_run: Snapshot,
) -> None:
    view = run_view(completed_run, resolve_mode("offline"))
    for value in (view.cost, view.duration, view.confidence):
        # The invariant is the equivalence, in both directions: a number is
        # present exactly when it was measured. A measured zero is a real
        # measurement and stays labelled measured; an absent value never
        # acquires a number at all, so it cannot reach a template as zero.
        assert (value.value is None) == (value.availability != "measured")
        if value.availability != "measured":
            assert value.text in {"Not measured", "Not provided", "Not called"}
            assert value.text != "0"
    assert view.cost.availability == "not_measured"
    assert view.cost.value is None


def test_display_maps_every_availability_without_inventing_a_number_offline() -> None:
    from unstuck.events import Measurement

    measured = display(Measurement(availability="measured", value=12.5))
    assert (measured.value, measured.availability) == (12.5, "measured")
    for availability in ("not_measured", "not_provided", "not_called"):
        absent = display(Measurement(availability=availability))
        assert absent.value is None
        assert absent.text != "0"
    assert display(None).availability == "not_provided"
    assert display(None).value is None


def test_history_is_presented_as_a_reference_never_as_an_answer_offline(
    completed_run: Snapshot,
) -> None:
    view = run_view(completed_run, resolve_mode("offline"))
    assert view.history_status in {"not_requested", "unavailable", "empty", "matched"}
    assert isinstance(view.receipt_references, tuple)
    lookup = [node for node in view.spine if node.kind == "history_lookup"]
    for node in lookup:
        detail = node.detail.lower()
        assert (
            "reference" in detail or "no history" in detail or "not requested" in detail
        )
        assert "answer" not in detail
        assert "because" not in detail
    assert len(view.receipt_references) <= 4


def test_confidence_is_never_presented_as_correctness_offline(
    completed_run: Snapshot,
) -> None:
    view = run_view(completed_run, resolve_mode("offline"))
    rendered = json.dumps(view.model_dump(mode="json")).lower()
    for claim in ("correct", "accuracy", "proven by confidence", "certain"):
        assert claim not in rendered
    if view.confidence.availability == "measured":
        decision = [node for node in view.spine if node.kind == "decision_first"]
        assert decision
        assert "confidence" in decision[0].detail.lower()
        assert "correct" not in decision[0].detail.lower()


def test_no_progress_is_never_presented_as_ground_truth_offline(
    completed_run: Snapshot,
) -> None:
    view = run_view(completed_run, resolve_mode("offline"))
    for node in view.spine:
        detail = node.detail.lower()
        assert "root cause" not in detail
        assert "the cause is" not in detail
        assert "hidden" not in detail
    if view.blocked_reason_code is not None:
        blocked = [node for node in view.spine if node.kind == "blocked_repeat"]
        assert blocked and blocked[0].present
        assert "no admissible evidence" in blocked[0].detail.lower()


def test_every_outcome_state_has_an_explicit_text_label_offline(
    completed_run: Snapshot,
) -> None:
    view = run_view(completed_run, resolve_mode("offline"))
    assert view.classification == "synthetic"
    assert view.mode.text
    assert view.outcome_text and view.reason_code
    states = {node.state for node in view.spine}
    assert states <= {
        "verified",
        "pending",
        "blocked",
        "unresolved",
        "error",
        "skipped",
        "informational",
    }
    assert "Synthetic" in json.dumps(view.model_dump(mode="json"))


def test_the_evidence_panel_scopes_to_one_node_and_redacts_offline(
    completed_run: Snapshot,
) -> None:
    spine = spine_for(completed_run)
    node = next(node for node in spine if node.kind == "verification")
    panel = evidence_panel(completed_run, node.node_id)
    assert panel.node_id == node.node_id
    assert panel.validation
    rendered = json.dumps(panel.model_dump(mode="json")).lower()
    for marker in SENTINEL_MARKERS:
        assert marker not in rendered, marker
    with pytest.raises(OperatorFault) as raised:
        evidence_panel(completed_run, "not-a-node")
    assert raised.value.reason is OperatorReason.NODE_NOT_FOUND


def test_a_blocked_mode_run_view_states_the_gate_offline(
    completed_run: Snapshot,
) -> None:
    view = run_view(completed_run, resolve_mode("live"))
    assert view.mode.blocked is True
    assert view.mode.gate
    assert "blocked" in view.mode.text.lower()
