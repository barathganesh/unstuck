# Unit command: UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/operator_experience -q
"""Offline fixtures for the operator-experience unit.

Every test here runs in process. The application is exercised through an ASGI
transport, so no listener is started and no port is bound. An autouse guard
fails any test that reaches for a socket.

Domain objects come from the production factories the core-runtime suite uses,
so no contract is duplicated here. A run under test is produced by the real
repository in a temporary directory rather than hand-assembled.
"""

import asyncio
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from unstuck.clock import FakeClock
from unstuck.domain import TaskSpec
from unstuck.fixtures import load_fixture
from unstuck.storage import LocalRunRepository, Snapshot

LOOPBACK_PORT = 8081
LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_AUTHORITY = f"{LOOPBACK_HOST}:{LOOPBACK_PORT}"
LOOPBACK_ORIGIN = f"http://{LOOPBACK_AUTHORITY}"

#: Markers that must never appear in a response, an export, or a static asset.
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
    "jev",
    "decider_jev",
    "bma",
)

JSON_HEADERS = {
    "host": LOOPBACK_AUTHORITY,
    "origin": LOOPBACK_ORIGIN,
    "content-type": "application/json",
}
READ_HEADERS = {"host": LOOPBACK_AUTHORITY}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a socket instead of using the ASGI transport."""

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network is forbidden in operator-experience tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket.socket, "bind", denied)
    monkeypatch.setattr(socket.socket, "listen", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "create_server", denied)


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_SPEND_LIMIT_USD",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def repository(tmp_path: Path, clock: FakeClock) -> LocalRunRepository:
    return LocalRunRepository(tmp_path / "runs", clock=clock)


@pytest.fixture
def task() -> TaskSpec:
    fixture = load_fixture()
    return TaskSpec(
        dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
    )


@pytest.fixture
def completed_run(
    repository: LocalRunRepository, clock: FakeClock, task: TaskSpec
) -> Snapshot:
    """A real finished run, produced by the real controller. Offline only."""
    from unstuck.cli import OfflineDecider
    from unstuck.controller import Controller
    from unstuck.query import EvidenceEngine

    fixture = load_fixture()
    snapshot = repository.create_run(task, fixture.dataset_version, "offline-ui-01")
    engine = EvidenceEngine(fixture, clock=clock)
    try:
        controller = Controller(repository, engine, OfflineDecider(), clock=clock)
        asyncio.run(controller.run(snapshot.state.run_id))
    finally:
        engine.close()
    return repository.load_run(snapshot.state.run_id)


@pytest.fixture
def client_factory() -> Callable[[Any], Iterator[Any]]:
    """Build an in-process ASGI client. No socket is created."""

    def build(app: Any) -> Iterator[Any]:
        import httpx

        transport = httpx.ASGITransport(app=app)
        with httpx.Client(transport=transport, base_url=LOOPBACK_ORIGIN) as client:  # type: ignore[arg-type]
            yield client

    return build
