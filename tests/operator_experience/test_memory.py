"""Cross-call Decision Receipt memory, through the real controller. Offline.

Finding 9: the surface ran the one-pass offline controller and exposed no
receipt history, so no call could ever see what an earlier call recorded. These
tests drive two ordered calls on one scenario through the API and assert the
product's own behaviour: call one writes a stalled receipt and re-decides once
in the same call; call two retrieves that receipt, decides fresh, refuses the
identical repetition before anything runs, and resolves through one second
decision at a cost of one read instead of two.

Namespaces are isolated per scenario, duplicate invocations resume rather than
decide again, and every panel resolves the producer's own event.
"""

from typing import Any

import pytest

from unstuck.clock import FakeClock
from unstuck.operator.app import OfflineRunService, create_app
from unstuck.operator.scenarios import LOCAL_ARM, resolve_scenario
from unstuck.storage import LocalRunRepository

from .conftest import JSON_HEADERS, READ_HEADERS
from .test_endpoints import LocalClient

MEMORY = "memory-repeat"


@pytest.fixture
def client(repository: LocalRunRepository, clock: FakeClock) -> LocalClient:
    return LocalClient(create_app(repository=repository, clock=clock))


def start(client: LocalClient, scenario: str, key: str) -> dict[str, Any]:
    response = client.post(
        "/api/runs",
        json={"scenario": scenario, "idempotency_key": key},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def events(client: LocalClient, run_id: str) -> list[str]:
    body = client.post(
        "/api/exports",
        json={"run_id": run_id, "format": "json"},
        headers=JSON_HEADERS,
    )
    assert body.status_code == 200, body.text
    return [event["type"] for event in body.json()["events"]]


def test_the_first_call_records_a_receipt_and_re_decides_once_offline(
    client: LocalClient,
) -> None:
    view = start(client, MEMORY, "ui-memory-01")
    assert view["baseline_id"] == LOCAL_ARM
    assert view["history_status"] == "empty", "a cold namespace has no history"
    assert view["receipt_references"] == []
    assert view["selector_calls"] == 2, "one same-call second decision"
    assert view["diagnostic_reads"] == 2
    assert view["path"] == "re_decided"
    assert view["first_choice"] != view["final_choice"]

    names = events(client, view["run_id"])
    assert names.count("decision_receipt_written") == 2
    assert names.count("decider_first_returned") == 1
    assert names.count("decider_final_returned") == 1
    assert "repeat_blocked" not in names, "nothing was prohibited on a cold call"


def test_the_second_call_precludes_the_repetition_before_any_selector_call_offline(
    client: LocalClient,
) -> None:
    first = start(client, MEMORY, "ui-memory-01")
    second = start(client, MEMORY, "ui-memory-02")
    assert second["run_id"] != first["run_id"]

    assert second["history_status"] == "matched"
    assert second["receipt_references"], "call two read call one's receipt"
    # The reason carried forward is the verifier's own reason from the matching
    # receipt, not a label this surface invented.
    assert second["blocked_reason_code"] == "insufficient_evidence"
    # The dead end was never offered. D1 chose among the rest; its read moved
    # no gate, so the one same-call D2 chose from what remained after both.
    assert second["first_choice"] != first["first_choice"]
    assert second["final_choice"] not in {first["first_choice"], second["first_choice"]}
    assert second["selector_calls"] == 2
    assert second["diagnostic_reads"] == 2

    names = events(client, second["run_id"])
    assert names.count("repeat_blocked") == 1
    assert names.index("repeat_blocked") < names.index("decider_first_requested")
    assert names.count("decision_receipt_written") == 2

    panel = client.get(
        f"/api/runs/{second['run_id']}/evidence/blocked-repeat", headers=READ_HEADERS
    )
    assert panel.status_code == 200
    raw = panel.json()["raw_event"]
    assert raw["type"] == "repeat_blocked"
    assert raw["payload"]["blocked_class"] == "exact_repeat"
    assert raw["payload"]["outcome_class"] == "no_progress"
    assert raw["payload"]["stage"] == "pre_selection"
    assert raw["payload"]["public_tool_id"] == first["first_choice"]


def test_a_historical_receipt_is_rendered_as_a_reference_offline(
    client: LocalClient,
) -> None:
    start(client, MEMORY, "ui-memory-01")
    second = start(client, MEMORY, "ui-memory-02")
    spine = {node["kind"]: node for node in second["spine"]}
    history = spine["history_lookup"]
    assert history["present"] is True
    assert "reference" in history["detail"]
    assert "never a reply" in history["detail"]
    blocked = spine["blocked_repeat"]
    assert blocked["present"] is True
    assert blocked["state"] == "blocked"
    assert "no admissible evidence" in blocked["detail"]


def test_the_receipt_node_reports_the_verifiers_own_outcome_offline(
    client: LocalClient,
) -> None:
    view = start(client, MEMORY, "ui-memory-01")
    spine = {node["kind"]: node for node in view["spine"]}
    receipt = spine["receipt"]
    assert receipt["present"] is True
    assert "2 receipt(s)" in receipt["detail"]

    panel = client.get(
        f"/api/runs/{view['run_id']}/evidence/decision-receipt", headers=READ_HEADERS
    )
    assert panel.status_code == 200
    body = panel.json()
    receipts = body["evidence"]["receipts"]
    assert len(receipts) == 2
    assert [item["outcome_class"] for item in receipts] == [
        "no_progress",
        "disposition_unchanged",
    ]
    # Two different proofs, both disposition-negative, both eligible. The
    # panel reports the verifier's own classification and computes neither.
    assert [item["blocking"] for item in receipts] == [True, True]
    assert receipts[0]["admitted_evidence"] is False
    assert receipts[1]["admitted_evidence"] is True
    # The evidence delta is shown, not inferred.
    assert receipts[0]["evidence_before_hash"] == receipts[0]["evidence_after_hash"], (
        "a stalled read admitted nothing"
    )
    assert receipts[1]["evidence_before_hash"] != receipts[1]["evidence_after_hash"]
    assert body["validation"]["outcome_class"] == "disposition_unchanged"
    assert body["raw_event"]["type"] == "decision_receipt_written"


def test_one_scenarios_history_never_reaches_another_offline(
    client: LocalClient,
) -> None:
    start(client, MEMORY, "ui-memory-01")
    other = start(client, "repairable-join", "ui-other-01")
    assert other["history_status"] == "empty", "namespaces are isolated per scenario"
    assert other["receipt_references"] == []
    assert other["blocked_tool_id"] is None


def test_a_duplicate_invocation_resumes_rather_than_deciding_again_offline(
    client: LocalClient,
) -> None:
    first = start(client, MEMORY, "ui-memory-01")
    repeated = start(client, MEMORY, "ui-memory-01")
    assert repeated["run_id"] == first["run_id"]
    assert repeated["selector_calls"] == first["selector_calls"]
    assert repeated["diagnostic_reads"] == first["diagnostic_reads"]
    assert events(client, repeated["run_id"]).count("decider_first_requested") == 1


def test_the_service_keeps_one_receipt_store_per_scenario_offline(
    repository: LocalRunRepository, clock: FakeClock
) -> None:
    service = OfflineRunService(repository, clock)
    memory = resolve_scenario(MEMORY)
    other = resolve_scenario("repairable-join")
    assert service.receipts(memory) is service.receipts(memory)
    assert service.receipts(memory) is not service.receipts(other)
    assert service.namespace(memory).arm == LOCAL_ARM
    assert service.namespace(memory).case_id != service.namespace(other).case_id
    assert service.settings.baseline_id == LOCAL_ARM
