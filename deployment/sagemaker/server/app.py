"""The SageMaker model server. FastAPI on Uvicorn, x86_64, port 8080.

This implements the SageMaker container contract for custom inference code and
nothing beyond it:

* `POST /invocations` and `GET /ping` are the two required paths, served by a
  web server listening on port 8080.
* `/ping` reports whether the checkpoint is actually loaded, rather than
  returning a constant 200, because `/ping` is the signal SageMaker uses to
  decide whether an instance is healthy and to replace it when it is not. The
  request timeout on a ping attempt is 2 seconds, so the check is a loaded-state
  read and never an inference.
* `/invocations` answers within the model's own bound; the contract allows a
  model at most 60 seconds, and the bound applied is this deployment's own
  configured answer cap, which defaults to 20 seconds and may not exceed the
  controller's 120 second run ceiling. The cap arrives as a deployment input
  and is unreachable from a request.

Source: "Custom Inference Code with Hosting Services" in the Amazon SageMaker
AI Developer Guide, which fixes the paths, the port, the health-check
behaviour, the `serve` argument, and the read-only `/opt/ml/model` artifact
location.

Nothing else is served. There is no interactive documentation, no schema
endpoint, no CORS surface, no bidirectional streaming path, and no
authentication of this server's own: the endpoint in front of it is private and
SigV4 authorized before a request arrives, so a credential invented here would
be a second, unverified path rather than a protection. Every other path and
method answers `route_unknown`.

Nothing leaks. An unexpected exception becomes a reason-coded body, never a
stack trace, an exception message, or a model path on the wire, and a refusal
carries its reason code and nothing a caller could read into.
"""

from collections.abc import Mapping
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from deployment.sagemaker.server.contract import (
    INVOCATIONS_PATH,
    MAX_REQUEST_BYTES,
    PING_PATH,
    SERVE_CONTENT_TYPE,
    ServeReason,
    ServeRefusal,
    ServeRequest,
)
from deployment.sagemaker.server.engine import (
    ChoiceEngine,
    ServingFault,
    serving_settings,
)

#: The methods the catch-all answers, so a method this server does not
#: implement cannot read as "allowed but unimplemented".
UNKNOWN_ROUTE_METHODS: Final = [
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
]


def _refusal(status: int, reason: ServeReason) -> JSONResponse:
    """One refusal vocabulary. A reason code and nothing else."""
    return JSONResponse(
        status_code=status,
        content=ServeRefusal(reason_code=reason).model_dump(
            mode="json", exclude={"schema_version"}
        ),
    )


def _declared_length(request: Request) -> int | None:
    declared = request.headers.get("content-length")
    if declared is None or not declared.strip().isdigit():
        return None
    return int(declared.strip())


def create_app(*, engine: ChoiceEngine) -> FastAPI:
    """Build the server over one already-constructed engine.

    The engine is injected rather than built here, so the configuration
    refusals have already happened by the time an application exists and a
    misconfigured container never reaches this function.
    """
    app = FastAPI(
        title="Unstuck Decider model server",
        description="Private SageMaker real-time serving surface.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exception: Exception) -> Response:
        """A bug is a refusal, never a traceback or an exception message."""
        return _refusal(500, ServeReason.INFERENCE_FAILED)

    @app.get(PING_PATH)
    async def ping() -> Response:
        """Readiness only. No revision, no path, no device, and no identity."""
        if not engine.ready():
            return _refusal(503, ServeReason.MODEL_UNAVAILABLE)
        return JSONResponse(status_code=200, content={})

    @app.post(INVOCATIONS_PATH)
    async def invocations(request: Request) -> Response:
        media = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media != SERVE_CONTENT_TYPE:
            return _refusal(415, ServeReason.ARGUMENT_REJECTED)
        declared = _declared_length(request)
        if declared is not None and declared > MAX_REQUEST_BYTES:
            return _refusal(413, ServeReason.ARGUMENT_REJECTED)
        raw = await request.body()
        if len(raw) > MAX_REQUEST_BYTES:
            return _refusal(413, ServeReason.ARGUMENT_REJECTED)
        try:
            parsed = ServeRequest.model_validate_json(raw)
        except ValidationError:
            return _refusal(400, ServeReason.ARGUMENT_REJECTED)
        try:
            answer = engine.answer(parsed)
        except ServingFault as fault:
            status = 400 if fault.reason is ServeReason.REVISION_MISMATCH else 500
            if fault.reason is ServeReason.MODEL_UNAVAILABLE:
                status = 503
            return _refusal(status, fault.reason)
        return JSONResponse(
            status_code=200,
            content=answer.model_dump(mode="json", exclude={"schema_version"}),
        )

    @app.api_route("/{unmatched:path}", methods=UNKNOWN_ROUTE_METHODS)
    async def unknown(unmatched: str) -> Response:
        """Every other path and method, including the absent docs surface."""
        return _refusal(404, ServeReason.ROUTE_UNKNOWN)

    return app


def app_from_environment(environ: Mapping[str, str] | None = None) -> FastAPI:
    """The application factory the container command names.

    Construction is where this fails closed. The deployment inputs are
    validated and the checkpoint is loaded before the application exists, so a
    container without them, or pointed at an artifact declaring another
    revision, raises before Uvicorn binds anything and the process exits
    non-zero without ever having listened.
    """
    engine = ChoiceEngine(serving_settings(environ))
    engine.load()
    return create_app(engine=engine)


__all__ = ["UNKNOWN_ROUTE_METHODS", "app_from_environment", "create_app"]
