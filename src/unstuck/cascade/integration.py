"""Projections onto the generic core OD-02 seam.

The core runtime neither retrieves nor matches Decision Receipts. It carries a
`DecisionHistory` the cascade looked up, records the lookup, and projects an
explicit status so an absent or unavailable history can never look like a
cached decision. This module produces exactly those core contracts from cascade
values and builds the one canonical decision context from one core state.

There is deliberately no adapter that serves the core `DecisionEngine` protocol
from the first decision alone. Such a seam would answer with a tool choice
while skipping receipt admission, exact-repetition blocking, and the second
decision, so the controller composes the whole bounded orchestrator instead.

Nothing here widens the core's typed-authority or sanitized-display boundary:
the private canonical capability stays inside the cascade, and every projected
field is one the core already validates.
"""

from unstuck.cascade.cards import CardBinding, capability_cards, card_index
from unstuck.cascade.identity import CandidateIdentity, DecisionContext
from unstuck.cascade.matching import HistoryLookupResult
from unstuck.cascade.policy import CascadePolicy
from unstuck.cascade.serialization import StageMeasurement, canonical_digest
from unstuck.decision import DecisionHistory, DecisionState, SelectionAuthority
from unstuck.domain import Reason, RuntimeFault
from unstuck.events import Measurement

#: Operational identity carried by one bounded observation. A fresh evidence
#: identity or a new observation timestamp for the same observed facts is a
#: caller retry, not a changed decision context, so neither enters the hash.
OPERATIONAL_EVIDENCE_FIELDS = frozenset({"evidence_id", "observed_at"})


def decision_history(history: HistoryLookupResult) -> DecisionHistory:
    """Project a cascade lookup onto the core history contract."""
    return DecisionHistory(
        status=history.status,
        references=history.references,
        truncated=history.truncated,
    )


def core_measurement(measurement: StageMeasurement) -> Measurement:
    """Narrow cascade telemetry to the core availability enum.

    The core has no `invalid` or `provider_error` availability, so those become
    `not_measured`. An unavailable measurement never becomes zero.
    """
    availability = measurement.availability
    if availability == "measured":
        return Measurement(availability="measured", value=measurement.value)
    if availability == "not_provided":
        return Measurement(availability="not_provided")
    if availability == "not_called":
        return Measurement(availability="not_called")
    return Measurement(availability="not_measured")


def selection_authority(result: object) -> SelectionAuthority:
    """Project the cascade's decision record onto the core authority contract."""
    record = getattr(result, "final", None) or getattr(result, "first", None)
    if record is None:
        raise RuntimeFault(Reason.FIRST_REJECTED)
    history = getattr(result, "history", HistoryLookupResult())
    # A first decision over a narrowed offer names the preclusion behind it.
    blocked = getattr(result, "blocked", ()) or getattr(result, "precluded", ())
    return SelectionAuthority(
        decision_id=record.decision_id,
        selected_id=record.selected_id,
        confidence=record.confidence,
        model_version=record.model_revision,
        accepted=record.accepted,
        pass_index=record.pass_index,
        # Carried from the normalized record, so this projection cannot become
        # a second path that reports a provider the call never had.
        provider=record.provider,
        history_status=history.status,
        receipt_ids=history.receipt_ids,
        history_truncated=history.truncated,
        blocked_tool_id=blocked[0].public_tool_id if blocked else None,
        blocked_reason=blocked[0].reason_code if blocked else None,
    )


def context_for(
    state: DecisionState,
    policy: CascadePolicy,
    cards: tuple[CardBinding, ...] | None = None,
    *,
    used_capabilities: tuple[str, ...] = (),
) -> DecisionContext:
    """Build the canonical decision context from one core selection state."""
    index = card_index(cards or capability_cards())
    candidates = []
    for probe in state.candidates:
        binding = index.get(probe.id)
        if binding is None:
            raise RuntimeFault(Reason.INVARIANT)
        candidates.append(
            CandidateIdentity(
                public_tool_id=binding.card.public_tool_id,
                description=binding.card.description,
                available=probe.available,
            )
        )
    # The admissible evidence enters the identity as a set of observed facts.
    # Two observations of the same facts are one fact about the situation, so a
    # repeated equivalent observation is collapsed here exactly as a reordered
    # one is: neither changes what is currently true.
    observed = tuple(
        dict.fromkeys(
            canonical_digest(
                record.model_dump(mode="json", exclude=set(OPERATIONAL_EVIDENCE_FIELDS))
            )
            for record in state.bounded_evidence
        )
    )
    try:
        return DecisionContext(
            task_hash=state.task_hash,
            query_hash=state.query_hash,
            base_query_hash=state.query.base_query_hash,
            dataset_id=state.task.dataset_id,
            versions=policy.versions(state.dataset_version),
            candidates=tuple(candidates),
            evidence_hashes=observed,
            used_capabilities=used_capabilities,
        )
    except ValueError as error:
        raise RuntimeFault(Reason.INVARIANT) from error
