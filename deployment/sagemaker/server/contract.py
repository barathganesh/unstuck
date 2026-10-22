"""The strict internal serving contract, in both directions.

This is a deliberate mirror of `unstuck.cloud.decider.DeciderServeRequest` and
`DeciderServeResponse`, not an import of them. The reason is a boundary rather
than a preference: the model host must not carry the controller, because the
controller's package reaches run state, receipts and artifacts, and a role that
only serves a checkpoint has no business being able to name any of them. So the
shapes are declared here and
`tests/cloud_platform/test_sagemaker_server.py` asserts them field for field
against the adapter's, which is what keeps the two from drifting.

Both shapes are closed. An extra field is a refusal rather than an ignored key,
in either direction, and a nonfinite probability, confidence or latency is
refused by the field itself rather than normalized afterwards.

What is deliberately absent from the request is everything the server has no
reason to know: there is no account, Region, run, dataset, scenario, mode,
provider, receipt, model path, device or evaluator field, so none of them is
representable and a caller cannot introduce one.
"""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: The one media type the contract uses, in both directions.
SERVE_CONTENT_TYPE = "application/json"

#: The two paths the SageMaker container contract requires. Source: "Custom
#: Inference Code with Hosting Services", which fixes a web server on port
#: 8080 accepting `POST /invocations` and `GET /ping`.
PING_PATH = "/ping"
INVOCATIONS_PATH = "/invocations"

#: The one question key the provider's Choice primitive is asked under. It is
#: the same key `evaluation.decider_provider` uses, because it is the same
#: library answering the same question.
QUESTION_KEY = "d1_tool_choice"

#: The file the model artifact carries to declare which immutable checkpoint
#: revision it holds. A snapshot directory does not record its own revision, so
#: the packaging step writes it and the server checks it: that is what makes
#: "this endpoint serves exactly this revision" a checkable claim rather than a
#: label.
REVISION_FILE = "MODEL_REVISION"

#: The largest request body this server will read. The state text is capped at
#: 8,000 characters and at most four bounded criteria travel with it, so
#: anything approaching this is already outside the contract and refusing on
#: length means an oversized body is never parsed.
MAX_REQUEST_BYTES = 32_768

#: The bounds the controller's own payload already respects, transcribed so the
#: server refuses the same shapes rather than trusting the caller to.
STATE_TEXT_MAX_CHARS = 8_000
CRITERION_MAX_CHARS = 1_280

#: The one typed question, spelled exactly as `unstuck.cascade.projector`
#: spells it. A request asking anything else is not this contract.
Question = Literal[
    "Which available diagnostic read would best distinguish the remaining "
    "explanations for this empty result, considering both the current scenario "
    "and the attached verified Decision Receipts?"
]
QUESTION: Question = (
    "Which available diagnostic read would best distinguish the remaining "
    "explanations for this empty result, considering both the current scenario "
    "and the attached verified Decision Receipts?"
)


class ServeReason(StrEnum):
    """Every refusal this server can answer with. No message, ever."""

    ARGUMENT_REJECTED = "serve_argument_rejected"
    REVISION_MISMATCH = "serve_revision_mismatch"
    MODEL_UNAVAILABLE = "serve_model_unavailable"
    INFERENCE_FAILED = "serve_inference_failed"
    ROUTE_UNKNOWN = "route_unknown"


class Closed(BaseModel):
    """Strict, frozen, closed. The same configuration the controller uses."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["1"] = "1"


class ServeOption(Closed):
    """One rendered option: its identifier, its criterion, and whether it reads."""

    option_id: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    ]
    criterion: Annotated[str, Field(min_length=1, max_length=CRITERION_MAX_CHARS)]
    executable: bool


class ServeRequest(Closed):
    """Everything a caller is allowed to say. Nothing else is representable."""

    question: Question = QUESTION
    state_text: Annotated[str, Field(min_length=1, max_length=STATE_TEXT_MAX_CHARS)]
    options: Annotated[tuple[ServeOption, ...], Field(min_length=2, max_length=4)]
    model_revision: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    ]
    model_call_id: UUID

    @property
    def option_ids(self) -> tuple[str, ...]:
        return tuple(option.option_id for option in self.options)

    @property
    def criteria(self) -> dict[str, str]:
        """Option identifier to criterion, in exact offered order."""
        return {option.option_id: option.criterion for option in self.options}


class ServeResponse(Closed):
    """One answer. The values are the library's, unrepaired and unrounded."""

    model_revision: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    ]
    model_call_id: UUID
    choice: Annotated[str, Field(min_length=1, max_length=128)]
    probabilities: Annotated[
        dict[str, Annotated[float, Field(allow_inf_nan=False)]],
        Field(min_length=1, max_length=8),
    ]
    confidence: Annotated[float, Field(allow_inf_nan=False)]
    latency_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ServeRefusal(Closed):
    """A refusal carries its reason code and nothing a caller could read into."""

    reason_code: ServeReason


__all__ = [
    "CRITERION_MAX_CHARS",
    "INVOCATIONS_PATH",
    "MAX_REQUEST_BYTES",
    "PING_PATH",
    "QUESTION",
    "QUESTION_KEY",
    "REVISION_FILE",
    "SERVE_CONTENT_TYPE",
    "STATE_TEXT_MAX_CHARS",
    "Closed",
    "Question",
    "ServeOption",
    "ServeReason",
    "ServeRefusal",
    "ServeRequest",
    "ServeResponse",
]
