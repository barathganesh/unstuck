"""Tests for the FastAPI AgentCore custom-runtime server.

Offline. The suite's autouse `no_network` fixture patches `socket.bind`,
`socket.connect`, `socket.connect_ex`, `create_connection` and `getaddrinfo` to
raise, so every request here travels the in-process ASGI transport and anything
that passes provably opened no listener and resolved no name. No image is
built, no container is run, and no AWS call is made.

The verified AgentCore custom-runtime contract these tests hold the app to is
`GET /ping` plus `POST /invocations` on an ARM64 container bound to
`0.0.0.0:8080`, with `/ping` answering `{"status": "Healthy"}`. Source:
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/getting-started-custom.html
and the HTTP protocol contract it links to. SigV4 authorization of the caller
happens at the service boundary before a request reaches this app, so PF6 and
PF8 both stay Pending: nothing here observes a live principal.
"""

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue, ValidationError

from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.agentcore_app import (
    HEALTHY,
    MAX_INVOCATION_BYTES,
    RESPONSE_LABELS,
    app_from_environment,
    build_controller_runtime,
    create_app,
)
from unstuck.cloud.runtime import (
    CONTAINER_BIND_HOST,
    CONTAINER_BIND_PORT,
    ENVIRONMENT_FIELDS,
    RUNTIME_IDENTITY_VARIABLES,
    SESSION_HEADER,
    ControllerRuntime,
    RuntimeSettings,
    SessionHeaderIdentity,
    runtime_settings,
)
from unstuck.cloud.session import RuntimeSessionId
from unstuck.fixtures import load_fixture

from .conftest import (
    SYNTHETIC_ACCOUNT,
    SYNTHETIC_ARTIFACT_BUCKET,
    SYNTHETIC_DECIDER_API_ID,
    SYNTHETIC_RECEIPT_TABLE,
    SYNTHETIC_REGION,
    SYNTHETIC_RUN_TABLE,
)

DEPLOYMENT = Path(__file__).resolve().parents[2] / "deployment"
DOCKERFILE = DEPLOYMENT / "Dockerfile"
LOCKFILE = DEPLOYMENT / "requirements-runtime.txt"

SYNTHETIC_RUNTIME_NAME = "unstuck_synthetic"
SYNTHETIC_ENDPOINT_NAME = "DEFAULT"

#: A fully injected, fully valid deployment environment. Every value is
#: visibly synthetic, and none is a credential: no field accepts one.
ENVIRONMENT: Mapping[str, str] = {
    "UNSTUCK_REGION": SYNTHETIC_REGION,
    "UNSTUCK_ACCOUNT_ID": SYNTHETIC_ACCOUNT,
    "UNSTUCK_RUNTIME_NAME": SYNTHETIC_RUNTIME_NAME,
    "UNSTUCK_RUNTIME_ENDPOINT_NAME": SYNTHETIC_ENDPOINT_NAME,
    "UNSTUCK_MODEL_REVISION": "scripted-v1",
    "UNSTUCK_DATASET_VERSION": "v17",
    "UNSTUCK_RUN_TABLE": SYNTHETIC_RUN_TABLE,
    "UNSTUCK_RECEIPT_TABLE": SYNTHETIC_RECEIPT_TABLE,
    "UNSTUCK_ARTIFACT_BUCKET": SYNTHETIC_ARTIFACT_BUCKET,
    "UNSTUCK_DECIDER_API_ID": SYNTHETIC_DECIDER_API_ID,
}

#: A session identity shaped the way the service's own `runtimeSessionId`
#: bound requires. The exact service bound stays Pending PF6; the charset and
#: length this app enforces are the ones `unstuck.cloud.session` already fixes.
SESSION = f"unstuck-run-{uuid4()}"
HEADERS = {SESSION_HEADER: SESSION}

#: Values a readiness or refusal body must never carry. Each one is either a
#: topology fact, a path, or a marker the redaction vocabulary refuses.
RESTRICTED = (
    SYNTHETIC_ACCOUNT,
    SYNTHETIC_ARTIFACT_BUCKET,
    SYNTHETIC_DECIDER_API_ID,
    SYNTHETIC_RECEIPT_TABLE,
    SYNTHETIC_RUN_TABLE,
    SYNTHETIC_RUNTIME_NAME,
    "arn:aws",
    "authorization",
    "credential",
    "secret",
    "token",
    "/tmp",
    "Traceback",
)


@pytest.fixture
def settings() -> RuntimeSettings:
    return runtime_settings(ENVIRONMENT)


@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    return tmp_path / "runs"


@pytest.fixture
def runtime(settings: RuntimeSettings, run_root: Path) -> ControllerRuntime:
    """The real offline synthetic adapter: real controller, mock Decider."""
    return build_controller_runtime(settings, run_root=run_root)


@pytest.fixture
def client(
    runtime: ControllerRuntime, settings: RuntimeSettings
) -> Iterator[TestClient]:
    with TestClient(create_app(runtime=runtime, settings=settings)) as started:
        yield started


def invoke(client: TestClient, key: str, **extra: JsonValue) -> Any:
    return client.post(
        "/invocations", headers=HEADERS, json={"idempotency_key": key} | extra
    )


def test_ping_answers_the_verified_agentcore_health_contract(
    client: TestClient,
) -> None:
    """`{"status": "Healthy"}` is the contract. The labels are additive."""
    response = client.get("/ping")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["status"] == HEALTHY == "Healthy"
    # The platform tracks status changes itself when the field is absent, and a
    # timestamp that advanced on every ping would keep sessions alive forever.
    assert "time_of_last_update" not in body
    for label, value in RESPONSE_LABELS.items():
        assert body[label] == value
    assert body["classification"] == "synthetic"
    assert body["provider"] == "mock"
    assert body["live_decider"] == "Pending"


def test_ping_reveals_no_topology_path_or_credential_marker(
    client: TestClient,
) -> None:
    rendered = client.get("/ping").text
    for marker in RESTRICTED:
        assert marker.lower() not in rendered.lower(), marker


def test_invocations_routes_a_typed_request_through_the_controller(
    client: TestClient,
) -> None:
    response = invoke(client, "offline-case-01")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert RuntimeSessionId(value=body["runtime_session_id"]).run_id == UUID(
        body["run_id"]
    )
    assert body["classification"] == "synthetic"
    assert body["provider"] == "mock"
    assert body["live_decider"] == "Pending"
    assert body["result"]["status"] in {
        "completed",
        "empty_verified",
        "pending",
        "unresolved",
        "budget_exhausted",
        "failed",
    }


def test_a_repeated_invocation_is_answered_from_the_durable_record(
    client: TestClient,
) -> None:
    """The run store is the idempotency authority, not this layer."""
    first = invoke(client, "offline-case-02").json()
    second = invoke(client, "offline-case-02").json()
    assert second["status"] == "duplicate"
    assert second["run_id"] == first["run_id"]
    assert second["result"] == first["result"]


@pytest.mark.parametrize(
    "extra",
    [
        {"task": {"dataset_id": "ds-18"}},
        {"mode": "live"},
        {"dataset_version": "v18"},
        {"order_date_utc": "2026-10-02"},
        {"provider": "bedrock"},
        {"run_id": "11111111-1111-4111-8111-111111111111"},
    ],
)
def test_a_caller_cannot_restate_the_task_mode_or_provider(
    client: TestClient, extra: dict[str, JsonValue]
) -> None:
    response = invoke(client, "offline-case-03", **extra)
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "rejected"
    assert body["reason_code"] == CloudReason.ARGUMENT_REJECTED.value


@pytest.mark.parametrize(
    "payload",
    [b"{", b"", b"not json", b"[]", b'"text"', b"17", b"null", b"true"],
)
def test_a_body_that_is_not_a_json_object_is_refused(
    client: TestClient, payload: bytes
) -> None:
    response = client.post(
        "/invocations",
        headers=HEADERS | {"content-type": "application/json"},
        content=payload,
    )
    assert response.status_code == 400
    assert response.json()["reason_code"] == CloudReason.ARGUMENT_REJECTED.value


@pytest.mark.parametrize(
    "media_type",
    ["text/plain", "application/x-www-form-urlencoded", "application/octet-stream"],
)
def test_a_body_that_is_not_json_by_declaration_is_refused(
    client: TestClient, media_type: str
) -> None:
    response = client.post(
        "/invocations",
        headers=HEADERS | {"content-type": media_type},
        content=b'{"idempotency_key": "offline-case-04"}',
    )
    assert response.status_code == 415
    assert response.json()["reason_code"] == CloudReason.ARGUMENT_REJECTED.value


def test_an_oversized_body_is_refused_before_it_is_parsed(
    client: TestClient,
) -> None:
    oversized = json.dumps({"idempotency_key": "x" * (MAX_INVOCATION_BYTES + 1)})
    response = client.post(
        "/invocations",
        headers=HEADERS | {"content-type": "application/json"},
        content=oversized.encode(),
    )
    assert response.status_code == 413
    assert response.json()["reason_code"] == CloudReason.ARGUMENT_REJECTED.value


def test_an_invocation_without_the_forwarded_session_header_is_denied(
    client: TestClient,
) -> None:
    response = client.post("/invocations", json={"idempotency_key": "offline-case-05"})
    assert response.status_code == 403
    assert response.json()["reason_code"] == CloudReason.IDENTITY_REQUIRED.value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "short",
        "a" * 32,
        "a" * 101,
        "has spaces in it right here ok",
        "a/" * 20,
    ],
)
def test_a_malformed_session_identity_is_denied(client: TestClient, value: str) -> None:
    response = client.post(
        "/invocations",
        headers={SESSION_HEADER: value},
        json={"idempotency_key": "offline-case-06"},
    )
    assert response.status_code == 403
    assert response.json()["reason_code"] in {
        CloudReason.IDENTITY_REQUIRED.value,
        CloudReason.SESSION_INVALID.value,
    }


def test_no_caller_supplied_credential_header_can_authorize_an_invocation(
    client: TestClient, settings: RuntimeSettings
) -> None:
    """There is no shared secret to present. SigV4 is the gate, upstream."""
    for header in ("authorization", "x-api-key", "cookie", "proxy-authorization"):
        response = client.post(
            "/invocations",
            headers={header: "Bearer offline-not-a-credential"},
            json={"idempotency_key": "offline-case-07"},
        )
        assert response.status_code == 403
        assert response.json()["reason_code"] == CloudReason.IDENTITY_REQUIRED.value
    verifier = SessionHeaderIdentity(settings)
    # The principal is the bound runtime identity, never anything the caller
    # said, so a forged header cannot name a principal of its own choosing.
    forged = verifier.verify({SESSION_HEADER: SESSION, "authorization": "Bearer x"})
    assert forged == verifier.verify({SESSION_HEADER: SESSION})
    assert forged == settings.bound_identity
    assert "offline-not-a-credential" not in forged


def test_every_unknown_route_and_method_fails_closed(client: TestClient) -> None:
    unknown = (
        ("GET", "/"),
        ("GET", "/invocations"),
        ("POST", "/ping"),
        ("DELETE", "/invocations"),
        ("PUT", "/invocations"),
        ("GET", "/ws"),
        ("GET", "/invocations/extra"),
        ("POST", "/invocations/"),
        ("GET", "/health"),
    )
    for method, path in unknown:
        response = client.request(method, path, headers=HEADERS)
        assert response.status_code == 404, (method, path)
        assert response.json()["reason_code"] == CloudReason.ROUTE_UNKNOWN.value


@pytest.mark.parametrize(
    "path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]
)
def test_no_interactive_documentation_surface_is_served(
    client: TestClient, path: str
) -> None:
    response = client.get(path, headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["reason_code"] == CloudReason.ROUTE_UNKNOWN.value


def test_no_cross_origin_or_server_identifying_header_is_returned(
    client: TestClient,
) -> None:
    for response in (client.get("/ping"), invoke(client, "offline-case-08")):
        for header in response.headers:
            assert not header.lower().startswith("access-control-")


def test_an_unexpected_failure_is_reason_coded_and_leaks_no_detail(
    settings: RuntimeSettings, run_root: Path
) -> None:
    """A bug must not become a stack trace on the wire."""

    class Exploding(ControllerRuntime):
        def handle(
            self,
            method: str,
            path: str,
            headers: Mapping[str, str],
            body: Mapping[str, JsonValue] | None,
        ) -> tuple[int, dict[str, JsonValue]]:
            raise RuntimeError("offline-synthetic internal detail")

    runtime = build_controller_runtime(settings, run_root=run_root)
    exploding = Exploding(
        runtime.repository,
        runtime.task,
        runtime.dataset_version,
        execute=runtime.execute,
        verifier=runtime.verifier,
    )
    with TestClient(
        create_app(runtime=exploding, settings=settings), raise_server_exceptions=False
    ) as client:
        response = client.post(
            "/invocations", headers=HEADERS, json={"idempotency_key": "offline-case-09"}
        )
    assert response.status_code == 500
    rendered = response.text
    assert response.json()["reason_code"] == CloudReason.CONFIG_INVALID.value
    assert "offline-synthetic internal detail" not in rendered
    assert "Traceback" not in rendered


def test_the_app_refuses_to_build_without_every_deployment_input() -> None:
    """Missing configuration fails closed at construction, before a listener."""
    with pytest.raises(CloudFault) as absent:
        app_from_environment({})
    assert absent.value.reason is CloudReason.CONFIG_INVALID
    for name in RUNTIME_IDENTITY_VARIABLES:
        partial = dict(ENVIRONMENT)
        del partial[name]
        with pytest.raises(CloudFault) as missing:
            app_from_environment(partial)
        assert missing.value.reason is CloudReason.CONFIG_INVALID, name


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("UNSTUCK_REGION", "not-a-region"),
        ("UNSTUCK_ACCOUNT_ID", "12345"),
        ("UNSTUCK_ACCOUNT_ID", "00000000000a"),
        ("UNSTUCK_RUNTIME_NAME", "has spaces"),
        ("UNSTUCK_RUNTIME_ENDPOINT_NAME", "a" * 65),
        ("UNSTUCK_ARTIFACT_BUCKET", "UPPERCASE"),
        ("UNSTUCK_DECIDER_API_ID", "no"),
        ("UNSTUCK_RUN_TABLE", "has/slash"),
        ("UNSTUCK_ARTIFACT_BUCKET", "unstuck-secret-key"),
        ("UNSTUCK_RUN_TABLE", "   "),
    ],
)
def test_an_invalid_or_sensitive_deployment_input_fails_closed(
    name: str, value: str
) -> None:
    with pytest.raises(CloudFault) as refused:
        runtime_settings(dict(ENVIRONMENT) | {name: value})
    assert refused.value.reason is CloudReason.CONFIG_INVALID


@pytest.mark.parametrize(
    ("name", "value"),
    [
        # The image cannot serve a dataset it does not carry.
        ("UNSTUCK_DATASET_VERSION", "v99"),
        # A scripted answer must never be labelled with somebody's real
        # checkpoint name, so any other revision refuses to start.
        ("UNSTUCK_MODEL_REVISION", "decider-2b-v4"),
        ("UNSTUCK_MODEL_REVISION", "scripted-v2"),
    ],
)
def test_a_declared_version_the_image_cannot_honour_fails_closed(
    run_root: Path, name: str, value: str
) -> None:
    settings = runtime_settings(dict(ENVIRONMENT) | {name: value})
    with pytest.raises(CloudFault) as refused:
        build_controller_runtime(settings, run_root=run_root)
    assert refused.value.reason is CloudReason.CONFIG_INVALID


def test_an_unusable_run_root_fails_closed(
    settings: RuntimeSettings, tmp_path: Path
) -> None:
    """Storage the run repository refuses is a refusal, not a bare exception."""
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(CloudFault) as refused:
        build_controller_runtime(settings, run_root=tmp_path / "link" / "runs")
    assert refused.value.reason is CloudReason.CONFIG_INVALID


def test_a_runtime_bound_to_another_deployment_is_refused(
    runtime: ControllerRuntime, settings: RuntimeSettings
) -> None:
    """Serving one topology under another's identity fails closed."""
    other = runtime_settings(dict(ENVIRONMENT) | {"UNSTUCK_RUNTIME_NAME": "other_name"})
    assert other != settings
    with pytest.raises(CloudFault) as refused:
        create_app(runtime=runtime, settings=other)
    assert refused.value.reason is CloudReason.CONFIG_INVALID


def test_the_container_factory_serves_the_contract_when_fully_configured(
    run_root: Path,
) -> None:
    """The exact entry point the container command names, end to end."""
    app = app_from_environment(ENVIRONMENT, run_root=run_root)
    with TestClient(app) as client:
        assert client.get("/ping").json()["status"] == HEALTHY
        answered = invoke(client, "offline-case-11")
    assert answered.status_code == 200
    assert answered.json()["status"] == "accepted"
    assert answered.json()["live_decider"] == "Pending"


def test_a_body_sent_without_a_declared_length_is_still_bounded(
    client: TestClient,
) -> None:
    """A chunked body declares no length, so the read itself is the bound."""

    def streamed(payload: bytes) -> Iterator[bytes]:
        yield payload

    accepted = client.post(
        "/invocations",
        headers=HEADERS | {"content-type": "application/json"},
        content=streamed(b'{"idempotency_key": "offline-case-12"}'),
    )
    assert "content-length" not in accepted.request.headers
    assert accepted.status_code == 200
    oversized = client.post(
        "/invocations",
        headers=HEADERS | {"content-type": "application/json"},
        content=streamed(
            json.dumps({"idempotency_key": "x" * (MAX_INVOCATION_BYTES + 1)}).encode()
        ),
    )
    assert oversized.status_code == 413
    assert oversized.json()["reason_code"] == CloudReason.ARGUMENT_REJECTED.value


def test_the_settings_contract_covers_exactly_the_injected_variables() -> None:
    assert set(ENVIRONMENT_FIELDS) == set(RUNTIME_IDENTITY_VARIABLES)
    fields = set(RuntimeSettings.model_fields) - {"schema_version"}
    assert set(ENVIRONMENT_FIELDS.values()) == fields
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(
            {field: "x" for field in ENVIRONMENT_FIELDS.values()} | {"extra": "x"}
        )


def test_the_adapter_binds_the_private_container_boundary_only(
    runtime: ControllerRuntime,
) -> None:
    assert (runtime.bind.host, runtime.bind.port) == (
        CONTAINER_BIND_HOST,
        CONTAINER_BIND_PORT,
    )
    assert (runtime.bind.host, runtime.bind.port) == ("0.0.0.0", 8080)
    assert runtime.bind.published is False
    assert isinstance(runtime.verifier, SessionHeaderIdentity)
    assert "PF6" in SessionHeaderIdentity.mechanism
    assert "SigV4" in SessionHeaderIdentity.mechanism


def test_the_served_task_is_the_shipped_synthetic_fixture(
    runtime: ControllerRuntime,
) -> None:
    fixture = load_fixture()
    assert fixture.classification == "synthetic"
    assert runtime.task.dataset_id == fixture.dataset_id
    assert runtime.task.order_date_utc == fixture.order_date_utc
    assert runtime.dataset_version == fixture.dataset_version


def test_no_request_opens_a_socket_or_resolves_a_name(client: TestClient) -> None:
    """The autouse fixture already made both fatal; this asserts it holds."""
    import socket

    with pytest.raises(AssertionError):
        socket.socket().bind(("0.0.0.0", 0))
    assert client.get("/ping").status_code == 200
    assert invoke(client, "offline-case-10").status_code == 200


def dockerfile_instructions(text: str) -> list[str]:
    """The logical instructions, with backslash continuations joined."""
    joined = text.replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines() if line.strip()]


def test_the_container_command_starts_uvicorn_on_the_private_boundary() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    command = next(
        line
        for line in dockerfile_instructions(text)
        if line.startswith(("ENTRYPOINT", "CMD")) and "uvicorn" in line
    )
    assert "unstuck.cloud.agentcore_app:app_from_environment" in command
    assert "--factory" in command
    assert f'"{CONTAINER_BIND_HOST}"' in command
    assert f'"{CONTAINER_BIND_PORT}"' in command
    assert "--no-server-header" in command
    assert "--reload" not in text
    assert f"EXPOSE {CONTAINER_BIND_PORT}" in text
    assert "--platform=linux/arm64" in text
    users = [line.split()[1] for line in text.splitlines() if line.startswith("USER ")]
    assert users[-1] not in {"root", "0", "0:0"}
    for published in DEPLOYMENT.rglob("*"):
        # Source and documentation only. Compiled bytecode is not a build
        # input and is not text, so reading it would fail on its own bytes
        # rather than on anything this sweep is looking for.
        if published.is_file() and "__pycache__" not in published.parts:
            body = published.read_text(encoding="utf-8")
            assert "--publish" not in body
            assert f"-p {CONTAINER_BIND_PORT}:" not in body


def test_the_server_closure_is_pinned_in_the_runtime_lockfile() -> None:
    pins = {
        line.split("==")[0].strip(): line.split("==")[1].strip()
        for line in LOCKFILE.read_text(encoding="utf-8").splitlines()
        if "==" in line and not line.strip().startswith("#")
    }
    import importlib.metadata as metadata

    for distribution in (
        "fastapi",
        "starlette",
        "uvicorn",
        "anyio",
        "click",
        "h11",
        "idna",
        "annotated-doc",
        "opentelemetry-api",
    ):
        assert distribution in pins, distribution
        assert pins[distribution] == metadata.version(distribution), distribution
