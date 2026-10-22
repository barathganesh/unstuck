"""Invariant tests for runtimeSessionId derivation and the controller lease.

Offline and synthetic. The AgentCore session charset and length bound stay
Pending PF6; what is asserted here is the derivation this unit controls.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from unstuck.cloud import CloudReason
from unstuck.cloud.session import (
    ACQUIRE_CONDITION,
    EVENT_CONDITION,
    SESSION_PREFIX,
    WRITE_CONDITION,
    LeaseTerms,
    RuntimeSessionId,
    lease_outcome,
)

from .conftest import SYNTHETIC_RUN_ID


def test_session_id_is_stable_across_calls_offline() -> None:
    first = RuntimeSessionId.for_run(SYNTHETIC_RUN_ID)
    second = RuntimeSessionId.for_run(SYNTHETIC_RUN_ID)
    assert first == second
    assert first.value == f"{SESSION_PREFIX}{SYNTHETIC_RUN_ID}"
    assert first.run_id == SYNTHETIC_RUN_ID
    assert RuntimeSessionId.for_run(uuid4()) != first


def test_session_id_charset_and_length_are_validated_offline() -> None:
    session = RuntimeSessionId.for_run(SYNTHETIC_RUN_ID)
    assert len(session.value) == len(SESSION_PREFIX) + 36
    assert set(session.value) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )
    for rejected in (
        f"{SESSION_PREFIX}{SYNTHETIC_RUN_ID}!",
        f"{SESSION_PREFIX}{SYNTHETIC_RUN_ID} ",
        f"{SESSION_PREFIX}{str(SYNTHETIC_RUN_ID).replace('-', '')}",
        SESSION_PREFIX,
    ):
        with pytest.raises(ValidationError):
            RuntimeSessionId(value=rejected)


def test_acquire_condition_is_the_design_expression_offline() -> None:
    assert ACQUIRE_CONDITION == (
        "attribute_not_exists(lease_owner) AND #status = :created"
    )
    assert WRITE_CONDITION == "lease_owner = :owner AND state_version = :expected"
    assert EVENT_CONDITION == "attribute_not_exists(SK)"


def test_lease_covers_the_run_and_is_never_renewed_offline() -> None:
    terms = LeaseTerms()
    assert (terms.lease_seconds, terms.run_seconds) == (150.0, 120.0)
    assert terms.renewable is False
    assert terms.lease_seconds > terms.run_seconds
    with pytest.raises(ValidationError):
        LeaseTerms(lease_seconds=100.0, run_seconds=120.0)
    with pytest.raises(ValidationError):
        LeaseTerms.model_validate(terms.model_dump() | {"renewable": True})


def test_first_invocation_acquires_the_lease_offline() -> None:
    decision = lease_outcome(status="created", lease_owner=None, expired=False)
    assert (decision.action, decision.reason) == ("acquire", None)


def test_duplicate_invocation_is_lease_rejected_offline() -> None:
    held = lease_outcome(status="running", lease_owner=uuid4(), expired=False)
    assert held.action == "reject"
    assert held.reason is CloudReason.LEASE_REJECTED
    terminal = lease_outcome(status="completed", lease_owner=uuid4(), expired=True)
    assert terminal.action == "reject"
    assert terminal.reason is CloudReason.LEASE_REJECTED


def test_expired_non_terminal_lease_only_maps_to_failed_offline() -> None:
    decision = lease_outcome(status="running", lease_owner=uuid4(), expired=True)
    assert decision.action == "fail_expired"
    assert decision.reason is CloudReason.LEASE_EXPIRED
    assert decision.reason.value == "lease_expired_incomplete"


def test_lease_outcome_refuses_an_incoherent_state_offline() -> None:
    with pytest.raises(ValidationError):
        lease_outcome(status="created", lease_owner=uuid4(), expired=False)
    with pytest.raises(ValidationError):
        lease_outcome(status="not_a_status", lease_owner=None, expired=False)
