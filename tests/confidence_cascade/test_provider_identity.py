"""Which provider answered one Decider call, and why a fake cannot claim it.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest \
tests/confidence_cascade/test_provider_identity.py -q

These are written before the seam carries a provider identity at all. The defect
they pin is that `RawSelection.provider` is `Literal["mock"]`, so a run served by
a real Decider 2B checkpoint is recorded as if a scripted stand-in had answered,
and nothing downstream can tell the two apart.

The identity has to be one-way: the real provider value must be expressible, and
the shipped offline fake must be structurally unable to express it. A fake that
could label itself `decider` would make every provider claim unfalsifiable.
"""

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from unstuck.cascade.matching import HistoryLookupResult
from unstuck.cascade.offline import MockSelection, ScriptedChoice, ScriptedDecider
from unstuck.cascade.projector import SelectorBudgets, project_selector_input
from unstuck.cascade.selection import (
    PROVIDERS,
    RawSelection,
    acceptable,
    normalize,
)
from unstuck.decision import OptionProbability

SCHEMA = "inspect_schema_keys"


def payload_for(context, cards, state):
    return project_selector_input(
        pass_index=1,
        context=context,
        cards=cards,
        state=state,
        history=HistoryLookupResult(status="empty"),
        budgets=SelectorBudgets(
            remaining_selector_calls=2,
            remaining_diagnostic_reads=2,
            remaining_ms=120000,
        ),
    )


def selection_for(payload, **changes) -> RawSelection:
    options = payload.option_ids
    return RawSelection.model_validate(
        {
            "model_revision": "hobson-v19",
            "decision_id": uuid4(),
            "model_call_id": uuid4(),
            "pass_index": payload.pass_index,
            "candidate_ids": options,
            "selected_id": SCHEMA,
            "confidence": 0.9,
            "probabilities": tuple(
                OptionProbability(
                    id=identifier, probability=float(identifier == SCHEMA)
                )
                for identifier in options
            ),
            "validation_status": "valid",
        }
        | changes
    )


def test_the_seam_names_exactly_the_two_providers_and_defaults_to_the_fake(
    context, cards, state
):
    payload = payload_for(context, cards, state)
    assert PROVIDERS == ("mock", "decider")
    assert selection_for(payload).provider == "mock"
    assert selection_for(payload, provider="decider").provider == "decider"
    with pytest.raises(ValidationError):
        selection_for(payload, provider="bedrock")


def test_the_shipped_offline_fake_cannot_claim_the_real_provider(context, cards, state):
    """The fake's own type forbids the value, so no scripted run can claim it."""
    payload = payload_for(context, cards, state)
    answer = asyncio.run(
        ScriptedDecider([ScriptedChoice(selected_id=SCHEMA)]).select(payload)
    )
    assert isinstance(answer, MockSelection)
    assert answer.provider == "mock"
    assert "provider" not in {
        field.name for field in ScriptedChoice.__dataclass_fields__.values()
    }
    assert "provider" not in {
        field.name for field in ScriptedDecider.__dataclass_fields__.values()
    }
    with pytest.raises(ValidationError):
        MockSelection.model_validate(
            selection_for(payload).model_dump() | {"provider": "decider"}
        )


def test_the_normalized_record_carries_the_provider_that_answered(
    context, cards, state
):
    payload = payload_for(context, cards, state)
    for provider in PROVIDERS:
        record = normalize(
            selection_for(payload, provider=provider), payload, threshold=0.8
        )
        assert record.provider == provider
        assert record.accepted is True


def test_the_provider_identity_is_evidence_and_never_authority(context, cards, state):
    """Naming the real provider buys no acceptance the fake would not get."""
    payload = payload_for(context, cards, state)
    low = {"confidence": 0.10}
    for provider in PROVIDERS:
        assert acceptable(selection_for(payload, provider=provider), payload, 0.8)
        assert not acceptable(
            selection_for(payload, provider=provider, **low), payload, 0.8
        )
        assert (
            normalize(
                selection_for(payload, provider=provider, **low),
                payload,
                threshold=0.8,
            ).accepted
            is False
        )
