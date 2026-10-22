"""AgentCore-compatible invocation surface. Private container boundary only.

REQ-FUN-38 fixes the shape: an ARM64 container exposing `GET /ping` and
`POST /invocations`. REQ-SEC-04 fixes the posture: `0.0.0.0:8080` exists only
inside the private AgentCore Runtime boundary, no host port is published, and
the controller is reached only through SigV4 `InvokeAgentRuntime`. Neither is
verified; both stay Pending PF6.

Two properties are structural rather than configured. The bind address is a
closed contract, so no deployment can widen it or mark it published. And the
task is server-owned: `InvocationRequest` carries an idempotency key and
nothing else, so a caller cannot restate the task, the dataset, the date, or
the mode. An extra field is a refusal, not an ignored key.

Identity fails closed twice over. `UnverifiedIdentity` denies every invocation
and names the mechanism it is waiting for; `SessionHeaderIdentity` adds the one
check the container itself can make once the deployment inputs are validated,
and refuses without them. Neither invents a credential: authorization of the
caller is the service's own SigV4 on `InvokeAgentRuntime`, and the caller
principal AgentCore hands the container stays Pending PF6.

This module performs no socket work, ships no listener, and imports nothing
that could open one: the bind address exists here as a contract, the serving
layer and the adapter it wires live in `unstuck.cloud.agentcore_app`, and
`main` stays the fail-closed configuration report an operator can run to see
what is still absent.
"""

import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol, Self, cast
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, ValidationError, model_validator

from unstuck.cascade.policy import ACCEPTANCE_TIERS, AcceptanceTier
from unstuck.cascade.selection import Provider
from unstuck.cascade.serialization import MAX_DIAGNOSTIC_READS
from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.config import (
    AgentCoreName,
    ApiGatewayId,
    AwsAccountId,
    AwsRegion,
    BucketName,
    SafeIdentifier,
    TableName,
)
from unstuck.cloud.decider import DEFAULT_PROVIDER_MS, SageMakerDeciderSettings
from unstuck.cloud.narration import NarrationSettings
from unstuck.cloud.session import SESSION_PATTERN, RuntimeSessionId
from unstuck.domain import Contract, Identifier, TaskSpec
from unstuck.storage import Snapshot

#: The private AgentCore Runtime boundary. No host port is ever published, so
#: this address is reachable only from inside the container's own network.
CONTAINER_BIND_HOST: Final[Literal["0.0.0.0"]] = "0.0.0.0"
CONTAINER_BIND_PORT: Final[Literal[8080]] = 8080
MIN_INVOCATION_SECONDS = 120.0
MAX_INVOCATION_SECONDS = 900.0
PING_PATH = "/ping"
INVOCATIONS_PATH = "/invocations"

#: The request headers `InvokeAgentRuntime` forwards, spelled exactly as the
#: installed `botocore` model for the `bedrock-agentcore` data plane spells
#: them. They are transcribed rather than loaded so this module keeps no
#: dependency of its own, and
#: `tests/cloud_platform/test_agentcore_deployment.py` compares them against
#: that model, so a drift fails there instead of at deploy time.
#:
#: `Authorization` is deliberately not among them. The caller is authorized by
#: SigV4 before the request reaches the container, so a forwarded authorization
#: header would be a second, unverified credential path.
AGENTCORE_REQUEST_HEADERS: tuple[str, ...] = (
    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id",
    "X-Amzn-Bedrock-AgentCore-Runtime-User-Id",
    "X-Amzn-Trace-Id",
    "baggage",
    "traceparent",
    "tracestate",
)

#: The deployment inputs the container needs before it could serve anything.
#: Each one is injected by the deployment as an AgentCore environment variable
#: (`unstuck.cloud.deployment.agentcore_environment` builds the map). None of
#: them has a default here: a default would be a guess about somebody else's
#: account, and the entrypoint refuses while any is absent.
#:
#: The runtime ARN is deliberately not among them. It is an attribute of the
#: resource the container runs in, so injecting it into that resource's own
#: environment would be a circular CloudFormation reference; the name and
#: endpoint are injected instead and the ARN is a deploy output.
RUNTIME_IDENTITY_VARIABLES: tuple[str, ...] = (
    "UNSTUCK_ACCOUNT_ID",
    "UNSTUCK_ARTIFACT_BUCKET",
    "UNSTUCK_DATASET_VERSION",
    "UNSTUCK_DECIDER_API_ID",
    "UNSTUCK_MODEL_REVISION",
    "UNSTUCK_RECEIPT_TABLE",
    "UNSTUCK_REGION",
    "UNSTUCK_RUNTIME_ENDPOINT_NAME",
    "UNSTUCK_RUNTIME_NAME",
    "UNSTUCK_RUN_TABLE",
)

#: Which injected variable fills which `RuntimeSettings` field. This mapping is
#: the single definition of that correspondence, so neither side can gain a
#: variable the other does not know about; `tests/cloud_platform`
#: `test_agentcore_app.py` asserts it covers `RUNTIME_IDENTITY_VARIABLES`
#: exactly and that it names every settings field.
ENVIRONMENT_FIELDS: Mapping[str, str] = {
    "UNSTUCK_ACCOUNT_ID": "account_id",
    "UNSTUCK_ARTIFACT_BUCKET": "artifact_bucket",
    "UNSTUCK_DATASET_VERSION": "dataset_version",
    "UNSTUCK_DECIDER_API_ID": "decider_api_id",
    "UNSTUCK_MODEL_REVISION": "model_revision",
    "UNSTUCK_RECEIPT_TABLE": "receipt_table_name",
    "UNSTUCK_REGION": "region",
    "UNSTUCK_RUNTIME_ENDPOINT_NAME": "endpoint_name",
    "UNSTUCK_RUNTIME_NAME": "runtime_name",
    "UNSTUCK_RUN_TABLE": "run_table_name",
}

#: The forwarded session header, lowercased. HTTP header names are
#: case-insensitive, so every lookup normalizes before comparing.
SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"

#: Where the container keeps run state. Inside the image, owned by the
#: unprivileged user the `Dockerfile` creates, and never a published volume.
#: Run state is therefore per-container and ephemeral; the durable DynamoDB run
#: store is a separate, PF-gated step and no claim is made that it is in use.
CONTAINER_RUN_ROOT = Path("/opt/unstuck/var/runs")

#: The model revision the shipped deterministic stand-in implements. A
#: deployment that declares any other revision refuses to start, so a mock
#: provider can never be labelled as somebody's real Decider checkpoint.
MOCK_MODEL_REVISION = "scripted-v1"

#: Which provider this deployment serves. It is explicit rather than inferred
#: from whether the real variables happen to be present: a typo in one of them
#: must refuse rather than quietly serve the deterministic stand-in under a
#: deployment that was meant to be real. `mock` is the default, so an existing
#: deployment that adds no variable keeps exactly the path it already had.
ProviderMode = Literal["mock", "real"]
MODE_VARIABLE: Final = "UNSTUCK_PROVIDER_MODE"
DEFAULT_PROVIDER_MODE: ProviderMode = "mock"

#: The deployment inputs `real` mode additionally requires. Each is injected as
#: an AgentCore runtime environment variable and none has a default, because a
#: default would name somebody else's endpoint or somebody else's enabled
#: model. They are deliberately not part of `RUNTIME_IDENTITY_VARIABLES`: that
#: tuple is a closed set `agentcore_environment` is checked against, and mock
#: deployments must keep validating against it unchanged.
#:
#: The checkpoint revision is not among them because it already arrives as
#: `UNSTUCK_MODEL_REVISION`. In `mock` mode it must equal the stand-in's
#: revision and in `real` mode it must not, so one variable covers both and
#: neither mode can be labelled as the other.
REAL_PROVIDER_VARIABLES: tuple[str, ...] = (
    "UNSTUCK_DECIDER_ENDPOINT_NAME",
    "UNSTUCK_NARRATION_MODEL_ID",
)

#: The acceptance tier, which is optional and defaults to the product default.
#: It is a deployment input rather than a constant because the two tiers mean
#: different things and the relaxed one has a precondition that only the
#: offered options can satisfy. Nothing is weakened by offering it: the shipped
#: default stays `strict_confidence` with its 0.80 floor, and
#: `unstuck.cascade.selection.acceptable` refuses `read_only_structural` unless
#: every executable option offered is an argument-free local read, so the
#: condition is checked in code rather than asserted by a deployment.
TIER_VARIABLE: Final = "UNSTUCK_ACCEPTANCE_TIER"

#: The per-Decider-call budget, in whole or fractional seconds. Optional, and
#: a deployment input rather than a constant because how long a hosted
#: checkpoint takes to answer is a property of the instance type it is served
#: on, not of the product: the same image in front of a slower endpoint needs a
#: wider budget, and baking that number into the image makes the account and
#: instance it was measured against part of the artifact.
#:
#: Nothing is weakened by offering it. Absent keeps the shipped
#: `DEFAULT_PROVIDER_MS`, the ceiling is `MAX_PROVIDER_MS` rather than anything
#: a deployment can name, and `SageMakerDeciderSettings` still refuses a value
#: past it, so the widest configurable budget is two calls inside one run
#: deadline. The value reaches the SDK client's own read timeout and the
#: adapter's late-answer validator as the same number, which is the property
#: that keeps a synchronous call from outliving the bound it is measured
#: against.
DECIDER_TIMEOUT_VARIABLE: Final = "UNSTUCK_DECIDER_TIMEOUT_SECONDS"

#: What a budget may look like. A plain non-negative decimal and nothing else:
#: `nan`, `inf`, `1e400`, `0x3c`, `6_0`, `60s` and `60,0` are all accepted by
#: `float()` or silently mean something other than they appear to, and a
#: deployment input is read once at startup where there is nobody to ask, so
#: the surface is the literal an operator writes rather than everything the
#: parser tolerates.
_DECIDER_TIMEOUT_PATTERN: Final = re.compile(r"^\d+(?:\.\d+)?$")

#: The per-incident diagnostic-read bound the hosted engine enforces. Optional,
#: and a deployment input for the same reason as the budget above: which bound
#: an experiment runs under is the deployer's frozen choice, not a property of
#: the image. It may lower the shipped bound to one read and never raise it:
#: `MAX_DIAGNOSTIC_READS` stays the absolute maximum, `CascadeEngine` refuses
#: anything outside `[1, MAX_DIAGNOSTIC_READS]`, and `CascadePolicy` and
#: `policy_hash` are untouched, so receipt identity is unchanged.
#:
#: It is not a request field and not a confidence setting. It reaches the
#: Decider only as the remaining-read count the engine already derives.
DIAGNOSTIC_READS_VARIABLE: Final = "UNSTUCK_MAX_DIAGNOSTIC_READS"

#: What a configured bound may be: an integer in `[1, MAX_DIAGNOSTIC_READS]`,
#: written as its plain literal token and nothing else, so `01`, `+1`, `1.0`
#: and `2_0` refuse rather than parse. A `bool` is refused by the strict
#: contract rather than read as `1`.
DiagnosticReadBound = Annotated[int, Field(ge=1, le=MAX_DIAGNOSTIC_READS)]
_DIAGNOSTIC_READ_TOKENS: Final[Mapping[str, int]] = {
    str(bound): bound for bound in range(1, MAX_DIAGNOSTIC_READS + 1)
}


def decider_timeout_ms(environ: Mapping[str, str]) -> float:
    """The configured per-call budget in milliseconds. Invalid means refused.

    An empty or whitespace value counts as unsupplied, for the same reason it
    does in `missing_real_provider_inputs`: once it arrives, a variable set to
    the empty string is indistinguishable from one nobody supplied. Everything
    else either parses to a value `SageMakerDeciderSettings` accepts or raises,
    so a budget outside the bound refuses rather than clamping to something
    serviceable.
    """
    declared = environ.get(DECIDER_TIMEOUT_VARIABLE, "").strip()
    if not declared:
        return DEFAULT_PROVIDER_MS
    if not _DECIDER_TIMEOUT_PATTERN.fullmatch(declared):
        raise CloudFault(CloudReason.CONFIG_INVALID)
    return float(declared) * 1000.0


def diagnostic_read_bound(environ: Mapping[str, str]) -> int:
    """The configured diagnostic-read bound. Invalid means refused.

    An empty or whitespace value counts as unsupplied, as it does for every
    other optional deployment input, and keeps the shipped bound of two.
    """
    declared = environ.get(DIAGNOSTIC_READS_VARIABLE, "").strip()
    if not declared:
        return MAX_DIAGNOSTIC_READS
    if declared not in _DIAGNOSTIC_READ_TOKENS:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    return _DIAGNOSTIC_READ_TOKENS[declared]


class BindAddress(Contract):
    """The container's own boundary. Closed, and never published to a host."""

    host: Literal["0.0.0.0"] = CONTAINER_BIND_HOST
    port: Literal[8080] = CONTAINER_BIND_PORT
    published: Literal[False] = False

    @property
    def authority(self) -> str:
        return f"{self.host}:{self.port}"


class InvocationAllowance(Contract):
    """The wall-clock allowance one invocation receives."""

    seconds: Annotated[
        float,
        Field(
            ge=MIN_INVOCATION_SECONDS, le=MAX_INVOCATION_SECONDS, allow_inf_nan=False
        ),
    ] = MIN_INVOCATION_SECONDS


class InvocationRequest(Contract):
    """Everything a caller is allowed to say. The task is not negotiable."""

    idempotency_key: Annotated[Identifier, Field(min_length=1, max_length=128)]


class ReadinessResponse(Contract):
    status: Literal["ready"] = "ready"
    mode: Literal["offline"] = "offline"
    #: Which provider answers a selector call here. It defaults to `mock`, so
    #: an unlabelled readiness probe never reads as a real one.
    provider: Provider = "mock"


class InvocationResponse(Contract):
    """One structured answer. A refusal always carries its reason code.

    A duplicate of an invocation that already finished answers with the stored
    terminal result, because the run store is the durable idempotency
    authority and the work is done. A duplicate of one that is still owned
    answers `in_flight`: it acquires nothing, executes nothing, and decides
    nothing.
    """

    status: Literal["accepted", "duplicate", "in_flight", "lease_rejected", "rejected"]
    run_id: UUID | None = None
    runtime_session_id: str | None = None
    reason_code: CloudReason | None = None
    result: dict[str, JsonValue] | None = None
    mode: Literal["offline"] = "offline"
    #: Which provider answered the selector calls of this invocation. It
    #: defaults to `mock`, so an unlabelled answer never reads as a real one.
    provider: Provider = "mock"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        answered = self.status in {"accepted", "duplicate"}
        if answered != (self.reason_code is None):
            raise ValueError("a refused invocation carries its reason code")
        if answered and (
            self.run_id is None
            or self.runtime_session_id is None
            or self.result is None
        ):
            raise ValueError("an answered invocation carries its run and result")
        if not answered and self.result is not None:
            raise ValueError("a refused invocation carries no result")
        if self.status == "rejected" and self.run_id is not None:
            raise ValueError("a rejected invocation admitted no run")
        if self.status != "rejected" and self.runtime_session_id is None:
            raise ValueError("an admitted run carries its session identity")
        return self


class IdentityVerifier(Protocol):
    """Resolves the caller principal from the invocation headers."""

    def verify(self, headers: Mapping[str, str]) -> str: ...


class UnverifiedIdentity:
    """Fail-closed verifier. Denies every request until PF6 lands.

    A stub that cannot verify a real credential rejects every request, so the
    absence of the mechanism can never read as an authorized call.
    """

    mechanism = (
        "SigV4 InvokeAgentRuntime caller principal supplied by Bedrock AgentCore; "
        "Pending PF6"
    )

    def verify(self, headers: Mapping[str, str]) -> str:
        raise CloudFault(CloudReason.IDENTITY_UNVERIFIED)


class RuntimeSettings(Contract):
    """The injected deployment inputs, validated. No credential is representable.

    Every field reuses the constrained vocabulary of `unstuck.cloud.config`, so
    a value shaped like a credential, an authorization header, or an evaluator
    reference is refused by the contract rather than redacted afterwards. There
    is no default anywhere: a default would be a guess about somebody else's
    account, and an unconfigured deployment must refuse rather than resolve.

    The runtime ARN is deliberately absent, for the same reason it is absent
    from `RUNTIME_IDENTITY_VARIABLES`: it is an attribute of the resource this
    container runs in, so injecting it would be a circular reference. The name
    and the endpoint name are injected instead.
    """

    region: AwsRegion
    account_id: AwsAccountId
    runtime_name: AgentCoreName
    endpoint_name: AgentCoreName
    model_revision: SafeIdentifier
    dataset_version: SafeIdentifier
    run_table_name: TableName
    receipt_table_name: TableName
    artifact_bucket: BucketName
    decider_api_id: ApiGatewayId

    @property
    def bound_identity(self) -> str:
        """The resource this container runs in, never a caller-supplied value."""
        return f"runtime/{self.runtime_name}/runtime-endpoint/{self.endpoint_name}"


def runtime_settings(environ: Mapping[str, str]) -> RuntimeSettings:
    """Read and validate the injected deployment inputs. Absent means refused.

    Two refusals collapse into one reason code because a caller cannot act on
    the difference: a variable nobody supplied and a variable whose value the
    contract rejects both leave the container without a topology to serve
    against. `missing_runtime_identity` names the first group for an operator,
    and the validation error names the second, so neither is silent.
    """
    if missing_runtime_identity(environ):
        raise CloudFault(CloudReason.CONFIG_INVALID)
    try:
        return RuntimeSettings.model_validate(
            {field: environ[name].strip() for name, field in ENVIRONMENT_FIELDS.items()}
        )
    except ValidationError as error:
        raise CloudFault(CloudReason.CONFIG_INVALID) from error


class RealProviderSettings(Contract):
    """The hosted Decider and the narrator, together, or neither.

    They travel as one object because `real` mode means both: a deployment with
    a Decider endpoint and no narration model, or the reverse, is a half
    configuration that would serve one real path and silently fall back on the
    other. `real_provider_settings` therefore refuses unless every input is
    present and valid.
    """

    decider: SageMakerDeciderSettings
    narration: NarrationSettings
    acceptance_tier: AcceptanceTier = "strict_confidence"
    max_diagnostic_reads: DiagnosticReadBound = MAX_DIAGNOSTIC_READS


def provider_mode(environ: Mapping[str, str]) -> ProviderMode:
    """Resolve the declared provider mode. An unknown token refuses.

    An empty value counts as unsupplied rather than as a mode nobody declared,
    so an existing deployment that sets nothing keeps `mock`.
    """
    declared = environ.get(MODE_VARIABLE, "").strip() or DEFAULT_PROVIDER_MODE
    if declared not in {"mock", "real"}:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    return "real" if declared == "real" else "mock"


def missing_real_provider_inputs(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Which `real` mode inputs are absent, in a fixed order.

    An empty value counts as absent, for the same reason it does in
    `missing_runtime_identity`: once it arrives, a variable set to the empty
    string is indistinguishable from one nobody supplied.
    """
    return tuple(
        name for name in REAL_PROVIDER_VARIABLES if not environ.get(name, "").strip()
    )


def real_provider_settings(
    environ: Mapping[str, str], *, model_revision: str
) -> RealProviderSettings:
    """Build the hosted provider settings. Absent or invalid means refused.

    `model_revision` is the already-validated `UNSTUCK_MODEL_REVISION` of the
    deployment, so the endpoint is bound to the exact checkpoint identity every
    answer is checked against. A deployment declaring the stand-in's revision
    in `real` mode refuses here: a scripted revision cannot be the identity a
    hosted checkpoint answers under.
    """
    if model_revision == MOCK_MODEL_REVISION:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    if missing_real_provider_inputs(environ):
        raise CloudFault(CloudReason.CONFIG_INVALID)
    tier = environ.get(TIER_VARIABLE, "").strip() or "strict_confidence"
    if tier not in ACCEPTANCE_TIERS:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    try:
        return RealProviderSettings(
            decider=SageMakerDeciderSettings(
                endpoint_name=environ["UNSTUCK_DECIDER_ENDPOINT_NAME"].strip(),
                model_revision=model_revision,
                provider_ms=decider_timeout_ms(environ),
            ),
            narration=NarrationSettings(
                model_id=environ["UNSTUCK_NARRATION_MODEL_ID"].strip()
            ),
            acceptance_tier=cast(AcceptanceTier, tier),
            max_diagnostic_reads=diagnostic_read_bound(environ),
        )
    except ValidationError as error:
        raise CloudFault(CloudReason.CONFIG_INVALID) from error


class SessionHeaderIdentity:
    """Verifies the session identity AgentCore forwards. SigV4 is the gate.

    Authorization of the caller happens at the AgentCore service boundary
    before a request reaches this container: `InvokeAgentRuntime` is SigV4
    signed and exactly one role holds that action against exactly the runtime
    and its endpoint. This verifier neither repeats that nor invents a second
    credential path. There is no shared secret to present, and no
    `Authorization`, API key, or cookie header is read at all.

    What it adds is the one check the container itself can make: the forwarded
    session identity must be present and must satisfy the charset and length
    bound `unstuck.cloud.session` already fixes, so a request that arrived
    without one, or with one the rest of the system could not name, refuses
    instead of running.

    The principal it returns is the bound runtime resource, taken from the
    validated deployment inputs, so no caller can name a principal of its own
    choosing. The caller principal the service actually hands the container is
    a different fact and stays Pending PF6: this verifier does not claim to
    resolve one, and the exact service-side `runtimeSessionId` bound stays
    Pending with it.
    """

    mechanism = (
        "AWS SigV4 InvokeAgentRuntime authorization at the AgentCore service "
        "boundary, plus the forwarded runtime session identity; the caller "
        "principal the service hands the container is Pending PF6"
    )

    def __init__(self, settings: RuntimeSettings):
        self.settings = settings

    def verify(self, headers: Mapping[str, str]) -> str:
        forwarded = ""
        for name, value in headers.items():
            if name.lower() == SESSION_HEADER:
                forwarded = value.strip()
                break
        if not forwarded:
            raise CloudFault(CloudReason.IDENTITY_REQUIRED)
        if re.fullmatch(SESSION_PATTERN, forwarded) is None:
            raise CloudFault(CloudReason.SESSION_INVALID)
        return self.settings.bound_identity


class RunAdmission(Protocol):
    """The repository surface admission needs. Nothing wider.

    `create_run` is the durable idempotency authority: one task and one
    idempotency key resolve to one run, and a repeated invocation is handed
    that same run rather than a new one. `load_run` reads it back when another
    writer won the lease in between. This layer keeps no cache of its own.
    """

    def create_run(
        self, task: TaskSpec, dataset_version: str, idempotency_key: str
    ) -> Snapshot: ...
    def load_run(self, run_id: UUID) -> Snapshot: ...
    def acquire(
        self, run_id: UUID, owner: UUID, *, run_seconds: float = 120.0
    ) -> Snapshot | None: ...


#: The injected run loop. It receives the snapshot this layer already leased
#: and the owner holding that lease, so it must continue from them rather than
#: acquire the same run again: `Controller.resume`, never `Controller.run`.
Execute = Callable[[Snapshot, UUID], dict[str, JsonValue]]


class ControllerRuntime:
    """Admission for one immutable task. The run loop is injected.

    This layer owns lease acquisition. It creates the run from the caller's
    idempotency key, acquires the single writer lease, and only then hands the
    leased snapshot and its owner to the injected run loop. A duplicate never
    reaches the run loop and no second acquisition of the same run is ever
    attempted: the durable run record says whether the work is finished, in
    which case its stored result is returned, or still owned, in which case
    the invocation is answered `in_flight`.
    """

    def __init__(
        self,
        repository: RunAdmission,
        task: TaskSpec,
        dataset_version: str,
        *,
        execute: Execute,
        verifier: IdentityVerifier | None = None,
        bind: BindAddress | None = None,
        invocation_seconds: float = MIN_INVOCATION_SECONDS,
    ):
        self.repository = repository
        self.task = task
        self.dataset_version = dataset_version
        self.execute = execute
        self.verifier: IdentityVerifier = verifier or UnverifiedIdentity()
        self.bind = bind or BindAddress()
        self.allowance = InvocationAllowance(seconds=invocation_seconds)

    @property
    def invocation_seconds(self) -> float:
        return self.allowance.seconds

    def ping(self) -> ReadinessResponse:
        return ReadinessResponse()

    def invoke(
        self, headers: Mapping[str, str], body: Mapping[str, JsonValue] | None
    ) -> InvocationResponse:
        self.verifier.verify(headers)
        try:
            request = InvocationRequest.model_validate(dict(body or {}))
        except ValidationError as error:
            raise CloudFault(CloudReason.ARGUMENT_REJECTED) from error
        snapshot = self.repository.create_run(
            self.task, self.dataset_version, request.idempotency_key
        )
        run_id = snapshot.state.run_id
        if snapshot.state.status != "created":
            return self._duplicate(snapshot)
        owner = uuid4()
        leased = self.repository.acquire(
            run_id, owner, run_seconds=min(self.invocation_seconds, 120.0)
        )
        if leased is None:
            # Another writer admitted this run between the two durable reads.
            return self._duplicate(self.repository.load_run(run_id))
        return InvocationResponse(
            status="accepted",
            run_id=run_id,
            runtime_session_id=RuntimeSessionId.for_run(run_id).value,
            result=self.execute(leased, owner),
        )

    def _duplicate(self, snapshot: Snapshot) -> InvocationResponse:
        """Answer a duplicate from the durable record, never by executing."""
        run_id = snapshot.state.run_id
        session = RuntimeSessionId.for_run(run_id).value
        if snapshot.result is not None:
            return InvocationResponse(
                status="duplicate",
                run_id=run_id,
                runtime_session_id=session,
                result=snapshot.result,
            )
        return InvocationResponse(
            status="in_flight",
            run_id=run_id,
            runtime_session_id=session,
            reason_code=CloudReason.LEASE_REJECTED,
        )

    def handle(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Mapping[str, JsonValue] | None,
    ) -> tuple[int, dict[str, JsonValue]]:
        """Dispatch one request. Every unknown route fails closed."""
        if (method, path) == ("GET", PING_PATH):
            return 200, self.ping().model_dump(mode="json", exclude={"schema_version"})
        if (method, path) != ("POST", INVOCATIONS_PATH):
            return 404, self._refusal(CloudReason.ROUTE_UNKNOWN)
        try:
            response = self.invoke(headers, body)
        except CloudFault as fault:
            status = 400 if fault.reason is CloudReason.ARGUMENT_REJECTED else 403
            return status, self._refusal(fault.reason)
        return 200, response.model_dump(mode="json", exclude={"schema_version"})

    def _refusal(self, reason: CloudReason) -> dict[str, JsonValue]:
        return InvocationResponse(status="rejected", reason_code=reason).model_dump(
            mode="json", exclude={"schema_version"}
        )


#: What an operator must wire before this container can serve a request.
SERVE_CHECKLIST = (
    "Inject every RUNTIME_IDENTITY_VARIABLES entry as an AgentCore runtime "
    "environment variable; the entrypoint refuses while any is absent.",
    "Establish PF6: AgentCore Runtime registration, the runtimeSessionId "
    "charset and length bound, and private API routing.",
    "Substitute a verifier that resolves the SigV4 InvokeAgentRuntime caller "
    "principal for UnverifiedIdentity, then grant action-level authorization.",
    "Establish PF8: anonymous-denial checks for the unsigned AgentCore invoke, "
    "the unsigned Decider API call, and the anonymous S3 GET.",
    "Publish no host port. The container boundary is the only reachable scope.",
)


def missing_runtime_identity(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Which injected deployment inputs are absent, in a fixed order.

    An empty value counts as absent. A variable set to the empty string is
    indistinguishable from one nobody supplied once it arrives, so treating it
    as supplied would let an unconfigured deployment read as a configured one.
    """
    return tuple(
        name for name in RUNTIME_IDENTITY_VARIABLES if not environ.get(name, "").strip()
    )


def main(environ: Mapping[str, str] | None = None) -> int:
    """Fail-closed configuration report. Says what is absent, serves nothing.

    The container's serving entrypoint is the server layer in
    `unstuck.cloud.agentcore_app`. This remains the report an operator can run
    by hand (`python -m unstuck.cloud.runtime`) to see what a deployment is
    still missing, and it opens no listener and serves no request.

    Two distinct refusals, in the order a deployment hits them. Configuration
    comes first: without the injected deployment inputs there is no topology to
    serve against, and the refusal names every variable that is missing.
    Identity comes second: with configuration present the shipped verifier is
    still `UnverifiedIdentity`, which denies every request until PF6 establishes
    the principal AgentCore hands the container.

    Either way the process refuses and exits non-zero. No listener is opened,
    no port is published, and no request is ever served from here.
    """
    supplied = os.environ if environ is None else environ
    missing = missing_runtime_identity(supplied)
    report = {
        "readiness": ReadinessResponse().model_dump(
            mode="json", exclude={"schema_version"}
        ),
        "bind": BindAddress().model_dump(mode="json", exclude={"schema_version"}),
        "serving": "refused",
        "reason_code": (
            CloudReason.CONFIG_INVALID.value
            if missing
            else CloudReason.IDENTITY_UNVERIFIED.value
        ),
        "mechanism": UnverifiedIdentity.mechanism,
        "missing_deployment_inputs": list(missing),
        "forwarded_headers": list(AGENTCORE_REQUEST_HEADERS),
        "checklist": list(SERVE_CHECKLIST),
    }
    sys.stderr.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 1


__all__ = [
    "AGENTCORE_REQUEST_HEADERS",
    "CONTAINER_BIND_HOST",
    "CONTAINER_BIND_PORT",
    "CONTAINER_RUN_ROOT",
    "DEFAULT_PROVIDER_MODE",
    "ENVIRONMENT_FIELDS",
    "INVOCATIONS_PATH",
    "MAX_INVOCATION_SECONDS",
    "MIN_INVOCATION_SECONDS",
    "DECIDER_TIMEOUT_VARIABLE",
    "DIAGNOSTIC_READS_VARIABLE",
    "MOCK_MODEL_REVISION",
    "MODE_VARIABLE",
    "PING_PATH",
    "REAL_PROVIDER_VARIABLES",
    "RUNTIME_IDENTITY_VARIABLES",
    "SESSION_HEADER",
    "TIER_VARIABLE",
    "BindAddress",
    "ProviderMode",
    "RealProviderSettings",
    "decider_timeout_ms",
    "diagnostic_read_bound",
    "missing_real_provider_inputs",
    "provider_mode",
    "real_provider_settings",
    "ControllerRuntime",
    "Execute",
    "IdentityVerifier",
    "InvocationAllowance",
    "InvocationRequest",
    "InvocationResponse",
    "ReadinessResponse",
    "RuntimeSettings",
    "SERVE_CHECKLIST",
    "RunAdmission",
    "SessionHeaderIdentity",
    "UnverifiedIdentity",
    "main",
    "missing_runtime_identity",
    "runtime_settings",
]


if __name__ == "__main__":
    raise SystemExit(main())
