from datetime import UTC, datetime
from typing import Literal

import duckdb

from unstuck.clock import Clock, SystemClock
from unstuck.domain import (
    EvidenceRecord,
    PipelineFacts,
    QueryFacts,
    QuerySpec,
    Reason,
    Row,
    RuntimeFault,
    SchemaFacts,
    ScopeFacts,
    SourceRow,
)
from unstuck.fixtures import Fixture

_LITERAL = "orders.country_id = countries.country_id"
_NORMALIZED = """CASE WHEN starts_with(orders.country_id, 'country:') THEN substr(orders.country_id, 9)
ELSE orders.country_id END = CASE WHEN starts_with(countries.country_id, 'country:')
THEN substr(countries.country_id, 9) ELSE countries.country_id END"""
_TEMPLATE = """SELECT country_code, sum(units),
list(struct_pack(order_id := order_id, order_date_utc := order_date_utc, units := units))
FROM orders JOIN countries ON {join}
WHERE order_date_utc = ? AND (? = 0 OR country_code IN (SELECT unnest(?)))
GROUP BY country_code ORDER BY country_code"""


def compile_query(query: QuerySpec, task_hash: str) -> tuple[str, list[object]]:
    if query.task_hash != task_hash:
        raise RuntimeFault(Reason.TASK_MISMATCH)
    join = _LITERAL if query.join_key_mode == "literal" else _NORMALIZED
    return _TEMPLATE.format(join=join), [
        query.task.order_date_utc,
        len(query.task.country_codes),
        list(query.task.country_codes),
    ]


class EvidenceEngine:
    def __init__(self, fixture: Fixture, clock: Clock | None = None):
        self.fixture = fixture
        self.clock = clock or SystemClock()
        self._ready = fixture.job_state == "complete"
        self._cancelled = False
        self._db = duckdb.connect(":memory:")
        self._db.execute(
            "CREATE TABLE orders(order_id VARCHAR, country_id VARCHAR, order_date_utc DATE, units BIGINT)"
        )
        self._db.execute(
            "CREATE TABLE countries(country_id VARCHAR, country_code VARCHAR, country_name VARCHAR)"
        )
        if fixture.countries:
            self._db.executemany(
                "INSERT INTO countries VALUES (?, ?, ?)",
                [
                    (country.country_id, country.country_code, country.country_name)
                    for country in fixture.countries
                ],
            )
        self._load_orders()
        for setting in (
            "enable_external_access=false",
            "autoinstall_known_extensions=false",
            "autoload_known_extensions=false",
            "lock_configuration=true",
        ):
            self._db.execute("SET " + setting)

    def _load_orders(self) -> None:
        if self._ready and self.fixture.orders:
            self._db.executemany(
                "INSERT INTO orders VALUES (?, ?, ?, ?)",
                [
                    (
                        order.order_id,
                        order.country_id,
                        order.order_date_utc,
                        order.units,
                    )
                    for order in self.fixture.orders
                ],
            )

    def close(self) -> None:
        self._db.close()

    def cancel(self) -> None:
        self._cancelled = True
        self._db.interrupt()

    def _assert_query(self, query: QuerySpec) -> None:
        if self._cancelled:
            raise RuntimeFault(Reason.TOOL_ERROR)
        if (
            query.task.dataset_id != self.fixture.dataset_id
            or query.dataset_version != self.fixture.dataset_version
        ):
            raise RuntimeFault(Reason.CONFLICT)

    def _record(
        self,
        query: QuerySpec,
        tool_id: str,
        payload: QueryFacts | PipelineFacts | SchemaFacts | ScopeFacts,
        kind: Literal["query", "pipeline", "schema_keys", "scope", "wait"],
    ) -> EvidenceRecord:
        return EvidenceRecord(
            kind=kind,
            tool_id=tool_id,
            arguments_hash=query.query_hash,
            dataset_id=self.fixture.dataset_id,
            dataset_version=self.fixture.dataset_version,
            observed_at=self.clock.utcnow(),
            payload=payload,
        )

    def report(self, query: QuerySpec, task_hash: str) -> EvidenceRecord:
        self._assert_query(query)
        sql, parameters = compile_query(query, task_hash)
        records = self._db.execute(sql, parameters).fetchall()
        rows = tuple(
            Row(country_code=record[0], units=int(record[1])) for record in records
        )
        sources = tuple(
            SourceRow(
                country_code=record[0],
                order_id=source["order_id"],
                order_date_utc=source["order_date_utc"],
                units=source["units"],
            )
            for record in records
            for source in record[2]
        )
        payload = QueryFacts(
            dataset_version=query.dataset_version,
            query_hash=query.query_hash,
            rows=rows,
            source_rows=sources,
            encoding_proven=self._encoding_proven(query),
            source_complete=self._source_complete(query, sources),
        )
        return self._record(query, "execute_report", payload, "query")

    def _encoding_proven(self, query: QuerySpec) -> bool:
        fixture = self.fixture
        normalized = [
            country.country_id.removeprefix("country:") for country in fixture.countries
        ]
        return (
            fixture.metadata_complete
            and fixture.scope_covered
            and query.task.order_date_utc == fixture.order_date_utc
            and query.join_key_mode in fixture.approved_join_modes
            and len(normalized) == len(set(normalized))
            and fixture.orders_key_encoding != "unknown"
            and fixture.countries_key_encoding != "unknown"
            and (
                query.join_key_mode != "literal"
                or fixture.orders_key_encoding == fixture.countries_key_encoding
            )
            and all(
                self._encoding_matches(order.country_id, fixture.orders_key_encoding)
                for order in fixture.orders
            )
            and all(
                self._encoding_matches(
                    country.country_id, fixture.countries_key_encoding
                )
                for country in fixture.countries
            )
        )

    def _source_complete(
        self, query: QuerySpec, sources: tuple[SourceRow, ...]
    ) -> bool:
        # Completeness is proven by comparing the reported sources against the
        # partition's own rows. An unready partition is not loaded, so both
        # sides are empty and the comparison would prove nothing; fail closed
        # rather than report an empty table as a complete source.
        if not self._ready:
            return False
        countries = {
            country.country_id.removeprefix("country:"): country.country_code
            for country in self.fixture.countries
        }
        orders = self._db.execute(
            "SELECT order_id, country_id FROM orders WHERE order_date_utc = ?",
            [query.task.order_date_utc],
        ).fetchall()
        expected = {
            order_id
            for order_id, key in orders
            if not query.task.country_codes
            or countries.get(key.removeprefix("country:")) in query.task.country_codes
            or key.removeprefix("country:") not in countries
        }
        return expected == {source.order_id for source in sources} and len(
            sources
        ) == len(expected)

    def diagnose(self, capability: str, query: QuerySpec) -> EvidenceRecord:
        self._assert_query(query)
        fixture = self.fixture
        version = fixture.dataset_version
        covered = (
            fixture.scope_covered
            and query.task.order_date_utc == fixture.order_date_utc
        )
        if capability == "inspect_pipeline":
            pipeline = PipelineFacts(
                dataset_version=version,
                job_state="complete" if self._ready else fixture.job_state,
                scope_complete=covered and self._ready and fixture.scope_complete,
                watermark_utc=datetime.combine(
                    fixture.order_date_utc, datetime.min.time(), UTC
                ),
            )
            return self._record(query, capability, pipeline, "pipeline")
        if capability == "inspect_schema_keys":
            normalized = [
                country.country_id.removeprefix("country:")
                for country in fixture.countries
            ]
            declared = all(
                self._encoding_matches(order.country_id, fixture.orders_key_encoding)
                for order in fixture.orders
            )
            declared = declared and all(
                self._encoding_matches(
                    country.country_id, fixture.countries_key_encoding
                )
                for country in fixture.countries
            )
            schema = SchemaFacts(
                dataset_version=version,
                orders_key_encoding=fixture.orders_key_encoding,
                countries_key_encoding=fixture.countries_key_encoding,
                approved_join_modes=fixture.approved_join_modes,
                normalized_dimension_keys_unique=len(normalized)
                == len(set(normalized)),
                metadata_complete=fixture.metadata_complete and declared,
                scope_covered=covered,
                partition_complete=self._ready and fixture.scope_complete,
                orders_examples=tuple(order.country_id for order in fixture.orders[:5]),
                countries_examples=tuple(
                    country.country_id for country in fixture.countries[:5]
                ),
            )
            return self._record(query, capability, schema, "schema_keys")
        if capability == "inspect_scope":
            scope = ScopeFacts(
                dataset_version=version,
                query_matches_task_scope=True,
                metadata_complete=fixture.metadata_complete,
                scope_covered=covered,
            )
            return self._record(query, capability, scope, "scope")
        raise RuntimeFault(Reason.TOOL_ERROR)

    @staticmethod
    def _encoding_matches(key: str, encoding: str) -> bool:
        return (encoding == "plain" and not key.startswith("country:")) or (
            encoding == "country_prefix" and key.startswith("country:")
        )

    def recheck(self, query: QuerySpec, elapsed_seconds: float) -> EvidenceRecord:
        self._assert_query(query)
        arrival = self.fixture.arrival_after_seconds
        if not self._ready and arrival is not None and elapsed_seconds >= arrival:
            self._ready = True
            self._load_orders()
        record = self.diagnose("inspect_pipeline", query)
        return EvidenceRecord.model_validate(
            record.model_dump() | {"kind": "wait", "tool_id": "wait_for_partition"}
        )
