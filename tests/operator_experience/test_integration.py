"""Cross-layer integration for the operator surface. Offline, in process.

A real offline run is started through the API, read back as a spine with its
evidence panels, exported, and swept for sentinel markers. The `serve` seam is
exercised without starting a listener.
"""

import ast
import csv
import importlib
import inspect
import io
import json
from pathlib import Path

import pytest

from unstuck import cli
from unstuck.clock import FakeClock
from unstuck.operator import OperatorReason
from unstuck.operator.app import STATIC_ASSETS, create_app
from unstuck.storage import LocalRunRepository

from .conftest import JSON_HEADERS, LOOPBACK_AUTHORITY, READ_HEADERS, SENTINEL_MARKERS
from .test_endpoints import RUN_BODY, LocalClient

OPERATOR_MODULES = (
    "unstuck.operator",
    "unstuck.operator.app",
    "unstuck.operator.benchmark",
    "unstuck.operator.contracts",
    "unstuck.operator.deciders",
    "unstuck.operator.export",
    "unstuck.operator.modes",
    "unstuck.operator.projection",
    "unstuck.operator.scenarios",
    "unstuck.operator.security",
)
OUTBOUND = ("socket", "socketserver", "urllib", "urllib.request", "requests", "boto3")


@pytest.fixture
def client(repository: LocalRunRepository, clock: FakeClock) -> LocalClient:
    return LocalClient(create_app(repository=repository, clock=clock))


def test_a_complete_run_is_started_read_back_and_exported_offline(
    client: LocalClient,
) -> None:
    created = client.post("/api/runs", json=RUN_BODY, headers=JSON_HEADERS).json()
    run_id = created["run_id"]

    detail = client.get(f"/api/runs/{run_id}", headers=READ_HEADERS).json()
    assert detail["run_id"] == run_id
    assert detail["status"] in {"completed", "empty_verified", "pending", "unresolved"}

    nodes = client.get(f"/api/runs/{run_id}/spine", headers=READ_HEADERS).json()[
        "spine"
    ]
    assert [node["order"] for node in nodes] == list(range(1, len(nodes) + 1))
    for node in nodes:
        panel = client.get(
            f"/api/runs/{run_id}/evidence/{node['node_id']}", headers=READ_HEADERS
        )
        assert panel.status_code == 200
        assert panel.json()["validation"]["node"] == node["kind"]

    export = client.post(
        "/api/exports",
        json={"run_id": run_id, "format": "csv"},
        headers=JSON_HEADERS,
    )
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert rows[0]["run_id"] == run_id
    assert rows[0]["task_hash"] == detail["task_hash"]
    assert rows[0]["verification_status"] == detail["verification_status"]


def test_the_export_agrees_with_the_run_view_in_every_format_offline(
    client: LocalClient,
) -> None:
    created = client.post("/api/runs", json=RUN_BODY, headers=JSON_HEADERS).json()
    run_id = created["run_id"]
    body = client.post(
        "/api/exports",
        json={"run_id": run_id, "format": "json"},
        headers=JSON_HEADERS,
    ).json()
    record = body["manifest"]
    assert record["run_id"] == run_id
    assert record["status"] == created["status"]
    assert record["path"] == created["path"]
    assert record["selector_calls"] == created["selector_calls"]
    assert len(body["spine"]) == len(created["spine"])
    assert body["events"]


def test_a_blocked_live_run_names_its_gate_and_admits_nothing_offline(
    client: LocalClient, repository: LocalRunRepository
) -> None:
    before = list(repository.root.iterdir()) if repository.root.exists() else []
    response = client.post(
        "/api/runs", json=RUN_BODY | {"mode": "live"}, headers=JSON_HEADERS
    )
    assert response.status_code == 409
    body = response.json()
    assert body["reason_code"] == OperatorReason.LIVE_MODE_BLOCKED.value
    assert body["gate"]
    after = list(repository.root.iterdir()) if repository.root.exists() else []
    assert before == after


def test_the_benchmark_view_is_empty_and_disables_retention_offline(
    client: LocalClient,
) -> None:
    body = client.get("/api/benchmark", headers=READ_HEADERS).json()
    assert body["corpus_status"] == "empty"
    assert body["retention_enabled"] is False
    assert body["retention_decision"] == "pending"
    assert [row["arm"] for row in body["arms"]] == [
        "fixed",
        "llm",
        "decider",
        "decider_memory",
    ]
    for row in body["arms"]:
        assert row["verified_success"]["text"] == "Not measured"


def test_a_redaction_sweep_covers_every_surface_the_suite_produced_offline(
    client: LocalClient,
) -> None:
    run_id = client.post("/api/runs", json=RUN_BODY, headers=JSON_HEADERS).json()[
        "run_id"
    ]
    surfaces: list[str] = [
        client.get("/api/health", headers=READ_HEADERS).text,
        client.get("/api/benchmark", headers=READ_HEADERS).text,
        client.get(f"/api/runs/{run_id}", headers=READ_HEADERS).text,
        client.get(f"/api/runs/{run_id}/spine", headers=READ_HEADERS).text,
        client.get("/", headers=READ_HEADERS).text,
    ]
    for asset in STATIC_ASSETS:
        surfaces.append(client.get(f"/static/{asset}", headers=READ_HEADERS).text)
    for fmt in ("jsonl", "json", "csv"):
        surfaces.append(
            client.post(
                "/api/exports",
                json={"run_id": run_id, "format": fmt},
                headers=JSON_HEADERS,
            ).text
        )
    for surface in surfaces:
        lowered = surface.lower()
        for marker in SENTINEL_MARKERS:
            assert marker not in lowered, marker


def test_no_operator_module_opens_an_outbound_connection_offline() -> None:
    for name in OPERATOR_MODULES:
        module = importlib.import_module(name)
        tree = ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        offending = {item for item in imported if item in OUTBOUND}
        assert offending == set(), (name, offending)


def test_the_unit_serves_no_cors_header_offline(client: LocalClient) -> None:
    for path in ("/api/health", "/", "/static/styles.css"):
        response = client.get(path, headers=READ_HEADERS)
        for key in response.headers:
            assert not key.lower().startswith("access-control-")


def test_serve_defaults_to_loopback_and_refuses_anything_else_offline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.serve("127.0.0.1", 8081, tmp_path / "runs", run=False)
    body = json.loads(capsys.readouterr().out)
    assert code == 0
    assert body["bind"] == LOOPBACK_AUTHORITY
    assert body["url"] == f"http://{LOOPBACK_AUTHORITY}/"
    assert "loopback only" in body["exposure"]
    for host in ("0.0.0.0", "::", "192.168.1.10", "example.invalid"):
        refused = cli.serve(host, 8081, tmp_path / "runs", run=False)
        refusal = json.loads(capsys.readouterr().out)
        assert refused == 2
        assert refusal["reason_code"] == OperatorReason.NON_LOOPBACK_HOST.value


def test_the_serve_default_in_the_parser_is_loopback_offline() -> None:
    source = inspect.getsource(cli.main)
    assert '"--host", default="127.0.0.1"' in source
    assert "0.0.0.0" not in source
    signature = inspect.signature(cli.serve)
    assert set(signature.parameters) == {"host", "port", "artifact_root", "run"}


def test_the_existing_commands_are_unchanged_offline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["unstuck", "doctor"])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "passed",
        "mode": "offline",
        "provider": "mock",
        "live_checks": "Pending",
    }
    source = inspect.getsource(cli.main)
    for command in ("skeleton", "doctor", "deploy", "teardown", "serve"):
        assert f'"{command}"' in source


def test_the_app_builds_nothing_outside_the_declared_asset_list_offline(
    client: LocalClient,
) -> None:
    denied = client.get("/static/../app.py", headers=READ_HEADERS)
    assert denied.status_code in {307, 404}
    unknown = client.get("/static/secrets.env", headers=READ_HEADERS)
    assert unknown.status_code == 404
    assert unknown.json()["reason_code"] == OperatorReason.ASSET_NOT_FOUND.value


def test_the_run_service_awaits_the_controller_rather_than_nesting_a_loop_offline() -> (
    None
):
    from unstuck.operator.app import OfflineRunService

    assert inspect.iscoroutinefunction(OfflineRunService.start)
    # Scan the code, not the prose: the docstring names the trap it avoids.
    tree = ast.parse(inspect.getsource(OfflineRunService).strip())
    calls = {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "asyncio.run" not in calls
    awaited = {
        ast.unparse(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
    }
    assert any("controller.run" in item for item in awaited)
