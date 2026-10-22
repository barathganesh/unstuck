# Unit command: UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade -q
import socket
from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from unstuck.cascade.cards import capability_cards
from unstuck.cascade.identity import BoundVersions, CallIdentity, DecisionContext
from unstuck.cascade.policy import CascadePolicy
from unstuck.cascade.receipts import DecisionReceipt, EvidenceDelta, ReceiptNamespace
from unstuck.cascade.repository import LocalReceiptRepository
from unstuck.cascade.verifier import DiagnosticReport, ReceiptVerifier
from unstuck.clock import FakeClock
from unstuck.decision import DecisionState, default_catalog
from unstuck.domain import Counters, QuerySpec, Reason, TaskSpec

BEFORE_HASH = "1" * 64
AFTER_HASH = "2" * 64


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network is forbidden in confidence-cascade tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "bind", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def policy() -> CascadePolicy:
    return CascadePolicy(baseline_id="decider_memory", model_revision="scripted-v1")


@pytest.fixture
def one_pass_policy() -> CascadePolicy:
    return CascadePolicy(baseline_id="decider", model_revision="scripted-v1")


@pytest.fixture
def namespace() -> ReceiptNamespace:
    return ReceiptNamespace(arm="decider_memory", case_id="case-01")


@pytest.fixture
def repository() -> LocalReceiptRepository:
    return LocalReceiptRepository()


@pytest.fixture
def verifier(clock) -> ReceiptVerifier:
    return ReceiptVerifier(clock)


@pytest.fixture
def make_state() -> Callable[..., DecisionState]:
    def build(
        *,
        candidate_count: int = 3,
        country_codes: tuple[str, ...] = (),
        dataset_version: str = "v17",
        order_date_utc: date = date(2026, 10, 1),
        available: tuple[str, ...] | None = None,
        **changes: object,
    ) -> DecisionState:
        task = TaskSpec(
            dataset_id="ds-17",
            order_date_utc=order_date_utc,
            country_codes=country_codes,
        )
        query = QuerySpec(task=task, dataset_version=dataset_version)
        candidates = tuple(
            probe.public.model_copy(
                update={"available": available is None or probe.public.id in available}
            )
            for probe in default_catalog()[:candidate_count]
        )
        return DecisionState.model_validate(
            {
                "task": task,
                "query": query,
                "task_hash": task.task_hash,
                "query_hash": query.query_hash,
                "dataset_version": dataset_version,
                "candidates": candidates,
                "budgets": Counters(),
                "remaining_ms": 120000,
            }
            | changes
        )

    return build


@pytest.fixture
def state(make_state) -> DecisionState:
    return make_state()


@pytest.fixture
def versions(policy, state) -> BoundVersions:
    return policy.versions(state.dataset_version)


@pytest.fixture
def make_context(policy) -> Callable[..., DecisionContext]:
    def build(state: DecisionState, **changes: object) -> DecisionContext:
        return DecisionContext.model_validate(
            {
                "task_hash": state.task_hash,
                "query_hash": state.query_hash,
                "base_query_hash": state.query.base_query_hash,
                "dataset_id": state.task.dataset_id,
                "versions": policy.versions(state.dataset_version),
                "evidence_hashes": (),
                "candidates": tuple(
                    {
                        "public_tool_id": probe.id,
                        "description": probe.description,
                        "available": probe.available,
                    }
                    for probe in state.candidates
                ),
                "used_capabilities": (),
            }
            | changes
        )

    return build


@pytest.fixture
def context(make_context, state) -> DecisionContext:
    return make_context(state)


@pytest.fixture
def call() -> CallIdentity:
    return CallIdentity(
        run_id=uuid4(),
        request_id=uuid4(),
        session_id=uuid4(),
        trace_id=uuid4(),
        lease_owner=uuid4(),
        observed_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )


@pytest.fixture
def cards():
    return capability_cards()


DEFAULT_REASONS = {
    ("evidence", True): Reason.POPULATED_INITIAL,
    ("evidence", False): Reason.INSUFFICIENT,
    ("provider_error", False): Reason.TOOL_ERROR,
    ("invalid_evidence", False): Reason.CONFLICT,
}


@pytest.fixture
def make_report(state) -> Callable[..., DiagnosticReport]:
    def build(
        *,
        capability: str = "inspect_schema_keys",
        public_tool_id: str | None = None,
        status: str = "evidence",
        admissible: bool | None = None,
        changed: bool | None = None,
        reason_code: Reason | None = None,
        diagnostic_call_id: UUID | None = None,
        arguments_hash: str | None = None,
        **changes: object,
    ) -> DiagnosticReport:
        if changed is None:
            changed = status == "evidence"
        delta = EvidenceDelta(
            evidence_before_hash=BEFORE_HASH,
            evidence_after_hash=AFTER_HASH if changed else BEFORE_HASH,
            evidence_ids=(uuid4(),) if changed else (),
        )
        if diagnostic_call_id is None and status != "provider_error":
            diagnostic_call_id = uuid4()
        return DiagnosticReport.model_validate(
            {
                "diagnostic_call_id": diagnostic_call_id,
                "capability": capability,
                "public_tool_id": public_tool_id or capability,
                "arguments_hash": arguments_hash or state.query_hash,
                "status": status,
                "delta": delta,
                "admissible": (status == "evidence")
                if admissible is None
                else admissible,
                # Incoherent combinations keep a plausible reason so the
                # contract validator, not this fixture, rejects them.
                "reason_code": DEFAULT_REASONS.get(
                    (status, changed), Reason.INSUFFICIENT
                )
                if reason_code is None
                else reason_code,
            }
            | changes
        )

    return build


@pytest.fixture
def useful_report(make_report) -> DiagnosticReport:
    return make_report(reason_code=Reason.POPULATED_INITIAL)


@pytest.fixture
def stalled_report(make_report) -> DiagnosticReport:
    return make_report(changed=False, reason_code=Reason.INSUFFICIENT)


@pytest.fixture
def make_receipt(verifier, namespace, context) -> Callable[..., DecisionReceipt]:
    def build(report: DiagnosticReport, **changes: object) -> DecisionReceipt:
        arguments = {
            "namespace": namespace,
            "decision_id": uuid4(),
            "model_call_id": uuid4(),
            "context": context,
            "report": report,
        } | changes
        return verifier.create(**arguments)

    return build
