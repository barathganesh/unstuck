import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import duckdb
import pytest
from pydantic import ValidationError
from test_controller import FakeDecider, replace, setup
from test_storage import new_run

from unstuck.decision import PublicEvidence
from unstuck.domain import Reason, RuntimeFault, VerificationResult
from unstuck.events import project_events
from unstuck.fixtures import load_fixture
from unstuck.storage import Snapshot


@pytest.mark.parametrize(
    "change", ["task", "identity", "sequence", "event", "projection"]
)
def test_TST_37_snapshot_integrity(tmp_path, change):
    repo, run = new_run(tmp_path)
    snapshot = repo.acquire(run.state.run_id, uuid4())
    values = snapshot.model_dump()
    if change == "task":
        values["query"]["task"]["country_codes"] = ("US",)
    elif change == "identity":
        values["query"]["dataset_version"] = "other-version"
    elif change == "sequence":
        values["events"][0]["sequence"] = 2
    elif change == "event":
        values["events"][0]["run_id"] = uuid4()
    else:
        values["result"] = {"status": "completed"}
    with pytest.raises(ValidationError):
        Snapshot.model_validate(values)


@pytest.mark.parametrize("key", ["", "x" * 129])
def test_TST_37_invalid_idempotency_key(tmp_path, key):
    repo, run = new_run(tmp_path)
    with pytest.raises(RuntimeFault, match="invariant"):
        repo.create_run(run.state.task, "v17", key)


def test_TST_27_uncommitted_snapshot_and_reserved_final(tmp_path):
    repo, run = new_run(tmp_path)
    owner = uuid4()
    current = repo.acquire(run.state.run_id, owner)
    with pytest.raises(RuntimeFault, match="invariant"):
        repo.record(current, owner, "run_finished", {})
    directory = tmp_path / str(run.state.run_id)
    (directory / "snapshot.json").unlink()
    (directory / ".pending-interrupted").write_text("incomplete")
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.create_run(run.state.task, "v17", "key-1")


def test_TST_13_local_symlink_guards(tmp_path):
    repo, run = new_run(tmp_path)
    target = tmp_path / "synthetic-target"
    target.write_text("unchanged")
    lock = tmp_path / str(run.state.run_id) / "writer.lock"
    lock.unlink()
    lock.symlink_to(target)
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.load_run(run.state.run_id)
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo._atomic(lock, "changed")
    other_id = uuid4()
    (tmp_path / str(other_id)).symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.load_run(other_id)
    assert target.read_text() == "unchanged"


@pytest.mark.parametrize(
    "case", ["stale", "schema", "pipeline", "scope", "scope_metadata", "mixed"]
)
def test_TST_25_conflicting_facts_stop(tmp_path, case):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    query = controller.current.query
    schema = controller.engine.diagnose("inspect_schema_keys", query)
    pipeline = controller.engine.diagnose("inspect_pipeline", query)
    scope = controller.engine.diagnose("inspect_scope", query)
    if case == "stale":
        records = (
            replace(schema, observed_at=schema.observed_at - timedelta(seconds=121)),
        )
    elif case == "schema":
        records = (
            schema,
            replace(
                schema,
                evidence_id=uuid4(),
                payload=replace(schema.payload, metadata_complete=False),
            ),
        )
    elif case == "pipeline":
        records = (
            pipeline,
            replace(
                pipeline,
                evidence_id=uuid4(),
                payload=replace(pipeline.payload, job_state="failed"),
            ),
        )
    elif case == "scope":
        records = (replace(scope, payload=replace(scope.payload, scope_covered=False)),)
    elif case == "scope_metadata":
        records = (
            replace(scope, payload=replace(scope.payload, metadata_complete=False)),
        )
    else:
        records = (
            schema,
            replace(pipeline, payload=replace(pipeline.payload, scope_complete=False)),
        )
    controller._event("public_evidence_observed", {}, evidence=records)
    with pytest.raises(RuntimeFault) as captured:
        controller._facts()
    expected = Reason.INSUFFICIENT if case == "scope_metadata" else Reason.CONFLICT
    assert captured.value.reason == expected
    controller._finish(expected)
    assert repo.list_events(run.state.run_id)[-1].type == "run_finished"


@pytest.mark.parametrize("repaired", [False, True])
def test_TST_22_verification_failure_no_more_actions(tmp_path, monkeypatch, repaired):
    controller, repo, run, decider = setup(tmp_path)
    controller.begin(run.state.run_id)
    report = controller.execute_report()
    monkeypatch.setattr(
        "unstuck.controller.verify",
        lambda *args, **kwargs: VerificationResult(
            status="failed", reason_code=Reason.CONFLICT
        ),
    )
    with pytest.raises(RuntimeFault) as captured:
        controller._verify(report, repaired=repaired)
    expected = Reason.REPAIR_FAILED if repaired else Reason.CONFLICT
    assert captured.value.reason == expected
    controller._finish(expected)
    assert not decider.calls
    assert repo.load_run(run.state.run_id).incident.counters.report_queries == 1


def test_TST_08_input_capacity_and_invalid_decision(tmp_path, monkeypatch):
    controller, repo, run, decider = setup(tmp_path)
    controller.begin(run.state.run_id)
    state = controller._decision_state(controller._candidates())
    record = controller.engine.report(controller.current.query, run.state.task_hash)
    observation = PublicEvidence(
        evidence_id=record.evidence_id,
        kind=record.kind,
        tool_id=record.tool_id,
        dataset_version=record.dataset_version,
        arguments_hash=record.arguments_hash,
        observed_at=record.observed_at.isoformat(),
        facts={"sample": "x" * 8000},
    )
    with pytest.raises(ValidationError, match="reserved capacity"):
        replace(state, bounded_evidence=(observation,))

    async def invalid(state):
        return {"selected_id": "inspect_schema_keys"}

    monkeypatch.setattr(decider, "select", invalid)
    with pytest.raises(RuntimeFault) as captured:
        asyncio.run(controller._select())
    assert captured.value.reason == Reason.FIRST_REJECTED
    assert repo.load_run(run.state.run_id).incident.counters.diagnostic_reads == 0


def test_TST_16_event_guards(tmp_path):
    repo, run = new_run(tmp_path)
    owner = uuid4()
    current = repo.acquire(run.state.run_id, owner)
    with pytest.raises(ValidationError, match="bounds"):
        replace(current.events[0], payload={"text": "x" * 32001})
    final = repo.finish(current, owner, Reason.INSUFFICIENT, {})
    with pytest.raises(RuntimeFault, match="invariant"):
        project_events((final.events[-1], final.events[-1]))


def test_TST_24_empty_after_supported_repair(tmp_path):
    controller, repo, run, _ = setup(tmp_path, replace(load_fixture(), orders=()))
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == Reason.EMPTY_AFTER_REPAIR.value
    assert repo.load_run(run.state.run_id).incident.counters.repair_attempts == 1


@pytest.mark.parametrize("scope_complete", [False, True])
def test_TST_04_wait_without_encoding_proof(tmp_path, scope_complete):
    fixture = replace(
        load_fixture(),
        job_state="pending",
        scope_complete=scope_complete,
        arrival_after_seconds=0.0,
    )
    controller, repo, run, _ = setup(
        tmp_path, fixture, FakeDecider(choice="inspect_pipeline")
    )
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == Reason.INSUFFICIENT.value
    assert repo.load_run(run.state.run_id).incident.counters.wait_checks == 1


@pytest.mark.parametrize(
    "error,reason",
    [
        (duckdb.Error("synthetic query failure"), Reason.TOOL_ERROR),
        (RuntimeFault(Reason.CONFLICT), Reason.CONFLICT),
    ],
)
def test_TST_27_typed_tool_error(tmp_path, monkeypatch, error, reason):
    controller, repo, run, _ = setup(tmp_path)

    def failed(*args):
        raise error

    monkeypatch.setattr(controller.engine, "report", failed)
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == reason.value
    assert repo.load_run(run.state.run_id).incident.counters.report_queries == 1


def test_TST_27_storage_failure_finalizing_runtime_fault(tmp_path, monkeypatch):
    controller, repo, run, decider = setup(tmp_path)
    finish_reasons = []

    def failed_report(*args):
        raise RuntimeFault(Reason.CONFLICT)

    def failed_finish(snapshot, owner, reason, payload):
        finish_reasons.append(reason)
        raise RuntimeFault(Reason.STORAGE_ERROR)

    monkeypatch.setattr(controller.engine, "report", failed_report)
    monkeypatch.setattr(repo, "finish", failed_finish)
    result = asyncio.run(controller.run(run.state.run_id))

    assert result == {
        "run_id": str(run.state.run_id),
        "status": "failed",
        "reason_code": "storage_error",
        "mode": "offline",
        "provider": "mock",
        "persistence": "unavailable",
    }
    assert finish_reasons == [Reason.CONFLICT]
    saved = repo.load_run(run.state.run_id)
    assert saved.incident.counters.report_queries == 1
    assert saved.result is None
    assert all(event.type != "run_finished" for event in saved.events)
    assert not decider.calls


@pytest.mark.parametrize(
    "error,reason",
    [
        pytest.param(
            RuntimeFault(Reason.CONFLICT), Reason.CONFLICT, id="runtime_fault"
        ),
        pytest.param(
            ValidationError.from_exception_data(
                "VerificationResult",
                [{"type": "missing", "loc": ("status",), "input": {}}],
            ),
            Reason.FIRST_REJECTED,
            id="validation_error",
        ),
        pytest.param(
            duckdb.Error("synthetic verification failure"),
            Reason.TOOL_ERROR,
            id="duckdb_error",
        ),
    ],
)
@pytest.mark.parametrize(
    "finalization_reason",
    [Reason.STORAGE_ERROR, None, Reason.INVARIANT],
    ids=["storage_error", "persisted", "non_storage_fault"],
)
def test_TST_27_classified_error_finalization(
    tmp_path, monkeypatch, error, reason, finalization_reason
):
    controller, repo, run, decider = setup(tmp_path)
    finish_reasons = []
    original_finish = repo.finish
    finalization_error = (
        RuntimeFault(finalization_reason) if finalization_reason is not None else None
    )

    def failed_verify(*args):
        raise error

    def finish(snapshot, owner, reason, payload):
        finish_reasons.append(reason)
        if finalization_error is not None:
            raise finalization_error
        return original_finish(snapshot, owner, reason, payload)

    monkeypatch.setattr(controller, "_verify", failed_verify)
    monkeypatch.setattr(repo, "finish", finish)
    if finalization_reason == Reason.INVARIANT:
        with pytest.raises(RuntimeFault) as captured:
            asyncio.run(controller.run(run.state.run_id))
        assert captured.value is finalization_error
    else:
        result = asyncio.run(controller.run(run.state.run_id))
        if finalization_reason == Reason.STORAGE_ERROR:
            assert result == {
                "run_id": str(run.state.run_id),
                "status": "failed",
                "reason_code": "storage_error",
                "mode": "offline",
                "provider": "mock",
                "persistence": "unavailable",
            }
        else:
            assert result["reason_code"] == reason.value
            assert result["status"] == "unresolved"
            assert repo.load_run(run.state.run_id).result == result

    assert finish_reasons == [reason]
    saved = repo.load_run(run.state.run_id)
    assert saved.incident.counters.report_queries == 1
    assert saved.query == run.query
    assert len(saved.evidence) == 1
    assert not decider.calls
    assert (saved.result is None) == (finalization_reason is not None)
    assert sum(event.type == "run_finished" for event in saved.events) == (
        1 if finalization_reason is None else 0
    )
    assert sum(event.type == "selection_unresolved" for event in saved.events) == (
        1 if reason == Reason.FIRST_REJECTED else 0
    )


def test_TST_02_dataset_version_mismatch(tmp_path):
    controller, _, run, _ = setup(tmp_path)
    with pytest.raises(RuntimeFault) as captured:
        controller.engine.report(
            replace(run.query, dataset_version="other"), run.state.task_hash
        )
    assert captured.value.reason == Reason.CONFLICT


def test_TST_13_duplicate_fixture_dimension():
    fixture = load_fixture()
    with pytest.raises(ValidationError, match="duplicate dimension"):
        replace(fixture, countries=(fixture.countries[0], fixture.countries[0]))


def test_TST_27_failure_before_temporary_write(tmp_path, monkeypatch):
    repo, run = new_run(tmp_path)
    owner = uuid4()
    current = repo.acquire(run.state.run_id, owner)

    original = os.open

    def failed(path, *args, **kwargs):
        if str(path).startswith(".pending-"):
            raise OSError("synthetic temporary file failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr("unstuck.storage.os.open", failed)
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.record(current, owner, "query_observed", {})
    assert repo.load_run(run.state.run_id) == current


def test_TST_24_no_dimension_rows(tmp_path):
    controller, repo, run, decider = setup(
        tmp_path, replace(load_fixture(), countries=())
    )
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == Reason.REPAIR_FAILED.value
    assert result["rows"] == []
    assert len(decider.calls) == 1
    assert repo.load_run(run.state.run_id).incident.counters.repair_attempts == 1


def test_TST_24_incomplete_partition_cannot_verify_empty(tmp_path):
    fixture = replace(load_fixture(), orders=(), scope_complete=False)
    controller, repo, run, _ = setup(tmp_path, fixture)
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == Reason.INSUFFICIENT.value
    assert repo.load_run(run.state.run_id).incident.counters.repair_attempts == 1
