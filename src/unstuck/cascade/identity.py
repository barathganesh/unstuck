"""Canonical decision identity for the Decision Receipt engine.

Two identities are computed here and nothing else may be used for matching:

* `problem_fingerprint` is the stable task and base-query lineage. It survives
  across calls so history for the same problem can be found at all.
* `decision_context_hash` is the complete current decision identity: task and
  query, bound versions, the ordered admissible evidence, candidate membership
  with availability, and the capabilities already used.

Operational identity lives in `CallIdentity` and is excluded from both hashes.
Reordered equivalent evidence, reordered candidates, and serialization
differences are likewise non-authoritative: the hash is computed over a
normalized projection, not over the stored presentation order.
"""

from datetime import datetime, timedelta
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from unstuck.cascade.cards import (
    CATALOG_VERSION,
    DATA_SCHEMA_VERSION,
    TOOL_CONTRACT_VERSION,
)
from unstuck.cascade.serialization import canonical_digest
from unstuck.domain import Contract, Digest, Identifier


class BoundVersions(Contract):
    """Every version that must hold for a receipt to still describe reality."""

    dataset_version: Identifier
    model_revision: Identifier
    policy_hash: Digest
    calibration_hash: Digest
    data_schema_version: Identifier = DATA_SCHEMA_VERSION
    catalog_version: Identifier = CATALOG_VERSION
    tool_contract_version: Identifier = TOOL_CONTRACT_VERSION

    @property
    def versions_hash(self) -> str:
        return canonical_digest(self)


class CandidateIdentity(Contract):
    """One offered option as it enters the decision identity."""

    public_tool_id: Identifier
    description: Annotated[str, Field(min_length=1, max_length=512)]
    available: bool


class DecisionContext(Contract):
    """The pre-D1 decision identity. Post-call evidence is a separate outcome."""

    task_hash: Digest
    query_hash: Digest
    base_query_hash: Digest
    dataset_id: Identifier
    versions: BoundVersions
    candidates: Annotated[
        tuple[CandidateIdentity, ...], Field(min_length=1, max_length=3)
    ]
    evidence_hashes: Annotated[tuple[Digest, ...], Field(max_length=4)] = ()
    used_capabilities: Annotated[tuple[Identifier, ...], Field(max_length=2)] = ()

    @model_validator(mode="after")
    def distinct(self) -> Self:
        identifiers = tuple(item.public_tool_id for item in self.candidates)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate candidate identities")
        if len(set(self.evidence_hashes)) != len(self.evidence_hashes):
            raise ValueError("duplicate evidence identities")
        if len(set(self.used_capabilities)) != len(self.used_capabilities):
            raise ValueError("duplicate used capabilities")
        return self

    @property
    def problem_fingerprint(self) -> str:
        return canonical_digest(
            {
                "kind": "problem",
                "dataset_id": self.dataset_id,
                "task_hash": self.task_hash,
                "base_query_hash": self.base_query_hash,
            }
        )

    @property
    def normalized(self) -> dict[str, object]:
        """Order-independent projection used for the context identity."""
        candidates = [item.model_dump(mode="json") for item in self.candidates]
        candidates.sort(key=lambda item: str(item["public_tool_id"]))
        return {
            "task_hash": self.task_hash,
            "query_hash": self.query_hash,
            "base_query_hash": self.base_query_hash,
            "dataset_id": self.dataset_id,
            "versions": self.versions.model_dump(mode="json"),
            "evidence_hashes": sorted(self.evidence_hashes),
            "candidates": candidates,
            "used_capabilities": sorted(self.used_capabilities),
        }

    @property
    def decision_context_hash(self) -> str:
        return canonical_digest(self.normalized)

    @property
    def available_tool_ids(self) -> tuple[str, ...]:
        return tuple(item.public_tool_id for item in self.candidates if item.available)


class CallIdentity(Contract):
    """Operational identity. Never part of the semantic decision identity.

    A new run, request, session, trace, lease owner, timestamp, or retry
    attempt is not a changed decision context and cannot reset a budget.
    """

    run_id: UUID
    request_id: UUID
    session_id: UUID
    trace_id: UUID
    lease_owner: UUID
    observed_at: datetime
    attempt: Annotated[int, Field(ge=1, le=4)] = 1

    @field_validator("observed_at")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("UTC timestamp required")
        return value
