import asyncio
import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from test_controller import replace, setup
from test_review_repair_02 import MARKER, terminal

from unstuck.decision import SelectionAuthority
from unstuck.domain import (
    Reason,
    ReportVerification,
    Row,
    RuntimeFault,
    SourceRow,
    VerificationResult,
)
from unstuck.events import Redactor
from unstuck.storage import LocalRunRepository
from unstuck.verification import verify

SURFACES = [
    ("query.task.dataset_id", "identifier"),
    ("query.task.user_goal", "text"),
    ("query.task.country_codes.0", "country"),
    ("query.dataset_version", "identifier"),
    ("incident.incident_id", "uuid"),
    ("incident.run_id", "uuid"),
    ("incident.fingerprint", "digest"),
    ("incident.task_hash", "digest"),
    ("incident.base_query_hash", "digest"),
    ("incident.used_probes.0", "identifier"),
    ("selection.decision_id", "uuid"),
    ("selection.selected_id", "identifier"),
    ("selection.model_version", "identifier"),
    ("verification.report_id", "uuid"),
    ("verification.dataset_id", "identifier"),
    ("verification.dataset_version", "identifier"),
    ("verification.query_hash", "digest"),
    ("verification.result.verification_id", "uuid"),
    ("verification.result.evidence_ids.0", "uuid"),
    ("evidence.0.evidence_id", "uuid"),
    ("evidence.0.tool_id", "identifier"),
    ("evidence.0.arguments_hash", "digest"),
    ("evidence.0.dataset_id", "identifier"),
    ("evidence.0.dataset_version", "identifier"),
    ("evidence.0.payload.dataset_version", "identifier"),
    ("evidence.0.payload.query_hash", "digest"),
    ("evidence.0.payload.rows.0.country_code", "country"),
    ("evidence.0.payload.source_rows.0.country_code", "country"),
    ("evidence.0.payload.source_rows.0.order_id", "identifier"),
    ("schema.payload.orders_examples.0", "text"),
    ("schema.payload.countries_examples.0", "text"),
    ("schema.payload.dataset_version", "identifier"),
    ("pipeline.payload.dataset_version", "identifier"),
    ("scope.payload.dataset_version", "identifier"),
    ("event_type", "identifier"),
    ("event_model_version", "identifier"),
    ("artifact_name", "identifier"),
]


def changed_path(value, path, replacement):
    head, *tail = path
    if isinstance(value, tuple):
        items = list(value)
        items[int(head)] = (
            changed_path(items[int(head)], tail, replacement) if tail else replacement
        )
        return tuple(items)
    original = getattr(value, head)
    changed = changed_path(original, tail, replacement) if tail else replacement
    changes = {head: changed}
    if head == "payload" and hasattr(changed, "dataset_version"):
        changes["dataset_version"] = changed.dataset_version
    elif head == "dataset_version" and hasattr(value, "payload"):
        changes["payload"] = replace(value.payload, dataset_version=changed)
    return replace(value, **changes)


def authority_inputs(controller):
    current = controller.current
    report = controller.engine.report(current.query, current.state.task_hash)
    source = SourceRow(
        order_id="order-safe",
        order_date_utc=current.query.task.order_date_utc,
        country_code="US",
        units=1,
    )
    report = replace(
        report,
        payload=replace(
            report.payload,
            rows=(Row(country_code="US", units=1),),
            source_rows=(source,),
        ),
    )
    return {
        "query": replace(
            current.query, task=replace(current.query.task, country_codes=("US",))
        ),
        "incident": replace(current.incident, used_probes=("probe-safe",)),
        "selection": SelectionAuthority(
            decision_id=uuid4(),
            selected_id="probe-safe",
            confidence=0.95,
            model_version="model-safe",
            accepted=True,
        ),
        "verification": ReportVerification(
            report_id=report.evidence_id,
            dataset_id=report.dataset_id,
            dataset_version=report.dataset_version,
            query_hash=current.query.query_hash,
            result=VerificationResult(
                status="passed",
                reason_code="verified",
                evidence_ids=(report.evidence_id,),
            ),
        ),
        "evidence": (report,),
        "schema": controller.engine.diagnose("inspect_schema_keys", current.query),
        "pipeline": controller.engine.diagnose("inspect_pipeline", current.query),
        "scope": controller.engine.diagnose("inspect_scope", current.query),
    }


def assert_clean_final(
    repo, current, owner, marker, path, before, exported, before_export
):
    assert path.read_bytes() == before
    assert exported.read_bytes() == before_export
    assert repo.load_run(current.state.run_id) == current
    assert repo.export_events(current.state.run_id).read_bytes() == before_export
    final = repo.finish(current, owner, Reason.INSUFFICIENT, {})
    assert marker not in json.dumps(final.result)
    assert marker.encode() not in path.read_bytes()
    assert marker.encode() not in repo.export_events(current.state.run_id).read_bytes()
    terminal(repo, current)


@pytest.mark.parametrize("surface,shape", SURFACES, ids=[item[0] for item in SURFACES])
@pytest.mark.parametrize("configured", [False, True], ids=["access-key", "configured"])
def test_boundary_record_replacement_matrix(
    tmp_path, monkeypatch, surface, shape, configured
):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    inputs = authority_inputs(controller)
    marker = (
        {
            "uuid": "f00dbabe-1234-4567-89ab-0123456789ab",
            "digest": "f00dbabe" * 8,
            "country": "ZZ",
        }.get(shape, "synthetic-private-identity")
        if configured
        else MARKER
    )
    repo.redactor = Redactor(values=(marker,) if configured else ())
    current = controller.current
    path = tmp_path / str(run.state.run_id) / "snapshot.json"
    before = path.read_bytes()
    exported = repo.export_events(run.state.run_id)
    before_export = exported.read_bytes()
    writes = []
    original_atomic = repo._atomic

    def capture(path, text):
        writes.append(text)
        return original_atomic(path, text)

    monkeypatch.setattr(repo, "_atomic", capture)

    def attempt():
        if surface == "event_type":
            return repo.record(current, controller.owner, marker, {})
        if surface == "event_model_version":
            return repo.record(
                current, controller.owner, "audit", {"model_version": marker}
            )
        if surface == "artifact_name":
            return repo.save_artifact(current, controller.owner, marker + ".json", {})
        root, *path_parts = surface.split(".")
        value = UUID(marker) if shape == "uuid" and configured else marker
        replacement = changed_path(inputs[root], path_parts, value)
        if root in {"schema", "pipeline", "scope"}:
            root, replacement = "evidence", (replacement,)
        return repo.record(
            current, controller.owner, "audit", {}, **{root: replacement}
        )

    expected = (
        ValidationError
        if not configured and shape in {"uuid", "digest", "country"}
        else RuntimeFault
    )
    with pytest.raises(expected):
        attempt()
    assert writes == []
    assert_clean_final(
        repo, current, controller.owner, marker, path, before, exported, before_export
    )


@pytest.mark.parametrize("surface", ["verification_id", "evidence_ids"])
@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("repository_only", [False, True])
def test_boundary_verifier_ingestion(
    tmp_path, monkeypatch, surface, recovery, repository_only
):
    marker = "f00dbabe-1234-4567-89ab-0123456789ab"
    controller, repo, run, decider = setup(tmp_path)
    repo.redactor = Redactor(values=(marker,))
    if not repository_only:
        controller.redactor = repo.redactor
    original_record = repo.record
    observed = []

    def checked_record(snapshot, owner, event_type, payload, **changes):
        if event_type == "verification_finished":
            path = tmp_path / str(run.state.run_id) / "snapshot.json"
            before = path.read_bytes()
            with pytest.raises(RuntimeFault) as failure:
                original_record(snapshot, owner, event_type, payload, **changes)
            assert path.read_bytes() == before
            observed.append(event_type)
            raise failure.value
        return original_record(snapshot, owner, event_type, payload, **changes)

    def unsafe_verify(*args, **kwargs):
        result = verify(*args, **kwargs)
        value = UUID(marker)
        return replace(
            result, **{surface: (value,) if surface == "evidence_ids" else value}
        )

    monkeypatch.setattr("unstuck.controller.verify", unsafe_verify)
    monkeypatch.setattr(repo, "record", checked_record)
    if recovery:
        controller.begin(run.state.run_id)
        with pytest.raises(RuntimeFault):
            controller._verify(controller.execute_report())
        controller.clock.advance(150)
        repo.acquire(run.state.run_id, uuid4())
    else:
        result = asyncio.run(controller.run(run.state.run_id))
        assert result["reason_code"] == "evidence_conflict"
    assert bool(observed) == repository_only
    final = terminal(repo, run)
    assert final.verification is None
    assert not decider.calls
    assert marker not in final.model_dump_json()
    assert marker not in json.dumps(final.result)
    assert marker not in repo.export_events(run.state.run_id).read_text()


@pytest.mark.parametrize("recovery", [False, True])
def test_boundary_protocol_literals_and_safe_identifiers(tmp_path, recovery):
    controller, repo, run, _ = setup(tmp_path)
    literals = (
        "passed",
        "verified",
        "literal",
        "strip_country_prefix",
        "offline",
        "mock",
        "DONE",
        "VERIFY",
        "valid",
        "country_prefix",
        "populated_after_repair",
        "lease_expired_incomplete",
    )
    repo.redactor = controller.redactor = Redactor(values=literals)
    if recovery:
        controller.begin(run.state.run_id)
        controller._verify(controller.execute_report())
        asyncio.run(controller._select())
        controller.clock.advance(150)
        repo.acquire(run.state.run_id, uuid4())
    else:
        result = asyncio.run(controller.run(run.state.run_id))
        assert result["status"] == "completed"
    final = terminal(repo, run)
    assert final.result["verification_status"] == "passed"
    assert final.result["final_choice"] == "inspect_schema_keys"
    assert final.result["model_version"] == "scripted-v1"
    assert any(event.payload.get("status") == "[REDACTED]" for event in final.events)
    assert (
        json.loads(repo.export_events(run.state.run_id).read_text().splitlines()[-1])[
            "payload"
        ]
        == final.result
    )


def test_boundary_protocol_word_in_identity_is_not_exempt(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    repo.redactor = Redactor(values=("passed",))
    controller.begin(run.state.run_id)
    with pytest.raises(RuntimeFault):
        repo.record(
            controller.current,
            controller.owner,
            "audit",
            {},
            incident=replace(controller.current.incident, used_probes=("passed",)),
        )


def test_boundary_protocol_literals_in_initial_evidence(tmp_path):
    controller, repo, run, decider = setup(tmp_path)
    controller.initial_evidence = (
        controller.engine.diagnose("inspect_schema_keys", run.query),
    )
    repo.redactor = controller.redactor = Redactor(values=("literal", "country_prefix"))
    result = asyncio.run(controller.run(run.state.run_id))
    # A protocol literal is never redacted into unsafety: the run reaches a
    # terminal result and persists it. The read repeats facts the initial
    # evidence already proved, so it admits nothing and the one second
    # decision this selector cannot answer ends the run.
    assert result["status"] == "unresolved"
    assert result["blocked_reason_code"] == "insufficient_evidence"
    assert len(decider.calls) == 2
    terminal(repo, run)


@pytest.mark.parametrize("surface", ["dataset_id", "user_goal", "dataset_version"])
@pytest.mark.parametrize("configured", [False, True])
def test_boundary_create_ingestion(tmp_path, surface, configured):
    controller, _, run, _ = setup(tmp_path / "seed")
    marker = "synthetic-private-identity" if configured else MARKER
    root = tmp_path / "new"
    repo = LocalRunRepository(
        root, redactor=Redactor(values=(marker,) if configured else ())
    )
    task, version = run.state.task, run.state.dataset_version
    if surface == "dataset_version":
        version = marker
    else:
        task = replace(task, **{surface: marker})
    with pytest.raises(RuntimeFault, match="task_mismatch"):
        repo.create_run(task, version, "new-run")
    assert not list(root.rglob("snapshot.json"))
    controller.engine.cancel()


def test_boundary_acquire_owner(tmp_path):
    _, repo, run, _ = setup(tmp_path)
    marker = "f00dbabe-1234-4567-89ab-0123456789ab"
    repo.redactor = Redactor(values=(marker,))
    path = tmp_path / str(run.state.run_id) / "snapshot.json"
    before = path.read_bytes()
    with pytest.raises(RuntimeFault):
        repo.acquire(run.state.run_id, UUID(marker))
    assert path.read_bytes() == before
    assert marker not in repo.export_events(run.state.run_id).read_text()


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("recovery", [False, True])
def test_boundary_adapter_report_id(tmp_path, monkeypatch, configured, recovery):
    marker = "f00dbabe-1234-4567-89ab-0123456789ab" if configured else MARKER
    controller, repo, run, decider = setup(tmp_path)
    repo.redactor = controller.redactor = Redactor(
        values=(marker,) if configured else ()
    )
    original = controller.engine.report

    def unsafe_report(*args):
        value = UUID(marker) if configured else marker
        return original(*args).model_copy(update={"evidence_id": value})

    monkeypatch.setattr(controller.engine, "report", unsafe_report)
    if recovery:
        controller.begin(run.state.run_id)
        with pytest.raises(RuntimeFault, match="evidence_conflict"):
            controller.execute_report()
        controller.clock.advance(150)
        repo.acquire(run.state.run_id, uuid4())
    else:
        result = asyncio.run(controller.run(run.state.run_id))
        assert result["reason_code"] == "evidence_conflict"
    final = terminal(repo, run)
    assert not final.evidence and final.verification is None
    assert not decider.calls
    assert marker not in final.model_dump_json()
    assert marker not in repo.export_events(run.state.run_id).read_text()


@pytest.mark.parametrize(
    "field",
    [
        "verification_id",
        "report_evidence_id",
        "evidence_ids",
        "first_choice",
        "final_choice",
        "model_version",
        "dataset_version",
        "query_hash",
        "task_hash",
        "base_query_hash",
        "artifact_refs",
    ],
)
def test_boundary_finish_replacement(tmp_path, field):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    controller._verify(controller.execute_report())
    current = controller.current
    marker = (
        "f00dbabe-1234-4567-89ab-0123456789ab"
        if "id" in field
        else "f00dbabe" * 8
        if "hash" in field
        else "synthetic-private-identity"
    )
    repo.redactor = Redactor(values=(marker,))
    value = UUID(marker) if "id" in field else marker
    if field in {"evidence_ids", "artifact_refs"}:
        value = [value]
    changes = {field: value}
    if field == "report_evidence_id":
        changes["evidence_ids"] = [value]
    elif field == "evidence_ids":
        changes["evidence_ids"] = [current.verification.report_id, *value]
    path = tmp_path / str(run.state.run_id) / "snapshot.json"
    before = path.read_bytes()
    exported = repo.export_events(run.state.run_id)
    before_export = exported.read_bytes()
    with pytest.raises(RuntimeFault):
        repo.finish(current, controller.owner, Reason.INSUFFICIENT, changes)
    assert_clean_final(
        repo, current, controller.owner, marker, path, before, exported, before_export
    )


def test_boundary_new_model_fields_are_checked_by_default(tmp_path):
    from unstuck.domain import Contract
    from unstuck.events import PersistenceBoundary

    class ExtendedAuthority(Contract):
        future_identity: str
        nested_identities: tuple[str, ...]

    boundary = PersistenceBoundary(Redactor(values=("synthetic-private-identity",)))
    for marker in (MARKER, "synthetic-private-identity"):
        for fields in (
            {"future_identity": marker, "nested_identities": ()},
            {"future_identity": "safe", "nested_identities": (marker,)},
        ):
            with pytest.raises(RuntimeFault):
                boundary.validate(ExtendedAuthority(**fields))


def test_boundary_safe_replacements_persist_without_rewriting(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    current = controller.current
    inputs = authority_inputs(controller)
    selection = inputs["selection"]
    incident = inputs["incident"]
    current = repo.record(
        current,
        controller.owner,
        "Audit_Custom-42",
        {"note": MARKER},
        selection=selection,
        incident=incident,
    )
    assert current.selection == selection and current.incident == incident
    current = repo.save_artifact(
        current, controller.owner, "audit-safe.json", {"note": MARKER}
    )
    final = repo.finish(current, controller.owner, Reason.INSUFFICIENT, {})
    assert final.result["final_choice"] == "probe-safe"
    assert "audit-safe.json" in final.result["artifact_refs"]
    assert MARKER not in repo.export_events(run.state.run_id).read_text()
    assert final.artifacts["audit-safe.json"] == {"note": "[REDACTED]"}
    terminal(repo, run)


@pytest.mark.parametrize(
    "field", ["run_id", "lease_owner", "lease_clock_epoch", "dataset_version"]
)
def test_boundary_replaced_snapshot_input(tmp_path, field):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    current = controller.current
    marker = "f00dbabe-1234-4567-89ab-0123456789ab"
    repo.redactor = Redactor(values=(marker,))
    value = marker if field == "dataset_version" else UUID(marker)
    supplied = current.model_copy(
        update={"state": current.state.model_copy(update={field: value})}
    )
    path = tmp_path / str(run.state.run_id) / "snapshot.json"
    before = path.read_bytes()
    exported = repo.export_events(run.state.run_id)
    before_export = exported.read_bytes()
    with pytest.raises(RuntimeFault):
        repo.record(supplied, controller.owner, "audit", {})
    assert_clean_final(
        repo, current, controller.owner, marker, path, before, exported, before_export
    )


@pytest.mark.parametrize("surface", ["incident", "verification", "event", "artifact"])
def test_boundary_load_rejects_unsafe_authority(tmp_path, surface):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    controller._verify(controller.execute_report())
    path = tmp_path / str(run.state.run_id) / "snapshot.json"
    contents = json.loads(path.read_text())
    marker = (
        "f00dbabe-1234-4567-89ab-0123456789ab"
        if surface == "verification"
        else "synthetic-private-identity"
    )
    if surface == "incident":
        contents["incident"]["used_probes"] = [marker]
    elif surface == "verification":
        contents["verification"]["result"]["verification_id"] = marker
    elif surface == "event":
        contents["events"][0]["type"] = marker
    else:
        contents["artifacts"][marker + ".json"] = {}
    path.write_text(json.dumps(contents))
    before = path.read_bytes()
    repo.redactor = Redactor(values=(marker,))
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.load_run(run.state.run_id)
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.export_events(run.state.run_id)
    controller.clock.advance(150)
    with pytest.raises(RuntimeFault, match="storage_error"):
        repo.acquire(run.state.run_id, uuid4())
    assert path.read_bytes() == before
    assert not (path.parent / "events.jsonl").exists()
