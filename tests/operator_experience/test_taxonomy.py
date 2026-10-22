"""Producer and projection share one event vocabulary. Offline, in process.

Finding 9: the raw-event map expected `selection_recorded`, `history_consulted`,
`probe_executed`, `wait_completed`, and `report_verified`, none of which any
producer emits, so most provenance panels could not find their event at all.

These tests bind the two sides together behaviourally. Every node a real run
marks present must resolve a real recorded event; every event name the
projection claims must be one the producer actually emitted across the declared
scenarios or one the controller's own incident-state table declares; and the
retired names must appear nowhere.
"""

from typing import Any

import pytest

from unstuck.clock import FakeClock
from unstuck.controller import _STATES
from unstuck.operator.app import create_app
from unstuck.operator.projection import NODE_ORDER, PROJECTED_EVENTS
from unstuck.operator.scenarios import SCENARIO_TOKENS
from unstuck.storage import LocalRunRepository

from .conftest import JSON_HEADERS, READ_HEADERS
from .test_endpoints import LocalClient

#: Names the shipped projection used to expect. No producer emits any of them.
RETIRED_NAMES = (
    "selection_recorded",
    "decision_recorded",
    "history_consulted",
    "probe_executed",
    "wait_completed",
    "report_verified",
)
#: An internal incident-state marker rather than a spine step: it records that
#: the first choice went straight to execution, which the spine already shows
#: as the absence of a blocked repetition.
INTERNAL_STATES = {"direct_path_selected"}


@pytest.fixture
def client(repository: LocalRunRepository, clock: FakeClock) -> LocalClient:
    return LocalClient(create_app(repository=repository, clock=clock))


def observe(client: LocalClient) -> tuple[set[str], list[dict[str, Any]]]:
    """Run every declared scenario and collect what really happened."""
    emitted: set[str] = set()
    views: list[dict[str, Any]] = []
    for index, token in enumerate(SCENARIO_TOKENS):
        created = client.post(
            "/api/runs",
            json={"scenario": token, "idempotency_key": f"ui-taxonomy-{index}"},
            headers=JSON_HEADERS,
        )
        assert created.status_code == 200, created.text
        view = created.json()
        views.append(view)
        export = client.post(
            "/api/exports",
            json={"run_id": view["run_id"], "format": "json"},
            headers=JSON_HEADERS,
        )
        assert export.status_code == 200, export.text
        emitted |= {event["type"] for event in export.json()["events"]}
    return emitted, views


def test_every_present_node_resolves_a_recorded_event_offline(
    client: LocalClient,
) -> None:
    _, views = observe(client)
    checked = 0
    for view in views:
        for node in view["spine"]:
            panel = client.get(
                f"/api/runs/{view['run_id']}/evidence/{node['node_id']}",
                headers=READ_HEADERS,
            )
            assert panel.status_code == 200, node["node_id"]
            raw = panel.json()["raw_event"]
            if not node["present"]:
                continue
            assert raw is not None, f"{node['node_id']} found no raw event"
            assert raw["type"] in PROJECTED_EVENTS
            checked += 1
    assert checked >= len(NODE_ORDER), "the scenarios covered the whole spine"


def test_the_projection_claims_no_event_no_producer_emits_offline(
    client: LocalClient,
) -> None:
    emitted, _ = observe(client)
    declared = set(_STATES) - INTERNAL_STATES
    # Everything the controller's own state table names is projected.
    assert declared <= PROJECTED_EVENTS, declared - PROJECTED_EVENTS
    # And everything the projection claims was either emitted by a real run or
    # is declared by that same table.
    unproven = PROJECTED_EVENTS - emitted - set(_STATES)
    assert unproven == set(), unproven


def test_no_retired_event_name_survives_offline(client: LocalClient) -> None:
    emitted, _ = observe(client)
    for name in RETIRED_NAMES:
        assert name not in PROJECTED_EVENTS, name
        assert name not in emitted, name
        assert name not in _STATES, name


def test_every_spine_node_has_a_projected_event_source_offline() -> None:
    from unstuck.operator.projection import _EVENT_KINDS

    for _node_id, kind, _label in NODE_ORDER:
        assert _EVENT_KINDS.get(kind), kind
