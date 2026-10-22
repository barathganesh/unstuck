"""DynamoDB key derivation and S3 prefix scoping.

Run state lives in `RUN#<run_id>` partitions and Decision Receipts live in
`PROBLEM#<problem_fingerprint>` partitions, per `docs/spec/design.md` section
16. A receipt sort key carries its arm and case, so a query bounded by that
prefix cannot reach another namespace even if the problem fingerprint matches.

Artifact keys are scoped by construction. Writes reach `runs/*` and
`receipt-evidence/*`; reads add read-only `fixtures/public/*`. The allowed set
is exhaustive and is checked first, so any other key is refused here, before a
request exists. That is what keeps the evaluator-truth fixtures unreachable:
`fixtures/public/` is the only fixtures prefix anything may read, so every
other fixtures path is refused without this module ever naming one. Naming it
would also put an evaluator path literal into the shipped package, which the
leakage scan of `docs/spec/design.md` section 20 refuses.

`DENIED_PREFIXES` is defence in depth for the staged-checkpoint and experiment
prefixes, which share the artifact bucket with the allowed prefixes.

Static prefix scoping is not per-run isolation; the controller lease and
conditional writes carry run identity.
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from unstuck.cascade.receipts import ReceiptNamespace
from unstuck.domain import Contract, Digest

RUN_PARTITION_PREFIX = "RUN#"
PROBLEM_PARTITION_PREFIX = "PROBLEM#"
META_SORT_KEY = "META"
EVENT_SORT_PREFIX = "EVENT#"
EVENT_SEQUENCE_DIGITS = 6
RECEIPT_SORT_TEMPLATE = "ARM#{arm}#CASE#{case_id}#RECEIPT#"

RUN_PREFIX = "runs/"
RECEIPT_EVIDENCE_PREFIX = "receipt-evidence/"
PUBLIC_FIXTURE_PREFIX = "fixtures/public/"
WRITE_PREFIXES = (RUN_PREFIX, RECEIPT_EVIDENCE_PREFIX)
READ_PREFIXES = (*WRITE_PREFIXES, PUBLIC_FIXTURE_PREFIX)
DENIED_PREFIXES = ("models/", "experiments/")

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_RUN_PARTITION = rf"^RUN#{_UUID}$"
_RUN_SORT = rf"^(?:META|EVENT#\d{{{EVENT_SEQUENCE_DIGITS}}})$"
_RECEIPT_PARTITION = r"^PROBLEM#[a-f0-9]{64}$"
_RECEIPT_SORT = (
    r"^ARM#(?:decider|decider_memory)#CASE#[A-Za-z0-9_-]{1,128}#RECEIPT#[a-f0-9]{64}$"
)

AttributeMap = dict[str, dict[str, str]]


class EventSequence(Contract):
    """One in-range event sequence number. Zero and overflow are refused."""

    value: Annotated[int, Field(ge=1, le=10**EVENT_SEQUENCE_DIGITS - 1)]

    @property
    def sort_suffix(self) -> str:
        return str(self.value).rjust(EVENT_SEQUENCE_DIGITS, "0")


class DynamoKey(Contract):
    """A run-state item key. The partition is always one run."""

    partition: Annotated[str, Field(pattern=_RUN_PARTITION)]
    sort: Annotated[str, Field(pattern=_RUN_SORT)]

    @classmethod
    def run_meta(cls, run_id: UUID) -> Self:
        return cls(partition=f"{RUN_PARTITION_PREFIX}{run_id}", sort=META_SORT_KEY)

    @classmethod
    def run_event(cls, run_id: UUID, sequence: int) -> Self:
        suffix = EventSequence(value=sequence).sort_suffix
        return cls(
            partition=f"{RUN_PARTITION_PREFIX}{run_id}",
            sort=f"{EVENT_SORT_PREFIX}{suffix}",
        )

    @classmethod
    def partition_for(cls, run_id: UUID) -> str:
        return f"{RUN_PARTITION_PREFIX}{run_id}"

    @property
    def item(self) -> AttributeMap:
        return {"PK": {"S": self.partition}, "SK": {"S": self.sort}}


class ReceiptIndexKey(Contract):
    """A Decision Receipt index key, scoped to one namespace by its sort key."""

    partition: Annotated[str, Field(pattern=_RECEIPT_PARTITION)]
    sort: Annotated[str, Field(pattern=_RECEIPT_SORT)]

    @classmethod
    def partition_for(cls, problem_fingerprint: str) -> str:
        return f"{PROBLEM_PARTITION_PREFIX}{problem_fingerprint}"

    @classmethod
    def namespace_prefix(cls, namespace: ReceiptNamespace) -> str:
        return RECEIPT_SORT_TEMPLATE.format(
            arm=namespace.arm, case_id=namespace.case_id
        )

    @classmethod
    def for_receipt(
        cls, namespace: ReceiptNamespace, problem_fingerprint: str, receipt_id: str
    ) -> Self:
        return cls(
            partition=cls.partition_for(problem_fingerprint),
            sort=f"{cls.namespace_prefix(namespace)}{receipt_id}",
        )

    @property
    def receipt_id(self) -> str:
        return self.sort.rsplit("#", 1)[1]

    @property
    def item(self) -> AttributeMap:
        return {"PK": {"S": self.partition}, "SK": {"S": self.sort}}


class ArtifactPrefix(Contract):
    """One scoped S3 object key. Construction is the scope check."""

    key: Annotated[
        str,
        Field(
            min_length=2,
            max_length=512,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*[A-Za-z0-9]$",
        ),
    ]
    mode: Literal["read", "write"]

    @model_validator(mode="after")
    def scoped(self) -> Self:
        if "//" in self.key or any(
            segment in {"", ".", ".."} for segment in self.key.split("/")
        ):
            raise ValueError("artifact key must be a normalized relative path")
        if any(self.key.startswith(denied) for denied in DENIED_PREFIXES):
            raise ValueError("artifact key targets a denied prefix")
        allowed = WRITE_PREFIXES if self.mode == "write" else READ_PREFIXES
        prefix = next((item for item in allowed if self.key.startswith(item)), None)
        if prefix is None:
            raise ValueError("artifact key is outside the scoped prefixes")
        if not self.key[len(prefix) :]:
            raise ValueError("artifact key must name an object inside its prefix")
        return self

    @property
    def prefix(self) -> str:
        allowed = WRITE_PREFIXES if self.mode == "write" else READ_PREFIXES
        return next(item for item in allowed if self.key.startswith(item))


def run_artifact_key(run_id: UUID, name: str) -> ArtifactPrefix:
    """The scoped write key for one run artifact."""
    return ArtifactPrefix(key=f"{RUN_PREFIX}{run_id}/{name}", mode="write")


def receipt_evidence_key(receipt_id: Digest, name: str) -> ArtifactPrefix:
    """The scoped write key for one receipt's evidence body."""
    return ArtifactPrefix(
        key=f"{RECEIPT_EVIDENCE_PREFIX}{receipt_id}/{name}", mode="write"
    )
