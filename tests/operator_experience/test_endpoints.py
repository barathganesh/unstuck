"""Endpoint tests over the ASGI app. Offline, in process, no socket."""

import asyncio
from typing import Any
from uuid import uuid4

import httpx
import pytest

from unstuck.clock import FakeClock
from unstuck.operator import OperatorReason
from unstuck.operator.app import create_app
from unstuck.storage import LocalRunRepository

from .conftest import (
    JSON_HEADERS,
    LOOPBACK_AUTHORITY,
    LOOPBACK_ORIGIN,
    READ_HEADERS,
    SENTINEL_MARKERS,
)

RUN_BODY = {"scenario": "repairable-join", "idempotency_key": "offline-ui-01"}


class LocalClient:
    """Drives the app in process over an ASGI transport. No socket exists."""

    def __init__(self, app: Any):
        self.app = app

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async def call() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url=LOOPBACK_ORIGIN
            ) as session:
                return await session.request(method, path, **kwargs)

        return asyncio.run(call())

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)


@pytest.fixture
def client(repository: LocalRunRepository, clock: FakeClock) -> LocalClient:
    return LocalClient(create_app(repository=repository, clock=clock))


def start(client: LocalClient) -> dict[str, Any]:
    response = client.post("/api/runs", json=RUN_BODY, headers=JSON_HEADERS)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_health_reports_the_local_bind_and_mode_offline(client: LocalClient) -> None:
    response = client.get("/api/health", headers=READ_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["bind"] == LOOPBACK_AUTHORITY
    assert body["mode"] == "offline"
    assert body["provider"] == "mock"
    assert not any(
        key.lower().startswith("access-control-allow") for key in response.headers
    )


def test_a_run_starts_once_and_returns_its_truthful_view_offline(
    client: LocalClient,
) -> None:
    body = start(client)
    assert body["classification"] == "synthetic"
    assert body["mode"]["requested"] == "offline"
    assert body["path"] in {"direct", "re_decided", "no_cascade"}
    assert body["spine"][0]["kind"] == "task"
    assert body["spine"][-1]["kind"] == "result"
    assert body["verification_status"] in {"passed", "failed", "not_run"}


def test_a_duplicate_start_resumes_rather_than_creating_a_second_run_offline(
    client: LocalClient,
) -> None:
    first = start(client)
    second = start(client)
    assert first["run_id"] == second["run_id"]
    assert first["task_hash"] == second["task_hash"]


def test_a_run_is_readable_back_with_its_spine_and_evidence_offline(
    client: LocalClient,
) -> None:
    run_id = start(client)["run_id"]
    detail = client.get(f"/api/runs/{run_id}", headers=READ_HEADERS)
    assert detail.status_code == 200
    assert detail.json()["run_id"] == run_id
    spine = client.get(f"/api/runs/{run_id}/spine", headers=READ_HEADERS)
    assert spine.status_code == 200
    nodes = spine.json()["spine"]
    assert [node["order"] for node in nodes] == list(range(1, len(nodes) + 1))
    panel = client.get(
        f"/api/runs/{run_id}/evidence/verification", headers=READ_HEADERS
    )
    assert panel.status_code == 200
    assert panel.json()["node_id"] == "verification"


def test_an_unknown_run_or_node_fails_closed_offline(client: LocalClient) -> None:
    missing = client.get(f"/api/runs/{uuid4()}", headers=READ_HEADERS)
    assert missing.status_code == 404
    assert missing.json()["reason_code"] == OperatorReason.RUN_NOT_FOUND.value
    run_id = start(client)["run_id"]
    node = client.get(f"/api/runs/{run_id}/evidence/not-a-node", headers=READ_HEADERS)
    assert node.status_code == 404
    assert node.json()["reason_code"] == OperatorReason.NODE_NOT_FOUND.value


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "text/html; charset=utf-8"),
        ("/static/index.html", "text/html; charset=utf-8"),
        ("/static/app.js", "text/javascript; charset=utf-8"),
        ("/static/styles.css", "text/css; charset=utf-8"),
    ],
)
def test_every_static_asset_is_served_as_its_own_type_offline(
    client: LocalClient, path: str, expected: str
) -> None:
    served = client.get(path, headers=READ_HEADERS)
    assert served.status_code == 200
    assert served.headers["content-type"] == expected


def test_an_unlisted_static_asset_fails_closed_offline(client: LocalClient) -> None:
    denied = client.get("/static/secrets.env", headers=READ_HEADERS)
    assert denied.status_code == 404
    assert denied.json()["reason_code"] == OperatorReason.ASSET_NOT_FOUND.value


@pytest.mark.parametrize(
    "path",
    [
        "/api/health",
        "/api/benchmark",
        "/",
        "/static/app.js",
    ],
)
def test_every_read_route_requires_the_exact_host_offline(
    client: LocalClient, path: str
) -> None:
    denied = client.get(path, headers={"host": "evil.invalid"})
    assert denied.status_code == 403
    assert denied.json()["reason_code"] == OperatorReason.HOST_REJECTED.value
    allowed = client.get(path, headers=READ_HEADERS)
    assert allowed.status_code == 200


@pytest.mark.parametrize("path", ["/api/runs", "/api/exports"])
def test_every_write_route_requires_origin_and_json_offline(
    client: LocalClient, path: str
) -> None:
    no_origin = client.post(
        path,
        json={},
        headers={"host": LOOPBACK_AUTHORITY, "content-type": "application/json"},
    )
    assert no_origin.status_code == 403
    assert no_origin.json()["reason_code"] == OperatorReason.ORIGIN_REJECTED.value
    wrong_type = client.post(
        path,
        content=b"scenario=x",
        headers={
            "host": LOOPBACK_AUTHORITY,
            "origin": LOOPBACK_ORIGIN,
            "content-type": "application/x-www-form-urlencoded",
        },
    )
    assert wrong_type.status_code == 415
    assert wrong_type.json()["reason_code"] == OperatorReason.MEDIA_TYPE_REJECTED.value


def test_a_request_carrying_a_task_argument_is_rejected_offline(
    client: LocalClient,
) -> None:
    response = client.post(
        "/api/runs",
        json=RUN_BODY | {"dataset_id": "ds-18"},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["reason_code"] == OperatorReason.ARGUMENT_REJECTED.value


def test_a_live_run_is_blocked_and_names_its_gate_offline(
    client: LocalClient,
) -> None:
    response = client.post(
        "/api/runs", json=RUN_BODY | {"mode": "live"}, headers=JSON_HEADERS
    )
    assert response.status_code == 409
    body = response.json()
    assert body["reason_code"] == OperatorReason.LIVE_MODE_BLOCKED.value
    assert body["gate"]
    assert "offline" not in body.get("effective", "") if body.get("effective") else True


def test_no_response_body_carries_a_sentinel_marker_offline(
    client: LocalClient,
) -> None:
    run_id = start(client)["run_id"]
    paths = [
        "/api/health",
        "/api/benchmark",
        f"/api/runs/{run_id}",
        f"/api/runs/{run_id}/spine",
        f"/api/runs/{run_id}/evidence/verification",
        f"/api/runs/{run_id}/evidence/diagnostic",
        f"/api/runs/{run_id}/evidence/result",
    ]
    for path in paths:
        rendered = client.get(path, headers=READ_HEADERS).text.lower()
        for marker in SENTINEL_MARKERS:
            assert marker not in rendered, (path, marker)


def test_the_app_installs_no_cors_middleware_offline(
    repository: LocalRunRepository, clock: FakeClock
) -> None:
    app = create_app(repository=repository, clock=clock)
    rendered = " ".join(
        type(item.cls).__name__ + str(item.cls) for item in app.user_middleware
    )
    assert "CORS" not in rendered
    assert not any("cors" in str(item.cls).lower() for item in app.user_middleware)
