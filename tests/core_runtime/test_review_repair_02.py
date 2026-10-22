import asyncio
import json
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from test_controller import FakeDecider, blocked_by, replace, setup

from unstuck.clock import FakeClock
from unstuck.decision import Probe, PublicProbe
from unstuck.domain import QueryFacts, Reason, Row, RuntimeFault, Settings, TaskSpec
from unstuck.events import Redactor, TerminalResult
from unstuck.fixtures import load_fixture
from unstuck.storage import LocalRunRepository

MARKER = "AKIA" + "A" * 16


def terminal(repo, run):
    snapshot = repo.load_run(run.state.run_id)
    assert snapshot.result is not None
    TerminalResult.model_validate_json(json.dumps(snapshot.result))
    assert snapshot.events[-1].payload == snapshot.result
    assert sum(event.type == "run_finished" for event in snapshot.events) == 1
    return snapshot


@pytest.mark.parametrize("surface", ["selected_id", "candidate_ids", "probability_id"])
@pytest.mark.parametrize("recovery", [False, True])
def test_R06_rejected_redacted_decision_finishes(tmp_path, surface, recovery):
    class UnsafeDecider(FakeDecider):
        async def select(self, state):
            decision = await super().select(state)
            if surface == "selected_id":
                return replace(decision, selected_id=MARKER)
            if surface == "candidate_ids":
                return replace(decision, candidate_ids=(MARKER,))
            return replace(
                decision,
                probabilities=(replace(decision.probabilities[0], id=MARKER),),
            )

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
    assert final.incident.counters.diagnostic_reads == 0
    assert len(decider.calls) == 1
    assert (
        MARKER not in (tmp_path / str(run.state.run_id) / "snapshot.json").read_text()
    )


def test_R06_display_verification_is_not_authority(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    repo.redactor = controller.redactor = Redactor(values=("passed",))
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["status"] == "completed"
    assert result["verification_status"] == "passed"
    terminal(repo, run)


@pytest.mark.parametrize("recovery", [False, True])
def test_R05_accepted_choice_survives_tool_failure(tmp_path, monkeypatch, recovery):
    controller, repo, run, decider = setup(tmp_path)

    def failed(*args):
        raise RuntimeFault(Reason.TOOL_ERROR)

    monkeypatch.setattr(controller.engine, "diagnose", failed)
    if recovery:
        controller.begin(run.state.run_id)
        controller._verify(controller.execute_report())
        with pytest.raises(RuntimeFault, match="final_selection_rejected"):
            asyncio.run(controller._select())
        controller.clock.advance(150)
        repo.acquire(run.state.run_id, uuid4())
    else:
        result = asyncio.run(controller.run(run.state.run_id))
        blocked_by(result, "provider_error_tool")
    final = terminal(repo, run)
    # The accepted first choice survives the read that failed under it: it is
    # recorded as the first choice with its reported confidence, and the
    # second decision that opened could name no remaining candidate.
    assert final.result["first_choice"] == "inspect_schema_keys"
    assert final.result["first_confidence"] == 0.95
    assert final.result["final_choice"] is None
    assert final.result["final_confidence"] is None
    assert final.first_selection is not None
    assert final.first_selection.selected_id == "inspect_schema_keys"
    assert final.first_selection.accepted
    assert final.incident.counters.diagnostic_reads == 1
    assert len(decider.calls) == 2


def test_A01_foreign_initial_report_cannot_supply_terminal_rows(tmp_path):
    fixture = load_fixture()
    fixture = replace(
        fixture,
        orders=(),
        countries=tuple(
            replace(country, country_id=country.country_code)
            for country in fixture.countries
        ),
        countries_key_encoding="plain",
    )
    controller, repo, run, decider = setup(tmp_path, fixture)
    schema = controller.engine.diagnose("inspect_schema_keys", run.query)
    foreign = replace(
        controller.engine.report(run.query, run.state.task_hash),
        dataset_id="foreign",
        arguments_hash="0" * 64,
        observed_at=controller.clock.utcnow() - timedelta(seconds=121),
        payload=QueryFacts(
            dataset_version=fixture.dataset_version,
            query_hash="0" * 64,
            rows=(Row(country_code="DE", units=999),),
        ),
    )
    controller.initial_evidence = (foreign, schema)
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == "evidence_conflict"
    assert result["rows"] == []
    assert not decider.calls
    assert foreign not in terminal(repo, run).evidence


def test_A01_recovery_does_not_verify_a_different_report(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    controller._verify(controller.execute_report())
    asyncio.run(controller._select())
    controller._attempt("repair_attempts", "repair_requested", {})
    controller._event(
        "repair_applied",
        {},
        query=replace(controller.current.query, join_key_mode="strip_country_prefix"),
    )
    controller.execute_report()
    controller.clock.advance(150)
    repo.acquire(run.state.run_id, uuid4())
    final = terminal(repo, run)
    assert final.result["rows"] == []
    assert final.result["verification_status"] == "not_run"


@pytest.mark.parametrize("defect", ["mixed", "unknown", "missing_dimension"])
def test_A02_literal_partial_results_never_complete(tmp_path, defect):
    fixture = load_fixture()
    countries = tuple(
        replace(country, country_id=country.country_code)
        for country in fixture.countries
    )
    if defect == "mixed":
        countries = (fixture.countries[0], countries[1])
    elif defect == "missing_dimension":
        countries = countries[:1]
    fixture = replace(
        fixture,
        countries=countries,
        countries_key_encoding="unknown" if defect == "unknown" else "plain",
    )
    controller, repo, run, decider = setup(tmp_path, fixture)
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["status"] == "unresolved"
    assert result["verification_status"] == "failed"
    assert result["rows"] == []
    assert not decider.calls
    assert terminal(repo, run).incident.counters.report_queries == 1


@pytest.mark.parametrize("marker", [MARKER, "synthetic-private-key"])
def test_A03_dictionary_keys_are_redacted_and_unsafe(tmp_path, marker):
    controller, repo, run, _ = setup(tmp_path)
    repo.redactor = Redactor(values=("synthetic-private-key",))
    controller.begin(run.state.run_id)
    payload = {"nested": [{marker: "synthetic"}]}
    assert not repo.redactor.safe_authority(payload)
    current = repo.record(controller.current, controller.owner, "audit", payload)
    repo.save_artifact(current, controller.owner, "audit.json", payload)
    text = (tmp_path / str(run.state.run_id) / "snapshot.json").read_text()
    text += repo.export_events(run.state.run_id).read_text()
    assert marker not in text


@pytest.mark.parametrize("marker", [MARKER, "synthetic-private-version"])
def test_A03_immutable_version_rejected_before_persistence(tmp_path, marker):
    fixture = load_fixture()
    repo = LocalRunRepository(tmp_path, redactor=Redactor(values=(marker,)))
    task = TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )
    with pytest.raises(RuntimeFault, match="task_mismatch"):
        repo.create_run(task, marker, "unsafe-version")
    assert not list(tmp_path.rglob("snapshot.json"))


@pytest.mark.parametrize(
    "configured", [False, True], ids=["access-key", "configured-secret"]
)
def test_R07_unsafe_event_type_rejected_before_persistence(tmp_path, configured):
    marker = "synthetic-private-event" if configured else MARKER
    controller, repo, run, decider = setup(tmp_path)
    repo.redactor = Redactor(values=(marker,) if configured else ())
    controller.begin(run.state.run_id)
    current = controller.current
    path = tmp_path / str(run.state.run_id) / "snapshot.json"
    before = path.read_bytes()
    exported = repo.export_events(run.state.run_id)
    before_export = exported.read_bytes()

    with pytest.raises(RuntimeFault) as failure:
        repo.record(current, controller.owner, marker, {"note": marker})

    assert failure.value.reason == Reason.INVARIANT
    assert marker not in str(failure.value)
    assert path.read_bytes() == before
    assert repo.load_run(run.state.run_id) == current
    assert exported.read_bytes() == before_export
    assert repo.export_events(run.state.run_id).read_bytes() == before_export
    assert marker.encode() not in path.read_bytes()
    assert marker.encode() not in exported.read_bytes()
    repo.finish(current, controller.owner, Reason.INSUFFICIENT, {})
    final = terminal(repo, run)
    assert final.events[:-1] == current.events
    assert marker.encode() not in path.read_bytes()
    assert marker.encode() not in repo.export_events(run.state.run_id).read_bytes()
    assert not decider.calls


def test_R07_safe_custom_event_type_preserved(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    current = controller.current
    event_type = "custom-audit_17"
    updated = repo.record(current, controller.owner, event_type, {"note": MARKER})
    event = updated.events[-1]
    assert event.type == event_type
    assert event.payload == {"note": "[REDACTED]"}
    assert event.sequence == len(current.events) + 1
    assert updated.state.state_version == current.state.state_version + 1
    assert repo.load_run(run.state.run_id) == updated
    exported = repo.export_events(run.state.run_id).read_text()
    assert json.loads(exported.splitlines()[-1])["type"] == event_type
    assert MARKER not in exported


@pytest.mark.parametrize("capability", ["inspect_pipeline", "inspect_scope"])
def test_A04_diagnostic_must_match_selected_capability(
    tmp_path, monkeypatch, capability
):
    controller, repo, run, _ = setup(tmp_path, decider=FakeDecider(choice=capability))
    original = controller.engine.diagnose
    monkeypatch.setattr(
        controller.engine,
        "diagnose",
        lambda selected, query: original("inspect_schema_keys", query),
    )
    result = asyncio.run(controller.run(run.state.run_id))
    # The mismatched read is refused as invalid evidence, which opens the one
    # second decision this selector cannot answer.
    blocked_by(result, "evidence_conflict", tool=capability)
    final = terminal(repo, run)
    assert final.incident.counters.repair_attempts == 0
    assert len(final.evidence) == 1


@pytest.mark.parametrize("identifiers", [("request_review",), ("same", "same")])
def test_A05_invalid_candidate_identity_never_executes(tmp_path, identifiers):
    catalog = tuple(
        Probe(
            PublicProbe(
                id=identifier,
                description="Inspect schema",
                applies_when="it is still open whether the encoding matches",
                observable_facts=("encoding",),
            ),
            "inspect_schema_keys",
        )
        for identifier in identifiers
    )
    controller, repo, run, decider = setup(
        tmp_path, decider=FakeDecider(choice=identifiers[0]), catalog=catalog
    )
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == "first_selection_rejected"
    assert not decider.calls
    assert terminal(repo, run).incident.counters.diagnostic_reads == 0


@pytest.mark.parametrize("target", ["root", "ancestor", "directory"])
def test_A06_replaced_directory_never_redirects_writes(tmp_path, target):
    parent = tmp_path / "parent"
    root = parent / "runs"
    controller, repo, run, _ = setup(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    replaced = root if target == "root" else parent
    if target == "directory":
        replaced = root / str(run.state.run_id)
    replaced.rename(tmp_path / "original")
    replaced.symlink_to(outside, target_is_directory=True)
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == "storage_error"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "corruption", ["terminal", "incident", "reason", "utf8", "legacy"]
)
def test_A07_incoherent_snapshot_is_typed_storage_error(tmp_path, corruption):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    path = tmp_path / str(run.state.run_id) / "snapshot.json"
    content = json.loads(path.read_text())
    if corruption == "terminal":
        content["state"]["status"] = "completed"
    elif corruption == "incident":
        content["incident"]["state"] = "DONE"
    elif corruption == "reason":
        content["state"]["reason_code"] = "populated_initial"
    elif corruption == "legacy":
        for field in ("authority_version", "selection", "verification"):
            content.pop(field, None)
    path.write_bytes(b"\xff" if corruption == "utf8" else json.dumps(content).encode())
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.load_run(run.state.run_id)


@pytest.mark.parametrize("boundary", ["report", "diagnose", "recheck"])
@pytest.mark.parametrize("value", [None, {}, "invalid"])
def test_A08_malformed_adapter_return_is_typed(tmp_path, monkeypatch, boundary, value):
    fixture = load_fixture()
    if boundary == "recheck":
        fixture = replace(fixture, job_state="pending")
    controller, repo, run, _ = setup(
        tmp_path,
        fixture,
        FakeDecider(
            choice="inspect_pipeline"
            if boundary == "recheck"
            else "inspect_schema_keys"
        ),
    )
    monkeypatch.setattr(controller.engine, boundary, lambda *args: value)
    result = asyncio.run(controller.run(run.state.run_id))
    if boundary == "diagnose":
        blocked_by(result, "provider_error_tool")
    else:
        assert result["reason_code"] == "provider_error_tool"
    terminal(repo, run)


def test_A09_deadline_after_verification_persistence_stops(tmp_path, monkeypatch):
    controller, repo, run, _ = setup(tmp_path)
    original = repo.record

    def delayed(snapshot, owner, event_type, payload, **kwargs):
        updated = original(snapshot, owner, event_type, payload, **kwargs)
        if (
            event_type == "verification_finished"
            and snapshot.query.join_key_mode != "literal"
        ):
            controller.clock.advance(120)
        return updated

    monkeypatch.setattr(repo, "record", delayed)
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == "run_deadline"
    final = terminal(repo, run)
    assert final.events[-2].type == "budget_stop"


@pytest.mark.parametrize("elapsed", [149, 150, 300])
def test_A10_cross_epoch_lease_never_replays(tmp_path, elapsed):
    controller, repo, run, _ = setup(tmp_path)
    controller.clock.advance(100000)
    controller.begin(run.state.run_id)
    restart = FakeClock()
    restart.origin = controller.clock.utcnow() + timedelta(seconds=elapsed)
    restarted = LocalRunRepository(tmp_path, clock=restart)
    assert restarted.acquire(run.state.run_id, uuid4()) is None
    final = restarted.load_run(run.state.run_id)
    if elapsed < 150:
        assert final.state.status == "running"
    else:
        assert terminal(restarted, run).state.reason_code == Reason.LEASE_EXPIRED
    assert final.incident.counters.report_queries == 0


@pytest.mark.parametrize("boundary", ["report", "diagnose", "recheck", "verify"])
def test_A08_unvalidated_models_cannot_cross_adapter_boundary(
    tmp_path, monkeypatch, boundary
):
    fixture = load_fixture()
    if boundary == "recheck":
        fixture = replace(fixture, job_state="pending")
    controller, repo, run, _ = setup(
        tmp_path,
        fixture,
        FakeDecider(
            choice="inspect_pipeline"
            if boundary == "recheck"
            else "inspect_schema_keys"
        ),
    )
    if boundary == "verify":
        monkeypatch.setattr("unstuck.controller.verify", lambda *args, **kwargs: {})
    else:
        original = getattr(controller.engine, boundary)

        def invalid(*args):
            record = original(*args)
            return record.model_copy(update={"payload": None})

        monkeypatch.setattr(controller.engine, boundary, invalid)
    result = asyncio.run(controller.run(run.state.run_id))
    if boundary == "diagnose":
        blocked_by(result, "evidence_conflict")
    else:
        assert result["reason_code"] == "evidence_conflict"
    terminal(repo, run)


@pytest.mark.parametrize(
    "field", ["query_hash", "rows", "verification_status", "reason_code"]
)
def test_A07_terminal_projection_tampering_fails_closed(tmp_path, field):
    controller, repo, run, _ = setup(tmp_path)
    asyncio.run(controller.run(run.state.run_id))
    path = tmp_path / str(run.state.run_id) / "snapshot.json"
    content = json.loads(path.read_text())
    values = {
        "query_hash": "0" * 64,
        "rows": [],
        "verification_status": "not_run",
        "reason_code": "insufficient_evidence",
    }
    content["result"][field] = values[field]
    content["events"][-1]["payload"][field] = values[field]
    path.write_text(json.dumps(content))
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.load_run(run.state.run_id)


def test_A01_latest_same_query_report_requires_new_verification(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    controller._verify(controller.execute_report())
    controller.execute_report()
    controller.clock.advance(150)
    repo.acquire(run.state.run_id, uuid4())
    assert terminal(repo, run).result["verification_status"] == "not_run"


def test_A10_same_epoch_uses_monotonic_despite_wall_clock_change(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    controller.clock.origin += timedelta(days=1)
    assert repo.acquire(run.state.run_id, uuid4()) is None
    assert repo.load_run(run.state.run_id).state.status == "running"
    controller.clock.origin -= timedelta(days=2)
    controller.clock.advance(150)
    repo.acquire(run.state.run_id, uuid4())
    assert terminal(repo, run).state.reason_code == Reason.LEASE_EXPIRED


def test_A06_root_swap_during_atomic_write_stays_anchored(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    controller, repo, run, _ = setup(root)
    controller.begin(run.state.run_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = os.replace

    def swap(*args, **kwargs):
        root.rename(tmp_path / "original")
        root.symlink_to(outside, target_is_directory=True)
        return original(*args, **kwargs)

    monkeypatch.setattr("unstuck.storage.os.replace", swap)
    updated = repo.record(controller.current, controller.owner, "audit", {})
    assert updated.events[-1].type == "audit"
    assert list(outside.iterdir()) == []
    assert (
        json.loads(
            (
                tmp_path / "original" / str(run.state.run_id) / "snapshot.json"
            ).read_text()
        )["events"][-1]["type"]
        == "audit"
    )


def test_R05_interruption_after_acceptance_before_probe(tmp_path, monkeypatch):
    controller, repo, run, decider = setup(tmp_path)
    controller.begin(run.state.run_id)
    controller._verify(controller.execute_report())
    original = controller._event

    def interrupted(name, payload, **kwargs):
        original(name, payload, **kwargs)
        if name == "direct_path_selected":
            raise RuntimeFault(Reason.TOOL_ERROR)

    monkeypatch.setattr(controller, "_event", interrupted)
    with pytest.raises(RuntimeFault):
        asyncio.run(controller._select())
    controller.clock.advance(150)
    repo.acquire(run.state.run_id, uuid4())
    final = terminal(repo, run)
    assert final.result["final_choice"] == "inspect_schema_keys"
    assert final.incident.counters.diagnostic_reads == 0
    assert len(decider.calls) == 1


def test_A09_deadline_during_final_storage_check_stops(tmp_path, monkeypatch):
    controller, repo, run, _ = setup(tmp_path)
    controller.settings = Settings(run_seconds=1.0)
    original = repo._check_writer

    def delayed(actual, proposed, owner):
        original(actual, proposed, owner)
        if proposed.verification and proposed.query.join_key_mode != "literal":
            controller.clock.advance(1)

    monkeypatch.setattr(repo, "_check_writer", delayed)
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == "run_deadline"
    assert terminal(repo, run).state.status == "budget_exhausted"


def test_A01_terminal_identifies_exact_verified_report(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    result = asyncio.run(controller.run(run.state.run_id))
    final = terminal(repo, run)
    binding = final.verification
    assert result["report_evidence_id"] == str(binding.report_id)
    assert result["verification_id"] == str(binding.result.verification_id)
    report = next(
        record for record in final.evidence if record.evidence_id == binding.report_id
    )
    assert result["rows"] == [
        row.model_dump(mode="json") for row in report.payload.rows
    ]
    assert result["query_hash"] == report.arguments_hash == binding.query_hash
    assert (
        result["dataset_version"] == report.dataset_version == binding.dataset_version
    )


def test_A07_verification_cannot_reference_other_evidence(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    asyncio.run(controller.run(run.state.run_id))
    path = tmp_path / str(run.state.run_id) / "snapshot.json"
    content = json.loads(path.read_text())
    content["verification"]["result"]["evidence_ids"] = [str(uuid4())]
    path.write_text(json.dumps(content))
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.load_run(run.state.run_id)
