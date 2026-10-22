"""The deterministic offline stand-in for the Decider, for local runs.

This is a scripted mock provider, not a model and not a measurement. It holds
no authority of its own: the bounded Decision Receipt orchestrator narrows the
candidates it may answer about, charges the attempt before the request leaves,
recomputes acceptance from the answer, and owns every refusal. All this object
does is answer, deterministically, from the candidates it was offered.

It answers from the offered candidates rather than from a fixed identifier,
which is what makes the one same-call second decision observable locally: when
the orchestrator removes a refused read and asks again, the next preference is
chosen instead. No latency, cost, or accuracy measured against it is a real
measurement.
"""

from unstuck.decision import Decision, DecisionState, OptionProbability

#: The fixed preference order. It is a declared order, not a learned one.
PREFERENCE: tuple[str, ...] = (
    "inspect_schema_keys",
    "inspect_pipeline",
    "inspect_scope",
)
#: The confidence this stand-in reports. It is scripted, so it is not evidence
#: about anything; it exists because a selection without one cannot execute.
SCRIPTED_CONFIDENCE = 0.95


class OfflineScenarioDecider:
    """Chooses the first offered candidate in a declared preference order."""

    model_version = "scripted-v1"

    async def select(self, state: DecisionState) -> Decision:
        offered = tuple(probe.id for probe in state.candidates)
        selected = next(
            (identifier for identifier in PREFERENCE if identifier in offered),
            offered[0] if offered else None,
        )
        identifiers = offered + ("request_review",)
        return Decision(
            model_version=self.model_version,
            candidate_ids=identifiers,
            selected_id=selected,
            probabilities=tuple(
                OptionProbability(
                    id=identifier, probability=float(identifier == selected)
                )
                for identifier in identifiers
            ),
            confidence=SCRIPTED_CONFIDENCE if selected is not None else None,
            validation_status="valid" if selected is not None else "invalid",
        )


__all__ = ["PREFERENCE", "SCRIPTED_CONFIDENCE", "OfflineScenarioDecider"]
