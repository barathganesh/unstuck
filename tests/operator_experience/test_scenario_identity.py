"""Scenario identity is bound to the idempotency key. Offline, in process.

Repair wave 3, finding 1: the run store keys a run by its idempotency key
alone, and every declared scenario shares one immutable task and one dataset
version, so a key first used for one scenario satisfied the store's duplicate
check when it was reused for another. The caller was handed the first
scenario's run, reported as a success, and the run view named no scenario at
all.

These tests assert the opposite four ways: the binding is persisted inside the
run repository boundary and survives a new service over the same root, an exact
duplicate resumes its own run, the same key with a different scenario fails
closed with a typed mismatch and returns no run, and the scenario and its
canonical fixture identity reach the run view, every export format, and the
visible static summary.
"""

import csv
import io
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from unstuck.clock import FakeClock
from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.app import STATIC_ROOT, OfflineRunService, create_app
from unstuck.operator.ledger import (
    BINDING_DIRECTORY,
    ScenarioBinding,
    ScenarioLedger,
    fixture_identity,
)
from unstuck.operator.scenarios import resolve_scenario
from unstuck.storage import LocalRunRepository

from .conftest import JSON_HEADERS, READ_HEADERS
from .test_endpoints import LocalClient

KEY = "ui-one-key"


@pytest.fixture
def client(repository: LocalRunRepository, clock: FakeClock) -> LocalClient:
    return LocalClient(create_app(repository=repository, clock=clock))


def start(client: LocalClient, scenario: str, key: str = KEY) -> Any:
    return client.post(
        "/api/runs",
        json={"scenario": scenario, "idempotency_key": key},
        headers=JSON_HEADERS,
    )


def test_one_key_reused_for_another_scenario_fails_closed_offline(
    client: LocalClient,
) -> None:
    """The confirmed defect: the second request must not be answered at all."""
    first = start(client, "repairable-join")
    assert first.status_code == 200, first.text
    repaired = first.json()
    assert repaired["status"] == "completed"

    second = start(client, "verified-empty")
    assert second.status_code == 409, second.text
    body = second.json()
    assert body["reason_code"] == OperatorReason.SCENARIO_MISMATCH.value
    assert "repairable-join" in body["detail"]
    assert "verified-empty" in body["detail"]
    # No run was returned, so the first scenario's result cannot be mistaken
    # for the second scenario's.
    assert "run_id" not in body
    assert body["status"] == "refused"
    assert "spine" not in body


def test_an_exact_duplicate_resumes_its_own_run_offline(client: LocalClient) -> None:
    first = start(client, "memory-repeat").json()
    second = start(client, "memory-repeat")
    assert second.status_code == 200, second.text
    resumed = second.json()
    assert resumed["run_id"] == first["run_id"]
    assert resumed["scenario"] == "memory-repeat"
    assert resumed["fixture_identity"] == first["fixture_identity"]


def test_the_run_view_reports_the_scenario_it_was_created_from_offline(
    client: LocalClient,
) -> None:
    created = start(client, "verified-empty").json()
    assert created["scenario"] == "verified-empty"
    expected = fixture_identity(resolve_scenario("verified-empty").fixture())
    assert created["fixture_identity"] == expected

    read = client.get(f"/api/runs/{created['run_id']}", headers=READ_HEADERS)
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["scenario"] == "verified-empty"
    assert body["fixture_identity"] == expected


def test_two_scenarios_under_two_keys_keep_their_own_identities_offline(
    client: LocalClient,
) -> None:
    first = start(client, "repairable-join", "ui-key-a").json()
    second = start(client, "verified-empty", "ui-key-b").json()
    assert first["run_id"] != second["run_id"]
    assert first["scenario"] == "repairable-join"
    assert second["scenario"] == "verified-empty"
    assert first["fixture_identity"] != second["fixture_identity"]


@pytest.mark.parametrize("fmt", ["json", "jsonl", "csv"])
def test_every_export_format_carries_the_scenario_identity_offline(
    client: LocalClient, fmt: str
) -> None:
    created = start(client, "repairable-join").json()
    response = client.post(
        "/api/exports",
        json={"run_id": created["run_id"], "format": fmt},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 200, response.text
    text = response.text
    if fmt == "json":
        record = json.loads(text)["manifest"]
    elif fmt == "jsonl":
        record = json.loads(text.splitlines()[0])
    else:
        record = next(iter(csv.DictReader(io.StringIO(text))))
    assert record["scenario"] == "repairable-join"
    assert record["fixture_identity"] == created["fixture_identity"]


def test_the_binding_is_persisted_and_survives_a_new_service_offline(
    repository: LocalRunRepository, clock: FakeClock
) -> None:
    """Process-local state would not answer finding 1. This is on disk."""
    client = LocalClient(create_app(repository=repository, clock=clock))
    created = start(client, "stalled-upstream").json()
    run_id = UUID(created["run_id"])
    recorded = Path(repository.root) / BINDING_DIRECTORY / f"{run_id}.json"
    assert recorded.is_file()
    body = json.loads(recorded.read_text(encoding="utf-8"))
    assert body["scenario"] == "stalled-upstream"
    assert body["fixture_identity"] == created["fixture_identity"]

    # A second service over the same root, as after a restart, reads it back
    # and still refuses the substitution.
    restarted = OfflineRunService(
        LocalRunRepository(Path(repository.root), clock=clock), clock
    )
    assert restarted.binding(run_id) is not None
    assert str(restarted.binding(run_id).scenario) == "stalled-upstream"  # type: ignore[union-attr]
    with pytest.raises(OperatorFault) as raised:
        restarted.ledger.bind(
            ScenarioBinding.of(
                run_id,
                resolve_scenario("verified-empty"),
                resolve_scenario("verified-empty").fixture(),
                "stalled-upstream-key",
            )
        )
    assert raised.value.reason is OperatorReason.SCENARIO_MISMATCH


def test_a_fixture_identity_change_is_a_different_binding_offline(
    repository: LocalRunRepository,
) -> None:
    """The binding is the scenario token and the fixture it actually built."""
    ledger = ScenarioLedger(Path(repository.root))
    scenario = resolve_scenario("repairable-join")
    run_id = UUID("00000000-0000-5000-8000-000000000001")
    ledger.bind(ScenarioBinding.of(run_id, scenario, scenario.fixture(), KEY))
    drifted = ScenarioBinding.of(run_id, scenario, scenario.fixture(), KEY).model_copy(
        update={"fixture_identity": "b" * 64}
    )
    with pytest.raises(OperatorFault) as raised:
        ledger.bind(drifted)
    assert raised.value.reason is OperatorReason.SCENARIO_MISMATCH


def test_a_linked_binding_file_is_never_followed_offline(
    repository: LocalRunRepository, tmp_path: Path
) -> None:
    import os

    ledger = ScenarioLedger(Path(repository.root))
    run_id = UUID("00000000-0000-5000-8000-000000000002")
    outside = tmp_path / "outside-binding.json"
    outside.write_text("{}", encoding="utf-8")
    os.symlink(outside, ledger.directory / f"{run_id}.json")
    with pytest.raises(OperatorFault) as raised:
        ledger.read(run_id)
    assert raised.value.reason is OperatorReason.SCENARIO_BINDING_UNREADABLE


def test_an_unreadable_binding_is_refused_rather_than_ignored_offline(
    repository: LocalRunRepository,
) -> None:
    """A binding that cannot be read is never read as an absent one."""
    ledger = ScenarioLedger(Path(repository.root))
    run_id = UUID("00000000-0000-5000-8000-000000000003")
    (ledger.directory / f"{run_id}.json").write_text(
        "not a scenario binding", encoding="utf-8"
    )
    with pytest.raises(OperatorFault) as raised:
        ledger.read(run_id)
    assert raised.value.reason is OperatorReason.SCENARIO_BINDING_UNREADABLE

    # A binding that is not a regular file is refused the same way.
    other = UUID("00000000-0000-5000-8000-000000000004")
    (ledger.directory / f"{other}.json").mkdir()
    with pytest.raises(OperatorFault) as raised:
        ledger.read(other)
    assert raised.value.reason is OperatorReason.SCENARIO_BINDING_UNREADABLE

    assert ledger.read(UUID("00000000-0000-5000-8000-000000000005")) is None


def test_a_linked_repository_root_carries_no_binding_offline(tmp_path: Path) -> None:
    """The ledger lives inside the declared root, not inside a link's target."""
    import os

    real = tmp_path / "real-runs"
    real.mkdir()
    os.symlink(real, tmp_path / "linked-runs")
    with pytest.raises(OperatorFault) as raised:
        ScenarioLedger(tmp_path / "linked-runs")
    assert raised.value.reason is OperatorReason.SCENARIO_BINDING_UNREADABLE

    ledger = ScenarioLedger(real)
    os.rmdir(ledger.directory)
    os.symlink(tmp_path, ledger.directory)
    with pytest.raises(OperatorFault) as raised:
        ledger.read(UUID("00000000-0000-5000-8000-000000000006"))
    assert raised.value.reason is OperatorReason.SCENARIO_BINDING_UNREADABLE


def test_the_visible_summary_names_the_scenario_offline() -> None:
    """FR10.1: a reader of the detail view can identify the chosen scenario."""
    body = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert '"Scenario"' in body
    assert '"Fixture identity"' in body
    assert "view.scenario" in body
    assert "view.fixture_identity" in body
