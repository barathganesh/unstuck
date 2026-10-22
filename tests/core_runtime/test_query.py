from datetime import UTC, datetime, timedelta
from uuid import uuid4

import duckdb
import pytest
from pydantic import ValidationError

from unstuck.domain import Incident, QuerySpec, RuntimeFault, TaskSpec
from unstuck.fixtures import load_fixture
from unstuck.policy import validate_repair
from unstuck.query import EvidenceEngine, compile_query
from unstuck.verification import verify


def setup_engine():
    fixture = load_fixture()
    task = TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )
    query = QuerySpec(task=task, dataset_version=fixture.dataset_version)
    return EvidenceEngine(fixture), query


def replace(model, **changes):
    return type(model).model_validate(model.model_dump() | changes)


def test_TST_02_fixed_bound_template():
    engine, query = setup_engine()
    statement, params = compile_query(query, query.task_hash)
    assert "?" in statement and query.task.dataset_id not in statement
    assert params[0] == query.task.order_date_utc
    assert engine.report(query, query.task_hash).payload.rows == ()
    with pytest.raises(RuntimeFault, match="task_mismatch"):
        compile_query(query, "0" * 64)
    with pytest.raises((TypeError, AttributeError)):
        compile_query("DROP TABLE orders", query.task_hash)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_csv('https://example.invalid/data.csv')",
        "INSTALL httpfs",
        "LOAD httpfs",
        "ATTACH 'forbidden.db' AS other",
        "COPY countries TO 'forbidden.csv'",
        "SET enable_external_access=true",
        "SET autoload_known_extensions=true",
        "SET autoinstall_known_extensions=true",
    ],
)
def test_TST_34_duckdb_denies_external_paths(sql):
    engine, _ = setup_engine()
    with pytest.raises(duckdb.Error):
        engine._db.execute(sql)
    engine.close()


def test_TST_34_settings_and_arbitrary_sql_boundary():
    engine, query = setup_engine()
    for setting in (
        "enable_external_access",
        "autoload_known_extensions",
        "autoinstall_known_extensions",
    ):
        assert (
            engine._db.execute(f"SELECT current_setting('{setting}')").fetchone()[0]
            is False
        )
    assert (
        engine._db.execute("SELECT current_setting('lock_configuration')").fetchone()[0]
        is True
    )
    with pytest.raises((TypeError, AttributeError)):
        engine.report("SELECT 1", query.task_hash)


def test_TST_08_one_evidence_proven_repair():
    engine, query = setup_engine()
    incident = Incident.open(uuid4(), query)
    evidence = engine.diagnose("inspect_schema_keys", query)
    repaired = validate_repair(
        query, incident, evidence, engine.fixture.dataset_version, datetime.now(UTC)
    )
    assert repaired.join_key_mode == "strip_country_prefix"
    assert repaired.task == query.task
    assert repaired.base_query_hash == query.base_query_hash
    report = engine.report(repaired, query.task_hash)
    assert [(row.country_code, row.units) for row in report.payload.rows] == [
        ("IN", 4),
        ("US", 5),
    ]
    assert verify(repaired, report, (evidence,)).status == "passed"
    with pytest.raises(RuntimeFault, match="unsupported_repair"):
        validate_repair(
            repaired,
            incident,
            evidence,
            engine.fixture.dataset_version,
            datetime.now(UTC),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"metadata_complete": False},
        {"scope_covered": False},
        {"normalized_dimension_keys_unique": False},
        {"approved_join_modes": ("literal",)},
        {"orders_key_encoding": "unknown"},
        {"countries_key_encoding": "plain"},
    ],
)
def test_TST_21_repair_rejects_missing_proof(changes):
    engine, query = setup_engine()
    proof = engine.diagnose("inspect_schema_keys", query)
    proof = replace(proof, payload=replace(proof.payload, **changes))
    with pytest.raises(RuntimeFault, match="unsupported_repair"):
        validate_repair(
            query,
            Incident.open(uuid4(), query),
            proof,
            query.dataset_version,
            datetime.now(UTC),
        )


@pytest.mark.parametrize(
    "change",
    ["dataset", "version", "stale", "future", "arguments", "incident", "base", "task"],
)
def test_TST_12_TST_21_rejects_provenance_and_scope_changes(change):
    engine, query = setup_engine()
    proof = engine.diagnose("inspect_schema_keys", query)
    incident = Incident.open(uuid4(), query)
    now = datetime.now(UTC)
    if change == "dataset":
        proof = replace(proof, dataset_id="other")
    elif change == "version":
        proof = replace(
            proof,
            dataset_version="other",
            payload=replace(proof.payload, dataset_version="other"),
        )
    elif change in {"stale", "future"}:
        proof = replace(
            proof, observed_at=now + timedelta(seconds=-121 if change == "stale" else 1)
        )
    elif change == "arguments":
        proof = replace(proof, arguments_hash="0" * 64)
    elif change == "incident":
        incident = replace(incident, fingerprint="0" * 64)
    elif change == "base":
        incident = replace(incident, base_query_hash="0" * 64)
    else:
        query = replace(query, task=replace(query.task, country_codes=("DE",)))
    with pytest.raises(RuntimeFault):
        validate_repair(query, incident, proof, query.dataset_version, now)


def test_TST_08_samples_do_not_override_ambiguous_dimension():
    fixture = load_fixture()
    country = fixture.countries[0]
    fixture = replace(
        fixture,
        countries=fixture.countries
        + (replace(country, country_id="US", country_code="DE"),),
    )
    engine = EvidenceEngine(fixture)
    query = QuerySpec(
        task=TaskSpec(
            dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
        ),
        dataset_version=fixture.dataset_version,
    )
    proof = engine.diagnose("inspect_schema_keys", query)
    assert not proof.payload.normalized_dimension_keys_unique
    with pytest.raises(RuntimeFault):
        validate_repair(
            query,
            Incident.open(uuid4(), query),
            proof,
            query.dataset_version,
            datetime.now(UTC),
        )


@pytest.mark.parametrize(
    "corruption", ["total", "scope", "source", "version", "mode", "hash"]
)
def test_TST_08_verifier_rejects_corrupt_output(corruption):
    engine, query = setup_engine()
    proof = engine.diagnose("inspect_schema_keys", query)
    repaired = validate_repair(
        query,
        Incident.open(uuid4(), query),
        proof,
        query.dataset_version,
        datetime.now(UTC),
    )
    report = engine.report(repaired, query.task_hash)
    payload = report.payload
    if corruption == "total":
        payload = replace(
            payload, rows=(replace(payload.rows[0], units=999),) + payload.rows[1:]
        )
    elif corruption == "scope":
        payload = replace(
            payload,
            source_rows=(
                replace(
                    payload.source_rows[0],
                    order_date_utc=query.task.order_date_utc - timedelta(days=1),
                ),
            )
            + payload.source_rows[1:],
        )
    elif corruption == "source":
        payload = replace(
            payload, source_rows=payload.source_rows + payload.source_rows[:1]
        )
    elif corruption == "version":
        report = replace(
            report,
            dataset_version="v-other",
            payload=replace(payload, dataset_version="v-other"),
        )
        payload = report.payload
    elif corruption == "mode":
        proof = replace(proof, payload=replace(proof.payload, metadata_complete=False))
    else:
        payload = replace(payload, query_hash="0" * 64)
    report = replace(report, payload=payload)
    assert verify(repaired, report, (proof,)).status == "failed"


def test_TST_13_synthetic_metadata_only_and_bounds():
    fixture = load_fixture()
    for changes in (
        {"classification": "production"},
        {"expected_rows": []},
        {"orders": fixture.orders * 1001},
    ):
        with pytest.raises(ValidationError):
            replace(fixture, **changes)
    with pytest.raises(ValidationError):
        replace(fixture, orders=fixture.orders + fixture.orders[:1])


def plain_dimensions(fixture):
    """The same dimensions published without the prefix encoding."""
    return tuple(
        replace(country, country_id=country.country_code)
        for country in fixture.countries
    )


def test_TST_01_source_completeness_requires_a_ready_partition():
    """An unloaded partition must not read as a complete source.

    Completeness is established by comparing the reported source rows against
    the partition's own rows. When the partition is not ready those rows are
    not loaded, so both sides are empty and the comparison proves nothing. The
    honest answer is that completeness is unestablished.
    """
    base = replace(
        load_fixture(),
        orders=(),
        countries=plain_dimensions(load_fixture()),
        countries_key_encoding="plain",
        approved_join_modes=("literal",),
    )
    task = TaskSpec(dataset_id=base.dataset_id, order_date_utc=base.order_date_utc)
    query = QuerySpec(task=task, dataset_version=base.dataset_version)
    ready = EvidenceEngine(base).report(query, query.task_hash).payload
    pending = (
        EvidenceEngine(replace(base, job_state="pending"))
        .report(query, query.task_hash)
        .payload
    )
    assert ready.rows == () and pending.rows == ()
    assert ready.source_complete is True
    assert pending.source_complete is False
    assert ready.encoding_proven is True and pending.encoding_proven is True


def test_TST_24_diagnostics_are_typed_and_scope_bound():
    engine, query = setup_engine()
    for capability in ("inspect_pipeline", "inspect_scope", "inspect_schema_keys"):
        record = engine.diagnose(capability, query)
        assert record.dataset_version == query.dataset_version
        assert record.arguments_hash == query.query_hash
    with pytest.raises(RuntimeFault, match="provider_error_tool"):
        engine.diagnose("arbitrary", query)
