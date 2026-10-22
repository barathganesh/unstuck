from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from unstuck.domain import (
    Counters,
    EvidenceRecord,
    Incident,
    PipelineFacts,
    QuerySpec,
    Reason,
    RunState,
    Settings,
    TaskSpec,
    VerificationResult,
    canonical_hash,
    terminal_mapping,
)


def task(**changes):
    return TaskSpec(dataset_id="ds-17", order_date_utc=date(2026, 10, 18), **changes)


def test_contract_TST_01_canonical_immutable_task():
    first = task(country_codes=("US", "IN"))
    second = task(country_codes=("IN", "US", "US"))
    assert first.task_hash == second.task_hash
    assert first.country_codes == ("IN", "US")
    with pytest.raises(ValidationError):
        first.metric = "other"
    assert TaskSpec.model_validate_json(first.model_dump_json()) == first
    assert canonical_hash({"b": 1, "a": 2}) == canonical_hash({"a": 2, "b": 1})


@pytest.mark.parametrize(
    "changes",
    [
        {"metric": "price"},
        {"country_codes": ("USA",)},
        {"extra": True},
        {"user_goal": "x" * 513},
        {"country_codes": ("US",) * 251},
        {"country_codes": (1,)},
    ],
)
def test_contract_TST_01_invalid_scope(changes):
    with pytest.raises(ValidationError):
        task(**changes)


def test_contract_TST_31_query_and_settings():
    query = QuerySpec(task=task(), dataset_version="v17")
    assert query.join_key_mode == "literal"
    assert query.task_hash == task().task_hash
    assert query.base_query_hash == query.query_hash
    with pytest.raises(ValidationError):
        QuerySpec(task=task(), dataset_version="v17", join_key_mode="trim")
    for changes in (
        {"join_key_mode": "strip_country_prefix"},
        {"mode": "live"},
        {"mode": "replay"},
        {"first_confidence_min": float("nan")},
        {"run_seconds": 121},
        {"provider_seconds": 61},
        {"wait_seconds": 4},
    ):
        with pytest.raises(ValidationError):
            Settings(**changes)


def test_contract_TST_12_evidence_provenance():
    now = datetime.now(UTC)
    payload = PipelineFacts(
        dataset_version="v17",
        job_state="pending",
        scope_complete=False,
        watermark_utc=now,
    )
    args = dict(
        kind="pipeline",
        tool_id="inspect_pipeline",
        arguments_hash=task().task_hash,
        dataset_id="ds-17",
        dataset_version="v17",
        observed_at=now,
        payload=payload,
    )
    record = EvidenceRecord(**args)
    assert record.evidence_id
    for changes in (
        {"payload": {"unknown": True}},
        {"dataset_version": "v18"},
        {"kind": "scope"},
        {"observed_at": now.replace(tzinfo=None)},
        {
            "observed_at": now.astimezone(
                tz=__import__("datetime").timezone(timedelta(hours=1))
            )
        },
    ):
        with pytest.raises(ValidationError):
            EvidenceRecord(**(args | changes))


@pytest.mark.parametrize(
    "name,limit",
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
def test_contract_TST_22_counter_bounds(name, limit):
    assert getattr(Counters(**{name: limit}), name) == limit
    for invalid in (-1, limit + 1, "1", True):
        with pytest.raises(ValidationError):
            Counters(**{name: invalid})


def test_contract_TST_04_incident_identity():
    run_id = uuid4()
    query = QuerySpec(task=task(), dataset_version="v17")
    first = Incident.open(run_id, query)
    second = Incident.open(run_id, query)
    assert first.fingerprint == second.fingerprint
    assert first.task_hash == query.task_hash
    assert first.base_query_hash == query.base_query_hash
    assert first.fingerprint != Incident.open(uuid4(), query).fingerprint
    with pytest.raises(ValidationError):
        RunState(run_id=run_id, task=task(), dataset_version="v17", state_version=-1)


@pytest.mark.parametrize("reason", list(Reason))
def test_contract_TST_36_terminal_mapping(reason):
    state, disposition, status = terminal_mapping(reason)
    assert state in {"DONE", "PENDING", "UNRESOLVED", "BUDGET_STOP", "FAILED"}
    assert status in {
        "completed",
        "empty_verified",
        "pending",
        "unresolved",
        "budget_exhausted",
        "failed",
    }
    assert (disposition is None) == (status == "failed")
    with pytest.raises(ValueError):
        terminal_mapping("not-a-reason")


def test_contract_TST_36_verification_strict():
    result = VerificationResult(
        status="passed", reason_code="verified", evidence_ids=(uuid4(),)
    )
    assert result.verification_id
    for changes in ({"status": "maybe"}, {"reason_code": "invented"}, {"extra": 1}):
        with pytest.raises(ValidationError):
            VerificationResult(
                **({"status": "passed", "reason_code": "verified"} | changes)
            )
