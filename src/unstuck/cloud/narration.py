"""The Bedrock narrator: one bounded read of an already-finished result.

This is the last step of an invocation and it holds no authority at all. It
selects no tool, admits no evidence, authorizes no repair, determines no
terminal truth, and cannot be reached before the controller and the verifier
have finished. If it fails, the deterministic result is what the caller
receives and the response says narration was unavailable.

Four properties are structural rather than configured.

It cannot see the run. `TerminalProjection` is a closed allowlist of counted
facts: the terminal status, its reason code, the verification outcome, the
bounded counters, which reads were chosen, and whether a repetition was
blocked. The row bodies, the evidence identities, the receipt identities, the
task and query hashes, the verification identities and the artifact references
are all absent, so none of them is representable in a model input. The reported
confidences are absent too, and that is a deliberate exclusion rather than an
oversight: a narration that was handed a confidence could present it as
correctness, which this project does not do.

It cannot run on an unsettled result. `narratable` requires a terminal status
the verifier passed, so a rejected invocation, an unresolved run, a failed run,
a budget stop and anything whose verification did not run are all refused
before a call exists, with `narration_not_permitted`.

It cannot act. No `toolConfig` is sent, so the model has no tool to call, and
the response contract admits exactly one text block. Two blocks, a tool block,
an empty string, an oversized string, or a stop reason other than a completed
turn all report narration unavailable rather than being partially accepted.

It cannot escape its bounds. The client is injected and its own read timeout
must already bound the configured budget, which is checked at construction and
refuses rather than warns. The token ceiling is explicit and low, the prompt is
capped, and this module constructs no client, imports no AWS SDK, reads no
ambient credential, and opens no socket: authorization is the AWS SDK's own
SigV4 under the AgentCore execution role.

Nothing here is verified against a deployment. No Bedrock call has been
observed from this repository, so every latency, cost and availability claim
about this path stays Pending its named gate.
"""

import json
import re
import time
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import Field, JsonValue, ValidationError

from unstuck.cascade.serialization import StageMeasurement
from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.config import ModelIdentifier, SafeIdentifier
from unstuck.domain import Contract, Identifier

#: Which provider narrated. It is a label on the record, never authority.
NarrationProvider = Literal["bedrock"]
NARRATION_PROVIDER: NarrationProvider = "bedrock"

#: The explicit output ceiling. Narration is one short paragraph about facts
#: that are already settled, so a low bound is the contract rather than a
#: tuning choice, and a response that hits it is still refused unless it is a
#: complete turn.
NARRATION_MAX_TOKENS: Final = 200

#: The largest prompt this module sends and the largest narration it accepts.
#: The projection is a closed set of scalars, so anything approaching this is
#: already outside the contract.
NARRATION_MAX_CHARS: Final = 1200

#: The default wall-clock budget. The bound is enforced by the injected
#: client's own read timeout, which is checked at construction.
DEFAULT_NARRATION_MS: Final = 5_000.0
MAX_NARRATION_MS: Final = 10_000.0

#: The terminal statuses that may be narrated. Both of them are statuses the
#: core runtime only reaches with a verification that passed, and the check
#: below requires that outcome explicitly rather than relying on it.
NARRATABLE_STATUSES: frozenset[str] = frozenset({"completed", "empty_verified"})

#: The stop reasons that mean the model finished a turn of text. Every other
#: value the Converse contract defines, including `tool_use`, is refused.
#: Source: the `stopReason` valid values of the installed `bedrock-runtime`
#: `Converse` model.
ACCEPTED_STOP_REASONS: frozenset[str] = frozenset({"end_turn", "max_tokens"})

#: Exactly what a narration is handed, in the order it is declared.
#: `tests/cloud_platform/test_cloud_narration.py` asserts the projection
#: carries this set and nothing else, so a field added to the terminal result
#: cannot reach a model input by default.
PROJECTION_FIELDS: tuple[str, ...] = (
    "status",
    "reason_code",
    "disposition",
    "verification_status",
    "dataset_version",
    "row_count",
    "diagnostic_reads",
    "wait_checks",
    "repair_attempts",
    "pass_count",
    "selection_pass",
    "first_choice",
    "final_choice",
    "blocked_tool_id",
    "blocked_reason_code",
    "history_status",
    "history_truncated",
    "confidence_available",
    "classification",
)

#: The fixed instruction. It is the whole of the model's brief: describe the
#: facts it was given, and claim nothing beyond them.
NARRATION_SYSTEM: Final = (
    "You write one short plain paragraph describing a diagnostic run that has "
    "already finished. Every fact is supplied to you as JSON and is final. "
    "Describe only those facts. Do not judge whether the outcome is correct, "
    "do not infer a cause, do not suggest a next action, do not recommend a "
    "tool, and do not add any number, name, or claim that is not in the input. "
    "Write at most four sentences and no lists."
)

#: What an AWS request identifier may contain. A correlation handle only: it
#: names no principal, session, account, or Region.
_AWS_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class TerminalProjection(Contract):
    """The bounded sanitized facts a narration is allowed to see.

    Counted rather than quoted. `row_count` replaces the rows, and no hash,
    evidence identity, receipt identity, verification identity, artifact
    reference, confidence value, account, Region, principal, session or trace
    value is representable at all.
    """

    status: Literal["completed", "empty_verified"]
    reason_code: SafeIdentifier
    disposition: Literal["CONTINUE", "EMPTY"]
    verification_status: Literal["passed"]
    dataset_version: SafeIdentifier
    row_count: Annotated[int, Field(ge=0, le=100_000)]
    diagnostic_reads: Annotated[int, Field(ge=0, le=2)]
    wait_checks: Annotated[int, Field(ge=0, le=1)]
    repair_attempts: Annotated[int, Field(ge=0, le=1)]
    pass_count: Annotated[int, Field(ge=0, le=2)]
    selection_pass: Literal[1, 2] | None
    first_choice: Identifier | None
    final_choice: Identifier | None
    blocked_tool_id: Identifier | None
    blocked_reason_code: SafeIdentifier | None
    history_status: Literal["not_requested", "unavailable", "empty", "matched"]
    history_truncated: bool
    confidence_available: bool
    classification: Literal["synthetic", "live"] = "synthetic"

    def prompt_text(self) -> str:
        """The one canonical input a narration is handed. Bounded, or refused."""
        encoded = json.dumps(
            self.model_dump(mode="json", exclude={"schema_version"}),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded) > NARRATION_MAX_CHARS:
            raise CloudFault(CloudReason.NARRATION_NOT_PERMITTED)
        return encoded


class NarrationSettings(Contract):
    """The narration deployment inputs. No live default anywhere.

    The model identifier is required because a default would name somebody
    else's enabled model in somebody else's Region and would work exactly once.
    """

    model_id: ModelIdentifier
    max_tokens: Annotated[int, Field(ge=1, le=NARRATION_MAX_TOKENS)] = (
        NARRATION_MAX_TOKENS
    )
    timeout_ms: Annotated[
        float, Field(gt=0, le=MAX_NARRATION_MS, allow_inf_nan=False)
    ] = DEFAULT_NARRATION_MS

    @property
    def timeout_seconds(self) -> float:
        return self.timeout_ms / 1000.0


class NarrationResult(Contract):
    """What a narration attempt produced. It carries no authority field.

    There is deliberately no selection, probability, confidence, row,
    disposition or verification field here. A narration cannot change any of
    them, so it cannot report one either: the deterministic result stays the
    answer and this sits beside it.
    """

    status: Literal["narrated", "unavailable"]
    provider: NarrationProvider | None = None
    model_id: ModelIdentifier | None = None
    text: Annotated[str, Field(min_length=1, max_length=NARRATION_MAX_CHARS)] | None = (
        None
    )
    #: Why there is no text. `narration_unavailable` means a call was made and
    #: did not produce one; `narration_not_permitted` means no call was made at
    #: all, because the result was not settled. They are different facts about
    #: the same unchanged deterministic answer.
    reason_code: (
        Literal[CloudReason.NARRATION_UNAVAILABLE, CloudReason.NARRATION_NOT_PERMITTED]
        | None
    ) = None
    latency_ms: StageMeasurement = StageMeasurement()
    aws_request_id: Annotated[str, Field(pattern=_AWS_REQUEST_ID.pattern)] | None = None
    #: The failure class, when the call failed. A class name, never a message.
    failure: (
        Annotated[str, Field(max_length=128, pattern=r"^[A-Za-z0-9_]+$")] | None
    ) = None


class BedrockRuntime(Protocol):
    """The injected client surface. Exactly one operation, nothing wider."""

    def converse(self, **kwargs: Any) -> Mapping[str, Any]: ...


def not_permitted(model_id: str | None = None) -> NarrationResult:
    """No call was made. The result was not settled enough to describe."""
    return NarrationResult(
        status="unavailable",
        provider=NARRATION_PROVIDER,
        model_id=model_id,
        reason_code=CloudReason.NARRATION_NOT_PERMITTED,
        latency_ms=StageMeasurement(availability="not_called"),
    )


def narratable(result: Mapping[str, JsonValue] | None) -> bool:
    """Whether this result is settled enough to describe.

    A rejected invocation body, an unresolved or failed run, a budget stop and
    anything whose verification did not pass are all refused here, before a
    call exists.
    """
    if not isinstance(result, Mapping):
        return False
    return (
        result.get("status") in NARRATABLE_STATUSES
        and result.get("verification_status") == "passed"
        and isinstance(result.get("reason_code"), str)
    )


def terminal_projection(result: Mapping[str, JsonValue] | None) -> TerminalProjection:
    """Project one finished result onto the bounded allowlist. Fails closed.

    The row bodies are counted and dropped. Everything the allowlist does not
    name is simply never read, so a field added to the terminal result upstream
    cannot start travelling to a model by accident.
    """
    if not narratable(result):
        raise CloudFault(CloudReason.NARRATION_NOT_PERMITTED)
    assert isinstance(result, Mapping)  # narrowed by `narratable`
    rows = result.get("rows")
    carried: dict[str, JsonValue] = {
        name: result.get(name) for name in PROJECTION_FIELDS if name != "row_count"
    }
    carried["row_count"] = len(rows) if isinstance(rows, list) else 0
    try:
        projection = TerminalProjection.model_validate(carried)
    except ValidationError as error:
        raise CloudFault(CloudReason.NARRATION_NOT_PERMITTED) from error
    projection.prompt_text()
    return projection


def check_client_bounds(client: object, *, budget_seconds: float) -> None:
    """Refuse a client whose own timeout does not bound the narration budget.

    Converse carries no per-request deadline, so the client's read timeout is
    the deadline. Retries multiply the wall clock, so the bound is the
    per-attempt timeout times the attempt count rather than the timeout alone.
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
    metadata = response.get("ResponseMetadata")
    if not isinstance(metadata, Mapping):
        return None
    reported = metadata.get("RequestId")
    if not isinstance(reported, str):
        return None
    candidate = reported.strip()
    return candidate if _AWS_REQUEST_ID.fullmatch(candidate) else None


def one_text_block(response: Mapping[str, Any]) -> str | None:
    """The single narration text, or nothing. Partial shapes are refused.

    The field names are the installed `bedrock-runtime` `Converse` response
    members: `stopReason`, and `output.message.content`, whose blocks carry a
    `text` member. A block carrying anything else, including `toolUse`, is not
    a narration.
    """
    if response.get("stopReason") not in ACCEPTED_STOP_REASONS:
        return None
    output = response.get("output")
    if not isinstance(output, Mapping):
        return None
    message = output.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return None
    block = content[0]
    if not isinstance(block, Mapping) or set(block) != {"text"}:
        return None
    text = block.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped or len(stripped) > NARRATION_MAX_CHARS:
        return None
    return stripped


class BedrockNarrator:
    """One narration attempt per finished result. Never more, never retried.

    The client is injected, so this object resolves no credential and names no
    Region. It performs exactly one `Converse` call with no tool configuration,
    and any failure becomes `narration_unavailable` rather than an exception
    the caller has to catch: the deterministic result must survive a narration
    that did not work.
    """

    def __init__(
        self,
        client: BedrockRuntime,
        settings: NarrationSettings,
        *,
        clock: object | None = None,
    ) -> None:
        check_client_bounds(client, budget_seconds=settings.timeout_seconds)
        self.client = client
        self.settings = NarrationSettings.model_validate(settings)
        self._monotonic: Callable[[], float] = getattr(
            clock, "monotonic", time.perf_counter
        )

    def request_for(self, projection: TerminalProjection) -> dict[str, Any]:
        """The exact Converse parameters. Closed, bounded, and tool free."""
        return {
            "modelId": self.settings.model_id,
            "messages": [
                {"role": "user", "content": [{"text": projection.prompt_text()}]}
            ],
            "system": [{"text": NARRATION_SYSTEM}],
            "inferenceConfig": {
                "maxTokens": self.settings.max_tokens,
                "temperature": 0.0,
                "topP": 1.0,
            },
        }

    def narrate(self, projection: TerminalProjection) -> NarrationResult:
        """Attempt one narration. Returns a result; never raises for a failure."""
        started = self._monotonic()
        try:
            response = self.client.converse(**self.request_for(projection))
        except Exception as error:  # noqa: BLE001 - the failure class is evidence
            return self._unavailable(started, failure=type(error).__name__)
        measured = max(0.0, (self._monotonic() - started) * 1000.0)
        if not isinstance(response, Mapping):
            return self._unavailable(started)
        request_id = _request_id(response)
        text = one_text_block(response)
        if text is None or measured > self.settings.timeout_ms:
            return self._unavailable(started, request_id=request_id)
        try:
            return NarrationResult(
                status="narrated",
                provider=NARRATION_PROVIDER,
                model_id=self.settings.model_id,
                text=text,
                latency_ms=StageMeasurement(availability="measured", value=measured),
                aws_request_id=request_id,
            )
        except ValidationError:
            return self._unavailable(started, request_id=request_id)

    def _unavailable(
        self,
        started: float,
        *,
        failure: str | None = None,
        request_id: str | None = None,
    ) -> NarrationResult:
        """Narration did not happen. The deterministic result is unchanged."""
        return NarrationResult(
            status="unavailable",
            provider=NARRATION_PROVIDER,
            model_id=self.settings.model_id,
            reason_code=CloudReason.NARRATION_UNAVAILABLE,
            latency_ms=StageMeasurement(
                availability="measured",
                value=max(0.0, (self._monotonic() - started) * 1000.0),
            ),
            aws_request_id=request_id,
            failure=failure,
        )


__all__ = [
    "ACCEPTED_STOP_REASONS",
    "DEFAULT_NARRATION_MS",
    "MAX_NARRATION_MS",
    "NARRATABLE_STATUSES",
    "NARRATION_MAX_CHARS",
    "NARRATION_MAX_TOKENS",
    "NARRATION_PROVIDER",
    "NARRATION_SYSTEM",
    "PROJECTION_FIELDS",
    "BedrockNarrator",
    "BedrockRuntime",
    "NarrationProvider",
    "NarrationResult",
    "NarrationSettings",
    "TerminalProjection",
    "check_client_bounds",
    "narratable",
    "not_permitted",
    "one_text_block",
    "terminal_projection",
]
