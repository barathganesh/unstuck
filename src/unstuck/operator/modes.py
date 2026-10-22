"""Explicit offline, live, and replay mode labelling and gating.

AC5.1.2 is the rule: missing live access blocks the live check and never
silently falls back. No live provider exists in this project yet, so `live` is
blocked by absence and the label names the preflights it waits for. A blocked
label has no effective mode and no provider, so a caller cannot read a mock
result as a live one.

Nothing here reads an environment variable or constructs a client. The caller
passes in what it observed; this module only decides and labels.
"""

from unstuck.operator.contracts import Mode, ModeLabel

#: The preflights a live run waits for. See design.md section 21.
LIVE_GATES: tuple[str, ...] = ("PF1", "PF2", "PF6", "PF7", "PF9")
SPEND_GATE = "AWS_SPEND_LIMIT_USD"

_NO_PROVIDER = (
    "no live provider is configured; "
    f"Pending {', '.join(LIVE_GATES[:2])} and {LIVE_GATES[3]}"
)
_NO_SPEND = f"{SPEND_GATE} is unset or its estimate exceeds the remaining limit"
_NO_RECORDING = "no replay recording is available for this run"


def resolve_mode(
    requested: Mode,
    *,
    spend_allowed: bool = False,
    live_provider: bool = False,
    replay_available: bool = False,
) -> ModeLabel:
    """Decide what actually runs. A blocked mode never degrades to another."""
    if requested == "offline":
        return ModeLabel(requested="offline", effective="offline", provider="mock")
    if requested == "replay":
        if not replay_available:
            return ModeLabel(requested="replay", blocked=True, gate=_NO_RECORDING)
        return ModeLabel(requested="replay", effective="replay", provider="replay")
    if not live_provider:
        return ModeLabel(requested="live", blocked=True, gate=_NO_PROVIDER)
    if not spend_allowed:
        return ModeLabel(requested="live", blocked=True, gate=_NO_SPEND)
    # Reaching here needs a verified provider and a passing spend gate, neither
    # of which exists today. The label still refuses rather than inventing a
    # live mode the contracts cannot represent.
    return ModeLabel(
        requested="live",
        blocked=True,
        gate=f"live execution is Pending {LIVE_GATES[2]} and owner approval",
    )


__all__ = ["LIVE_GATES", "SPEND_GATE", "resolve_mode"]
