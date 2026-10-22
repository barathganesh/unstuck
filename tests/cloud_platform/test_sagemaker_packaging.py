# Focused command:
# UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/cloud_platform/test_sagemaker_packaging.py -q
"""The two container images, asserted structurally rather than built.

Neither image is built here, and that is a stated limit rather than a gap: both
Dockerfiles declare their base image digests as build arguments with no
default, so a build refuses until an operator records them, and
`deployment/base-image.lock` and `deployment/sagemaker/base-image.lock` are
both Pending. What can be checked without a build is the structure, and that is
what these assert.

The size question is treated the same way. A projected image size cannot be
computed from here: the base image and the pinned wheel closure are the bulk of
it and neither has been resolved, so an estimate would be a fabricated number.
What is checkable is that no model weight can reach either image at all, which
is the property the 2 GB AgentCore limit actually turns on, and that is
asserted below.
"""

import re
from pathlib import Path

import pytest

from deployment.sagemaker.server.contract import (
    INVOCATIONS_PATH,
    PING_PATH,
    REVISION_FILE,
)
from deployment.sagemaker.server.engine import (
    CONTAINER_BIND_PORT,
    DEFAULT_MODEL_DIR,
    DEVICE_VARIABLE,
    MODEL_DIR_VARIABLE,
    OFFLINE_VARIABLES,
    REVISION_VARIABLE,
    SERVING_DEVICES,
)

from .test_agentcore_app import dockerfile_instructions

ROOT = Path(__file__).resolve().parents[2]
AGENTCORE_DOCKERFILE = ROOT / "deployment" / "Dockerfile"
SAGEMAKER_DIR = ROOT / "deployment" / "sagemaker"
SAGEMAKER_DOCKERFILE = SAGEMAKER_DIR / "Dockerfile"
SERVING_LOCKFILE = SAGEMAKER_DIR / "requirements-serving.txt"
SERVING_IMAGE_LOCK = SAGEMAKER_DIR / "base-image.lock"

#: Any path that could carry model weights. None may appear in either image.
WEIGHT_MARKERS = (
    ".safetensors",
    ".gguf",
    ".ckpt",
    ".pt",
    ".bin",
    "hf-cache",
    "hf_cache",
    "decider-pf12",
    "snapshots/",
    "lora/",
)

#: Build inputs that would be a credential. Neither image declares one.
SENSITIVE_BUILD = re.compile(
    r"(?i)(secret|password|token|credential|api[_-]?key|access[_-]?key)"
)


def _instructions(path: Path) -> list[str]:
    return dockerfile_instructions(path.read_text(encoding="utf-8"))


def _copies(path: Path) -> list[str]:
    return [line for line in _instructions(path) if line.startswith(("COPY", "ADD"))]


# --- The serving image ------------------------------------------------------


def test_the_serving_image_is_single_platform_x86_64() -> None:
    text = SAGEMAKER_DOCKERFILE.read_text(encoding="utf-8")
    froms = [
        line for line in _instructions(SAGEMAKER_DOCKERFILE) if line.startswith("FROM")
    ]
    assert len(froms) == 1, "one stage: the build host and the target agree"
    assert "--platform=linux/amd64" in froms[0]
    assert "linux/arm64" not in text, "the ARM64 constraint is the other image's"


def test_the_serving_base_image_is_pinned_by_digest_with_no_default() -> None:
    instructions = _instructions(SAGEMAKER_DOCKERFILE)
    args = {
        line.split()[1].split("=")[0]: line
        for line in instructions
        if line.startswith("ARG ")
    }
    assert "BASE_IMAGE_REPOSITORY" in args
    assert "BASE_IMAGE_DIGEST" in args
    for name in ("BASE_IMAGE_REPOSITORY", "BASE_IMAGE_DIGEST"):
        assert args[name].strip() == f"ARG {name}", "no default base image"
    froms = [line for line in instructions if line.startswith("FROM")]
    assert "${BASE_IMAGE_REPOSITORY}@${BASE_IMAGE_DIGEST}" in froms[0]
    assert ":latest" not in froms[0]


def test_the_serving_base_image_lock_records_only_immutable_resolved_values() -> None:
    """Recorded values must be immutable; emptiness is no longer the invariant.

    This file used to be required to be empty, because before any registry read
    every value here would have been invented. Once an operator resolves the
    base digest from a real `docker buildx imagetools inspect` and keeps the
    manifest as evidence, recording it is reporting a verified fact. What must
    never appear is a mutable reference, so that is what is checked: the digest
    is a sha256, no tag is recorded, and the wheel indexes are https.
    """
    recorded = dict(
        line.strip().split("=", 1)
        for line in SERVING_IMAGE_LOCK.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    allowed = {
        "BASE_IMAGE_REPOSITORY",
        "BASE_IMAGE_DIGEST",
        "TORCH_INDEX_URL",
        "STRANDS_DECIDER_INDEX_URL",
    }
    for name, value in recorded.items():
        assert name in allowed, name
        if name.endswith("_DIGEST"):
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", value), (name, value)
        elif name.endswith("_INDEX_URL"):
            assert value.startswith("https://"), (name, value)
        else:
            assert ":" not in value, (name, value, "a tag here would be mutable")
    assert ":latest" not in SERVING_IMAGE_LOCK.read_text(encoding="utf-8")
    if "BASE_IMAGE_DIGEST" in recorded:
        assert "BASE_IMAGE_REPOSITORY" in recorded, (
            "a digest without its repository names nothing"
        )


def test_the_serving_closure_is_fully_pinned() -> None:
    lines = [
        line.strip()
        for line in SERVING_LOCKFILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines
    for line in lines:
        assert re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[A-Za-z0-9_.\-+]+", line), line
    text = SAGEMAKER_DOCKERFILE.read_text(encoding="utf-8")
    assert "--no-deps" in text
    assert "--only-binary=:all:" in text


def test_the_serving_image_pins_the_libraries_the_golden_path_actually_ran() -> None:
    pinned = dict(
        line.strip().split("==")
        for line in SERVING_LOCKFILE.read_text(encoding="utf-8").splitlines()
        if "==" in line and not line.strip().startswith("#")
    )
    # Transcribed from the resolved environment at .venv/decider-pf12/env, so
    # the hosted path runs the same loader against the same checkpoint.
    assert pinned["strands-decider"] == "0.2.0"
    assert pinned["transformers"] == "5.20.2"
    assert pinned["peft"] == "0.21.2"
    assert pinned["torch"] == "2.14.1"
    # `httpx2` used to be asserted absent here, as "the test client's
    # dependency, not the server's". A build proved that wrong: it is a
    # `Requires-Dist` of huggingface-hub 2.2.0 with no extra marker, and
    # `huggingface_hub.file_download` imports it at module load, so the image
    # failed at `import transformers` with ModuleNotFoundError. It and its own
    # transport are now required to be pinned, at the versions the resolved
    # local environment carries.
    assert pinned["httpx2"] == "2.15.1"
    assert pinned["httpcore2"] == "2.15.1"
    # The v1 test client is still not a serving dependency.
    assert "httpx" not in pinned
    assert "httpcore" not in pinned
    # Importing an HTTP client is not reaching a network: the two offline flags
    # are set in the image and re-checked before a listener exists.
    dockerfile = SAGEMAKER_DOCKERFILE.read_text(encoding="utf-8")
    assert "ENV HF_HUB_OFFLINE=1" in dockerfile
    assert "ENV TRANSFORMERS_OFFLINE=1" in dockerfile
    # The model host reaches no AWS API.
    assert "boto3" not in pinned
    assert "botocore" not in pinned


def test_the_serving_entrypoint_is_exec_form_and_takes_the_serve_argument() -> None:
    instructions = _instructions(SAGEMAKER_DOCKERFILE)
    entrypoint = next(line for line in instructions if line.startswith("ENTRYPOINT"))
    assert entrypoint.startswith('ENTRYPOINT ["'), "exec form, so the process is PID 1"
    assert "deployment.sagemaker.server" in entrypoint
    assert not any(line.startswith("CMD") for line in instructions), (
        "SageMaker overrides CMD with `serve`, so declaring one is misleading"
    )
    assert f"EXPOSE {CONTAINER_BIND_PORT}" in "\n".join(instructions)


def test_the_serving_image_runs_as_root_as_the_contract_requires() -> None:
    users = [
        line.split()[1]
        for line in _instructions(SAGEMAKER_DOCKERFILE)
        if line.startswith("USER ")
    ]
    assert users == ["root"], (
        "the SageMaker contract requires a root user; the AgentCore image is "
        "the one that runs unprivileged"
    )


def test_the_serving_image_forbids_a_fetch_at_serving_time() -> None:
    text = SAGEMAKER_DOCKERFILE.read_text(encoding="utf-8")
    for name in OFFLINE_VARIABLES:
        assert f"ENV {name}=1" in text, name
    assert "ENV HF_HOME=" in text, "caches stay out of the read-only artifact"


def test_the_serving_image_declares_no_credential_build_input() -> None:
    for line in _instructions(SAGEMAKER_DOCKERFILE):
        if line.startswith(("ARG ", "ENV ")):
            assert not SENSITIVE_BUILD.search(line), line
        assert "--mount=type=secret" not in line


def test_the_serving_image_copies_the_server_package_and_nothing_else() -> None:
    copied = _copies(SAGEMAKER_DOCKERFILE)
    assert copied
    for line in copied:
        assert not line.startswith("ADD"), "ADD can fetch a remote asset"
        for forbidden in (
            "tests",
            "fixtures/evaluator",
            "experiments",
            "aidlc",
            ".git",
            "src/unstuck",
            " . ",
            "evaluation",
        ):
            assert forbidden not in line, (forbidden, line)
    # Exactly the server package and its two package markers.
    sources = [line.split()[1] for line in copied if not line.startswith("COPY --from")]
    assert set(sources) >= {
        "deployment/sagemaker/server",
        "deployment/__init__.py",
        "deployment/sagemaker/__init__.py",
    }


# --- No model weight reaches either image -----------------------------------


@pytest.mark.parametrize(
    "path", [AGENTCORE_DOCKERFILE, SAGEMAKER_DOCKERFILE], ids=["agentcore", "sagemaker"]
)
def test_no_image_copies_a_model_weight(path: Path) -> None:
    for line in _copies(path):
        for marker in WEIGHT_MARKERS:
            assert marker not in line, (path.name, marker, line)


def test_the_agentcore_image_copies_only_sources_and_the_staged_closure() -> None:
    sources = [
        line.split()[1]
        for line in _copies(AGENTCORE_DOCKERFILE)
        if not line.startswith("COPY --from")
    ]
    # The requirements file goes into the builder stage; the application
    # sources go into the runtime stage. Nothing else is copied from the
    # repository at all.
    assert sorted(sources) == [
        "deployment/requirements-runtime.txt",
        "pyproject.toml",
        "src/unstuck",
    ]


def test_the_agentcore_sources_carry_no_weight_and_are_small() -> None:
    """The repository content the ARM64 image copies, measured.

    This is the one part of the image size that is measurable from here, and it
    is measured rather than estimated. The base image and the pinned wheel
    closure are the bulk of the image and neither has been resolved, so no
    projected total is asserted: see the module docstring.
    """
    copied = ROOT / "src" / "unstuck"
    total = 0
    for path in copied.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            total += path.stat().st_size
            for marker in WEIGHT_MARKERS:
                assert marker not in path.name, path
    total += (ROOT / "pyproject.toml").stat().st_size
    # Well under a megabyte of application source. The AgentCore limit is 2 GB
    # and the model cache for this checkpoint is about 4.7 GB, which is why the
    # weights are hosted separately rather than packaged here at all.
    assert total < 4 * 1024 * 1024, total


def test_the_agentcore_image_installs_no_model_runtime() -> None:
    pinned = {
        line.strip().split("==")[0]
        for line in (ROOT / "deployment" / "requirements-runtime.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if "==" in line and not line.strip().startswith("#")
    }
    for forbidden in (
        "torch",
        "transformers",
        "peft",
        "accelerate",
        "strands-decider",
        "safetensors",
        "tokenizers",
    ):
        assert forbidden not in pinned, forbidden


# --- The two images stay distinct -------------------------------------------


def _pinned(path: Path) -> set[str]:
    """The pinned distribution names, ignoring the prose around them."""
    return {
        line.strip().split("==")[0]
        for line in path.read_text(encoding="utf-8").splitlines()
        if "==" in line and not line.strip().startswith("#")
    }


def test_the_two_images_share_no_closure_and_no_platform() -> None:
    agentcore = _pinned(ROOT / "deployment" / "requirements-runtime.txt")
    serving = _pinned(SERVING_LOCKFILE)
    assert "--platform=linux/arm64" in AGENTCORE_DOCKERFILE.read_text(encoding="utf-8")
    assert "--platform=linux/amd64" in SAGEMAKER_DOCKERFILE.read_text(encoding="utf-8")
    # The controller reaches AWS; the model host does not.
    assert "boto3" in agentcore and "boto3" not in serving
    # The model host loads a checkpoint; the controller does not.
    assert "torch" in serving and "torch" not in agentcore


def test_the_serving_contract_constants_match_the_dockerfile_and_the_readme() -> None:
    readme = (SAGEMAKER_DIR / "README.md").read_text(encoding="utf-8")
    for token in (
        PING_PATH,
        INVOCATIONS_PATH,
        REVISION_FILE,
        DEFAULT_MODEL_DIR,
        MODEL_DIR_VARIABLE,
        REVISION_VARIABLE,
        DEVICE_VARIABLE,
        *OFFLINE_VARIABLES,
        *SERVING_DEVICES,
    ):
        assert token in readme, token
    assert str(CONTAINER_BIND_PORT) in readme


def test_no_image_publishes_a_host_port() -> None:
    for path in (AGENTCORE_DOCKERFILE, SAGEMAKER_DOCKERFILE):
        text = path.read_text(encoding="utf-8")
        assert "--publish" not in text
        assert f"-p {CONTAINER_BIND_PORT}:" not in text
