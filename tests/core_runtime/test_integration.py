import asyncio
import json
import subprocess
import sys
from pathlib import Path

from unstuck.cli import skeleton


def test_TST_40_offline_walking_skeleton(tmp_path):
    result = asyncio.run(skeleton(tmp_path))
    assert result["status"] == "completed"
    assert result["verification_status"] == "passed"
    assert [(row["country_code"], row["units"]) for row in result["rows"]] == [
        ("IN", 4),
        ("US", 5),
    ]
    events = [
        json.loads(line)
        for line in (tmp_path / result["run_id"] / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    assert events[-1]["payload"] == result
    assert all(
        event["mode"] == "offline" and event["provider"] == "mock" for event in events
    )
    assert events[-1]["budgets"]["history_lookups"] == 0
    assert events[-1]["budgets"]["report_queries"] == 2


def test_TST_26_cli_offline_and_refuses_live(tmp_path):
    command = [
        sys.executable,
        "-m",
        "unstuck.cli",
        "skeleton",
        "--artifact-root",
        str(tmp_path),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["reason_code"] == "populated_after_repair"
    for mode in ("live", "replay"):
        refused = subprocess.run(
            command + ["--mode", mode], text=True, capture_output=True, check=False
        )
        assert refused.returncode != 0
    assert not (tmp_path / "forbidden.csv").exists()


#: The one module that may name the AWS SDK. It is the serving layer of the
#: AgentCore container: reaching AWS is its whole purpose, and it is already
#: the one module exempt from the "no cloud module imports anything that can
#: open a socket" guard of `tests/cloud_platform/test_integration.py` for the
#: same reason. Every other forbidden term below still applies to it, and the
#: test below bounds the exemption to one named function rather than leaving it
#: open, so the library modules of the wheel are checked more tightly than a
#: blanket text search checked them.
SDK_SERVING_LAYER = ("src", "unstuck", "cloud", "agentcore_app.py")


def test_TST_25_package_source_excludes_runtime_truth():
    root = Path(__file__).parents[2] / "src" / "unstuck"
    for path in root.rglob("*"):
        if path.suffix in {".py", ".json"}:
            text = path.read_text().lower()
            serving = path.parts[-len(SDK_SERVING_LAYER) :] == SDK_SERVING_LAYER
            for term in (
                "fixtures/evaluator",
                "expected_rows",
                "hidden_category",
                "bma",
                "boto3",
                "requests.get",
            ):
                if term == "boto3" and serving:
                    continue
                assert term not in text
            if "cascade" not in path.parts:
                assert "jev" not in text


def test_TST_25_the_sdk_exemption_is_bounded_to_one_function():
    """The serving layer may build a client, in one place, and nowhere else.

    This is the compensating bound for the exemption above. The AWS SDK is
    imported inside exactly one function, that function is the only place a
    client is constructed, and it passes no credential, profile, endpoint
    override, or signature override: the SDK resolves the AgentCore execution
    role's own SigV4 identity.
    """
    import ast

    path = Path(__file__).parents[2].joinpath(*SDK_SERVING_LAYER)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_level = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level.add(node.module)
    assert not any(name.startswith("boto") for name in module_level), module_level

    holders = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(inner, (ast.Import, ast.ImportFrom))
            and any(
                alias.name.startswith("boto") for alias in getattr(inner, "names", [])
            )
            or (
                isinstance(inner, ast.ImportFrom)
                and (inner.module or "").startswith("boto")
            )
            for inner in ast.walk(node)
        )
    }
    assert holders == {"bounded_client"}, holders

    built = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(".client")
    ]
    assert len(built) == 1
    passed = {keyword.arg for keyword in built[0].keywords}
    assert passed == {"region_name", "config"}, passed
    source = path.read_text(encoding="utf-8")
    for forbidden in (
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "profile_name",
        "endpoint_url",
        "signature_version",
    ):
        assert forbidden not in source, forbidden
