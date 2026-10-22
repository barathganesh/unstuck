import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_controller import blocked_by, replace, setup
from test_query import setup_engine

from unstuck.clock import FakeClock
from unstuck.decision import (
    Decision,
    DecisionState,
    OptionProbability,
    Probe,
    PublicEvidence,
    accepted,
    default_catalog,
)
from unstuck.domain import (
    Counters,
    Incident,
    Reason,
    RuntimeFault,
    TaskSpec,
)
from unstuck.events import Measurement, Redactor, project_events
from unstuck.fixtures import load_fixture
from unstuck.policy import compatible, consume, current_evidence, validate_repair
from unstuck.storage import LocalRunRepository
from unstuck.verification import verify


@pytest.mark.parametrize(
    "change",
    [
        "missing_probs",
        "wrong_ids",
        "bad_sum",
        "missing_confidence",
        "invalid",
        "review",
    ],
)
def test_TST_05_decision_numeric_validation(change):
    engine, query = setup_engine()
    state = DecisionState(
        task=query.task,
        query=query,
        task_hash=query.task_hash,
        query_hash=query.query_hash,
        dataset_version=query.dataset_version,
        candidates=(default_catalog()[0].public,),
        budgets=Counters(),
        remaining_ms=120000,
    )
    ids = ("inspect_pipeline", "request_review")
    decision = Decision(
        candidate_ids=ids,
        selected_id=ids[0],
        confidence=0.8,
        validation_status="valid",
        probabilities=(
            OptionProbability(id=ids[0], probability=1.0),
            OptionProbability(id=ids[1], probability=0.0),
        ),
    )
    assert accepted(decision, state, 0.8)
    changes = {
        "missing_probs": {"probabilities": None},
        "wrong_ids": {
            "probabilities": (OptionProbability(id="wrong", probability=1.0),)
        },
        "bad_sum": {
            "probabilities": tuple(
                replace(item, probability=0.2) for item in decision.probabilities
            )
        },
        "missing_confidence": {"confidence": None},
        "invalid": {"validation_status": "invalid"},
        "review": {"selected_id": "request_review"},
    }
    assert not accepted(replace(decision, **changes[change]), state, 0.8)


@pytest.mark.parametrize(
    "schema_changes,mode",
    [
        ({"metadata_complete": False}, "literal"),
        ({"scope_covered": False}, "literal"),
        ({"orders_key_encoding": "unknown"}, "literal"),
        ({"approved_join_modes": ()}, "literal"),
        ({}, "literal"),
        ({}, "strip_country_prefix"),
        ({"countries_key_encoding": "plain"}, "literal"),
    ],
)
def test_TST_08_encoding_compatibility_guards(schema_changes, mode):
    engine, query = setup_engine()
    schema = replace(
        engine.diagnose("inspect_schema_keys", query).payload, **schema_changes
    )
    query = replace(query, join_key_mode=mode)
    expected = mode == "strip_country_prefix" or schema_changes == {
        "countries_key_encoding": "plain"
    }
    assert compatible(query, schema) == expected


def test_TST_21_remaining_repair_identity_guards():
    engine, query = setup_engine()
    proof = engine.diagnose("inspect_schema_keys", query)
    incident = Incident.open(uuid4(), query)
    now = datetime.now(UTC)
    assert current_evidence(query, proof, now, allow_base=True)
    for modified_incident, evidence, version in (
        (
            replace(incident, counters=Counters(repair_attempts=1)),
            proof,
            query.dataset_version,
        ),
        (incident, engine.diagnose("inspect_pipeline", query), query.dataset_version),
        (incident, proof, "other"),
    ):
        with pytest.raises(RuntimeFault):
            validate_repair(query, modified_incident, evidence, version, now)
    with pytest.raises(RuntimeFault):
        consume(Counters(), "renamed_tool")


def test_TST_08_verifier_source_scope_and_type_checks():
    engine, query = setup_engine()
    proof = engine.diagnose("inspect_schema_keys", query)
    assert verify(query, proof).reason_code == "source_mismatch"
    repaired = validate_repair(
        query,
        Incident.open(uuid4(), query),
        proof,
        query.dataset_version,
        datetime.now(UTC),
    )
    report = engine.report(repaired, query.task_hash)
    changed = replace(repaired, task=replace(repaired.task, country_codes=("DE",)))
    report = replace(
        report,
        arguments_hash=changed.query_hash,
        payload=replace(report.payload, query_hash=changed.query_hash),
    )
    changed_proof = replace(proof, arguments_hash=changed.base_query_hash)
    assert verify(changed, report, (changed_proof,)).reason_code == "scope_mismatch"


@pytest.mark.parametrize(
    "condition",
    [
        "no_candidates",
        "changed_candidates",
        "oversized",
        "tool_timeout",
        "report_timeout",
        "disk",
    ],
)
def test_TST_27_execution_boundary_failures(tmp_path, monkeypatch, condition):
    clock = FakeClock()
    controller, repo, run, decider = setup(tmp_path, clock=clock)
    if condition == "no_candidates":
        controller.catalog = ()
    elif condition == "changed_candidates":
        decider.hook = lambda state: setattr(controller, "catalog", ())
    elif condition == "oversized":
        original = controller._decision_state

        def oversized(candidates):
            state = original(candidates)
            return replace(state, bounded_evidence=state.bounded_evidence * 5)

        monkeypatch.setattr(controller, "_decision_state", oversized)
    elif condition == "tool_timeout":
        original = controller.engine.diagnose

        def slow(*args):
            clock.advance(20)
            return original(*args)

        monkeypatch.setattr(controller.engine, "diagnose", slow)
    elif condition == "report_timeout":
        original = controller.engine.report

        def slow(*args):
            clock.advance(20)
            return original(*args)

        monkeypatch.setattr(controller.engine, "report", slow)
    else:
        monkeypatch.setattr(
            repo,
            "record",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeFault(Reason.STORAGE_ERROR)
            ),
        )
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["status"] in {"unresolved", "failed"}
    assert repo.load_run(run.state.run_id).incident.counters.repair_attempts == 0


def test_TST_27_a_mutated_selector_input_never_reaches_the_authority(tmp_path):
    """What a selector is handed is a projection, not the authority itself.

    The selector receives a bounded projection of the current state. Mutating
    it cannot change what the controller holds, what the decision context
    identifies, or what the run goes on to execute.
    """
    controller, repo, run, decider = setup(tmp_path)
    decider.hook = lambda state: state.bounded_evidence[0].facts.update(
        {"row_count": 99}
    )
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    assert result["status"] == "completed"
    assert "99" not in str(
        [
            record.payload.model_dump(mode="json")
            for record in final.evidence
            if record.kind == "query"
        ]
    )
    assert "row_count" not in final.model_dump_json()
    assert final.incident.counters.decider_calls == 1


def test_TST_13_secrets_rejected_in_immutable_task(tmp_path):
    repo = LocalRunRepository(
        tmp_path, redactor=Redactor(values=("synthetic-sensitive-value",))
    )
    fixture = load_fixture()
    task = TaskSpec(
        dataset_id=fixture.dataset_id,
        order_date_utc=fixture.order_date_utc,
        user_goal="synthetic-sensitive-value",
    )
    with pytest.raises(RuntimeFault, match="task_mismatch"):
        repo.create_run(task, fixture.dataset_version, "key")
    assert project_events(()) is None
    assert Redactor().clean(None) is None
    assert (
        Redactor().object({"token_usage": Measurement().model_dump(mode="json")})[
            "token_usage"
        ]
        is not None
    )


@pytest.mark.parametrize("reason", list(Reason))
def test_TST_36_each_reason_exactly_one_final_event(tmp_path, reason):
    fixture = load_fixture()
    if reason.value.startswith(("populated_", "empty_")):
        fixture = replace(
            fixture,
            orders=() if reason.value.startswith("empty_") else fixture.orders,
            countries=tuple(
                replace(country, country_id=country.country_code)
                for country in fixture.countries
            ),
            countries_key_encoding="plain",
        )
    controller, repo, run, _ = setup(tmp_path, fixture)
    controller.begin(run.state.run_id)
    if reason.value.startswith(("populated_", "empty_")):
        controller._verify(controller.execute_report())
    result = controller._finish(reason)
    events = repo.list_events(run.state.run_id)
    assert result["reason_code"] == reason.value
    assert len([event for event in events if event.type == "run_finished"]) == 1
    assert events[-1].payload == result
    if result["status"] == "budget_exhausted":
        assert events[-2].type == "budget_stop"


@pytest.mark.parametrize("origin", ["initial", "diagnostic"])
@pytest.mark.parametrize("field", ["orders_examples", "countries_examples", "tool_id"])
def test_R02_unsafe_evidence_never_persisted(tmp_path, monkeypatch, origin, field):
    controller, repo, run, decider = setup(tmp_path)
    unsafe = controller.engine.diagnose("inspect_schema_keys", run.query)
    sentinel = "synthetic-review-secret"
    if field == "tool_id":
        sentinel = "AKIA" + "A" * 16
        unsafe = replace(unsafe, tool_id=sentinel)
    else:
        unsafe = replace(
            unsafe, payload=replace(unsafe.payload, **{field: ("api_key=" + sentinel,)})
        )
    before = unsafe.model_dump_json()
    writes = []
    original = repo._atomic

    def capture(path, text):
        writes.append(text)
        return original(path, text)

    monkeypatch.setattr(repo, "_atomic", capture)
    if origin == "initial":
        controller.initial_evidence = (unsafe,)
    else:
        monkeypatch.setattr(controller.engine, "diagnose", lambda *args: unsafe)
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    exported = repo.export_events(run.state.run_id).read_text()
    for text in [*writes, final.model_dump_json(), exported]:
        assert sentinel not in text
    assert unsafe.model_dump_json() == before
    assert unsafe.evidence_id not in {record.evidence_id for record in final.evidence}
    assert result["status"] == "unresolved"
    if origin == "diagnostic":
        # The refused read is invalid evidence, which opens the one second
        # decision this fixed-choice selector cannot answer.
        blocked_by(result, "evidence_conflict")
    else:
        assert result["reason_code"] == "evidence_conflict"
    assert final.incident.counters.repair_attempts == 0
    assert final.incident.counters.report_queries == 1
    assert len(decider.calls) == (2 if origin == "diagnostic" else 0)
    assert sum(event.type == "run_finished" for event in final.events) == 1


@pytest.mark.parametrize(
    "surface",
    [
        "task",
        "query",
        "candidate_description",
        "candidate_id",
        "candidate_facts",
        "evidence_tool",
        "evidence_version",
        "facts",
    ],
)
def test_R03_complete_decision_state_rejects_unsafe_content(surface):
    engine, query = setup_engine()
    proof = engine.diagnose("inspect_scope", query)
    public = PublicEvidence(
        evidence_id=proof.evidence_id,
        kind=proof.kind,
        tool_id=proof.tool_id,
        dataset_version=proof.dataset_version,
        arguments_hash=proof.arguments_hash,
        observed_at=proof.observed_at.isoformat(),
        facts=proof.payload.model_dump(mode="json"),
    )
    values = dict(
        task=query.task,
        query=query,
        task_hash=query.task_hash,
        query_hash=query.query_hash,
        dataset_version=query.dataset_version,
        candidates=(default_catalog()[0].public,),
        budgets=Counters(),
        remaining_ms=120000,
        bounded_evidence=(public,),
    )
    if surface == "task":
        values["task"] = replace(
            query.task, user_goal="expected_rows=synthetic-evaluator-marker"
        )
    elif surface == "query":
        values["query"] = replace(
            query,
            task=replace(query.task, user_goal="hidden_category=synthetic-marker"),
        )
    elif surface.startswith("candidate"):
        changes = {
            "candidate_description": {"description": "api_key=synthetic-review-secret"},
            "candidate_id": {"id": "expected_action"},
            "candidate_facts": {"observable_facts": ("evaluator_truth",)},
        }
        values["candidates"] = (replace(values["candidates"][0], **changes[surface]),)
    else:
        changes = {
            "evidence_tool": {"tool_id": "hidden_category"},
            "evidence_version": {"dataset_version": "expected_rows"},
            "facts": {
                "facts": {"note": "Authorization: Bearer synthetic-review-secret"}
            },
        }
        values["bounded_evidence"] = (replace(public, **changes[surface]),)
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DecisionState(**values)


@pytest.mark.parametrize(
    "surface", ["task", "candidate", "evidence_metadata", "facts", "configured_secret"]
)
def test_R03_unsafe_request_never_reaches_adapter(tmp_path, surface):
    fixture = load_fixture()
    controller, repo, _, decider = setup(tmp_path)
    task = TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )
    if surface == "task":
        task = replace(task, user_goal="expected_rows=synthetic-evaluator-marker")
        with pytest.raises(RuntimeFault, match="task_mismatch"):
            repo.create_run(task, fixture.dataset_version, "unsafe-request")
        assert not decider.calls
        assert task.user_goal == "expected_rows=synthetic-evaluator-marker"
        return
    run = repo.create_run(task, fixture.dataset_version, "unsafe-request")
    if surface in {"candidate", "configured_secret"}:
        text = (
            "api_key=synthetic-review-secret"
            if surface == "candidate"
            else "synthetic-private-value"
        )
        if surface == "configured_secret":
            controller.redactor = Redactor(values=(text,))
        probe = controller.catalog[0]
        controller.catalog = (
            Probe(replace(probe.public, description=text), probe.capability),
            *controller.catalog[1:],
        )
    if surface in {"evidence_metadata", "facts"}:
        proof = controller.engine.diagnose("inspect_schema_keys", run.query)
        if surface == "evidence_metadata":
            proof = replace(proof, tool_id="evaluator_truth")
        else:
            proof = replace(
                proof,
                payload=replace(
                    proof.payload, orders_examples=("expected_action=synthetic-marker",)
                ),
            )
        controller.initial_evidence = (proof,)
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    assert not decider.calls
    assert result["status"] == "unresolved"
    assert final.state.task == task and final.state.task_hash == task.task_hash
    assert final.incident.counters.decider_calls == 0
    assert final.incident.counters.diagnostic_reads == 0
    assert final.incident.counters.report_queries == 1
    assert sum(event.type == "run_finished" for event in final.events) == 1
