"""Local operator experience: a loopback-only API and an accessible static UI.

This package presents what the core runtime, the Decision Receipt engine, and
the evaluation unit already produced. It computes no statistic, derives no
outcome class, selects no tool, and authorizes no repair: every value it shows
is read from a terminal result, an event, or an export produced elsewhere.

The surface is local by construction. It binds `127.0.0.1` only, refuses a
non-loopback host before a socket exists, requires an exact `Host` and
`Origin`, requires `application/json` on a state-changing request, and serves
no CORS header at all. It is a single operator's tool on their own machine;
reaching it from another machine would be a separate, owner-approved decision
and is not built here.

The reason taxonomy below is the package's only error vocabulary. It is defined
here, with no imports from the submodules, so the submodules can depend on it
without a circular import.
"""

from enum import StrEnum


class OperatorReason(StrEnum):
    """Explicit reason codes for every operator-surface refusal."""

    NON_LOOPBACK_HOST = "operator_non_loopback_host"
    HOST_REJECTED = "operator_host_rejected"
    ORIGIN_REJECTED = "operator_origin_rejected"
    MEDIA_TYPE_REJECTED = "operator_media_type_rejected"
    ARGUMENT_REJECTED = "operator_argument_rejected"
    RUN_NOT_FOUND = "operator_run_not_found"
    NODE_NOT_FOUND = "operator_node_not_found"
    LIVE_MODE_BLOCKED = "operator_live_mode_blocked"
    REPLAY_UNAVAILABLE = "operator_replay_unavailable"
    EXPORT_FORMAT_REJECTED = "operator_export_format_rejected"
    RETENTION_UNDECIDABLE = "operator_retention_undecidable"
    ASSET_NOT_FOUND = "operator_asset_not_found"
    SCENARIO_REJECTED = "operator_scenario_rejected"
    SCENARIO_MISMATCH = "operator_scenario_identity_mismatch"
    SCENARIO_BINDING_UNREADABLE = "operator_scenario_binding_unreadable"
    EVALUATION_PATH_REJECTED = "operator_evaluation_export_path_rejected"
    EVALUATION_EXPORT_INVALID = "operator_evaluation_export_invalid"


class OperatorFault(Exception):
    """A reason-coded operator-surface refusal. Never a bare exception."""

    def __init__(self, reason: OperatorReason, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(reason.value)


__all__ = ["OperatorFault", "OperatorReason"]
