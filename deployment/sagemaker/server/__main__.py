"""The container entrypoint. SageMaker runs the image as `docker run <image> serve`.

The `serve` argument is required rather than ignored. SageMaker overrides a
container's default `CMD` by appending `serve` after the image name, and the
same image shape is also run with `train` by the training path, so a server
that started regardless of the argument would start for a request it cannot
serve. This entrypoint refuses anything but `serve`.

The exec form is what the Dockerfile uses, so the interpreter is PID 1 and
receives `SIGTERM` directly. SageMaker sends `SIGTERM` and then `SIGKILL`
thirty seconds later when it stops a container, so a process behind a shell
would be killed rather than asked to stop.

Nothing is served until every deployment input validates and the checkpoint
loads. A container that is missing an input, pointed at a directory that is not
there, pointed at an artifact declaring another revision, or still permitted to
reach the network writes its reason code to stderr and exits non-zero without
ever having listened.
"""

import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from deployment.sagemaker.server.contract import ServeReason
from deployment.sagemaker.server.engine import (
    CONTAINER_BIND_HOST,
    CONTAINER_BIND_PORT,
    DEFAULT_ANSWER_SECONDS,
    DEVICE_VARIABLE,
    MAX_ANSWER_SECONDS,
    MODEL_DIR_VARIABLE,
    OFFLINE_VARIABLES,
    REVISION_VARIABLE,
    TIMEOUT_VARIABLE,
    ServingFault,
)

#: The one argument SageMaker passes for inference.
SERVE_ARGUMENT = "serve"

#: What an operator must supply before this container can serve a request.
SERVE_CHECKLIST: tuple[str, ...] = (
    "Package the pinned checkpoint as the SageMaker model artifact, with a "
    "MODEL_REVISION file naming the immutable revision it holds. SageMaker "
    "extracts it read-only into the container.",
    f"Inject {MODEL_DIR_VARIABLE}, {REVISION_VARIABLE} and {DEVICE_VARIABLE} "
    f"as container environment values. The entrypoint refuses while any is "
    f"absent, and the device is cuda or cpu and nothing else.",
    f"Keep {' and '.join(OFFLINE_VARIABLES)} set to 1 so no checkpoint can be "
    f"fetched at serving time. The image sets both; an overridden environment "
    f"that clears one refuses to start.",
    f"Optionally inject {TIMEOUT_VARIABLE} to widen the wall-clock cap on one "
    f"answer for the instance this deployment serves on. Absent applies "
    f"{DEFAULT_ANSWER_SECONDS:.0f} seconds, the ceiling is "
    f"{MAX_ANSWER_SECONDS:.0f} seconds, and the engine applies exactly the "
    f"value read here. A plain positive decimal and nothing else; anything "
    f"outside that refuses before a listener exists.",
    "Publish no host port. The container boundary is the only reachable scope, "
    "and the endpoint in front of it is private and SigV4 authorized.",
)


def _run(
    **kwargs: Any,
) -> None:  # pragma: no cover - the listener is never started here
    """Start Uvicorn over the validated application factory."""
    import uvicorn

    uvicorn.run(**kwargs)


def main(
    argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None
) -> int:
    """Validate, then serve. Every refusal is a reason code on stderr.

    Exit 2 means the argument was not `serve`, so this process was asked to do
    something it does not implement. Exit 1 means it was asked to serve and
    could not: the reason code names which input is absent or disagrees.
    """
    arguments = list(sys.argv if argv is None else argv)
    supplied = os.environ if environ is None else environ
    if len(arguments) < 2 or arguments[1] != SERVE_ARGUMENT:
        sys.stderr.write(
            json.dumps(
                {
                    "serving": "refused",
                    "reason_code": ServeReason.ARGUMENT_REJECTED.value,
                    "expected_argument": SERVE_ARGUMENT,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    from deployment.sagemaker.server.app import app_from_environment

    try:
        app = app_from_environment(supplied)
    except ServingFault as fault:
        sys.stderr.write(
            json.dumps(
                {
                    "serving": "refused",
                    "reason_code": fault.reason.value,
                    "checklist": list(SERVE_CHECKLIST),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return 1
    _run(
        app=app,
        host=CONTAINER_BIND_HOST,
        port=CONTAINER_BIND_PORT,
        workers=1,
        server_header=False,
        date_header=False,
        timeout_keep_alive=5,
        log_level="info",
    )
    return 0


__all__ = ["SERVE_ARGUMENT", "SERVE_CHECKLIST", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
