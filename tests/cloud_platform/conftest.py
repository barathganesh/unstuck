# Unit command: UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/cloud_platform -q
"""Offline fixtures for the cloud-platform unit.

Every value here is synthetic and every result is offline. No AWS account,
credential chain, profile, socket, live provider, customer identifier, or
evaluator asset is reachable from these tests.

Domain objects are built from the same production factories the core-runtime
and confidence-cascade suites use, so no contract is duplicated here.
"""

import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from botocore.session import Session
from botocore.stub import Stubber

from unstuck.cascade.identity import DecisionContext
from unstuck.cascade.policy import CascadePolicy
from unstuck.cascade.receipts import DecisionReceipt, EvidenceDelta, ReceiptNamespace
from unstuck.cascade.repository import ReceiptWriteToken
from unstuck.cascade.verifier import DiagnosticReport, ReceiptVerifier
from unstuck.clock import FakeClock
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.synth import SynthesisInputs
from unstuck.decision import default_catalog
from unstuck.domain import QuerySpec, Reason, TaskSpec

#: Visibly synthetic topology values. None of these names exists.
SYNTHETIC_REGION = "us-east-1"
SYNTHETIC_ACCOUNT = "000000000000"
SYNTHETIC_RUN_TABLE = "unstuck-synthetic-runs"
SYNTHETIC_RECEIPT_TABLE = "unstuck-synthetic-receipts"
SYNTHETIC_ARTIFACT_BUCKET = "unstuck-synthetic-artifacts"
# The separate evaluator-truth bucket of design.md section 16. Its name never
# advertises its contents, so a redacted export and a leakage scan stay clean.
SYNTHETIC_HOLDOUT_BUCKET = "unstuck-synthetic-holdout"
SYNTHETIC_CONTROLLER_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/unstuck-synthetic"
)
SYNTHETIC_DECIDER_API_ID = "synth0api01"
SYNTHETIC_VPC_ENDPOINT = "vpce-0123456789abcdef0"
SYNTHETIC_RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
SYNTHETIC_FINGERPRINT = "a" * 64
BEFORE_HASH = "1" * 64
AFTER_HASH = "2" * 64

#: Markers that must never appear in a configuration export, captured request,
#: artifact body, or container content listing.
SENTINEL_MARKERS = (
    "authorization",
    "secret",
    "password",
    "api_key",
    "api-key",
    "credential",
    "bearer ",
    "evaluator",
    "hidden_cause",
    "hidden category",
    "expected_rows",
    "expected_action",
    "fixtures/evaluator",
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that reaches for a socket instead of a stub."""

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network is forbidden in cloud-platform tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket.socket, "bind", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deny the ambient credential chain, profile, and instance metadata."""
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SPEND_LIMIT_USD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/offline/config")
    monkeypatch.setenv(
        "AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/offline/credentials"
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client_factory() -> Callable[[str], Any]:
    """Build a botocore client from fixed fake region and static credentials."""

    def build(service: str) -> Any:
        return Session().create_client(
            service,
            region_name=SYNTHETIC_REGION,
            aws_access_key_id="offline-key-id",
            aws_secret_access_key="offline-signing-material",
        )

    return build


@dataclass
class StubbedClient:
    """A stubbed client plus the exact API parameters it was handed."""

    client: Any
    stubber: Stubber
    calls: list[tuple[str, dict[str, Any]]]

    def params(self, operation: str) -> list[dict[str, Any]]:
        return [captured for name, captured in self.calls if name == operation]

    def only(self, operation: str) -> dict[str, Any]:
        captured = self.params(operation)
        assert len(captured) == 1, f"{operation} called {len(captured)} times"
        return captured[0]


def _stubbed(service: str, factory: Callable[[str], Any]) -> Iterator[StubbedClient]:
    client = factory(service)
    calls: list[tuple[str, dict[str, Any]]] = []

    def record(params: dict[str, Any], model: Any, **kwargs: object) -> None:
        calls.append((str(model.name), dict(params)))

    client.meta.events.register("provide-client-params", record)
    stubber = Stubber(client)
    stubber.activate()
    try:
        yield StubbedClient(client=client, stubber=stubber, calls=calls)
    finally:
        stubber.deactivate()


@pytest.fixture
def dynamo(client_factory: Callable[[str], Any]) -> Iterator[StubbedClient]:
    yield from _stubbed("dynamodb", client_factory)


@pytest.fixture
def receipt_dynamo(client_factory: Callable[[str], Any]) -> Iterator[StubbedClient]:
    yield from _stubbed("dynamodb", client_factory)


@pytest.fixture
def s3(client_factory: Callable[[str], Any]) -> Iterator[StubbedClient]:
    yield from _stubbed("s3", client_factory)


@pytest.fixture
def task() -> TaskSpec:
    return TaskSpec(dataset_id="ds-17", order_date_utc=date(2026, 10, 1))


@pytest.fixture
def query(task: TaskSpec) -> QuerySpec:
    return QuerySpec(task=task, dataset_version="v17")


@pytest.fixture
def policy() -> CascadePolicy:
    return CascadePolicy(baseline_id="decider_memory", model_revision="scripted-v1")


@pytest.fixture
def config(policy: CascadePolicy) -> CloudResourceConfig:
    """Synthetic runtime configuration. Identifiers and versions only."""
    return CloudResourceConfig(
        region=SYNTHETIC_REGION,
        account_id=SYNTHETIC_ACCOUNT,
        run_table_name=SYNTHETIC_RUN_TABLE,
        receipt_table_name=SYNTHETIC_RECEIPT_TABLE,
        artifact_bucket=SYNTHETIC_ARTIFACT_BUCKET,
        controller_runtime_arn=SYNTHETIC_CONTROLLER_ARN,
        decider_api_id=SYNTHETIC_DECIDER_API_ID,
        decider_vpc_endpoint_id=SYNTHETIC_VPC_ENDPOINT,
        dataset_version="v17",
        model_revision="scripted-v1",
        policy_hash=policy.policy_hash,
        calibration_hash=policy.calibration.calibration_hash,
    )


@pytest.fixture
def inputs(config: CloudResourceConfig) -> SynthesisInputs:
    """Synthetic synthesis inputs. No value here names a real resource."""
    return SynthesisInputs(
        config=config,
        holdout_bucket=SYNTHETIC_HOLDOUT_BUCKET,
        decider_ami_id="ami-0123456789abcdef0",
        main_model_arn=(
            f"arn:aws:bedrock:{SYNTHETIC_REGION}:{SYNTHETIC_ACCOUNT}:"
            "inference-profile/synthetic-main"
        ),
        baseline_model_arn=(
            f"arn:aws:bedrock:{SYNTHETIC_REGION}:{SYNTHETIC_ACCOUNT}:"
            "inference-profile/synthetic-baseline"
        ),
        operator_principal_arn=(
            f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:role/offline-operator"
        ),
        evaluator_principal_arn=(
            f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:role/offline-evaluator"
        ),
        deployer_principal_arn=(
            f"arn:aws:iam::{SYNTHETIC_ACCOUNT}:role/offline-deployer"
        ),
    )


@pytest.fixture
def namespace() -> ReceiptNamespace:
    return ReceiptNamespace(arm="decider_memory", case_id="case-01")


@pytest.fixture
def other_namespace() -> ReceiptNamespace:
    return ReceiptNamespace(arm="decider", case_id="case-02")


@pytest.fixture
def write_token(clock: FakeClock) -> ReceiptWriteToken:
    """The verifier's own write capability. Tests never mint one themselves."""
    return ReceiptVerifier(clock).token


@pytest.fixture
def context(policy: CascadePolicy, query: QuerySpec) -> DecisionContext:
    return DecisionContext(
        task_hash=query.task_hash,
        query_hash=query.query_hash,
        base_query_hash=query.base_query_hash,
        dataset_id=query.task.dataset_id,
        versions=policy.versions(query.dataset_version),
        candidates=tuple(
            {
                "public_tool_id": probe.public.id,
                "description": probe.public.description,
                "available": True,
            }
            for probe in default_catalog()[:3]
        ),
    )


@pytest.fixture
def make_receipt(
    clock: FakeClock, namespace: ReceiptNamespace, context: DecisionContext
) -> Callable[..., DecisionReceipt]:
    """Mint a verifier-owned receipt. The verifier, not the test, owns outcome."""
    verifier = ReceiptVerifier(clock)

    def build(
        *,
        changed: bool = True,
        reason_code: Reason | None = None,
        receipt_namespace: ReceiptNamespace | None = None,
        capability: str = "inspect_schema_keys",
    ) -> DecisionReceipt:
        delta = EvidenceDelta(
            evidence_before_hash=BEFORE_HASH,
            evidence_after_hash=AFTER_HASH if changed else BEFORE_HASH,
            evidence_ids=(uuid4(),) if changed else (),
        )
        report = DiagnosticReport(
            diagnostic_call_id=uuid4(),
            capability=capability,
            public_tool_id=capability,
            arguments_hash=context.query_hash,
            status="evidence",
            delta=delta,
            admissible=True,
            reason_code=reason_code
            or (Reason.POPULATED_INITIAL if changed else Reason.INSUFFICIENT),
        )
        return verifier.create(
            namespace=receipt_namespace or namespace,
            decision_id=uuid4(),
            model_call_id=uuid4(),
            context=context,
            report=report,
        )

    return build


@pytest.fixture
def observed_at() -> datetime:
    return datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
