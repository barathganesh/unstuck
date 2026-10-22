"""Offline cloud packaging for the Unstuck controller.

This package holds the AgentCore-compatible invocation surface, the DynamoDB
and S3 adapters behind the existing repository protocols, the deterministic
secret, egress, preflight and spend policies, and the in-process CDK synthesis
entry point.

Nothing here performs an AWS API call, reads an ambient credential, resolves a
CloudFormation context lookup, builds an image, deploys, or spends. Every
client is injected by the caller. Every claim about live AWS behaviour stays
Pending its named preflight.

The reason taxonomy below is the package's only error vocabulary. It is
deliberately defined here, with no imports from the submodules, so the
submodules can depend on it without a circular import.
"""

from enum import StrEnum


class CloudReason(StrEnum):
    """Explicit reason codes for every cloud-platform refusal.

    Values that mirror a core runtime `unstuck.domain.Reason` keep the core
    spelling so a terminal result reads the same wherever it was produced.
    """

    CONFIG_INVALID = "cloud_config_invalid"
    SECRET_REJECTED = "cloud_secret_rejected"
    KEY_SCOPE_DENIED = "cloud_key_scope_denied"
    PREFIX_SCOPE_DENIED = "cloud_prefix_scope_denied"
    EVALUATOR_DENIED = "cloud_evaluator_access_denied"
    EGRESS_DENIED = "cloud_egress_denied"
    SESSION_INVALID = "cloud_session_invalid"
    LEASE_REJECTED = "lease_rejected"
    LEASE_EXPIRED = "lease_expired_incomplete"
    SPEND_LIMIT_UNSET = "spend_limit_unset"
    PRICE_UNKNOWN = "price_input_unknown"
    SPEND_UNREADABLE = "current_spend_unreadable"
    ESTIMATE_EXCEEDS_LIMIT = "estimate_exceeds_remaining_limit"
    PREFLIGHT_REQUIRED = "preflight_required"
    OWNER_APPROVAL_REQUIRED = "owner_approval_required"
    STORAGE_CONDITION_FAILED = "storage_condition_failed"
    RECEIPT_INVALID = "receipt_invalid"
    RECEIPT_CORPUS_FROZEN = "receipt_corpus_frozen"
    NAMESPACE_DENIED = "receipt_namespace_denied"
    IDENTITY_UNVERIFIED = "session_identity_unverified"
    IDENTITY_REQUIRED = "session_identity_required"
    ARGUMENT_REJECTED = "task_argument_rejected"
    ROUTE_UNKNOWN = "route_unknown"
    RETENTION_PROTECTED = "retention_protected"
    SYNTHESIS_INVALID = "synthesis_invalid"
    #: The hosted Decider answered off its strict serving contract. It is a
    #: measurement about the answer, never a transport failure, so it carries
    #: no choice, no distribution and no confidence forward.
    DECIDER_CONTRACT_REJECTED = "decider_contract_rejected"
    #: The hosted Decider call failed, timed out, or never arrived. Nothing was
    #: learned about the model's shape and nothing is substituted.
    DECIDER_PROVIDER_ERROR = "decider_provider_error"
    #: Narration was asked for and could not be produced. The deterministic
    #: result is unchanged and is what the caller receives.
    NARRATION_UNAVAILABLE = "narration_unavailable"
    #: Narration was refused before any call: the result was rejected,
    #: unresolved, or never verified, so there is nothing settled to narrate.
    NARRATION_NOT_PERMITTED = "narration_not_permitted"


class CloudFault(Exception):
    """A reason-coded cloud-platform refusal. Never a bare exception."""

    def __init__(self, reason: CloudReason):
        self.reason = reason
        super().__init__(reason.value)


__all__ = ["CloudFault", "CloudReason"]
