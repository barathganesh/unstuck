"""Offline Decision Receipt engine.

Decider 2B is the only model tool selector. Every new first decision receives
the complete current authoritative state, the static Tool Capability Cards,
runtime availability, remaining budgets, and a bounded set of labeled Decision
Receipts, then chooses fresh. Historical choices never replay automatically and
current authoritative facts always dominate history.

Live Decider transport, cloud storage, and benchmark evaluation remain Pending.
"""

from unstuck.cascade.cards import (
    CATALOG_VERSION,
    DATA_SCHEMA_VERSION,
    TOOL_CONTRACT_VERSION,
    CardBinding,
    ToolCapabilityCard,
    canonical_capability,
    capability_cards,
    card_for,
    card_index,
    catalog_hash,
)
from unstuck.cascade.engine import (
    CASCADE_REASONS,
    CascadeEngine,
    CascadeJournal,
    CascadeRequest,
    CascadeResult,
    SilentJournal,
)
from unstuck.cascade.execution import (
    DiagnosticExecutor,
    DiagnosticRequest,
    StateProjector,
)
from unstuck.cascade.identity import (
    BoundVersions,
    CallIdentity,
    CandidateIdentity,
    DecisionContext,
)
from unstuck.cascade.integration import (
    context_for,
    core_measurement,
    decision_history,
    selection_authority,
)
from unstuck.cascade.matching import (
    HistoryLookupResult,
    HistoryMatcher,
    HistoryStatus,
    MatchClass,
    ReceiptMatch,
)
from unstuck.cascade.offline import (
    ScriptedChoice,
    ScriptedDecider,
    ScriptedExecutor,
    ScriptedOutcome,
)
from unstuck.cascade.policy import Arm, Calibration, CascadePolicy
from unstuck.cascade.projector import (
    QUESTION,
    BlockedExecution,
    EvidenceSummary,
    FirstChoice,
    SelectorBudgets,
    SelectorInput,
    project_selector_input,
)
from unstuck.cascade.receipts import (
    BLOCKING_OUTCOMES,
    EVIDENCE_OUTCOMES,
    OUTCOME_REASONS,
    RECEIPT_REASONS,
    DecisionReceipt,
    EvidenceDelta,
    OutcomeClass,
    ReceiptNamespace,
    receipt_identity,
)
from unstuck.cascade.repository import (
    LocalReceiptRepository,
    ReceiptLookupError,
    ReceiptRepository,
    ReceiptWriteToken,
)
from unstuck.cascade.selection import (
    Decider,
    RawSelection,
    SelectionRecord,
    acceptable,
    first_choice,
    normalize,
    rejection_for,
)
from unstuck.cascade.serialization import (
    MAX_DIAGNOSTIC_READS,
    MAX_RECEIPT_REFERENCES,
    MAX_SELECTOR_CALLS,
    SELECTOR_INPUT_MAX_CHARS,
    TOOL_SUMMARY_MAX_CHARS,
    StageMeasurement,
    bounded_text,
    canonical_digest,
    canonical_text,
)
from unstuck.cascade.verifier import DiagnosticReport, ReceiptVerifier

__all__ = [
    "BLOCKING_OUTCOMES",
    "CASCADE_REASONS",
    "CATALOG_VERSION",
    "DATA_SCHEMA_VERSION",
    "EVIDENCE_OUTCOMES",
    "MAX_DIAGNOSTIC_READS",
    "MAX_RECEIPT_REFERENCES",
    "MAX_SELECTOR_CALLS",
    "OUTCOME_REASONS",
    "QUESTION",
    "RECEIPT_REASONS",
    "SELECTOR_INPUT_MAX_CHARS",
    "TOOL_CONTRACT_VERSION",
    "TOOL_SUMMARY_MAX_CHARS",
    "Arm",
    "BlockedExecution",
    "BoundVersions",
    "Calibration",
    "CallIdentity",
    "CandidateIdentity",
    "CardBinding",
    "CascadeEngine",
    "CascadeJournal",
    "CascadePolicy",
    "CascadeRequest",
    "CascadeResult",
    "Decider",
    "DecisionContext",
    "DecisionReceipt",
    "DiagnosticExecutor",
    "DiagnosticReport",
    "DiagnosticRequest",
    "EvidenceDelta",
    "EvidenceSummary",
    "FirstChoice",
    "HistoryLookupResult",
    "HistoryMatcher",
    "HistoryStatus",
    "LocalReceiptRepository",
    "MatchClass",
    "OutcomeClass",
    "RawSelection",
    "ReceiptLookupError",
    "ReceiptMatch",
    "ReceiptNamespace",
    "ReceiptRepository",
    "ReceiptVerifier",
    "ReceiptWriteToken",
    "ScriptedChoice",
    "ScriptedDecider",
    "ScriptedExecutor",
    "ScriptedOutcome",
    "SelectionRecord",
    "SelectorBudgets",
    "SelectorInput",
    "SilentJournal",
    "StageMeasurement",
    "StateProjector",
    "ToolCapabilityCard",
    "acceptable",
    "bounded_text",
    "canonical_capability",
    "canonical_digest",
    "canonical_text",
    "capability_cards",
    "card_for",
    "card_index",
    "catalog_hash",
    "context_for",
    "core_measurement",
    "decision_history",
    "first_choice",
    "normalize",
    "project_selector_input",
    "receipt_identity",
    "rejection_for",
    "selection_authority",
]
