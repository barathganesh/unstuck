"""The hosted Decider 2B adapter: a private SageMaker real-time endpoint.

This is the cloud counterpart of `evaluation.decider_provider`. It owns the
same three things and nothing else:

1. It builds the payload with the shipped `unstuck.cascade.choice.choice_request`
   and validates the answer with the shipped `validate_choice`, so the
   criteria, the option order, the bounded state text and the acceptance shape
   are the product's rather than this module's.
2. It stamps the provider identity and the configured immutable model revision
   on the answer, and records one observation per call with its own wall-clock
   measurement and the AWS correlation the SDK reported.
3. It substitutes nothing. A transport failure, a late answer, a body outside
   the strict serving contract and an answer outside the offered options each
   become a typed refusal carrying no choice, no distribution and no
   confidence, and there is no fallback order anywhere in this file.

What it is not: it selects no tool, it blocks no execution, it writes no
receipt, it labels no outcome, and it reads no evaluator truth. Decider 2B
remains the only model tool selector; this module is the transport that reaches
it.

Three properties are structural rather than configured.

The endpoint name has no ambient default. `SageMakerDeciderSettings` requires
it, so an unconfigured deployment cannot resolve one, and no account, Region,
ARN or model identifier is spelled anywhere in this file. The client itself is
injected, so this module reads no ambient credential chain, constructs no
client, imports no AWS SDK, and opens no socket; authorization is the SDK's
own SigV4 under the AgentCore execution role, applied by the injected client.

The wire contract is closed in both directions. `DeciderServeRequest` carries
the question, the bounded state text, the ordered options with their criteria,
the model revision and the call identity, and nothing else; an extra field is a
refusal rather than an ignored key. `DeciderServeResponse` is closed the same
way, and a nonfinite probability, confidence or latency is refused by the
contract rather than normalized afterwards.

The deadline is enforced twice. The injected client's own read timeout and
retry count must already bound the provider budget, which is checked at
construction and refuses rather than warns: a blocking SDK call cannot be
preempted by the caller's `asyncio.timeout`, so the bound has to live in the
client. On top of that this adapter measures the call itself and treats an
answer later than the budget as a provider failure rather than a slow success.

A hosted checkpoint is a real provider. Whether it is a *live* one is a
separate fact: no provider boundary event has been observed for this path, so
PF7 stays Pending and nothing produced here supports a latency, cost or
benchmark claim.
"""

import json
import re
import time
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field, ValidationError

from unstuck.cascade.choice import (
    ChoiceOption,
    ChoiceOutcome,
    ChoiceRequest,
    ProviderChoice,
    choice_request,
    validate_choice,
)
from unstuck.cascade.matching import HistoryStatus
from unstuck.cascade.projector import QUESTION, Question, SelectorInput
from unstuck.cascade.selection import Provider, RawSelection, ValidationStatus
from unstuck.cascade.serialization import SELECTOR_INPUT_MAX_CHARS, StageMeasurement
from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.config import SafeIdentifier, SageMakerEndpointName
from unstuck.decision import OptionProbability
from unstuck.domain import Contract, Digest, Identifier, Probability
from unstuck.telemetry.runtime import scoped_digest

#: The provider identity a hosted checkpoint answers with. It is the same value
#: `evaluation.decider_provider` uses, because it is the same checkpoint
#: answering the same seam; only the transport differs.
REAL_PROVIDER: Provider = "decider"

#: The one media type the serving contract uses, in both directions. A response
#: declaring anything else is refused rather than sniffed.
DECIDER_CONTENT_TYPE = "application/json"

#: The largest request this adapter will send and the largest response it will
#: read. The state text is already capped at `SELECTOR_INPUT_MAX_CHARS` and at
#: most four bounded criteria travel with it, so anything approaching these is
#: already outside the contract, and refusing on length means an oversized body
#: is never parsed.
MAX_SERVE_REQUEST_BYTES = 32_768
MAX_SERVE_RESPONSE_BYTES = 8_192

#: The project's own provider budget, in milliseconds. An answer later than
#: this is a provider failure rather than a slow success. The ceiling matches
#: `unstuck.domain.Settings.provider_seconds`, so a deployment cannot configure
#: a budget the core runtime would not honour.
#:
#: The ceiling and the default are separate numbers on purpose. The default is
#: the fast offline budget every local path keeps, and the ceiling is the run
#: ceiling halved: a deployment whose hosted checkpoint is slower than the
#: default reconfigures the budget rather than rebuilding the image, and two
#: calls at the widest it can name still fit inside one shared run deadline.
DEFAULT_PROVIDER_MS = 20_000.0
MAX_PROVIDER_MS = 60_000.0

#: The domain the endpoint name is hashed under for the observation record. The
#: name is a resource identifier rather than a secret, but it is a deployment
#: value, so what travels on the record is the correlation handle and not the
#: name itself.
ENDPOINT_DIGEST_DOMAIN = "sagemaker-decider-endpoint"

#: What an AWS request identifier may contain. The SDK reports an opaque
#: correlation token; anything outside this charset or longer than this is not
#: one, and is dropped rather than recorded.
AWS_REQUEST_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
AwsRequestId = Annotated[str, Field(pattern=AWS_REQUEST_ID_PATTERN)]
_AWS_REQUEST_ID = re.compile(AWS_REQUEST_ID_PATTERN)


class DeciderServeRequest(Contract):
    """The complete bounded payload one hosted Decider call is made from.

    Closed on purpose. The server has no reason to know the account, the
    Region, the run, the dataset, the scenario, the receipts, the evaluator or
    anything else, so none of it is representable here: the fields are the
    question, the bounded canonical state text, the ordered options with their
    criteria and executability, and the identity the answer must come back
    under.
    """

    question: Question = QUESTION
    state_text: Annotated[str, Field(min_length=1, max_length=SELECTOR_INPUT_MAX_CHARS)]
    options: Annotated[tuple[ChoiceOption, ...], Field(min_length=2, max_length=4)]
    model_revision: Identifier
    model_call_id: UUID


class DeciderServeResponse(Contract):
    """One answer, exactly as the strict serving contract defines it.

    Nonfinite values are refused by the fields themselves rather than
    normalized later, so a `NaN` confidence can never reach the acceptance
    rule as a number. Everything else about the answer is still untrusted:
    `validate_choice` checks it against the offered options, and nothing here
    is authority.
    """

    model_revision: Identifier
    model_call_id: UUID
    choice: Annotated[str, Field(min_length=1, max_length=128)]
    probabilities: Annotated[
        dict[str, Annotated[float, Field(allow_inf_nan=False)]],
        Field(min_length=1, max_length=8),
    ]
    confidence: Annotated[float, Field(allow_inf_nan=False)]
    latency_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]

    def as_provider_choice(self) -> ProviderChoice:
        """The shipped untrusted answer shape, for the shipped validator."""
        return ProviderChoice(
            model_revision=self.model_revision,
            model_call_id=self.model_call_id,
            choice=self.choice,
            probabilities={
                option: float(value) for option, value in self.probabilities.items()
            },
            confidence=float(self.confidence),
            latency_ms=float(self.latency_ms),
        )


class SageMakerDeciderSettings(Contract):
    """The hosted Decider deployment inputs. No live default anywhere.

    The endpoint name is required because a default would be a guess about
    somebody else's account, and the revision is required because an answer
    that cannot be checked against an immutable checkpoint identity is not
    evidence about that checkpoint.
    """

    endpoint_name: SageMakerEndpointName
    model_revision: SafeIdentifier
    provider_ms: Annotated[
        float, Field(gt=0, le=MAX_PROVIDER_MS, allow_inf_nan=False)
    ] = DEFAULT_PROVIDER_MS

    @property
    def provider_seconds(self) -> float:
        return self.provider_ms / 1000.0

    @property
    def endpoint_digest(self) -> str:
        """The correlation handle for this endpoint. Never the name itself."""
        return scoped_digest(ENDPOINT_DIGEST_DOMAIN, self.endpoint_name)


class CloudProviderObservation(Contract):
    """One hosted provider call, recorded as observed. Carries no truth."""

    provider: Provider
    model_revision: Identifier
    endpoint_digest: Digest
    decision_id: UUID
    model_call_id: UUID
    pass_index: Literal[1, 2]
    option_ids: Annotated[tuple[Identifier, ...], Field(min_length=2, max_length=4)]
    validation_status: ValidationStatus
    selector_input_hash: Digest
    decision_context_hash: Digest
    history_status: HistoryStatus
    state_chars: Annotated[int, Field(ge=1, le=SELECTOR_INPUT_MAX_CHARS)]
    measured_latency_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    selected_id: Identifier | None = None
    confidence: Probability | None = None
    probabilities: (
        Annotated[tuple[OptionProbability, ...], Field(max_length=4)] | None
    ) = None
    #: The opaque SDK correlation token for the call, when the response carried
    #: one. It names no principal, session or account.
    aws_request_id: AwsRequestId | None = None
    #: The failure class, when the call failed. A class name, never a message.
    failure: str | None = None


class SageMakerRuntime(Protocol):
    """The injected client surface. Exactly one operation, nothing wider."""

    def invoke_endpoint(self, **kwargs: Any) -> Mapping[str, Any]: ...


def serve_request(
    payload: ChoiceRequest, *, model_revision: str, call_id: UUID
) -> DeciderServeRequest:
    """Build the wire payload from one already-validated Choice request.

    The criteria and the option order are carried across unchanged, because
    they are the product's: `choice_request` derived them from each card's own
    published surface, and re-deriving them here would be a second source of
    truth for what the provider is shown.
    """
    try:
        return DeciderServeRequest(
            question=payload.question,
            state_text=payload.state_text,
            options=payload.options,
            model_revision=model_revision,
            model_call_id=call_id,
        )
    except ValidationError as error:
        raise CloudFault(CloudReason.CONFIG_INVALID) from error


def encode_serve_request(request: DeciderServeRequest) -> bytes:
    """Serialize to the one canonical form. Refuses anything over the bound."""
    document = request.model_dump(mode="json")
    try:
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except ValueError as error:  # pragma: no cover - the contract refuses first
        raise CloudFault(CloudReason.DECIDER_CONTRACT_REJECTED) from error
    if len(encoded) > MAX_SERVE_REQUEST_BYTES:
        raise CloudFault(CloudReason.DECIDER_CONTRACT_REJECTED)
    return encoded


def decode_serve_response(body: bytes) -> DeciderServeResponse:
    """Parse one answer against the closed contract. Fails closed.

    An oversized body is refused on length before it is parsed. Everything
    else collapses into one reason code because a caller cannot act on the
    difference: a body that is not JSON, is not an object, omits a field,
    carries an extra one, or carries a nonfinite number all leave the adapter
    without an answer the acceptance rule could read.

    Validation happens in JSON mode rather than over a parsed dictionary. The
    contract is strict, so a UUID arrives as the JSON string the wire actually
    carries only in JSON mode, and the strict parser is also what refuses the
    non-standard `NaN` and `Infinity` literals outright.
    """
    if not body or len(body) > MAX_SERVE_RESPONSE_BYTES:
        raise CloudFault(CloudReason.DECIDER_CONTRACT_REJECTED)
    try:
        return DeciderServeResponse.model_validate_json(body)
    except ValidationError as error:
        raise CloudFault(CloudReason.DECIDER_CONTRACT_REJECTED) from error


def check_client_bounds(client: object, *, budget_seconds: float) -> None:
    """Refuse a client whose own timeout does not bound the provider budget.

    This is the one check that cannot be deferred. The SDK call is synchronous,
    so a caller's `asyncio.timeout` cannot preempt it, and a client with no read
    timeout would hold the whole invocation open past every deadline the run has.
    Retries multiply the wall clock, so the bound is the per-attempt timeout
    times the attempt count rather than the timeout alone.
    """
    config = getattr(getattr(client, "meta", None), "config", None)
    read_timeout = getattr(config, "read_timeout", None)
    if not isinstance(read_timeout, (int, float)) or read_timeout <= 0:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    retries = getattr(config, "retries", None) or {}
    attempts = retries.get("total_max_attempts") or retries.get("max_attempts") or 1
    if not isinstance(attempts, int) or attempts < 1:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    if float(read_timeout) * attempts > budget_seconds:
        raise CloudFault(CloudReason.CONFIG_INVALID)


def _request_id(response: Mapping[str, Any]) -> str | None:
    """The opaque SDK correlation token, when the response carried a usable one.

    It is a correlation handle and nothing else: it names no principal, no
    session, no account and no Region, and a value outside the bounded charset
    is dropped rather than recorded, so an unexpected shape cannot smuggle text
    onto an observation.
    """
    metadata = response.get("ResponseMetadata")
    if not isinstance(metadata, Mapping):
        return None
    reported = metadata.get("RequestId")
    if not isinstance(reported, str):
        return None
    candidate = reported.strip()
    return candidate if _AWS_REQUEST_ID.fullmatch(candidate) else None


def _body_bytes(response: Mapping[str, Any]) -> bytes:
    """Read the streamed body once, bounded. A wider read is never attempted."""
    stream = response.get("Body")
    reader: Callable[..., Any] | None = getattr(stream, "read", None)
    if reader is None:
        raise CloudFault(CloudReason.DECIDER_CONTRACT_REJECTED)
    raw = reader(MAX_SERVE_RESPONSE_BYTES + 1)
    if not isinstance(raw, (bytes, bytearray)):
        raise CloudFault(CloudReason.DECIDER_CONTRACT_REJECTED)
    return bytes(raw)


class CloudDeciderProvider:
    """One hosted Decider 2B selector. It answers; it decides nothing else.

    The client is injected, so this object neither resolves a credential nor
    names a Region: the deployment hands it a `sagemaker-runtime` client whose
    SigV4 identity is the AgentCore execution role, and the endpoint name comes
    from validated runtime configuration.
    """

    def __init__(
        self,
        client: SageMakerRuntime,
        settings: SageMakerDeciderSettings,
        *,
        clock: object | None = None,
    ) -> None:
        check_client_bounds(client, budget_seconds=settings.provider_seconds)
        self.client = client
        self.settings = SageMakerDeciderSettings.model_validate(settings)
        # The clock is accepted so a caller can supply the run's own, but the
        # measurement this adapter reports is a wall-clock duration, so a
        # monotonic source is used when the supplied clock offers one.
        self._monotonic: Callable[[], float] = getattr(
            clock, "monotonic", time.perf_counter
        )
        self.observations: list[CloudProviderObservation] = []

    @property
    def model_version(self) -> str:
        """The immutable checkpoint identity every answer is checked against."""
        return self.settings.model_revision

    async def select(self, request: SelectorInput) -> RawSelection:
        """Answer one selector call through the shipped Choice seam."""
        payload = choice_request(request)
        call_id = uuid4()
        decision_id = uuid4()
        started = self._monotonic()
        answer, failure, request_id = self._call(payload, call_id)
        measured = max(0.0, (self._monotonic() - started) * 1000.0)
        if failure is not None:
            # Nothing was learned about the model's shape.
            outcome = ChoiceOutcome(validation_status="error")
        elif answer is None:
            # A shape the contract cannot carry is a measurement about the
            # answer rather than a transport failure, so it is `invalid`.
            outcome = ChoiceOutcome(validation_status="invalid")
        elif measured > self.settings.provider_ms:
            # A late answer is ignored for execution and never retried.
            outcome = ChoiceOutcome(validation_status="error")
        else:
            outcome = validate_choice(
                answer.as_provider_choice(),
                payload,
                expected_revision=self.settings.model_revision,
                timeout_ms=self.settings.provider_ms,
                expected_call_id=call_id,
            )
        self.observations.append(
            CloudProviderObservation(
                provider=REAL_PROVIDER,
                model_revision=self.settings.model_revision,
                endpoint_digest=self.settings.endpoint_digest,
                decision_id=decision_id,
                model_call_id=call_id,
                pass_index=request.pass_index,
                option_ids=payload.option_ids,
                validation_status=outcome.validation_status,
                selector_input_hash=request.input_hash,
                decision_context_hash=request.decision_context_hash,
                history_status=request.history.status,
                state_chars=len(payload.state_text),
                measured_latency_ms=measured,
                selected_id=outcome.selected_id,
                confidence=outcome.confidence,
                probabilities=outcome.probabilities,
                aws_request_id=request_id,
                failure=failure,
            )
        )
        return RawSelection(
            model_revision=self.settings.model_revision,
            decision_id=decision_id,
            model_call_id=call_id,
            pass_index=request.pass_index,
            candidate_ids=payload.option_ids,
            validation_status=outcome.validation_status,
            provider=REAL_PROVIDER,
            selected_id=outcome.selected_id,
            probabilities=outcome.probabilities,
            confidence=outcome.confidence,
            latency_ms=StageMeasurement(availability="measured", value=measured),
        )

    def _call(
        self, payload: ChoiceRequest, call_id: UUID
    ) -> tuple[DeciderServeResponse | None, str | None, str | None]:
        """Make one call. Returns the answer, the failure class, and the token.

        Nothing is inferred and nothing is repaired. A call that raises is a
        transport failure named by its exception class, never by its message,
        so no provider text can reach a record. A response outside the strict
        contract returns no answer and no failure, which is what separates a
        measurement about the answer from one about the transport.
        """
        body = encode_serve_request(
            serve_request(
                payload, model_revision=self.settings.model_revision, call_id=call_id
            )
        )
        try:
            response = self.client.invoke_endpoint(
                EndpointName=self.settings.endpoint_name,
                ContentType=DECIDER_CONTENT_TYPE,
                Accept=DECIDER_CONTENT_TYPE,
                Body=body,
            )
        except Exception as error:  # noqa: BLE001 - the failure class is evidence
            return None, type(error).__name__, None
        if not isinstance(response, Mapping):
            return None, None, None
        request_id = _request_id(response)
        declared = response.get("ContentType")
        if isinstance(declared, str) and (
            declared.split(";")[0].strip().lower() != DECIDER_CONTENT_TYPE
        ):
            return None, None, request_id
        try:
            return decode_serve_response(_body_bytes(response)), None, request_id
        except CloudFault:
            return None, None, request_id


__all__ = [
    "DECIDER_CONTENT_TYPE",
    "DEFAULT_PROVIDER_MS",
    "ENDPOINT_DIGEST_DOMAIN",
    "MAX_PROVIDER_MS",
    "MAX_SERVE_REQUEST_BYTES",
    "MAX_SERVE_RESPONSE_BYTES",
    "REAL_PROVIDER",
    "AwsRequestId",
    "CloudDeciderProvider",
    "CloudProviderObservation",
    "DeciderServeRequest",
    "DeciderServeResponse",
    "SageMakerDeciderSettings",
    "SageMakerRuntime",
    "check_client_bounds",
    "decode_serve_response",
    "encode_serve_request",
    "serve_request",
]
