"""Repository-contained, typed publication evidence (REQ-PUB-06, NFR8.2).

Four separate defects are answered here. A path was resolved with
`(root / relative).exists()`, so `../../../../etc/hosts` satisfied a claim; a
path that existed was treated as proof, so no record had to say anything; the
declared root was canonicalized rather than checked, so a root that was itself
a link turned its target into the trusted root; and an owner approval was bound
only to the checklist item it named, so it approved no particular content.

This module therefore does three things and nothing else.

It verifies the declared root structurally: the root is absolute, is not itself
a link, and has no linked component. The verified root directory is then opened
once per operation, component by component, with no-follow semantics, and every
declared path is resolved relative to that descriptor the same way. A link
anywhere on the way, a directory, a device, a file with more than one link, and
anything whose canonical location escapes the root are refusals. The root's own
device and inode are recorded at construction and re-checked on every open, so
a root replaced underneath the boundary is refused rather than read.

It reads a declared evidence record as a typed document, checking the subject
it names, the decision it records, the moment and version it was recorded at,
and its content hash where the declaration binds one.

And it binds an owner approval to the exact artifact set it approves. An owner
approval reference carries both the content hash of the approval record and the
hash of the artifact manifest that record authorizes; the record carries the
manifest itself; and every bound artifact's current content is hashed and
compared before the approval is accepted. A changed artifact, a changed
manifest, or an absent binding is a refusal, so an approval can never authorize
content the owner did not approve. Publishing and submitting carry separate
manifests, so neither approval can ever stand in for the other.

Nothing here publishes, uploads, sends, or submits anything. It reads files and
returns verdicts.
"""

import errno
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, PrivateAttr, field_validator, model_validator

from publishing import PublicationFault, PublicationReason
from unstuck.domain import Contract, Digest, Identifier, canonical_hash

#: The record kinds a declaration may require. `publish-approval` and
#: `submit-approval` are separate kinds because they are separate decisions.
RecordType = Literal[
    "claim-evidence",
    "verification",
    "publish-approval",
    "submit-approval",
]
#: What a record may record. A declined decision is recorded, never a pass.
RecordDecision = Literal["recorded", "approved", "declined"]
#: The two owner decisions, and the item each belongs to.
OWNER_RECORD_TYPES: dict[str, RecordType] = {
    "publish-approval": "publish-approval",
    "submit-approval": "submit-approval",
}
#: What each owner decision approves. Publishing binds the public artifact set;
#: submitting binds the submission payload and its required links. They are
#: separate manifests, so one approval can never authorize the other's content.
ManifestKind = Literal["publication", "submission"]
MANIFEST_KIND_FOR: dict[str, ManifestKind] = {
    "publish-approval": "publication",
    "submit-approval": "submission",
}
#: The errno values a link or a non-directory on the path reports under
#: no-follow semantics. They are the refusal, not a retry condition.
_LINK_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK, errno.ENOTDIR})


class ArtifactEntry(Contract):
    """One artifact an owner approval is bound to, by path and content."""

    path: Annotated[str, Field(min_length=1, max_length=512)]
    content_hash: Digest


class ArtifactManifest(Contract):
    """The immutable artifact set one owner decision approves.

    The entries are ordered by path so the manifest hash is a function of the
    set and not of the order it was written in. Any added, removed, renamed or
    rewritten artifact changes the hash.
    """

    kind: ManifestKind
    entries: Annotated[tuple[ArtifactEntry, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        paths = [entry.path for entry in self.entries]
        if len(set(paths)) != len(paths):
            raise ValueError("an artifact path appears twice")
        if paths != sorted(paths):
            raise ValueError("artifact entries are ordered by path")
        return self

    @property
    def manifest_hash(self) -> str:
        return canonical_hash(
            {
                "kind": self.kind,
                "entries": [
                    {"path": entry.path, "content_hash": entry.content_hash}
                    for entry in self.entries
                ],
            }
        )


class ApprovalBinding(Contract):
    """What exactly an owner approval authorizes.

    The manifest hash is recomputed from the entries carried here, so a record
    cannot name one hash and list another set. A submission additionally binds
    its payload and the links it declares resolve, because submitting is a
    different decision from publishing and authorizes different content.
    """

    manifest_kind: ManifestKind
    artifact_manifest_hash: Digest
    artifacts: Annotated[tuple[ArtifactEntry, ...], Field(min_length=1, max_length=64)]
    submission_payload_hash: Digest | None = None
    required_links: Annotated[tuple[str, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def bound(self) -> Self:
        manifest = ArtifactManifest(kind=self.manifest_kind, entries=self.artifacts)
        if manifest.manifest_hash != self.artifact_manifest_hash:
            raise ValueError("the manifest hash does not describe these artifacts")
        if self.manifest_kind == "submission":
            if self.submission_payload_hash is None:
                raise ValueError("a submission approval binds its payload")
            if not self.required_links:
                raise ValueError("a submission approval names its required links")
        elif self.submission_payload_hash is not None:
            raise ValueError("a publication approval binds no submission payload")
        return self

    @property
    def manifest(self) -> ArtifactManifest:
        return ArtifactManifest(kind=self.manifest_kind, entries=self.artifacts)


class EvidenceReference(Contract):
    """One declared evidence record: where it is and what it must say.

    The declaration is the requirement. A record that resolves but names
    another subject, records another decision, or no longer hashes to the
    declared content is not this evidence.

    An owner approval binds two hashes or neither: the content hash of the
    approval record, and the hash of the artifact manifest it authorizes.
    Neither is the shipped state of a decision nobody has taken, and it cannot
    pass; one without the other is a contradiction and is refused outright.
    """

    path: Annotated[str, Field(min_length=1, max_length=512)]
    record_type: RecordType
    subject_id: Identifier
    decision: RecordDecision = "recorded"
    content_hash: Digest | None = None
    artifact_manifest_hash: Digest | None = None

    @model_validator(mode="after")
    def owner_binding(self) -> Self:
        if self.record_type not in MANIFEST_KIND_FOR:
            if self.artifact_manifest_hash is not None:
                raise ValueError("only an owner approval binds an artifact manifest")
            return self
        if (self.content_hash is None) != (self.artifact_manifest_hash is None):
            raise ValueError(
                "an owner approval binds both its content and its artifact manifest"
            )
        return self

    @property
    def owner_decision(self) -> bool:
        return self.record_type in MANIFEST_KIND_FOR

    @property
    def bound(self) -> bool:
        """Whether this declaration names the content it approves."""
        return self.content_hash is not None and (
            not self.owner_decision or self.artifact_manifest_hash is not None
        )


class EvidenceRecordBody(Contract):
    """The typed body of one recorded evidence document."""

    record_type: RecordType
    subject_id: Identifier
    decision: RecordDecision
    recorded_at: datetime
    record_version: Identifier
    detail: Annotated[str, Field(max_length=1024)] = ""
    classification: Literal["synthetic", "offline", "live"] = "offline"
    #: Required on an owner approval, and forbidden on anything else: an
    #: approval that names no artifact set approves nothing in particular.
    approval: ApprovalBinding | None = None

    @field_validator("recorded_at")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("UTC timestamp required")
        return value

    @model_validator(mode="after")
    def owner_approvals_bind_their_artifacts(self) -> Self:
        expected = MANIFEST_KIND_FOR.get(self.record_type)
        if expected is None:
            if self.approval is not None:
                raise ValueError("only an owner approval carries an artifact binding")
            return self
        if self.approval is None:
            raise ValueError("an owner approval names the artifact set it approves")
        if self.approval.manifest_kind != expected:
            raise ValueError(f"a {self.record_type} binds a {expected} manifest")
        return self


class ResolvedEvidence(Contract):
    """One file that really is inside the root, with its content hash."""

    declared: Annotated[str, Field(min_length=1, max_length=512)]
    relative: Annotated[str, Field(min_length=1, max_length=512)]
    content_hash: Digest
    bytes_read: Annotated[int, Field(ge=0)]


class EvidenceRoot(Contract):
    """The one verified root every declared path is opened relative to.

    The root is explicit: this module never infers it from the working
    directory, an environment variable, or the location of a claim. It is also
    never canonicalized into something else. A root that is itself a link, or
    that has a linked component, is refused at construction, because the
    caller declared that path and not wherever it points.
    """

    root: Path
    _identity: tuple[int, int] = PrivateAttr(default=(0, 0))

    @model_validator(mode="after")
    def verified(self) -> Self:
        if not self.root.is_absolute():
            raise ValueError("the evidence root is an absolute path")
        if Path(os.path.realpath(self.root)) != self.root:
            raise PublicationFault(
                PublicationReason.EVIDENCE_ROOT_REJECTED,
                f"{self.root} resolves through a link",
            )
        with self._open_root() as descriptor:
            status = os.fstat(descriptor)
        self._identity = (status.st_dev, status.st_ino)
        return self

    @contextmanager
    def _open_root(self) -> Iterator[int]:
        """Open the declared root, component by component, following no link."""
        try:
            descriptor = os.open(
                self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError as error:
            raise PublicationFault(
                PublicationReason.EVIDENCE_ROOT_REJECTED, str(self.root)
            ) from error
        try:
            for component in self.root.parts[1:]:
                descriptor = _descend(descriptor, component, str(self.root))
            yield descriptor
        finally:
            os.close(descriptor)

    @property
    def canonical_root(self) -> Path:
        """The verified root itself. Nothing is resolved into anything else."""
        return self.root

    def resolve(self, declared: str) -> ResolvedEvidence:
        """Resolve one declared relative path, or refuse with a reason."""
        resolved, _ = self.read(declared)
        return resolved

    def read(self, declared: str) -> tuple[ResolvedEvidence, bytes]:
        """Resolve and read one declared path under no-follow semantics.

        The file is read from the descriptor that was checked, so the bytes
        hashed are the bytes of the file that passed containment rather than of
        whatever the path names by the time it is opened again.
        """
        relative = _relative(declared)
        with self._open_root() as root_descriptor:
            status = os.fstat(root_descriptor)
            if (status.st_dev, status.st_ino) != self._identity:
                raise PublicationFault(
                    PublicationReason.EVIDENCE_ROOT_REJECTED,
                    "the evidence root was replaced",
                )
            descriptor = root_descriptor
            opened: int | None = None
            try:
                for component in relative.parts[:-1]:
                    nested = _descend(descriptor, component, declared)
                    if descriptor is not root_descriptor:
                        os.close(descriptor)
                    descriptor = nested
                opened = _open_file(descriptor, relative.parts[-1], declared)
                body = _read_regular(opened, declared)
            finally:
                if descriptor is not root_descriptor:
                    os.close(descriptor)
                if opened is not None:
                    os.close(opened)
        return (
            ResolvedEvidence(
                declared=declared,
                relative=str(relative),
                content_hash=hashlib.sha256(body).hexdigest(),
                bytes_read=len(body),
            ),
            body,
        )

    def read_record(self, reference: EvidenceReference) -> EvidenceRecordBody:
        """Resolve and validate one declared record. Existence proves nothing."""
        resolved, body = self.read(reference.path)
        if (
            reference.content_hash is not None
            and reference.content_hash != resolved.content_hash
        ):
            raise PublicationFault(
                PublicationReason.EVIDENCE_HASH_MISMATCH,
                f"{reference.path} is not the content this claim was bound to",
            )
        try:
            record = EvidenceRecordBody.model_validate_json(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise PublicationFault(
                PublicationReason.EVIDENCE_SCHEMA_INVALID,
                f"{reference.path} is not a typed evidence record",
            ) from error
        if record.record_type != reference.record_type:
            raise PublicationFault(
                PublicationReason.EVIDENCE_SCHEMA_INVALID,
                f"{reference.path} records a {record.record_type}",
            )
        if record.subject_id != reference.subject_id:
            raise PublicationFault(
                PublicationReason.EVIDENCE_SUBJECT_MISMATCH,
                f"{reference.path} names {record.subject_id}",
            )
        if record.decision != reference.decision:
            raise PublicationFault(
                PublicationReason.EVIDENCE_DECISION_MISMATCH,
                f"{reference.path} records {record.decision}",
            )
        if reference.owner_decision:
            self._require_approved_artifacts(reference, record)
        return record

    def _require_approved_artifacts(
        self, reference: EvidenceReference, record: EvidenceRecordBody
    ) -> None:
        """Hold the approval to the exact artifact set it was bound to."""
        if not reference.bound:
            raise PublicationFault(
                PublicationReason.APPROVAL_BINDING_MISSING,
                (
                    f"{reference.path} is awaited: an owner approval names the "
                    "content hash of its record and the hash of the artifact "
                    "manifest it approves"
                ),
            )
        approval = record.approval
        if approval is None:
            raise PublicationFault(
                PublicationReason.APPROVAL_BINDING_MISSING,
                f"{reference.path} approves no artifact set",
            )
        if approval.artifact_manifest_hash != reference.artifact_manifest_hash:
            raise PublicationFault(
                PublicationReason.APPROVAL_MANIFEST_MISMATCH,
                (
                    f"{reference.path} approves another artifact manifest than "
                    "the one this item is bound to"
                ),
            )
        for entry in approval.artifacts:
            try:
                observed = self.resolve(entry.path)
            except PublicationFault as fault:
                raise PublicationFault(
                    PublicationReason.APPROVAL_ARTIFACT_CHANGED,
                    f"{entry.path}: {fault.detail or fault.reason.value}",
                ) from fault
            if observed.content_hash != entry.content_hash:
                raise PublicationFault(
                    PublicationReason.APPROVAL_ARTIFACT_CHANGED,
                    f"{entry.path} is not the content the owner approved",
                )


def _descend(descriptor: int, component: str, declared: str) -> int:
    """Open one directory component, following no link."""
    try:
        return os.open(
            component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
        )
    except FileNotFoundError as error:
        raise PublicationFault(
            PublicationReason.EVIDENCE_UNRESOLVED, declared
        ) from error
    except OSError as error:
        if error.errno in _LINK_ERRNOS:
            raise PublicationFault(
                PublicationReason.EVIDENCE_SYMLINK,
                f"{declared} resolves through a link",
            ) from error
        raise PublicationFault(
            PublicationReason.EVIDENCE_PATH_REJECTED, declared
        ) from error


def _open_file(descriptor: int, name: str, declared: str) -> int:
    try:
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
    except FileNotFoundError as error:
        raise PublicationFault(
            PublicationReason.EVIDENCE_UNRESOLVED, declared
        ) from error
    except OSError as error:
        if error.errno in _LINK_ERRNOS:
            raise PublicationFault(PublicationReason.EVIDENCE_SYMLINK, declared) from (
                error
            )
        raise PublicationFault(
            PublicationReason.EVIDENCE_PATH_REJECTED, declared
        ) from error


def _read_regular(descriptor: int, declared: str) -> bytes:
    """Read one regular, single-linked file from a verified descriptor."""
    status = os.fstat(descriptor)
    if stat.S_ISLNK(status.st_mode):
        raise PublicationFault(PublicationReason.EVIDENCE_SYMLINK, declared)
    if not stat.S_ISREG(status.st_mode):
        raise PublicationFault(PublicationReason.EVIDENCE_NOT_A_FILE, declared)
    if status.st_nlink != 1:
        raise PublicationFault(
            PublicationReason.EVIDENCE_NOT_A_FILE,
            f"{declared} is reachable under more than one name",
        )
    try:
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            return handle.read()
    except OSError as error:
        raise PublicationFault(
            PublicationReason.EVIDENCE_UNRESOLVED, declared
        ) from error


def _relative(declared: str) -> Path:
    """Reject the path shapes that must never be resolved at all."""
    if not declared or declared != declared.strip():
        raise PublicationFault(
            PublicationReason.EVIDENCE_PATH_REJECTED, "empty or padded path"
        )
    relative = Path(declared)
    if relative.is_absolute() or relative.anchor:
        raise PublicationFault(
            PublicationReason.EVIDENCE_PATH_REJECTED, f"{declared} is absolute"
        )
    if any(part in {"..", "", "."} for part in relative.parts):
        raise PublicationFault(
            PublicationReason.EVIDENCE_PATH_REJECTED, f"{declared} traverses"
        )
    return relative


def artifact_manifest(
    root: EvidenceRoot, kind: ManifestKind, paths: tuple[str, ...]
) -> ArtifactManifest:
    """Hash the artifact set as it is on disk right now.

    Offered so a recorder binds the content it actually has rather than a hash
    it typed. Every path is resolved inside the verified root first.
    """
    entries = tuple(
        ArtifactEntry(path=path, content_hash=root.resolve(path).content_hash)
        for path in sorted(set(paths))
    )
    return ArtifactManifest(kind=kind, entries=entries)


def record_document(body: EvidenceRecordBody) -> str:
    """Render a typed record. Offered so a recorder cannot invent a shape."""
    return json.dumps(
        body.model_dump(mode="json", exclude={"schema_version"}),
        indent=2,
        sort_keys=True,
    )


__all__ = [
    "MANIFEST_KIND_FOR",
    "OWNER_RECORD_TYPES",
    "ApprovalBinding",
    "ArtifactEntry",
    "ArtifactManifest",
    "EvidenceRecordBody",
    "EvidenceReference",
    "EvidenceRoot",
    "ManifestKind",
    "RecordDecision",
    "RecordType",
    "ResolvedEvidence",
    "artifact_manifest",
    "record_document",
]
