from datetime import datetime

from unstuck.domain import (
    Counters,
    EvidenceRecord,
    Incident,
    QuerySpec,
    Reason,
    RuntimeFault,
    SchemaFacts,
)


def consume(counters: Counters, name: str) -> Counters:
    limits = {
        "diagnostic_reads": (2, Reason.DIAGNOSTICS_EXHAUSTED),
        "decider_calls": (2, Reason.FIRST_REJECTED),
        "history_lookups": (2, Reason.HISTORY_UNAVAILABLE),
        "repair_attempts": (1, Reason.UNSUPPORTED_REPAIR),
        "report_queries": (3, Reason.REPORTS_EXHAUSTED),
        "wait_checks": (1, Reason.INSUFFICIENT),
        "agent_turns": (8, Reason.TURNS_EXHAUSTED),
    }
    if name not in limits:
        raise RuntimeFault(Reason.INVARIANT)
    limit, reason = limits[name]
    if getattr(counters, name) >= limit:
        raise RuntimeFault(reason)
    return Counters.model_validate(
        counters.model_dump() | {name: getattr(counters, name) + 1}
    )


def current_evidence(
    query: QuerySpec, record: EvidenceRecord, now: datetime, *, allow_base: bool = False
) -> bool:
    hashes = (
        {query.query_hash, query.base_query_hash} if allow_base else {query.query_hash}
    )
    return (
        record.dataset_id == query.task.dataset_id
        and record.dataset_version == query.dataset_version
        and record.arguments_hash in hashes
        and 0 <= (now - record.observed_at).total_seconds() <= 120
    )


def compatible(query: QuerySpec, schema: SchemaFacts) -> bool:
    if (
        not schema.metadata_complete
        or not schema.scope_covered
        or not schema.normalized_dimension_keys_unique
    ):
        return False
    if "unknown" in (schema.orders_key_encoding, schema.countries_key_encoding):
        return False
    if query.join_key_mode not in schema.approved_join_modes:
        return False
    return (
        query.join_key_mode == "strip_country_prefix"
        or schema.orders_key_encoding == schema.countries_key_encoding
    )


def validate_repair(
    query: QuerySpec,
    incident: Incident,
    proof: EvidenceRecord,
    current_version: str,
    now: datetime,
) -> QuerySpec:
    identity = Incident.open(incident.run_id, query)
    if (
        incident.task_hash != query.task_hash
        or incident.base_query_hash != query.base_query_hash
        or incident.fingerprint != identity.fingerprint
    ):
        raise RuntimeFault(Reason.TASK_MISMATCH)
    schema = proof.payload
    if (
        query.join_key_mode != "literal"
        or incident.counters.repair_attempts > 0
        or not isinstance(schema, SchemaFacts)
        or proof.kind != "schema_keys"
    ):
        raise RuntimeFault(Reason.UNSUPPORTED_REPAIR)
    if current_version != query.dataset_version or not current_evidence(
        query, proof, now
    ):
        raise RuntimeFault(Reason.CONFLICT)
    if (
        not schema.metadata_complete
        or not schema.scope_covered
        or not schema.normalized_dimension_keys_unique
        or "strip_country_prefix" not in schema.approved_join_modes
        or {schema.orders_key_encoding, schema.countries_key_encoding}
        != {"plain", "country_prefix"}
    ):
        raise RuntimeFault(Reason.UNSUPPORTED_REPAIR)
    return QuerySpec(
        task=query.task,
        dataset_version=query.dataset_version,
        join_key_mode="strip_country_prefix",
    )
