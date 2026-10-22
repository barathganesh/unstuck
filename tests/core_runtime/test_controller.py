import asyncio
from datetime import timedelta

import pytest

from unstuck.clock import FakeClock
from unstuck.controller import Controller
from unstuck.decision import Decision, OptionProbability, default_catalog
from unstuck.domain import Counters, Reason, RuntimeFault, TaskSpec
from unstuck.fixtures import load_fixture
from unstuck.policy import consume
from unstuck.query import EvidenceEngine
from unstuck.storage import LocalRunRepository
from unstuck.tools import ReportTool


def replace(model, **changes):
    return type(model).model_validate(model.model_dump() | changes)


class FakeDecider:
    def __init__(
        self, choice="inspect_schema_keys", confidence=0.95, hook=None, error=False
    ):
        self.choice = choice
        self.confidence = confidence
        self.hook = hook
        self.error = error
        self.calls = []

    async def select(self, state):
        self.calls.append(state)
        if self.hook:
            self.hook(state)
        if self.error:
            raise TimeoutError("synthetic provider failure")
        ids = tuple(probe.id for probe in state.candidates) + ("request_review",)
        return Decision(
            candidate_ids=ids,
            selected_id=self.choice,
            confidence=self.confidence,
            probabilities=tuple(
                OptionProbability(
                    id=identifier, probability=1.0 if identifier == self.choice else 0.0
                )
                for identifier in ids
            ),
            validation_status="valid" if self.confidence is not None else "invalid",
        )


def blocked_by(result, reason, tool="inspect_schema_keys"):
    """Assert the run stopped after one second decision it could not answer.

    A read that failed or made no progress opens exactly one same-call second
    decision. These selectors answer with one fixed choice, which the second
    decision can no longer have, so the run ends unresolved while the read's
    own verified reason is preserved as the blocked reason.
    """
    assert result["reason_code"] == "final_selection_rejected"
    assert result["blocked_reason_code"] == reason
    assert result["blocked_tool_id"] == tool
    assert result["pass_count"] == 2


def setup(
    tmp_path,
    fixture=None,
    decider=None,
    clock=None,
    catalog=None,
    key="controller-key",
    **changes,
):
    """Compose one controller over the real repository and orchestrator.

    Extra keyword arguments reach the controller, so a test can share one
    receipt store across ordered calls or select the memory arm.
    """
    fixture = fixture or load_fixture()
    clock = clock or FakeClock()
    repo = LocalRunRepository(tmp_path, clock=clock)
    task = TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )
    run = repo.create_run(task, fixture.dataset_version, key)
    decider = decider or FakeDecider()
    engine = EvidenceEngine(fixture)
    controller = Controller(
        repo,
        engine,
        decider,
        clock=clock,
        catalog=catalog or default_catalog(),
        **changes,
    )
    return controller, repo, run, decider


def test_TST_14_TST_08_direct_choice_causes_repair(tmp_path):
    controller, repo, run, decider = setup(tmp_path)
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    assert (
        result["status"] == "completed"
        and result["reason_code"] == "populated_after_repair"
    )
    assert len(decider.calls) == 1
    assert final.incident.counters == Counters(
        report_queries=2,
        diagnostic_reads=1,
        decider_calls=1,
        repair_attempts=1,
        agent_turns=1,
    )
    assert result["pass_count"] == 1 and result["selection_pass"] == 1
    assert result["history_status"] == "not_requested" and result["receipt_ids"] == []
    assert result["first_choice"] == result["final_choice"] == "inspect_schema_keys"
    assert final.query.task_hash == run.query.task_hash
    assert final.incident.fingerprint == run.incident.fingerprint
    types = [event.type for event in final.events]
    assert (
        types.index("decider_first_requested")
        < types.index("probe_started")
        < types.index("repair_applied")
    )
    assert types.count("run_finished") == 1
    assert "decider_final_requested" not in types
    assert result["cost"]["value"] is None


def test_TST_24_healthy_bypasses_selection(tmp_path):
    fixture = load_fixture()
    fixture = replace(
        fixture,
        countries=tuple(
            replace(country, country_id=country.country_code)
            for country in fixture.countries
        ),
        countries_key_encoding="plain",
    )
    controller, repo, run, decider = setup(tmp_path, fixture)
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == "populated_initial"
    assert not decider.calls
    assert repo.load_run(run.state.run_id).query.join_key_mode == "literal"


@pytest.mark.parametrize(
    "choice,confidence",
    [
        ("inspect_schema_keys", 0.799),
        ("request_review", 0.99),
        ("invented", 0.99),
        ("inspect_schema_keys", None),
    ],
)
def test_TST_05_one_pass_rejects_uncertain_selection(tmp_path, choice, confidence):
    controller, repo, run, decider = setup(
        tmp_path, decider=FakeDecider(choice, confidence)
    )
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == "first_selection_rejected"
    assert len(decider.calls) == 1
    assert result["pass_count"] == 1 and result["blocked_tool_id"] is None
    assert repo.load_run(run.state.run_id).incident.counters.diagnostic_reads == 0


@pytest.mark.parametrize(
    "choice", ["inspect_schema_keys", "inspect_scope", "inspect_pipeline"]
)
def test_TST_14_selection_wiring_and_no_post_tool_round(tmp_path, choice):
    controller, repo, run, decider = setup(tmp_path, decider=FakeDecider(choice, 0.80))
    result = asyncio.run(controller.run(run.state.run_id))
    events = repo.list_events(run.state.run_id)
    assert [
        event.payload["tool_id"] for event in events if event.type == "probe_started"
    ] == [choice]
    # One read, and no selection round after the tool or the repair. Only a
    # read whose admitted facts moved a supported disposition resolves on the
    # first decision. The other two admit canonical facts that open no gate, so
    # each still opens exactly one same-call second decision, which this
    # selector answers with the tool it can no longer have.
    productive = choice == "inspect_schema_keys"
    assert len(decider.calls) == (1 if productive else 2)
    assert result["status"] == ("completed" if productive else "unresolved")
    if not productive:
        # The unproductive read is carried into the second decision as its own
        # verifier-owned same-call history, labeled by what it actually was.
        assert result["blocked_tool_id"] == choice
        assert result["blocked_reason_code"] == "insufficient_evidence"
        receipts = [
            event.payload
            for event in events
            if event.type == "decision_receipt_written"
        ]
        assert [item["outcome_class"] for item in receipts] == ["disposition_unchanged"]
        # Eligible to refuse the identical repetition on a later call, and
        # still no authority over this one: the second decision is the
        # selector's own and the read it names was never replayed.
        assert all(item["blocking"] is True for item in receipts)


@pytest.mark.parametrize(
    "arrival,expected",
    [
        (None, "still_pending_after_wait"),
        (3.0, "populated_after_wait"),
        (4.0, "still_pending_after_wait"),
    ],
)
def test_TST_10_one_wait_then_readiness(tmp_path, arrival, expected):
    fixture = load_fixture()
    fixture = replace(
        fixture,
        job_state="pending",
        arrival_after_seconds=arrival,
        countries=tuple(
            replace(country, country_id=country.country_code)
            for country in fixture.countries
        ),
        countries_key_encoding="plain",
    )
    controller, repo, run, decider = setup(
        tmp_path, fixture, FakeDecider("inspect_pipeline")
    )
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == expected
    assert result["wait_checks"] == 1
    assert controller.clock.monotonic() == 3.0
    assert len(decider.calls) == 1


@pytest.mark.parametrize(
    "complete,expected", [(True, "empty_verified"), (False, "unresolved")]
)
def test_TST_11_empty_requires_complete_compatible_scope(tmp_path, complete, expected):
    fixture = load_fixture()
    fixture = replace(
        fixture,
        orders=(),
        metadata_complete=complete,
        countries_key_encoding="plain",
        countries=tuple(
            replace(country, country_id=country.country_code)
            for country in fixture.countries
        ),
    )
    controller, repo, run, _ = setup(tmp_path, fixture)
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["status"] == expected
    assert repo.load_run(run.state.run_id).incident.counters.repair_attempts == 0


def test_TST_09_provider_failure_counted_durably(tmp_path):
    controller, repo, run, decider = setup(tmp_path, decider=FakeDecider(error=True))

    def inspect_attempt(state):
        saved = repo.load_run(run.state.run_id)
        assert saved.incident.counters.decider_calls == 1
        assert saved.events[-1].type == "decider_first_requested"

    decider.hook = inspect_attempt
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == "provider_error_decider"
    assert len(decider.calls) == 1 and result["diagnostic_reads"] == 0
    # The run store is the idempotency authority: a duplicate invocation of a
    # finished run answers with its stored result and calls nothing again.
    assert asyncio.run(controller.run(run.state.run_id)) == result
    assert len(decider.calls) == 1


@pytest.mark.parametrize(
    "seconds,reason",
    [
        (19.999, "populated_after_repair"),
        (20.0, "provider_error_decider"),
        (20.001, "provider_error_decider"),
        (120.0, "run_deadline"),
        (120.001, "run_deadline"),
    ],
)
def test_TST_27_late_response_never_executes(tmp_path, seconds, reason):
    clock = FakeClock()
    decider = FakeDecider(hook=lambda state: clock.advance(seconds))
    controller, repo, run, _ = setup(tmp_path, decider=decider, clock=clock)
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == reason
    if seconds >= 20:
        assert result["diagnostic_reads"] == 0
    assert (
        len(
            [
                event
                for event in repo.list_events(run.state.run_id)
                if event.type == "run_finished"
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "name,cap",
    [
        ("diagnostic_reads", 2),
        ("decider_calls", 2),
        ("history_lookups", 2),
        ("repair_attempts", 1),
        ("report_queries", 3),
        ("wait_checks", 1),
        ("agent_turns", 8),
    ],
)
def test_TST_22_all_attempt_budgets(name, cap):
    counters = Counters()
    for expected in range(1, cap + 1):
        counters = consume(counters, name)
        assert getattr(counters, name) == expected
    with pytest.raises(RuntimeFault):
        consume(counters, name)


def test_TST_31_report_tool_rejects_arguments_and_counts(tmp_path):
    controller, repo, run, _ = setup(tmp_path)
    controller.begin(run.state.run_id)
    tool = ReportTool(controller)
    assert tool.input_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    for arguments in (
        {"task": "other"},
        {"join_key_mode": "strip_country_prefix"},
        {"sql": "SELECT 1"},
    ):
        assert tool.invoke(arguments) == {"error": "validation_error"}
    assert repo.load_run(run.state.run_id).incident.counters.report_queries == 3
    assert repo.load_run(run.state.run_id).query == run.query
    with pytest.raises(RuntimeFault, match="report_queries_exhausted"):
        tool.invoke({})


def test_TST_13_TST_25_allowlisted_provider_state(tmp_path):
    catalog = tuple(
        probe for probe in default_catalog() if probe.public.id == "inspect_scope"
    )
    controller, repo, run, decider = setup(
        tmp_path, decider=FakeDecider("inspect_scope"), catalog=catalog
    )
    asyncio.run(controller.run(run.state.run_id))
    state = decider.calls[0]
    text = state.model_dump_json()
    assert len(text) <= 8000 and len(state.candidates) == 1
    for forbidden in (
        "capability",
        "expected_rows",
        "expected_action",
        "fixtures/evaluator",
        "environment",
        "authorization",
        "classification",
    ):
        assert forbidden not in text.lower()
    assert state.task_hash == run.state.task_hash


@pytest.mark.parametrize("failure", ["version", "tool", "repair"])
def test_TST_12_TST_20_selected_tool_failures_stop(tmp_path, monkeypatch, failure):
    controller, repo, run, decider = setup(tmp_path)
    if failure == "version":
        decider.hook = lambda state: setattr(
            controller.engine,
            "fixture",
            replace(controller.engine.fixture, dataset_version="v99"),
        )
    elif failure == "tool":
        monkeypatch.setattr(
            controller.engine,
            "diagnose",
            lambda *args: (_ for _ in ()).throw(RuntimeFault(Reason.TOOL_ERROR)),
        )
    else:
        original = controller.engine.report

        def broken(query, task_hash):
            if query.join_key_mode != "literal":
                raise RuntimeFault(Reason.REPAIR_FAILED)
            return original(query, task_hash)

        monkeypatch.setattr(controller.engine, "report", broken)
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["status"] == "unresolved"
    # A failed read opens one second decision; a failed repair comes after a
    # useful read and opens none. Either way nothing is retried.
    failed_read = failure in {"version", "tool"}
    assert len(decider.calls) == (2 if failed_read else 1)
    assert result["pass_count"] == (2 if failed_read else 1)
    assert (result["blocked_tool_id"] is None) != failed_read
    assert (result["blocked_reason_code"] is None) != failed_read


def test_TST_11_complete_facts_finish_before_selection(tmp_path):
    fixture = load_fixture()
    fixture = replace(
        fixture,
        orders=(),
        countries_key_encoding="plain",
        countries=tuple(
            replace(country, country_id=country.country_code)
            for country in fixture.countries
        ),
    )
    controller, repo, run, decider = setup(tmp_path, fixture)
    controller.initial_evidence = (
        controller.engine.diagnose("inspect_schema_keys", run.query),
    )
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == "empty_initial" and not decider.calls


def test_TST_11_empty_after_wait_with_known_compatible_schema(tmp_path):
    fixture = load_fixture()
    fixture = replace(
        fixture,
        orders=(),
        job_state="pending",
        arrival_after_seconds=3.0,
        countries_key_encoding="plain",
        countries=tuple(
            replace(country, country_id=country.country_code)
            for country in fixture.countries
        ),
    )
    controller, repo, run, decider = setup(
        tmp_path, fixture, FakeDecider("inspect_pipeline")
    )
    controller.initial_evidence = (
        controller.engine.diagnose("inspect_schema_keys", run.query),
    )
    result = asyncio.run(controller.run(run.state.run_id))
    assert result["reason_code"] == "empty_after_wait" and len(decider.calls) == 1


@pytest.mark.parametrize("arrival", [None, 3.0], ids=["pending", "complete"])
@pytest.mark.parametrize(
    "corruption",
    [
        "stale",
        "future",
        "dataset",
        "hash",
        "version",
        "kind",
        "payload_version",
        "naive_timestamp",
    ],
)
def test_R01_recheck_provenance(tmp_path, monkeypatch, arrival, corruption):
    fixture = load_fixture()
    fixture = replace(
        fixture,
        job_state="pending",
        arrival_after_seconds=arrival,
        countries_key_encoding="plain",
        countries=tuple(
            replace(country, country_id=country.country_code)
            for country in fixture.countries
        ),
    )
    controller, repo, run, decider = setup(
        tmp_path, fixture, FakeDecider("inspect_pipeline")
    )
    original = controller.engine.recheck

    def corrupt(query, elapsed):
        record = original(query, elapsed)
        changes = {
            "stale": {
                "observed_at": controller.clock.utcnow() - timedelta(seconds=121)
            },
            "future": {"observed_at": controller.clock.utcnow() + timedelta(seconds=1)},
            "dataset": {"dataset_id": "other"},
            "hash": {"arguments_hash": "0" * 64},
            "version": {
                "dataset_version": "other",
                "payload": replace(record.payload, dataset_version="other"),
            },
            "kind": {"kind": "pipeline"},
            "payload_version": {
                "payload": replace(record.payload, dataset_version="other")
            },
            "naive_timestamp": {"observed_at": record.observed_at.replace(tzinfo=None)},
        }
        if corruption in {"payload_version", "naive_timestamp"}:
            return record.model_copy(update=changes[corruption])
        return replace(record, **changes[corruption])

    monkeypatch.setattr(controller.engine, "recheck", corrupt)
    result = asyncio.run(controller.run(run.state.run_id))
    final = repo.load_run(run.state.run_id)
    assert result["reason_code"] == "evidence_conflict"
    assert result["status"] == "unresolved"
    assert final.incident.counters == Counters(
        agent_turns=1,
        report_queries=1,
        decider_calls=1,
        diagnostic_reads=1,
        wait_checks=1,
    )
    assert len(decider.calls) == 1
    assert sum(event.type == "report_requested" for event in final.events) == 1
    assert sum(event.type == "run_finished" for event in final.events) == 1
    assert final.events[-1].payload == result


#: Evaluator-side vocabulary that must never appear in a selector payload. The
#: same token list `evaluation/isolation.py` and the qualification leakage scan
#: use, so one vocabulary governs both sides of the boundary.
FORBIDDEN_TOKENS = (
    "kappa-11",
    "kappa-27",
    "kappa-43",
    "kappa-58",
    "case_class",
    "valid_empty",
    "expected_action",
    "expected_status",
    "expected_rows",
    "expected_row_count",
    "acceptable_first_tools",
    "acceptable_final_tools",
    "holdout",
    "ground_truth",
    "answer_key",
    "expected_answer",
)


def plain_dimensions(fixture):
    return tuple(
        replace(country, country_id=country.country_code)
        for country in fixture.countries
    )


def query_observation(state):
    """The one query record the selector payload carries, as the model sees it."""
    records = [record for record in state.bounded_evidence if record.kind == "query"]
    assert len(records) == 1
    facts = records[0].facts
    assert isinstance(facts, dict)
    return facts


#: Three partition shapes stated purely as public fixture properties. No case
#: class, acceptable-tool set or expected outcome is named here or reachable
#: from here.
PARTITION_SHAPES = {
    "not_ready": {"job_state": "pending", "encodings": "matching"},
    "encodings_disagree": {"job_state": "complete", "encodings": "mismatched"},
    "ready_and_empty": {
        "job_state": "complete",
        "encodings": "matching",
        "orders": (),
    },
}


def shaped_fixture(shape):
    fixture = load_fixture()
    changes = {"job_state": shape["job_state"]}
    if "orders" in shape:
        changes["orders"] = shape["orders"]
    if shape["encodings"] == "matching":
        changes |= {
            "countries": plain_dimensions(fixture),
            "countries_key_encoding": "plain",
            "approved_join_modes": ("literal",),
        }
    return replace(fixture, **changes)


def test_TST_08_selector_payload_separates_three_partition_shapes(tmp_path):
    """The D1 payload must differ in facts, not only in opaque hashes.

    Each shape is an ordinary public property of a partition. The payload
    carries what the report established about the current state; which
    diagnostic to read, and what is finally true, stay with the selector, the
    controller and the verifier.
    """
    observed = {}
    for name, shape in PARTITION_SHAPES.items():
        controller, _, run, _ = setup(
            tmp_path / name, fixture=shaped_fixture(shape), key=f"shape-{name}"
        )
        controller.begin(run.state.run_id)
        controller._event(
            "public_evidence_observed",
            {},
            evidence=(controller.execute_report(),),
        )
        state = controller._decision_state(controller._candidates())
        facts = query_observation(state)
        assert set(facts) >= {
            "row_count",
            "query_hash",
            "encoding_proven",
            "source_complete",
        }
        text = state.model_dump_json()
        assert len(text) <= 8000
        lowered = text.lower()
        for token in FORBIDDEN_TOKENS:
            assert token not in lowered
        observed[name] = facts

    signatures = {
        name: (facts["row_count"], facts["encoding_proven"], facts["source_complete"])
        for name, facts in observed.items()
    }
    assert len(set(signatures.values())) == len(PARTITION_SHAPES)
    assert signatures["not_ready"] == (0, True, False)
    assert signatures["encodings_disagree"] == (0, False, False)
    assert signatures["ready_and_empty"] == (0, True, True)
