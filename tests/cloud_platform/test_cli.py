"""Tests for the offline `deploy` and `teardown` command seams.

Neither command can reach a live mode in this unit: a deploy without
`--synth-only` and a teardown without `--dry-run` both refuse with
`owner_approval_required`. The existing `skeleton` and `doctor` commands are
unchanged (REQ-OPS-01, REQ-OPS-05).
"""

import ast
import inspect
import json
from pathlib import Path

import pytest

from unstuck import cli
from unstuck.cloud.synth import SynthesisInputs

EXAMPLE_CONFIG = (
    Path(__file__).resolve().parents[2] / "deployment" / "cloud-config.example.json"
)


def run(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *arguments: str,
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr("sys.argv", ["unstuck", *arguments])
    code = cli.main()
    return code, json.loads(capsys.readouterr().out)


def test_the_example_configuration_validates_offline() -> None:
    inputs = SynthesisInputs.model_validate_json(
        EXAMPLE_CONFIG.read_text(encoding="utf-8")
    )
    assert inputs.config.account_id == "000000000000"
    assert inputs.holdout_bucket != inputs.config.artifact_bucket


def test_deploy_without_synth_only_refuses_for_owner_approval_offline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, body = run(capsys, monkeypatch, "deploy")
    assert code == 2
    assert body["reason_code"] == "owner_approval_required"
    assert "owner approval" in str(body["detail"])


def test_deploy_synth_only_synthesizes_and_reports_pending_offline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, body = run(
        capsys,
        monkeypatch,
        "deploy",
        "--synth-only",
        "--cloud-config",
        str(EXAMPLE_CONFIG),
        "--out",
        str(tmp_path / "cdk.out"),
    )
    assert code == 0
    assert body["status"] == "synthesized"
    assert body["mode"] == "offline"
    assert "Pending" in str(body["deployment"])
    diff = body["design_15_policy_diff"]
    assert isinstance(diff, dict)
    assert all(not any(findings.values()) for findings in diff.values()), diff
    live = body["live_checks"]
    assert isinstance(live, list)
    assert {entry["status"] for entry in live} == {"pending"}


def test_deploy_refuses_an_unreadable_configuration_offline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, body = run(
        capsys,
        monkeypatch,
        "deploy",
        "--synth-only",
        "--cloud-config",
        str(tmp_path / "absent.json"),
    )
    assert code == 2
    assert body["reason_code"] == "cloud_config_invalid"


def test_teardown_without_dry_run_refuses_for_owner_approval_offline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, body = run(capsys, monkeypatch, "teardown", "--retain-artifacts")
    assert code == 2
    assert body["reason_code"] == "owner_approval_required"


def test_teardown_dry_run_without_retain_artifacts_is_refused_offline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, body = run(
        capsys,
        monkeypatch,
        "teardown",
        "--dry-run",
        "--cloud-config",
        str(EXAMPLE_CONFIG),
        "--out",
        str(tmp_path / "cdk.out"),
    )
    assert code == 2
    assert body["reason_code"] == "retention_protected"
    assert "deletion_protection" in str(body["detail"])


def test_teardown_dry_run_plans_an_ordered_removal_offline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, body = run(
        capsys,
        monkeypatch,
        "teardown",
        "--retain-artifacts",
        "--dry-run",
        "--cloud-config",
        str(EXAMPLE_CONFIG),
        "--out",
        str(tmp_path / "cdk.out"),
    )
    assert code == 0
    assert body["status"] == "planned"
    steps = body["steps"]
    assert isinstance(steps, list)
    assert [step["order"] for step in steps] == list(range(1, len(steps) + 1))
    assert body["retained"]
    assert "Pending" in str(body["execution"])


def test_the_existing_commands_are_unchanged_offline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, body = run(capsys, monkeypatch, "doctor")
    assert code == 0
    assert body == {
        "status": "passed",
        "mode": "offline",
        "provider": "mock",
        "live_checks": "Pending",
    }
    assert inspect.signature(cli.skeleton).parameters.keys() == {"artifact_root"}


def test_no_command_can_reach_a_live_mode_offline() -> None:
    tree = ast.parse(inspect.getsource(cli))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(name.startswith(("boto3", "botocore")) for name in imported)
    choices = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "choices"
    ]
    rendered = [ast.unparse(node.value) for node in choices]
    assert "('offline',)" in rendered
    assert any("'deploy'" in item and "'teardown'" in item for item in rendered)
