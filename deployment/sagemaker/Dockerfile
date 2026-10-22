# syntax=docker/dockerfile:1.7
#
# Offline packaging input for the x86_64 SageMaker Decider model-server image.
#
# Nothing in this unit builds, tags, pushes, or runs this image, and no
# endpoint exists. Every claim about the resulting image or endpoint stays
# Pending its named gate.
#
# This is a single-platform image, deliberately. SageMaker real-time inference
# instance types are x86_64, so the image is linux/amd64 and there is no
# cross-build to arrange: the build host and the target agree, every RUN
# executes natively, and no emulation is involved. That is the opposite of the
# ARM64 AgentCore image beside it, which cross-builds because AgentCore
# requires arm64; the two are separate images with separate platforms on
# purpose, and neither one's constraints apply to the other.
#
# The base image is pinned by digest and is never a mutable tag. The digest is
# an operator-supplied build argument recorded in
# deployment/sagemaker/base-image.lock, which is Pending: no digest is invented
# here, so a build without it fails rather than silently resolving a floating
# tag.
#
#   docker buildx build --platform linux/amd64 \
#     --build-arg BASE_IMAGE_REPOSITORY="$(sed -n \
#       's/^BASE_IMAGE_REPOSITORY=//p' deployment/sagemaker/base-image.lock)" \
#     --build-arg BASE_IMAGE_DIGEST="$(sed -n \
#       's/^BASE_IMAGE_DIGEST=//p' deployment/sagemaker/base-image.lock)" \
#     -f deployment/sagemaker/Dockerfile -t unstuck-decider-server:offline .
#
# Add the torch wheel index the operator resolved for the chosen device:
#
#     --build-arg TORCH_INDEX_URL="<the resolved wheel index>"
#
# The argument is required for BOTH devices on linux x86_64, and the reason is
# a reproduced build failure rather than a preference. The default index's
# `torch` wheel for linux x86_64 is the CUDA build: it links libcudart and
# libcublasLt and expects the `nvidia-*` runtime distributions as dependencies.
# This build installs with --no-deps, so those are absent, and `import torch`
# in the resulting image fails with
# `OSError: libcudart.so.13: cannot open shared object file`. A device=cpu
# deployment therefore supplies the CPU wheel index, where the same pin
# resolves to `2.14.1+cpu`; a local version sorts above the plain release under
# PEP 440, so --extra-index-url selects it deterministically rather than
# leaving the choice to index order. The resolved value is recorded in
# deployment/sagemaker/base-image.lock.
#
# The container binds 0.0.0.0:8080 inside its own network boundary only, which
# is what the SageMaker container contract requires: "the container must have a
# web server listening on port 8080 and must accept POST requests to the
# /invocations and /ping endpoints". No host port is published, by this file or
# by any command above, so that address is reachable only from inside the
# container's own network. SageMaker reaches it over its own managed network,
# and the endpoint in front of it is private and SigV4 authorized; the callers
# that may invoke it are bounded by the IAM grant on the endpoint resource.
# See deployment/sagemaker/server/engine.py, which states the same bound beside
# the constant.
#
# No model weight is in this image. SageMaker extracts the model artifact into
# /opt/ml/model read-only before the container starts, so the checkpoint lives
# in the artifact and the image carries code only. Nothing is fetched at
# serving time either: HF_HUB_OFFLINE and TRANSFORMERS_OFFLINE are set below
# and the server refuses to start if either has been cleared.
#
# The image carries no deployment value. The model directory, the pinned
# revision and the device all arrive at deploy time as container environment
# values; a baked default would be a guess about somebody else's deployment.
# The names are MODEL_DIR_VARIABLE, REVISION_VARIABLE and DEVICE_VARIABLE in
# deployment/sagemaker/server/engine.py, and the entrypoint refuses and names
# the reason while any is absent or disagrees.

ARG BASE_IMAGE_REPOSITORY
ARG BASE_IMAGE_DIGEST

FROM --platform=linux/amd64 ${BASE_IMAGE_REPOSITORY}@${BASE_IMAGE_DIGEST} AS runtime

# The torch wheel index. No default: a cuda deployment supplies the index the
# operator resolved, and a cpu deployment omits it.
ARG TORCH_INDEX_URL=

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# No checkpoint is ever fetched at serving time. These are set here and
# re-checked by the server at startup, because an image can be run with an
# overridden environment and a cache miss must never become a network fetch.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1
# The model artifact is read-only, so the libraries are told to keep their
# caches out of it. Nothing is written into /opt/ml/model.
ENV HF_HOME=/opt/unstuck/cache
ENV PYTHONPATH=/opt/unstuck

WORKDIR /opt/unstuck

COPY deployment/sagemaker/requirements-serving.txt /tmp/requirements-serving.txt

# Pinned transitive closure. --no-deps makes the lockfile the only authority,
# and --only-binary=:all: means a distribution with no linux x86_64 wheel fails
# the build loudly instead of falling back to a source build that would compile
# an unpinned toolchain into the image.
#
# The index arguments are passed only when the operator supplied one, so a cpu
# build uses the default index and a cuda build uses the resolved one.
RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: \
      ${TORCH_INDEX_URL:+--extra-index-url "${TORCH_INDEX_URL}"} \
      --requirement /tmp/requirements-serving.txt \
 && rm -f /tmp/requirements-serving.txt \
 && mkdir -p /opt/unstuck/cache

# The server package only. No controller source, no test, no fixture answer,
# no evaluator asset, no staged checkpoint, no experiment output, no workflow
# record, and no repository history. The model host cannot name run state,
# receipts, artifacts, or evaluator truth, because the code that could is not
# in the image.
COPY deployment/__init__.py ./deployment/__init__.py
COPY deployment/sagemaker/__init__.py ./deployment/sagemaker/__init__.py
COPY deployment/sagemaker/server ./deployment/sagemaker/server

# SageMaker expects inference containers to run as root: "SageMaker AI expects
# all containers to run with root users. Create your container so that it uses
# only root users." That is the documented contract for this runtime, so it is
# honoured here rather than overridden, and it is the one way this image
# differs from the AgentCore image beside it, which runs as an unprivileged
# numeric user. The container publishes no port and holds no credential, and
# /opt/ml/model is mounted read-only by the platform.
USER root

# Documents the private container boundary. EXPOSE publishes nothing.
EXPOSE 8080

# The exec form, so the interpreter is PID 1 and receives SIGTERM directly:
# SageMaker sends SIGTERM and then SIGKILL thirty seconds later when it stops a
# container, and a process behind a shell would be killed rather than asked to
# stop. SageMaker runs the image as `docker run <image> serve`, appending
# `serve` to this entrypoint; the entrypoint requires that argument and refuses
# anything else. It also validates every deployment input and loads the
# checkpoint before Uvicorn opens a socket, so a misconfigured container exits
# non-zero without ever having listened.
ENTRYPOINT ["python", "-m", "deployment.sagemaker.server"]
