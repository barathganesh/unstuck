"""Secret and serving-time egress guards.

`docs/spec/design.md` section 14 states the secret posture: there is no
external supplemental provider secret, runtime configuration carries only
identifiers and non-secret versions and hashes, and every receipt or model
input excludes credentials, authorization values, environment values,
evaluator truth, expected outcomes, customer identifiers, and model prose.

Egress is an allowlist, never a denylist. The controller reaches the named AWS
service endpoints and the private Decider API and nothing else. The Decider
host has an empty allowlist: design.md section 9 D6 gives it no serving-time
internet egress, and an empty policy denies by construction rather than by a
rule someone has to remember to write.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import Field

from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.config import CloudResourceConfig, sensitive_text
from unstuck.domain import Contract
from unstuck.events import Redactor

HTTPS_PORT = 443


class EgressDestination(Contract):
    """One permitted outbound destination. TLS only."""

    host: Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[a-z0-9.-]+$")]
    port: Annotated[int, Field(ge=1, le=65535)]
    scheme: Literal["https"] = "https"

    @property
    def authority(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


@dataclass(frozen=True)
class EgressPolicy:
    """A frozen allowlist. An empty allowlist permits nothing."""

    destinations: tuple[EgressDestination, ...] = field(default=())

    @property
    def allows_internet(self) -> bool:
        """Whether any serving-time outbound destination is permitted at all."""
        return bool(self.destinations)

    def check(self, destination: EgressDestination) -> None:
        if destination not in self.destinations:
            raise CloudFault(CloudReason.EGRESS_DENIED)

    def check_redirect(
        self, origin: EgressDestination, destination: EgressDestination
    ) -> None:
        """A redirect is a second destination and is checked as one."""
        self.check(origin)
        self.check(destination)


#: design.md section 9 D6: the Decider host has no serving-time internet egress.
DECIDER_HOST_EGRESS = EgressPolicy()


def controller_egress(config: CloudResourceConfig) -> EgressPolicy:
    """The controller's serving-time allowlist, derived from its own config."""
    region = config.region
    hosts = (
        f"dynamodb.{region}.amazonaws.com",
        f"s3.{region}.amazonaws.com",
        f"bedrock-runtime.{region}.amazonaws.com",
        f"logs.{region}.amazonaws.com",
        f"{config.decider_api_id}.execute-api.{region}.amazonaws.com",
    )
    return EgressPolicy(
        tuple(EgressDestination(host=host, port=HTTPS_PORT) for host in hosts)
    )


class SecretGuard:
    """Refuses a sensitive marker in any configuration key or value."""

    def __init__(self, redactor: Redactor | None = None):
        self.redactor = redactor or Redactor()

    def check(self, values: Mapping[str, object]) -> None:
        for key, value in values.items():
            if sensitive_text(key) or sensitive_text(str(value)):
                raise CloudFault(CloudReason.SECRET_REJECTED)

    def export(self, config: CloudResourceConfig) -> dict[str, str]:
        """The scannable configuration export. Checked before it is returned."""
        exported = {
            name: str(value) for name, value in config.model_dump(mode="json").items()
        }
        self.check(exported)
        return exported
