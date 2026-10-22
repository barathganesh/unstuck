"""Private S3 artifact store with prefix scoping and evaluator denial.

`docs/spec/design.md` sections 15 and 16 fix the shape. The store is bound to
one bucket at construction and its write and read surfaces take keys, never
buckets, so the ordinary path cannot address another bucket at all. The one
method that does accept a bucket refuses anything but the bound bucket, and
refuses a declared evaluator bucket by its own reason code, before a request is
built.

Keys are scoped by `unstuck.cloud.keys.ArtifactPrefix`: writes reach `runs/*`
and `receipt-evidence/*`, reads add read-only `fixtures/public/*`, and anything
reaching `models/*`, `experiments/*`, or the evaluator fixtures is refused.

Redaction happens before a body is handed to the client, not after. The client
must already be TLS bound; a plaintext endpoint is refused at construction.
Static prefix scoping is not per-run isolation and a planned bucket policy is
not a denied request: REQ-SEC-11, REQ-SEC-12 and PF8 stay Pending.
"""

import json
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from pydantic import JsonValue, ValidationError

from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.config import CloudResourceConfig
from unstuck.cloud.keys import ArtifactPrefix, receipt_evidence_key, run_artifact_key
from unstuck.events import Redactor

SERVER_SIDE_ENCRYPTION = "AES256"
CONTENT_TYPE = "application/json"


class S3Client(Protocol):
    """The narrow surface this store uses. Always injected, never built."""

    def put_object(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...


def _scoped(key: str, mode: str) -> ArtifactPrefix:
    try:
        return ArtifactPrefix.model_validate({"key": key, "mode": mode})
    except ValidationError as error:
        raise CloudFault(CloudReason.PREFIX_SCOPE_DENIED) from error


class S3ArtifactStore:
    """One bucket, scoped prefixes, redacted bodies, encryption in transit."""

    def __init__(
        self,
        client: S3Client,
        config: CloudResourceConfig,
        *,
        redactor: Redactor | None = None,
        denied_buckets: frozenset[str] = frozenset(),
    ):
        endpoint = getattr(getattr(client, "meta", None), "endpoint_url", "")
        if not str(endpoint).startswith("https://"):
            raise CloudFault(CloudReason.CONFIG_INVALID)
        self._client = client
        self._config = config
        self._redactor = redactor or Redactor()
        self._denied = frozenset(denied_buckets)

    @property
    def bucket(self) -> str:
        return self._config.artifact_bucket

    def run_key(self, run_id: UUID, name: str) -> str:
        return _scoped(run_artifact_key(run_id, name).key, "write").key

    def receipt_key(self, receipt_id: str, name: str) -> str:
        return _scoped(receipt_evidence_key(receipt_id, name).key, "write").key

    def put(self, key: str, body: JsonValue) -> str:
        """Redact, then write. The client never sees the original body."""
        scoped = _scoped(key, "write")
        cleaned = self._redactor.clean(body)
        payload = json.dumps(cleaned, sort_keys=True, separators=(",", ":")).encode()
        self._client.put_object(
            Bucket=self.bucket,
            Key=scoped.key,
            Body=payload,
            ContentType=CONTENT_TYPE,
            ServerSideEncryption=SERVER_SIDE_ENCRYPTION,
        )
        return scoped.key

    def get(self, key: str) -> JsonValue:
        scoped = _scoped(key, "read")
        response = self._client.get_object(Bucket=self.bucket, Key=scoped.key)
        raw = response["Body"].read()
        decoded: JsonValue = json.loads(raw)
        return decoded

    def read_from(self, bucket: str, key: str) -> JsonValue:
        """Refuse any bucket but the bound one, before a request exists."""
        if bucket in self._denied:
            raise CloudFault(CloudReason.EVALUATOR_DENIED)
        if bucket != self.bucket:
            raise CloudFault(CloudReason.PREFIX_SCOPE_DENIED)
        return self.get(key)


__all__ = [
    "CONTENT_TYPE",
    "SERVER_SIDE_ENCRYPTION",
    "S3ArtifactStore",
    "S3Client",
]
