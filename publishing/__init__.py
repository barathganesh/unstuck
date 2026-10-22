"""Publication support: the claims audit, the submission checklist, outlines.

Nothing in this package publishes, uploads, pushes, tags a release, or sends an
external communication. Publishing and submitting are separate operator actions
(REQ-PUB-08) and require the owner.

Its job is the opposite of persuasion: every claim must name the recorded
evidence that supports it, and a claim whose evidence is absent is refused by
category. No figure, finding, or rubric score originates here.

The directory is named `publishing` rather than `publication` on purpose: the
PyPI distribution `publication` is a transitive dependency of `aws_cdk`, and a
top-level `publication` package on the path shadows it and breaks CDK
synthesis. The name is the only thing that changed.
"""

from enum import StrEnum


class PublicationReason(StrEnum):
    """Explicit reason codes for every publication refusal."""

    EVIDENCE_MISSING = "publication_evidence_missing"
    EVIDENCE_UNRESOLVED = "publication_evidence_path_unresolved"
    EVIDENCE_PATH_REJECTED = "publication_evidence_path_rejected"
    EVIDENCE_SYMLINK = "publication_evidence_symlink_rejected"
    EVIDENCE_NOT_A_FILE = "publication_evidence_not_a_regular_file"
    EVIDENCE_SCHEMA_INVALID = "publication_evidence_schema_invalid"
    EVIDENCE_SUBJECT_MISMATCH = "publication_evidence_subject_mismatch"
    EVIDENCE_DECISION_MISMATCH = "publication_evidence_decision_mismatch"
    EVIDENCE_HASH_MISMATCH = "publication_evidence_hash_mismatch"
    EVIDENCE_ROOT_REJECTED = "publication_evidence_root_rejected"
    APPROVAL_BINDING_MISSING = "publication_owner_approval_binding_missing"
    APPROVAL_MANIFEST_MISMATCH = "publication_owner_approval_manifest_mismatch"
    APPROVAL_ARTIFACT_CHANGED = "publication_owner_approval_artifact_changed"
    CLAIM_UNSUPPORTED = "publication_claim_unsupported"
    CLAIM_FORBIDDEN = "publication_claim_category_forbidden"
    LABEL_MISSING = "publication_synthetic_label_missing"
    PREFIX_UNDISCLOSED = "publication_controlled_prefix_undisclosed"
    CHECKLIST_INCOMPLETE = "publication_checklist_incomplete"
    OWNER_APPROVAL_REQUIRED = "publication_owner_approval_required"
    VERIFICATION_MISSING = "publication_verification_missing"
    OUTLINE_CLAIM_PRESENT = "publication_outline_carries_a_claim"


class PublicationFault(Exception):
    """A reason-coded publication refusal. Never a bare exception."""

    def __init__(self, reason: PublicationReason, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(reason.value)


__all__ = ["PublicationFault", "PublicationReason"]
