"""Emitted telemetry and server-owned scenario selection on the hosted surface.

Offline. Every request here travels the in-process ASGI transport, no image is
built, no container is run, and no AWS call is made. The autouse `no_network`
fixture of this suite's `conftest.py` makes that provable rather than asserted.

Two gaps in the first hosted runtime are closed here and both are tested from
the outside. The serving layer attached no telemetry sink, so an invocation
produced no structured event line at all and the response body was the only
record of what happened. And the task is server-owned, which is correct, but
the server owned exactly one path, so the Decision Receipt behaviour the
product is about was unreachable once deployed.

The scenario is still not the caller's. It arrives as a deployment input, it is
resolved against the declared allowlist before a listener exists, and the
request contract is unchanged: an idempotency key and nothing else, with an
extra field refused rather than ignored.

Nothing here observes a live principal or a live provider. The deployment
serves the deterministic stand-in, so PF1, PF2, PF6 and PF8 all stay Pending
and no figure below is a measurement of anything.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/cloud_platform/test_agentcore_telemetry.py -q
"""

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.agentcore_app import (
    DEFAULT_SCENARIO,
    SCENARIO_VARIABLE,
    app_from_environment,
    build_controller_runtime,
    create_app,
    runtime_scenario,
)
from unstuck.cloud.runtime import (
    MOCK_MODEL_REVISION,
    ControllerRuntime,
    RuntimeSettings,
    runtime_settings,
)
from unstuck.operator.scenarios import LOCAL_ARM, SCENARIO_TOKENS
from unstuck.telemetry.collector import PUBLIC_LINE_PREFIX, public_line_payload
from unstuck.telemetry.contracts import TelemetryRecord, unsafe_public_fields

from .test_agentcore_app import ENVIRONMENT, HEADERS, SESSION

#: Values no emitted line may carry. The response-body list is deliberately not
#: reused: it refuses the bare word `token`, which a telemetry record carries
#: legitimately as the field names `input_tokens` and `output_tokens`, both of
#: which are declared `not_supplied` here because no provider was called.
FORBIDDEN_IN_LINES = (
    "arn:aws",
    "authorization",
    "bearer",
    "credential",
    "secret",
    "access_key",
    "api_key",
    "evaluator",
    "hidden_cause",
    "expected_rows",
    "/tmp",
    "/Users/",
    "Traceback",
)

#: The one deployment input an emitted line may carry, and why. The dataset
#: version is not hosting identity: the image carries exactly one synthetic
#: dataset and the invocation response already states it. Everything else
#: injected names the owner's resources or the model that was asked, and is
#: asserted absent: the model revision is hashed by the public projection, so
#: no source model identity reaches a line whether or not the served checkpoint
#: is a stand-in.
DISCLOSED_INPUTS = ("UNSTUCK_DATASET_VERSION",)


@pytest.fixture
def settings() -> RuntimeSettings:
    return runtime_settings(ENVIRONMENT)


def runtime_for(
    settings: RuntimeSettings, run_root: Path, scenario: str | None = None
) -> ControllerRuntime:
    return build_controller_runtime(settings, run_root=run_root, scenario=scenario)


@pytest.fixture
def client(settings: RuntimeSettings, tmp_path: Path) -> Iterator[TestClient]:
    runtime = runtime_for(settings, tmp_path / "runs")
    with TestClient(create_app(runtime=runtime, settings=settings)) as started:
        yield started


def invoke(client: TestClient, key: str, **extra: Any) -> Any:
    return client.post(
        "/invocations", headers=HEADERS, json={"idempotency_key": key} | extra
    )


def emitted(captured: str) -> list[dict[str, Any]]:
    """Every telemetry line the serving layer wrote, in order.

    The declared prefix is required and stripped before the payload is parsed,
    so a line the container wrote for some other reason is not telemetry here
    and is skipped rather than validated as a record.
    """
    written = []
    for line in captured.splitlines():
        payload = public_line_payload(line)
        if payload is None:
            continue
        written.append(json.loads(payload))
    return written


def record_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The record lines only. A line with no `kind` is the summary."""
    return [line for line in lines if "kind" in line]


def scenario_client(
    settings: RuntimeSettings, run_root: Path, token: str
) -> Iterator[TestClient]:
    runtime = runtime_for(settings, run_root, token)
    with TestClient(create_app(runtime=runtime, settings=settings)) as started:
        yield started


# --------------------------------------------------------------------------
# Telemetry reaches the log


def test_an_invocation_emits_public_telemetry_lines_offline(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run's durable events arrive on stdout as public record lines."""
    assert invoke(client, "offline-telemetry-01").status_code == 200

    written = emitted(capsys.readouterr().out)
    assert written, "the serving layer attached a sink"
    lines = record_lines(written)
    assert lines
    assert all(line["visibility"] == "public" for line in lines)
    assert all(line["mode"] == "offline" for line in lines)
    assert all(line["provider"] == "mock" for line in lines)
    kinds = [line["kind"] for line in lines]
    assert "run_started" in kinds
    assert "run_finished" in kinds


def test_every_emitted_line_carries_the_declared_prefix_offline(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """The extraction contract: a log reader selects lines by one fixed token."""
    assert invoke(client, "offline-telemetry-prefix").status_code == 200

    captured = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert captured
    for line in captured:
        assert line.startswith(PUBLIC_LINE_PREFIX), line
        assert line[len(PUBLIC_LINE_PREFIX) :].startswith("{")


def test_unrelated_container_stdout_is_not_accepted_as_telemetry_offline() -> None:
    """A line the container wrote for another reason is never a record."""
    for foreign in (
        "INFO application startup complete",
        '{"visibility":"public","kind":"run_started"}',
        "UNSTUCK_TELEMETRY_V1{}",
    ):
        assert emitted(foreign) == []
        assert public_line_payload(foreign) is None


def test_every_emitted_record_line_is_public_and_revalidates_offline(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each line is a complete public record, not a partial projection."""
    assert invoke(client, "offline-telemetry-02").status_code == 200

    for line in record_lines(emitted(capsys.readouterr().out)):
        reread = TelemetryRecord.model_validate_json(json.dumps(line))
        assert reread.visibility == "public"
        assert reread.classification == "synthetic"
        assert unsafe_public_fields(reread) == ()


def test_the_final_emitted_line_reconciles_the_record_offline(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """The completeness summary is written, so the record is not unreconciled."""
    assert invoke(client, "offline-telemetry-03").status_code == 200

    written = emitted(capsys.readouterr().out)
    summary = written[-1]
    assert "kind" not in summary
    assert summary["finalized"] is True
    assert summary["dropped"] == 0
    assert summary["refused"] == 0
    assert summary["emitted"] == len(record_lines(written))
    assert summary["expected_event_count"] == len(record_lines(written))
    assert summary["first_sequence"] == 1


def test_no_emitted_line_carries_deployment_identity_offline(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """The account, Region, runtime, endpoint, bucket and tables never appear."""
    assert invoke(client, "offline-telemetry-04").status_code == 200

    text = capsys.readouterr().out
    assert text
    assert ENVIRONMENT["UNSTUCK_MODEL_REVISION"] == MOCK_MODEL_REVISION
    for restricted in FORBIDDEN_IN_LINES:
        assert restricted not in text, restricted
    for name, value in ENVIRONMENT.items():
        if name in DISCLOSED_INPUTS:
            continue
        assert value not in text, name


def test_no_emitted_line_carries_the_forwarded_session_identity_offline(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """The session the service forwarded is never written in the clear."""
    assert invoke(client, "offline-telemetry-05").status_code == 200

    text = capsys.readouterr().out
    assert SESSION not in text
    assert "x-amzn" not in text.lower()


def test_no_emitted_line_carries_the_report_rows_or_an_idempotency_key_offline(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Telemetry observes the run. It is not a copy of the run's own data."""
    key = "offline-telemetry-06"
    response = invoke(client, key)
    assert response.status_code == 200

    assert response.json()["result"]["rows"], "the run did produce rows"
    text = capsys.readouterr().out
    assert key not in text
    assert '"units"' not in text
    assert "country_code" not in text


def test_a_refusal_emits_nothing_at_all_offline(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """No run, no durable event, no telemetry. A refusal observes nothing."""
    assert client.post("/invocations", json={"idempotency_key": "x"}).status_code == 403

    assert emitted(capsys.readouterr().out) == []


def test_a_duplicate_invocation_emits_no_second_record_offline(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """A duplicate is answered from the store, so it writes no new events."""
    assert invoke(client, "offline-telemetry-07").status_code == 200
    first = emitted(capsys.readouterr().out)
    assert first

    duplicate = invoke(client, "offline-telemetry-07")

    assert duplicate.json()["status"] == "duplicate"
    assert emitted(capsys.readouterr().out) == []


# --------------------------------------------------------------------------
# The scenario is server-owned


def test_the_request_contract_is_unchanged_and_refuses_a_scenario_offline(
    client: TestClient,
) -> None:
    """A caller cannot choose the scenario, the task, the mode, or the tools."""
    for extra in (
        {"scenario": "memory-repeat"},
        {"mode": "live"},
        {"tool_id": "inspect_pipeline"},
        {"baseline_id": LOCAL_ARM},
    ):
        response = invoke(client, "offline-scenario-01", **extra)
        assert response.status_code == 400, extra
        assert response.json()["reason_code"] == CloudReason.ARGUMENT_REJECTED.value


def test_an_unknown_scenario_fails_closed_before_a_listener_offline(
    settings: RuntimeSettings, tmp_path: Path
) -> None:
    """An undeclared token refuses at construction, with no server built."""
    with pytest.raises(CloudFault) as refused:
        runtime_for(settings, tmp_path / "runs", "not-a-declared-scenario")
    assert refused.value.reason is CloudReason.CONFIG_INVALID

    with pytest.raises(CloudFault) as injected:
        app_from_environment(
            dict(ENVIRONMENT) | {SCENARIO_VARIABLE: "not-a-declared-scenario"},
            run_root=tmp_path / "runs",
        )
    assert injected.value.reason is CloudReason.CONFIG_INVALID


def test_an_absent_scenario_variable_resolves_the_declared_default_offline() -> None:
    """A deployment that declares no scenario serves the declared default."""
    assert DEFAULT_SCENARIO in SCENARIO_TOKENS
    assert SCENARIO_VARIABLE not in ENVIRONMENT

    assert runtime_scenario(ENVIRONMENT).token == DEFAULT_SCENARIO
    assert runtime_scenario({}).token == DEFAULT_SCENARIO
    assert runtime_scenario({SCENARIO_VARIABLE: "   "}).token == DEFAULT_SCENARIO


def test_the_injected_scenario_is_the_one_resolved_offline() -> None:
    """The deployment input selects it. Nothing else can."""
    for token in SCENARIO_TOKENS:
        resolved = runtime_scenario(dict(ENVIRONMENT) | {SCENARIO_VARIABLE: token})
        assert resolved.token == token


def test_the_session_gate_is_unchanged_under_every_scenario_offline(
    settings: RuntimeSettings, tmp_path: Path
) -> None:
    """Scenario selection does not touch inbound identity."""
    for token in SCENARIO_TOKENS:
        for started in scenario_client(settings, tmp_path / token, token):
            absent = started.post("/invocations", json={"idempotency_key": "k"})
            assert absent.status_code == 403
            assert absent.json()["reason_code"] == (CloudReason.IDENTITY_REQUIRED.value)
            malformed = started.post(
                "/invocations",
                headers={"x-amzn-bedrock-agentcore-runtime-session-id": "short"},
                json={"idempotency_key": "k"},
            )
            assert malformed.status_code == 403
            assert malformed.json()["reason_code"] == (
                CloudReason.SESSION_INVALID.value
            )


# --------------------------------------------------------------------------
# Every Decision Receipt path is reachable


def result_of(client: TestClient, key: str) -> Mapping[str, Any]:
    response = client.post(
        "/invocations", headers=HEADERS, json={"idempotency_key": key}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted", body
    outcome: Mapping[str, Any] = body["result"]
    return outcome


def test_the_direct_path_runs_one_decision_and_one_read_offline(
    settings: RuntimeSettings, tmp_path: Path
) -> None:
    """`repairable-join`: one fresh decision, one read, one repair."""
    for started in scenario_client(settings, tmp_path / "direct", "repairable-join"):
        result = result_of(started, "offline-direct-01")

    assert result["baseline_id"] == LOCAL_ARM
    assert result["history_status"] == "empty", "a cold namespace has no history"
    assert result["pass_count"] == 1
    assert result["diagnostic_reads"] == 1
    assert result["repair_attempts"] == 1
    assert result["reason_code"] == "populated_after_repair"
    assert result["verification_status"] == "passed"
    assert result["first_choice"] == result["final_choice"]


def test_the_same_call_second_decision_is_reachable_offline(
    settings: RuntimeSettings, tmp_path: Path
) -> None:
    """`memory-repeat` call one: a stalled read opens one second decision."""
    for started in scenario_client(settings, tmp_path / "same", "memory-repeat"):
        result = result_of(started, "offline-same-call-01")

    assert result["history_status"] == "empty"
    assert result["pass_count"] == 2, "one same-call second decision"
    assert result["diagnostic_reads"] == 2
    assert result["first_choice"] != result["final_choice"]
    # `receipt_ids` is what the lookup retrieved, not what the run wrote. A
    # cold namespace retrieves nothing; that this call did write receipts is
    # what the cross-call test reads back.
    assert result["receipt_ids"] == []
    # The read ran and admitted nothing, so the first choice is refused as a
    # failed execution rather than as a repetition: both reads were spent, and
    # that is exactly what the cross-call path does not do.
    assert result["blocked_tool_id"] == result["first_choice"]
    assert result["blocked_reason_code"] == "insufficient_evidence"


def test_the_cross_call_repetition_is_precluded_before_any_selector_call_offline(
    settings: RuntimeSettings, tmp_path: Path
) -> None:
    """`memory-repeat` call two: the exact repeat is never offered at all."""
    for started in scenario_client(settings, tmp_path / "cross", "memory-repeat"):
        first = result_of(started, "offline-cross-call-01")
        second = result_of(started, "offline-cross-call-02")

    assert second["run_id"] != first["run_id"]
    assert second["history_status"] == "matched", "call two read call one's receipt"
    assert second["receipt_ids"]
    assert second["blocked_reason_code"] == "insufficient_evidence"
    # D1 chose among the rest; its read moved no gate, so the one same-call D2
    # chose from what remained. The precluded dead end ran in neither.
    assert second["first_choice"] != first["first_choice"]
    assert second["final_choice"] not in {first["first_choice"], second["first_choice"]}
    assert second["pass_count"] == 2
    assert second["diagnostic_reads"] == 2
    assert second["status"] == "completed"


def test_a_duplicate_invocation_reuses_the_stored_result_offline(
    settings: RuntimeSettings, tmp_path: Path
) -> None:
    """The run store is the idempotency authority, under a scenario too."""
    for started in scenario_client(settings, tmp_path / "dup", "memory-repeat"):
        first = result_of(started, "offline-duplicate-01")
        again = started.post(
            "/invocations",
            headers=HEADERS,
            json={"idempotency_key": "offline-duplicate-01"},
        )

    assert again.status_code == 200
    body = again.json()
    assert body["status"] == "duplicate"
    assert body["result"] == first


def test_the_controlled_error_paths_are_reachable_offline(
    settings: RuntimeSettings, tmp_path: Path
) -> None:
    """A proven empty result and an unresolved run are distinct terminals."""
    for started in scenario_client(settings, tmp_path / "empty", "verified-empty"):
        empty = result_of(started, "offline-empty-01")
    for started in scenario_client(settings, tmp_path / "stalled", "stalled-upstream"):
        stalled = result_of(started, "offline-stalled-01")

    assert empty["rows"] == []
    assert empty["verification_status"] == "passed"
    assert empty["repair_attempts"] == 0
    assert stalled["rows"] == []
    assert stalled["reason_code"] != empty["reason_code"]
    assert stalled["disposition"] != empty["disposition"]


def test_every_scenario_reaches_its_own_terminal_signature_offline(
    settings: RuntimeSettings, tmp_path: Path
) -> None:
    """Five declared scenarios, five distinct live outcomes."""
    signatures: set[tuple[Any, ...]] = set()
    for token in SCENARIO_TOKENS:
        for started in scenario_client(settings, tmp_path / f"sig-{token}", token):
            result = result_of(started, f"offline-signature-{token}")
        signatures.add(
            (
                result["reason_code"],
                result["disposition"],
                result["pass_count"],
                result["diagnostic_reads"],
                result["repair_attempts"],
                result["wait_checks"],
            )
        )
    assert len(signatures) == len(SCENARIO_TOKENS)
