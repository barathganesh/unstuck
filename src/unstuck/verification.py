from datetime import UTC, datetime

from unstuck.domain import (
    EvidenceRecord,
    QueryFacts,
    QuerySpec,
    SchemaFacts,
    VerificationResult,
)
from unstuck.policy import compatible, current_evidence


def verify(
    query: QuerySpec,
    report: EvidenceRecord,
    evidence: tuple[EvidenceRecord, ...] = (),
    now: datetime | None = None,
) -> VerificationResult:
    identifiers = (report.evidence_id,) + tuple(
        record.evidence_id for record in evidence
    )

    def failure(reason: str) -> VerificationResult:
        return VerificationResult.model_validate(
            {"status": "failed", "reason_code": reason, "evidence_ids": identifiers}
        )

    now = now or datetime.now(UTC)
    if not current_evidence(query, report, now) or any(
        not current_evidence(query, record, now, allow_base=True) for record in evidence
    ):
        return failure("evidence_conflict")
    payload = report.payload
    if not isinstance(payload, QueryFacts) or payload.query_hash != query.query_hash:
        return failure("source_mismatch")
    if payload.rows or query.join_key_mode != "literal":
        if not payload.encoding_proven:
            return failure("encoding_unproven")
        if not payload.source_complete:
            return failure("source_mismatch")
    if query.join_key_mode != "literal" and not any(
        isinstance(record.payload, SchemaFacts) and compatible(query, record.payload)
        for record in evidence
    ):
        return failure("encoding_unproven")
    totals: dict[str, int] = {}
    seen: set[str] = set()
    for source in payload.source_rows:
        if source.order_id in seen:
            return failure("source_mismatch")
        seen.add(source.order_id)
        if source.order_date_utc != query.task.order_date_utc or (
            query.task.country_codes
            and source.country_code not in query.task.country_codes
        ):
            return failure("scope_mismatch")
        totals[source.country_code] = totals.get(source.country_code, 0) + source.units
    actual = [(row.country_code, row.units) for row in payload.rows]
    if actual != sorted(totals.items()):
        return failure("shape_mismatch")
    return VerificationResult(
        status="passed", reason_code="verified", evidence_ids=identifiers
    )
