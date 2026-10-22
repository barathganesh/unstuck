"""The controller-owned diagnostic execution seam.

The cascade never executes a diagnostic read itself. It asks the controller to
run exactly one canonical capability with one bound argument set, and the
controller returns the structured facts it observed. The capability stays
private to this seam; only the public tool identity travels to the selector.

The same seam projects the current authoritative state, because the controller
is the only holder of it. One authority serves exactly one invocation and is
handed to the orchestrator per call, so nothing about one run is ever reachable
from another.
"""

from typing import Literal, Protocol
from uuid import UUID

from unstuck.cascade.verifier import DiagnosticReport
from unstuck.decision import DecisionState
from unstuck.domain import Contract, Digest, Identifier


class DiagnosticRequest(Contract):
    """One bound request to execute one canonical capability."""

    diagnostic_call_id: UUID
    pass_index: Literal[1, 2]
    capability: Identifier
    public_tool_id: Identifier
    arguments_hash: Digest


class StateProjector(Protocol):
    def project(self) -> DecisionState: ...


class DiagnosticExecutor(StateProjector, Protocol):
    """One run's execution authority: it executes reads and projects state."""

    async def execute(self, request: DiagnosticRequest) -> DiagnosticReport: ...
