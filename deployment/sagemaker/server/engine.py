"""Serving settings and the single-process checkpoint engine.

Three properties are structural rather than configured.

The checkpoint comes from the model artifact and from nowhere else. SageMaker
copies `ModelDataUrl` into `/opt/ml/model` and gives the container read-only
access to it, so that directory is the only source: the path arrives as a
deployment input whose default is exactly that directory, it is never reachable
from a request, and no module in this package spells a registry URL, a
`snapshot_download`, or an `hf_hub_download`. The two documented offline flags
must already be set in the process environment or the server refuses to start,
so a cache miss at serving time cannot silently become a network fetch.

The revision is checked, not labelled. A snapshot directory does not record its
own revision, so the packaging step writes it into `MODEL_REVISION` inside the
artifact and the server compares that against the injected deployment input.
A mismatch, an absent file and an absent directory all refuse before a listener
exists, which is what makes "this endpoint serves exactly this revision" a
checkable claim.

The answer cap is a deployment input, and the only one with a default. How long
one answer may take is a property of the instance the checkpoint is served on
and of the size of the state it is asked about, not of the product, so the same
image in front of a slower instance needs a wider cap. Absent keeps the shipped
20 seconds, the ceiling is this module's own `MAX_ANSWER_SECONDS` rather than
anything a deployment can name, and no request can reach it.

One process holds one checkpoint. The engine loads on first use and keeps it,
so a second request reuses the loaded model rather than loading a second copy,
and the heavy dependency is imported inside the loader alone: this module
imports in an environment that carries no model runtime at all, which is what
lets the contract be tested without one.

Nothing here opens a socket, reads a credential, or reaches an AWS API. The
server is reachable only through the SageMaker endpoint, which authorizes its
callers with SigV4 before a request arrives.
"""

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import ValidationError

from deployment.sagemaker.server.contract import (
    QUESTION_KEY,
    REVISION_FILE,
    ServeReason,
    ServeRequest,
    ServeResponse,
)

#: The bind the SageMaker container contract requires: "the container must have
#: a web server listening on port 8080 and must accept POST requests to the
#: /invocations and /ping endpoints".
#:
#: `0.0.0.0` here is the container's own boundary and nothing wider. No host
#: port is published by this image, by its Dockerfile, or by any command in
#: this repository: SageMaker reaches the container over its own managed
#: network, so this address is unreachable from outside that boundary. The
#: endpoint in front of it is private and SigV4 authorized, and the callers
#: that may invoke it are bounded by the IAM grant on the endpoint resource.
CONTAINER_BIND_HOST: Final[Literal["0.0.0.0"]] = "0.0.0.0"
CONTAINER_BIND_PORT: Final[Literal[8080]] = 8080

#: The directory SageMaker copies the model artifact into, read-only. It is the
#: default and the expected value; a deployment may point the server at another
#: directory for a local contract check, but no request can.
DEFAULT_MODEL_DIR: Final = "/opt/ml/model"

#: The only two devices this server will serve on. `mps` is deliberately absent:
#: it is a local development accelerator, not a SageMaker instance type, so
#: accepting it here would describe a host that does not exist.
SERVING_DEVICES: Final[tuple[str, ...]] = ("cuda", "cpu")

#: The deployment inputs, each one required and none defaulted to a guess.
MODEL_DIR_VARIABLE: Final = "UNSTUCK_SERVING_MODEL_DIR"
REVISION_VARIABLE: Final = "UNSTUCK_SERVING_MODEL_REVISION"
DEVICE_VARIABLE: Final = "UNSTUCK_SERVING_DEVICE"

#: The one optional deployment input: how long this deployment allows one answer
#: to take. It is a property of the instance the checkpoint is served on, not of
#: the product. The same image in front of a slower instance, or asked a larger
#: state, needs a wider cap, and baking that number into the image makes the
#: instance type it was measured against part of the artifact.
TIMEOUT_VARIABLE: Final = "UNSTUCK_SERVING_TIMEOUT_SECONDS"

#: The flags that must already be set for a serving-time fetch to be impossible.
#: They are set in the image and re-checked here, because an image can be run
#: with an overridden environment.
OFFLINE_VARIABLES: Final[tuple[str, ...]] = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")

#: The bounded wall-clock ceiling one answer gets when a deployment names none.
#: Unchanged: an image run with no budget injected applies exactly the 20 seconds
#: it always applied, and every offline contract check keeps the fast cap.
DEFAULT_ANSWER_SECONDS: Final = 20.0

#: The widest cap a deployment may name. 120 seconds, because that is the
#: controller's own total run ceiling (`MIN_INVOCATION_SECONDS`), and no single
#: answer may outlive the run that is waiting for it. A deployment that names
#: more refuses rather than clamping to something serviceable.
#:
#: The SageMaker container contract separately gives a model at most 60 seconds
#: to respond to `/invocations`, so in a SageMaker deployment that platform
#: limit binds first and a configured cap above it can never be reached. The
#: ceiling here is the product invariant; it is deliberately not a restatement
#: of a per-platform limit, because this server is also run directly for the
#: offline container contract check, where no such limit applies.
MAX_ANSWER_SECONDS: Final = 120.0

#: What a cap may look like. A plain positive decimal and nothing else: `nan`,
#: `inf`, `1e400`, `0x3c`, `6_0`, `60s` and `60,0` are all accepted by `float()`
#: or silently mean something other than they appear to, and a deployment input
#: is read once at startup where there is nobody to ask, so the accepted surface
#: is the literal an operator writes rather than everything the parser
#: tolerates. The same pattern governs the controller's own
#: `UNSTUCK_DECIDER_TIMEOUT_SECONDS`, so the two budgets are written alike.
_TIMEOUT_PATTERN: Final = re.compile(r"^\d+(?:\.\d+)?$")


class ServingFault(Exception):
    """A reason-coded serving refusal. Never a bare exception."""

    def __init__(self, reason: ServeReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class ServingSettings:
    """The validated deployment inputs. No credential is representable."""

    model_dir: Path
    model_revision: str
    device: str
    answer_seconds: float = DEFAULT_ANSWER_SECONDS


def answer_seconds(environ: Mapping[str, str]) -> float:
    """The configured wall-clock cap for one answer. Invalid means refused.

    An empty or whitespace value counts as unsupplied, for the same reason the
    other inputs treat it so: once it arrives, a variable set to the empty
    string is indistinguishable from one nobody supplied. Everything else is a
    plain positive decimal inside `MAX_ANSWER_SECONDS` or it raises, so a cap
    outside the bound refuses rather than quietly becoming a serviceable one.

    Zero refuses too. A zero cap could never admit an answer, so a container
    carrying one would start, pass `/ping`, and then discard every real answer
    the checkpoint produced; failing at construction says so once instead.
    """
    declared = environ.get(TIMEOUT_VARIABLE, "").strip()
    if not declared:
        return DEFAULT_ANSWER_SECONDS
    if not _TIMEOUT_PATTERN.fullmatch(declared):
        raise ServingFault(ServeReason.MODEL_UNAVAILABLE)
    configured = float(declared)
    if configured <= 0.0 or configured > MAX_ANSWER_SECONDS:
        raise ServingFault(ServeReason.MODEL_UNAVAILABLE)
    return configured


def serving_settings(environ: Mapping[str, str] | None = None) -> ServingSettings:
    """Read and validate the deployment inputs. Absent means refused.

    Every refusal happens here, before an application exists, so a container
    that is missing an input, pointed at a directory that is not there, pointed
    at an artifact declaring another revision, or still permitted to reach the
    network exits rather than serving.
    """
    supplied = os.environ if environ is None else environ
    for name in OFFLINE_VARIABLES:
        if supplied.get(name, "").strip() not in {"1", "true", "True"}:
            raise ServingFault(ServeReason.MODEL_UNAVAILABLE)
    device = supplied.get(DEVICE_VARIABLE, "").strip()
    if device not in SERVING_DEVICES:
        raise ServingFault(ServeReason.MODEL_UNAVAILABLE)
    declared = supplied.get(REVISION_VARIABLE, "").strip()
    if not declared:
        raise ServingFault(ServeReason.REVISION_MISMATCH)
    model_dir = Path(supplied.get(MODEL_DIR_VARIABLE, "").strip() or DEFAULT_MODEL_DIR)
    if not model_dir.is_dir():
        raise ServingFault(ServeReason.MODEL_UNAVAILABLE)
    marker = model_dir / REVISION_FILE
    if not marker.is_file():
        raise ServingFault(ServeReason.REVISION_MISMATCH)
    if marker.read_text(encoding="utf-8").strip() != declared:
        raise ServingFault(ServeReason.REVISION_MISMATCH)
    return ServingSettings(
        model_dir=model_dir,
        model_revision=declared,
        device=device,
        answer_seconds=answer_seconds(supplied),
    )


#: The checkpoint loader. It is injected so the contract can be exercised
#: without a model runtime, and the real one imports its dependency lazily.
Loader = Callable[..., Any]


def default_loader(path: str, *, device: str) -> Any:
    """Load the pinned checkpoint from the artifact directory. Imports lazily.

    This is the same loader `evaluation.decider_provider` uses, so the hosted
    path and the local path run the same library against the same checkpoint
    and the shipped criteria reach the model through identical code.
    """
    from strands_decider.infer import load_engine

    return load_engine(path, device=device)


class ChoiceEngine:
    """One loaded checkpoint, answering one Choice question per request.

    It answers; it decides nothing else. It writes no receipt, labels no
    outcome, reads no run state, and selects nothing beyond returning the
    library's own choice, distribution and confidence unchanged. An answer the
    strict contract cannot carry is a refusal rather than a repaired value.
    """

    def __init__(self, settings: ServingSettings, *, loader: Loader | None = None):
        self.settings = settings
        self._loader: Loader = loader or default_loader
        self._engine: Any | None = None

    def load(self) -> Any:
        """Load once per process. A second call returns the loaded engine."""
        if self._engine is None:
            try:
                self._engine = self._loader(
                    str(self.settings.model_dir), device=self.settings.device
                )
            except Exception as error:  # noqa: BLE001 - startup failure is a refusal
                raise ServingFault(ServeReason.MODEL_UNAVAILABLE) from error
        return self._engine

    def ready(self) -> bool:
        """Whether the checkpoint is loaded and could answer. Never a static yes.

        The SageMaker contract uses `/ping` to decide whether an instance is
        healthy, and recommends verifying that the artifact is loaded rather
        than returning a constant 200, so that is what this reports.
        """
        try:
            return self.load() is not None
        except ServingFault:
            return False

    def answer(self, request: ServeRequest) -> ServeResponse:
        """Answer one request. Refuses rather than returning an off-contract body."""
        if request.model_revision != self.settings.model_revision:
            raise ServingFault(ServeReason.REVISION_MISMATCH)
        engine = self.load()
        question = self._question(request)
        started = time.perf_counter()
        try:
            reply = engine.ask(request.state_text, {QUESTION_KEY: question})
        except Exception as error:  # noqa: BLE001 - the failure is a refusal
            raise ServingFault(ServeReason.INFERENCE_FAILED) from error
        latency_ms = (time.perf_counter() - started) * 1000.0
        if latency_ms > self.settings.answer_seconds * 1000.0:
            raise ServingFault(ServeReason.INFERENCE_FAILED)
        answered = getattr(reply, "answers", {}) or {}
        found = answered.get(QUESTION_KEY) if isinstance(answered, Mapping) else None
        choice = getattr(found, "choice", None)
        reported = getattr(found, "probabilities", None)
        confidence = getattr(found, "confidence", None)
        if (
            not isinstance(choice, str)
            or not isinstance(reported, Mapping)
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
        ):
            raise ServingFault(ServeReason.INFERENCE_FAILED)
        try:
            return ServeResponse(
                model_revision=self.settings.model_revision,
                model_call_id=request.model_call_id,
                choice=choice,
                probabilities={
                    str(option): float(value) for option, value in reported.items()
                },
                confidence=float(confidence),
                latency_ms=latency_ms,
            )
        except (ValidationError, TypeError, ValueError) as error:
            # An off-contract value the shape itself cannot carry. Nothing is
            # rounded, clamped, renormalized, or substituted.
            raise ServingFault(ServeReason.INFERENCE_FAILED) from error

    def _question(self, request: ServeRequest) -> Any:
        """Build the library's own question object from the offered options.

        The instructions and the per-option criteria are the controller's, in
        the order the controller offered them, so this server neither reframes
        the question nor renames an option.
        """
        try:
            from strands_decider.schema import ChoiceQuestion
        except ImportError:  # pragma: no cover - the stub loader supplies its own
            return _LocalQuestion(
                instructions=request.question, criteria=dict(request.criteria)
            )
        return ChoiceQuestion(
            instructions=request.question, criteria=dict(request.criteria)
        )


@dataclass(frozen=True)
class _LocalQuestion:
    """The question shape, for an environment with no model runtime installed."""

    instructions: str
    criteria: dict[str, str]


__all__ = [
    "CONTAINER_BIND_HOST",
    "CONTAINER_BIND_PORT",
    "DEFAULT_ANSWER_SECONDS",
    "DEFAULT_MODEL_DIR",
    "DEVICE_VARIABLE",
    "MAX_ANSWER_SECONDS",
    "MODEL_DIR_VARIABLE",
    "OFFLINE_VARIABLES",
    "REVISION_VARIABLE",
    "SERVING_DEVICES",
    "TIMEOUT_VARIABLE",
    "ChoiceEngine",
    "Loader",
    "ServingFault",
    "ServingSettings",
    "answer_seconds",
    "default_loader",
    "serving_settings",
]
