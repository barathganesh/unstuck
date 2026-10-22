# syntax=docker/dockerfile:1.7
#
# Offline packaging input for the ARM64 Unstuck controller container.
#
# Nothing in this unit builds, tags, pushes, or runs this image. Every claim
# about the resulting image stays Pending its named gate.
#
# This is a cross-build, and the reason is a reproduced failure rather than a
# preference. The build host is x86_64 and no binfmt emulation is installed, so
# `docker buildx build --platform linux/arm64` on a single arm64 stage aborts at
# the first `RUN` with `exec /bin/sh: exec format error`: the host cannot
# execute an aarch64 `/bin/sh`. Installing an emulator would mean running a
# privileged binfmt container, which this project does not do. So every
# instruction that executes runs on the native build platform, and the
# `linux/arm64` stage contains no `RUN` at all: it only copies bytes and
# declares metadata. Nothing arm64 is ever executed at build time, which is why
# no emulation is needed.
#
# Both base images are pinned by digest, never by a mutable tag, and the two
# digests are separate build inputs because they name different things:
#
#   BUILDER_IMAGE_DIGEST  the multi-arch index digest, so `$BUILDPLATFORM`
#                         selects the build host's own native manifest from it
#   RUNTIME_IMAGE_DIGEST  the single `linux/arm64` manifest digest, so the
#                         shipped image cannot drift to another architecture
#
# Both are operator-supplied values recorded in deployment/base-image.lock,
# which is Pending: no digest is invented here, so a build without them fails
# rather than silently resolving a floating tag.
#
#   docker buildx build --platform linux/arm64 \
#     --build-arg BUILDER_IMAGE_DIGEST="$(sed -n \
#       's/^BUILDER_IMAGE_DIGEST=//p' deployment/base-image.lock)" \
#     --build-arg RUNTIME_IMAGE_DIGEST="$(sed -n \
#       's/^RUNTIME_IMAGE_DIGEST=//p' deployment/base-image.lock)" \
#     -f deployment/Dockerfile -t unstuck-controller:offline .
#
# The container binds 0.0.0.0:8080 inside its own network boundary only, which
# is what the AgentCore HTTP protocol contract requires. No host port is
# published, by this file or by any command above, so that address is reachable
# only from inside the container's own network. The application factory
# validates the injected deployment inputs before Uvicorn binds anything, so a
# container without them exits non-zero without ever having listened; see
# src/unstuck/cloud/agentcore_app.py and src/unstuck/cloud/runtime.py.
#
# The image carries no deployment value. The AgentCore Runtime resource injects
# every one at deploy time as an environment variable, which is why none is
# baked in as an ENV here: a baked default would be a guess about somebody
# else's account and would work exactly once. The names are
# RUNTIME_IDENTITY_VARIABLES in src/unstuck/cloud/runtime.py together with the
# documented OpenTelemetry variables in src/unstuck/telemetry/runtime.py, and
# unstuck.cloud.deployment.agentcore_environment builds the map the resource
# applies. The entrypoint refuses and names every variable that is absent.
#
# This image publishes no registry reference of its own. The AgentCore runtime
# resource takes a digest-pinned URI in the deployment account's own private
# registry; see deployment/agentcore/README.md.

ARG BUILDER_IMAGE_REPOSITORY=public.ecr.aws/docker/library/python
ARG BUILDER_IMAGE_DIGEST
ARG RUNTIME_IMAGE_REPOSITORY=public.ecr.aws/docker/library/python
ARG RUNTIME_IMAGE_DIGEST

# --- Native staging stage ---------------------------------------------------
#
# Runs on the build host's own architecture, whatever that is. It assembles the
# arm64 runtime tree without ever executing an arm64 byte.

FROM --platform=$BUILDPLATFORM ${BUILDER_IMAGE_REPOSITORY}@${BUILDER_IMAGE_DIGEST} AS builder

# The target interpreter, which is not this stage's interpreter. These select
# wheel tags; they do not select the Python that runs here.
ARG TARGET_PYTHON_VERSION=3.12
ARG TARGET_ABI=cp312

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY deployment/requirements-runtime.txt /tmp/requirements-runtime.txt

# Pinned transitive closure, resolved for the target platform by wheel tag.
#
# --no-deps makes the lockfile the only authority. --only-binary=:all: means a
# package with no aarch64 wheel fails the build loudly instead of falling back
# to a source build this stage could not perform for another architecture: no
# wheel compatibility is guessed, and nothing from the closure is imported,
# because pip matches tags and unpacks archives rather than running them. The
# platform tags are manylinux only, listed most portable first, so a wheel
# built against a newer glibc than the runtime base cannot be selected in
# preference to one that is portable. --no-compile keeps this stage's
# interpreter from writing byte-code into the target tree.
RUN python -m pip install \
      --no-cache-dir --no-compile --no-deps \
      --only-binary=:all: \
      --implementation cp \
      --python-version "${TARGET_PYTHON_VERSION}" \
      --abi "${TARGET_ABI}" \
      --platform manylinux_2_17_aarch64 \
      --platform manylinux2014_aarch64 \
      --platform manylinux_2_28_aarch64 \
      --target /staging/vendor \
      --requirement /tmp/requirements-runtime.txt

# Run state lives inside the image, owned by the unprivileged user, and is
# never a published volume. It is therefore per-container and ephemeral; the
# durable DynamoDB run store is a separate step behind its own gates. The path
# is CONTAINER_RUN_ROOT in src/unstuck/cloud/runtime.py. The directory is
# created here and carried across already owned, because the arm64 stage runs
# nothing and so can neither mkdir nor chown.
RUN mkdir -p /staging/var/runs

# --- ARM64 runtime stage ----------------------------------------------------
#
# No RUN instruction. Adding one would reintroduce the `exec format error`.

FROM --platform=linux/arm64 ${RUNTIME_IMAGE_REPOSITORY}@${RUNTIME_IMAGE_DIGEST} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
# The application sources and the staged closure, in that order, and nothing
# else. The closure is not installed into the base interpreter's own
# site-packages, so the base image's package set is left exactly as the pinned
# digest shipped it and the two sets stay distinguishable in the filesystem.
ENV PYTHONPATH=/opt/unstuck/src:/opt/unstuck/vendor

WORKDIR /opt/unstuck

COPY --from=builder /staging/vendor /opt/unstuck/vendor
COPY --from=builder --chown=10001:10001 /staging/var /opt/unstuck/var

# Application sources only. No test, fixture answer, evaluator asset, staged
# checkpoint, experiment output, workflow record, or repository history.
COPY pyproject.toml ./pyproject.toml
COPY src/unstuck ./src/unstuck

# Numeric, so no passwd entry is required and no user has to be created: this
# stage executes nothing, and the interpreter and the server need no name
# lookup for the account they run as.
USER 10001:10001

# Documents the private container boundary. EXPOSE publishes nothing.
EXPOSE 8080

# The required bind for an AgentCore HTTP agent, inside the unpublished
# container boundary only. `--factory` is what makes the configuration check
# fail closed: the factory validates every injected deployment input and
# raises before the socket is opened, so a misconfigured container never
# serves. The server and date headers are suppressed because a private
# container has no reason to advertise its implementation or its clock.
ENTRYPOINT ["python", "-m", "uvicorn", \
            "unstuck.cloud.agentcore_app:app_from_environment", "--factory", \
            "--host", "0.0.0.0", "--port", "8080", \
            "--no-server-header", "--no-date-header", \
            "--timeout-keep-alive", "5", "--log-level", "info"]
