"""Invariant tests for explicit offline, live, and replay modes. Offline.

AC5.1.2 is the rule under test: missing live access blocks the live check and
never silently falls back. No live provider exists in this project, so `live`
is blocked by absence and names the gate it waits for.
"""

import pytest

from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.modes import (
    LIVE_GATES,
    resolve_mode,
)


def test_offline_is_the_only_mode_that_runs_today_offline() -> None:
    label = resolve_mode("offline")
    assert label.requested == "offline"
    assert label.effective == "offline"
    assert label.provider == "mock"
    assert label.blocked is False
    assert label.gate is None


def test_live_is_blocked_and_names_its_exact_gate_offline() -> None:
    label = resolve_mode("live")
    assert label.blocked is True
    assert label.effective is None
    assert label.provider is None
    assert label.gate is not None
    assert any(gate in label.gate for gate in LIVE_GATES)


def test_live_never_falls_back_to_offline_or_mock_offline() -> None:
    label = resolve_mode("live")
    assert label.effective != "offline"
    assert label.provider != "mock"
    rendered = label.model_dump_json().lower()
    assert "fallback" not in rendered
    # A caller that insists on executing a blocked mode is refused outright.
    with pytest.raises(OperatorFault) as raised:
        label.require_runnable()
    assert raised.value.reason is OperatorReason.LIVE_MODE_BLOCKED


def test_a_live_run_is_blocked_even_when_the_spend_gate_would_allow_it_offline() -> (
    None
):
    label = resolve_mode("live", spend_allowed=True)
    assert label.blocked is True
    assert label.gate is not None


def test_a_live_run_names_the_spend_gate_when_that_is_what_is_missing_offline() -> None:
    label = resolve_mode("live", spend_allowed=False, live_provider=True)
    assert label.blocked is True
    assert "AWS_SPEND_LIMIT_USD" in str(label.gate)


def test_replay_is_labelled_distinctly_and_blocked_without_a_recording_offline() -> (
    None
):
    blocked = resolve_mode("replay")
    assert blocked.blocked is True
    assert blocked.requested == "replay"
    with pytest.raises(OperatorFault) as raised:
        blocked.require_runnable()
    assert raised.value.reason is OperatorReason.REPLAY_UNAVAILABLE
    available = resolve_mode("replay", replay_available=True)
    assert available.blocked is False
    assert available.effective == "replay"
    assert available.provider == "replay"
    assert available.effective != "offline"


def test_every_mode_label_is_distinguishable_by_text_alone_offline() -> None:
    labels = [
        resolve_mode("offline"),
        resolve_mode("live"),
        resolve_mode("replay"),
        resolve_mode("replay", replay_available=True),
    ]
    texts = [label.text for label in labels]
    assert len(set(texts)) == len(texts)
    assert all(text and not text.isspace() for text in texts)
    assert "Offline" in texts[0]
    assert "blocked" in texts[1].lower()
    assert "Replay" in texts[3]


def test_resolving_a_mode_constructs_no_aws_client_offline() -> None:
    import ast
    import inspect

    import unstuck.operator.modes as module

    tree = ast.parse(inspect.getsource(module))
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
    calls = {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert not any("client" in call for call in calls)
