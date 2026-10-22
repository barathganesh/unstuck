"""Scenario selection reaches the real run. Offline, in process, no socket.

Finding 9: `RunRequest.scenario` was accepted and then ignored, so every run
loaded the same fixture and the selector had no effect on anything. These tests
assert the opposite three ways: only an allowlisted token resolves, an unknown
one fails closed before a run exists, and each token produces a materially
different run through the real controller.
"""

import re
from pathlib import Path
from typing import Any

import pytest

from unstuck.clock import FakeClock
from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.app import STATIC_ROOT, create_app
from unstuck.operator.scenarios import (
    SCENARIO_TOKENS,
    SCENARIOS,
    resolve_scenario,
)
from unstuck.storage import LocalRunRepository

from .conftest import JSON_HEADERS, READ_HEADERS
from .test_endpoints import LocalClient


@pytest.fixture
def client(repository: LocalRunRepository, clock: FakeClock) -> LocalClient:
    return LocalClient(create_app(repository=repository, clock=clock))


def run(client: LocalClient, scenario: str, key: str) -> dict[str, Any]:
    response = client.post(
        "/api/runs",
        json={"scenario": scenario, "idempotency_key": key},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_every_declared_scenario_resolves_to_one_fixture_offline() -> None:
    assert len(SCENARIO_TOKENS) == len(set(SCENARIO_TOKENS))
    for scenario in SCENARIOS:
        fixture = scenario.fixture()
        assert fixture.classification == "synthetic"
        assert scenario.case_id.startswith("operator-")
        assert scenario.detail.strip()


@pytest.mark.parametrize(
    "token", ["unknown-scenario", "repairable_join", " repairable-join", "ds-99"]
)
def test_an_unlisted_scenario_token_fails_closed_offline(token: str) -> None:
    with pytest.raises(OperatorFault) as raised:
        resolve_scenario(token)
    assert raised.value.reason is OperatorReason.SCENARIO_REJECTED
    for declared in SCENARIO_TOKENS:
        assert declared in raised.value.detail


def test_an_unlisted_scenario_creates_no_run_offline(
    client: LocalClient, repository: LocalRunRepository
) -> None:
    response = client.post(
        "/api/runs",
        json={"scenario": "not-a-scenario", "idempotency_key": "ui-reject-01"},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["reason_code"] == OperatorReason.SCENARIO_REJECTED.value
    assert "not-a-scenario" in body["detail"]
    # Nothing was started: the refusal happens before the run is created.
    assert not list(Path(repository.root).glob("*/state.json"))


def test_each_scenario_changes_the_run_that_happens_offline(
    client: LocalClient,
) -> None:
    observed: dict[str, tuple[Any, ...]] = {}
    for index, token in enumerate(SCENARIO_TOKENS):
        view = run(client, token, f"ui-scenario-{index}")
        observed[token] = (
            view["status"],
            view["reason_code"],
            view["path"],
            view["selector_calls"],
            view["diagnostic_reads"],
            view["wait_checks"],
            view["repair_attempts"],
            len(view["rows"]),
        )
    assert len(set(observed.values())) == len(SCENARIO_TOKENS), observed

    assert observed["repairable-join"][0] == "completed"
    assert observed["repairable-join"][6] == 1, "the join key was normalized"
    assert observed["verified-empty"][0] == "empty_verified"
    assert observed["verified-empty"][7] == 0
    assert observed["pending-partition"][0] == "pending"
    assert observed["pending-partition"][5] == 1, "one bounded wait ran"
    assert observed["stalled-upstream"][0] == "unresolved"
    assert observed["memory-repeat"][2] == "re_decided"
    assert observed["memory-repeat"][3] == 2, "one same-call second decision"


def test_the_same_scenario_twice_is_deterministic_offline(
    client: LocalClient,
) -> None:
    first = run(client, "verified-empty", "ui-determinism-1")
    second = run(client, "verified-empty", "ui-determinism-2")
    for field in ("status", "reason_code", "path", "task_hash", "query_hash"):
        assert first[field] == second[field], field


def test_the_user_interface_offers_exactly_the_allowlist_offline() -> None:
    """A token the UI offers that the service refuses would be a dead control."""
    markup = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    block = markup.split('<select id="scenario"', 1)[1].split("</select>", 1)[0]
    offered = tuple(re.findall(r'<option value="([^"]+)"', block))
    assert offered == SCENARIO_TOKENS
    for scenario in SCENARIOS:
        assert scenario.title in block


def test_a_scenario_label_is_never_a_measurement_offline(
    client: LocalClient,
) -> None:
    view = run(client, "stalled-upstream", "ui-label-01")
    assert view["classification"] == "synthetic"
    detail = " ".join(node["detail"] for node in view["spine"])
    assert "Synthetic" in view["outcome_text"] or "synthetic" in detail
    panel = client.get(
        f"/api/runs/{view['run_id']}/evidence/result", headers=READ_HEADERS
    )
    assert panel.status_code == 200
