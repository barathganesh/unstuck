from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_controller import replace, setup

from unstuck.clock import FakeClock
from unstuck.domain import Counters, Reason, RuntimeFault, TaskSpec
from unstuck.events import EventRecord, Measurement, Redactor, project_events
from unstuck.storage import LocalRunRepository


def new_run(tmp_path, clock=None, redactor=None):
    repo = LocalRunRepository(
        tmp_path, clock=clock or FakeClock(), redactor=redactor or Redactor()
    )
    task = TaskSpec(dataset_id="ds-17", order_date_utc=date(2026, 10, 18))
    run = repo.create_run(task, "v17", "key-1")
    return repo, run


def test_TST_37_idempotency_and_concurrent_lease(tmp_path):
    repo, run = new_run(tmp_path)
    assert (
        repo.create_run(run.state.task, "v17", "key-1").state.run_id == run.state.run_id
    )
    with pytest.raises(RuntimeFault, match="task_mismatch"):
        repo.create_run(run.state.task, "v18", "key-1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: repo.acquire(run.state.run_id, uuid4()), range(2))
        )
    assert sum(result is not None for result in results) == 1
    assert len(repo.load_run(run.state.run_id).events) == 1


def test_TST_27_expired_lease_does_not_resume(tmp_path):
    clock = FakeClock()
    repo, run = new_run(tmp_path, clock)
    owner = uuid4()
    leased = repo.acquire(run.state.run_id, owner)
    assert leased is not None
    clock.advance(150)
    assert repo.acquire(run.state.run_id, uuid4()) is None
    final = repo.load_run(run.state.run_id)
    assert final.state.reason_code == Reason.LEASE_EXPIRED
    assert [event.type for event in final.events].count("run_finished") == 1
    assert repo.acquire(run.state.run_id, uuid4()) is None


def test_TST_37_compare_and_set_and_event_sequence(tmp_path):
    repo, run = new_run(tmp_path)
    owner = uuid4()
    current = repo.acquire(run.state.run_id, owner)
    updated = repo.record(current, owner, "query_observed", {"count": 0})
    assert updated.state.state_version == current.state.state_version + 1
    assert [event.sequence for event in updated.events] == [1, 2]
    for snapshot, writer in ((current, owner), (updated, uuid4())):
        with pytest.raises(RuntimeFault, match="storage_error"):
            repo.record(snapshot, writer, "query_observed", {})


def test_TST_27_partial_write_never_accepted(tmp_path, monkeypatch):
    repo, run = new_run(tmp_path)
    owner = uuid4()
    current = repo.acquire(run.state.run_id, owner)

    def broken(*args, **kwargs):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("unstuck.storage.os.replace", broken)
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.record(current, owner, "query_observed", {})
    assert repo.load_run(run.state.run_id) == current
    assert repo.acquire(run.state.run_id, uuid4()) is None


def test_TST_27_corruption_fails_closed(tmp_path):
    repo, run = new_run(tmp_path)
    snapshot = tmp_path / str(run.state.run_id) / "snapshot.json"
    snapshot.write_text('{"partial":')
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.load_run(run.state.run_id)
    with pytest.raises(RuntimeFault):
        repo.acquire(run.state.run_id, uuid4())


def test_TST_36_one_final_event_and_projection(tmp_path):
    repo, run = new_run(tmp_path)
    owner = uuid4()
    current = repo.acquire(run.state.run_id, owner)
    final = repo.finish(current, owner, Reason.INSUFFICIENT, {"rows": [], "cost": None})
    assert final.state.status == "unresolved"
    assert project_events(final.events) == final.result
    with pytest.raises(RuntimeFault):
        repo.finish(final, owner, Reason.INSUFFICIENT, {})
    with pytest.raises(RuntimeFault):
        repo.record(final, owner, "probe_started", {})
    assert len(repo.list_events(run.state.run_id, after_seq=1)) == 1


def test_TST_30_redaction_all_serialization_surfaces(tmp_path):
    sentinel = "synthetic-private-value"
    redactor = Redactor(values=(sentinel,))
    repo, run = new_run(tmp_path, redactor=redactor)
    owner = uuid4()
    current = repo.acquire(run.state.run_id, owner)
    payload = {
        "Authorization": "Bearer synthetic-value",
        "nested": [{"api_key": sentinel}],
        "text": "Authorization: Bearer synthetic-value\npassword=synthetic-pass "
        + sentinel,
        "environment_values": {"setting": "synthetic-raw"},
    }
    current = repo.record(current, owner, "query_observed", payload)
    current = repo.save_artifact(current, owner, "capture.json", payload)
    exported = repo.export_events(run.state.run_id)
    serialized = (
        tmp_path / str(run.state.run_id) / "snapshot.json"
    ).read_text() + exported.read_text()
    for forbidden in (sentinel, "synthetic-value", "synthetic-pass", "synthetic-raw"):
        assert forbidden not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.parametrize(
    "name",
    ["../escape", "/absolute", "a/b", "a\\b", "..", "snapshot.json", "events.jsonl"],
)
def test_TST_13_artifact_paths_cannot_escape(tmp_path, name):
    repo, run = new_run(tmp_path)
    owner = uuid4()
    current = repo.acquire(run.state.run_id, owner)
    with pytest.raises(RuntimeFault):
        repo.save_artifact(current, owner, name, {})


def test_TST_13_symlink_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "link"
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeFault):
        LocalRunRepository(root)
    repo, run = new_run(target)
    snapshot = target / str(run.state.run_id) / "snapshot.json"
    snapshot.unlink()
    snapshot.symlink_to(target / "other.json")
    with pytest.raises(RuntimeFault):
        repo.load_run(run.state.run_id)


def test_TST_16_measurements_explicit_and_event_bounds():
    measurement = Measurement()
    assert measurement.model_dump()["value"] is None
    assert measurement.availability == "not_measured"
    for changes in (
        {"availability": "measured"},
        {"value": 0.0},
        {"value": float("inf")},
    ):
        with pytest.raises(ValidationError):
            Measurement(**changes)
    assert Measurement(availability="measured", value=0.0).value == 0.0
    with pytest.raises(ValidationError):
        EventRecord(run_id=uuid4(), sequence=0)
    assert Counters().history_lookups == 0


@pytest.mark.parametrize("boundary", ["initialization", "acquire", "mid_run"])
def test_R04_directory_failures_are_typed(tmp_path, monkeypatch, boundary):
    import asyncio

    controller, repo, run, decider = setup(tmp_path)
    original_report = controller.engine.report
    calls = []

    def denied(*args, **kwargs):
        raise PermissionError("synthetic storage access failure")

    def report(*args):
        calls.append("report")
        observation = original_report(*args)
        context.setattr("unstuck.storage.os.mkdir", denied)
        return observation

    with monkeypatch.context() as context:
        if boundary == "initialization":
            context.setattr("unstuck.storage.os.mkdir", denied)
            with pytest.raises(RuntimeFault, match="storage_error"):
                LocalRunRepository(tmp_path / "new")
            return
        if boundary == "acquire":
            context.setattr("unstuck.storage.os.mkdir", denied)
        else:
            context.setattr(controller.engine, "report", report)
        result = asyncio.run(controller.run(run.state.run_id))
    assert result == {
        "run_id": str(run.state.run_id),
        "status": "failed",
        "reason_code": "storage_error",
        "mode": "offline",
        "provider": "mock",
        "persistence": "unavailable",
    }
    saved = repo.load_run(run.state.run_id)
    assert calls == (["report"] if boundary == "mid_run" else [])
    assert not decider.calls
    assert saved.result is None
    assert not any(event.type == "run_finished" for event in saved.events)
    assert saved.incident.counters.report_queries == (boundary == "mid_run")


@pytest.mark.parametrize("stage", ["unstarted", "diagnosed", "reported"])
def test_R05_recovery_complete_terminal_contract(tmp_path, stage):
    controller, repo, run, decider = setup(tmp_path)
    controller.begin(run.state.run_id)
    recorded = stage != "unstarted"
    rows = []
    if recorded:
        import asyncio

        report = controller.execute_report()
        controller._verify(report)
        asyncio.run(controller._select())
        controller._attempt("wait_checks", "wait_started", {})
        controller._attempt("repair_attempts", "repair_requested", {})
        if stage == "reported":
            controller._event(
                "repair_applied",
                {},
                query=replace(
                    controller.current.query, join_key_mode="strip_country_prefix"
                ),
            )
            report = controller.execute_report()
            controller._verify(report, repaired=True)
            rows = [row.model_dump(mode="json") for row in report.payload.rows]
            assert rows
        controller.snapshot = repo.save_artifact(
            controller.current,
            controller.owner,
            "diagnostic.json",
            {"note": "synthetic"},
        )
    before = repo.load_run(run.state.run_id)
    controller.clock.advance(150)
    assert repo.acquire(run.state.run_id, uuid4()) is None
    final = repo.load_run(run.state.run_id)
    result = final.result
    required = {
        "run_id",
        "status",
        "reason_code",
        "rows",
        "evidence_ids",
        "dataset_version",
        "diagnostic_reads",
        "wait_checks",
        "repair_attempts",
        "history_status",
        "receipt_ids",
        "history_truncated",
        "selection_pass",
        "blocked_tool_id",
        "blocked_reason_code",
        "first_choice",
        "final_choice",
        "confidence_available",
        "verification_status",
        "artifact_refs",
    }
    assert required <= result.keys()
    assert result["run_id"] == str(run.state.run_id)
    assert (
        result["status"] == "failed"
        and result["reason_code"] == "lease_expired_incomplete"
    )
    assert result["rows"] == rows
    assert result["dataset_version"] == run.state.dataset_version
    assert result["evidence_ids"] == [
        str(record.evidence_id) for record in before.evidence
    ]
    for counter in ("diagnostic_reads", "wait_checks", "repair_attempts"):
        assert result[counter] == getattr(before.incident.counters, counter)
    assert result["history_status"] == "not_requested" and result["receipt_ids"] == []
    assert result["selection_pass"] == (1 if recorded else None)
    assert result["blocked_tool_id"] is None and result["blocked_reason_code"] is None
    assert result["confidence_available"] is recorded
    assert result["verification_status"] == ("passed" if recorded else "not_run")
    for field in ("first_choice", "final_choice"):
        assert result[field] == ("inspect_schema_keys" if recorded else None)
    for field in ("first_confidence", "final_confidence"):
        assert result[field] == (0.95 if recorded else None)
    for field in ("cost", "duration_ms"):
        assert result[field]["availability"] == "not_measured"
        assert result[field]["value"] is None
    assert result["artifact_refs"] == (
        ["events.jsonl", "diagnostic.json"] if recorded else ["events.jsonl"]
    )
    assert (
        final.evidence == before.evidence
        and final.incident.counters == before.incident.counters
    )
    assert final.events[:-1] == before.events
    assert sum(event.type == "run_finished" for event in final.events) == 1
    assert final.events[-1].payload == result
    assert repo.acquire(run.state.run_id, uuid4()) is None
    assert repo.load_run(run.state.run_id) == final
    assert len(decider.calls) == int(recorded)


def test_R05_terminal_result_validation(tmp_path):
    repo, run = new_run(tmp_path)
    owner = uuid4()
    current = repo.acquire(run.state.run_id, owner)
    with pytest.raises(ValidationError):
        repo.finish(current, owner, Reason.INSUFFICIENT, {"rows": "not-rows"})
    assert repo.load_run(run.state.run_id) == current
