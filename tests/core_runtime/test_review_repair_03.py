import asyncio
import json
from uuid import uuid4

import pytest
from test_controller import FakeDecider, replace, setup
from test_review_repair_02 import MARKER, terminal

from unstuck.decision import SelectionAuthority
from unstuck.domain import Reason, RuntimeFault, TaskSpec
from unstuck.events import Redactor, TerminalResult
from unstuck.fixtures import load_fixture
from unstuck.storage import LocalRunRepository

EMBEDDED = [
    ("akia", "probe_" + MARKER, b"AKIA"),
    ("asia", "xASIA" + "B" * 16 + "y", b"ASIA"),
]
DISPLAY_MARKERS = EMBEDDED + [
    ("evaluator", "evaluator_fixture_marker", b"evaluator"),
    ("dispatch", "private_dispatch_marker", b"private_dispatch"),
]
NEW_SECRET = "synthetic-newly-private-identity"


def snapshot_path(tmp_path, run):
    return tmp_path / str(run.state.run_id) / "snapshot.json"


@pytest.mark.parametrize("marker", [case[1] for case in EMBEDDED], ids=["akia", "asia"])
@pytest.mark.parametrize("surface", ["user_goal", "dataset_version"])
def test_rr03_embedded_access_key_rejected_at_create(tmp_path, surface, marker):
    fixture = load_fixture()
    repo = LocalRunRepository(tmp_path)
    task = TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )
    version = fixture.dataset_version
    if surface == "user_goal":
        task = replace(task, user_goal=marker)
    else:
        version = marker
    with pytest.raises(RuntimeFault, match="task_mismatch"):
        repo.create_run(task, version, "embedded-key")
    assert not list(tmp_path.rglob("snapshot.json"))


@pytest.mark.parametrize("marker", [case[1] for case in EMBEDDED], ids=["akia", "asia"])
@pytest.mark.parametrize("surface", ["used_probes", "selected_id"])
def test_rr03_embedded_access_key_rejected_in_authority(tmp_path, surface, marker):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    current = controller.current
    path = snapshot_path(tmp_path, run)
    before = path.read_bytes()
    if surface == "used_probes":
        changes = {"incident": replace(current.incident, used_probes=(marker,))}
    else:
        changes = {
            "selection": SelectionAuthority(
                decision_id=uuid4(),
                selected_id=marker,
                confidence=0.9,
                model_version="model-safe",
                accepted=True,
            )
        }
    with pytest.raises(RuntimeFault):
        repo.record(current, controller.owner, "audit", {}, **changes)
    assert path.read_bytes() == before
    assert marker.encode() not in before


@pytest.mark.parametrize("marker", [case[1] for case in EMBEDDED], ids=["akia", "asia"])
@pytest.mark.parametrize("recovery", [False, True])
def test_rr03_adapter_embedded_key_never_persists(tmp_path, marker, recovery):
    class UnsafeDecider(FakeDecider):
        async def select(self, state):
            return replace(await super().select(state), selected_id=marker)

    controller, repo, run, decider = setup(tmp_path, decider=UnsafeDecider())
    if recovery:
        controller.begin(run.state.run_id)
        controller._verify(controller.execute_report())
        with pytest.raises(RuntimeFault, match="first_selection_rejected"):
            asyncio.run(controller._select())
        controller.clock.advance(150)
        repo.acquire(run.state.run_id, uuid4())
    else:
        result = asyncio.run(controller.run(run.state.run_id))
        assert result["reason_code"] == "first_selection_rejected"
    final = terminal(repo, run)
    assert final.selection is None
    assert final.result["first_choice"] is None
    assert len(decider.calls) == 1
    assert marker.encode() not in snapshot_path(tmp_path, run).read_bytes()
    assert marker not in repo.export_events(run.state.run_id).read_text()


@pytest.mark.parametrize(
    "marker,needle",
    [case[1:] for case in DISPLAY_MARKERS],
    ids=[case[0] for case in DISPLAY_MARKERS],
)
def test_rr03_display_write_is_sanitized_projection(tmp_path, marker, needle):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    payload = {"note": marker, marker: {"nested": [marker]}}
    current = repo.record(controller.current, controller.owner, "audit", payload)
    current = repo.save_artifact(current, controller.owner, "display.json", payload)
    final = repo.finish(current, controller.owner, Reason.INSUFFICIENT, {})
    raw = snapshot_path(tmp_path, run).read_bytes()
    assert needle not in raw
    assert needle not in repo.export_events(run.state.run_id).read_bytes()
    audit = next(event for event in final.events if event.type == "audit")
    assert "[REDACTED]" in json.dumps(audit.payload)
    assert "[REDACTED]" in json.dumps(final.artifacts["display.json"])
    terminal(repo, run)


@pytest.mark.parametrize("surface", ["event", "artifact"])
def test_rr03_newly_configured_secret_fails_closed(tmp_path, surface):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    payload = {"note": NEW_SECRET}
    if surface == "event":
        current = repo.record(controller.current, controller.owner, "audit", payload)
    else:
        current = repo.save_artifact(
            controller.current, controller.owner, "note.json", payload
        )
    path = snapshot_path(tmp_path, run)
    before = path.read_bytes()
    assert NEW_SECRET.encode() in before
    repo.redactor = Redactor(values=(NEW_SECRET,))
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.load_run(run.state.run_id)
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.list_events(run.state.run_id)
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.export_events(run.state.run_id)
    with pytest.raises(RuntimeFault):
        repo.record(current, controller.owner, "later", {})
    with pytest.raises(RuntimeFault):
        repo.save_artifact(current, controller.owner, "later.json", {})
    with pytest.raises(RuntimeFault):
        repo.finish(current, controller.owner, Reason.INSUFFICIENT, {})
    controller.clock.advance(150)
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.acquire(run.state.run_id, uuid4())
    assert path.read_bytes() == before
    assert not (path.parent / "events.jsonl").exists()


@pytest.mark.parametrize(
    "marker",
    [case[1] for case in DISPLAY_MARKERS] + [NEW_SECRET],
    ids=[case[0] for case in DISPLAY_MARKERS] + ["configured"],
)
@pytest.mark.parametrize("surface", ["event", "artifact"])
def test_rr03_forged_display_data_fails_closed(tmp_path, surface, marker):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    path = snapshot_path(tmp_path, run)
    contents = json.loads(path.read_text())
    if surface == "event":
        contents["events"][0]["payload"]["probe"] = marker
    else:
        contents["artifacts"]["forged.json"] = {"probe": marker}
    path.write_text(json.dumps(contents))
    before = path.read_bytes()
    repo.redactor = Redactor(values=(NEW_SECRET,))
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.load_run(run.state.run_id)
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.export_events(run.state.run_id)
    controller.clock.advance(150)
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.acquire(run.state.run_id, uuid4())
    assert path.read_bytes() == before
    assert not (path.parent / "events.jsonl").exists()


def test_rr03_safe_authority_recurses_collections():
    redactor = Redactor()
    assert redactor.safe_authority({"safe": ["ok", 1], "nested": {"k": "v"}}) is True
    assert redactor.safe_authority(["probe_" + MARKER]) is False
    assert redactor.safe_authority({"k": "evaluator_fixture_marker"}) is False
    assert redactor.safe_authority({"private_dispatch_marker": "v"}) is False


def test_rr03_safe_timeline_controls_preserved(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    repo.redactor = controller.redactor = Redactor(values=("unrelated-secret",))
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["status"] == "completed"
    assert result["reason_code"] == "populated_after_repair"
    assert result["first_choice"] == result["final_choice"] == "inspect_schema_keys"
    assert result["first_confidence"] == result["final_confidence"] == 0.95
    assert result["confidence_available"] is True
    assert result["model_version"] == "scripted-v1"
    assert result["verification_status"] == "passed"
    assert result["repair_attempts"] == 1
    assert result["report_evidence_id"] is not None
    assert result["verification_id"] is not None
    assert result["duration_ms"]["availability"] == "measured"
    final = terminal(repo, run)
    for event in final.events:
        if event.type == "run_finished":
            assert event.payload == final.result
            TerminalResult.model_validate_json(json.dumps(event.payload))
        else:
            assert repo.redactor.clean(event.payload) == event.payload
    for value in final.artifacts.values():
        assert repo.redactor.clean(value) == value
    probe = next(event for event in final.events if event.type == "probe_returned")
    assert probe.payload["tool_id"] == "inspect_schema_keys"
    assert "evidence_id" in probe.payload
    first = next(
        event for event in final.events if event.type == "decider_first_returned"
    )
    assert first.payload["measured_latency_ms"]["availability"] == "measured"
    repair = next(event for event in final.events if event.type == "repair_applied")
    assert repair.payload["join_key_mode"] == "strip_country_prefix"
