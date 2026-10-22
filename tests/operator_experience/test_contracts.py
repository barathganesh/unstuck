"""Contract tests for the operator surface. Offline and synthetic."""

import pytest
from pydantic import ValidationError

from unstuck.operator.contracts import (
    DEFAULT_PORT,
    LOOPBACK_HOSTS,
    BindAddress,
    DisplayValue,
    EvidencePanel,
    ExportRequest,
    ModeLabel,
    RunRequest,
    SpineNode,
)

from .conftest import LOOPBACK_AUTHORITY, LOOPBACK_ORIGIN

REJECTED_VALUES = (
    "Bearer offline-placeholder",
    "authorization=offline-placeholder",
    "AWS_SECRET_ACCESS_KEY",
    "fixtures/evaluator/answers.json",
    "customer-0001@example.invalid",
)


def test_bind_address_accepts_loopback_only_offline() -> None:
    bind = BindAddress()
    assert (bind.host, bind.port) == ("127.0.0.1", DEFAULT_PORT)
    assert bind.authority == LOOPBACK_AUTHORITY
    assert LOOPBACK_ORIGIN in bind.origins
    assert set(LOOPBACK_HOSTS) == {"127.0.0.1", "localhost", "::1"}
    for host in LOOPBACK_HOSTS:
        assert BindAddress(host=host).host == host
    for rejected in ("0.0.0.0", "::", "192.168.1.10", "example.invalid", ""):
        with pytest.raises(ValidationError):
            BindAddress(host=rejected)


def test_bind_address_is_frozen_and_closed_offline() -> None:
    bind = BindAddress()
    with pytest.raises(ValidationError):
        bind.host = "0.0.0.0"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        BindAddress.model_validate({"host": "127.0.0.1", "public": True})
    with pytest.raises(ValidationError):
        BindAddress(port=80)


def test_run_request_is_closed_and_server_owns_the_task_offline() -> None:
    request = RunRequest(scenario="repairable-join", idempotency_key="offline-ui-01")
    assert request.mode == "offline"
    for forbidden in ("task", "dataset_id", "order_date_utc", "country_codes"):
        with pytest.raises(ValidationError):
            RunRequest.model_validate(request.model_dump() | {forbidden: "anything"})
    with pytest.raises(ValidationError):
        RunRequest.model_validate(request.model_dump() | {"mode": "production"})
    with pytest.raises(ValidationError):
        RunRequest(scenario="repairable-join", idempotency_key="")


@pytest.mark.parametrize("value", REJECTED_VALUES)
def test_no_request_field_accepts_a_sensitive_value_offline(value: str) -> None:
    base = RunRequest(scenario="repairable-join", idempotency_key="offline-ui-01")
    for field in ("scenario", "idempotency_key"):
        with pytest.raises(ValidationError):
            RunRequest.model_validate(base.model_dump() | {field: value})


def test_mode_label_states_blocked_and_gate_together_offline() -> None:
    offline = ModeLabel(requested="offline", effective="offline", provider="mock")
    assert offline.blocked is False
    assert offline.gate is None
    blocked = ModeLabel(requested="live", blocked=True, gate="PF7")
    assert blocked.effective is None
    assert blocked.provider is None
    with pytest.raises(ValidationError):
        ModeLabel(requested="live", blocked=True, gate=None)
    with pytest.raises(ValidationError):
        ModeLabel(requested="live", effective="offline", blocked=True, gate="PF7")
    with pytest.raises(ValidationError):
        ModeLabel.model_validate({"requested": "offline", "fallback": "offline"})


def test_display_value_never_renders_an_absent_number_as_zero_offline() -> None:
    measured = DisplayValue(text="1204.5", availability="measured", value=1204.5)
    assert measured.value == 1204.5
    for availability in ("not_measured", "not_provided", "not_called"):
        absent = DisplayValue.for_availability(availability)
        assert absent.value is None
        assert absent.text != "0"
        assert absent.text in {"Not measured", "Not provided", "Not called"}
    with pytest.raises(ValidationError):
        DisplayValue(text="0", availability="not_measured", value=0.0)
    with pytest.raises(ValidationError):
        DisplayValue(text="1204.5", availability="measured", value=None)


def test_spine_node_contract_is_closed_offline() -> None:
    node = SpineNode(
        node_id="decision-first",
        order=2,
        kind="decision_first",
        label="First tool choice",
        state="informational",
        detail="Chose inspect_schema_keys.",
        present=True,
    )
    assert node.evidence_ids == ()
    with pytest.raises(ValidationError):
        SpineNode.model_validate(node.model_dump() | {"colour": "teal"})
    with pytest.raises(ValidationError):
        SpineNode.model_validate(node.model_dump() | {"kind": "jev"})
    with pytest.raises(ValidationError):
        SpineNode.model_validate(node.model_dump() | {"order": 0})


def test_evidence_panel_contract_is_closed_offline() -> None:
    panel = EvidencePanel(node_id="diagnostic", evidence={}, validation={})
    assert panel.raw_event is None
    with pytest.raises(ValidationError):
        EvidencePanel.model_validate(panel.model_dump() | {"headers": {}})


def test_export_request_accepts_the_three_declared_formats_offline() -> None:
    from uuid import uuid4

    run_id = uuid4()
    for fmt in ("jsonl", "json", "csv"):
        assert ExportRequest(run_id=run_id, format=fmt).format == fmt
    with pytest.raises(ValidationError):
        ExportRequest(run_id=run_id, format="xlsx")
    with pytest.raises(ValidationError):
        ExportRequest.model_validate(
            {"run_id": str(run_id), "format": "json", "include_secrets": True}
        )
